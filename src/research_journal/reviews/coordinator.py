"""Coordinator-owned review lifecycle. Inference and execution never run in a transaction."""

import hashlib
import json
import secrets
import re
from datetime import datetime, timedelta, timezone

from .packets import Packets
from .store import Store, encode, fingerprint, identifier, now
from ..storage import Conflict
from .execution import Execution
from .bundles import Bundles

TERMINAL = {"submitted", "failed", "missing"}
ACTIVE_PHASES = {"collecting", "follow_up"}


class Reviews(Execution, Bundles):
    def __init__(self, config):
        self.config = config
        self.store = Store(config.state)
        self.packets = Packets(self.store.root)

    def _mutate(self, review_id, kind, action, *, actor="owner", message_id=None, payload=None):
        if message_id is not None and (not isinstance(message_id, str) or not message_id.strip() or len(message_id) > 200):
            raise ValueError("Message ID must be a nonempty string of at most 200 characters")
        with self.store.transaction() as db:
            review = self.store.load(db, review_id)
            if message_id:
                old = db.execute("SELECT data FROM events WHERE message_id=?", (message_id,)).fetchone()
                if old:
                    event = json.loads(old[0])
                    if (event["review_id"], event["kind"], event["actor"], event["payload"]) != (
                            review_id, kind, actor, payload or {}):
                        raise ValueError("Message ID reused with different content")
                    return {"duplicate": True, "event": event}
            result = action(review, db) or {}
            self.store.save(db, review, kind, actor, payload, message_id)
        # A projection failure must never undo an accepted protocol transition.
        try:
            self.render(review_id)
        except (OSError, ValueError, Conflict) as error:
            result["projection_error"] = str(error)
        return {"review_id": review_id, **result}

    @staticmethod
    def _open(review):
        if review["phase"] == "closed":
            raise ValueError("Review is closed")
        if review.get("paused"):
            raise ValueError("Imported review is paused; reconcile it before continuing")

    def create(self, title, source, criteria, *, context="", participants=None, include=None, project=None):
        if not title.strip() or not criteria.strip():
            raise ValueError("Title and evaluation criteria are required")
        if project is not None and project not in self.config.projects:
            raise ValueError("Unknown project")
        profiles = self.config.review.get("reviewers", {})
        participants = participants or self.config.review.get("participants") or [{"slot": "reviewer-1", "client": "codex", "mode": "manual"},
                                        {"slot": "reviewer-2", "client": "claude", "mode": "manual"}]
        if len(participants) != 2 or len({p["slot"] for p in participants}) != 2:
            raise ValueError("V1 requires two distinct reviewer slots")
        assignments = []
        for participant in participants:
            profile = {**profiles.get(participant.get("profile", ""), {}), **participant}
            if set(profile) - {"slot", "profile", "client", "mode", "model", "binary", "timeout"}:
                raise ValueError("Unknown reviewer profile fields")
            timeout = profile.get("timeout", 600)
            if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 3600:
                raise ValueError("Reviewer timeout must be positive and at most one hour")
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", profile["slot"]):
                raise ValueError("Reviewer slots must use letters, digits, underscores or hyphens")
            if profile.get("client") not in {"codex", "claude", "antigravity"}:
                raise ValueError("Unknown reviewer client")
            if profile.get("mode", "manual") not in {"manual", "automatic"}:
                raise ValueError("Mode must be manual or automatic")
            assignments.append({"id": identifier(), "slot": profile["slot"], "round": 1,
                                "version": 1, "profile": profile, "status": "pending"})
        packet = self.packets.snapshot(source, context=context, criteria=criteria, include=include)
        review_id = identifier()
        review = {"schema_version": 1, "id": review_id, "title": title, "criteria": criteria,
                  "project": project, "created": now(), "phase": "preparing", "outcome": None,
                  "version": 1, "round": 1, "packets": {"1": packet}, "assignments": assignments,
                  "checks": [], "jobs": [], "responses": [], "resolutions": {}, "paused": False}
        with self.store.transaction() as db:
            self.store.save(db, review, "created", payload={"packet_hash": packet["hash"]})
        result = {"review_id": review_id, "phase": "preparing", "packet_hash": packet["hash"]}
        try:
            self.render(review_id)
        except (OSError, ValueError, Conflict) as error:
            result["projection_error"] = str(error)
        return result

    def start(self, review_id):
        def action(r, db):
            self._open(r)
            if r["phase"] != "preparing":
                raise ValueError("Review has already started")
            pending = [j for j in r["jobs"] if j["status"] not in {"succeeded", "failed", "cancelled", "timed_out"}]
            if pending:
                raise ValueError("Baseline checks are still pending")
            r["baseline_evidence"] = [j["id"] for j in r["jobs"]]
            r["phase"] = "collecting"
            return {"phase": r["phase"]}
        return self._mutate(review_id, "started", action)

    def invite(self, review_id, slot, role="reviewer"):
        if role not in {"author", "reviewer"}:
            raise ValueError("Role must be author or reviewer")
        token = secrets.token_urlsafe(32)
        def action(r, db):
            self._open(r)
            if role == "author":
                r["author_token"] = hashlib.sha256(token.encode()).hexdigest()
            else:
                assignment = self._assignment(r, slot=slot)
                if assignment["profile"].get("mode", "manual") != "manual":
                    raise ValueError("Automatic assignments do not need manual invitations")
                if assignment["status"] not in {"pending", "joined"}:
                    raise ValueError("Assignment has finished")
                assignment["token_hash"] = hashlib.sha256(token.encode()).hexdigest()
                assignment.pop("session_id", None)
            return {"token": token, "role": role, "slot": slot}
        return self._mutate(review_id, "invited", action, payload={"slot": slot, "role": role})

    @staticmethod
    def _assignment(r, *, slot=None, assignment_id=None):
        matches = [a for a in r["assignments"] if (a["id"] == assignment_id if assignment_id else
                   a["slot"] == slot and a["round"] == r["round"])]
        if len(matches) != 1:
            raise ValueError("Unknown or ambiguous assignment")
        return matches[0]

    @staticmethod
    def _actor(r, token):
        hashed = hashlib.sha256(token.encode()).hexdigest()
        if secrets.compare_digest(r.get("author_token", ""), hashed):
            return "author", None
        for a in r["assignments"]:
            if secrets.compare_digest(a.get("token_hash", ""), hashed):
                return a["id"], a
        raise ValueError("Invalid or expired participant token")

    def join(self, review_id, token, client, session_id, fresh_context):
        def action(r, db):
            self._open(r)
            actor, assignment = self._actor(r, token)
            if assignment is None:
                return {"role": "author"}
            if assignment["round"] != r["round"] or r["phase"] not in ACTIVE_PHASES:
                raise ValueError("Assignment is not accepting participants")
            if assignment["profile"]["client"] != client:
                raise ValueError("Client does not match the invitation")
            receipt = db.execute("SELECT data FROM receipts WHERE client=? AND session_id=?",
                                 (client, session_id)).fetchone()
            if receipt is None:
                raise ValueError("No SessionStart receipt; start a fresh conversation with journal hooks")
            receipt = json.loads(receipt[0])
            if not fresh_context or not receipt["fresh"] or receipt["first_seen"] <= assignment.get("created", r["created"]):
                raise ValueError("First reviews require fresh context created after this review")
            if any(a.get("session_id") == session_id and a["id"] != assignment["id"]
                   for a in r["assignments"] if a["round"] == r["round"]):
                raise ValueError("One conversation cannot fill both reviewer slots")
            if assignment.get("session_id") not in {None, session_id}:
                raise ValueError("Invitation has already been bound to another session")
            assignment["session_id"] = session_id
            if assignment["status"] in TERMINAL:
                raise ValueError("Assignment has finished")
            if assignment["status"] == "pending":
                assignment["status"] = "joined"
            return {"assignment_id": actor, "round": r["round"], "packet_version": assignment["version"]}
        return self._mutate(review_id, "joined", action, payload={"client": client, "session_id": session_id})

    def claim(self, review_id, token, *, lease_seconds=600):
        def action(r, db):
            self._open(r)
            actor, a = self._actor(r, token)
            if a is None or not a.get("session_id"):
                raise ValueError("Join a reviewer session before claiming")
            if r["phase"] not in ACTIVE_PHASES or a["round"] != r["round"]:
                raise ValueError("Assignment belongs to another phase")
            if a["status"] == "claimed":
                raise ValueError("Assignment is already claimed; the owner can retry expired attempts")
            if a["status"] != "joined":
                raise ValueError("Assignment is not ready to claim")
            a.update(status="claimed", attempt_id=identifier(), expires=(datetime.now(timezone.utc) +
                     timedelta(seconds=min(max(lease_seconds, 1), 86400))).isoformat())
            return {"attempt_id": a["attempt_id"], "expires": a["expires"], "assignment_id": actor}
        return self._mutate(review_id, "claimed", action)

    def packet(self, review_id, token):
        with self.store.transaction() as db:
            r = self.store.load(db, review_id)
            actor, a = self._actor(r, token)
            if a is not None and not a.get("session_id"):
                raise ValueError("Join the assignment first")
        version = a["version"] if a else r["version"]
        packet = r["packets"][str(version)]
        self.packets.verify(packet)
        result = {"review_id": review_id, "version": version, "round": a["round"] if a else r["round"],
                  "packet": packet, "files": {e["path"]: self.packets.get(e["sha256"]).decode("utf-8", "replace")
                                              for e in packet["files"]}}
        if (a is None and r["phase"] not in {"preparing", "collecting"}) or (a is not None and a["round"] > 1):
            result["prior_reviews"] = [self._public_assignment(p) for p in r["assignments"] if p["round"] == 1]
            result["responses"] = r["responses"]
            result["evidence"] = [j for j in r["jobs"] if j["id"] in r.get("follow_up_evidence", [j["id"] for j in r["jobs"]])]
        else:
            result["evidence"] = [j for j in r["jobs"] if j["id"] in r.get("baseline_evidence", [])]
        return result

    @staticmethod
    def _validate_result(result):
        if not isinstance(result, dict) or not isinstance(result.get("summary"), str):
            raise ValueError("Review result needs a summary")
        if not isinstance(result.get("findings"), list):
            raise ValueError("Review result needs a findings array")
        if len(encode(result).encode()) > 1024 * 1024 or len(result["findings"]) > 1000:
            raise ValueError("Review result exceeds its size limit")
        requests = result.get("check_requests", [])
        if not isinstance(requests, list) or any(not isinstance(request, dict) or
                not isinstance(request.get("reason"), str) or not request["reason"].strip() for request in requests):
            raise ValueError("Check requests need a reason")
        seen = set()
        for finding in result["findings"]:
            if not isinstance(finding, dict):
                raise ValueError("Each finding must be an object")
            for key in ("id", "claim", "concern", "evidence", "recommendation"):
                if not isinstance(finding.get(key), str) or not finding[key].strip():
                    raise ValueError(f"Finding needs nonempty {key}")
            if finding["id"] in seen:
                raise ValueError("Duplicate finding ID")
            seen.add(finding["id"])
        return result

    def submit(self, review_id, token, attempt_id, version, result, message_id):
        self._validate_result(result)
        def action(r, db):
            self._open(r)
            actor, a = self._actor(r, token)
            if (a is None or a["round"] != r["round"] or r["phase"] not in ACTIVE_PHASES
                    or a["status"] != "claimed" or a.get("attempt_id") != attempt_id
                    or a["version"] != version or a["expires"] < now()):
                raise ValueError("Late, stale, or unclaimed review result")
            a.update(status="submitted", result=result)
            self._advance(r)
            return {"phase": r["phase"], "assignment_id": actor}
        return self._mutate(review_id, "critique", action, actor=hashlib.sha256(token.encode()).hexdigest(),
                            message_id=message_id, payload={"attempt_id": attempt_id, "version": version, "result": result})

    @staticmethod
    def _advance(r):
        current = [a for a in r["assignments"] if a["round"] == r["round"]]
        if all(a["status"] in TERMINAL for a in current):
            r["phase"] = "responding" if r["round"] == 1 else "awaiting_closure"

    def missing(self, review_id, slot, reason):
        if not reason.strip():
            raise ValueError("A reason is required")
        def action(r, db):
            self._open(r)
            if r["phase"] not in ACTIVE_PHASES:
                raise ValueError("No active collection phase")
            a = self._assignment(r, slot=slot)
            if a["status"] == "submitted":
                raise ValueError("Cannot discard a submitted review")
            a.update(status="missing", failure=reason)
            self._advance(r)
        return self._mutate(review_id, "participant_missing", action, payload={"slot": slot, "reason": reason})

    def retry_assignment(self, review_id, slot):
        def action(r, db):
            self._open(r)
            if r["phase"] not in ACTIVE_PHASES:
                raise ValueError("Cannot retry after a round has been revealed")
            a = self._assignment(r, slot=slot)
            if a["status"] == "submitted" or (a["status"] == "claimed" and a["expires"] > now()):
                raise ValueError("Cannot retry a submitted or live assignment")
            a["history"] = a.get("history", []) + [{k: v for k, v in a.items() if k != "history"}]
            for key in ("attempt_id", "expires", "token_hash", "session_id", "result", "failure"):
                a.pop(key, None)
            a["status"] = "pending"
            a["created"] = now()
        return self._mutate(review_id, "retry", action, payload={"slot": slot})

    def respond(self, review_id, token, responses, message_id):
        with self.store.transaction() as db:
            if self._actor(self.store.load(db, review_id), token)[0] != "author":
                raise ValueError("Only the author can respond")
        def action(r, db):
            self._open(r)
            actor, a = self._actor(r, token)
            if actor != "author" or r["phase"] != "responding" or r["responses"]:
                raise ValueError("Only the author can submit one response round after reveal")
            ids = {f"{a['slot']}:{f['id']}" for a in r["assignments"] if a["round"] == 1
                   for f in a.get("result", {}).get("findings", [])}
            if set(responses) != ids:
                raise ValueError("Respond to every finding using slot:finding-id keys")
            for response in responses.values():
                if not isinstance(response, dict) or response.get("disposition") not in {"accepted", "rejected", "unresolved"} or not response.get("reason"):
                    raise ValueError("Each response needs a disposition and reason")
            r["responses"].append(responses)
        return self._mutate(review_id, "response", action, actor="author", message_id=message_id, payload=responses)

    def revise(self, review_id, token, source, *, include=None):
        # Prepare immutable assets before opening a database write transaction.
        with self.store.transaction() as db:
            current = self.store.load(db, review_id)
            self._open(current)
            actor, _ = self._actor(current, token)
            if actor != "author" or current["phase"] != "responding" or current["version"] != 1:
                raise ValueError("Only one author revision is allowed after the initial round")
        original = current["packets"]["1"]
        packet = self.packets.snapshot(source, context=original["context"], criteria=original["criteria"], include=include)
        def action(r, db):
            self._open(r)
            actor, a = self._actor(r, token)
            if actor != "author" or r["phase"] != "responding" or r["version"] != 1:
                raise ValueError("Only one author revision is allowed after the initial round")
            r["version"] = 2
            r["packets"]["2"] = packet
            return {"packet_version": 2, "phase": "responding"}
        return self._mutate(review_id, "revision", action, actor="author")

    def follow_up(self, review_id, token):
        def action(r, db):
            self._open(r)
            actor, a = self._actor(r, token)
            if actor != "author" or r["phase"] != "responding" or not r["responses"]:
                raise ValueError("An author response is required before the follow-up round")
            if any(j["status"] not in {"succeeded", "failed", "timed_out", "cancelled"} for j in r["jobs"]):
                raise ValueError("Checks are pending; collect or cancel them before sealing follow-up evidence")
            r["round"] = 2
            r["follow_up_evidence"] = [j["id"] for j in r["jobs"]]
            r["phase"] = "follow_up"
            for old in list(r["assignments"]):
                r["assignments"].append({"id": identifier(), "slot": old["slot"], "round": 2,
                    "version": r["version"], "profile": old["profile"], "status": "pending", "created": now()})
            return {"phase": r["phase"], "packet_version": r["version"]}
        return self._mutate(review_id, "follow_up_started", action, actor="author")

    def close(self, review_id, outcome, rationale, resolutions=None):
        def action(r, db):
            self._open(r)
            if outcome not in {"completed", "incomplete", "cancelled", "failed"} or not rationale.strip():
                raise ValueError("Choose a terminal outcome and provide a rationale")
            pending = [j for j in r["jobs"] if j["status"] not in {"succeeded", "failed", "timed_out", "cancelled"}]
            missing = any(a["status"] != "submitted" for a in r["assignments"])
            if outcome == "completed" and (r["phase"] != "awaiting_closure" or pending or missing):
                raise ValueError("Completed closure requires both rounds and no missing or pending work")
            findings = {f"{a['round']}:{a['slot']}:{f['id']}" for a in r["assignments"]
                        for f in a.get("result", {}).get("findings", [])}
            for key, resolution in (resolutions or {}).items():
                if not isinstance(resolution, dict) or key not in findings or resolution.get("status") not in {"open", "addressed", "verified"}:
                    raise ValueError("Invalid finding resolution")
                if resolution["status"] == "verified":
                    if not resolution.get("evidence") or not resolution.get("explanation") or resolution.get("version") != r["version"]:
                        raise ValueError("Verification needs evidence, an explanation, and the current packet version")
            r.update(phase="closed", outcome=outcome, rationale=rationale,
                     resolutions={key: (resolutions or {}).get(key, {"status": "open"}) for key in findings})
        return self._mutate(review_id, "decision", action, payload={"outcome": outcome, "rationale": rationale,
                                                                   "resolutions": resolutions or {}})

    @staticmethod
    def _public_assignment(a):
        return {k: v for k, v in a.items() if k not in {"token_hash", "history", "profile"}}

    def status(self, review_id=None, token=None, *, owner=False):
        with self.store.transaction() as db:
            if review_id is None:
                if not owner:
                    raise ValueError("Participants must identify a review")
                return [{"id": r["id"], "title": r["title"], "phase": r["phase"], "outcome": r["outcome"]}
                        for row in db.execute("SELECT data FROM reviews") for r in [json.loads(row[0])]]
            r = self.store.load(db, review_id)
            actor, own = ("owner", None) if owner else self._actor(r, token or "")
            result = {k: v for k, v in r.items() if k not in {"author_token", "assignments", "packets", "checks", "jobs"}}
            blind = r["phase"] in {"preparing", "collecting"}
            result["assignments"] = []
            for a in r["assignments"]:
                item = self._public_assignment(a)
                if blind and not owner and a is not own:
                    item = {k: item[k] for k in ("id", "slot", "round", "version", "status")}
                result["assignments"].append(item)
            result["checks"] = [c for c in r["checks"] if owner or not blind or c["requestor"] == actor]
            result["jobs"] = [j for j in r["jobs"] if owner or not blind or j["id"] in r.get("baseline_evidence", [])]
            return result

    def refresh(self, review_id):
        self.render(review_id)
        return {"review_id": review_id, "refreshed": True}

    def render(self, review_id):
        import fcntl
        self.store.root.mkdir(parents=True, exist_ok=True)
        # Serialize projection reads and writes so an older snapshot cannot overwrite a newer view.
        with (self.store.root / "projection.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self._render(review_id)

    def _render(self, review_id):
        from ..journal import Journal, link
        with self.store.transaction() as db:
            r = self.store.load(db, review_id)
        journal = Journal(self.config)
        body = f"Phase: **{r['phase']}** · Outcome: **{r['outcome'] or 'pending'}**\n\n"
        body += f"Review ID: `{r['id']}` · Project: {r.get('project') or 'inbox'}\n\n"
        body += f"Packet version: {r['version']} · Round: {r['round']}\n\n## Criteria\n\n{r['criteria']}\n\n"
        body += "## Immutable packets\n\n"
        for version, packet in r["packets"].items():
            body += f"- Version {version}: `{packet['hash']}` ({len(packet['files'])} files)\n"
        body += "\nInput objects and complete evidence are held in review state; use `review packet` or `review export` to retrieve them.\n\n"
        blind = r["phase"] in {"preparing", "collecting"}
        for a in r["assignments"]:
            body += f"## Round {a['round']} — {a['slot']}\n\nStatus: {a['status']}\n\n"
            if not blind and a.get("result"):
                body += a["result"]["summary"] + "\n\n"
                for f in a["result"]["findings"]:
                    body += f"### {f['id']}: {f['claim']}\n\n{f['concern']}\n\nEvidence: {f['evidence']}\n\nRecommendation: {f['recommendation']}\n\n"
        if blind:
            body += "Initial submissions and proposed checks remain embargoed.\n\n"
        else:
            body += "## Author responses\n\n```json\n" + json.dumps(r["responses"], indent=2) + "\n```\n\n"
            body += "## Checks and evidence\n\n"
            for check in r["checks"]:
                body += f"- Proposed `{check['id']}`: {json.dumps(check['spec']['argv'])} · executor {check['spec']['executor']} · approval {'approved' if check['approval'] else 'required'}\n"
            body += "\n"
            for job in r["jobs"]:
                body += f"### Attempt `{job['id']}` — version {job['version']}: {job['status']}\n\n"
                body += "```json\n" + json.dumps({"kind": job["kind"], "executor": job["executor"],
                    "argv": job.get("spec", {}).get("argv"), "timeout": job.get("spec", {}).get("timeout"),
                    "result": {k: (v[:2000] + " [see state for complete bounded output]" if isinstance(v, str) and len(v) > 2000 else v)
                               for k, v in job.get("result", {}).items() if k not in {"artifacts"}},
                    "artifacts": [{k: v for k, v in item.items() if k != "content"}
                                  for item in job.get("result", {}).get("artifacts", [])]}, indent=2) + "\n```\n\n"
            body += "\n## Resolutions\n\n```json\n" + json.dumps(r["resolutions"], indent=2) + "\n```\n\n"
        if r.get("rationale"):
            body += f"## Decision\n\n{r['rationale']}\n"
        if r.get("paused"):
            body += "\nImported state is paused pending reconciliation.\n"
        path = journal._note(f"Reviews/{review_id}.md")
        body = body.replace("<!-- research-journal:", "&lt;!-- research-journal:")
        journal._managed(path, r["title"], body)
        with self.store.transaction() as db:
            entries = [json.loads(row[0]) for row in db.execute("SELECT data FROM reviews")]
        index = "\n".join("- " + link(f"{self.config.folder}/Reviews/{item['id']}.md", item["title"]) +
                          f" — {item['phase']}" for item in entries)
        journal._managed(journal._note("Reviews/Index.md"), "Reviews", index)
        if r.get("project") is None:
            journal._managed(journal._note(f"Inbox/Review-{review_id}.md"), "Unassigned review",
                             link(f"{self.config.folder}/Reviews/{review_id}.md", r["title"]) +
                             "\n\nNo registered project was assigned. Review this in the inbox.")
