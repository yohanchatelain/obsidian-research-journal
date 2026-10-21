"""Locked, recoverable file mutations. Markdown remains the source of truth."""

from contextlib import contextmanager
import fcntl
import hashlib
import os
from pathlib import Path
import sqlite3
import tempfile


class Conflict(Exception):
    """The target changed after it was read."""


def read(path: Path) -> str | None:
    try:
        with path.open(encoding="utf-8", newline="") as stream:
            return stream.read()
    except FileNotFoundError:
        return None


def digest(text: str | None) -> str:
    return hashlib.sha256((text or "").encode()).hexdigest()


def atomic_write(path: Path, content: str, expected: str | None):
    path.parent.mkdir(parents=True, exist_ok=True)
    if read(path) != expected:
        raise Conflict(f"Concurrent edit: {path.name}")
    fd, temporary = tempfile.mkstemp(prefix=".rj-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if path.exists():
            os.chmod(temporary, path.stat().st_mode & 0o777)
        if read(path) != expected:
            raise Conflict(f"Concurrent edit: {path.name}")
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def locked(state: Path):
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (state / "write.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        db = sqlite3.connect(state / "journal.sqlite3", timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS events (
                    id TEXT PRIMARY KEY, payload TEXT NOT NULL, status TEXT NOT NULL,
                    error TEXT, created TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS writes (
                    event_id TEXT NOT NULL, path TEXT NOT NULL, before_text TEXT,
                    after_text TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                    PRIMARY KEY(event_id, path));
                CREATE TABLE IF NOT EXISTS sessions (
                    client TEXT NOT NULL, id TEXT NOT NULL, workspace TEXT NOT NULL DEFAULT '',
                    turn INTEGER NOT NULL DEFAULT 0, saved INTEGER NOT NULL DEFAULT -1,
                    reminded INTEGER NOT NULL DEFAULT -1, state TEXT NOT NULL DEFAULT 'active',
                    updated TEXT NOT NULL, token TEXT NOT NULL DEFAULT '', PRIMARY KEY(client, id));
                CREATE TABLE IF NOT EXISTS health (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS gaps (
                    client TEXT NOT NULL, id TEXT NOT NULL, turn INTEGER NOT NULL,
                    reason TEXT NOT NULL, created TEXT NOT NULL, PRIMARY KEY(client,id,turn));
            """)
            db.commit()
            yield db
            db.commit()
        finally:
            db.close()
            fcntl.flock(lock, fcntl.LOCK_UN)
