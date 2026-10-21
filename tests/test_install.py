import json
from pathlib import Path
import subprocess
import tomllib
from unittest.mock import patch

import pytest

from research_journal.install import apply, plan, uninstall
from research_journal.storage import Conflict

RUNNER = Path(__file__).resolve().parents[1] / "bin/research-journal"


@pytest.fixture
def setup_home(tmp_path):
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex/config.toml").write_text(
        '# Keep my comment\nmodel = "my-model"\n'
        '[[hooks.SessionStart]]\n[[hooks.SessionStart.hooks]]\n'
        'type = "command"\ncommand = "existing-hook"\n'
        '[hooks.state]\nexample = "preserve"\n')
    (home / ".claude").mkdir()
    (home / ".claude/settings.json").write_text(json.dumps({
        "permissions": {"allow": ["Read"]}, "hooks": {"SessionStart": [
            {"hooks": [{"type": "command", "command": "existing-claude-hook"}]}]}}))
    (home / ".claude.json").write_text(json.dumps({"mcpServers": {"existing": {"command": "other"}}}))
    (home / ".gemini/antigravity").mkdir(parents=True)
    (home / ".gemini/antigravity/mcp_config.json").write_text('{"mcpServers": {"existing": {"command":"other"}}}')
    (home / ".codex/AGENTS.md").write_text("My existing instructions.\n")
    return home


def test_setup_preview_is_nonmutating_and_does_not_expose_unrelated_settings(journal, setup_home, tmp_path):
    config_path = tmp_path / "config.json"
    before = (setup_home / ".codex/config.toml").read_text()
    changes = plan(journal.config, config_path, setup_home, RUNNER)
    assert not config_path.exists()
    assert (setup_home / ".codex/config.toml").read_text() == before
    review = json.dumps([c.review() for c in changes])
    assert "existing-claude-hook" not in review
    assert "my-model" not in review
    assert "research-journal" in review


def test_setup_preserves_and_merges_all_three_clients(journal, setup_home, tmp_path):
    config_path = tmp_path / "config.json"
    changes = plan(journal.config, config_path, setup_home, RUNNER)
    result = apply(changes, journal.config)
    codex_text = (setup_home / ".codex/config.toml").read_text()
    codex = tomllib.loads(codex_text)
    assert "# Keep my comment" in codex_text
    assert codex["model"] == "my-model"
    assert len(codex["hooks"]["SessionStart"]) == 2
    assert codex["hooks"]["state"] == {"example": "preserve"}
    assert codex["mcp_servers"]["research-journal"]["args"][-1] == "codex"
    claude = json.loads((setup_home / ".claude/settings.json").read_text())
    assert claude["permissions"] == {"allow": ["Read"]}
    assert len(claude["hooks"]["SessionStart"]) == 2
    servers = json.loads((setup_home / ".claude.json").read_text())["mcpServers"]
    assert "existing" in servers and "research-journal" in servers
    agy = json.loads((setup_home / ".gemini/config/hooks.json").read_text())
    assert isinstance(agy["research-journal"]["Stop"][0]["command"], str)
    assert (setup_home / ".local/bin/research-journal").stat().st_mode & 0o111
    assert "My existing instructions." in (setup_home / ".codex/AGENTS.md").read_text()
    assert Path(result["backups"]).is_dir()


def test_doctor_accepts_extra_codex_hooks_and_detects_changed_owned_command(journal, setup_home, tmp_path):
    from research_journal.cli import doctor

    config_path = tmp_path / "config.json"
    apply(plan(journal.config, config_path, setup_home, RUNNER), journal.config)
    path = setup_home / ".codex/config.toml"
    text = path.read_text()
    entry = '\n[[hooks.SessionStart]]\n[[hooks.SessionStart.hooks]]\ntype = "command"\ncommand = "later-hook"\n'
    path.write_text(text + entry)

    def configured():
        return next(item["configured"] for item in doctor(journal.config, config_path)["integration_files"]
                    if item["path"] == str(path))

    assert configured()
    # Reformatting TOML must not invalidate a working setup.
    path.write_text((text + entry).replace('type = "command"', "type='command'"))
    assert configured()
    path.write_text((text + entry).replace("--event SessionStart", "--event NotSessionStart"))
    assert not configured()
    path.write_text(text + entry + '\n[mcp_servers.research-journal.env]\nEXAMPLE = "kept"\n')
    assert configured()


def test_doctor_toml_keeps_argument_order_and_disabled_state():
    from research_journal.cli import _configured_toml

    expected = {"mcp_servers": {"research-journal": {"args": ["mcp", "--client", "codex"]}}}
    assert not _configured_toml({"mcp_servers": {"research-journal": {"args": ["codex", "mcp", "--client"]}}}, expected)
    assert not _configured_toml({"mcp_servers": {"research-journal": {
        "args": ["mcp", "--client", "codex"], "enabled": False}}}, expected)


def test_repeated_install_has_no_duplicate_hooks(journal, setup_home, tmp_path):
    config_path = tmp_path / "config.json"
    apply(plan(journal.config, config_path, setup_home, RUNNER), journal.config)
    changes = plan(journal.config, config_path, setup_home, RUNNER)
    assert all(c.before == c.after for c in changes)
    apply(changes, journal.config)
    result = uninstall(journal.config)
    assert not (setup_home / ".local/bin/research-journal").exists()
    assert result["removed"]


def test_uninstall_preserves_later_settings_and_user_notes(journal, setup_home, tmp_path):
    config_path = tmp_path / "config.json"
    apply(plan(journal.config, config_path, setup_home, RUNNER), journal.config)
    codex = setup_home / ".codex/config.toml"
    codex.write_text(codex.read_text() + '\n[later]\nvalue = "preserve"\n')
    claude = setup_home / ".claude/settings.json"
    data = json.loads(claude.read_text())
    data["hooks"]["Stop"].append({"hooks": [{"type": "command", "command": "later-hook"}]})
    claude.write_text(json.dumps(data))
    journal.add_task("Keep my captured task")
    uninstall(journal.config)
    parsed = tomllib.loads(codex.read_text())
    assert parsed["later"]["value"] == "preserve"
    assert len(parsed["hooks"]["SessionStart"]) == 1
    assert "research-journal" not in parsed.get("mcp_servers", {})
    data = json.loads(claude.read_text())
    assert data["hooks"]["Stop"] == [{"hooks": [{"type": "command", "command": "later-hook"}]}]
    assert journal.search_tasks("Keep my captured task")
    assert config_path.exists()


def test_setup_preflight_prevents_partial_writes(journal, setup_home, tmp_path):
    config_path = tmp_path / "config.json"
    changes = plan(journal.config, config_path, setup_home, RUNNER)
    original = (setup_home / ".codex/config.toml").read_text()
    with patch("research_journal.install.writable", return_value=False):
        with pytest.raises(PermissionError):
            apply(changes, journal.config)
    assert not config_path.exists()
    assert (setup_home / ".codex/config.toml").read_text() == original


def test_setup_rejects_changes_after_preview(journal, setup_home, tmp_path):
    changes = plan(journal.config, tmp_path / "config.json", setup_home, RUNNER)
    path = setup_home / ".codex/config.toml"
    path.write_text(path.read_text() + "\n# edited after preview\n")
    with pytest.raises(Conflict):
        apply(changes, journal.config)


def test_setup_refuses_conflicting_server_registration(journal, setup_home, tmp_path):
    path = setup_home / ".claude.json"
    path.write_text('{"mcpServers":{"research-journal":{"command":"different"}}}')
    with pytest.raises(Conflict):
        plan(journal.config, tmp_path / "config.json", setup_home, RUNNER)


def test_installed_command_uses_custom_config_from_any_directory(journal, setup_home, tmp_path):
    config_path = tmp_path / "directory with spaces" / "config.json"
    apply(plan(journal.config, config_path, setup_home, RUNNER), journal.config)
    command = setup_home / ".local/bin/research-journal"
    result = subprocess.run([str(command), "status"], cwd=tmp_path, capture_output=True,
                            text=True, timeout=5)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["pending_writes"] == []
    codex = tomllib.loads((setup_home / ".codex/config.toml").read_text())
    hook = codex["hooks"]["Stop"][0]["hooks"][0]["command"]
    result = subprocess.run(hook, shell=True, input='{"session_id":"installed-test"}', cwd=tmp_path,
                            capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["decision"] == "block"
