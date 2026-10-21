"""Human coordinator commands and optional manual participant operations."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import time

from .coordinator import Reviews
from .execution import FINISHED, reviewer_spec


def add_parser(commands):
    group = commands.add_parser("review", help="Coordinate durable two-reviewer rounds; see docs/reviews.md")
    sub = group.add_subparsers(dest="review_command", required=True)
    create = sub.add_parser("create", help="Freeze inputs from a JSON specification")
    create.add_argument("file", type=Path)
    sub.add_parser("doctor", help="Probe reviewer help and executor profiles without inference or remote calls")
    load = sub.add_parser("import", help="Import a verified recovery bundle in paused state")
    load.add_argument("file", type=Path)
    status = sub.add_parser("status")
    status.add_argument("review_id", nargs="?")
    status.add_argument("--token", default=os.environ.get("RJ_REVIEW_TOKEN"))
    for name in ("start", "run", "poll", "refresh"):
        sub.add_parser(name).add_argument("review_id")
    watch = sub.add_parser("watch", help="Poll in the foreground; stopping the watcher leaves jobs durable")
    watch.add_argument("review_id")
    watch.add_argument("--interval", type=float, default=2)
    watch.add_argument("--timeout", type=float, default=60)
    invite = sub.add_parser("invite")
    invite.add_argument("review_id")
    invite.add_argument("slot", nargs="?", default="")
    invite.add_argument("--role", choices=["reviewer", "author"], default="reviewer")
    for name in ("missing", "retry"):
        command = sub.add_parser(name)
        command.add_argument("review_id")
        command.add_argument("slot")
        if name == "missing":
            command.add_argument("--reason", required=True)
    for name in ("join", "claim", "packet", "submit", "respond", "revise", "follow-up"):
        command = sub.add_parser(name)
        command.add_argument("review_id")
        command.add_argument("--token", default=os.environ.get("RJ_REVIEW_TOKEN"))
        if name == "join":
            command.add_argument("--client", choices=["codex", "claude", "antigravity"], required=True)
            command.add_argument("--session-id", required=True)
            command.add_argument("--fresh-context", action="store_true")
        if name == "claim":
            command.add_argument("--lease-seconds", type=int, default=600)
        if name in {"submit", "respond"}:
            command.add_argument("file", type=Path)
            command.add_argument("--message-id", required=True)
        if name == "submit":
            command.add_argument("--attempt-id", required=True)
            command.add_argument("--version", type=int, required=True)
        if name == "revise":
            command.add_argument("source")
            command.add_argument("--include", action="append", default=[])
    close = sub.add_parser("close")
    close.add_argument("review_id")
    close.add_argument("--outcome", choices=["completed", "incomplete", "cancelled", "failed"], required=True)
    close.add_argument("--rationale", required=True)
    close.add_argument("--resolutions", type=Path)
    cancel = sub.add_parser("cancel")
    cancel.add_argument("review_id")
    cancel.add_argument("--job")
    cancel.add_argument("--reason", default="Cancelled by owner")
    export = sub.add_parser("export")
    export.add_argument("review_id")
    export.add_argument("file", type=Path)
    reconcile = sub.add_parser("reconcile")
    reconcile.add_argument("review_id")
    reconcile.add_argument("--decisions", type=Path, help="job-id: {state: failed/cancelled, evidence, reason}")
    reconcile.add_argument("--resume", action="store_true")
    check = sub.add_parser("check")
    checks = check.add_subparsers(dest="check_command", required=True)
    for name in ("propose", "approve", "run", "retry"):
        command = checks.add_parser(name)
        command.add_argument("review_id")
        command.add_argument("target", help="JSON spec path, check ID, or failed job ID")


def load(path):
    return json.loads(Path(path).read_text())


def doctor(reviews):
    profiles = list(reviews.config.review.get("reviewers", {}).values()) or [
        {"client": "codex", "mode": "manual"}, {"client": "claude", "mode": "manual"}]
    result = {"reviewers": [], "executors": {}, "remote_execution": "unverified; no remote commands launched"}
    for profile in profiles:
        client = profile["client"]
        binary = profile.get("binary", {"codex": "codex", "claude": "claude", "antigravity": "agy"}[client])
        entry = {"client": client, "mode": profile.get("mode", "manual"), "binary": shutil.which(binary),
                 "inference": "unverified; no model call launched"}
        try:
            probe = subprocess.run([binary, *(["exec"] if client == "codex" else []), "--help"],
                                   capture_output=True, text=True, timeout=10)
            reviewer_spec(profile, "preflight only", probe.stdout + probe.stderr)
            entry["automatic_preflight"] = "passed" if probe.returncode == 0 else "failed"
        except (ValueError, OSError, subprocess.SubprocessError) as error:
            entry["automatic_preflight"] = str(error)
        result["reviewers"].append(entry)
    for name in {"local", *reviews.config.review.get("executors", {})}:
        try:
            result["executors"][name] = reviews._executor(name)
        except ValueError as error:
            result["executors"][name] = {"error": str(error)}
    return result


def run(config, args):
    reviews = Reviews(config)
    rid = getattr(args, "review_id", None)
    name = args.review_command
    if hasattr(args, "token") and name != "status" and not args.token:
        raise ValueError("Supply --token or RJ_REVIEW_TOKEN from an invitation")
    if name == "create":
        return reviews.create(**load(args.file))
    if name == "import":
        return reviews.import_bundle(args.file)
    if name == "doctor":
        return doctor(reviews)
    if name == "status":
        return reviews.status(rid, args.token, owner=not args.token)
    if name == "refresh":
        return reviews.refresh(rid)
    if name == "start":
        return reviews.start(rid)
    if name == "run":
        return reviews.run_reviewers(rid)
    if name == "poll":
        reviews.poll(rid)
        return reviews.status(rid, owner=True)
    if name == "watch":
        if args.interval < 0.1 or args.timeout <= 0:
            raise ValueError("Watch interval must be >= 0.1 seconds and timeout positive")
        deadline = time.monotonic() + args.timeout
        while True:
            reviews.poll(rid)
            state = reviews.status(rid, owner=True)
            if all(j["status"] in FINISHED for j in state["jobs"]) or time.monotonic() >= deadline:
                return state
            time.sleep(min(args.interval, max(0, deadline - time.monotonic())))
    if name == "invite":
        return reviews.invite(rid, args.slot, args.role)
    if name == "missing":
        return reviews.missing(rid, args.slot, args.reason)
    if name == "retry":
        return reviews.retry_assignment(rid, args.slot)
    if name == "join":
        return reviews.join(rid, args.token, args.client, args.session_id, args.fresh_context)
    if name == "claim":
        return reviews.claim(rid, args.token, lease_seconds=args.lease_seconds)
    if name == "packet":
        return reviews.packet(rid, args.token)
    if name == "submit":
        return reviews.submit(rid, args.token, args.attempt_id, args.version, load(args.file), args.message_id)
    if name == "respond":
        return reviews.respond(rid, args.token, load(args.file), args.message_id)
    if name == "revise":
        return reviews.revise(rid, args.token, args.source, include=args.include)
    if name == "follow-up":
        return reviews.follow_up(rid, args.token)
    if name == "close":
        return reviews.close(rid, args.outcome, args.rationale, load(args.resolutions) if args.resolutions else None)
    if name == "cancel":
        if args.job:
            return reviews.cancel_job(rid, args.job)
        for job in reviews.status(rid, owner=True)["jobs"]:
            if job["status"] not in FINISHED:
                reviews.cancel_job(rid, job["id"])
        return reviews.close(rid, "cancelled", args.reason)
    if name == "export":
        return reviews.export_bundle(rid, args.file)
    if name == "reconcile":
        return reviews.reconcile(rid, decisions=load(args.decisions) if args.decisions else None, resume=args.resume)
    if name == "check":
        if args.check_command == "propose":
            return reviews.request_check(rid, load(args.target), owner=True)
        if args.check_command == "approve":
            return reviews.approve_check(rid, args.target)
        if args.check_command == "run":
            return reviews.run_check(rid, args.target, owner=True)
        return reviews.retry_check(rid, args.target)
    raise ValueError("Unknown review command")
