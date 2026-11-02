#!/usr/bin/env python3
"""Materialize and execute the hosted Linux R-24 vertical slice."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
import uuid
from pathlib import Path
from typing import Any

try:
    from . import ci_journey_provider as provider
    from .ci_journey_r24 import PUBLISHED_CANDIDATE
except ImportError:
    import ci_journey_provider as provider
    from ci_journey_r24 import PUBLISHED_CANDIDATE


PINOKIOD_REF = "add4a674ad1ba95bb62ec427fce519fe5bf0e9bf"
PTERM_REF = "5be43718701c448b43daf0ce51d8935f170065bb"
CONFIRMATION = "R24_LIVE_CURRENT_TUPLE"
PORT = 42000
HEX40 = re.compile(r"[0-9a-f]{40}\Z")
HEX32 = re.compile(r"[0-9a-f]{32}\Z")


class MaterializerError(RuntimeError):
    pass


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(json.dumps(value, indent=2, sort_keys=True,
                                allow_nan=False).encode("ascii") + b"\n")


def _read(path: Path) -> Any:
    if path.is_symlink() or not path.is_file():
        raise MaterializerError(f"missing regular file: {path}")
    return json.loads(path.read_bytes())


def _git(root: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(root), *args], stderr=subprocess.DEVNULL,
        text=True, timeout=30,
    ).strip()


def validate_checkout(root: Path, ref: str, *, tracked_only: bool = False) -> Path:
    if not root.is_absolute() or root.is_symlink() or not root.is_dir():
        raise MaterializerError("absolute nonsymlink checkout required")
    selected = root.resolve(strict=True)
    dirty_args = ["status", "--porcelain=v1",
                  "--untracked-files=no" if tracked_only else "--untracked-files=all"]
    if (Path(_git(selected, "rev-parse", "--show-toplevel")).resolve() != selected
            or _git(selected, "rev-parse", "HEAD") != ref
            or _git(selected, *dirty_args)):
        raise MaterializerError(f"checkout identity mismatch: {root.name}")
    return selected


def pterm_argv(node: Path, pterm: Path, script: str) -> list[str]:
    return [str(node), str(pterm / "index.js"), "start", script,
            "--ref", f"pinokio://127.0.0.1:{PORT}/api/astrid"]


def route_argv(runner: Path, run_root: Path, candidate: Path, config: Path,
               producer_root: Path, checkouts: dict[str, Path]) -> list[str]:
    return [sys.executable, "-B", str(runner / "scripts/ci_journey.py"),
            "--shared-executor", "r24-live",
            "--run-root", str(run_root), "--candidate", str(candidate),
            "--config", str(config), "--producer-checkout", str(producer_root),
            "--app-checkout", str(checkouts["app"]),
            "--agent-checkout", str(checkouts["agent"]),
            "--runtime-checkout", str(checkouts["runtime"]),
            "--pinokio-checkout", str(checkouts["pinokio"])]


def _run(argv: list[str], *, log: Path, receipt: dict[str, Any], label: str,
         env: dict[str, str] | None = None, cwd: Path | None = None,
         timeout: int = 3600, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    started = time.monotonic()
    with log.open("wb") as stream:
        process = subprocess.run(argv, stdout=stream, stderr=subprocess.STDOUT,
                                 cwd=cwd, env=env, timeout=timeout)
    receipt.setdefault("commands", []).append({
        "label": label, "argv": argv, "exit": process.returncode,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "log": str(log), "log_sha256": _sha(log),
    })
    if check and process.returncode:
        raise MaterializerError(f"{label} exited {process.returncode}")
    return process


def materialize(resource: Path, pinokio: Path, producer_root: Path,
                scratch: Path, receipt: dict[str, Any]) -> None:
    if resource.exists() or resource.is_symlink():
        raise MaterializerError("materialized resource path already exists")
    resource.mkdir(parents=True)
    archive = scratch / "candidate-pinokio.tar"
    _run(["git", "-C", str(pinokio), "archive", "--format=tar",
          "--output", str(archive), PUBLISHED_CANDIDATE["pinokio"]],
         log=scratch / "git-archive.log", receipt=receipt, label="materialize-archive")
    _run(["tar", "-xf", str(archive), "-C", str(resource)],
         log=scratch / "tar-extract.log", receipt=receipt, label="materialize-extract")
    archive.unlink()
    for relative in ("pinokio/providers/r24-action.cjs",
                     "pinokio/providers/r24-live-action.js"):
        source = producer_root / relative
        expected = provider.LIVE_PRODUCER_FILES[relative]
        if _sha(source) != expected:
            raise MaterializerError("producer action digest mismatch")
        target = resource / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    environment = resource / "_ENVIRONMENT"
    raw = environment.read_text(encoding="utf-8")
    changed, count = re.subn(r"(?m)^ASTRID_APP_REF=.*$",
                             f"ASTRID_APP_REF={PUBLISHED_CANDIDATE['app']}", raw)
    if count != 1:
        raise MaterializerError("candidate App ref setting unavailable")
    environment.write_text(changed, encoding="utf-8")


def _engine_environment(home: Path) -> dict[str, str]:
    env = dict(os.environ)
    env.update({
        "HOME": str(home / "user-home"),
        "XDG_DATA_HOME": str(home / "xdg-data"),
        "PINOKIO_HOME": str(home),
        "PINOKIO_HTTPS_ACTIVE": "0",
        "PINOKIO_NETWORK_ACTIVE": "0",
        "ASTRID_APP_REF": PUBLISHED_CANDIDATE["app"],
    })
    return env


def _wait_engine(process: subprocess.Popen[bytes], timeout: int = 240) -> None:
    deadline = time.monotonic() + timeout
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise MaterializerError(f"pinokiod exited before readiness: {process.returncode}")
        try:
            with opener.open(f"http://127.0.0.1:{PORT}/pinokio/version", timeout=2) as response:
                if response.status == 200:
                    return
        except OSError:
            pass
        time.sleep(1)
    raise MaterializerError("pinokiod readiness timeout")


def _wait_runtime(data: Path, start: subprocess.Popen[bytes], timeout: int = 900) -> dict[str, Any]:
    discovery = data / "runtime/discovery.json"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if start.poll() is not None:
            raise MaterializerError(f"Pinokio Start client exited early: {start.returncode}")
        try:
            value = _read(discovery)
            endpoint = value["endpoint"]
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            request = urllib.request.Request(endpoint + "/v1/health")
            with opener.open(request, timeout=3) as response:
                health = json.load(response)
            if (response.status == 200 and health.get("status") == "ok"
                    and health.get("runtime_instance_id") == value.get("runtime_instance_id")):
                return value
        except (OSError, ValueError, KeyError):
            pass
        time.sleep(2)
    raise MaterializerError("installed Runtime readiness timeout")


def _proc_start_ticks(pid: int) -> str:
    raw = (Path("/proc") / str(pid) / "stat").read_text(encoding="ascii")
    fields = raw[raw.rfind(")") + 2:].split()
    if len(fields) < 20:
        raise MaterializerError("pinokiod process identity unavailable")
    return fields[19]


def build_config(*, checkouts: dict[str, Path], pterm: Path, pinokiod: Path,
                 node: Path, daemon_pid: int, home: Path, scratch: Path) -> dict[str, Any]:
    return {
        "checkouts": {key: str(value) for key, value in checkouts.items()},
        "pterm": str(pterm),
        "pinokiod": str(pinokiod),
        "node": str(node),
        "node_sha256": _sha(node),
        "pterm_entry_sha256": _sha(pterm / "index.js"),
        "unws_sha256": _sha(pterm / "node_modules/unws/src/node.cjs"),
        "engine": {
            "pid": daemon_pid,
            "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
            "start_ticks": _proc_start_ticks(daemon_pid),
            "pinokio_home": str(home),
            "port": PORT,
        },
        "scratch_root": str(scratch),
    }


def _installed(backend: Path) -> tuple[dict[str, Any], Path]:
    install = _read(backend / "install.json")
    if (install.get("owner") != "haus.astrid.runtime"
            or install.get("managed_by") != "pinokio"):
        raise MaterializerError("genuine Pinokio install marker unavailable")
    data = Path(str(install.get("data_root", ""))).resolve(strict=True)
    current = (backend / "current").read_text(encoding="utf-8").strip()
    version = _read(backend / "versions" / current / "version.json")
    if version.get("identity") != {"release": 1, "managed_by": "pinokio"}:
        raise MaterializerError("installed candidate identity mismatch")
    return version, data


def _installed_env(version: dict[str, Any], data: Path) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items()
           if key not in {"PYTHONPATH", "PYTHONHOME"}}
    selected = version.get("env") or {}
    if not isinstance(selected, dict) or any(not isinstance(key, str) or not isinstance(value, str)
                                            for key, value in selected.items()):
        raise MaterializerError("installed environment mismatch")
    env.update(selected)
    env["ASTRID_RUNTIME_DATA_ROOT"] = str(data)
    env["ASTRID_INSTALL_MANAGED_BY"] = "pinokio"
    env["ASTRID_HOST_EXECUTION_PROFILE"] = "installed"
    return env


def _task_id(invocation: Path) -> str:
    value = _read(invocation)
    refs = (value.get("details") or {}).get("astridTaskRefs") or []
    if len(refs) != 1 or not HEX32.fullmatch(str(refs[0].get("task_id", ""))):
        raise MaterializerError("installed Agent did not return one task identity")
    return refs[0]["task_id"]


def _terminate(process: subprocess.Popen[bytes] | None, timeout: int = 10) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=timeout)


def _hosted_binding(receipt: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
    workflow = {
        "repository": os.environ.get("GITHUB_REPOSITORY"),
        "workflow": os.environ.get("GITHUB_WORKFLOW"),
        "workflow_ref": os.environ.get("GITHUB_WORKFLOW_REF"),
        "workflow_sha": os.environ.get("GITHUB_WORKFLOW_SHA"),
        "run_id": os.environ.get("GITHUB_RUN_ID"),
        "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
        "job": os.environ.get("GITHUB_JOB"),
        "sha": os.environ.get("GITHUB_SHA"),
    }
    if any(value in (None, "") for value in workflow.values()):
        raise MaterializerError("hosted Actions binding unavailable")
    return {
        "schema": "astrid.r24.hosted-observation-binding.v1",
        "workflow": workflow,
        "run_id": receipt["run_id"],
        "session": receipt["session"],
        "request_sha256": receipt["request_sha256"],
        "task": {
            "task_id": task["task"]["task_id"],
            "run_id": task["task"]["run_id"],
            "state": task["task"]["state"],
            "execution_binding": task["task"]["execution_binding"],
        },
        "candidate": PUBLISHED_CANDIDATE,
        "provider_ref": provider.LIVE_PRODUCER_REF,
        "export": receipt["export"],
        "qualification": "not-evaluated",
    }


def execute(args: argparse.Namespace) -> int:
    run_root = args.run_root.resolve()
    if run_root.exists() or run_root.is_symlink():
        raise MaterializerError("fresh run root required")
    run_root.mkdir(parents=True)
    evidence = run_root / "evidence/materializer"
    evidence.mkdir(parents=True)
    receipt_path = evidence / "r24-materializer.json"
    receipt: dict[str, Any] = {
        "schema": "astrid.r24.materializer.v1", "status": "running",
        "stage": "preflight", "first_stop": None, "commands": [],
        "candidate": PUBLISHED_CANDIDATE,
        "provider_ref": provider.LIVE_PRODUCER_REF,
        "hosted": {key: os.environ.get(key) for key in
                   ("GITHUB_REPOSITORY", "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT",
                    "GITHUB_JOB", "GITHUB_WORKFLOW_REF", "GITHUB_WORKFLOW_SHA", "GITHUB_SHA")},
    }
    _write(receipt_path, receipt)
    daemon = None
    start = None
    daemon_log = None
    start_log = None
    backend = None
    task_id = None
    installed_env = None
    try:
        if sys.platform != "linux" or args.confirmation != CONFIRMATION:
            raise MaterializerError("Linux and exact confirmation are required")
        runner = validate_checkout(args.runner, args.runner_ref)
        if args.producer_ref != provider.LIVE_PRODUCER_REF:
            raise MaterializerError("producer ref differs from audited live producer")
        producer_root = validate_checkout(args.producer, args.producer_ref)
        checkouts = {
            name: validate_checkout(getattr(args, name), PUBLISHED_CANDIDATE[name])
            for name in ("app", "agent", "runtime", "pinokio")
        }
        pterm = validate_checkout(args.pterm, PTERM_REF, tracked_only=True)
        pinokiod = validate_checkout(args.pinokiod, PINOKIOD_REF)
        roots = {runner, producer_root, pterm, pinokiod, *checkouts.values()}
        if len(roots) != 8:
            raise MaterializerError("all source checkouts must be distinct")
        manifest = checkouts["app"] / "public/install-manifest.json"
        if _sha(manifest) != PUBLISHED_CANDIDATE["manifest_sha256"]:
            raise MaterializerError("published manifest digest mismatch")
        node = Path(shutil.which("node") or "").resolve(strict=True)
        scratch = run_root / "scratch"
        scratch.mkdir()
        home = run_root / "pinokio-home"
        for path in (home, home / "api", home / "user-home", home / "xdg-data"):
            path.mkdir(mode=0o700)
        resource = home / "api/astrid"
        receipt["stage"] = "materialize-candidate"
        _write(receipt_path, receipt)
        materialize(resource, checkouts["pinokio"], producer_root, scratch, receipt)

        receipt["stage"] = "start-pinokiod"
        _write(receipt_path, receipt)
        engine_env = _engine_environment(home)
        daemon_log = (evidence / "pinokiod.log").open("wb")
        daemon = subprocess.Popen(
            [str(node), str(pinokiod / "script/index.js")], cwd=pinokiod,
            env=engine_env, stdout=daemon_log, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        _wait_engine(daemon)

        receipt["stage"] = "install-candidate"
        _write(receipt_path, receipt)
        _run(pterm_argv(node, pterm, "pinokio/install.js"),
             log=evidence / "install.log", receipt=receipt, label="pinokio-install",
             env=engine_env, cwd=home, timeout=3600)
        backend = resource / "app/astrid"
        version, data = _installed(backend)
        owner = {"schema_version": 1, "run_id": uuid.uuid4().hex,
                 "root": str(backend.resolve()), "data_root": str(data)}
        _write(run_root / "tst-b1-owner.json", owner)

        receipt["stage"] = "start-installed-runtime"
        _write(receipt_path, receipt)
        start_log = (evidence / "start.log").open("wb")
        start = subprocess.Popen(
            pterm_argv(node, pterm, "pinokio/start.js"), cwd=home,
            env=engine_env, stdout=start_log, stderr=subprocess.STDOUT,
        )
        discovery = _wait_runtime(data, start)
        installed_env = _installed_env(version, data)

        receipt["stage"] = "authenticate-and-start-worker"
        _write(receipt_path, receipt)
        helper = runner / "scripts/ci_journey_r24_task.py"
        python = str(version["python"])
        worker_file = evidence / "worker-ready.json"
        _run([python, "-I", str(helper), "ensure-worker", "--backend", str(backend),
              "--output", str(worker_file)], log=evidence / "worker-ready.log",
             receipt=receipt, label="authenticate-health-start-local-worker",
             env=installed_env, timeout=900)

        receipt["stage"] = "admit-installed-agent-task"
        _write(receipt_path, receipt)
        project = f"r24-live-{owner['run_id'][:12]}"
        _run([python, "-I", "-m", "astrid", "projects", "create", project,
              "--name", "R-24 hosted live task", "--json"],
             log=evidence / "project.json", receipt=receipt,
             label="installed-agent-create-project", env=installed_env, timeout=120)
        invocation = evidence / "omp-invocation.json"
        omp_request = {
            "capability_id": "chiptune.compose", "kind": "executor", "project": project,
            "inputs": {"duration_s": 600, "bpm": 132, "key": "A minor", "seed": 24,
                       "sections": [{"start_s": 0, "end_s": 600,
                                     "energy": 0.8, "mood": "tense"}]},
            "execution_request": {"target": {"kind": "profile", "id": "astrid"}},
        }
        _run([python, "-I", "-m", "astrid.omp_invoke",
              json.dumps(omp_request, sort_keys=True, separators=(",", ":"))],
             log=invocation, receipt=receipt, label="installed-agent-admit-task-target",
             env=installed_env, timeout=180)
        task_id = _task_id(invocation)
        running_file = evidence / "task-running.json"
        _run([python, "-I", str(helper), "observe", "--backend", str(backend),
              "--task-id", task_id, "--timeout", "180", "--output", str(running_file)],
             log=evidence / "task-running.log", receipt=receipt,
             label="installed-client-get-task-running", env=installed_env, timeout=240)

        receipt["stage"] = "invoke-integrated-provider"
        _write(receipt_path, receipt)
        candidate_file = run_root / "candidate.json"
        config_file = run_root / "config.json"
        _write(candidate_file, PUBLISHED_CANDIDATE)
        _write(config_file, build_config(
            checkouts=checkouts, pterm=pterm, pinokiod=pinokiod, node=node,
            daemon_pid=daemon.pid, home=home, scratch=scratch))
        _run(route_argv(runner, run_root, candidate_file, config_file,
                        producer_root, checkouts),
             log=evidence / "r24-live-route.log", receipt=receipt,
             label="documented-r24-live-route", env=dict(os.environ), timeout=300)
        provider_receipt = _read(run_root / "evidence/r24-live/r24-invocation.json")
        task_running = _read(running_file)
        hosted = _hosted_binding(provider_receipt, task_running)
        _write(run_root / "evidence/r24-live/hosted-binding.json", hosted)
        receipt.update(status="captured-untrusted", stage="settle-task",
                       run_id=owner["run_id"], task_id=task_id,
                       provider_session=provider_receipt["session"],
                       request_sha256=provider_receipt["request_sha256"],
                       runtime={"discovery": discovery,
                                "worker": _read(worker_file)["generation"]})
        _write(receipt_path, receipt)

        settled = evidence / "task-settled.json"
        _run([python, "-I", str(helper), "settle", "--backend", str(backend),
              "--task-id", task_id, "--timeout", "300", "--output", str(settled)],
             log=evidence / "task-settled.log", receipt=receipt,
             label="installed-client-cancel-settle-task", env=installed_env, timeout=360)
        task_id = None
        receipt["stage"] = "stop-installed-runtime"
        _write(receipt_path, receipt)
        _run(pterm_argv(node, pterm, "pinokio/stop.js"),
             log=evidence / "stop.log", receipt=receipt, label="pinokio-stop",
             env=engine_env, cwd=home, timeout=300)
        receipt.update(status="complete", stage="complete")
        _write(receipt_path, receipt)
        return 0
    except Exception as exc:
        receipt.update(status="blocked", first_stop=receipt.get("stage"),
                       error=f"{type(exc).__name__}: {exc}")
        _write(receipt_path, receipt)
        return 1
    finally:
        if task_id and backend is not None and installed_env is not None:
            try:
                version, _data = _installed(backend)
                _run([str(version["python"]), "-I",
                      str(args.runner / "scripts/ci_journey_r24_task.py"), "settle",
                      "--backend", str(backend), "--task-id", task_id,
                      "--timeout", "120", "--output", str(evidence / "task-cleanup.json")],
                     log=evidence / "task-cleanup.log", receipt=receipt,
                     label="failure-cleanup-task", env=installed_env,
                     timeout=180, check=False)
            except Exception:
                pass
        _terminate(start)
        if start_log is not None:
            start_log.close()
        if daemon is not None and daemon.poll() is None:
            try:
                os.killpg(daemon.pid, signal.SIGTERM)
                daemon.wait(timeout=10)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    os.killpg(daemon.pid, signal.SIGKILL)
                except OSError:
                    pass
                try:
                    daemon.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    pass
        if daemon_log is not None:
            daemon_log.close()
        _write(receipt_path, receipt)


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser()
    for name in ("runner", "producer", "app", "agent", "runtime", "pinokio",
                 "pterm", "pinokiod", "run-root"):
        value.add_argument("--" + name, type=Path, required=True)
    value.add_argument("--runner-ref", required=True)
    value.add_argument("--producer-ref", required=True)
    value.add_argument("--confirmation", required=True)
    return value


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if not HEX40.fullmatch(args.runner_ref) or not HEX40.fullmatch(args.producer_ref):
        raise MaterializerError("full runner and producer commit SHAs required")
    return execute(args)


if __name__ == "__main__":
    raise SystemExit(main())
