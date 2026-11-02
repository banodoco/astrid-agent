"""Build an isolated, non-qualifying fixture for the artifact-cap boundary.

This never invokes an installer or a journey.  It writes the two named cleanup
snapshots which a verbose Release-0 bootstrap failure would leave behind, so a
hosted Actions upload can measure the real artifact ZIP without representing a
direct-journey result.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path


DEFAULT_BOOTSTRAP_BYTES = 25_000_000
PHASES = ("before-cleanup", "after-cleanup")
FIXTURE_ID = "r01-artifact-cap-measurement-v1"


def _write_pattern(path: Path, *, size: int) -> str:
    """Write deterministic non-sparse bytes and return their SHA-256."""
    digest = hashlib.sha256()
    # Cleanup copies of a single captured bootstrap stream must be byte-for-byte
    # identical.  Phase identity lives in their receipt paths, not in the bytes.
    pattern = f"{FIXTURE_ID}:duplicated-oversized-bootstrap\n".encode("ascii")
    remaining = size
    with path.open("wb") as stream:
        while remaining:
            block = pattern[:min(len(pattern), remaining)]
            stream.write(block)
            digest.update(block)
            remaining -= len(block)
    return digest.hexdigest()


def _write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def build(root: Path, *, bootstrap_bytes: int = DEFAULT_BOOTSTRAP_BYTES) -> None:
    """Create two oversized snapshots plus small named diagnostics.

    The receipts deliberately state ``qualification.eligible=false``.  Their
    only purpose is to let the artifact-boundary code preserve truthful source
    and retained-byte fields during a hosted upload measurement.
    """
    if bootstrap_bytes <= 0:
        raise ValueError("bootstrap_bytes must be positive")
    if root.exists():
        raise ValueError(f"measurement evidence root already exists: {root}")
    root.mkdir(parents=True)

    for phase in PHASES:
        folder = root / phase
        folder.mkdir()
        bootstrap = folder / "release0-bootstrap.log"
        bootstrap_sha256 = _write_pattern(bootstrap, size=bootstrap_bytes)
        (folder / "install.log").write_text(
            f"{FIXTURE_ID} {phase} named diagnostic retained for upload measurement\n",
            encoding="utf-8",
        )
        _write_json(folder / "manager-state-before-cleanup.json", {
            "fixture": FIXTURE_ID,
            "phase": phase,
            "purpose": "hosted artifact-cap measurement only",
        })
        (folder / "runtime-service-service-seed-0001-stderr.log").write_text(
            f"{FIXTURE_ID} {phase} service diagnostic\n", encoding="utf-8")
        _write_json(folder / "journey-result.json", {
            "schema_version": 1,
            "status": "fixture",
            "evidence_class": "synthetic-artifact-cap-measurement",
            "qualification": {
                "eligible": False,
                "reason": "isolated artifact-cap fixture; not a direct journey",
            },
            "failure_diagnostics": {
                phase: {
                    "release0_bootstrap": {
                        "status": "captured",
                        "path": f"{phase}/release0-bootstrap.log",
                        "exit_code": 37,
                        "sha256": bootstrap_sha256,
                    },
                },
            },
        })


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print("usage: journey_artifact_cap_measurement_fixture.py EVIDENCE_ROOT", file=sys.stderr)
        return 2
    try:
        build(Path(argv[0]))
    except Exception as exc:
        print(f"Artifact-cap measurement fixture failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(
        f"Created {len(PHASES)} non-qualifying bootstrap copies of "
        f"{DEFAULT_BOOTSTRAP_BYTES} bytes under {argv[0]}."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
