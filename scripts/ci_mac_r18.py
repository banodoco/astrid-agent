#!/usr/bin/env python3
"""Run the exact R-18 diagnostic inside a fresh runner-owned task directory."""
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
import time
from pathlib import Path

UID = 503
REFS = {
    "driver": ("astrid-agent", "f91fa67ec223d3e8756934d9758a2ace7bbe369b"),
    "app": ("astrid-app", "ee66de5d100e0a9b8cf5198961556a68b24cad3e"),
    "candidate-agent": ("astrid-agent", "80e19ea05d926172bbcf2588e46f6f9919776a81"),
    "runtime": ("astrid-runtime", "255ecda21ba904520eb867936ef173cc5f7d3ae4"),
    "pinokio": ("astrid-pinokio", "3a6a98ae251ca126505245286e2d3e760f4348e9"),
}
ARTIFACTS = ("public/install", "public/install.sh", "public/install.ps1")


def owned_dir(path):
    st = path.lstat()
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != UID or path != path.resolve():
        raise RuntimeError("Runner-owned directory validation failed")


def disk(home, minimum=10_000_000_000):
    free = shutil.disk_usage(home).free
    if free < minimum:
        raise RuntimeError("Insufficient free disk")
    return free


def service_absent():
    result = subprocess.run(["/bin/launchctl", "print", "gui/503/haus.astrid.runtime"],
                            capture_output=True, text=True, timeout=20)
    msg = 'Could not find service "haus.astrid.runtime" in domain for user gui: 503'
    return result.returncode == 113 and result.stderr.strip().replace("\r\n", "\n") in {msg, "Bad request.\n" + msg}


def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""): h.update(chunk)
    return h.hexdigest()


def environment(home, task, python_dir, node_dir, runner_temp):
    # Only deliberate runner and OS environment is inherited; no credentials/config.
    env = {key: os.environ[key] for key in ("RUNNER_OS", "RUNNER_NAME", "RUNNER_ENVIRONMENT", "CI", "GITHUB_ACTIONS") if key in os.environ}
    env.update(HOME=str(home), PATH="", GIT_CONFIG_NOSYSTEM="1",
               GIT_CONFIG_SYSTEM="/dev/null", GIT_CONFIG_GLOBAL="/dev/null",
               GIT_TERMINAL_PROMPT="0", GIT_LFS_SKIP_SMUDGE="1", GIT_NO_REPLACE_OBJECTS="1",
               PIP_CONFIG_FILE="/dev/null", ASTRID_CI_EXPECTED_RUNNER="astrid-mac")
    env["PATH"] = ":".join((str(python_dir), str(node_dir), "/usr/bin", "/bin", "/usr/sbin", "/sbin"))
    env["RUNNER_TEMP"] = str(runner_temp)
    for name in ("npm-user", "npm-global"):
        p = task / (name + ".conf")
        p.write_text(""); p.chmod(0o600)
    env["NPM_CONFIG_USERCONFIG"] = str(task / "npm-user.conf")
    env["NPM_CONFIG_GLOBALCONFIG"] = str(task / "npm-global.conf")
    return env


def stage(name, argv, cwd, env, logs, home):
    disk(home)
    with (logs / (name + ".log")).open("wb") as out:
        proc = subprocess.Popen(argv, cwd=str(cwd), env=env, stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
        deadline = time.monotonic() + 1800
        while proc.poll() is None:
            if shutil.disk_usage(home).free < 3_000_000_000 or time.monotonic() >= deadline:
                os.killpg(proc.pid, signal.SIGTERM)
                try: proc.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL); proc.wait()
                raise RuntimeError("Stage interrupted for disk or timeout: " + name)
            time.sleep(2)
    if proc.returncode != 0:
        raise RuntimeError("Stage failed: " + name)



def run_driver(argv, task, env, home, logs):
    """Keep watching disk; SIGINT asks the driver's own finally block to clean up."""
    with (logs / "journey.log").open("wb") as out:
        proc = subprocess.Popen(argv, cwd=str(task), env=env, stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
        interrupted = False
        terminated = False
        interrupted_at = None
        while proc.poll() is None:
            if not interrupted and shutil.disk_usage(home).free < 3_000_000_000:
                try: os.killpg(proc.pid, signal.SIGINT)
                except ProcessLookupError: pass
                interrupted = True
                interrupted_at = time.monotonic()
            elif interrupted and not terminated and time.monotonic() - interrupted_at >= 300:
                # Bounded grace for the driver's own cleanup; never kill a launchd service.
                try: os.killpg(proc.pid, signal.SIGTERM)
                except ProcessLookupError: pass
                terminated = True
            time.sleep(3)
    return {"exit": proc.returncode, "disk_interrupt_requested": interrupted,
            "cleanup_grace_expired": terminated}


def make_candidate(task):
    app = task / "app"
    candidate = {key: REFS[key][1] for key in ("app", "runtime", "pinokio")}
    candidate["agent"] = REFS["candidate-agent"][1]
    candidate["manifest_sha256"] = digest(app / "public/install-manifest.json")
    candidate["artifacts"] = {path: digest(app / path) for path in ARTIFACTS}
    (task / "candidate.json").write_text(json.dumps(candidate, indent=2) + "\n")
    return candidate


def export_evidence(home, task, run_root, result):
    destination = task / "upload"
    destination.mkdir(mode=0o700, exist_ok=True)
    entries = []
    # Runner temp is cleaned after the job; preserve originals under runner home first.
    retained = task / "retained"
    retained.mkdir(mode=0o700, exist_ok=True)
    omissions = []
    for name in ("evidence", "evidence-upload"):
        original = run_root / name
        if not original.exists():
            continue
        for path in original.rglob("*"):
            if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(run_root):
                continue
            relative = path.relative_to(run_root)
            if shutil.disk_usage(home).free < 3_000_000_000 + path.stat().st_size:
                omissions.append(str(relative))
                continue
            target = retained / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
    result["retention_omissions"] = omissions
    sources = [task / "logs", retained / "evidence", retained / "evidence-upload"]
    for base in sources:
        if not base.exists(): continue
        for path in base.rglob("*"):
            if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(task): continue
            relative = path.relative_to(task)
            size = path.stat().st_size
            record = {"path": str(relative), "source_sha256": digest(path), "bytes": size}
            if size <= 16_000_000:
                raw = path.read_bytes()
                raw = raw.replace(str(home).encode(), b"<RUNNER_HOME>")
                raw = re.sub(rb'(?i)(token|password|secret|authorization)(["\']?\s*[:=]\s*["\']?)[^\s,;"\']+', rb'\1\2<REDACTED>', raw)
                dest = destination / relative
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(raw)
                record["artifact_sha256"] = hashlib.sha256(raw).hexdigest()
            else:
                record["omitted"] = "over 16 MB; original retained in task"
            entries.append(record)
    result["evidence"] = entries
    # Extract the complete predicate object before any issue-facing redaction.
    source = task / "retained/evidence-upload/before-cleanup/install.log"
    result["stop_identity"] = {"status": "unavailable"}
    if source.is_file() and not source.is_symlink():
        text = source.read_text(encoding="utf-8", errors="replace")
        marker = "stop_identity="
        for match in re.finditer(re.escape(marker), text):
            try: value, length = json.JSONDecoder().raw_decode(text[match.end():])
            except json.JSONDecodeError: continue
            if isinstance(value, dict) and value.get("format") == "astrid-launchd-owner-predicate/v1":
                raw = text[match.end():match.end() + length].encode()
                safe = raw.replace(str(home).encode(), b"<RUNNER_HOME>")
                (destination / "stop-identity.json").write_bytes(safe + b"\n")
                result["stop_identity"] = {"status": "captured", "source_sha256": hashlib.sha256(raw).hexdigest(),
                                           "artifact_sha256": hashlib.sha256(safe + b"\n").hexdigest()}
                break
    (destination / "result.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    return destination


def main():
    if os.getuid() != UID or os.environ.get("RUNNER_NAME") != "hannah-mac" or sys.platform != "darwin":
        raise RuntimeError("Unexpected runner identity")
    home = Path(pwd.getpwuid(UID).pw_dir)
    owned_dir(home)
    disk(home)
    if not service_absent(): raise RuntimeError("Runtime service is present or absence is unverified")
    user_plist = home / "Library/LaunchAgents/haus.astrid.runtime.plist"
    if user_plist.exists() or user_plist.is_symlink(): raise RuntimeError("Runtime plist already exists")
    py = home / "actions-runner/_work/_tool/Python/3.11.18/arm64/bin/python3.11"
    node = home / "actions-runner/externals/node20/bin/node"
    for exe in (py, node):
        if (not exe.is_file() or not os.access(str(exe), os.X_OK)
                or not exe.resolve().is_relative_to(home) or exe.stat().st_uid != UID):
            raise RuntimeError("Runner-owned interpreter is unavailable")
    probe_env = {"HOME": str(home), "PATH": "/usr/bin:/bin"}
    probe = json.loads(subprocess.check_output([str(py), "-I", "-c",
        "import json,platform,sys; print(json.dumps({'version':platform.python_version(),'arch':platform.machine(),'prefix':sys.prefix}))"],
        env=probe_env, text=True, timeout=20))
    if probe != {"version": "3.11.18", "arch": "arm64", "prefix": str(py.parent.parent)}:
        raise RuntimeError("Runner-owned Python identity differs")
    node_version = subprocess.check_output([str(node), "--version"], env=probe_env, text=True, timeout=20).strip()
    if not node_version.startswith("v20."):
        raise RuntimeError("Runner Node version differs")
    parent = home / ".astrid-r18"
    if not parent.exists() and not parent.is_symlink(): parent.mkdir(mode=0o700)
    owned_dir(parent)
    if stat.S_IMODE(parent.stat().st_mode) != 0o700: raise RuntimeError("Task parent is not private")
    run_id = os.environ.get("GITHUB_RUN_ID", "") + "-" + os.environ.get("GITHUB_RUN_ATTEMPT", "")
    if not re.fullmatch(r"[0-9]+-[0-9]+", run_id): raise RuntimeError("Invalid run id")
    task = parent / run_id; task.mkdir(mode=0o700)
    logs = task / "logs"; logs.mkdir(mode=0o700)
    upload = task / "upload"; upload.mkdir(mode=0o700)
    with open(os.environ["GITHUB_ENV"], "a") as out: out.write("R18_UPLOAD=" + str(upload) + "\n")
    runner_temp = Path(os.environ.get("RUNNER_TEMP", ""))
    if runner_temp != home / "actions-runner/_work/_temp":
        raise RuntimeError("Unexpected GitHub runner temporary directory")
    owned_dir(runner_temp)
    run_root = runner_temp / ("r18-" + run_id)
    if run_root.exists() or run_root.is_symlink(): raise RuntimeError("Run root already exists")
    env = environment(home, task, py.parent, node.parent, runner_temp)
    result = {"status": "failed", "refs": {k:v[1] for k,v in REFS.items()},
              "interpreter": "runner-owned Astral CPython 3.11.18; requested Homebrew Python not used",
              "node": "runner bundled node20 " + node_version, "stages": {}, "disk_free_before": disk(home)}
    try:
        for name, (repo, sha) in REFS.items():
            stage("clone-" + name, ["/usr/bin/git", "-c", "core.hooksPath=/dev/null", "clone", "--filter=blob:none", "--no-checkout", "https://github.com/banodoco/" + repo + ".git", name], task, env, logs, home)
            stage("fetch-" + name, ["/usr/bin/git", "-C", name, "-c", "core.hooksPath=/dev/null", "fetch", "--depth=1", "origin", sha], task, env, logs, home)
            stage("checkout-" + name, ["/usr/bin/git", "-C", name, "-c", "core.hooksPath=/dev/null", "checkout", "--detach", sha], task, env, logs, home)
            got = subprocess.check_output(["/usr/bin/git", "-C", str(task / name), "rev-parse", "HEAD"], env=env, text=True).strip()
            if got != sha: raise RuntimeError("Checkout identity differs")
            result["stages"]["checkout-" + name] = "pass"
        stage("manifest-update", [str(node), "scripts/installer/update-manifest.mjs", "--runtime-checkout", "../runtime", "--agent-checkout", "../candidate-agent", "--write"], task / "app", env, logs, home)
        result["stages"]["manifest-update"] = "pass"
        stage("manifest-pin-check", [str(node), "scripts/installer/pin.mjs", "--check"], task / "app", env, logs, home)
        result["stages"]["manifest-pin-check"] = "pass"
        result["candidate"] = make_candidate(task)
        stage("prepare-only", [str(py), str(task / "driver/scripts/check_journey_evidence.py"), "--candidate", str(task / "candidate.json"), "--app-checkout", str(task / "app"), "--prepare-only"], task, env, logs, home)
        result["stages"]["prepare-only"] = "pass"
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0)); port = sock.getsockname()[1]
        if port == 17340: raise RuntimeError("Unexpected default port")
        env["ASTRID_RUNTIME_DOOR_PORT"] = str(port)
        argv = [str(py), "-u", str(task / "driver/scripts/ci_journey.py"), "--job-id", "direct-macos", "--mode", "direct", "--leg", "release0-to-release1", "--candidate", str(task / "candidate.json"), "--app-checkout", str(task / "app"), "--candidate-agent-checkout", str(task / "candidate-agent"), "--candidate-runtime-checkout", str(task / "runtime"), "--pinokio-checkout", str(task / "pinokio"), "--run-root", str(run_root), "--root", str(run_root / "program"), "--data-root", str(run_root / "data"), "--shell", "sh", "--release0-fixture", str(task / "driver/tests/fixtures/release0"), "--allow-release0-to-release1"]
        disk(home)
        result["journey"] = run_driver(argv, task, env, home, logs)
        result["status"] = "journey-exited"
    except Exception as exc:
        result["error"] = str(exc).replace(str(home), "<RUNNER_HOME>")
    finally:
        result["disk_free_after"] = shutil.disk_usage(home).free
        try: result["service_absent_after"] = service_absent()
        except Exception: result["service_absent_after"] = "unverified"
        export_evidence(home, task, run_root, result)
        print(json.dumps({"status": result["status"], "service_absent_after": result["service_absent_after"], "journey": result.get("journey"), "run_id": run_id}))
    if result["status"] != "journey-exited" or result["journey"]["exit"] != 0: raise SystemExit(1)


if __name__ == "__main__":
    try: main()
    except Exception:
        print(json.dumps({"status": "preflight-failed", "reason": "Runner identity, disk, service or interpreter preflight failed"}))
        raise SystemExit(1)
