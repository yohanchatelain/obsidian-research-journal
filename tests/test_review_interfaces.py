"""CLI/MCP journeys and concurrency around durable transitions."""

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sys

import pytest

from research_journal.cli import main
from research_journal.reviews import Reviews
from research_journal.server import Server
from research_journal.reviews.store import identifier
from test_reviews import reviews, invite_join, initial_round, respond, wait_jobs


def call(server, name, args):
    return server.dispatch({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                            "params": {"name": name, "arguments": args}})["result"]


def test_mcp_participant_journey_and_schema_errors(reviews, journal):
    rid = reviews.rid
    reviews.start(rid)
    token = reviews.invite(rid, "reviewer-1")["token"]
    reviews.store.receipt("codex", "fresh-interface")
    server = Server(journal, "codex")
    server.ready = True
    args = {"review_id": rid, "token": token}
    assert not call(server, "review_join", {**args, "session_id": "fresh-interface", "fresh_context": True})["isError"]
    claim = json.loads(call(server, "review_claim", args)["content"][0]["text"])
    assert not call(server, "review_get_packet", args)["isError"]
    bad = call(server, "review_run_check", {**args, "check_id": "fake", "owner": True})
    assert bad["isError"] and "unknown fields" in bad["content"][0]["text"]
    assert not call(server, "review_submit", {**args, "attempt_id": claim["attempt_id"], "version": 1,
                   "message_id": "mcp-submit", "result": {"summary": "Checked", "findings": []}})["isError"]


def test_cli_create_check_watch_and_export(journal, tmp_path, capsys):
    config = tmp_path / "config.json"
    config.write_text(json.dumps(journal.config.to_dict()))
    artifact = tmp_path / "data.txt"
    artifact.write_text("Reviewable artifact")
    spec = tmp_path / "review.json"
    spec.write_text(json.dumps({"title": "CLI review", "source": str(artifact), "criteria": "Correctness"}))
    def cli(*args):
        assert main(["--config", str(config), "review", *map(str, args)]) == 0
        return json.loads(capsys.readouterr().out)
    rid = cli("create", spec)["review_id"]
    check_spec = tmp_path / "check.json"
    check_spec.write_text(json.dumps({"argv": [sys.executable, "-c", "print('verified')"], "timeout": 3}))
    check = cli("check", "propose", rid, check_spec)["check_id"]
    cli("check", "approve", rid, check)
    cli("check", "run", rid, check)
    result = cli("watch", rid, "--interval", "0.1", "--timeout", "5")
    assert result["jobs"][0]["status"] == "succeeded"
    assert cli("start", rid)["phase"] == "collecting"
    assert cli("status", rid)["phase"] == "collecting"
    bundle = tmp_path / "bundle.json"
    assert cli("export", rid, bundle)["path"] == str(bundle)
    cli("cancel", rid, "--reason", "CLI test ended")
    assert cli("status", rid)["outcome"] == "cancelled"


def test_single_lease_under_concurrent_claims(reviews):
    reviews.start(reviews.rid)
    token = reviews.invite(reviews.rid, "reviewer-1")["token"]
    reviews.store.receipt("codex", "concurrent")
    reviews.join(reviews.rid, token, "codex", "concurrent", True)
    def attempt(_):
        try:
            return Reviews(reviews.config).claim(reviews.rid, token)
        except ValueError as error:
            return str(error)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(attempt, range(4)))
    assert sum(isinstance(result, dict) and "attempt_id" in result for result in results) == 1
    assert sum("already claimed" in result for result in results if isinstance(result, str)) == 3


def test_check_transport_calls_do_not_hold_review_write_lock(reviews):
    class ConcurrentObserver:
        def call(self, profile, request):
            with ThreadPoolExecutor(max_workers=1) as pool:
                state = pool.submit(Reviews(reviews.config).status, reviews.rid, owner=True).result(timeout=2)
            assert state["phase"] == "preparing"
            return {"state": "staged" if request["operation"] == "stage" else "unknown"}
    reviews._transport = ConcurrentObserver()
    check = reviews.request_check(reviews.rid, {"argv": ["true"], "timeout": 1}, owner=True)["check_id"]
    reviews.approve_check(reviews.rid, check)
    reviews.run_check(reviews.rid, check, owner=True)


def test_setup_script_changes_require_new_approval(reviews, tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    import subprocess
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    script = root / "setup.sh"
    script.write_text("export RESEARCH_CHECK_VALUE=first\n")
    (root / "artifact.txt").write_text("artifact")
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    reviews.rid = reviews.create("Setup pin", root, "Correctness")["review_id"]
    check = reviews.request_check(reviews.rid, {"argv": [sys.executable, "-c", "import os; print(os.environ['RESEARCH_CHECK_VALUE'])"],
        "timeout": 3, "setup": "setup.sh"}, owner=True)["check_id"]
    reviews.approve_check(reviews.rid, check)
    reviews.run_check(reviews.rid, check, owner=True)
    assert wait_jobs(reviews)["jobs"][0]["result"]["stdout"] == "first\n"
    author, _ = initial_round(reviews)
    respond(reviews, author)
    script.write_text("export RESEARCH_CHECK_VALUE=second\n")
    reviews.revise(reviews.rid, author, root)
    with pytest.raises(ValueError, match="no longer matches"):
        reviews.run_check(reviews.rid, check, owner=True)
    reviews.approve_check(reviews.rid, check)
    reviews.run_check(reviews.rid, check, owner=True)
    assert wait_jobs(reviews)["jobs"][-1]["result"]["stdout"] == "second\n"


def test_follow_up_without_revision_and_one_response_only(reviews):
    author, _ = initial_round(reviews)
    respond(reviews, author)
    with pytest.raises(ValueError):
        respond(reviews, author)
    reviews.follow_up(reviews.rid, author)
    assert reviews.status(reviews.rid, owner=True)["version"] == 1
    with pytest.raises(ValueError):
        reviews.revise(reviews.rid, author, reviews.source)


def test_broken_note_is_projection_error_not_rollback(reviews):
    note = reviews.config.path(f"{reviews.config.folder}/Reviews/{reviews.rid}.md")
    note.write_text(note.read_text().replace("<!-- research-journal:end -->", ""))
    result = reviews.start(reviews.rid)
    assert "projection_error" in result
    assert reviews.status(reviews.rid, owner=True)["phase"] == "collecting"


def test_completed_closure_cannot_skip_follow_up(reviews):
    initial_round(reviews)
    with pytest.raises(ValueError, match="both rounds"):
        reviews.close(reviews.rid, "completed", "Not actually complete")
