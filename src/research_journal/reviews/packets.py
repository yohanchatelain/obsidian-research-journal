"""Immutable, content-addressed input packets and safe portable bundles."""

import hashlib
import os
import tempfile
from pathlib import Path, PurePosixPath
import subprocess

from .store import fingerprint

MAX_BYTES = 64 * 1024 * 1024
MAX_FILES = 10000


def safe_relative(name):
    path = PurePosixPath(name)
    if (not name or path.is_absolute() or ".." in path.parts or "\\" in name
            or any(p in {"", "."} for p in name.split("/"))):
        raise ValueError(f"Unsafe packet path: {name}")
    return path


class Packets:
    def __init__(self, root):
        self.root = root

    def object_path(self, digest):
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ValueError("Invalid object digest")
        return self.root / "objects" / digest

    def put(self, data):
        digest = hashlib.sha256(data).hexdigest()
        target = self.object_path(digest)
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(dir=target.parent, prefix=".object-")
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, 0o444)
            try:
                os.link(temporary, target)
            except FileExistsError:
                if target.read_bytes() != data:
                    raise ValueError("Corrupt packet object")
        finally:
            os.unlink(temporary)
        return digest

    def get(self, digest):
        data = self.object_path(digest).read_bytes()
        if hashlib.sha256(data).hexdigest() != digest:
            raise ValueError("Packet integrity check failed")
        return data

    def snapshot(self, source, *, context="", criteria="", include=None):
        if Path(source).expanduser().is_symlink():
            raise ValueError("Snapshot source cannot be a symlink")
        source = Path(source).expanduser().resolve()
        if not source.exists():
            raise ValueError("Review source does not exist")
        root = source if source.is_dir() else source.parent
        if source.is_file():
            names = [source.name]
        else:
            tracked = subprocess.run(["git", "-C", str(root), "ls-files", "-z"],
                                     capture_output=True, check=False)
            if tracked.returncode:
                raise ValueError("Directory snapshots require a Git repository; use a file or initialize Git")
            names = [n for n in tracked.stdout.decode().split("\0") if n]
        names += list(include or [])
        files, size = [], 0
        for name in sorted(set(names)):
            relative = safe_relative(name)
            path = root / str(relative)
            if any(part in {".git", ".env", ".aws", ".ssh", ".rj-control", "credentials.json"} for part in relative.parts):
                raise ValueError(f"Remove credential/application data from the review selection: {name}")
            if any(p.is_symlink() for p in [path, *path.parents] if p != root.parent):
                raise ValueError(f"Symlinks are not supported in packets: {name}")
            if not path.resolve().is_relative_to(root):
                raise ValueError("Snapshot path escaped its root")
            if not path.exists():
                continue  # Deleted tracked file: absence is part of this working-tree snapshot.
            if not path.is_file():
                raise ValueError(f"Snapshot entry is not a regular file: {name}")
            size += path.stat().st_size
            if size > MAX_BYTES or len(files) >= MAX_FILES:
                raise ValueError("Packet exceeds 64 MiB or 10,000 files; select a smaller artifact")
            with path.open("rb") as stream:
                data = stream.read(MAX_BYTES - sum(entry["size"] for entry in files) + 1)
            if sum(entry["size"] for entry in files) + len(data) > MAX_BYTES:
                raise ValueError("Packet exceeded its size limit during capture")
            files.append({"path": str(relative), "sha256": self.put(data), "size": len(data),
                          "executable": bool(path.stat().st_mode & 0o111)})
        if not files:
            raise ValueError("Review packet contains no files")
        packet = {"schema_version": 1, "files": files, "context": context, "criteria": criteria}
        packet["hash"] = fingerprint(packet)
        return packet

    def verify(self, packet):
        if packet.get("schema_version") != 1 or not isinstance(packet.get("files"), list):
            raise ValueError("Unsupported packet format")
        if len(packet["files"]) > MAX_FILES or sum(e["size"] for e in packet["files"]) > MAX_BYTES:
            raise ValueError("Packet exceeds its size limit")
        unsigned = {k: v for k, v in packet.items() if k != "hash"}
        if fingerprint(unsigned) != packet["hash"]:
            raise ValueError("Packet manifest integrity check failed")
        names = set()
        for entry in packet["files"]:
            safe_relative(entry["path"])
            if entry["path"] in names:
                raise ValueError("Duplicate packet path")
            names.add(entry["path"])
            if len(self.get(entry["sha256"])) != entry["size"]:
                raise ValueError("Packet size mismatch")

    def materialize(self, packet, destination):
        self.verify(packet)
        destination.mkdir(parents=True, exist_ok=False)
        for entry in packet["files"]:
            target = destination / entry["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(self.get(entry["sha256"]))
            target.chmod(0o755 if entry.get("executable") else 0o644)
        return destination
