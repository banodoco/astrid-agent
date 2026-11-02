#!/usr/bin/env python3
"""Truthful, isolated R0-only capture; never invokes an R1 update."""
import hashlib
import json
import os
import pwd
import re
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

UID = 503
REFS = {"driver": ("astrid-agent", "332b478afb459b0f9290fb5f73aefbd8169d7fd4"),
        "release0-agent": ("astrid-agent", "3204bb00135bec8ee2d2d3fb21eed5abfceca649"),
        "release0-runtime": ("astrid-runtime", "6d53a19070faf63fc15ae1f81417722edc2c8a5f")}


def owner_match(command):
    return ("-m runtime_protocol start" in command or
            (re.search(r"(?:^|\s)-c(?:\s|$)", command) is not None
             and "from astrid_runtime.cli import main" in command
             and re.search(r"main\(\s*\[\s*['\"]run['\"]", command) is not None))


def host_owners_exist():
    result = subprocess.run(["/bin/ps", "-ww", "-axo", "pid=,command="], capture_output=True,
                            text=True, timeout=20, check=True)
    records = []
    for line in result.stdout.splitlines():
        match = re.match(r"^\s*(\d+)\s+(.*)$", line)
        if match: records.append(match.group(2))
        elif records: records[-1] += "\n" + line
    return any(owner_match(command) for command in records)


def service_absent():
    out = subprocess.run(["/bin/launchctl", "print", "gui/503/haus.astrid.runtime"],
                         capture_output=True, text=True, timeout=20)
    missing = 'Could not find service "haus.astrid.runtime" in domain for user gui: 503'
    return out.returncode == 113 and out.stderr.strip().replace("\r\n", "\n") in {missing, "Bad request.\n" + missing}


def port_free():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try: sock.bind(("127.0.0.1", 17340))
        except OSError: return False
    return True


def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as src:
        for chunk in iter(lambda: src.read(1024 * 1024), b""): h.update(chunk)
    return h.hexdigest()


def environment(home, root, py):
    env = {key: os.environ[key] for key in ("RUNNER_OS", "RUNNER_NAME", "RUNNER_ENVIRONMENT", "CI", "GITHUB_ACTIONS") if key in os.environ}
    env.update(HOME=str(home), RUNNER_TEMP=str(root), PATH=":".join((str(py.parent), "/usr/bin", "/bin", "/usr/sbin", "/sbin")),
               GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_SYSTEM="/dev/null", GIT_CONFIG_GLOBAL="/dev/null",
               GIT_TERMINAL_PROMPT="0", GIT_LFS_SKIP_SMUDGE="1", GIT_NO_REPLACE_OBJECTS="1", PIP_CONFIG_FILE="/dev/null")
    return env


def stop_job_children(driver_pid):
    listed = subprocess.run(["/bin/ps", "-axo", "uid=,pid=,pgid=,stat="], capture_output=True,
                            text=True, timeout=10, check=True)
    sent = []
    for line in listed.stdout.splitlines():
        fields = line.split()
        if len(fields) != 4: raise RuntimeError("Process census malformed")
        uid, pid, pgid = map(int, fields[:3])
        if uid == UID and pgid == driver_pid and pid != driver_pid and not fields[3].startswith("Z"):
            try:
                if os.getpgid(pid) == driver_pid:
                    os.kill(pid, signal.SIGTERM); sent.append(pid)
            except ProcessLookupError: pass
    return sent


def run_process(argv, cwd, env, logfile, home, is_driver=False):
    with logfile.open("wb") as log:
        proc = subprocess.Popen(argv, cwd=str(cwd), env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        abort = False; census_failed = False; grace_expired = False; started = None
        while proc.poll() is None:
            if not abort and shutil.disk_usage(home).free < 3_000_000_000:
                if is_driver:
                    try: stop_job_children(proc.pid)
                    except Exception: census_failed = True
                else:
                    try: os.killpg(proc.pid, signal.SIGTERM)
                    except ProcessLookupError: pass
                abort = True; started = time.monotonic()
            elif abort and not grace_expired and time.monotonic() - started >= 300:
                # Do not force-kill an unknown Runtime or interrupt cleanup again.
                grace_expired = True
            time.sleep(2)
    return {"exit": proc.returncode, "disk_threshold_crossed": abort,
            "process_census_failed": census_failed, "cleanup_grace_expired": grace_expired}


def redact(data, home, root):
    text = data.decode("utf-8", errors="replace")
    text = text.replace(str(root), "<CAPTURE_ROOT>").replace(str(home), "<RUNNER_HOME>")
    text = re.sub(r"/Users/[^/\s\"']+", "<REDACTED_HOME>", text)
    text = re.sub(r"/private/var/folders/[^\s\"']+", "<MAC_TEMP>", text)
    text = re.sub(r"\b(?:gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+|sk-[A-Za-z0-9_-]+)", "<REDACTED_TOKEN>", text)
    text = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~+/-]+", r"\1<REDACTED>", text)
    text = re.sub(r'(?i)(["\'](?:token|api[_-]?key|secret|password)["\']\s*:\s*["\'])[^"\']*', r'\1<REDACTED>', text)
    return text.encode("utf-8")


def export_capture(root, home, output, status):
    original = root / "run/capture-upload"
    source_rows = []
    if original.is_dir() and not original.is_symlink():
        for source in sorted(original.rglob("*")):
            if source.is_symlink() or not source.is_file() or not source.resolve().is_relative_to(original): continue
            name = source.relative_to(original)
            if name.name == "SHA256SUMS": continue
            if source.stat().st_size > 16_000_000: raise RuntimeError("Capture exceeds safe export limit")
            data = source.read_bytes()
            dest = output / "capture-upload" / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(redact(data, home, root))
            source_rows.append(hashlib.sha256(data).hexdigest() + "  " + (Path("capture-upload") / name).as_posix())
    (output / "SOURCE-SHA256SUMS").write_text("\n".join(source_rows) + ("\n" if source_rows else ""))
    (output / "custody.json").write_text(json.dumps(status, indent=2, sort_keys=True) + "\n")
    rows=[]
    for path in sorted(output.rglob("*")):
        if path.is_file() and not path.is_symlink() and path.name != "SHA256SUMS":
            rows.append(digest(path) + "  " + path.relative_to(output).as_posix())
    (output / "SHA256SUMS").write_text("\n".join(rows) + "\n")


def main():
    if os.getuid() != UID or os.environ.get("RUNNER_NAME") != "hannah-mac" or sys.platform != "darwin":
        raise RuntimeError("Runner identity is not the dedicated macOS runner")
    home = Path(pwd.getpwuid(UID).pw_dir)
    root = Path(tempfile.mkdtemp(prefix="astrid-r0-capture-src.", dir="/private/tmp"))
    if root.is_symlink() or root.resolve() != root or root.stat().st_uid != UID or stat.S_IMODE(root.stat().st_mode) != 0o700:
        raise RuntimeError("Fresh capture root is unsafe")
    runner_temp = Path(os.environ["RUNNER_TEMP"])
    if (runner_temp != home / "actions-runner/_work/_temp" or runner_temp.resolve() != runner_temp
            or not runner_temp.is_dir() or runner_temp.is_symlink() or runner_temp.stat().st_uid != UID):
        raise RuntimeError("Runner action temp is unexpected")
    output = runner_temp / "r05-artifact"
    output.mkdir(mode=0o700)
    with open(os.environ["GITHUB_ENV"], "a") as fh: fh.write("R05_UPLOAD=" + str(output) + "\n")
    status = {"status": "not-run", "runner_custody": "hannah-mac; macOS self-hosted diagnostic", "qualification": False,
              "expected_runner_image": "local", "refs": {name: ref for name, (_, ref) in REFS.items()}, "stages": {}}
    try:
        if shutil.disk_usage(home).free < 10_000_000_000: raise RuntimeError("Insufficient disk")
        if not service_absent(): raise RuntimeError("Runner Runtime service present or absence unverified")
        plist = home / "Library/LaunchAgents/haus.astrid.runtime.plist"
        if plist.exists() or plist.is_symlink(): raise RuntimeError("Runner Runtime plist already exists")
        if host_owners_exist(): raise RuntimeError("Host has existing Runtime owner; no installation attempted")
        if not port_free(): raise RuntimeError("Default Runtime port is occupied; no installation attempted")
        pyroot = home / "actions-runner/_work/_tool/Python/3.11.18/arm64"
        py = pyroot / "bin/python3.11"
        if (pyroot.is_symlink() or pyroot.resolve() != pyroot or not py.is_file()
                or pyroot.stat().st_uid != UID or not py.resolve().is_relative_to(pyroot) or py.stat().st_uid != UID):
            raise RuntimeError("Runner-owned Python is unavailable")
        env = environment(home, root, py)
        version = subprocess.check_output([str(py), "-I", "-c", "import platform; print(platform.python_version())"], env=env, text=True, timeout=20).strip()
        if version != "3.11.18": raise RuntimeError("Runner Python version differs")
        status["interpreter"] = "runner-owned Python " + version
        logs = root / "private-logs"; logs.mkdir(mode=0o700)
        for name, (repo, sha) in REFS.items():
            for step, args in (("clone", ["/usr/bin/git", "-c", "core.hooksPath=/dev/null", "clone", "--filter=blob:none", "--no-checkout", "https://github.com/banodoco/" + repo + ".git", name]),
                               ("fetch", ["/usr/bin/git", "-C", name, "-c", "core.hooksPath=/dev/null", "fetch", "--depth=1", "origin", sha]),
                               ("checkout", ["/usr/bin/git", "-C", name, "-c", "core.hooksPath=/dev/null", "checkout", "--detach", sha])):
                if shutil.disk_usage(home).free < 10_000_000_000: raise RuntimeError("Insufficient disk before source operation")
                result = run_process(args, root, env, logs / (step + "-" + name + ".log"), home)
                if result["exit"] != 0: raise RuntimeError("Source operation failed: " + name + "/" + step)
            head = subprocess.check_output(["/usr/bin/git", "-C", str(root / name), "rev-parse", "HEAD"], env=env, text=True, timeout=20).strip()
            if head != sha: raise RuntimeError("Source checkout identity differs")
            status["stages"]["checkout-" + name] = "pass"
        fixture = root / "driver/tests/fixtures/release0"
        checked = run_process(["/usr/bin/shasum", "-a", "256", "-c", "SHA256SUMS"], fixture, env, logs / "fixture-check.log", home)
        if checked["exit"] != 0: raise RuntimeError("Frozen fixture hashes differ")
        status["fixture_sha256sums"] = digest(fixture / "SHA256SUMS")
        status["stages"]["fixture-check"] = "pass"
        if not service_absent() or host_owners_exist() or not port_free():
            raise RuntimeError("Host became unavailable before capture")
        argv = [str(py), "-u", str(root / "driver/scripts/ci_journey.py"), "--shared-executor", "authentic-r0-capture",
                "--run-root", str(root / "run"), "--root", str(root / "run/program"), "--data-root", str(root / "run/data"),
                "--shell", "sh", "--fixture", str(fixture), "--release0-agent-checkout", str(root / "release0-agent"),
                "--release0-runtime-checkout", str(root / "release0-runtime")]
        status["inner"] = run_process(argv, root, env, logs / "capture-private.log", home, is_driver=True)
        status["status"] = "capture-exited"
        receipt = root / "run/capture-upload/capture-receipt.json"
        if receipt.is_file() and not receipt.is_symlink():
            value = json.loads(receipt.read_text())
            status["receipt"] = {"status": value.get("status"), "runner_image": (value.get("runner") or {}).get("runner_image"),
                                 "cleanup": (value.get("cleanup") or {}).get("status"), "r1_update_invoked": value.get("r1_update_invoked")}
            if status["receipt"]["runner_image"] != "local": raise RuntimeError("Receipt runner image is not local")
    except Exception as exc:
        status["error"] = str(exc).replace(str(root), "<CAPTURE_ROOT>").replace(str(home), "<RUNNER_HOME>")
    finally:
        try: status["service_absent_after"] = service_absent()
        except Exception: status["service_absent_after"] = "unverified"
        status["disk_free_after"] = shutil.disk_usage(home).free
        export_capture(root, home, output, status)
        print(json.dumps({"status": status["status"], "receipt": status.get("receipt"), "service_absent_after": status["service_absent_after"]}))
    if status["status"] != "capture-exited" or status["inner"]["exit"] != 0 or status.get("receipt", {}).get("runner_image") != "local": raise SystemExit(1)


if __name__ == "__main__":
    try: main()
    except Exception:
        print(json.dumps({"status": "preflight-failed"}))
        raise SystemExit(1)
