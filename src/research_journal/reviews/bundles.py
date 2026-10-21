"""Owner-only portable recovery bundles. Import never launches work or restores capabilities."""

import base64
import copy
import hashlib
import json
import os
from pathlib import Path

from .store import encode, fingerprint, now


def redact(value):
    if isinstance(value, dict):
        return {k: redact(v) for k, v in value.items() if k not in {"token_hash", "author_token"}}
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


class Bundles:
    def export_bundle(self, review_id, destination):
        with self.store.transaction() as db:
            review = redact(self.store.load(db, review_id))
            events = [redact(json.loads(row[0])) for row in db.execute(
                "SELECT data FROM events WHERE review_id=? ORDER BY seq", (review_id,))]
        objects = {}
        for packet in review["packets"].values():
            self.packets.verify(packet)
            for entry in packet["files"]:
                objects[entry["sha256"]] = base64.b64encode(self.packets.get(entry["sha256"])).decode()
        bundle = {"schema_version": 1, "review": review, "events": events, "objects": objects}
        bundle["hash"] = fingerprint(bundle)
        encoded = encode(bundle) + "\n"
        if len(encoded.encode()) > 128 * 1024 * 1024:
            raise ValueError("Bundle exceeds 128 MiB; select smaller input packets or evidence")
        path = Path(destination)
        path.parent.mkdir(parents=True, exist_ok=True)
        with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as stream:
            stream.write(encoded)
        return {"path": str(path), "hash": bundle["hash"], "review_id": review_id}

    def import_bundle(self, source):
        path = Path(source)
        if path.stat().st_size > 128 * 1024 * 1024:
            raise ValueError("Bundle exceeds 128 MiB")
        bundle = json.loads(path.read_text())
        if bundle.get("schema_version") != 1 or fingerprint({k: v for k, v in bundle.items() if k != "hash"}) != bundle.get("hash"):
            raise ValueError("Unsupported or corrupt bundle")
        r = copy.deepcopy(bundle["review"])
        rid = r["id"]
        if len(rid) != 32 or any(c not in "0123456789abcdef" for c in rid) or r.get("schema_version") != 1:
            raise ValueError("Invalid review identity")
        if r["phase"] not in {"preparing", "collecting", "responding", "follow_up", "awaiting_closure", "closed"}:
            raise ValueError("Invalid review phase")
        if len(r["assignments"]) not in {2, 4} or r["round"] not in {1, 2} or r["version"] not in {1, 2}:
            raise ValueError("Invalid review assignments")
        total = 0
        for digest, content in bundle["objects"].items():
            data = base64.b64decode(content, validate=True)
            total += len(data)
            if total > 128 * 1024 * 1024 or hashlib.sha256(data).hexdigest() != digest:
                raise ValueError("Corrupt or oversized bundle objects")
            self.packets.put(data)
        for packet in r["packets"].values():
            self.packets.verify(packet)
        r = redact(r)
        r["paused"] = r["phase"] != "closed"
        r["bundle_origin_hash"] = bundle["hash"]
        for a in r["assignments"]:
            # A new manual invitation must bind a fresh session after recovery.
            if a["status"] == "joined" or (a["status"] == "claimed" and a["profile"].get("mode") == "manual"):
                a["status"] = "pending"
                a["created"] = now()
                for key in ("attempt_id", "expires", "session_id"):
                    a.pop(key, None)
        with self.store.transaction() as db:
            old = db.execute("SELECT data FROM reviews WHERE id=?", (rid,)).fetchone()
            if old:
                if json.loads(old[0]).get("bundle_origin_hash") == bundle["hash"]:
                    return {"review_id": rid, "duplicate": True}
                raise ValueError("Review already exists; import into a separate state directory")
            db.execute("INSERT INTO reviews VALUES(?,?)", (rid, encode(r)))
            for event in bundle["events"]:
                if event.get("schema_version") != 1 or event.get("review_id") != rid:
                    raise ValueError("Invalid bundled event")
                db.execute("INSERT INTO events(message_id,review_id,data) VALUES(?,?,?)",
                           (event["message_id"], rid, encode(redact(event))))
            self.store.save(db, r, "imported", payload={"bundle_hash": bundle["hash"]})
        self.render(rid)
        return {"review_id": rid, "paused": r["paused"], "phase": r["phase"]}

    def reconcile(self, review_id, *, decisions=None, resume=False):
        """Human evidence can settle abandoned attempts, never manufacture success."""
        decisions = decisions or {}
        def action(r, db):
            for job_id, decision in decisions.items():
                job = next((j for j in r["jobs"] if j["id"] == job_id), None)
                if job is None or job["status"] in {"succeeded", "failed", "timed_out", "cancelled"}:
                    raise ValueError("Reconcile only an unresolved attempt")
                if (decision.get("state") not in {"failed", "cancelled"} or not decision.get("evidence")
                        or not decision.get("reason")):
                    raise ValueError("Reconciliation requires failed/cancelled, evidence, and reason")
                job.update(status=decision["state"], reconciliation={**decision, "time": now()})
                if job["kind"] == "reviewer":
                    a = self._assignment(r, assignment_id=job["assignment_id"])
                    if a.get("attempt_id") == job_id and a["status"] == "claimed":
                        a.update(status="failed", failure=decision["reason"])
            if resume:
                if any(j["status"] not in {"succeeded", "failed", "timed_out", "cancelled"} for j in r["jobs"]):
                    raise ValueError("Poll known handles or settle unknown attempts before resuming")
                r["paused"] = False
            if r["phase"] in {"collecting", "follow_up"}:
                self._advance(r)
            return {"paused": r.get("paused", False), "phase": r["phase"]}
        return self._mutate(review_id, "reconciled", action, payload={"decisions": decisions, "resume": resume})
