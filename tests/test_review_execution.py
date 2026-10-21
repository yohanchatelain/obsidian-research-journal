"""Execution contracts with real local children and fake native/scheduler boundaries."""

import base64
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time

import pytest

from research_journal.reviews import Reviews
from research_journal.reviews import worker
from research_journal.reviews.execution import Transport, reviewer_spec, parse_native_result, RESULT_SCHEMA
from research_journal.reviews.store import identifier
from test_reviews import reviews, wait_jobs, invite_join, critique


def completed(stdout="", returncode=0, stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


@pytest.fixture
def staged(tmp_path):
    request = {"root": str(tmp_path / "runs"), "attempt": identifier(), "backend": "process",
               "packet_hash": "a" * 64, "spec": {"argv": [sys.executable, "-c", "print('okay')"], "timeout": 3},
               "files": [], "worker_source": Path(worker.__file__).read_text()}
    worker.stage(request)
    return request


def test_worker_launch_is_idempotent_and_launch_intent_prevents_duplicate(staged):
    first = worker.launch(staged)
    assert worker.launch(staged)["pid"] == first["pid"]
    until = time.monotonic() + 5
    while time.monotonic() < until:
        result = worker.status(staged)
        if result["state"] == "succeeded":
            break
        time.sleep(0.03)
    assert result["stdout"] == "okay\n"
    fresh = {**staged, "attempt": identifier()}
    worker.stage(fresh)
    directory = worker.attempt_dir(fresh)
    worker.write_json(directory / "launch.intent", {})
    assert worker.launch(fresh)["state"] == "unknown"
    assert not (directory / "handle.json").exists()


def test_stage_verifies_inputs_and_never_executes(staged):
    assert worker.stage(staged)["state"] == "staged"
    changed = {**staged, "spec": {"argv": ["different"], "timeout": 3}}
    with pytest.raises(ValueError, match="other inputs"):
        worker.stage(changed)
    fresh = {**staged, "attempt": identifier(), "files": [{"path": "../escape", "content": "", "sha256": "a" * 64}]}
    with pytest.raises(ValueError):
        worker.stage(fresh)
    assert not (worker.attempt_dir(staged) / "result.json").exists()


def test_slurm_submit_then_accounting_success_requires_result(staged, monkeypatch):
    staged["backend"] = "slurm"
    staged["resources"] = {"time": "00:01:00", "cpus-per-task": 1}
    seen = []
    name = f"rj-{staged['attempt']}"
    def fake(argv, **kwargs):
        seen.append(argv)
        if argv[0] == "sbatch":
            return completed("123;cluster\n")
        if argv[0] == "sacct":
            return completed(f"COMPLETED|0:0|{name}\n")
        raise AssertionError(argv)
    monkeypatch.setattr(worker.subprocess, "run", fake)
    handle = worker.launch(staged)
    assert handle["state"] == "queued" and handle["job_id"] == "123"
    assert "--cpus-per-task=1" in seen[0]
    assert worker.status(staged)["state"] == "unknown"
    worker.write_json(worker.attempt_dir(staged) / "result.json", {"state": "succeeded", "exit_code": 0})
    assert worker.status(staged)["state"] == "succeeded"


@pytest.mark.parametrize("scheduler,expected", [("PENDING", "queued"), ("RUNNING", "running"),
                                               ("CANCELLED by 3", "cancelled"), ("TIMEOUT", "failed")])
def test_slurm_state_contract(staged, monkeypatch, scheduler, expected):
    staged["backend"] = "slurm"
    worker.write_json(worker.attempt_dir(staged) / "handle.json", {"job_id": "123", "backend": "slurm"})
    monkeypatch.setattr(worker.subprocess, "run", lambda *args, **kwargs:
                        completed(f"{scheduler}|1:0|rj-{staged['attempt']}\n"))
    assert worker.status(staged)["state"] == expected


def test_slurm_ambiguous_submission_reconciles_without_resubmitting(staged, monkeypatch):
    staged["backend"] = "slurm"
    calls = []
    def fake(argv, **kwargs):
        calls.append(argv[0])
        if argv[0] == "sbatch":
            return completed("", returncode=1, stderr="connection lost after submission")
        if argv[0] == "squeue":
            return completed("432\n")
        if "--name" in argv:
            return completed("432\n")
        return completed(f"RUNNING|0:0|rj-{staged['attempt']}\n")
    monkeypatch.setattr(worker.subprocess, "run", fake)
    assert worker.launch(staged)["state"] == "unknown"
    assert worker.launch(staged)["state"] == "unknown"
    assert calls.count("sbatch") == 1
    assert worker.status(staged)["job_id"] == "432"
    assert calls.count("sbatch") == 1


def test_cancel_slurm_requires_matching_unique_name(staged, monkeypatch):
    staged["backend"] = "slurm"
    worker.write_json(worker.attempt_dir(staged) / "handle.json", {"job_id": "123", "backend": "slurm"})
    calls = []
    def fake(argv, **kwargs):
        calls.append(argv)
        return completed("someone-elses-job\n" if argv[0] == "squeue" else "")
    monkeypatch.setattr(worker.subprocess, "run", fake)
    assert worker.cancel(staged)["state"] == "unknown"
    assert not any(argv[0] == "scancel" for argv in calls)
    calls.clear()
    monkeypatch.setattr(worker.subprocess, "run", lambda argv, **kwargs:
                        (calls.append(argv) or completed(f"rj-{staged['attempt']}\n")))
    assert worker.cancel(staged)["state"] == "cancelling"
    assert calls[-1] == ["scancel", "123"]


def test_ssh_passes_command_argv_as_json_not_remote_shell(monkeypatch, tmp_path):
    calls = []
    def fake(argv, **kwargs):
        calls.append((argv, kwargs))
        return completed('{"state":"running"}')
    monkeypatch.setattr(subprocess, "run", fake)
    request = {"operation": "stage", "spec": {"argv": ["echo", "$(touch /tmp/not-run); `bad`"]}}
    Transport(tmp_path).call({"kind": "ssh", "host": "lab-cluster", "root": "~/reviews"}, request)
    argv, kwargs = calls[0]
    assert argv[:6] == ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "lab-cluster"]
    assert "touch /tmp" not in argv[-1]
    assert json.loads(kwargs["input"]) == request


@pytest.mark.parametrize("client", ["codex", "claude", "antigravity"])
def test_native_adapters_structured_flags_and_result_parse(client):
    help_text = "--ignore-user-config --ignore-rules --ephemeral --output-schema --safe-mode --restricted --strict-mcp-config --tools " \
                "--json-schema --no-session-persistence --new-project --sandbox --mode --print"
    spec, extras = reviewer_spec({"client": client}, "independent packet", help_text)
    assert spec["env"]["RJ_REVIEW_RUN"] == "1"
    assert spec["timeout"] == 600
    assert extras[0]["path"] == ".rj-control/schema.json"
    value = {"summary": "Reviewed", "findings": []}
    if client == "codex":
        evidence = {"artifacts": [{"path": ".rj-control/result.json", "content":
                                    base64.b64encode(json.dumps(value).encode()).decode()}]}
        assert "read-only" in spec["argv"] and spec["stdin"] == "independent packet"
    else:
        evidence = {"stdout": json.dumps({"structured_output": value, "model": "fake-model"})}
    parsed, provenance = parse_native_result(client, evidence)
    assert parsed == value and provenance["client"] == client
    with pytest.raises(ValueError, match="lacks"):
        reviewer_spec({"client": client}, "packet", "old version help")


def test_automatic_reviewers_run_fresh_processes_and_reveal_together(reviews, tmp_path):
    binary = tmp_path / "fake-reviewer"
    binary.write_text('#!' + sys.executable + '\n' + '''import json, os, pathlib, sys
if '--version' in sys.argv:
    print('fake-reviewer 1.0')
elif '--help' in sys.argv:
    print('--safe-mode --restricted --strict-mcp-config --tools --json-schema --no-session-persistence')
else:
    prompt = sys.stdin.read()
    print('DO_NOT_COPY_CONVERSATION', file=sys.stderr)
    assert os.environ['RJ_REVIEW_RUN'] == '1'
    assert 'Private summary' not in prompt
    print(json.dumps({'structured_output': {'summary': 'Fresh fake review', 'findings': []},
                      'model': 'fake-only', 'usage': {'input_tokens': 1}}))
''')
    binary.chmod(0o755)
    rid = reviews.create("Automatic", reviews.source, "Correctness", participants=[
        {"slot": "one", "client": "claude", "mode": "automatic", "binary": str(binary)},
        {"slot": "two", "client": "claude", "mode": "automatic", "binary": str(binary)}])["review_id"]
    reviews.rid = rid
    reviews.start(rid)
    reviews.run_reviewers(rid)
    reviews.run_reviewers(rid)  # Does not duplicate reserved assignments.
    state = wait_jobs(reviews)
    assert state["phase"] == "responding" and len(state["jobs"]) == 2
    assert all(a["status"] == "submitted" for a in state["assignments"])
    assert all(a["provenance"]["model"] == "fake-only" for a in state["assignments"])
    assert reviews.source.read_text() == "Initial artifact\n"
    assert "DO_NOT_COPY_CONVERSATION" not in json.dumps(state)
    for job in state["jobs"]:
        persisted = reviews.store.root / "runs" / job["id"] / "result.json"
        assert "DO_NOT_COPY_CONVERSATION" not in persisted.read_text()


def test_automatic_output_cannot_overwrite_human_missing_decision(reviews):
    reviews.start(reviews.rid)
    token, attempt = invite_join(reviews, "reviewer-1", "codex")
    def prepare(r, db):
        a = r["assignments"][0]
        a["profile"].update(mode="automatic", client="claude")
        r["jobs"].append({"id": attempt, "kind": "reviewer", "assignment_id": a["id"], "version": 1,
                          "status": "running", "profile": a["profile"], "executor": {"kind": "local"}, "spec": {}})
    reviews._mutate(reviews.rid, "fixture", prepare)
    reviews.missing(reviews.rid, "reviewer-1", "Abandoned")
    with reviews.store.transaction() as db:
        packet_hash = reviews.store.load(db, reviews.rid)["packets"]["1"]["hash"]
    reviews._record_job(reviews.rid, attempt, {"state": "succeeded", "stdout": json.dumps(critique()),
        "packet_hash": packet_hash, "spec_hash": hashlib.sha256(b"{}").hexdigest()})
    state = reviews.status(reviews.rid, owner=True)
    assert state["assignments"][0]["status"] == "missing" and state["jobs"][0]["late"]
    reviews.close(reviews.rid, "incomplete", "Stopped after missing result")
    assert reviews.status(reviews.rid, owner=True)["phase"] == "closed"


def test_unknown_attempts_require_evidence_and_never_auto_retry(reviews):
    class LostTransport:
        calls = []
        def call(self, profile, request):
            self.calls.append(request["operation"])
            raise OSError("Transport lost")
    reviews._transport = LostTransport()
    check = reviews.request_check(reviews.rid, {"argv": ["true"], "timeout": 1}, owner=True)["check_id"]
    reviews.approve_check(reviews.rid, check)
    job = reviews.run_check(reviews.rid, check, owner=True)["job_id"]
    assert reviews.status(reviews.rid, owner=True)["jobs"][0]["status"] == "unknown"
    with pytest.raises(ValueError):
        reviews.retry_check(reviews.rid, job)
    reviews.poll(reviews.rid)
    assert reviews._transport.calls == ["stage", "status"]
    with pytest.raises(ValueError, match="evidence"):
        reviews.reconcile(reviews.rid, decisions={job: {"state": "failed"}})
    reviews.reconcile(reviews.rid, decisions={job: {"state": "failed", "evidence": "Verified no worker or scheduler job",
                                                  "reason": "Launch did not occur"}})
    assert reviews.retry_check(reviews.rid, job)["job_id"] != job


def test_cancel_running_local_attempt(reviews):
    check = reviews.request_check(reviews.rid, {"argv": [sys.executable, "-c", "import time; time.sleep(10)"],
                                               "timeout": 12}, owner=True)["check_id"]
    reviews.approve_check(reviews.rid, check)
    job = reviews.run_check(reviews.rid, check, owner=True)["job_id"]
    # Wait for the helper to install its signal handler and record the child handle.
    child = reviews.store.root / "runs" / job / "child.json"
    until = time.monotonic() + 3
    while not child.exists() and time.monotonic() < until:
        time.sleep(0.02)
    assert child.exists()
    reviews.cancel_job(reviews.rid, job)
    assert wait_jobs(reviews)["jobs"][0]["status"] == "cancelled"


def test_automatic_check_proposal_is_embargoed_and_requires_human_approval(reviews):
    reviews.start(reviews.rid)
    token, attempt = invite_join(reviews, "reviewer-1", "codex")
    def prepare(r, db):
        a = r["assignments"][0]
        a["profile"].update(mode="automatic", client="claude")
        r["jobs"].append({"id": attempt, "kind": "reviewer", "assignment_id": a["id"], "version": 1,
                          "status": "running", "profile": a["profile"], "executor": {"kind": "local"}, "spec": {}})
    reviews._mutate(reviews.rid, "fixture", prepare)
    with reviews.store.transaction() as db:
        packet_hash = reviews.store.load(db, reviews.rid)["packets"]["1"]["hash"]
    result = {"summary": "Needs experiment", "findings": [], "check_requests": [
        {"argv": ["true"], "timeout": 1, "executor": "local", "reason": "Distinguish two hypotheses"}]}
    reviews._record_job(reviews.rid, attempt, {"state": "succeeded", "stdout": json.dumps(result),
        "packet_hash": packet_hash, "spec_hash": hashlib.sha256(b"{}").hexdigest()})
    author = reviews.invite(reviews.rid, "", "author")["token"]
    assert not reviews.status(reviews.rid, author)["checks"]
    reviews.missing(reviews.rid, "reviewer-2", "Unavailable")
    checks = reviews.status(reviews.rid, author)["checks"]
    assert len(checks) == 1 and checks[0]["approval"] is None
    with pytest.raises(ValueError, match="approval"):
        reviews.run_check(reviews.rid, checks[0]["id"], token=author)
    reviews.approve_check(reviews.rid, checks[0]["id"])
    assert reviews.run_check(reviews.rid, checks[0]["id"], token=author)["job_id"]
    wait_jobs(reviews)


def test_success_requires_integrity_evidence(reviews):
    class FakeTransport:
        def call(self, profile, request):
            return {"state": "staged" if request["operation"] == "stage" else "running"}
    reviews._transport = FakeTransport()
    check = reviews.request_check(reviews.rid, {"argv": ["true"], "timeout": 1}, owner=True)["check_id"]
    reviews.approve_check(reviews.rid, check)
    job = reviews.run_check(reviews.rid, check, owner=True)["job_id"]
    with pytest.raises(ValueError, match="integrity evidence"):
        reviews._record_job(reviews.rid, job, {"state": "succeeded"})
    with pytest.raises(ValueError, match="different packet"):
        reviews._record_job(reviews.rid, job, {"state": "succeeded", "packet_hash": "0" * 64, "spec_hash": "0" * 64})
    assert reviews.status(reviews.rid, owner=True)["jobs"][0]["status"] == "running"
