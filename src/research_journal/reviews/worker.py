"""Standalone local/SSH/Slurm helper. Only the standard library is required remotely.

All command execution occurs here, outside coordinator transactions. The same JSON
operations can be sent over SSH; no remote shell interpolation of user commands is used.
"""

import base64
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import selectors
import re
import shlex
import signal
import subprocess
import sys
import tempfile
import time

LOG_LIMIT = 1024 * 1024
ARTIFACT_LIMIT = 16 * 1024 * 1024


def write_json(path, value):
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=".state-")
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def signature(pid):
    try:
        stat = Path(f"/proc/{pid}/stat")
        if stat.exists():
            fields = stat.read_text().rsplit(")", 1)[1].split()
            if fields[0] == "Z":
                return None
            return fields[19]
        result = subprocess.run(["ps", "-p", str(pid), "-o", "lstart="], capture_output=True,
                                text=True, timeout=5)
        return result.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def relative(value):
    p = PurePosixPath(value)
    if not value or p.is_absolute() or ".." in p.parts or "\\" in value:
        raise ValueError("Unsafe attempt-relative path")
    return str(p)


def attempt_dir(request):
    root = Path(request["root"]).expanduser().resolve()
    attempt = request["attempt"]
    if len(attempt) != 32 or any(c not in "0123456789abcdef" for c in attempt):
        raise ValueError("Invalid attempt identifier")
    if root == Path("/"):
        raise ValueError("Choose a dedicated execution staging root")
    target = root / attempt
    if target.is_symlink():
        raise ValueError("Attempt directory cannot be a symlink")
    return target


def stage(request):
    directory = attempt_dir(request)
    spec = request["spec"]
    digest = hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()
    directory.mkdir(parents=True, exist_ok=True)
    import fcntl
    with (directory / "stage.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        descriptor = directory / "request.json"
        if descriptor.exists():
            current = json.loads(descriptor.read_text())
            if current["spec_hash"] != digest or current["packet_hash"] != request["packet_hash"]:
                raise ValueError("Attempt ID already belongs to other inputs")
            return {"state": "staged"}
        work = directory / "work"
        work.mkdir(exist_ok=True)
        total = 0
        for entry in request["files"]:
            target = work / relative(entry["path"])
            if not target.resolve().is_relative_to(work.resolve()) or target.is_symlink():
                raise ValueError("Staging path escaped work directory")
            data = base64.b64decode(entry["content"], validate=True)
            total += len(data)
            if total > 64 * 1024 * 1024 or hashlib.sha256(data).hexdigest() != entry["sha256"]:
                raise ValueError("Staging integrity or size failure")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            target.chmod(0o755 if entry.get("executable") else 0o644)
        source = request["worker_source"]
        (directory / "worker.py").write_text(source)
        write_json(descriptor, {"spec": spec, "spec_hash": digest, "packet_hash": request["packet_hash"]})
    return {"state": "staged"}


def execute(request_file):
    descriptor = Path(request_file)
    directory = descriptor.parent
    request = json.loads(descriptor.read_text())
    spec = request["spec"]
    work = directory / "work"
    cwd = (work / spec.get("cwd", ".")).resolve()
    if not cwd.is_relative_to(work.resolve()):
        raise ValueError("Check working directory escaped packet")
    argv = spec["argv"]
    if spec.get("setup"):
        setup = work / relative(spec["setup"])
        argv = ["bash", "-c", 'source "$1" || exit $?; shift; exec "$@"', "review-check", str(setup), *argv]
    started = time.time()
    logs = {"stdout": bytearray(), "stderr": bytearray()}
    truncated = {"stdout": False, "stderr": False}
    child = None
    stopped = False

    def terminate(signum, frame):
        nonlocal stopped
        stopped = True
        if child is not None:
            try:
                os.killpg(child.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass

    signal.signal(signal.SIGTERM, terminate)
    timed_out = False
    exit_code = None
    try:
        child = subprocess.Popen(argv, cwd=cwd, env={**os.environ, **spec.get("env", {})},
                                 stdin=subprocess.PIPE if "stdin" in spec else subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        write_json(directory / "child.json", {"pid": child.pid, "signature": signature(child.pid)})
        if "stdin" in spec:
            # Prompts are bounded by the coordinator; feed in a thread so output cannot deadlock input.
            import threading
            def feed():
                try:
                    child.stdin.write(spec["stdin"].encode())
                    child.stdin.close()
                except (BrokenPipeError, OSError):
                    pass
            threading.Thread(target=feed, daemon=True).start()
        selector = selectors.DefaultSelector()
        for stream, name in ((child.stdout, "stdout"), (child.stderr, "stderr")):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ, name)
        deadline = started + spec["timeout"]
        kill_at = None
        while selector.get_map() or child.poll() is None:
            if time.time() >= deadline and not timed_out:
                timed_out = True
                terminate(None, None)
            if stopped:
                kill_at = kill_at or time.time() + 2
                if time.time() >= kill_at:
                    try:
                        os.killpg(child.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            for key, _ in selector.select(timeout=0.1):
                data = os.read(key.fileobj.fileno(), 65536)
                if not data:
                    selector.unregister(key.fileobj)
                    key.fileobj.close()
                    continue
                name = key.data
                remaining = LOG_LIMIT - len(logs[name])
                logs[name].extend(data[:max(remaining, 0)])
                truncated[name] |= len(data) > remaining
            # Descendants retaining pipes must not keep a completed attempt alive forever.
            if child.poll() is not None and time.time() > deadline + 2:
                break
        selector.close()
        exit_code = child.wait()
        state = "timed_out" if timed_out else "cancelled" if stopped else "succeeded" if exit_code == 0 else "failed"
    except Exception as error:
        state = "failed"
        logs["stderr"].extend(str(error).encode()[:LOG_LIMIT])
    artifacts = []
    total = 0
    for name in spec.get("artifacts", []):
        target = work / relative(name)
        if target.is_file() and not target.is_symlink() and target.resolve().is_relative_to(work.resolve()):
            remaining = ARTIFACT_LIMIT - total
            with target.open("rb") as stream:
                data = stream.read(remaining + 1)
            if len(data) > remaining:
                artifacts.append({"path": name, "error": "artifact size limit exceeded"})
                continue
            total += len(data)
            artifacts.append({"path": name, "sha256": hashlib.sha256(data).hexdigest(),
                              "content": base64.b64encode(data).decode()})
    if spec.get("native_client"):
        # Native CLIs can echo prompts and reasoning in diagnostics. Persist only the
        # structured review envelope and model metadata, never a CLI conversation log.
        diagnostic = logs["stderr"].decode("utf-8", "replace")
        model = re.search(r"(?m)^model:\s*(.+)$", diagnostic)
        logs["stderr"] = bytearray(("model: " + model.group(1) if model else
                                    "native exit code: " + str(exit_code)).encode())
        if spec["native_client"] == "codex":
            logs["stdout"] = bytearray()
        else:
            try:
                envelope = json.loads(logs["stdout"])
                value = envelope.get("structured_output") or envelope.get("response") or envelope.get("result") or envelope
                if isinstance(value, str):
                    value = json.loads(value)
                logs["stdout"] = bytearray(json.dumps({"structured_output": value, "model": envelope.get("model"),
                                                       "usage": envelope.get("usage")}).encode())
            except (ValueError, TypeError, AttributeError):
                logs["stdout"] = bytearray()
    result = {"state": state, "exit_code": exit_code, "started": started, "ended": time.time(),
              "packet_hash": request["packet_hash"], "spec_hash": request["spec_hash"],
              "stdout": logs["stdout"].decode("utf-8", "replace"),
              "stderr": logs["stderr"].decode("utf-8", "replace"), "truncated": truncated,
              "artifacts": artifacts, "environment": {"python": sys.version.split()[0],
                                                          "platform": sys.platform}}
    write_json(directory / "result.json", result)
    return 0 if state == "succeeded" else 1


def launch(request):
    directory = attempt_dir(request)
    import fcntl
    with (directory / "launch.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        marker = directory / "handle.json"
        if marker.exists():
            return json.loads(marker.read_text())
        if (directory / "launch.intent").exists():
            return {"state": "unknown", "reason": "launch interrupted before a handle was recorded"}
        write_json(directory / "launch.intent", {"started": time.time()})
        if request.get("backend") == "slurm":
            argv = ["sbatch", "--parsable", f"--job-name=rj-{request['attempt']}",
                    f"--comment=rj-{request['attempt']}", f"--chdir={directory}"]
            for key, value in request.get("resources", {}).items():
                if key not in {"time", "mem", "cpus-per-task", "partition", "account", "gres"}:
                    raise ValueError("Unsupported Slurm resource")
                argv.append(f"--{key}={value}")
            argv += ["--wrap", shlex.join([sys.executable, str(directory / "worker.py"), "execute", str(directory / "request.json")])]
            result = subprocess.run(argv, capture_output=True, text=True, timeout=30)
            job_id = result.stdout.strip().split(";")[0]
            if result.returncode or not job_id.isdigit():
                # It is unsafe to resubmit an ambiguous sbatch request.
                return {"state": "unknown", "reason": result.stderr[:2000] or "No scheduler handle"}
            handle = {"state": "queued", "job_id": job_id, "backend": "slurm"}
        else:
            with (directory / "worker.log").open("ab") as log:
                process = subprocess.Popen([sys.executable, str(directory / "worker.py"), "execute",
                                            str(directory / "request.json")], stdin=subprocess.DEVNULL,
                                           stdout=log, stderr=log, start_new_session=True)
            handle = {"state": "running", "pid": process.pid, "signature": signature(process.pid),
                      "backend": "process"}
        write_json(marker, handle)
        return handle


def reconcile_slurm(directory, attempt):
    matches = set()
    name = f"rj-{attempt}"
    for argv in (["squeue", "-h", "--name", name, "-o", "%i"],
                 ["sacct", "-n", "-X", "--name", name, "--format=JobIDRaw", "--starttime=1970-01-01"]):
        result = subprocess.run(argv, capture_output=True, text=True, timeout=30)
        if result.returncode:
            continue
        matches.update(line.strip() for line in result.stdout.splitlines() if line.strip().isdigit())
    if len(matches) == 1:
        handle = {"state": "queued", "job_id": matches.pop(), "backend": "slurm"}
        write_json(directory / "handle.json", handle)
        return handle
    return {"state": "unknown", "reason": "Scheduler submission could not be uniquely reconciled"}


def status(request):
    directory = attempt_dir(request)
    result = directory / "result.json"
    handle_file = directory / "handle.json"
    handle = json.loads(handle_file.read_text()) if handle_file.exists() else {}
    if request.get("backend") == "slurm" or handle.get("backend") == "slurm":
        if not handle.get("job_id"):
            handle = reconcile_slurm(directory, request["attempt"])
        if not handle.get("job_id"):
            return handle
        accounting = subprocess.run(["sacct", "-n", "-P", "-X", "-j", handle["job_id"],
                                    "--format=State,ExitCode,JobName%80"], capture_output=True, text=True, timeout=30)
        rows = [line.split("|") for line in accounting.stdout.splitlines() if line.strip()]
        rows = [row for row in rows if len(row) >= 3 and row[2] == f"rj-{request['attempt']}"]
        if rows:
            scheduler_state, code = rows[0][:2]
            if scheduler_state.startswith(("PENDING", "CONFIGURING")):
                return {**handle, "state": "queued"}
            if scheduler_state.startswith(("RUNNING", "COMPLETING")):
                return {**handle, "state": "running"}
            if result.exists():
                evidence = json.loads(result.read_text())
                if scheduler_state != "COMPLETED" or code != "0:0":
                    evidence["state"] = "cancelled" if scheduler_state.startswith("CANCELLED") else "failed"
                return {**evidence, "job_id": handle["job_id"], "scheduler_state": scheduler_state,
                        "scheduler_exit_code": code}
            if scheduler_state.startswith("CANCELLED"):
                return {**handle, "state": "cancelled", "scheduler_state": scheduler_state}
            if scheduler_state.startswith(("FAILED", "TIMEOUT", "OUT_OF_MEMORY", "NODE_FAIL")):
                return {**handle, "state": "failed", "scheduler_state": scheduler_state}
        return {**handle, "state": "unknown", "reason": "Waiting for matching scheduler accounting and evidence"}
    if result.exists():
        return json.loads(result.read_text())
    if handle.get("pid") and handle.get("signature") == signature(handle["pid"]):
        return {**handle, "state": "running"}
    return {"state": "unknown", "reason": "Worker absent without a durable result"}


def cancel(request):
    directory = attempt_dir(request)
    handle_file = directory / "handle.json"
    if not handle_file.exists():
        return {"state": "unknown", "reason": "No verified handle to cancel"}
    handle = json.loads(handle_file.read_text())
    if handle.get("backend") == "slurm":
        check = subprocess.run(["squeue", "-h", "-j", handle["job_id"], "-o", "%j"],
                               capture_output=True, text=True, timeout=30)
        if check.returncode == 0 and check.stdout.strip() == f"rj-{request['attempt']}":
            subprocess.run(["scancel", handle["job_id"]], check=True, capture_output=True, timeout=30)
            return {"state": "cancelling", "job_id": handle["job_id"]}
        return status(request)
    if handle.get("signature") and handle["signature"] == signature(handle["pid"]):
        os.kill(handle["pid"], signal.SIGTERM)
        return {"state": "cancelling"}
    return status(request)


def dispatch(request):
    return {"stage": stage, "launch": launch, "status": status, "cancel": cancel}[request["operation"]](request)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "execute":
        raise SystemExit(execute(sys.argv[2]))
    try:
        print(json.dumps(dispatch(json.load(sys.stdin))))
    except Exception as error:
        print(json.dumps({"state": "unknown", "error": str(error)}))
        raise SystemExit(1)
