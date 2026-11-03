"""Single-row dispatch and explicitly non-proof, job-local manifest preparation."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

ARTIFACTS = ("public/install", "public/install.sh", "public/install.ps1")


def require_diagnostic_branch(ref: str) -> None:
    # These are the tokens excluded by the existing M-CI handoff gate.
    if not ref.startswith("refs/heads/") or not any(
        token in ref.upper() for token in ("LOOKAHEAD", "KEEPGOING")
    ):
        raise ValueError("diag requires a LOOKAHEAD or KEEPGOING branch excluded by the handoff gate")


def select_rows(matrix: list[dict], rows: str, *, direct_only=False,
                backend_only=False, other_suites=False) -> list[dict]:
    if rows == "all":
        return [row for row in matrix if not direct_only or row["mode"] == "direct"
                if not backend_only or row["mode"] == "pinokio-backend"]
    if other_suites:
        raise ValueError("rows cannot select a Release-1 row while optional suites are enabled")
    selected = [row for row in matrix if row["job_id"] == rows]
    if len(selected) != 1:
        raise ValueError("rows must be all or exactly one Release-1 job_id (OS and channel)")
    if (direct_only and selected[0]["mode"] != "direct"
            or backend_only and selected[0]["mode"] != "pinokio-backend"):
        raise ValueError("rows conflicts with the requested channel filter")
    return selected


def dispatch_candidate(candidate: dict, *, proof_mode: str, agent_ref: str,
                       runtime_ref: str, workflow_ref: str, rows: str) -> dict:
    if proof_mode not in {"proof", "diag"}:
        raise ValueError("proof_mode must explicitly be proof or diag")
    if not isinstance(candidate, dict):
        raise ValueError("candidate must be an object")
    candidate = dict(candidate)
    if proof_mode == "proof":
        if agent_ref or runtime_ref or candidate.get("proof_mode", "proof") != "proof":
            raise ValueError("Agent/Runtime overrides and diagnostic candidates require proof_mode diag")
        if candidate.get("diagnostic_manifest"):
            raise ValueError("a job-built diagnostic manifest cannot be proof")
        return candidate
    require_diagnostic_branch(workflow_ref)
    if rows == "all":
        raise ValueError("diag requires exactly one explicit row")
    for name, value in (("agent_ref", agent_ref), ("runtime_ref", runtime_ref)):
        if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}", value):
            raise ValueError(f"diag {name} must be a full immutable commit SHA")
    candidate.update(agent=agent_ref, runtime=runtime_ref, proof_mode="diag")
    return candidate


def checkout_sha(path: Path) -> str:
    return subprocess.check_output(["git", "-C", str(path), "rev-parse", "HEAD"], text=True).strip()


def runtime_schema_digest(runtime: Path) -> str:
    # Same length-prefixed digest as App scripts/installer/pin.mjs, using the
    # immutable checkout rather than network reads or the Agent's old lock pin.
    contract_root = (runtime / "contract").resolve(strict=True)
    contract = json.loads((contract_root / "manifest.json").read_text(encoding="utf-8"))
    if not isinstance(contract.get("schemas"), list):
        raise ValueError("Runtime contract needs a schemas list")
    digest = hashlib.sha256()
    for name in [contract["openapi"], *contract["schemas"]]:
        if not isinstance(name, str) or not name or "\\" in name:
            raise ValueError("invalid Runtime contract path")
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Runtime contract path escapes contract directory")
        path = contract_root / relative
        if path.is_symlink() or not path.resolve(strict=True).is_relative_to(contract_root):
            raise ValueError("Runtime contract path escapes contract directory")
        encoded = ("contract/" + relative.as_posix()).encode("utf-8")
        data = path.read_bytes()
        digest.update(len(encoded).to_bytes(4, "big"))
        digest.update(encoded)
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return "sha256:" + digest.hexdigest()


def prepare_diagnostic(candidate: dict, app: Path, agent: Path, runtime: Path, *,
                       proof_mode: str, workflow_ref: str) -> tuple[dict, dict]:
    if proof_mode != "diag" or candidate.get("proof_mode") != "diag":
        raise ValueError("in-job manifest creation requires explicit proof_mode diag")
    require_diagnostic_branch(workflow_ref)
    for name, path in (("app", app), ("agent", agent), ("runtime", runtime)):
        if not re.fullmatch(r"[0-9a-f]{40}", str(candidate.get(name, ""))):
            raise ValueError(f"candidate {name} must be an immutable SHA")
        if checkout_sha(path) != candidate[name]:
            raise ValueError(f"requested {name} ref differs from its checkout")
    manifest_path = app / "public/install-manifest.json"
    original = manifest_path.read_bytes()
    manifest = json.loads(original)
    if manifest.get("release") != 1:
        raise ValueError("diagnostic preparation requires an App Release-1 manifest")
    for name in ("agent", "runtime"):
        if not isinstance(manifest.get(name), dict):
            raise ValueError(f"App manifest needs a {name} object")
        manifest[name] = {**manifest[name], "sha": candidate[name]}
    manifest["runtime"]["schema_digest"] = runtime_schema_digest(runtime)
    manifest["proof_mode"] = "diag"
    encoded = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")
    receipt = {
        "proof_mode": "diag", "classification": "NON-PROOF",
        "qualification": {"eligible": False},
        "original_manifest_sha256": hashlib.sha256(original).hexdigest(),
        "manifest_sha256": hashlib.sha256(encoded).hexdigest(),
        "requested_refs": {name: candidate[name] for name in ("app", "agent", "runtime", "pinokio")},
        "runtime_schema_digest": manifest["runtime"]["schema_digest"],
    }
    # Hash shipped bootstrap artifacts; no source or bootstrap code is rewritten.
    candidate = {**candidate, "manifest_sha256": receipt["manifest_sha256"],
                 "artifacts": {name: hashlib.sha256((app / name).read_bytes()).hexdigest()
                               for name in ARTIFACTS}, "diagnostic_manifest": receipt}
    manifest_path.write_bytes(encoded)
    return candidate, receipt


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", required=True, type=Path)
    parser.add_argument("--app-checkout", required=True, type=Path)
    parser.add_argument("--agent-checkout", required=True, type=Path)
    parser.add_argument("--runtime-checkout", required=True, type=Path)
    parser.add_argument("--proof-mode", required=True, choices=("diag",))
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    candidate, receipt = prepare_diagnostic(
        json.loads(args.candidate.read_text(encoding="utf-8")), args.app_checkout,
        args.agent_checkout, args.runtime_checkout, proof_mode=args.proof_mode,
        workflow_ref=os.environ.get("GITHUB_REF", ""))
    args.candidate.write_text(json.dumps(candidate, indent=2) + "\n", encoding="utf-8")
    args.output.mkdir(parents=True, exist_ok=False)
    (args.output / "diagnostic-manifest-receipt.json").write_text(
        json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    (args.output / "candidate.json").write_bytes(args.candidate.read_bytes())
    (args.output / "install-manifest.json").write_bytes(
        (args.app_checkout / "public/install-manifest.json").read_bytes())
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as summary:
            summary.write("\nC6 diagnostic manifest: NON-PROOF (`proof_mode: diag`).\n\n")
            summary.write(f"Manifest SHA-256: `{receipt['manifest_sha256']}`\n\n")
            summary.write(f"Requested refs: `{json.dumps(receipt['requested_refs'], sort_keys=True)}`\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
