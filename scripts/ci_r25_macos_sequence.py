#!/usr/bin/env python3
"""Run the explicitly pinned R25 sequence and stage redacted evidence."""
import hashlib
import json
import os
import pwd
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

UID = 503
PIN = "f0f51d6761f0409dba74e24c051f17b8c021b641"
SEQUENCE_SHA256 = "a5b196da7647e9ae8a02374f2bd6dc19597d511e56978e5a208581276e0b4a3d"
SLOTS = ("pinokio-backend-macos", "pinokio-fully-local-macos")


def redact(text, home):
    text = text.replace(str(home), "<RUNNER_HOME>")
    text = re.sub(r"/Users/[^/\s\"']+", "<REDACTED_HOME>", text)
    text = re.sub(r"\b(?:gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+|sk-[A-Za-z0-9_-]+)", "<REDACTED_TOKEN>", text)
    text = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~+/-]+", r"\1<REDACTED>", text)
    text = re.sub(r'(?i)(["\'](?:token|api[_-]?key|secret|password)["\']\s*:\s*["\'])[^"\']*', r'\1<REDACTED>', text)
    return text


def stage_file(source, destination, home):
    if not source.is_file() or source.is_symlink():
        return False
    if source.stat().st_size > 2_000_000:
        return False
    data = source.read_bytes().decode("utf-8", errors="replace")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(redact(data, home), encoding="utf-8")
    return True


def stage_evidence(run_root, output, home, supervisor, status):
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    stage_file(supervisor, output / "supervisor.log", home)
    selected = ("setup.stdout.log", "setup.stderr.log", "sequence.env", "route.exit", "sequence.exit", "cleanup.exit", "cleanup.log")
    if run_root is not None:
        for name in selected:
            stage_file(run_root / name, output / name, home)
        original = run_root / "results/SHA256SUMS"
        stage_file(original, output / "source-SHA256SUMS.txt", home)
        results = run_root / "results"
        if results.is_dir() and not results.is_symlink():
            total = 0
            for source in sorted(results.rglob("*")):
                if source.is_symlink() or not source.is_file() or source == original:
                    continue
                if source.suffix.lower() not in {".json", ".log", ".txt", ".exit"} and source.name != "SHA256SUMS":
                    continue
                size = source.stat().st_size
                if size > 2_000_000 or total + size > 40_000_000:
                    status["omitted_large_files"] = status.get("omitted_large_files", 0) + 1
                    continue
                if stage_file(source, output / "results" / source.relative_to(results), home):
                    total += size
    def exit_value(path):
        if not path.is_file(): return "not-recorded"
        value = path.read_text(encoding="utf-8").strip()
        return value if re.fullmatch(r"-?[0-9]{1,3}", value) else "invalid"
    status["sequence_exit"] = exit_value(output / "sequence.exit")
    status["route_exit"] = exit_value(output / "route.exit")
    status["cleanup_exit"] = exit_value(output / "cleanup.exit")
    status["runner_exits"] = {slot: exit_value(output / "results" / slot / "runner.exit") for slot in SLOTS}
    if status.get("status") == "complete" and (
            any(status[key] != "0" for key in ("sequence_exit", "route_exit", "cleanup_exit"))
            or any(value != "0" for value in status["runner_exits"].values())):
        status["status"] = "failed"
        status["reason"] = "A required sequence, route, cleanup or runner exit is missing or nonzero"
    tree = sorted(str(path.relative_to(output)) for path in output.rglob("*") if path.is_file() and not path.is_symlink())
    (output / "results-tree.txt").write_text("\n".join(redact(path, home) for path in tree) + "\n")
    (output / "status.json").write_text(json.dumps(status, indent=2, sort_keys=True) + "\n")
    (output / "EVIDENCE-README.txt").write_text(
        "SHA256SUMS hashes this redacted artifact. source-SHA256SUMS.txt, when present, "
        "is the original sequence manifest for unredacted pre-cleanup files; it may name files removed by cleanup.\n")
    entries = []
    for path in sorted(output.rglob("*")):
        if path.is_file() and not path.is_symlink() and path.name != "SHA256SUMS":
            entries.append(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.relative_to(output).as_posix()}")
    (output / "SHA256SUMS").write_text("\n".join(entries) + "\n", encoding="utf-8")
    return status


def command(args, env, *, cwd=None, log=None):
    result = subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True, timeout=300, check=False)
    if result.returncode:
        if log is not None:
            with log.open("a", encoding="utf-8") as stream:
                stream.write(f"Source checkout command failed, exit {result.returncode}\n")
                stream.write(result.stdout[-100000:] + result.stderr[-100000:])
        raise RuntimeError("Required preflight or source checkout command failed")
    return result.stdout.strip()


def preflight(home, temp, env):
    if os.getuid() != UID or os.environ.get("RUNNER_NAME") != "hannah-mac":
        raise RuntimeError("Wrong runner identity")
    if temp != home / "actions-runner/_work/_temp" or temp != temp.resolve():
        raise RuntimeError("Unexpected runner temporary directory")
    if shutil.disk_usage(home).free < 10_000_000_000:
        raise RuntimeError("Insufficient disk for R25 sequence")
    if command(["uname", "-s"], env) != "Darwin" or command(["uname", "-m"], env) != "arm64":
        raise RuntimeError("R25 requires macOS arm64")
    if command(["node", "--version"], env) != "v20.19.4":
        raise RuntimeError("R25 requires Node v20.19.4")
    domain = subprocess.run(["launchctl", "print", f"gui/{UID}/haus.astrid.runtime"], env=env,
                            capture_output=True, text=True, timeout=20, check=False)
    missing = f'Could not find service "haus.astrid.runtime" in domain for user gui: {UID}'
    if domain.returncode != 113 or domain.stderr.strip().replace("\r\n", "\n") not in {missing, f"Bad request.\n{missing}"}:
        raise RuntimeError("Runner Astrid LaunchAgent is present or unknown")
    port = subprocess.run(["lsof", "-nP", "-iTCP:42000"], env=env, capture_output=True, text=True, timeout=20, check=False)
    if port.returncode != 1 or port.stdout.strip() or port.stderr.strip():
        raise RuntimeError("Port 42000 is occupied or cannot be checked")
    return command(["brew", "--prefix", "python@3.11"], env) + "/bin/python3.11"


def execute():
    temp = Path(os.environ["RUNNER_TEMP"])
    output = temp / "r25-artifact"
    output.mkdir(mode=0o700, exist_ok=False)
    supervisor = temp / "r25-supervisor.log"
    home = Path(pwd.getpwuid(UID).pw_dir)
    config = temp / "r25-git-config"
    config.mkdir(mode=0o700)
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin"), "HOME": str(home),
           "USER": "astrid-runner", "LOGNAME": "astrid-runner", "TMPDIR": str(temp),
           "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
           "XDG_CONFIG_HOME": str(config), "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_COUNT": "2",
           "GIT_CONFIG_KEY_0": "credential.helper", "GIT_CONFIG_VALUE_0": "",
           "GIT_CONFIG_KEY_1": "core.hooksPath", "GIT_CONFIG_VALUE_1": "/dev/null"}
    run_root = None
    status = {"status": "failed", "command_exit": "not-run", "aborted_low_disk": False}
    supervisor.touch(mode=0o600)
    try:
        python = preflight(home, temp, env)
        clone = temp / "astrid-pinokio-r25"
        if clone.exists() or clone.is_symlink(): raise RuntimeError("Source checkout already exists")
        command(["git", "clone", "https://github.com/banodoco/astrid-pinokio.git", str(clone)], env, cwd=temp, log=supervisor)
        command(["git", "fetch", "origin", PIN], env, cwd=clone, log=supervisor)
        command(["git", "checkout", "--detach", "FETCH_HEAD"], env, cwd=clone, log=supervisor)
        if command(["git", "rev-parse", "HEAD"], env, cwd=clone) != PIN:
            raise RuntimeError("Sequence source commit differs")
        sequence = clone / ".github/venue/r25-option-b-macos-sequence.sh"
        if sequence.is_symlink() or hashlib.sha256(sequence.read_bytes()).hexdigest() != SEQUENCE_SHA256:
            raise RuntimeError("Sequence source checksum differs")
        env["PYTHON_BIN"] = python
        env["PYTHON"] = python
        if shutil.disk_usage(home).free < 3_000_000_000:
            status["aborted_low_disk"] = True
            raise RuntimeError("Disk fell below the abort threshold before the sequence")
        with supervisor.open("ab") as log:
            child = subprocess.Popen(["bash", ".github/venue/r25-option-b-macos-sequence.sh"], cwd=clone, env=env,
                                     stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            while child.poll() is None:
                if shutil.disk_usage(home).free < 3_000_000_000:
                    status["aborted_low_disk"] = True
                    os.killpg(child.pid, signal.SIGTERM)
                    try: child.wait(timeout=45)
                    except subprocess.TimeoutExpired:
                        os.killpg(child.pid, signal.SIGKILL)
                        child.wait()
                    break
                time.sleep(5)
            status["command_exit"] = child.returncode
        text = supervisor.read_text(encoding="utf-8", errors="replace")
        matches = re.findall(r"(?m)^R25_RUN_ROOT=(.+)$", text)
        if len(matches) == 1:
            candidate = Path(matches[0])
            parent = home / "r25-option-b-runs"
            if (candidate.parent == parent and re.fullmatch(r"run\.[A-Za-z0-9]+", candidate.name)
                    and not parent.is_symlink() and parent.stat().st_uid == UID
                    and not candidate.is_symlink() and candidate.is_dir() and candidate.stat().st_uid == UID
                    and candidate.resolve().parent == parent.resolve()):
                run_root = candidate
                status["run_root"] = "~/r25-option-b-runs/" + candidate.name
        status["status"] = "complete" if child.returncode == 0 else "failed"
    except Exception as exc:
        status["reason"] = str(exc) if type(exc) is RuntimeError else "System or data error"
    status["free_bytes_after"] = shutil.disk_usage(home).free
    stage_evidence(run_root, output, home, supervisor, status)
    print(json.dumps(status, sort_keys=True))
    if status["status"] == "complete" and status["cleanup_exit"] == "0":
        (temp / "r25-success").write_text("ok\n")


if __name__ == "__main__":
    execute()
