#!/usr/bin/env python3
"""Verify the transported source pair before workspace setup or engine downloads."""
from __future__ import annotations

from astrid.pack_api import checkout_root

import hashlib
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys


def verify(source_root: Path, profile_path: Path) -> dict:
    try:
        return _verify(source_root, profile_path)
    except (OSError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        raise ValueError("pod source identity evidence is missing or invalid") from exc


def _verify(source_root: Path, profile_path: Path) -> dict:
    from astrid_runtime.bootstrap import SourceProfile
    from astrid_runtime.identity import requested_startup_identity
    from runtime_protocol.source_identity import validate_checkout_root

    root = Path(source_root).resolve(strict=True)
    # A source bootstrap must never silently fall back to an installer stamp.
    if any(k in os.environ for k in ("ASTRID_INSTALL_RELEASE", "ASTRID_INSTALL_MANAGED_BY")):
        raise ValueError("pod source identity cannot use installer identity stamps")
    source = SourceProfile.load(Path(profile_path).resolve(strict=True))
    agent, runtime = root / "astrid-agent", root / "astrid-runtime"
    if (Path(source.source_checkout) != agent or Path(source.runtime_checkout) != runtime):
        raise ValueError("pod source profile differs from transported checkouts")
    manifest = json.loads((root / ".pod-source.json").read_text())
    names = {"astrid-agent", "astrid-runtime", "runpod-python", "runpod-lifecycle", "VibeComfy"}
    if set(manifest) != {"heads", "runtime_lock_sha256"} or set(manifest["heads"]) != names:
        raise ValueError("pod source manifest is incomplete")
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    for name in sorted(names):
        checkout = validate_checkout_root(root / name)
        def git(*args):
            return subprocess.run(["git", "-C", str(checkout), *args], env=env,
                                  capture_output=True, check=True, timeout=30).stdout.strip()
        if git("rev-parse", "HEAD").decode() != manifest["heads"][name] or git("status", "--porcelain", "--untracked-files=all"):
            raise ValueError(f"pod source checkout identity differs: {name}")
    lock = agent / "runtime.lock.json"
    if hashlib.sha256(lock.read_bytes()).hexdigest() != manifest["runtime_lock_sha256"]:
        raise ValueError("pod Runtime lock digest differs from transported provenance")
    check = runpy.run_path(str(checkout_root() / "scripts" / "vendor_lock.py"))["check"]
    if check(lock, runtime, agent):
        raise ValueError("pod Runtime producer or vendored artifacts differ from runtime.lock.json")
    identity = requested_startup_identity(source)
    if (identity.get("managed_by") != "source" or identity.get("dirty") is not False
            or identity.get("runtime_head") != manifest["heads"]["astrid-runtime"]
            or identity.get("agent_head") != manifest["heads"]["astrid-agent"]):
        raise ValueError("pod Runtime source identity differs from transported provenance")
    return identity


if __name__ == "__main__":
    try:
        identity = verify(Path(sys.argv[1]), Path(sys.argv[2]))
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        sys.exit("pod_bootstrap: source Runtime identity verification failed")
    print("pod_bootstrap: source Runtime identity verified " + json.dumps(identity, sort_keys=True))
