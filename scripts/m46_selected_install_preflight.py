#!/usr/bin/env python3
"""Fail-closed input preflight for the M46 selected-install venue.

This is NOT an install executor or an observer binding.  It always exits
nonzero, including when all inputs validate and Docker is available.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys

SCHEMA = "astrid.m46.selected-install-preflight-request.v1"
BASE = "b54117c9312165e2799554ec27480665d5cad2b1"
REFS = {
    "app": "79f7e5397c044c4c97dc7e97b20ad96f787e5247",
    "agent": "ab43bad818ef65ca1e929b973a2f61cd9fc4bf61",
    "runtime": "2776ba973b6c8f3555b5629b0840d9691a98d3c2",
    "pinokio": "f8605a3877ac76c0d7f65e4b013bc112b8c96992",
}
SOURCE_HASHES = {
    ("app", "public/install-manifest.json"): "b2584f0bb5a949890364004572a0182f2b568adcdba061e73b739e1d3d2d623f",
    ("app", "public/install"): "100bab1deb15d620abd910c8fb0c9a6d77f4945428a3229dac6b506705e3fd0d",
    ("pinokio", "pinokio/install.js"): "60f4473e91b769af0480098b96a36500f3c1628ac33a7617f68df4de1fe436e2",
    ("pinokio", "pinokio/start.js"): "442ff8fb73d65047bc4dd34de997bd36920efab16733aeb6ddd967afe291c037",
    ("pinokio", "pinokio/lib.js"): "59bb0a556b425dafb4d986aaab54f0dbd23a988814a0d64a6cb4437c52d3d998",
    ("pinokio", "pinokio/session.mjs"): "460ff77a8cda2b711c785a607e4a399f8c33ddc903496e4c769b717d010aad99",
    ("pinokio", "pinokio/providers/r24-action.cjs"): "2901c4e75b11a96547d2033e62118ee64247670b8f9531fc6bbad9fb1b34be01",
    ("pinokio", "pinokio/providers/r24-linux.py"): "978b52548281250ed264d7f08d8c8179419a1aa11035ddb1d6450960839e0dc9",
    ("pinokio", "pinokio/providers/r24-linux-binding.json"): "52ef516056a70c033cfc33b8a411d98fcb358474810a0a957fde2c3f3e5c6f53",
    ("pinokio", ".github/venue/m46-engine-overlay.cjs"): "26822d88bffd8b038b954d3e851b3b061776ab2d9ae081f5eda3463e85048ca1",
}


class Refusal(ValueError):
    pass


def pairs_no_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise Refusal("duplicate JSON key")
        result[key] = value
    return result


def exact_keys(value, keys, label):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise Refusal(f"{label} has missing or extra keys")


def absolute_existing_directory(value, label):
    if not isinstance(value, str) or not value or not os.path.isabs(value):
        raise Refusal(f"{label} must be an absolute path")
    path = Path(value)
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise Refusal(f"{label} is unavailable") from exc
    if str(resolved) != value or not resolved.is_dir():
        raise Refusal(f"{label} must be a canonical directory without symlinks")
    return resolved


def git(root, *args):
    try:
        result = subprocess.run(["git", "-C", str(root), *args], stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
                                check=False, env={"PATH": os.defpath, "HOME": "/nonexistent",
                                                  "GIT_CONFIG_NOSYSTEM": "1",
                                                  "GIT_OPTIONAL_LOCKS": "0"})
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Refusal("git inspection failed") from exc
    if result.returncode:
        raise Refusal("git inspection failed")
    return result.stdout.decode("utf-8", errors="strict").strip()


def validate_checkout(root, expected):
    if git(root, "rev-parse", "--show-toplevel") != str(root):
        raise Refusal("checkout root mismatch")
    if git(root, "rev-parse", "HEAD") != expected:
        raise Refusal("checkout head mismatch")
    if git(root, "status", "--porcelain=v1", "--untracked-files=all", "--ignored=matching"):
        raise Refusal("checkout is dirty or contains ignored files")
    if git(root, "submodule", "status", "--recursive"):
        raise Refusal("submodules are not supported")


def validate_request(request, script=Path(__file__)):
    exact_keys(request, ("schema", "runner_head", "checkouts", "mode", "output_directory"), "request")
    if request["schema"] != SCHEMA or request["mode"] not in ("backend", "local"):
        raise Refusal("schema or mode is not supported")
    head = request["runner_head"]
    if not isinstance(head, str) or not re.fullmatch(r"[0-9a-f]{40}", head):
        raise Refusal("runner_head must be a full lowercase commit")
    exact_keys(request["checkouts"], (*REFS, "runner"), "checkouts")
    roots = {name: absolute_existing_directory(value, f"checkouts.{name}")
             for name, value in request["checkouts"].items()}
    output = absolute_existing_directory(request["output_directory"], "output_directory")
    paths = [*roots.values(), output]
    if any(a == b or a in b.parents or b in a.parents
           for index, a in enumerate(paths) for b in paths[index + 1:]):
        raise Refusal("checkout and output directories must be disjoint")
    if script.resolve(strict=True) != roots["runner"] / "scripts/m46_selected_install_preflight.py":
        raise Refusal("executable is not from the selected runner checkout")
    for name, sha in {**REFS, "runner": head}.items():
        validate_checkout(roots[name], sha)
    if git(roots["runner"], "rev-list", "--parents", "-n", "1", head).split() != [head, BASE]:
        raise Refusal("runner must be a direct single-parent child of the fixed base")
    info = output.stat()
    if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700 or any(output.iterdir()):
        raise Refusal("output directory must be empty, owned by caller, and mode 0700")
    hashes = {}
    for (name, relative), expected in SOURCE_HASHES.items():
        file = roots[name] / relative
        if file.is_symlink() or file.resolve(strict=True) != file:
            raise Refusal("source path is symlinked")
        actual = hashlib.sha256(file.read_bytes()).hexdigest()
        if actual != expected:
            raise Refusal(f"source hash mismatch: {name}/{relative}")
        hashes[f"{name}/{relative}"] = actual
    manifest = json.loads((roots["app"] / "public/install-manifest.json").read_bytes(), object_pairs_hook=pairs_no_duplicates)
    if manifest.get("agent", {}).get("sha") != REFS["agent"] or manifest.get("runtime", {}).get("sha") != REFS["runtime"]:
        raise Refusal("manifest tuple mismatch")
    return roots, output, hashes


def run(request):
    if sys.platform != "linux":
        raise Refusal("Linux is required")
    roots, output, hashes = validate_request(request)
    try:
        docker = subprocess.run(["docker", "info", "--format", "{{.ID}}"], stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20, check=False)
        docker_result = {"exit_code": docker.returncode, "daemon_available": docker.returncode == 0 and bool(docker.stdout.strip())}
    except (OSError, subprocess.TimeoutExpired):
        docker_result = {"exit_code": None, "daemon_available": False}
    result = {"schema": "astrid.m46.selected-install-preflight-result.v1", "status": "blocked",
              "runner_head": request["runner_head"], "tuple": REFS, "mode": request["mode"],
              "checkout_paths": {name: str(root) for name, root in roots.items()},
              "source_sha256": hashes, "docker_probe": docker_result,
              "install": "not-run", "observer_binding": "not-produced", "slot_receipt": "not-produced",
              "reason": "selected-install executor and protected live observer binding are not implemented"}
    payload = (json.dumps(result, sort_keys=True, indent=2) + "\n").encode()
    fd = os.open(output / "preflight-blocked.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(payload)
    return 2


def main(argv):
    if len(argv) != 2 or not os.path.isabs(argv[1]):
        print("usage: m46_selected_install_preflight.py /absolute/request.json", file=sys.stderr)
        return 2
    try:
        with open(argv[1], "rb") as stream:
            raw = stream.read(65537)
        if len(raw) > 65536:
            raise Refusal("request exceeds size limit")
        return run(json.loads(raw, object_pairs_hook=pairs_no_duplicates))
    except (ValueError, OSError, UnicodeError) as exc:
        print(f"preflight refused: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
