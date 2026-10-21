"""Protocol contracts: authority, embargo, versioning, recovery and immutable inputs."""

import copy
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time

import pytest

from research_journal.config import Config
from research_journal.hooks import handle
from research_journal.reviews import Reviews
from research_journal.reviews.execution import normalize_spec, reviewer_spec, parse_native_result
from research_journal.reviews.store import fingerprint, identifier
from research_journal.server import Server, TOOLS, validate


@pytest.fixture
def reviews(journal, tmp_path):
    source = tmp_path / "artifact.txt"
    source.write_text("Initial artifact\n")
    result = Reviews(journal.config)
    result.source = source
    result.rid = result.create("Artifact review", source, "Check correctness")['review_id']
    return result


def invite_join(reviews, slot, client, suffix="", lease=600):
    rid = reviews.rid
    token = reviews.invite(rid, slot)["token"]
    session = slot + suffix + identifier()
    reviews.store.receipt(client, session)
    reviews.join(rid, token, client, session, True)
    claim = reviews.claim(rid, token, lease_seconds=lease)
    return token, claim["attempt_id"]


def critique(tag="a"):
    return {"summary": f"Private summary {tag}", "findings": [{"id": tag, "claim": "Claim " + tag,
        "concern": "A concern", "evidence": "artifact.txt:1", "recommendation": "Check the claim"}]}


def initial_round(reviews):
    reviews.start(reviews.rid)
    tokens = [invite_join(reviews, "reviewer-1", "codex"), invite_join(reviews, "reviewer-2", "claude")]
    for index, (token, attempt) in enumerate(tokens):
        reviews.submit(reviews.rid, token, attempt, 1, critique(str(index)), identifier())
    author = reviews.invite(reviews.rid, "", "author")["token"]
    return author, tokens


def respond(reviews, author):
    reviews.respond(reviews.rid, author, {f"reviewer-{index+1}:{index}": {
        "disposition": "accepted", "reason": "Revised with source evidence"} for index in range(2)}, identifier())


def test_full_two_round_revision_and_human_evidence(reviews):
    author, _ = initial_round(reviews)
    respond(reviews, author)
    reviews.source.write_text("Revised artifact\n")
    reviews.revise(reviews.rid, author, reviews.source)
    state = reviews.status(reviews.rid, owner=True)
    assert state["version"] == 2 and state["phase"] == "responding"
    reviews.follow_up(reviews.rid, author)
    for slot, client in [("reviewer-1", "codex"), ("reviewer-2", "claude")]:
        token, attempt = invite_join(reviews, slot, client, "follow-up")
        packet = reviews.packet(reviews.rid, token)
        assert "Revised artifact" in packet["files"]["artifact.txt"]
        assert len(packet["prior_reviews"]) == 2
        reviews.submit(reviews.rid, token, attempt, 2, {"summary": "Follow-up passed", "findings": []}, identifier())
    with pytest.raises(ValueError, match="Verification"):
        reviews.close(reviews.rid, "completed", "Reviewed", {"1:reviewer-1:0": {"status": "verified"}})
    reviews.close(reviews.rid, "completed", "Both reviewers checked revision", {
        "1:reviewer-1:0": {"status": "verified", "evidence": "artifact.txt version 2 line 1",
                           "explanation": "The revised statement fixes this finding", "version": 2}})
    state = reviews.status(reviews.rid, owner=True)
    assert state["phase"] == "closed" and state["outcome"] == "completed"
    assert state["resolutions"]["1:reviewer-2:1"] == {"status": "open"}
    with pytest.raises(ValueError, match="closed"):
        reviews.follow_up(reviews.rid, author)


def test_initial_embargo_covers_author_peer_packet_and_notes(reviews):
    rid = reviews.rid
    author = reviews.invite(rid, "", "author")["token"]
    reviews.start(rid)
    first, attempt = invite_join(reviews, "reviewer-1", "codex")
    second, _ = invite_join(reviews, "reviewer-2", "claude")
    reviews.request_check(rid, {"argv": ["true"], "timeout": 1}, token=first)
    reviews.submit(rid, first, attempt, 1, critique("SECRET"), "one")
    for token in (author, second):
        assert "SECRET" not in json.dumps(reviews.status(rid, token))
        assert "SECRET" not in json.dumps(reviews.packet(rid, token))
        assert not reviews.status(rid, token)["checks"]
    note = reviews.config.path(f"{reviews.config.folder}/Reviews/{rid}.md").read_text()
    assert "SECRET" not in note
    assert "SECRET" in json.dumps(reviews.status(rid, first))
    with pytest.raises(ValueError, match="reveal"):
        reviews.approve_check(rid, reviews.status(rid, owner=True)["checks"][0]["id"])
    reviews.missing(rid, "reviewer-2", "Owner confirmed unavailable")
    assert "SECRET" in json.dumps(reviews.status(rid, author))
    assert "SECRET" in reviews.config.path(f"{reviews.config.folder}/Reviews/{rid}.md").read_text()
    with pytest.raises(ValueError):
        reviews.close(rid, "completed", "Not enough reviews")
    reviews.close(rid, "incomplete", "One reviewer unavailable")


def test_duplicate_messages_and_stale_attempts(reviews):
    reviews.start(reviews.rid)
    token, attempt = invite_join(reviews, "reviewer-1", "codex")
    args = (reviews.rid, token, attempt, 1, critique(), "deduplicate")
    reviews.submit(*args)
    assert reviews.submit(*args)["duplicate"]
    with pytest.raises(ValueError, match="different content"):
        reviews.submit(reviews.rid, token, attempt, 1, critique("b"), "deduplicate")
    with pytest.raises(ValueError, match="stale"):
        reviews.submit(reviews.rid, token, attempt, 1, critique(), "new-message")
    with pytest.raises(ValueError):
        reviews.join(reviews.rid, token, "codex", "different", True)
    with pytest.raises(ValueError):
        reviews.invite(reviews.rid, "reviewer-1")


def test_freshness_and_client_guards(reviews):
    rid = reviews.rid
    token = reviews.invite(rid, "reviewer-1")["token"]
    reviews.start(rid)
    with pytest.raises(ValueError, match="receipt"):
        reviews.join(rid, token, "codex", "no-hook", True)
    reviews.store.receipt("codex", "resumed", fresh=False)
    with pytest.raises(ValueError, match="fresh"):
        reviews.join(rid, token, "codex", "resumed", True)
    reviews.store.receipt("codex", "new")
    with pytest.raises(ValueError, match="fresh"):
        reviews.join(rid, token, "codex", "new", False)
    with pytest.raises(ValueError, match="Client"):
        reviews.join(rid, token, "claude", "new", True)
    reviews.join(rid, token, "codex", "new", True)
    claim = reviews.claim(rid, token)
    # Joining again cannot reset a live lease and permit a second claim.
    reviews.join(rid, token, "codex", "new", True)
    with pytest.raises(ValueError, match="already claimed"):
        reviews.claim(rid, token)
    with pytest.raises(ValueError, match="live"):
        reviews.retry_assignment(rid, "reviewer-1")
    assert claim["attempt_id"]


def test_expired_lease_retry_invalidates_capability(reviews):
    reviews.start(reviews.rid)
    token, attempt = invite_join(reviews, "reviewer-1", "codex")
    def expire(r, db):
        r["assignments"][0]["expires"] = "2000-01-01T00:00:00+00:00"
    reviews._mutate(reviews.rid, "test_expiry", expire)
    with pytest.raises(ValueError, match="stale"):
        reviews.submit(reviews.rid, token, attempt, 1, critique(), identifier())
    reviews.retry_assignment(reviews.rid, "reviewer-1")
    with pytest.raises(ValueError, match="token"):
        reviews.status(reviews.rid, token)
    assert invite_join(reviews, "reviewer-1", "codex")[1] != attempt


def test_snapshot_freezes_worktree_and_explicit_untracked(reviews, tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / "tracked.txt").write_text("committed")
    subprocess.run(["git", "-C", str(root), "add", "tracked.txt"], check=True)
    (root / "tracked.txt").write_text("working tree")
    (root / "untracked.txt").write_text("explicit")
    (root / "excluded.txt").write_text("private")
    packet = reviews.packets.snapshot(root, include=["untracked.txt"])
    assert {e["path"] for e in packet["files"]} == {"tracked.txt", "untracked.txt"}
    (root / "tracked.txt").write_text("later")
    destination = tmp_path / "materialized"
    reviews.packets.materialize(packet, destination)
    assert (destination / "tracked.txt").read_text() == "working tree"
    altered = copy.deepcopy(packet)
    altered["files"][0]["path"] = "../escape"
    altered["hash"] = fingerprint({k: v for k, v in altered.items() if k != "hash"})
    with pytest.raises(ValueError):
        reviews.packets.verify(altered)
    (root / "link").symlink_to(reviews.source)
    with pytest.raises(ValueError, match="Symlinks"):
        reviews.packets.snapshot(root, include=["link"])
    with pytest.raises(ValueError):
        reviews.packets.snapshot(root, include=["../artifact.txt"])


def test_atomic_packet_object_writers(reviews):
    data = b"concurrent object" * 10000
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(reviews.packets.put, [data] * 20))
    assert len(set(results)) == 1 and reviews.packets.get(results[0]) == data


def test_checks_require_approval_and_bind_executor(reviews):
    rid = reviews.rid
    spec = {"argv": [sys.executable, "-c", "print('checked')"], "timeout": 10}
    check = reviews.request_check(rid, spec, owner=True)["check_id"]
    with pytest.raises(ValueError, match="approval"):
        reviews.run_check(rid, check, owner=True)
    reviews.approve_check(rid, check)
    reviews.config.review["executors"] = {"local": {"kind": "local", "resources": {"time": "1"}}}
    with pytest.raises(ValueError, match="no longer matches"):
        reviews.run_check(rid, check, owner=True)
    reviews.config.review.pop("executors")
    job = reviews.run_check(rid, check, owner=True)["job_id"]
    assert reviews.run_check(rid, check, owner=True)["job_id"] == job
    state = wait_jobs(reviews)
    assert state["jobs"][0]["status"] == "succeeded"
    assert state["jobs"][0]["result"]["stdout"] == "checked\n"
    reviews.start(rid)
    assert reviews.status(rid, owner=True)["baseline_evidence"] == [job]


def wait_jobs(reviews, timeout=8):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        reviews.poll(reviews.rid)
        state = reviews.status(reviews.rid, owner=True)
        if all(j["status"] in {"succeeded", "failed", "cancelled", "timed_out"} for j in state["jobs"]):
            return state
        time.sleep(0.03)
    pytest.fail("worker did not finish")


def test_revision_checks_are_distinct_and_sealed_for_follow_up(reviews):
    check = reviews.request_check(reviews.rid, {"argv": [sys.executable, "-c", "print(open('artifact.txt').read())"],
                                                "timeout": 5}, owner=True)["check_id"]
    reviews.approve_check(reviews.rid, check)
    reviews.run_check(reviews.rid, check, owner=True)
    wait_jobs(reviews)
    author, _ = initial_round(reviews)
    respond(reviews, author)
    reviews.source.write_text("version two")
    reviews.revise(reviews.rid, author, reviews.source)
    second = reviews.run_check(reviews.rid, check, token=author)["job_id"]
    with pytest.raises(ValueError, match="pending"):
        reviews.follow_up(reviews.rid, author)
    state = wait_jobs(reviews)
    assert [j["version"] for j in state["jobs"]] == [1, 2]
    assert state["jobs"][-1]["result"]["stdout"] == "version two\n"
    reviews.follow_up(reviews.rid, author)
    token, _ = invite_join(reviews, "reviewer-1", "codex")
    assert second in {j["id"] for j in reviews.packet(reviews.rid, token)["evidence"]}
    with pytest.raises(ValueError):
        reviews.run_check(reviews.rid, check, token=author)


@pytest.mark.parametrize("argv,timeout,expected", [
    ([sys.executable, "-c", "raise SystemExit(3)"], 3, "failed"),
    ([sys.executable, "-c", "import time; time.sleep(5)"], 0.15, "timed_out"),
    ([sys.executable, "-c", "print('x'*1100000)"], 3, "succeeded"),
])
def test_local_worker_terminal_states_and_log_bound(reviews, argv, timeout, expected):
    check = reviews.request_check(reviews.rid, {"argv": argv, "timeout": timeout}, owner=True)["check_id"]
    reviews.approve_check(reviews.rid, check)
    reviews.run_check(reviews.rid, check, owner=True)
    job = wait_jobs(reviews)["jobs"][0]
    assert job["status"] == expected
    assert len(job["result"]["stdout"].encode()) <= 1024 * 1024
    if "1100000" in argv[-1]:
        assert job["result"]["truncated"]["stdout"]


def test_bundle_round_trip_redacts_tokens_and_requires_reconciliation(reviews, tmp_path):
    reviews.start(reviews.rid)
    token, _ = invite_join(reviews, "reviewer-1", "codex")
    reviews.invite(reviews.rid, "", "author")
    bundle = tmp_path / "review.json"
    reviews.export_bundle(reviews.rid, bundle)
    content = bundle.read_text()
    assert token not in content and "token_hash" not in content and "author_token" not in content
    recovered = Reviews(Config(tmp_path / "new-vault", tmp_path / "new-state"))
    assert recovered.import_bundle(bundle)["paused"]
    assert recovered.import_bundle(bundle)["duplicate"]
    with pytest.raises(ValueError, match="paused"):
        recovered.invite(reviews.rid, "reviewer-1")
    recovered.reconcile(reviews.rid, resume=True)
    state = recovered.status(reviews.rid, owner=True)
    assert state["assignments"][0]["status"] == "pending"
    assert not state["paused"]
    assert recovered.invite(reviews.rid, "reviewer-1")["token"] != token
    tampered = json.loads(content)
    tampered["review"]["title"] = "tampered"
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(tampered))
    with pytest.raises(ValueError, match="corrupt"):
        recovered.import_bundle(bad)


def test_annotations_survive_projection_and_notes_do_not_drive_state(reviews):
    path = reviews.config.path(f"{reviews.config.folder}/Reviews/{reviews.rid}.md")
    path.write_text(path.read_text() + "\nMy annotation\n")
    reviews.start(reviews.rid)
    assert path.read_text().endswith("My annotation\n")
    path.write_text(path.read_text().replace("collecting", "closed"))
    assert reviews.status(reviews.rid, owner=True)["phase"] == "collecting"
    reviews.render(reviews.rid)
    assert "collecting" in path.read_text()


def test_mcp_has_no_owner_actions_and_binds_client(reviews, journal):
    assert {"review_approve", "review_close", "review_invite", "review_import", "review_retry"}.isdisjoint(TOOLS)
    server = Server(journal, "claude")
    server.ready = True
    token = reviews.invite(reviews.rid, "reviewer-1")["token"]
    response = server.dispatch({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
        "name": "review_status", "arguments": {"review_id": reviews.rid, "token": token}}})
    assert response["result"]["isError"]
    validate({"summary": "okay", "findings": []}, TOOLS["review_submit"][1]["properties"]["result"])
    with pytest.raises(ValueError):
        validate({"summary": "okay", "findings": ["bad"]}, TOOLS["review_submit"][1]["properties"]["result"])


def test_fresh_receipt_hook_and_worker_suppression(reviews, journal, monkeypatch):
    handle(journal, "codex", "SessionStart", {"session_id": "fresh", "source": "startup"})
    handle(journal, "codex", "SessionStart", {"session_id": "resume", "source": "resume"})
    with reviews.store.transaction() as db:
        assert json.loads(db.execute("SELECT data FROM receipts WHERE session_id='fresh'").fetchone()[0])["fresh"]
        assert not json.loads(db.execute("SELECT data FROM receipts WHERE session_id='resume'").fetchone()[0])["fresh"]
    handle(journal, "codex", "SessionStart", {"session_id": "fresh", "source": "resume"})
    with reviews.store.transaction() as db:
        assert not json.loads(db.execute("SELECT data FROM receipts WHERE session_id='fresh'").fetchone()[0])["fresh"]
    monkeypatch.setenv("RJ_REVIEW_RUN", "1")
    assert handle(journal, "codex", "SessionStart", {"session_id": "worker"}) == {}
    with reviews.store.transaction() as db:
        assert not db.execute("SELECT 1 FROM receipts WHERE session_id='worker'").fetchone()


@pytest.mark.parametrize("bad", [
    {"argv": "echo hi", "timeout": 1}, {"argv": ["true"], "timeout": True},
    {"argv": ["true"]}, {"argv": ["true"], "timeout": 1, "cwd": "../escape"},
    {"argv": ["true"], "timeout": 1, "env": {"API_KEY": "secret"}},
    {"argv": ["true"], "timeout": 1, "shell": True},
])
def test_invalid_check_spec_rejected(bad):
    with pytest.raises(ValueError):
        normalize_spec(bad)
