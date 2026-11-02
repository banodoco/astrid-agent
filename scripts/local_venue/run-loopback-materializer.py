#!/usr/bin/env python3
"""Run the exact R-24 materializer with a task-local loopback bind shim."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

HOST = "127.0.0.1"
PORT = 42000
PINOKIOD_REF = "add4a674ad1ba95bb62ec427fce519fe5bf0e9bf"
SETUP_MODE = "managed-conda"
SETUP_TIMEOUT_SECONDS = 2400
MIN_FREE_BYTES = 2 * 1024 * 1024 * 1024
FAILURE_MARKER = b"ASTRID_PINOKIO_FAILURE"
UV_SYSTEM_CERTS_VALUE = "true"
INSTALL_ACTION_CALL = "app.shellStep(app.bootstrapScript('install'))"
INSTALL_ACTION_CALL_WITH_TRUST = (
    "app.shellStep(app.bootstrapScript('install'), "
    "{ env: { UV_SYSTEM_CERTS: 'true' } })"
)
MaterializerError = RuntimeError


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _argument(argv: list[str], name: str) -> Path:
    try:
        value = argv[argv.index(name) + 1]
    except (ValueError, IndexError) as exc:
        raise SystemExit(f"missing required launcher argument: {name}") from exc
    selected = Path(value)
    if not selected.is_absolute():
        raise SystemExit(f"launcher argument must be absolute: {name}")
    return selected


def _require_loopback_port_free(port: int = PORT) -> None:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind((HOST, port))
    except OSError as exc:
        raise MaterializerError(
            f"local-only loopback port {port} is unavailable before pinokiod launch"
        ) from exc


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="ascii",
    )


def _local_venue_environment(env: dict[str, str]) -> dict[str, str]:
    """Propagate system trust to UV without changing TLS verification."""
    selected = dict(env)
    selected["UV_SYSTEM_CERTS"] = UV_SYSTEM_CERTS_VALUE
    return selected


def _patch_materialized_install_action(resource: Path) -> Path:
    """Add the trust setting through Pinokio's explicit local shell env seam."""
    action = resource / "pinokio/install.js"
    raw = action.read_text(encoding="utf-8")
    if raw.count(INSTALL_ACTION_CALL) != 1:
        raise MaterializerError("pinned install action shape is not the audited local seam")
    action.write_text(
        raw.replace(INSTALL_ACTION_CALL, INSTALL_ACTION_CALL_WITH_TRUST),
        encoding="utf-8",
    )
    return action


def _stop_process_group(process: subprocess.Popen[bytes], timeout: int = 10) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            pass
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            pass


def _run_bounded(
    argv: list[str],
    *,
    log: Path,
    receipt: dict[str, Any],
    label: str,
    env: dict[str, str],
    cwd: Path,
    timeout: int,
    disk_root: Path,
    receipt_path: Path | None = None,
    failure_marker: bytes | None = None,
) -> subprocess.CompletedProcess[bytes]:
    """Run one task-local child with a disk floor and optional failure fence."""
    started = time.monotonic()
    log.parent.mkdir(parents=True, exist_ok=True)
    stream = log.open("wb")
    try:
        process = subprocess.Popen(
            argv,
            cwd=cwd,
            env=env,
            stdout=stream,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except OSError as exc:
        stream.close()
        record = {
            "label": label,
            "argv": argv,
            "exit": None,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "error": type(exc).__name__,
            "log": str(log),
            "log_sha256": _sha(log),
        }
        receipt.setdefault("commands", []).append(record)
        if receipt_path is not None:
            _write_json(receipt_path, receipt)
        raise MaterializerError(
            f"{label} could not start: {type(exc).__name__}"
        ) from exc

    record: dict[str, Any] = {
        "label": label,
        "argv": argv,
        "pid": process.pid,
        "exit": None,
        "status": "running",
        "timeout_seconds": timeout,
        "log": str(log),
    }
    receipt.setdefault("commands", []).append(record)
    if receipt_path is not None:
        _write_json(receipt_path, receipt)

    marker_observed = False
    marker_tail = b""
    offset = 0
    deadline = started + timeout
    reason: str | None = None
    try:
        try:
            while True:
                stream.flush()
                with log.open("rb") as reader:
                    reader.seek(offset)
                    chunk = reader.read()
                offset += len(chunk)
                if failure_marker is not None and failure_marker in marker_tail + chunk:
                    marker_observed = True
                    reason = "known-failure-marker"
                if failure_marker is not None:
                    marker_tail = (marker_tail + chunk)[-(len(failure_marker) - 1):]

                if reason is not None or process.poll() is not None:
                    break
                if shutil.disk_usage(disk_root).free < MIN_FREE_BYTES:
                    reason = "disk-floor"
                    break
                if time.monotonic() >= deadline:
                    reason = "timeout"
                    break
                time.sleep(0.1)
            if process.poll() is None:
                _stop_process_group(process)
            else:
                process.wait()
        except BaseException:
            _stop_process_group(process)
            raise
    finally:
        stream.close()

    record.update(
        exit=process.poll(),
        status="exited" if process.poll() is not None else "exit-unavailable",
        elapsed_seconds=round(time.monotonic() - started, 3),
        log_sha256=_sha(log),
        known_failure_marker_observed=marker_observed,
        termination_reason=reason,
        free_bytes_after=shutil.disk_usage(disk_root).free,
    )
    if receipt_path is not None:
        _write_json(receipt_path, receipt)
    if reason == "known-failure-marker":
        raise MaterializerError(
            f"{label} emitted {failure_marker.decode('ascii')}; "
            f"child exit {process.poll()}"
        )
    if reason == "disk-floor":
        raise MaterializerError(
            f"{label} stopped at the {MIN_FREE_BYTES}-byte host disk floor; "
            f"child exit {process.poll()}"
        )
    if reason == "timeout":
        raise MaterializerError(
            f"{label} timed out after {timeout} seconds; child exit {process.poll()}"
        )
    if process.returncode:
        raise MaterializerError(f"{label} exited {process.returncode}")
    return subprocess.CompletedProcess(argv, process.returncode)


def _provision_managed_home(
    *,
    pinokiod: Path,
    node: Path,
    home: Path,
    runner_home: Path,
    run_root: Path,
    env: dict[str, str],
) -> None:
    """Provision a fresh home through the pinned engine's official setup API."""
    evidence = run_root / "evidence/materializer"
    receipt_path = evidence / "managed-home-provision.json"
    stock_setup = (pinokiod / "script/install-mode.js").resolve(strict=True)
    setup = (Path(__file__).parent / "pinokiod-install-mode.cjs").resolve(strict=True)
    conda_source = (pinokiod / "kernel/bin/conda.js").resolve(strict=True)
    setup_env = dict(env)
    setup_env["PINOKIO_SETUP_MODE"] = SETUP_MODE
    selected_home = home.resolve(strict=True)
    selected_runner_home = runner_home.resolve(strict=True)
    receipt: dict[str, Any] = {
        "schema": "astrid.pinokio.managed-home-provision.v1",
        "status": "running",
        "stage": "official-setup",
        "pinokiod_ref": PINOKIOD_REF,
        "setup_mode": SETUP_MODE,
        "setup_requirements": [{"name": "conda"}],
        "environment_boundary": {
            "pinokio_home": str(selected_home),
            "home": str(selected_runner_home),
            "home_inside_pinokio_home": selected_home in selected_runner_home.parents,
        },
        "source_hashes": {
            "stock_install_mode_sha256": _sha(stock_setup),
            "local_setup_driver_sha256": _sha(setup),
            "conda_module_sha256": _sha(conda_source),
        },
        "local_setup_patch": (
            "initialize the pinned Bin modules before Shells and request only "
            "managed Conda through the official check/install/retry paths"
        ),
        "disk": {
            "minimum_free_bytes": MIN_FREE_BYTES,
            "free_bytes_before": shutil.disk_usage(run_root).free,
        },
        "commands": [],
    }
    _write_json(receipt_path, receipt)
    try:
        _run_bounded(
            [str(node), str(setup), str(pinokiod)],
            log=evidence / "managed-home-setup.log",
            receipt=receipt,
            label="pinokio-managed-home-setup",
            env=setup_env,
            cwd=pinokiod,
            timeout=SETUP_TIMEOUT_SECONDS,
            disk_root=run_root,
            receipt_path=receipt_path,
        )
        conda = home / "bin/miniforge/bin/conda"
        hook = home / "bin/miniforge/etc/profile.d/conda.sh"
        metadata = home / "bin/miniforge/conda-meta"
        if (
            conda.is_symlink()
            or not conda.is_file()
            or not os.access(conda, os.X_OK)
            or hook.is_symlink()
            or not hook.is_file()
            or metadata.is_symlink()
            or not metadata.is_dir()
        ):
            raise MaterializerError(
                "official Pinokio setup did not produce a runnable managed Miniforge"
            )
        receipt["stage"] = "verify-managed-conda"
        _write_json(receipt_path, receipt)
        _run_bounded(
            [str(conda), "--version"],
            log=evidence / "managed-conda-version.log",
            receipt=receipt,
            label="verify-managed-conda-executable",
            env=setup_env,
            cwd=home,
            timeout=30,
            disk_root=run_root,
            receipt_path=receipt_path,
        )
        _run_bounded(
            [str(conda), "shell.bash", "hook"],
            log=evidence / "managed-conda-hook.log",
            receipt=receipt,
            label="verify-managed-conda-hook",
            env=setup_env,
            cwd=home,
            timeout=30,
            disk_root=run_root,
            receipt_path=receipt_path,
        )
        receipt.update(
            status="complete",
            stage="complete",
            postconditions={
                "conda_executable": str(conda),
                "conda_executable_sha256": _sha(conda),
                "conda_hook": str(hook),
                "conda_hook_sha256": _sha(hook),
                "conda_metadata": str(metadata),
            },
        )
    except Exception as exc:
        receipt.update(
            status="blocked",
            first_stop=receipt.get("stage"),
            error=f"{type(exc).__name__}: {exc}",
        )
        raise
    finally:
        receipt["disk"]["free_bytes_after"] = shutil.disk_usage(run_root).free
        _write_json(receipt_path, receipt)


def _write_config(*, run_root: Path, runner: Path, pinokiod: Path,
                  preload: Path, result: int) -> None:
    materializer = runner / "scripts/ci_journey_r24_materializer.py"
    entry = pinokiod / "script/index.js"
    server = pinokiod / "server/index.js"
    config = {
        "schema": "astrid.pinokio.local-venue-bind.v1",
        "scope": "local-reproduction-only",
        "result": result,
        "bind_host": HOST,
        "port": PORT,
        "pterm_endpoint": f"pinokio://{HOST}:{PORT}/api/astrid",
        "hosted_default_changed": False,
        "pinned_checkout_changed": False,
        "preexisting_node_options_preserved": bool(
            os.environ.get("ASTRID_LOCAL_PREEXISTING_NODE_OPTIONS")
        ),
        "effective_node_options_sha256": hashlib.sha256(
            os.environ["NODE_OPTIONS"].encode("utf-8")
        ).hexdigest(),
        "pinokiod_ref": PINOKIOD_REF,
        "hashes": {
            "launcher_sha256": _sha(Path(__file__).resolve()),
            "preload_sha256": _sha(preload),
            "materializer_sha256": _sha(materializer),
            "pinokiod_entry_sha256": _sha(entry),
            "pinokiod_server_sha256": _sha(server),
        },
        "runtime_patch": (
            "one-shot Node preload changes only the pinned entrypoint process's "
            "first listen(42000, callback) call to listen(42000, 127.0.0.1, callback)"
        ),
        "preflight_patch": "probe 127.0.0.1:42000 for this local run only",
    }
    provision = run_root / "evidence/materializer/managed-home-provision.json"
    if provision.is_file() and not provision.is_symlink():
        config["managed_home_provision"] = {
            "path": str(provision),
            "sha256": _sha(provision),
        }
    action = run_root / "pinokio-home/api/astrid/pinokio/install.js"
    if action.is_file() and not action.is_symlink():
        config["local_install_action_overlay"] = {
            "path": str(action),
            "sha256": _sha(action),
            "environment": {"UV_SYSTEM_CERTS": UV_SYSTEM_CERTS_VALUE},
            "tls_verification": "enabled",
        }
    target = run_root / "evidence/materializer/local-venue-bind.json"
    _write_json(target, config)


def main(argv: list[str]) -> int:
    runner = _argument(argv, "--runner").resolve(strict=True)
    pinokiod = _argument(argv, "--pinokiod").resolve(strict=True)
    run_root = _argument(argv, "--run-root").resolve()
    entry = (pinokiod / "script/index.js").resolve(strict=True)
    preload = (Path(__file__).parent / "pinokiod-loopback-preload.cjs").resolve(strict=True)

    sys.path.insert(0, str(runner))

    global MaterializerError
    from scripts import ci_journey_r24_materializer as materializer

    MaterializerError = materializer.MaterializerError
    if materializer.PINOKIOD_REF != PINOKIOD_REF or materializer.PORT != PORT:
        raise SystemExit("pinned materializer daemon contract differs")

    existing_node_options = os.environ.get("NODE_OPTIONS", "").strip()
    if "\x00" in existing_node_options or "\n" in existing_node_options:
        raise SystemExit("unsafe pre-existing NODE_OPTIONS encoding")
    os.environ.update({
        "ASTRID_LOCAL_PINOKIOD_BIND_HOST": HOST,
        "ASTRID_LOCAL_PINOKIOD_ENTRY": str(entry),
        "ASTRID_LOCAL_PREEXISTING_NODE_OPTIONS": "1" if existing_node_options else "",
        "NODE_OPTIONS": f"{existing_node_options} --require={preload}".strip(),
    })
    selected_node = shutil.which("node")
    if selected_node is None:
        raise SystemExit("Node executable is unavailable")
    node = Path(selected_node).resolve(strict=True)
    original_engine_environment = materializer._engine_environment
    original_install_candidate = materializer._install_candidate
    original_materialize = materializer.materialize
    provision_calls = 0

    def provisioned_engine_environment(home: Path, runner_home: Path) -> dict[str, str]:
        nonlocal provision_calls
        provision_calls += 1
        if provision_calls != 1:
            raise MaterializerError("managed home provisioning is single-use")
        env = _local_venue_environment(original_engine_environment(home, runner_home))
        _provision_managed_home(
            pinokiod=pinokiod,
            node=node,
            home=home,
            runner_home=runner_home,
            run_root=run_root,
            env=env,
        )
        return env

    def bounded_install_candidate(**kwargs: Any) -> tuple[dict[str, Any], Path]:
        original_run = materializer._run

        def bounded_run(command: list[str], **run_kwargs: Any) -> Any:
            if run_kwargs.get("label") != "pinokio-install":
                return original_run(command, **run_kwargs)
            return _run_bounded(
                command,
                log=run_kwargs["log"],
                receipt=run_kwargs["receipt"],
                label=run_kwargs["label"],
                env=run_kwargs["env"],
                cwd=run_kwargs["cwd"],
                timeout=run_kwargs["timeout"],
                disk_root=run_root,
                failure_marker=FAILURE_MARKER,
            )

        materializer._run = bounded_run
        try:
            return original_install_candidate(**kwargs)
        finally:
            materializer._run = original_run

    materializer._engine_environment = provisioned_engine_environment
    materializer._install_candidate = bounded_install_candidate
    materializer._require_port_free = _require_loopback_port_free
    def patched_materialize(*args: Any, **kwargs: Any) -> Any:
        result = original_materialize(*args, **kwargs)
        _patch_materialized_install_action(args[0] if args else kwargs["resource"])
        return result

    materializer.materialize = patched_materialize
    try:
        result = materializer.main(argv)
    finally:
        materializer.materialize = original_materialize
    _write_config(
        run_root=run_root,
        runner=runner,
        pinokiod=pinokiod,
        preload=preload,
        result=result,
    )
    return result


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
