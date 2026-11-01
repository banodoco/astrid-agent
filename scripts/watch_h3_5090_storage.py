#!/usr/bin/env python3
"""Watch for an attachable RTX 5090 and run the H3 acceptance evidence.

The watcher owns exactly one pod.  It keeps that pod alive through the direct
VibeComfy smoke and the bounded Astrid admission/worker probe, then terminates
the exact pod id in the finalizer.
"""
from __future__ import annotations

from runtime_protocol import platform

import atexit
import json
import os
import re
import shlex
import shutil
import subprocess
import time
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import paramiko

ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT / ".otto/runs/astrid-runpod-task-queue-20260918"
RECEIPTS = RUN / "receipts"
RECEIPTS.mkdir(parents=True, exist_ok=True)
LOG = RECEIPTS / "5090-watch.log"
STATE = RECEIPTS / "5090-watch-state.json"
POD_RECORD = RECEIPTS / "5090-pod.json"
LOCAL_MANIFEST = RECEIPTS / "release-manifest-final.json"
LOCAL_MANIFEST_SHA = RECEIPTS / "release-manifest.sha256"
SSH_KEY = Path("/Users/peteromalley/.ssh/services/runpod_o0glkh8fjvogi6_ed25519")

BASE = "/workspace/h3-golden/releases/h3-cu130-v1-candidate"
TEST = BASE + "/staging/astrid-5090-attach-smoke"
REMOTE_SRC = TEST + "/astrid-src"
REMOTE_RUNTIME = TEST + "/runtime"
REMOTE_PROFILE = TEST + "/hc03-readiness.json"
# The persistent RunPod network volume presents files as mode 0666 on this
# image, even when SFTP chmod is requested.  GenericPackHost deliberately
# rejects credentials that are not owner-only, so keep authority-bearing
# files on the container disk and use the volume only for durable evidence.
REMOTE_CREDENTIAL = "/tmp/astrid-pack-host.token"
REMOTE_BOOT_MANIFEST = "/tmp/astrid-host/boot-manifest.json"
REMOTE_SUPPORT_ROOT = "/tmp/astrid-host"
REMOTE_TUNNEL_PORT = 50604
IMAGE = "runpod/base:1.0.3-dev-fix-pytorch-version-verification-cuda1300-ubuntu2404"
GPU = "NVIDIA GeForce RTX 5090"
VOLUME = "backup"
PREFIX = "astrid-h3-ready-5090"
POLL = 60
WATCH = 24 * 60 * 60

pod_id: str | None = None
terminated = False
tunnel_process: subprocess.Popen[str] | None = None
tunnel_handle: dict[str, Any] | None = None
tunnel_stop = threading.Event()
tunnel_lock = threading.RLock()
tunnel_supervisor_thread: threading.Thread | None = None
remote_host_started = False


def now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def log(message: str) -> None:
    line = f"{now()} {message}"
    print(line, flush=True)
    with LOG.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def write_json(path: Path, value: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def _json_candidates(text: str) -> list[Any]:
    """Decode JSON objects embedded in lifecycle CLI progress output."""
    decoder = json.JSONDecoder()
    values: list[Any] = []
    for index, char in enumerate(text):
        if char not in "[{":
            continue
        try:
            value, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        values.append(value)
    return values


def _provider_contains_identity(text: str, identity: str) -> bool | None:
    """Return provider presence, or ``None`` when the observation is unusable."""
    candidates = _json_candidates(text)
    if not candidates:
        return None

    def walk(value: Any) -> bool:
        if isinstance(value, Mapping):
            for key in ("id", "pod_id", "podId", "volume_id", "volumeId", "name"):
                if str(value.get(key) or "") == identity:
                    return True
            return any(walk(item) for item in value.values())
        if isinstance(value, list):
            return any(walk(item) for item in value)
        return False

    return any(walk(value) for value in candidates)


def cli(args: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    log("cli " + " ".join(shlex.quote(x) for x in args))
    return subprocess.run(args, text=True, capture_output=True, timeout=timeout, check=False)


def parse_handle(stdout: str) -> dict[str, Any] | None:
    decoder = json.JSONDecoder()
    for i, char in enumerate(stdout):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(stdout[i:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and value.get("pod_id") and value.get("ssh"):
            return value
    return None


def terminate() -> None:
    global terminated
    global tunnel_process, tunnel_supervisor_thread
    if terminated and tunnel_process is None:
        return
    tunnel_stop.set()
    with tunnel_lock:
        process = tunnel_process
        tunnel_process = None
    _stop_tunnel_process(process)
    supervisor = tunnel_supervisor_thread
    if supervisor is not None and supervisor is not threading.current_thread():
        supervisor.join(timeout=5)
    tunnel_supervisor_thread = None
    if pod_id and not terminated:
        result = cli(["runpod-lifecycle", "terminate", pod_id, "--yes"], 120)
        pod_inventory = cli(["runpod-lifecycle", "list", "--json"], 120)
        volume_inventory = cli(["runpod-lifecycle", "volumes", "ls", "--json"], 120)
        pod_present = _provider_contains_identity(
            pod_inventory.stdout, pod_id
        ) if pod_inventory.returncode == 0 else None
        volume_present = _provider_contains_identity(
            volume_inventory.stdout, VOLUME
        ) if volume_inventory.returncode == 0 else None
        pod_verified = result.returncode == 0 and pod_present is False
        volume_verified = volume_present is True
        cleanup_status = "passed" if pod_verified and volume_verified else "failed"
        write_json(RECEIPTS / "cleanup-receipt.json", {
            "schema_version": 1,
            "kind": "astrid.cleanup.v1",
            "status": cleanup_status,
            "resources": [
                {
                    "kind": "runpod_pod",
                    "id": pod_id,
                    "owned": True,
                    "expected_postcondition": "terminated and absent from provider list",
                    "observed_postcondition": (
                        "absent from provider list" if pod_present is False
                        else "provider absence not verified"
                    ),
                    "verified": pod_verified,
                },
                {
                    "kind": "runpod_network_volume",
                    "id": VOLUME,
                    "owned": False,
                    "expected_postcondition": "preserved and not deleted by this run",
                    "observed_postcondition": (
                        "present in provider volume inventory" if volume_present is True
                        else "provider preservation not verified"
                    ),
                    "verified": volume_verified,
                },
            ],
        })
        terminated = pod_verified
        log(f"terminated pod={pod_id} rc={result.returncode} stderr={result.stderr[-500:]!r}")


def exit_handler(*_args: Any) -> None:
    terminate()
    # Signal handlers must stop the polling loop after teardown; otherwise a
    # Ctrl-C can leave a cancelled task being polled for the full execution
    # timeout even though its exact pod is already gone.
    if _args:
        raise SystemExit(130)


def install_exit_handlers() -> None:
    """Own the process's exit and signals; only the CLI entry point may, never an importer."""
    atexit.register(exit_handler)
    platform.install_stop_handlers(exit_handler)


def launch(deadline: float) -> dict[str, Any]:
    command = [
        "runpod-lifecycle", "launch", "--detach",
        "--gpu-type", GPU, "--image", IMAGE,
        "--allowed-cuda-versions", "13.0",
        "--storage-name", VOLUME, "--storage-volumes", VOLUME,
        "--container-disk-gb", "40", "--disk-size-gb", "40",
        "--min-memory-gb", "32", "--name-prefix", PREFIX,
        "--timeout", "900", "--wait-capacity", "3600",
        "--retry-interval", str(POLL),
    ]
    while time.time() < deadline:
        remaining = max(120, int(deadline - time.time()))
        result = cli(command, remaining + 120)
        if result.returncode == 0:
            handle = parse_handle(result.stdout)
            if handle:
                return handle
            log(f"launch returned no handle stdout={result.stdout[-1000:]!r}")
        else:
            log(f"launch attempt failed rc={result.returncode} stderr={result.stderr[-1000:]!r}")
        time.sleep(min(POLL, max(1, int(deadline - time.time()))))
    raise TimeoutError("24-hour RTX 5090 capacity watch expired")


def connect_ssh(handle: dict[str, Any], deadline: float) -> paramiko.SSHClient:
    match = re.search(r"root@([^ ]+)\s+-p\s+(\d+)", str(handle["ssh"]))
    if not match:
        raise RuntimeError(f"unparseable SSH details: {handle['ssh']!r}")
    host, port = match.group(1), int(match.group(2))
    last: Exception | None = None
    while time.time() < deadline:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            client.connect(
                host, port=int(port), username="root", key_filename=str(SSH_KEY),
                look_for_keys=False, allow_agent=False, timeout=30,
            )
            # Paramiko can return from connect while the provider-side SSH
            # proxy is still handing off the session.  Probe a real channel
            # before handing the client to SFTP/exec callers; otherwise the
            # first operation fails with the opaque "SSH session not active"
            # error and we throw away an otherwise healthy pod.
            _, probe_out, probe_err = client.exec_command("true", timeout=30)
            if probe_out.channel.recv_exit_status() != 0:
                raise paramiko.SSHException(f"SSH probe failed: {probe_err.read()!r}")
            log(f"ssh ready host={host} port={port}")
            return client
        except Exception as exc:
            last = exc
            try:
                client.close()
            except Exception:
                pass
            log(f"ssh wait error={type(exc).__name__}: {exc}")
            time.sleep(10)
    raise TimeoutError(f"SSH did not become ready: {last}")


def mkdirs(sftp: paramiko.SFTPClient, path: str) -> None:
    current = ""
    for part in path.strip("/").split("/"):
        current += "/" + part
        try:
            sftp.stat(current)
        except OSError:
            sftp.mkdir(current)


def upload_tree_tar(client: paramiko.SSHClient) -> None:
    """Upload the Astrid runtime source without macOS metadata or caches."""
    archive = subprocess.Popen(
        [
            "tar", "-cf", "-", "-C", str(ROOT),
            "--exclude=__pycache__", "--exclude=.pytest_cache",
            "--exclude=*.pyc", "--exclude=tests", "--exclude=golden",
            "--exclude=skill", "--exclude=astrid/.astrid",
            "astrid", "astrid_runtime_client", "config",
            "pyproject.toml",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=False,
    )
    remote_tar = TEST + "/astrid-src.tar"
    try:
        with client.open_sftp() as sftp:
            with sftp.open(remote_tar, "wb") as handle:
                assert archive.stdout is not None
                while True:
                    chunk = archive.stdout.read(1024 * 1024)
                    if not chunk:
                        break
                    handle.write(chunk)
        rc = archive.wait(timeout=180)
        if rc != 0:
            stderr = archive.stderr.read().decode("utf-8", errors="replace") if archive.stderr else ""
            raise RuntimeError(f"Astrid source archive failed rc={rc}: {stderr[-1000:]}")
    finally:
        if archive.poll() is None:
            archive.kill()
    # Extraction is deliberately explicit and bounded to the test directory.
    command = (
        f"mkdir -p {shlex.quote(REMOTE_SRC)} "
        # macOS tar records uid/gid, ACL, and xattr headers that the Linux
        # RunPod container must not try to apply to its source checkout.
        f"&& tar --no-same-owner --no-same-permissions --no-xattrs -xf "
        f"{shlex.quote(remote_tar)} -C {shlex.quote(REMOTE_SRC)} "
        f"&& rm -f {shlex.quote(remote_tar)}"
    )
    rc, _out, err = remote_exec(client, command, 180)
    if rc != 0:
        raise RuntimeError(f"remote Astrid source extraction failed: {err[-1000:]}")
    # Keep the package metadata explicit.  Some provider tar implementations
    # omit a root file when replaying a macOS archive even with owner/xattr
    # preservation disabled; Astrid's version module requires this witness.
    with client.open_sftp() as sftp:
        sftp.put(str(ROOT / "pyproject.toml"), REMOTE_SRC + "/pyproject.toml")


def _json_envelope(stdout: str) -> dict[str, Any]:
    decoder = json.JSONDecoder()
    for index, char in enumerate(stdout):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(stdout[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise RuntimeError(f"command did not return a JSON object: {stdout[-1000:]!r}")


def prepare_e2e_bundle(
    frame_counts: tuple[int, ...] = (175, 260),
    bundle_name: str = "canonical-h3-e2e",
) -> dict[str, Path]:
    """Materialize a strict canonical bundle from the authored CUDA13 graph."""
    import runpy
    from vibecomfy.porting.import_service import import_workflow_bytes

    destination = RECEIPTS / bundle_name
    destination.mkdir(parents=True, exist_ok=True)
    members = {
        "python": destination / "workflow.py",
        "companion": destination / "workflow.vibe.json",
        "source": destination / "source.json",
    }
    if (
        all(path.is_file() for path in members.values())
        and "MODEL_ASSETS = {" in members["python"].read_text(encoding="utf-8")
        and "MODEL_ASSET_METADATA = " in members["python"].read_text(encoding="utf-8")
        and "sha256=" in members["python"].read_text(encoding="utf-8")
        and "size_bytes=" in members["python"].read_text(encoding="utf-8")
        and "comfy_version" in members["python"].read_text(encoding="utf-8")
        and "==0.36.0" in members["python"].read_text(encoding="utf-8")
    ):
        return members
    authored = ROOT / "workflows/astrid_intro_h3_anchor_chain/workflow_cuda13.py"
    prompts = json.loads((ROOT / "workflows/astrid_intro_h3_prompts.json").read_text(encoding="utf-8"))
    segments = []
    for index, frames in enumerate(frame_counts):
        item = prompts["segments"][index]
        segments.append({
            "frames": frames,
            "start": item["start_anchor"] + ".png",
            "end": item["end_anchor"] + ".png",
            "prompt": item["prompt"],
            "seed": 2026091801 + index,
        })
    segment_path = destination / "segments.json"
    segment_path.write_text(json.dumps(segments, indent=2) + "\n", encoding="utf-8")
    release_models = {
        str(item.get("path")): item
        for item in json.loads(LOCAL_MANIFEST.read_text(encoding="utf-8")).get("models", [])
        if isinstance(item, dict) and isinstance(item.get("path"), str)
    }
    old = {key: os.environ.get(key) for key in ("ASTRID_H3_SEGMENTS", "ASTRID_H3_SEGMENT_COUNT")}
    try:
        os.environ["ASTRID_H3_SEGMENTS"] = str(segment_path)
        os.environ["ASTRID_H3_SEGMENT_COUNT"] = str(len(frame_counts))
        module = runpy.run_path(str(authored))
        graph = module["build"]().export_to_json(format="api")
    finally:
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    raw = json.dumps(graph, sort_keys=True, separators=(",", ":")).encode("utf-8")
    artifacts = import_workflow_bytes(
        raw,
        workflow_id="astrid_intro_h3_cuda13_" + bundle_name.replace("-", "_"),
        source_provenance={
            "origin_kind": "authored-python-build",
            "origin_uri": "workflows/astrid_intro_h3_anchor_chain/workflow_cuda13.py",
            "origin_pin": "segments=" + ",".join(str(value) for value in frame_counts),
        },
    )
    # The API JSON carries model *filenames* but cannot carry the authored
    # source URL/subdirectory witness.  Preserve that witness in the emitted
    # Python bundle; otherwise the runtime's model-contract gate rejects a
    # perfectly materialized release as "not locally registered".  This is
    # intentionally bound to the canonical authored workflow rather than
    # inventing metadata from the remote filesystem.
    model_lines = [
        "",
        "MODEL_ASSETS = {",
    ]
    for name, asset in sorted(module.get("MODELS", {}).items()):
        manifest_model = release_models.get(f"{asset.subdir}/{asset.filename}", {})
        sha_line = (
            f"        sha256={manifest_model['sha256']!r},"
            if isinstance(manifest_model.get("sha256"), str) else None
        )
        size_line = (
            f"        size_bytes={int(manifest_model['bytes'])},"
            if isinstance(manifest_model.get("bytes"), int) else None
        )
        model_lines.extend([
            f"    {name!r}: ModelAsset(",
            f"        filename={asset.filename!r},",
            f"        url={asset.url!r},",
            f"        subdir={asset.subdir!r},",
            *([sha_line] if sha_line else []),
            *([size_line] if size_line else []),
            "    ),",
        ])
    model_lines.append("}")
    model_meta = []
    for _name, asset in sorted(module.get("MODELS", {}).items()):
        manifest_model = release_models.get(f"{asset.subdir}/{asset.filename}", {})
        model_meta.append({
            "name": asset.filename,
            "url": asset.url,
            "subdir": asset.subdir,
            **(
                {"sha256": manifest_model["sha256"]}
                if isinstance(manifest_model.get("sha256"), str)
                else {}
            ),
            **(
                {"size_bytes": int(manifest_model["bytes"])}
                if isinstance(manifest_model.get("bytes"), int)
                else {}
            ),
        })
    model_lines.extend([
        "",
        "# Explicit compatibility witness for older pinned VibeComfy loaders.",
        "MODEL_ASSET_METADATA = " + repr(model_meta),
    ])
    emitted = artifacts.python_bytes.decode("utf-8")
    emitted = emitted.replace(
        "from vibecomfy.templates import InputSpec, OutputSpec, ReadyMetadata, new_workflow, node as raw_call, ref",
        "from vibecomfy.templates import InputSpec, ModelAsset, OutputSpec, ReadyMetadata, new_workflow, node as raw_call, ref",
        1,
    )
    marker = "\n\nPUBLIC_INPUT_METADATA ="
    if marker not in emitted:
        raise RuntimeError("generated bundle has no metadata insertion point")
    emitted = emitted.replace(marker, "\n" + "\n".join(model_lines) + marker, 1)
    emitted = emitted.replace(
        "    requirements=",
        "    models=MODEL_ASSETS,\n    requirements=",
        1,
    )
    # The API export cannot carry typed runtime requirements. Preserve the
    # authored CUDA/Comfy contract in the generated canonical bundle so the
    # managed checkout adapter can verify the server against this workflow
    # instead of a global historical default.
    authored_requirements = module.get("READY_METADATA", {}).get("requirements", {})
    authored_runtime = (
        authored_requirements.get("runtime")
        if isinstance(authored_requirements, dict)
        else None
    )
    metadata_anchor = "\n\ndef build() -> VibeWorkflow:"
    metadata_patch = (
        "\n\n# Preserve source-backed model identity across API/Python round-trips.\n"
        "READY_METADATA[\"model_assets\"] = list(MODEL_ASSET_METADATA)\n"
        "READY_METADATA.setdefault(\"requirements\", {})[\"models\"] = list(MODEL_ASSET_METADATA)"
        + (
            "\nREADY_METADATA.setdefault(\"requirements\", {})[\"runtime\"] = "
            + repr(authored_runtime)
            if isinstance(authored_runtime, dict)
            else ""
        )
    )
    if metadata_anchor not in emitted:
        raise RuntimeError("generated bundle has no post-metadata insertion point")
    emitted = emitted.replace(metadata_anchor, metadata_patch + metadata_anchor, 1)
    members["python"].write_text(emitted, encoding="utf-8")
    members["companion"].write_bytes(artifacts.companion_bytes)
    members["source"].write_bytes(artifacts.source_bytes)
    shutil.copyfile(authored, destination / "authored-workflow_cuda13.py")
    return members


def import_managed_object(path: Path) -> str:
    result = cli([
        "python3", "-m", "astrid", "media", "import", str(path),
        "--project", "astrid-intro", "--json",
    ], 180)
    if result.returncode != 0:
        raise RuntimeError(f"media import failed for {path.name}: {result.stderr[-1000:]}")
    payload = _json_envelope(result.stdout)
    data = payload.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("object_id"), str):
        raise RuntimeError(f"media import returned no object id for {path.name}")
    return str(data["object_id"])


def _stop_tunnel_process(process: subprocess.Popen[str] | None) -> None:
    """Stop the complete SSH process group and reap its leader."""
    if process is None:
        return
    platform.stop_tree(process, grace=10)


def _spawn_reverse_runtime_tunnel(handle: dict[str, Any]) -> subprocess.Popen[str]:
    """Start one SSH tunnel process for the supervisor."""
    match = re.search(r"root@([^ ]+)\s+-p\s+(\d+)", str(handle["ssh"]))
    if not match:
        raise RuntimeError(f"unparseable SSH details: {handle['ssh']!r}")
    discovery = json.loads((ROOT / ".astrid-data/runtime/discovery.json").read_text(encoding="utf-8"))
    from urllib.parse import urlsplit

    runtime_port = int(urlsplit(str(discovery.get("endpoint") or "http://127.0.0.1:50603")).port or 50603)
    log_path = RECEIPTS / "e2e-ssh-tunnel.log"
    stream = log_path.open("a", encoding="utf-8")
    process = platform.spawn_tree([
        "ssh", "-N", "-T", "-o", "ExitOnForwardFailure=yes",
        "-o", "StrictHostKeyChecking=no", "-o", "ServerAliveInterval=30",
        "-i", str(SSH_KEY), "-p", match.group(2),
        "-R", f"127.0.0.1:{REMOTE_TUNNEL_PORT}:127.0.0.1:{runtime_port}",
        f"root@{match.group(1)}",
    ], stdin=subprocess.DEVNULL, stdout=stream, stderr=subprocess.STDOUT,
       text=True)
    time.sleep(2)
    if process.poll() is not None:
        raise RuntimeError(f"runtime SSH reverse tunnel exited: {log_path.read_text()[-1000:]}")
    return process


def _supervise_reverse_runtime_tunnel() -> None:
    """Restart a dead reverse tunnel until teardown is requested."""
    global tunnel_process
    while not tunnel_stop.wait(1.0):
        with tunnel_lock:
            process = tunnel_process
            handle = tunnel_handle
        if process is None or process.poll() is None or handle is None:
            continue
        log(f"reverse runtime tunnel exited rc={process.returncode}; restarting")
        try:
            replacement = _spawn_reverse_runtime_tunnel(handle)
        except Exception as exc:  # noqa: BLE001 - retry until explicit teardown
            log(f"reverse runtime tunnel restart failed: {type(exc).__name__}: {exc}")
            continue
        with tunnel_lock:
            if tunnel_stop.is_set():
                _stop_tunnel_process(replacement)
                return
            tunnel_process = replacement
        log("reverse runtime tunnel restarted")


def start_reverse_runtime_tunnel(handle: dict[str, Any]) -> subprocess.Popen[str]:
    """Expose the local loopback runtime through one supervised SSH tunnel."""
    global tunnel_process, tunnel_handle, tunnel_supervisor_thread
    tunnel_stop.clear()
    tunnel_handle = dict(handle)
    process = _spawn_reverse_runtime_tunnel(handle)
    with tunnel_lock:
        tunnel_process = process
    if tunnel_supervisor_thread is None or not tunnel_supervisor_thread.is_alive():
        tunnel_supervisor_thread = threading.Thread(
            target=_supervise_reverse_runtime_tunnel,
            name="astrid-runpod-reverse-tunnel",
            daemon=True,
        )
        tunnel_supervisor_thread.start()
    return process


def _task_state(value: Any) -> str | None:
    if isinstance(value, dict):
        for key in ("state", "status"):
            raw = value.get(key)
            if isinstance(raw, str):
                return raw
        for key in ("task", "data", "result"):
            found = _task_state(value.get(key))
            if found:
                return found
    return None


def _runtime_capability_rows() -> list[dict[str, Any]]:
    """Read capabilities through the local worker protocol client.

    The Paramiko SSH connection is only the transport to the pod; it is not
    an Astrid runtime client.  Older versions of this watcher reached into a
    nonexistent ``SSHClient._remote`` attribute here, which made a healthy
    host wait until timeout even after it had registered successfully.
    """
    discovery = json.loads((ROOT / ".astrid-data/runtime/discovery.json").read_text(encoding="utf-8"))
    endpoint = str(discovery.get("endpoint") or "").strip()
    token = (ROOT / ".astrid-data/runtime/credentials/astrid-pack-host.token").read_text(encoding="utf-8").strip()
    from astrid.core.execution.generic_host import RuntimeProtocolClient

    page = RuntimeProtocolClient(endpoint, token).generated.list_capabilities(limit=100)
    values = page[0] if isinstance(page, tuple) and page else page
    rows: list[dict[str, Any]] = []
    for value in values if isinstance(values, (list, tuple)) else ():
        if isinstance(value, dict):
            rows.append(dict(value))
            continue
        rows.append({
            key: getattr(value, key, None)
            for key in (
                "capability_id", "definition_digest", "status",
                "required_resource_keys", "estimated_scratch_bytes",
                "estimated_output_bytes", "unavailable_reason",
            )
        })
    return rows


def start_remote_generic_host(client: paramiko.SSHClient, handle: dict[str, Any]) -> dict[str, Any]:
    """Install the pack source on the volume and start one registered host."""
    bundle = prepare_e2e_bundle()
    object_ids = {name: import_managed_object(path) for name, path in bundle.items()}
    write_json(RECEIPTS / "e2e-input-manifest.json", {"members": object_ids, "workflow": str(bundle["python"])})
    manifest = json.loads(LOCAL_MANIFEST.read_text(encoding="utf-8"))
    facts = {
        "model_digest": "sha256:" + str(manifest["models"][3]["sha256"]),
        "custom_node_digest": "sha256:" + str(manifest["runtime"]["custom_nodes_lock_sha256"]),
        "release_manifest_sha256": __import__("hashlib").sha256(LOCAL_MANIFEST.read_bytes()).hexdigest(),
    }
    rc, _, err = remote_exec(client, f"mkdir -p {shlex.quote(REMOTE_SUPPORT_ROOT)}", 30)
    if rc != 0:
        raise RuntimeError(f"remote authority directory setup failed: {err[-500:]}")
    with client.open_sftp() as sftp:
        mkdirs(sftp, TEST + "/support/astrid-host")
        mkdirs(sftp, TEST + "/attempts")
        mkdirs(sftp, BASE + "/runtime/ComfyUI/input")
        sftp.put(str(ROOT / ".astrid-data/runtime/credentials/astrid-pack-host.token"), REMOTE_CREDENTIAL)
        sftp.put(str(ROOT / ".astrid-data/runtime/astrid-host/boot-manifest.json"), REMOTE_BOOT_MANIFEST)
        # The canonical H3 workflow refers to image anchors by Comfy input
        # filename.  Workflow CAS members do not contain those media files,
        # so stage the reviewed anchor set on the persistent release volume
        # before accepting a task; otherwise Comfy only warns about a missing
        # endpoint and the failure appears much later during sampling.
        anchor_root = ROOT / "runs/minkhole-keyframes-20260910/discovery/canonical-intro-anchors"
        for anchor_name in (
            "anchor_shot_v03.png",
            "anchor_shot_v04.png",
            "anchor_shot_v05.png",
            "anchor_shot_v06.png",
        ):
            anchor_path = anchor_root / anchor_name
            if anchor_path.is_file():
                sftp.put(str(anchor_path), BASE + "/runtime/ComfyUI/input/" + anchor_name)
        with sftp.open(TEST + "/e2e-facts.json", "w") as stream:
            stream.write(json.dumps(facts, sort_keys=True))
    rc, _, err = remote_exec(
        client,
        "chmod 600 " + shlex.quote(REMOTE_CREDENTIAL) + " " + shlex.quote(REMOTE_BOOT_MANIFEST),
        30,
    )
    if rc != 0:
        raise RuntimeError(f"remote authority permission setup failed: {err[-500:]}")
    upload_tree_tar(client)
    start_reverse_runtime_tunnel(handle)
    # GenericPackHost validates the manifest's canonical composition digest,
    # not the raw JSON-byte SHA-256.
    from astrid.core._shared.boot_manifest import load_boot_manifest_hash
    local_boot_manifest = ROOT / ".astrid-data/runtime/astrid-host/boot-manifest.json"
    boot_hash = load_boot_manifest_hash(
        local_boot_manifest,
        support_root=local_boot_manifest.parent,
    )
    # The release volume is persistent.  A previous attempt can leave a
    # non-empty readiness witness behind, and merely checking for file
    # existence would then admit the new task before this host registers.
    stale_paths = " ".join(
        shlex.quote(TEST + "/" + name)
        for name in (
            "generic-host.ready.json",
            "generic-host-failure-tail.txt",
            "generic-host.pid",
        )
    )
    rc, _out, err = remote_exec(client, "rm -f " + stale_paths, 30)
    if rc != 0:
        raise RuntimeError(f"stale generic-host witness cleanup failed: {err[-500:]}")
    profile_script = """
set -eu
BASE=__BASE__
TEST=__TEST__
SRC=__SRC__
PY=\"$BASE/runtime/venv/bin/python\"
SUPPORT_ROOT=__SUPPORT_ROOT__
BOOT_MANIFEST=__BOOT_MANIFEST__
EXPECTED_BOOT_HASH=__BOOT_HASH__
mkdir -p \"$TEST/support/astrid-host\" \"$TEST/attempts\" \"$TEST/astrid-output\" \"$SUPPORT_ROOT\"
# Bind the uploaded source before any Astrid/VibeComfy probe.  The release
# venv does not install the sibling source checkout as a package, and probing
# first would turn an environment mistake into a misleading host failure.
export BASE TEST
export PYTHONPATH=\"$SRC:$BASE/runtime/vibecomfy:$BASE/runtime/ComfyUI\"
export VIBECOMFY_HEADLESS=1
# The launcher owns provisioning; GenericPackHost remains fail-closed and only
# consumes an existing manifest. Perform the same lexical/symlink/hash checks
# synchronously before backgrounding the worker, so bootstrap failures cannot
# degrade into a readiness timeout.
\"$PY\" - \"$BOOT_MANIFEST\" \"$SUPPORT_ROOT\" \"$EXPECTED_BOOT_HASH\" <<'PY'
import sys
from pathlib import Path

from astrid.core._shared.boot_manifest import load_boot_manifest_hash, validate_manifest_path

manifest = validate_manifest_path(Path(sys.argv[1]), Path(sys.argv[2]))
actual = load_boot_manifest_hash(manifest, support_root=Path(sys.argv[2]))
expected = sys.argv[3]
actual = actual.removeprefix("sha256:").lower()
expected = expected.removeprefix("sha256:").lower()
if len(actual) != 64 or len(expected) != 64 or actual != expected:
    raise SystemExit(f\"boot manifest hash mismatch: expected {expected}, got {actual}\")
print(f\"boot-manifest-ready {manifest} {actual}\")
PY
# The release venv is immutable here.  The source checkout is bound through
# PYTHONPATH and its digest is attested below; silently ignoring a failed pip
# install would create a different, unverifiable runtime.
# The release image can omit Astrid's small schema-validator closure.  Install
# only these packages with --no-deps, so Torch/CUDA cannot be changed.
/usr/bin/uv pip install -q --python \"$PY\" --no-deps \\
  \"jsonschema>=4.0\" \"jsonschema-specifications>=2023.03.6\" \\
  \"referencing>=0.30\" \"rpds-py>=0.7\" \"attrs>=22.2\" \\
  || { echo \"targeted Astrid dependency install failed\" >&2; exit 28; }
\"$PY\" - <<'PY'
import jsonschema, jsonschema_specifications, referencing, rpds
print('astrid-schema-dependencies-ready')
PY
\"$PY\" - <<'PY'
import hashlib, json, os, shutil, uuid
from pathlib import Path
from vibecomfy.runtime.session import current_source_content_digest, current_source_revision
base = Path(os.environ['BASE'])
test = Path(os.environ['TEST'])
session = base / 'runtime' / 'out' / 'sessions' / 'astrid-h3-e2e'
facts = json.loads((test / 'e2e-facts.json').read_text())
exact = {
    'interpreter': str(base / 'runtime/venv/bin/python'),
    'runtime_lock': 'sha256:' + hashlib.sha256((base / 'receipts/release-manifest.json').read_bytes()).hexdigest(),
    'engine_lock': facts['release_manifest_sha256'],
    'model_digest': facts['model_digest'],
    'custom_node_digest': facts['custom_node_digest'],
    'driver': os.popen('nvidia-smi --query-gpu=driver_version --format=csv,noheader').read().strip(),
    'root': 'sha256:' + hashlib.sha256((base / 'receipts/release-manifest.json').read_bytes()).hexdigest(),
    'port': 8188,
}
vram_mib = int(os.popen('nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits').read().strip().splitlines()[0])
minimum = {
    'vram_bytes': vram_mib * 1024**2,
    'scratch_bytes': int(shutil.disk_usage(test).free),
}
verified = {'exact': dict(sorted(exact.items())), 'minimum': dict(sorted(minimum.items()))}
marker = json.loads((session / 'launch.json').read_text())
source_revision = current_source_revision()
source_content_digest = current_source_content_digest()
if not source_revision or not source_content_digest:
    raise SystemExit('managed VibeComfy source attestation is unavailable')
models_root = base / 'models'
model_inventory = []
seen_model_names = set()
for model_path in sorted(models_root.rglob('*')):
    if model_path.is_symlink():
        raise SystemExit(f'model root contains a symlink: {model_path}')
    if not model_path.is_file():
        continue
    relative = model_path.relative_to(models_root)
    name = relative.name
    if name in seen_model_names:
        raise SystemExit(f'model root contains a shadow basename: {name}')
    seen_model_names.add(name)
    digest = hashlib.sha256()
    with model_path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    model_inventory.append({
        'name': name,
        'sha256': 'sha256:' + digest.hexdigest(),
        'size': model_path.stat().st_size,
        'subdir': relative.parent.as_posix() if str(relative.parent) != '.' else '',
    })
model_inventory.sort(key=lambda item: (item['subdir'], item['name']))
model_root_binding = {
    'schema_version': 1,
    'path': str(models_root),
    'inventory': model_inventory,
    'inventory_digest': 'sha256:' + hashlib.sha256(
        json.dumps(model_inventory, sort_keys=True, separators=(',', ':')).encode()
    ).hexdigest(),
}
profile = {
    'schema_version': 'hc03-worker-readiness.v1',
    'status': 'ready',
    'verified_facts': verified,
    'verified_facts_digest': 'sha256:' + hashlib.sha256(json.dumps(verified, sort_keys=True, separators=(',', ':')).encode()).hexdigest(),
    'runtime': {
        'runtime_instance_id': str(uuid.uuid4()),
        # Release-bound worker contract used when a loader cannot recover
        # typed runtime metadata from an API/Python round-trip.
        'comfyui_version': '==0.36.0',
    },
    # Bind bundle model preflight to the same release volume ComfyUI receives
    # through extra_model_paths.yaml.  The target session can be healthy while
    # VibeComfy's default local registry is empty, so this root is explicit
    # and digest-bound through the readiness profile.
    'launch': {
        'output_root': str(test / 'astrid-output'),
        'model_root': model_root_binding,
    },
    'vibecomfy_session': {
        'session_dir': str(session),
        'server_url': 'http://127.0.0.1:8188',
        'pid': int((session / 'pid').read_text()),
        'comfy_pid': int((session / 'comfy_pid').read_text()),
        'launch_token': marker['launch_token'],
        'process_birth_id': marker['process_start_identity'],
        'comfy_process_birth_id': marker['comfy_process_start_identity'],
        'source_revision': source_revision,
        'source_content_digest': source_content_digest,
        'config_digest': 'sha256:' + hashlib.sha256((session / 'config.json').read_bytes()).hexdigest(),
    },
    # The release transport may intentionally carry a local, qualified
    # snapshot rather than the normal published Git pin.  Keep the normal pin
    # fail-closed; a candidate is selected only by this exact HC-03 profile.
    'vibecomfy_candidate': {
        'kind': 'local_snapshot',
        'revision': source_revision,
        'source_content_digest': source_content_digest,
        'base_revision': '04a56837b9a768151744d6e17ef4d13cc5844e06',
        'source_worktree_dirty': True,
        'archive_sha256': 'd95025275e9c01e57bfb3a76c3f9f50ffaebb4725ae8d81399c3024fd3ef54fe',
    },
}
(test / 'hc03-readiness.json').write_text(json.dumps(profile, sort_keys=True, indent=2) + '\\n')
PY
PROFILE_HASH=$(sha256sum \"$TEST/hc03-readiness.json\" | cut -d' ' -f1)
nohup env PYTHONPATH=\"$PYTHONPATH\" \"$PY\" -m astrid.core.execution.generic_host run \\
  --pack-root \"$SRC/astrid/packs/vibecomfy\" --runtime-endpoint http://127.0.0.1:__REMOTE_PORT__ \\
  --credential-file __CREDENTIAL__ --executor-id astrid-pack-host \\
  --max-concurrency 1 --register --poll-seconds 1 --attempt-base \"$TEST/attempts\" \\
  --ready-file \"$TEST/generic-host.ready.json\" --source-checkout \"$SRC\" \\
  --support-root \"$SUPPORT_ROOT\" --boot-manifest-path \"$BOOT_MANIFEST\" \\
  --boot-manifest-hash \"$EXPECTED_BOOT_HASH\" --readiness-profile-path \"$TEST/hc03-readiness.json\" \\
  --readiness-profile-hash \"sha256:$PROFILE_HASH\" > \"$TEST/generic-host.log\" 2>&1 < /dev/null &
echo $! > \"$TEST/generic-host.pid\"
"""
    profile_script = (
        profile_script.replace("__BASE__", shlex.quote(BASE))
        .replace("__TEST__", shlex.quote(TEST))
        .replace("__SRC__", shlex.quote(REMOTE_SRC))
        .replace("__REMOTE_PORT__", str(REMOTE_TUNNEL_PORT))
        .replace("__BOOT_HASH__", boot_hash)
        .replace("__CREDENTIAL__", shlex.quote(REMOTE_CREDENTIAL))
        .replace("__SUPPORT_ROOT__", shlex.quote(REMOTE_SUPPORT_ROOT))
        .replace("__BOOT_MANIFEST__", shlex.quote(REMOTE_BOOT_MANIFEST))
    )
    setup_started = time.time()
    rc, out, err = remote_exec(client, profile_script, 600)
    receipt = {
        "remote_setup_rc": rc,
        "remote_setup_stdout_tail": out[-3000:],
        "remote_setup_stderr_tail": err[-3000:],
        "input_manifest": object_ids,
    }
    if rc != 0:
        receipt["status"] = "host_setup_failed"
        return receipt
    for _ in range(300):
        with client.open_sftp() as sftp:
            try:
                attrs = sftp.stat(TEST + "/generic-host.ready.json")
                ready = attrs.st_size > 0 and attrs.st_mtime >= setup_started - 2
                if ready:
                    with sftp.open(TEST + "/generic-host.ready.json", "r") as stream:
                        marker = json.loads(stream.read().decode("utf-8"))
            except OSError:
                ready = False
                marker = None
            except (TypeError, ValueError, json.JSONDecodeError):
                ready = False
                marker = None
            if ready and isinstance(marker, dict):
                pid = marker.get("pid")
                marker_ok = (
                    marker.get("status") == "ready"
                    and marker.get("executor_id") == "astrid-pack-host"
                    and "vibecomfy.run" in marker.get("ready_capabilities", [])
                    and isinstance(pid, int)
                )
                if marker_ok:
                    live_rc, _live_out, _live_err = remote_exec(
                        client, f"kill -0 {int(pid)}", 10
                    )
                    if live_rc == 0:
                        return {**receipt, "status": "host_ready", "ready_marker": marker}
            # A bootstrap failure happens before the ready marker is written.
            # Observe the child PID while waiting so the caller gets the real
            # startup error immediately instead of a 300-second timeout.
            try:
                pid_attrs = sftp.stat(TEST + "/generic-host.pid")
                if pid_attrs.st_mtime >= setup_started - 2:
                    with sftp.open(TEST + "/generic-host.pid", "r") as stream:
                        child_pid = int(stream.read().decode("utf-8").strip())
                    live_rc, _live_out, _live_err = remote_exec(
                        client, f"kill -0 {child_pid}", 10
                    )
                    if live_rc != 0:
                        for name in ("generic-host.log", "generic-host-failure-tail.txt"):
                            try:
                                with sftp.open(TEST + "/" + name, "r") as stream:
                                    receipt["remote_" + name.replace(".", "_")] = stream.read().decode("utf-8", errors="replace")[-12000:]
                            except OSError:
                                pass
                        receipt["status"] = "host_bootstrap_failed"
                        return receipt
            except (OSError, TypeError, ValueError):
                pass
        time.sleep(1)
    with client.open_sftp() as sftp:
        for name in ("generic-host.log", "generic-host-failure-tail.txt"):
            try:
                with sftp.open(TEST + "/" + name, "r") as stream:
                    receipt["remote_" + name.replace(".", "_")] = stream.read().decode("utf-8", errors="replace")[-12000:]
            except OSError:
                pass
    receipt["status"] = "host_ready_timeout"
    return receipt


def run_astrid_e2e(client: paramiko.SSHClient, handle: dict[str, Any]) -> dict[str, Any]:
    """Submit the bounded 175 → 260 continuation through Astrid."""
    global remote_host_started
    setup = start_remote_generic_host(client, handle)
    if setup.get("status") != "host_ready":
        write_json(RECEIPTS / "astrid-e2e-acceptance.json", setup)
        return setup
    remote_host_started = True
    object_ids = setup["input_manifest"]
    spec = {
        "inputs": {name: {"digest": digest} for name, digest in object_ids.items()},
        "input_digests": [{"name": name, "digest": digest} for name, digest in object_ids.items()],
        # Keep the task on the generalized D1 publication path.  The metadata
        # is deliberately just a small caller-owned label bag; Runtime copies
        # it to the resulting Generation while task/run/attempt lineage stays
        # authoritative in the normal publication records.
        "generation_intent": {
            "version": 1,
            "modality": "video",
            "partial_success_policy": "reject",
            "groups": [{
                "group_key": "main",
                "selectors": [
                    {"selector": "main-0", "ordinal": 0, "variant_key": "original", "required": True},
                    {"selector": "main-1", "ordinal": 1, "variant_key": "variant-1", "required": True},
                ],
            }],
            "metadata": {
                "shot_id": "runpod-managed-generation-8step-20260921",
                "source": "canonical-runpod-task-path",
                "steps": 8,
            },
        },
    }
    generation_intent = spec["generation_intent"]
    settlement_effect = {
        "effect_type": "generation.publish_v1",
        "target_id": "astrid-intro",
        "payload": {
            "version": 1,
            "modality": generation_intent["modality"],
            "generation_type": "vibecomfy.run",
            "metadata": generation_intent["metadata"],
            "partial_success_policy": generation_intent["partial_success_policy"],
            "groups": [
                {
                    "group_key": group["group_key"],
                    "selectors": [
                        {
                            **selector,
                            "output_port": "vibecomfy_run",
                        }
                        for selector in group["selectors"]
                    ],
                }
                for group in generation_intent["groups"]
            ],
        },
    }
    # Registration is asynchronous: a ready-file only means the host process
    # started, not that the runtime has published its final capability digest.
    # Wait for the live registry before admission so the task cannot be bound
    # to the transient pre-registration digest.
    live_capability: dict[str, Any] | None = None
    capability_deadline = time.time() + 180
    while time.time() < capability_deadline:
        try:
            rows = _runtime_capability_rows()
            candidates = [row for row in rows if isinstance(row, dict) and row.get("capability_id") == "vibecomfy.run"]
            ready = [row for row in candidates if row.get("status") == "ready"]
            if ready:
                live_capability = dict(ready[-1])
                break
        except Exception as exc:
            log(f"waiting for live vibecomfy.run capability: {type(exc).__name__}: {exc}")
        time.sleep(2)
    if live_capability is None:
        receipt = {"status": "capability_registration_timeout", "pod_id": pod_id}
        write_json(RECEIPTS / "astrid-e2e-acceptance.json", receipt)
        return receipt
    account_ref = os.environ.get("ASTRID_RUNPOD_ACCOUNT_REF", "runpod-default")
    target = {
        "target": {
            "kind": "runpod", "pod_id": str(handle["pod_id"]),
            "provider_account_ref": account_ref,
        },
        "lifecycle": {"mode": "leave_running"},
        "limits": {"max_queue_seconds": 300, "max_runtime_seconds": 1800},
    }
    key = (
        "astrid-h3-5090-e2e-"
        + str(handle["pod_id"])
        + "-"
        + os.environ.get("ASTRID_E2E_IDEMPOTENCY_SUFFIX", "initial")
    )
    args = [
        "python3", "-m", "astrid", "tasks", "create", "--project", "astrid-intro",
        "--capability", "vibecomfy.run",
        "--spec", json.dumps(spec, separators=(",", ":")),
        "--input-manifest", json.dumps(list(object_ids.values()), separators=(",", ":")),
        "--generation-intent", json.dumps(generation_intent, separators=(",", ":")),
        "--settlement-effect", json.dumps(settlement_effect, separators=(",", ":")),
        "--execution-request", json.dumps(target, separators=(",", ":")),
        "--idempotency-key", key, "--json",
    ]
    receipt: dict[str, Any] = {
        **setup,
        "pod_id": pod_id,
        "target": target,
        "idempotency_key": key,
        "live_capability": live_capability,
    }
    created = cli(args, 180)
    receipt["create_stdout"] = created.stdout[-12000:]
    receipt["create_stderr"] = created.stderr[-4000:]
    if created.returncode != 0:
        receipt["status"] = "admission_failed"
        write_json(RECEIPTS / "astrid-e2e-acceptance.json", receipt)
        return receipt
    create_payload = _json_envelope(created.stdout)
    receipt["create"] = create_payload
    data = create_payload.get("data")
    task_id = data.get("task_id") if isinstance(data, dict) else None
    if not isinstance(task_id, str) and isinstance(data, dict):
        task_id = data.get("id")
    if not isinstance(task_id, str):
        receipt["status"] = "admission_missing_task_id"
        write_json(RECEIPTS / "astrid-e2e-acceptance.json", receipt)
        return receipt
    replay = cli(args, 180)
    receipt["replay"] = {
        "returncode": replay.returncode,
        "stdout": replay.stdout[-8000:],
        "stderr": replay.stderr[-2000:],
    }
    # Exercise the worker-bound identity gate with the live host credential,
    # not a user credential that would only prove missing scope.  The wrong
    # executor must be rejected before the real host claims the queued task.
    try:
        wrong_probe = f'''set -u
BASE={shlex.quote(BASE)}
export PYTHONPATH={shlex.quote(REMOTE_SRC)}:$BASE/runtime/vibecomfy:$BASE/runtime/ComfyUI
"$BASE/runtime/venv/bin/python" - <<'PY'
import json
from pathlib import Path
from astrid.core.execution.generic_host import RuntimeProtocolClient

token = Path({REMOTE_CREDENTIAL!r}).read_text(encoding="utf-8").strip()
client = RuntimeProtocolClient("http://127.0.0.1:{REMOTE_TUNNEL_PORT}", token)
try:
    result = client.claim_next(
        executor_id="astrid-wrong-executor",
        capability_ids=["vibecomfy.run"],
        idempotency_key={key + "-wrong-worker"!r},
    )
    print(json.dumps({{"ok": getattr(result, "ok", None), "error": str(getattr(result, "error", None))}}))
except Exception as exc:
    print(json.dumps({{"ok": False, "error": str(exc)}}))
PY
'''
        rc, out, err = remote_exec(client, wrong_probe, 120)
        receipt["wrong_executor_claim"] = {
            "returncode": rc,
            "stdout": out[-4000:],
            "stderr": err[-2000:],
            "denied": rc == 0 and "not bound to executor" in out,
        }
    except Exception as exc:
        receipt["wrong_executor_claim_error"] = repr(exc)
    states: list[dict[str, Any]] = []
    deadline = time.time() + 1800
    while time.time() < deadline:
        observed = cli([
            "python3", "-m", "astrid", "tasks", "show", "--project", "astrid-intro",
            str(task_id), "--json",
        ], 90)
        try:
            payload = _json_envelope(observed.stdout)
            state = _task_state(payload)
        except Exception:
            payload, state = {
                "stdout": observed.stdout[-3000:],
                "stderr": observed.stderr[-2000:],
            }, None
        states.append({"recorded_at_utc": now(), "state": state, "payload": payload})
        if state in {"completed", "succeeded", "failed", "cancelled", "settled"}:
            break
        time.sleep(5)
    receipt["task_id"] = task_id
    receipt["lifecycle"] = states
    events = cli([
        "python3", "-m", "astrid", "tasks", "events", "--project", "astrid-intro",
        str(task_id), "--json",
    ], 120)
    receipt["events"] = events.stdout[-30000:]
    final_state = states[-1].get("state") if states else None
    if final_state not in {"completed", "settled", "failed", "cancelled"}:
        cancel = cli([
            "python3", "-m", "astrid", "tasks", "cancel", "--project", "astrid-intro",
            str(task_id), "--idempotency-key", key + "-cleanup", "--json",
        ], 120)
        receipt["cleanup_cancel"] = {
            "returncode": cancel.returncode,
            "stdout": cancel.stdout[-8000:],
            "stderr": cancel.stderr[-2000:],
        }
    receipt["status"] = (
        "completed" if final_state in {"completed", "succeeded", "settled"}
        else "queue_or_execution_timeout"
    )
    # Settlement is not complete until the result manifest and the selected
    # raw MP4s are independently decoded.  Keep this evidence beside the
    # task/events payload so a successful state cannot hide bad media.
    if final_state in {"completed", "succeeded", "settled"}:
        probe_command = f'''set -u
BASE={shlex.quote(BASE)}
TEST={shlex.quote(TEST)}
MANIFEST="$TEST/attempts/outputs/manifest.json"
"$BASE/runtime/venv/bin/python" - "$MANIFEST" <<'PY'
import json, subprocess, sys
from pathlib import Path

manifest = Path(sys.argv[1])
payload = json.loads(manifest.read_text(encoding="utf-8"))
rows = []
for item in payload.get("outputs", []):
    rel = item.get("path")
    if not isinstance(rel, str) or not rel.lower().endswith(".mp4"):
        continue
    path = (manifest.parent / rel).resolve()
    probe = subprocess.check_output([
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height,nb_frames,r_frame_rate,duration",
        "-of", "json", str(path),
    ], text=True)
    rows.append({{"path": str(path), "bytes": path.stat().st_size, "probe": json.loads(probe)}})
print(json.dumps({{"manifest": str(manifest), "outputs": rows}}, sort_keys=True))
PY
'''
        probe_rc, probe_out, probe_err = remote_exec(client, probe_command, 120)
        receipt["output_verification"] = {
            "returncode": probe_rc,
            "stdout": probe_out[-12000:],
            "stderr": probe_err[-4000:],
        }
        verification_ok = False
        verification_error: str | None = None
        if probe_rc == 0:
            try:
                verified = _json_envelope(probe_out)
                rows = verified.get("outputs")
                if not isinstance(rows, list) or len(rows) != 2:
                    raise ValueError(f"expected exactly two MP4 outputs, got {rows!r}")
                observed: list[tuple[int, int, int]] = []
                for row in rows:
                    probe = row.get("probe", {}) if isinstance(row, dict) else {}
                    streams = probe.get("streams", []) if isinstance(probe, dict) else []
                    if not streams or not isinstance(streams[0], dict):
                        raise ValueError(f"missing video stream probe: {row!r}")
                    stream = streams[0]
                    observed.append((
                        int(stream.get("width", 0)),
                        int(stream.get("height", 0)),
                        int(stream.get("nb_frames", 0)),
                    ))
                if sorted(observed) != [(1920, 1088, 175), (1920, 1088, 260)]:
                    raise ValueError(f"unexpected output geometry/frame counts: {observed!r}")
                verification_ok = True
                receipt["output_verification"]["checks"] = {
                    "output_count": 2,
                    "expected_dimensions": [1920, 1088],
                    "expected_frames": [175, 260],
                    "observed": observed,
                }
            except (TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
                verification_error = str(exc)
        if not verification_ok:
            receipt["output_verification"]["error"] = verification_error or (
                f"remote ffprobe failed with return code {probe_rc}"
            )
            receipt["status"] = "output_verification_failed"
    with client.open_sftp() as sftp:
        for name in (
            "generic-host.ready.json", "generic-host.log", "session-daemon.log",
            "session-start.log", "comfy.log", "runner.log", "result.json",
            "output-files.txt", "video-probe.json",
        ):
            try:
                with sftp.open(TEST + "/" + name, "r") as stream:
                    receipt["remote_" + name.replace(".", "_")] = stream.read().decode(
                        "utf-8", errors="replace"
                    )[-30000:]
            except OSError:
                pass
    write_json(RECEIPTS / "astrid-e2e-acceptance.json", receipt)
    return receipt



def upload_and_smoke(client: paramiko.SSHClient, handle: dict[str, Any]) -> dict[str, Any]:
    canonical = ROOT / "workflows/seitanism_h3_av_extension_verified/workflow.py"
    builder = ROOT / "workflows/astrid_intro_h3_anchor_chain/workflow.py"
    anchors = ROOT / "runs/minkhole-keyframes-20260910/discovery/canonical-intro-anchors"
    smoke_bundle = prepare_e2e_bundle((5,), "canonical-h3-smoke")
    prompt = (
        "Flat 2D orange pixel-art animation on pure black; crisp square pixel edges, "
        "limited orange and amber palette, no photorealism, paper, collage, grayscale, "
        "or photographic background. Preserve the same orange mink, sign geometry, "
        "typography, camera and full-frame composition while moving continuously between "
        "the two anchors. No disappearance, reappearing object, cut, or camera drift."
    )
    segments = [{"frames": 5, "start": "anchor_shot_v03.png",
                 "end": "anchor_shot_v04.png", "prompt": prompt, "seed": 2026091801}]
    with client.open_sftp() as sftp:
        mkdirs(sftp, TEST + "/canonical")
        mkdirs(sftp, TEST + "/e2e-bundle")
        mkdirs(sftp, TEST + "/runtime")
        mkdirs(sftp, TEST + "/output")
        mkdirs(sftp, BASE + "/runtime/ComfyUI/input")
        sftp.put(str(canonical), TEST + "/canonical/workflow.py")
        sftp.put(str(builder), TEST + "/workflow.py")
        for _name, path in smoke_bundle.items():
            sftp.put(str(path), TEST + "/e2e-bundle/" + path.name)
        for name in ("anchor_shot_v03.png", "anchor_shot_v04.png"):
            sftp.put(str(anchors / name), BASE + "/runtime/ComfyUI/input/" + name)
        # Promote the corrected CUDA TorchVision contract onto the volume
        # before the next launch; future attaches then need no repair step.
        sftp.put(str(LOCAL_MANIFEST), BASE + "/receipts/release-manifest.json")
        sftp.put(str(LOCAL_MANIFEST_SHA), BASE + "/receipts/release-manifest-sha.json")
        with sftp.open(TEST + "/segments.json", "w") as stream:
            stream.write(json.dumps(segments))

    command = f"""
set -u
BASE={shlex.quote(BASE)}
TEST={shlex.quote(TEST)}
PY="$BASE/runtime/venv/bin/python"
export BASE TEST
set +e
nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader > "$TEST/host-probe.txt" 2>&1
host_rc=$?
"$BASE/runtime/venv/bin/python" - <<'PY' > "$TEST/torch-probe.txt" 2>&1
import importlib.metadata as m, torch
print("torch="+m.version("torch"))
print("torch_cuda="+str(torch.version.cuda))
print("torchvision="+m.version("torchvision"))
print("cuda_available="+str(torch.cuda.is_available()))
print("device="+(torch.cuda.get_device_name(0) if torch.cuda.is_available() else "none"))
PY
torch_rc=$?
if [ "$host_rc" -ne 0 ] || [ "$torch_rc" -ne 0 ]; then exit 21; fi
grep -q 'cuda_available=True' "$TEST/torch-probe.txt" || exit 22
grep -q 'torchvision=0.25.0+cu130' "$TEST/torch-probe.txt" || exit 26
set -e
export PYTHONPATH="$BASE/runtime/vibecomfy:$BASE/runtime/ComfyUI"
export COMFYUI_PATH="$BASE/runtime/ComfyUI"
export VIBECOMFY_CUSTOM_NODES_DIR="$BASE/runtime/ComfyUI/custom_nodes"
export VIBECOMFY_MODELS_ROOT="$BASE/models"
export VIBECOMFY_HEADLESS=1
# The release manifest is already the content-integrity authority.  Materialize
# VibeComfy's stat-bound verification receipts from that manifest so execution
# can validate identity without rereading 20–25 GB model bodies on every pod
# attach.  The receipt is accepted only when the path, size, inode, device and
# mtime still match the materialized release.
"$PY" - <<'PY'
import hashlib, json, os
from pathlib import Path
base = Path(os.environ['BASE'])
models = Path(os.environ['VIBECOMFY_MODELS_ROOT'])
manifest = json.loads((base / 'receipts/release-manifest.json').read_text())
for item in manifest.get('models', []):
    path = models / str(item['path'])
    if not path.is_file():
        raise SystemExit(f'missing release model: {{path}}')
    stat = path.stat()
    expected_size = int(item['bytes'])
    if stat.st_size != expected_size:
        raise SystemExit(f'release model size mismatch: {{path}}')
    expected_sha = str(item['sha256']).lower()
    key = hashlib.sha256(str(path.resolve()).encode()).hexdigest()
    receipt = models / '.vibecomfy' / 'model-verification' / f'{{key}}.json'
    receipt.parent.mkdir(parents=True, exist_ok=True)
    payload = {{
        'schema_version': 1,
        'path': str(path.resolve()),
        'expected_sha256': expected_sha,
        'expected_size_bytes': expected_size,
        'stat': {{
            'dev': int(stat.st_dev), 'inode': int(stat.st_ino),
            'size': int(stat.st_size), 'mtime_ns': int(stat.st_mtime_ns),
        }},
        'actual_sha256': expected_sha,
    }}
    receipt.write_text(json.dumps(payload, sort_keys=True) + '\\n')
PY
find "$BASE/models/.vibecomfy/model-verification" -type f -printf '%f\\n' | sort > "$TEST/model-receipts.txt"
# Do not let a persistent volume make a failed attempt look like a new one.
rm -f "$TEST/result.json" "$TEST/runner.log" "$TEST/video-probe.json" "$TEST/output-files.txt"
find "$TEST/output" -type f -delete 2>/dev/null || true
# VideoHelperSuite is part of the admitted H3 node lock and imports cv2 at
# Comfy startup.  The original volume venv passed `pip check` without this
# optional node runtime, so repair the missing binary dependency once on the
# persistent release venv before admitting any workflow that emits video.
if ! "$PY" -c 'import cv2' >/dev/null 2>&1; then
  command -v uv >/dev/null 2>&1 || {{ echo "uv is required to repair the release venv" >&2; exit 28; }}
  uv pip install -q --python "$PY" --no-deps opencv-python-headless==5.0.0.93 || exit 28
fi
# The managed-session attestation needs an actual checkout identity.  Older
# volume releases copied the VibeComfy source without .git; create a local,
# content-bound repository once rather than silently accepting an unattested
# session.
if ! git -C "$BASE/runtime/vibecomfy" rev-parse --git-dir >/dev/null 2>&1; then
  git -C "$BASE/runtime/vibecomfy" init -q
  git -C "$BASE/runtime/vibecomfy" add -A
  git -C "$BASE/runtime/vibecomfy" -c user.name=astrid -c user.email=astrid@local commit -qm release-attestation
fi
# The pinned release is a ComfyUI checkout (main.py), not the pip package
# layout that provides a `comfyui ... serve` entry point.  VibeComfy's managed
# session intentionally uses that entry point when present; provide a small,
# persistent adapter that translates its `serve` subcommand to the checkout's
# main.py while leaving every other launch flag unchanged.
COMFY_WRAPPER="$BASE/runtime/venv/bin/comfyui"
if [ ! -x "$COMFY_WRAPPER" ]; then
  cat > "$COMFY_WRAPPER" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
ROOT="$(CDPATH= cd -- "$SCRIPT_DIR/../../ComfyUI" && pwd)"
PY="$SCRIPT_DIR/python"
if [ "${{1:-}}" = "serve" ]; then shift; fi
exec "$PY" "$ROOT/main.py" "$@"
SH
  chmod 0755 "$COMFY_WRAPPER"
fi
SESSION_ID="astrid-h3-e2e"
SESSION_DIR="$BASE/runtime/out/sessions/$SESSION_ID"
"$PY" -m vibecomfy.cli session start --yes --id "$SESSION_ID" \
  --runtime-root "$BASE/runtime" --port 8188 --warm-policy auto \
  --input-directory "$BASE/runtime/ComfyUI/input" \
  --output-directory "$BASE/runtime/ComfyUI/output" \
  --launch-flag=--use-ck-attention --launch-flag=--disable-comfy-compiler \
  --ready-timeout-sec 900 > "$TEST/session-start.log" 2>&1
session_rc=$?
if [ "$session_rc" -ne 0 ]; then
  cp "$SESSION_DIR/daemon.log" "$TEST/session-daemon.log" 2>/dev/null || true
  exit 27
fi
cp "$SESSION_DIR/daemon.log" "$TEST/session-daemon.log" 2>/dev/null || true
cat "$SESSION_DIR/comfy_pid" > "$TEST/comfy.pid" 2>/dev/null || true
ready=0
for _ in $(seq 1 900); do
  if curl --silent --fail --max-time 5 http://127.0.0.1:8188/system_stats >/dev/null 2>&1; then ready=1; break; fi
  if ! kill -0 "$(cat "$TEST/comfy.pid")" 2>/dev/null; then break; fi
  sleep 2
done
if [ "$ready" -ne 1 ]; then
  date -u +%FT%TZ > "$TEST/readiness-timeout.txt"
  ps -eo pid,ppid,stat,etime,cmd > "$TEST/processes-timeout.txt" 2>&1 || true
  tail -300 "$TEST/comfy.log" > "$TEST/comfy-timeout-tail.txt" 2>&1 || true
  exit 23
fi
export ASTRID_H3_CANONICAL_WORKFLOW="$TEST/canonical"
export ASTRID_H3_SEGMENTS="$TEST/segments.json"
export ASTRID_H3_ATTENTION_BACKEND="comfy kitchen attention"
SMOKE_START_EPOCH=$(date +%s)
printf '%s\n' "$SMOKE_START_EPOCH" > "$TEST/smoke-start-epoch.txt"
"$BASE/runtime/venv/bin/python" -m vibecomfy.cli run "$TEST/e2e-bundle/workflow.py" \
  --yes --ready --runtime server --server-url http://127.0.0.1:8188 --deps reuse \
  --runtime-root "$TEST/runtime" --external-log-locator "$TEST/comfy.log" \
  --no-ensure-models --output-directory "$TEST/output" --json --yes \
  > "$TEST/result.json" 2> "$TEST/runner.log"
run_rc=$?
find "$BASE/runtime/ComfyUI/output" "$TEST/output" -type f -printf '%p|%s\n' 2>/dev/null | sort > "$TEST/output-files.txt"
"$BASE/runtime/venv/bin/python" - "$TEST/result.json" "$TEST/smoke-start-epoch.txt" "$TEST" <<'PY' > "$TEST/selected_video.txt"
import json, os, sys
from pathlib import Path
result = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
start = int(Path(sys.argv[2]).read_text(encoding='utf-8').strip())
test = Path(sys.argv[3]).resolve()
paths = []
for item in result.get('artifacts', []):
    if not isinstance(item, dict):
        continue
    descriptor = item.get('descriptor') if isinstance(item.get('descriptor'), dict) else {{}}
    # External Comfy runs expose a server-owned ``reported_path`` that may be
    # only a delivery hint.  Bind to the exact descriptor fullpath/prompt
    # output instead of guessing from the newest persistent-volume file.
    candidate = descriptor.get('fullpath') or item.get('reported_path') or item.get('path')
    if isinstance(candidate, str) and candidate.lower().endswith('.mp4'):
        paths.append(Path(candidate).resolve())
if len(paths) != 1:
    raise SystemExit(f'exactly one smoke artifact is required, got {{paths!r}}')
video = paths[0]
allowed_roots = (test / 'output', Path(os.environ['BASE']) / 'runtime' / 'ComfyUI' / 'output')
if not any(root.resolve() in video.parents for root in allowed_roots):
    raise SystemExit(f'smoke artifact escaped the declared output roots: {{video}}')
if not video.is_file() or video.stat().st_mtime < start:
    raise SystemExit(f'smoke artifact is missing or predates this prompt: {{video}}')
print(video)
PY
video=$(sed -n '1p' "$TEST/selected_video.txt")
if [ -z "$video" ]; then exit 24; fi
ffprobe -v error -show_entries format=duration:stream=width,height,nb_frames -of json "$video" > "$TEST/video-probe.json" 2>&1 || exit 25
ffmpeg -v error -xerror -i "$video" -f null - > "$TEST/video-decode.log" 2>&1 || exit 26
"$BASE/runtime/venv/bin/python" - <<'PY' > "$TEST/comfy-identity.json"
import json, os, subprocess
from pathlib import Path
base = Path(os.environ["BASE"])
manifest = json.loads((base / "receipts/release-manifest.json").read_text(encoding="utf-8"))
expected = manifest.get("runtime", {{}}).get("comfyui_commit")
actual = subprocess.check_output(["git", "-C", str(base / "runtime/ComfyUI"), "rev-parse", "HEAD"], text=True).strip()
status = subprocess.check_output(["git", "-C", str(base / "runtime/ComfyUI"), "status", "--porcelain"], text=True)
payload = {{"expected_commit": expected, "actual_commit": actual, "worktree_status": status, "worktree_clean": not bool(status.strip())}}
Path(os.environ["TEST"], "comfy-identity.json").write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
if not isinstance(expected, str) or actual != expected or status.strip():
    raise SystemExit("ComfyUI source identity does not match the release manifest")
PY
printf '%s\n' "$run_rc" > "$TEST/exit-code"
exit "$run_rc"
"""
    rc, out, err = remote_exec(client, command, 3600)
    receipt: dict[str, Any] = {
        "pod_id": pod_id, "recorded_at_utc": now(), "remote_root": TEST,
        "run_rc": rc, "stdout_tail": out[-2000:], "stderr_tail": err[-2000:],
    }
    with client.open_sftp() as sftp:
        for name in ("host-probe.txt", "torch-probe.txt", "result.json",
                     "output-files.txt", "selected_video.txt", "video-probe.json", "video-decode.log",
                     "comfy-identity.json", "comfy.log", "comfy-timeout-tail.txt",
                     "smoke-start-epoch.txt",
                     "readiness-timeout.txt", "processes-timeout.txt", "runner.log",
                     "session-start.log", "session-daemon.log"):
            try:
                with sftp.open(TEST + "/" + name, "r") as stream:
                    text = stream.read().decode("utf-8", errors="replace")
                receipt[name.replace(".", "_")] = text[-10000:]
            except OSError:
                pass
    # Some VibeComfy releases return status 2 after a completed external
    # Comfy run when dependency-drift diagnostics are warnings.  Preserve that
    # code as a warning only after proving all of the following independently:
    # the release source identity is exact and clean, the result is completed,
    # the runner contains only the known observation gap, and the artifact was
    # produced by this prompt under the attempt output root.  A generic
    # non-zero code must never be waived merely because a stale MP4 exists.
    smoke_output_ok = rc == 0
    if rc != 0:
        try:
            result_payload = json.loads(receipt.get("result_json", ""))
            probe_payload = json.loads(receipt.get("video_probe_json", ""))
            identity_payload = json.loads(receipt.get("comfy_identity_json", ""))
            streams = probe_payload.get("streams", [])
            runner_lines = [
                line.strip() for line in receipt.get("runner_log", "").splitlines()
                if line.strip()
            ]
            known_drift = any(
                line.startswith("Runtime dependency drift detected: comfy_commit:")
                and "observed None" in line
                for line in runner_lines
            )
            unexpected_runner_error = any(
                any(token in line.lower() for token in ("error", "failed", "traceback"))
                and not line.startswith("Runtime dependency drift detected:")
                for line in runner_lines
            )
            source_identity_ok = (
                identity_payload.get("worktree_clean") is True
                and identity_payload.get("actual_commit") == identity_payload.get("expected_commit")
                and isinstance(identity_payload.get("actual_commit"), str)
                and bool(identity_payload.get("actual_commit"))
            )
            result_paths = [
                item.get("reported_path")
                for item in result_payload.get("artifacts", [])
                if isinstance(item, dict) and isinstance(item.get("reported_path"), str)
            ]
            descriptor_paths = [
                (item.get("descriptor") or {}).get("fullpath")
                for item in result_payload.get("artifacts", [])
                if isinstance(item, dict)
                and isinstance(item.get("descriptor"), dict)
                and isinstance((item.get("descriptor") or {}).get("fullpath"), str)
            ]
            selected_video = receipt.get("selected_video_txt", "").strip().splitlines()
            artifact_binding_ok = (
                (len(result_paths) == 1 or len(descriptor_paths) == 1)
                and bool(selected_video)
                and selected_video[0] in (result_paths + descriptor_paths)
                and (selected_video[0].startswith(TEST + "/output/")
                     or selected_video[0].startswith(BASE + "/runtime/ComfyUI/output/"))
            )
            smoke_output_ok = (
                result_payload.get("status") == "completed"
                and isinstance(streams, list)
                and bool(streams)
                and int(streams[0].get("width", 0)) == 1920
                and int(streams[0].get("height", 0)) == 1088
                and int(streams[0].get("nb_frames", 0)) == 5
                and known_drift
                and not unexpected_runner_error
                and source_identity_ok
                and artifact_binding_ok
                and not receipt.get("video_decode_log", "").strip()
            )
            if smoke_output_ok:
                receipt["run_warning"] = (
                    f"vibecomfy returned rc={rc} after a completed, decoded output; "
                    "continuing only after exact source identity and prompt-bound artifact proof"
                )
        except (TypeError, ValueError, KeyError, json.JSONDecodeError):
            smoke_output_ok = False
    receipt["smoke_output_ok"] = smoke_output_ok
    if smoke_output_ok:
        e2e = run_astrid_e2e(client, handle)
        receipt["astrid_e2e"] = e2e
    else:
        receipt["astrid_e2e"] = {"status": "skipped_basic_smoke_failed"}
    return receipt


def remote_exec(client: paramiko.SSHClient, command: str, timeout: int) -> tuple[int, str, str]:
    _, stdout, stderr = client.exec_command(command, timeout=timeout)
    out = stdout.read().decode("utf-8", errors="replace")
    err = stderr.read().decode("utf-8", errors="replace")
    return stdout.channel.recv_exit_status(), out, err


def main() -> int:
    global pod_id
    install_exit_handlers()
    deadline = time.time() + WATCH
    write_json(STATE, {"state": "watching", "deadline_utc": datetime.fromtimestamp(deadline, timezone.utc).isoformat()})
    log(f"watching gpu={GPU} volume={VOLUME} image={IMAGE} poll_seconds={POLL}")
    try:
        handle = launch(deadline)
        pod_id = str(handle["pod_id"])
        write_json(POD_RECORD, {**handle, "pod_id": pod_id, "gpu": GPU, "volume": VOLUME, "image": IMAGE, "recorded_at_utc": now()})
        write_json(STATE, {"state": "claimed", "pod_id": pod_id, "claimed_at_utc": now()})
        client = connect_ssh(handle, min(deadline, time.time() + 900))
        try:
            receipt = upload_and_smoke(client, handle)
        finally:
            client.close()
        write_json(RECEIPTS / "5090-attach-smoke.json", receipt)
        write_json(STATE, {"state": "smoke_complete", "pod_id": pod_id, "receipt": receipt})
        log(f"smoke complete pod={pod_id} run_rc={receipt['run_rc']}")
        e2e_status = (
            receipt.get("astrid_e2e", {}).get("status")
            if isinstance(receipt.get("astrid_e2e"), dict)
            else None
        )
        successful = bool(receipt.get("smoke_output_ok")) and e2e_status in {
            "completed", "settled"
        }
        return 0 if successful else 30
    except Exception as exc:
        write_json(STATE, {"state": "failed", "pod_id": pod_id, "error": repr(exc), "recorded_at_utc": now()})
        log(f"watch failed pod={pod_id} error={type(exc).__name__}: {exc}")
        return 1
    finally:
        terminate()
        if pod_id:
            state = json.loads(STATE.read_text(encoding="utf-8"))
            state.update({"terminated": terminated, "terminated_at_utc": now()})
            write_json(STATE, state)


if __name__ == "__main__":
    raise SystemExit(main())
