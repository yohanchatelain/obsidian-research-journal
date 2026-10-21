"""Reviewable setup, preserving unrelated settings and existing hooks."""

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
import json
import os
from pathlib import Path
import re
import shlex
import sys
import tomllib
from uuid import uuid4

from .config import Config
from .hooks import INSTRUCTIONS
from .storage import Conflict, atomic_write, read

START = "# research-journal:begin"
FINISH = "# research-journal:end"
MD_START = "<!-- research-journal:instructions:begin -->"
MD_END = "<!-- research-journal:instructions:end -->"


def strip_block(text: str, start=START, end=FINISH) -> str:
    if start not in text and end not in text:
        return text
    if text.count(start) != 1 or text.count(end) != 1 or text.index(end) < text.index(start):
        raise Conflict("Invalid installation markers; preserve and repair the settings manually")
    return re.sub(re.escape(start) + r".*?" + re.escape(end) + r"\n?", "", text, flags=re.S)


def writable(path: Path) -> bool:
    candidate = path if path.exists() else path.parent
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    return os.access(candidate, os.W_OK)


@dataclass
class Change:
    path: Path
    before: str | None
    after: str
    operations: list = field(default_factory=list)
    executable: bool = False

    def review(self):
        # Only expose bridge-owned values, never a user's other configuration or credentials.
        return {"path": str(self.path), "changed": self.before != self.after,
                "writable": writable(self.path), "operations": self.operations}


def json_change(path: Path, values: list[tuple[list[str], object, bool]]) -> Change:
    before = read(path)
    data = json.loads(before) if before else {}
    operations = []
    for keys, value, append in values:
        target = data
        for key in keys[:-1]:
            target = target.setdefault(key, {})
            if not isinstance(target, dict):
                raise ValueError(f"Expected an object at {path}: {key}")
        key = keys[-1]
        if append:
            items = target.setdefault(key, [])
            if not isinstance(items, list):
                raise ValueError(f"Expected a hook list at {path}")
            if value not in items:
                items.append(deepcopy(value))
        else:
            if key in target and target[key] != value:
                raise Conflict(f"Existing bridge setting differs: {path}: {'.'.join(keys)}")
            target[key] = deepcopy(value)
        operations.append({"kind": "json-append" if append else "json-set", "keys": keys, "value": value})
    return Change(path, before, json.dumps(data, indent=2, ensure_ascii=False) + "\n", operations)


def plan(config: Config, config_path: Path, home: Path, runner: Path, python: str | None = None):
    home, config_path, runner = home.resolve(), config_path.resolve(), runner.resolve()
    python = python or sys.executable
    base = [python, str(runner), "--config", str(config_path)]
    changes = []
    config_before = read(config_path)
    if config_before and Config.load(config_path).to_dict() != config.to_dict():
        raise Conflict(f"Existing journal configuration differs: {config_path}; use that configuration or another path")
    changes.append(Change(config_path, config_before, json.dumps(config.to_dict(), indent=2) + "\n",
                          [{"kind": "keep", "description": "Journal configuration; retained on uninstall"}]))

    def command(client, event):
        return shlex.join([*base, "hook", "--client", client, "--event", event])

    # Append a bounded TOML block, then parse the full result before considering a write.
    path = home / ".codex/config.toml"
    original = read(path)
    clean = strip_block(original or "")
    parsed = tomllib.loads(clean)
    if "research-journal" in parsed.get("mcp_servers", {}):
        raise Conflict("A non-managed Codex research-journal server already exists")
    if parsed.get("features", {}).get("hooks") is False:
        raise Conflict("Codex hooks are explicitly disabled; enable them before installing automatic capture")
    block = f"{START}\n[mcp_servers.research-journal]\ncommand = {json.dumps(python)}\n"
    block += "args = " + json.dumps([*base[1:], "mcp", "--client", "codex"]) + "\n"
    for event in ("SessionStart", "UserPromptSubmit", "Stop", "Interrupt", "SessionEnd"):
        block += f"\n[[hooks.{event}]]\n[[hooks.{event}.hooks]]\ntype = \"command\"\n"
        block += f"command = {json.dumps(command('codex', event))}\n"
        block += f"timeout = {3 if event in {'SessionEnd', 'Interrupt'} else 10}\n"
    block += FINISH + "\n"
    after = clean.rstrip() + "\n\n" + block
    tomllib.loads(after)
    changes.append(Change(path, original, after, [{"kind": "text-block", "start": START, "end": FINISH,
                                                   "value": block}]))

    claude = [( ["mcpServers", "research-journal"],
                {"type": "stdio", "command": python,
                 "args": [*base[1:], "mcp", "--client", "claude"]}, False)]
    changes.append(json_change(home / ".claude.json", claude))
    hooks = []
    for event in ("SessionStart", "UserPromptSubmit", "Stop", "SessionEnd", "StopFailure"):
        hooks.append((["hooks", event], {"hooks": [{"type": "command", "command": command("claude", event),
                                                     "timeout": 3 if event == "SessionEnd" else 10}]}, True))
    changes.append(json_change(home / ".claude/settings.json", hooks))

    server = {"command": python, "args": [*base[1:], "mcp", "--client", "antigravity"]}
    changes.append(json_change(home / ".gemini/config/mcp_config.json",
                               [(["mcpServers", "research-journal"], server, False)]))
    # Existing legacy clients can still read this location. Keep both registrations identical.
    legacy = home / ".gemini/antigravity/mcp_config.json"
    if legacy.exists():
        changes.append(json_change(legacy, [(["mcpServers", "research-journal"], server, False)]))
    ag_hooks = {event: [{"type": "command", "command": command("antigravity", event), "timeout": 10}]
                for event in ("SessionStart", "PreInvocation", "Stop")}
    changes.append(json_change(home / ".gemini/config/hooks.json", [(["research-journal"], ag_hooks, False)]))

    for path in (home / ".codex/AGENTS.md", home / ".claude/CLAUDE.md", home / ".gemini/GEMINI.md"):
        before = read(path)
        content = strip_block(before or "", MD_START, MD_END).rstrip()
        owned = f"{MD_START}\n{INSTRUCTIONS.strip()}\n{MD_END}\n"
        changes.append(Change(path, before, content + ("\n\n" if content else "") + owned,
                              [{"kind": "text-block", "start": MD_START, "end": MD_END, "value": owned}]))
    launcher = "#!/bin/sh\nexec " + shlex.join(base) + ' "$@"\n'
    path = home / ".local/bin/research-journal"
    existing = read(path)
    if existing and existing != launcher:
        raise Conflict(f"A different command already exists: {path}")
    changes.append(Change(path, existing, launcher, [{"kind": "owned-file", "value": launcher}], True))
    return changes


def apply(changes: list[Change], config: Config):
    from .journal import Journal
    modified = [c for c in changes if c.before != c.after]
    manifest_path = config.state / "installation.json"
    blocked = [str(c.path) for c in modified if not writable(c.path)]
    for path in (config.state, config.vault):
        if not writable(path):
            blocked.append(str(path))
    if blocked:
        raise PermissionError("Setup needs write access to: " + ", ".join(blocked))
    for change in modified:
        if read(change.path) != change.before:
            raise Conflict(f"Settings changed since preview: {change.path}")
    backup = config.state / "backups" / (datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid4().hex[:8])
    backup.mkdir(parents=True, mode=0o700)
    manifest_before = read(manifest_path)
    previous = json.loads(manifest_before) if manifest_before else {"files": []}
    # Persist uninstall ownership before writing any settings, so interrupted setup is recoverable.
    ownership = {entry["path"]: entry for entry in previous["files"]}
    for change in changes:
        old = ownership.get(str(change.path), {})
        ownership[str(change.path)] = {"path": str(change.path), "operations": change.operations,
                                       "created": old.get("created", change.before is None)}
    atomic_write(manifest_path, json.dumps({"files": list(ownership.values())}, indent=2) + "\n", manifest_before)
    for index, change in enumerate(modified):
        if change.before is not None:
            destination = backup / f"{index:02d}-{change.path.name}"
            atomic_write(destination, change.before, None)
            destination.chmod(0o600)
        atomic_write(change.path, change.after, change.before)
        if change.executable:
            change.path.chmod(0o755)
    Journal(config).initialize()
    return {"installed": [str(c.path) for c in modified], "backups": str(backup),
            "home": str(config.vault / config.folder / "Home.md"),
            "next": "Restart assistants and inspect /mcp and /hooks; trust the new hooks if the client requests it."}


def uninstall(config: Config):
    path = config.state / "installation.json"
    before = read(path)
    if before is None:
        return {"removed": [], "retained": [], "message": "No installation manifest"}
    manifest = json.loads(before)
    removed, retained = [], []
    for entry in manifest["files"]:
        target = Path(entry["path"])
        original = read(target)
        if original is None:
            continue
        content = original
        for operation in entry["operations"]:
            kind = operation["kind"]
            if kind == "keep":
                retained.append(str(target))
                continue
            if kind == "owned-file":
                if content == operation["value"]:
                    content = ""
                else:
                    retained.append(str(target))
            elif kind == "text-block":
                # Preserve a user-edited owned block for manual review.
                if operation["value"] in content:
                    content = content.replace(operation["value"], "", 1)
                elif operation["start"] in content:
                    retained.append(str(target))
            elif kind.startswith("json-"):
                data = json.loads(content)
                current = data
                for key in operation["keys"][:-1]:
                    current = current.get(key, {})
                key = operation["keys"][-1]
                if kind == "json-append" and operation["value"] in current.get(key, []):
                    current[key].remove(operation["value"])
                elif kind == "json-set" and current.get(key) == operation["value"]:
                    del current[key]
                elif key in current and kind == "json-set":
                    retained.append(str(target))
                content = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
        if content != original:
            if not content.strip() and entry.get("created"):
                if read(target) != original:
                    raise Conflict(f"Settings changed during uninstall: {target}")
                target.unlink()
            else:
                atomic_write(target, content, original)
            removed.append(str(target))
    return {"removed": removed, "retained": sorted(set(retained)),
            "message": "Journal notes, task changes, configuration, ledger, and backups are retained."}
