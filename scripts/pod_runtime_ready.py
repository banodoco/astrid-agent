#!/usr/bin/env python3
"""Check the transported Runtime through its strict SDK handshake before models."""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys

RUNTIME_DATA = Path("/root/.local/share/astrid-runtime")


def validate_request(request):
    if (not isinstance(request, dict)
            or set(request) != {"pod_id", "volume_id", "nonce", "runtime_head", "runtime_lock_sha256"}
            or any(not isinstance(value, str) for value in request.values())
            or not re.fullmatch(r"[a-z0-9]+", request["pod_id"])
            or not re.fullmatch(r"[A-Za-z0-9_-]+", request["volume_id"])
            or not re.fullmatch(r"[0-9a-f]{16,128}", request["nonce"])
            or not re.fullmatch(r"[0-9a-f]{40}", request["runtime_head"])
            or not re.fullmatch(r"[0-9a-f]{64}", request["runtime_lock_sha256"])):
        raise ValueError("invalid Runtime readiness request")
    return request


def validate_runtime_status(status, owner, expected, *, endpoint):
    from astrid_runtime.bootstrap import _validated_matching_owner_identity

    if not isinstance(status, dict) or not isinstance(owner, dict):
        raise ValueError("Runtime readiness evidence is invalid")
    discovery, health, support = (status.get(key) for key in ("discovery", "health", "support"))
    if any(not isinstance(value, dict) for value in (discovery, health, support)):
        raise ValueError("Runtime readiness evidence is incomplete")
    instance, realm = discovery.get("runtime_instance_id"), discovery.get("active_realm")
    if (not isinstance(instance, str) or not instance or not isinstance(realm, str) or not realm
            or support.get("healthy") is not True or status.get("stale_discovery")
            or health.get("status") != "ok" or support.get("realm_id") != realm
            or discovery.get("endpoint") != endpoint
            or owner.get("runtime_instance_id") != instance or owner.get("realm_id") != realm
            or type(owner.get("pid")) is not int or owner["pid"] <= 0
            or owner["pid"] != discovery.get("pid")
            or not owner.get("process_birth_id")
            or owner["process_birth_id"] != discovery.get("process_birth_id")
            or not owner.get("realm_root") or owner["realm_root"] != discovery.get("realm_root")
            or discovery.get("identity") != expected):
        raise ValueError("Runtime owner, realm or readiness differs")
    _validated_matching_owner_identity(health, instance_id=instance, requested_identity=expected)
    return {"runtime_instance_id": instance, "realm_id": realm, "identity": expected}


def _owner(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "r", encoding="utf-8") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 65536:
            raise ValueError("Runtime owner receipt is unsafe")
        return json.load(stream)


def runtime_probe(request, *, source_root, data_root=RUNTIME_DATA):
    from astrid.sdk.client import AstridClient
    from pod_source_identity import verify

    validate_request(request)
    source_root, data_root = Path(source_root), Path(data_root)
    if not data_root.is_absolute() or any(path.is_symlink() for path in (data_root, *data_root.parents)):
        raise ValueError("Runtime support root is unsafe")
    profile = data_root / "runtime/source-profiles/astrid.json"
    # Retain the early static check and rebind this live probe to the same lock.
    expected = verify(source_root, profile)
    lock = source_root / "astrid-agent/runtime.lock.json"
    if (expected["runtime_head"] != request["runtime_head"]
            or hashlib.sha256(lock.read_bytes()).hexdigest() != request["runtime_lock_sha256"]):
        raise ValueError("Runtime differs from controller's exact lock")
    overrides = {"ASTRID_RUNTIME_ENDPOINT", "ASTRID_RUNTIME_CREDENTIAL", "ASTRID_RUNTIME_LAUNCHER"}
    if any(value for key, value in os.environ.items()
           if key.startswith("ASTRID_TIMELINE_EVAL_") or key in overrides):
        raise ValueError("ambient Runtime connection override is unsafe")
    binary = str(Path(sys.executable).with_name("astrid-runtime"))
    selected = {"ASTRID_RUNTIME_DATA_ROOT": str(data_root),
                "ASTRID_RUNTIME_SOURCE_MANIFEST": str(profile), "ASTRID_RUNTIME_LAUNCHER": binary}
    previous = {key: os.environ.get(key) for key in selected}
    os.environ.update(selected)

    def lifecycle(verb):
        # status is an observer and has no --profile option in the pinned CLI.
        argv = [binary, verb, "--data-root", str(data_root), "--json"]
        if verb == "down":
            argv.extend(("--profile", "astrid"))
        result = subprocess.run(argv, capture_output=True, text=True, timeout=180, check=False)
        if result.returncode:
            raise ValueError("Runtime lifecycle readiness/cleanup failed")
        value = json.loads(result.stdout)
        if not isinstance(value, dict):
            raise ValueError("Runtime lifecycle receipt is invalid")
        return value

    receipt = None
    try:
        try:
            # Runtime connect/up checks exact source identity; this SDK call then
            # validates authenticated health, protocol, scopes, realm and actor.
            with AstridClient.open_from_launcher(start_pack_host=False) as client:
                status = lifecycle("status")
                receipt = validate_runtime_status(
                    status, _owner(data_root / "runtime/instance.lock"), expected, endpoint=client.endpoint)
        finally:
            # action run owns a separate ephemeral Runtime; free the door first.
            stopped = lifecycle("down")
            if (stopped.get("status") != "stopped"
                    or (receipt is not None and stopped.get("realm_id") != receipt["realm_id"])):
                raise ValueError("Runtime cleanup was not confirmed")
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    return {**receipt, **request, "ready": True, "stopped": True}


def main():
    try:
        if len(sys.argv) != 2:
            raise ValueError("expected encoded Runtime readiness request")
        request = json.loads(base64.b64decode(sys.argv[1], validate=True))
        receipt = runtime_probe(request, source_root=Path(__file__).resolve().parents[2])
        print(json.dumps(receipt, sort_keys=True))
    except Exception:
        # SDK and lifecycle diagnostics can contain connection credentials.
        sys.exit("pod Runtime identity/readiness verification failed")


if __name__ == "__main__":
    main()
