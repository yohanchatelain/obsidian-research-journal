"""Optional owner-operated Markdown synchronization over existing OpenSSH connections."""

from configparser import ConfigParser
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import signal
import subprocess
import tempfile
from uuid import uuid4

from .journal import Journal
from .storage import Conflict, atomic_write, read


def settings(path):
    data = json.loads(Path(path).read_text())
    if data.get("schema_version") != 1 or set(data) - {"schema_version", "rclone", "hosts", "timeout"}:
        raise ValueError("Unsupported sync configuration")
    if not isinstance(data.get("rclone"), str) or not Path(data["rclone"]).is_absolute():
        raise ValueError("Sync requires an absolute rclone executable path")
    if not isinstance(data.get("hosts"), list) or not data["hosts"]:
        raise ValueError("Configure at least one sync host")
    seen = set()
    for host in data["hosts"]:
        if not isinstance(host, dict) or set(host) - {"name", "path", "backup", "python", "source"}:
            raise ValueError("Invalid sync host fields")
        name = host.get("name", "")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", name) or name in seen:
            raise ValueError("Use distinct existing SSH host aliases")
        seen.add(name)
        for key in ("path", "backup"):
            value = host.get(key, "")
            if (not isinstance(value, str) or not value.startswith("/") or value == "/"
                    or ".." in PurePosixPath(value).parts or any(c in value for c in "\n\r\0")):
                raise ValueError("Remote vault and backup paths must be dedicated absolute paths")
        vault, backup = PurePosixPath(host["path"]), PurePosixPath(host["backup"])
        if vault == backup or vault in backup.parents or backup in vault.parents:
            raise ValueError("Remote backup and vault paths must not overlap")
        if bool(host.get("source")) != bool(host.get("python")):
            raise ValueError("Remote refresh needs both source and python")
    timeout = data.get("timeout", 240)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 1 <= timeout <= 3600:
        raise ValueError("Sync timeout must be between 1 and 3600 seconds")
    return data


def filters(config):
    if any(c in config.folder for c in "\n\r[]*?{}\\"):
        raise ValueError("Sync cannot filter this journal folder name")
    return ("- .*\n- .*/**\n- **/.*/**\n"
            f"- /{config.folder}/Home.md\n- /{config.folder}/Projects/**\n"
            f"- /{config.folder}/Reviews/Index.md\n"
            "+ /RCLONE_TEST\n+ **/\n+ *.md\n+ *.md.rj-conflict-*\n- **\n")


def ssh(host):
    return ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", "-o", "StrictHostKeyChecking=yes", host]


def execute(argv, timeout):
    child = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        stdout, stderr = child.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(child.pid, signal.SIGTERM)
        try:
            child.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(child.pid, signal.SIGKILL)
            child.communicate()
        raise ValueError("Sync command timed out; inspect its state before recovery") from None
    return {"returncode": child.returncode, "stdout": stdout[-3000:], "stderr": stderr[-3000:]}


@contextmanager
def exclusive(root):
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (root / "sync.lock").open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("A vault synchronization is already running") from None
        yield


def conflicts(config):
    return sorted(str(p.relative_to(config.vault)) for p in config.vault.rglob("*.md.rj-conflict-*")
                  if p.is_file() and not p.is_symlink() and not any(part.startswith(".")
                      for part in p.relative_to(config.vault).parts))


def run(config, settings_path, *, hosts=None, initialize=False, dry_run=False, status=False):
    data = settings(settings_path)
    root = config.state / "sync"
    report_path = root / "last-run.json"
    if status:
        return {"ok": True, "last_run": json.loads(read(report_path) or "null"), "conflicts": conflicts(config)}
    selected = [host for host in data["hosts"] if not hosts or host["name"] in hosts]
    if not selected or (hosts and set(hosts) - {host["name"] for host in selected}):
        raise ValueError("Unknown sync host")
    if not config.vault.is_dir() or not (config.vault / "RCLONE_TEST").is_file():
        raise ValueError("Vault or RCLONE_TEST access marker is missing; refusing to synchronize")
    if config.state.is_relative_to(config.vault) or config.vault.is_relative_to(config.state):
        raise ValueError("Sync state and vault must not overlap")
    with exclusive(root):
        config_file = root / "remotes.conf"
        parsed = ConfigParser(interpolation=None)
        for host in data["hosts"]:
            parsed[host["name"]] = {"type": "sftp", "ssh": shlex.join(ssh(host["name"])),
                                    "disable_hashcheck": "true",
                                    "known_hosts_file": str(Path.home() / ".ssh/known_hosts")}
        import io
        stream = io.StringIO()
        parsed.write(stream)
        atomic_write(config_file, stream.getvalue(), read(config_file))
        filter_file = root / "notes.filter"
        filter_text = filters(config)
        atomic_write(filter_file, filter_text, read(filter_file))
        report = {"time": datetime.now(timezone.utc).isoformat(), "ok": True, "dry_run": dry_run, "hosts": []}
        for host in selected:
            name = host["name"]
            directory = root / name
            directory.mkdir(exist_ok=True)
            marker = directory / "initialized.json"
            identity = hashlib.sha256(json.dumps({"vault": str(config.vault), "host": host,
                                      "filters": filter_text}, sort_keys=True).encode()).hexdigest()
            try:
                if initialize and marker.exists():
                    raise ValueError("Already initialized; refusing an automatic resync")
                if not initialize and not marker.exists():
                    raise ValueError("Sync pair has no baseline; initialize it explicitly once")
                if marker.exists() and json.loads(marker.read_text())["identity"] != identity:
                    raise ValueError("Sync paths or filters changed; inspect before rebuilding the baseline")
                workdir = directory / "listings"
                if initialize and workdir.exists() and any(workdir.iterdir()):
                    raise ValueError("An interrupted initialization needs manual inspection")
                stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid4().hex[:8]
                argv = [data["rclone"], "bisync", str(config.vault), name + ":" + host["path"],
                        "--config", str(config_file), "--workdir", str(workdir),
                        "--filters-file", str(filter_file), "--check-access", "--max-delete", "10",
                        "--conflict-resolve", "path1", "--conflict-loser", "num",
                        "--conflict-suffix", "rj-conflict-local,rj-conflict-remote",
                        "--backup-dir1", str(root / "backups" / name / stamp),
                        "--backup-dir2", name + ":" + host["backup"] + "/" + stamp,
                        "--resilient", "--recover", "--checkers", "2", "--transfers", "2",
                        "--contimeout", "20s", "--timeout", "60s", "--log-level", "NOTICE", "--color", "NEVER"]
                if initialize:
                    argv += ["--resync", "--resync-mode", "path1"]
                if dry_run:
                    argv += ["--dry-run"]
                # Initial dry-runs must not leave listing state that looks like a partial real run.
                with tempfile.TemporaryDirectory(prefix="dry-", dir=directory) as temporary:
                    if initialize and dry_run:
                        argv[argv.index("--workdir") + 1] = temporary
                    result = execute(argv, data.get("timeout", 240))
                if result["returncode"]:
                    report["ok"] = False
                elif initialize and not dry_run:
                    atomic_write(marker, json.dumps({"identity": identity, "created": report["time"]}) + "\n", None)
                if not result["returncode"] and not dry_run and host.get("source"):
                    command = [host["python"], str(PurePosixPath(host["source"]) / "bin/research-journal"), "refresh"]
                    result["remote_refresh"] = execute([*ssh(name), shlex.join(command)], 60)
                    if result["remote_refresh"]["returncode"]:
                        report["ok"] = False
                report["hosts"].append({"name": name, **result})
            except (OSError, ValueError) as error:
                report["ok"] = False
                report["hosts"].append({"name": name, "error": str(error)})
        report["conflicts"] = conflicts(config)
        if not dry_run:
            try:
                if report["conflicts"]:
                    journal = Journal(config)
                    journal._managed(journal._note("Inbox/Sync conflicts.md"), "Notes with synchronization conflicts",
                                     "Both versions were preserved. Compare each saved copy with its original note before resolving it.\n\n" +
                                     "\n".join("- `" + path.replace("`", "\\`") + "`" for path in report["conflicts"]))
                Journal(config).refresh()
            except (OSError, ValueError, Conflict) as error:
                report.update(ok=False, refresh_error=str(error))
            atomic_write(report_path, json.dumps(report, indent=2) + "\n", read(report_path))
        return report
