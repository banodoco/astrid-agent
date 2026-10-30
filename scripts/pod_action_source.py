#!/usr/bin/env python3
"""Fail-closed source selection for one pod action (embedded by pod_session)."""
from __future__ import annotations

import base64
import hashlib
import contextlib
import io
import importlib
import json
import os
from pathlib import Path
import re
import runpy
import subprocess
import sys
import tempfile


def validate_selection(value):
    keys = {"agent_root", "runtime_root", "agent_head", "runtime_head", "runtime_lock_sha256"}
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError("H3 source selection must contain exactly the required roots, heads and lock digest")
    for key in ("agent_root", "runtime_root"):
        path = value[key]
        if (not isinstance(path, str) or not path.startswith("/") or path == "/"
                or any(c in path for c in (":", "\x00", "\n", "\r"))
                or str(Path(path)) != path or ".." in Path(path).parts):
            raise ValueError("H3 source selection has an invalid absolute checkout root")
    if value["agent_root"] == value["runtime_root"]:
        raise ValueError("H3 source selection requires distinct checkouts")
    for key, size in (("agent_head", 40), ("runtime_head", 40), ("runtime_lock_sha256", 64)):
        if not isinstance(value[key], str) or not re.fullmatch(r"[0-9a-f]{%d}" % size, value[key]):
            raise ValueError("H3 source selection has an invalid identity")
    return dict(value)


def load_selection(text):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("ambiguous H3 source selection")
            result[key] = value
        return result
    return validate_selection(json.loads(text, object_pairs_hook=unique))


def verify_selection(value):
    """Select imports and reuse the producer-lock and canonical source identity checks."""
    selected = validate_selection(value)
    forbidden = {"ASTRID_RUNTIME_ENDPOINT", "ASTRID_RUNTIME_CREDENTIAL", "ASTRID_RUNTIME_LAUNCHER",
                 "ASTRID_INSTALL_RELEASE", "ASTRID_INSTALL_MANAGED_BY"}
    if any(key in os.environ for key in forbidden) or any(k.startswith("ASTRID_TIMELINE_EVAL_") for k in os.environ):
        raise ValueError("ambient Runtime identity override is unsafe")
    agent, runtime = (Path(selected[key]) for key in ("agent_root", "runtime_root"))
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    for key in list(os.environ):
        if key.upper().startswith("GIT_"):
            del os.environ[key]
    os.environ.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    for root, expected in ((agent, selected["agent_head"]), (runtime, selected["runtime_head"])):
        if any(p.is_symlink() for p in (root, *root.parents)) or root.resolve(strict=True) != root:
            raise ValueError("selected source root is not canonical")
        def git(*args):
            return subprocess.run(["git", "-C", str(root), *args], env=env, capture_output=True,
                                  check=True, timeout=60).stdout
        if (git("rev-parse", "--show-toplevel").decode().strip() != str(root)
                or git("rev-parse", "HEAD").decode().strip() != expected
                or git("status", "--porcelain", "--untracked-files=all")):
            raise ValueError("selected source checkout identity differs")
    # -I excluded ambient PYTHONPATH and the working directory in this process.
    sys.dont_write_bytecode = True
    sys.path[:0] = [str(agent), str(runtime)]
    for name, root in (("astrid", agent), ("astrid_runtime_client", agent),
                       ("astrid_runtime", runtime), ("runtime_protocol", runtime)):
        module = importlib.import_module(name)
        if Path(module.__file__).resolve() != root / name / "__init__.py":
            raise ValueError("selected source import differs")
    lock = agent / "runtime.lock.json"
    if (hashlib.sha256(lock.read_bytes()).hexdigest() != selected["runtime_lock_sha256"]
            or json.loads(lock.read_bytes()).get("source_commit") != selected["runtime_head"]):
        raise ValueError("selected Runtime lock differs")
    check = runpy.run_path(str(agent / "scripts/vendor_lock.py"))["check"]
    if check(lock, runtime, agent):
        raise ValueError("selected Runtime producer or vendored artifacts differ")
    from astrid_runtime.bootstrap import SourceProfile
    from astrid_runtime.identity import requested_startup_identity

    profile = SourceProfile.from_mapping({"profile": "astrid", "source_checkout": str(agent),
                                          "runtime_checkout": str(runtime), "runtime_environment": sys.prefix})
    identity = requested_startup_identity(profile)
    if (identity.get("managed_by") != "source" or identity.get("dirty") is not False
            or identity.get("agent_head") != selected["agent_head"]
            or identity.get("runtime_head") != selected["runtime_head"]):
        raise ValueError("selected Runtime source identity differs")
    return profile, identity


def install_selected(args, selected, identity):
    """Canonical same-root recovery followed by the ordinary local readiness gate."""
    from astrid.core import engine_cli
    from astrid.core.generation.engine_local_endpoint import (
        RECEIPT_RELATIVE_PATH, local_engine_endpoint, recorded_engine_root,
    )

    parsed = engine_cli._parser().parse_args(args[1:])
    if (args[:3] != ["engines", "install", "vibecomfy-h3"] or parsed.root is None
            or parsed.placement != "local" or parsed.dry_run
            or recorded_engine_root("vibecomfy-h3") != parsed.root):
        raise ValueError("installer requires the existing registered root")
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        code = engine_cli.main(args[1:])
    if code != 0:
        raise ValueError("canonical engine installation failed")
    # Canonical CLI output must equal the receipt it committed at the same root.
    result = json.loads(output.getvalue())
    receipt_path = Path(parsed.root) / RECEIPT_RELATIVE_PATH
    receipt_bytes = receipt_path.read_bytes()
    if (result != json.loads(receipt_bytes) or result.get("status") != "installed"
            or result.get("engine") != "vibecomfy-h3" or result.get("root") != parsed.root
            or recorded_engine_root("vibecomfy-h3") != parsed.root):
        raise ValueError("canonical engine receipt differs")
    ready = local_engine_endpoint(root=parsed.root, deadline_seconds=parsed.deadline).endpoint
    digest = hashlib.sha256(receipt_bytes).hexdigest()
    if (ready.state != "ready" or ready.attestation.install_receipt_sha256 != digest
            or receipt_path.read_bytes() != receipt_bytes):
        raise ValueError("canonical engine readiness receipt differs")
    _, after = verify_selection(selected)
    if after != identity:
        raise ValueError("installer source identity changed")
    print(json.dumps({"schema": "astrid.pod-h3-install.v1", "engine": "vibecomfy-h3",
                      "root": parsed.root, "status": "ready", "receipt_sha256": digest,
                      "receipt_id": result["receipt_id"]}, sort_keys=True))


def main():
    try:
        selected = load_selection(base64.b64decode(sys.argv[1], validate=True))
        evidence, action_args = Path(sys.argv[2]), sys.argv[3:]
        profile, identity = verify_selection(selected)
    except Exception:
        # Never expose paths, connection diagnostics, or ambient credentials.
        sys.exit("H3 selected source identity verification failed")
    with tempfile.TemporaryDirectory(prefix="astrid-source-") as temporary:
        manifest = Path(temporary) / "source-profile.json"
        manifest.write_text(json.dumps(profile.as_dict()))
        os.environ.pop("PYTHONHOME", None)
        os.environ.update(PYTHONPATH=os.pathsep.join((selected["agent_root"], selected["runtime_root"])),
                          PYTHONNOUSERSITE="1", PYTHONSAFEPATH="1", ASTRID_RUNTIME_SOURCE_MANIFEST=str(manifest))
        # Engine installation is local; only actions launch Runtime descendants.
        if action_args[:2] != ["engines", "install"]:
            os.environ["ASTRID_RUNTIME_LAUNCHER"] = str(Path(sys.executable).with_name("astrid-runtime"))
        receipt = {k: selected[k] for k in ("agent_head", "runtime_head", "runtime_lock_sha256")}
        # Canonical identity contains digests/versions, never input or checkout paths.
        receipt.update(schema="astrid.pod-action-source.v1", identity=identity)
        with evidence.open("x", encoding="utf-8") as stream:
            json.dump(receipt, stream, sort_keys=True)
        if action_args[:2] == ["engines", "install"]:
            try:
                install_selected(action_args, selected, identity)
            except Exception:
                sys.exit("H3 selected source installer verification failed")
        else:
            sys.argv = ["astrid", *action_args]
            runpy.run_module("astrid", run_name="__main__")


if __name__ == "__main__":
    main()
