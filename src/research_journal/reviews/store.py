"""Review state has its own short SQLite transactions, independent of vault locks."""

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

VERSION = 1


def now():
    return datetime.now(timezone.utc).isoformat()


def encode(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def fingerprint(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def identifier():
    return uuid4().hex


class Store:
    def __init__(self, state: Path):
        self.root = state / "reviews"
        self.database = state / "reviews.sqlite3"

    @contextmanager
    def transaction(self):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        db = sqlite3.connect(self.database, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS reviews (
                    id TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, message_id TEXT UNIQUE NOT NULL,
                    review_id TEXT NOT NULL REFERENCES reviews(id), data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS receipts (
                    client TEXT NOT NULL, session_id TEXT NOT NULL, data TEXT NOT NULL,
                    PRIMARY KEY(client,session_id));
            """)
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version > VERSION:
                raise ValueError("Review database is newer than this program")
            db.execute(f"PRAGMA user_version={VERSION}")
            db.commit()
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def load(db, review_id):
        row = db.execute("SELECT data FROM reviews WHERE id=?", (review_id,)).fetchone()
        if row is None:
            raise ValueError("Unknown review")
        return json.loads(row[0])

    @staticmethod
    def save(db, review, kind, actor="owner", payload=None, message_id=None, reply_to=None):
        message_id = message_id or identifier()
        event = {"schema_version": VERSION, "message_id": message_id, "review_id": review["id"],
                 "kind": kind, "actor": actor, "packet_version": review["version"],
                 "round": review["round"], "reply_to": reply_to or (payload or {}).get("attempt_id"), "created": now(),
                 "payload": payload or {}}
        db.execute("INSERT INTO reviews(id,data) VALUES(?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data",
                   (review["id"], encode(review)))
        db.execute("INSERT INTO events(message_id,review_id,data) VALUES(?,?,?)",
                   (message_id, review["id"], encode(event)))
        return event

    def receipt(self, client, session_id, fresh=True):
        with self.transaction() as db:
            previous = db.execute("SELECT data FROM receipts WHERE client=? AND session_id=?",
                                  (client, session_id)).fetchone()
            receipt = json.loads(previous[0]) if previous else {"first_seen": now(), "fresh": True}
            # Resuming or compacting a known session cannot renew its freshness proof.
            receipt["fresh"] = receipt["fresh"] and fresh
            db.execute("INSERT INTO receipts VALUES(?,?,?) ON CONFLICT(client,session_id) DO UPDATE SET data=excluded.data",
                       (client, session_id, encode(receipt)))
