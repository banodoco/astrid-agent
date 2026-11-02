#!/usr/bin/env python3
"""Runner-local R-24 invocation bundle. This is not an admission receipt."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sys

if __package__:
    from . import ci_journey_provider as provider
else:
    import ci_journey_provider as provider


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def session_from_env(env: dict[str, str]) -> dict[str, str]:
    keys = ("GITHUB_REPOSITORY", "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT", "GITHUB_JOB",
            "GITHUB_WORKFLOW_REF", "GITHUB_WORKFLOW_SHA", "RUNNER_NAME", "RUNNER_OS")
    if env.get("GITHUB_ACTIONS") != "true" or env.get("RUNNER_OS") != "Linux" or sys.platform != "linux":
        raise ValueError("Linux Actions runner required")
    if any(not env.get(k) or len(env[k]) > 512 or any(ord(c) < 32 for c in env[k]) for k in keys):
        raise ValueError("incomplete runner identity")
    if (not re.fullmatch(r"[1-9][0-9]*", env["GITHUB_RUN_ID"]) or
            not re.fullmatch(r"[1-9][0-9]*", env["GITHUB_RUN_ATTEMPT"]) or
            not re.fullmatch(r"[0-9a-f]{40}", env["GITHUB_WORKFLOW_SHA"]) or
            not re.fullmatch(r"[^/\s]+/[^/\s]+", env["GITHUB_REPOSITORY"])):
        raise ValueError("invalid runner identity")
    return {k: env[k] for k in keys}


def invoke(config_raw: bytes, producer_root: str, output: Path, evidence_dir: Path | None,
           env: dict[str, str]) -> dict:
    session = session_from_env(env)
    # Parse with the pinned contract's duplicate-key and non-finite checks.
    config = provider._decode(config_raw)
    config_bytes = provider._canonical(config)
    run_id = secrets.token_hex(16)
    request = provider.ProviderRequest.parse(provider._canonical({
        "schema": provider.REQUEST_SCHEMA, "candidate": provider._CANDIDATE,
        "provider": {"ref": provider._SUCCESS_PRODUCER_REF, "sources": provider._SUCCESS_PRODUCER_FILES},
        "stop": provider._STOP, "config_sha256": digest(config_bytes), "run_id": run_id,
    }))
    # An exclusive fresh bundle prevents accidental reuse of a previous invocation.
    output.mkdir(mode=0o700, parents=False, exist_ok=False)
    manifest = {"schema": "astrid.r24.runner-bundle.v1", "qualification": "not-qualified",
                "custody": "unauthenticated-local-files", "session_authentication": "unverified-environment",
                "session": session, "session_nonce": secrets.token_hex(16), "run_id": run_id,
                "request_sha256": request.sha256, "config_sha256": digest(config_bytes),
                "producer_ref": provider._SUCCESS_PRODUCER_REF, "invoked": False,
                "exit_code": None, "timed_out": False, "status": "rejected", "artifacts": {}}

    def save(name: str, content: bytes) -> None:
        (output / name).write_bytes(content)
        manifest["artifacts"][name] = digest(content)

    def capture(stdout: bytes, stderr: bytes, code: int | None, timed_out: bool) -> None:
        manifest.update(invoked=True, exit_code=code, timed_out=timed_out)
        save("stdout.bin", stdout)
        save("stderr.bin", stderr)

    try:
        save("request.json", request.canonical)
        save("config.json", config_bytes)
        result = provider.invoke_linux_provider(request, config_bytes, producer_root, capture=capture)
        # The API returns only digests, not evidence bytes. Require all bytes and
        # match them before accepting even a structural result; these files are
        # still caller-supplied and have no authenticated custody.
        if evidence_dir is None or evidence_dir.is_symlink() or not evidence_dir.is_dir():
            raise ValueError("evidence bytes unavailable")
        for obs in result.result.observations:
            path = evidence_dir / (obs.kind + ".bin")
            if path.is_symlink() or not path.is_file():
                raise ValueError("evidence missing")
            data = path.read_bytes()
            if not data or digest(data) != obs.evidence_sha256:
                raise ValueError("evidence digest mismatch")
            save(obs.kind + ".bin", data)
        manifest["result_sha256"] = manifest["artifacts"]["stdout.bin"]
        manifest["status"] = "structural-result-only"
    except (provider.ProviderContractError, ValueError, OSError) as exc:
        manifest["error"] = str(exc)
    finally:
        (output / "bundle.json").write_bytes(provider._canonical(manifest) + b"\n")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--producer-root", required=True)
    parser.add_argument("--output", required=True, type=Path, help="new bundle directory outside the checkouts")
    parser.add_argument("--evidence-dir", type=Path, help="optional directory with busy.bin, unknown.bin, restoration.bin")
    args = parser.parse_args()
    try:
        result = invoke(args.config.read_bytes(), args.producer_root, args.output, args.evidence_dir, dict(os.environ))
    except (ValueError, OSError, provider.ProviderContractError) as exc:
        print(f"R-24 hook rejected: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"status": result["status"], "run_id": result["run_id"], "bundle": str(args.output)}))
    return 0 if result["status"] == "structural-result-only" else 1


if __name__ == "__main__":
    sys.exit(main())
