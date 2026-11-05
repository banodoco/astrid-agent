import hashlib
import json
import subprocess
import sys
from copy import deepcopy
from argparse import Namespace
from pathlib import Path

import pytest

from scripts import make_standin_release as generator
from scripts import update_operation_surface

ROOT = Path(__file__).resolve().parents[1]
CANDIDATE = ROOT / "tests" / "fixtures" / "release0" / "served" / "install-manifest.json"


def candidate_manifest():
    candidate = json.loads(CANDIDATE.read_text(encoding="utf-8"))
    candidate["release"] = 1
    return candidate


def test_standin_increments_release_and_preserves_candidate_pins():
    candidate = candidate_manifest()
    original = deepcopy(candidate)

    stand_in = generator.make_standin_release(candidate, marker="test marker")

    assert candidate == original
    assert stand_in["release"] == candidate["release"] + 1
    assert stand_in["qualification_marker"] == "test marker"
    for field in ("agent", "runtime", "tools"):
        assert stand_in[field] == candidate[field]
    assert stand_in["vibecomfy"] == {"sha": generator.VIBECOMFY_ENGINE_REVISION}


def test_standin_has_installer_canonical_bytes_hash_and_preflight_acceptance(tmp_path):
    candidate = candidate_manifest()
    stand_in = generator.make_standin_release(candidate)
    raw = generator.canonical_manifest_bytes(stand_in)
    path = tmp_path / "stand-in.json"
    path.write_bytes(raw)

    digest = hashlib.sha256(raw).hexdigest()
    assert raw.endswith(b"\n")
    assert json.loads(raw) == stand_in
    assert update_operation_surface._manifest_preservation_preflight(
        Namespace(stand_in_manifest=path), stand_in
    ) == digest
    # Same release monotonicity condition enforced by the App release-check gate.
    assert stand_in["release"] > candidate["release"]


def test_cli_writes_manifest_and_sha256sums(tmp_path):
    candidate_path = tmp_path / "candidate.json"
    candidate_path.write_text(json.dumps(candidate_manifest()), encoding="utf-8")
    output = tmp_path / "generated" / "install-manifest.json"
    sums = output.with_name("SHA256SUMS")
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "make_standin_release.py"),
            str(candidate_path),
            "--output",
            str(output),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    raw = output.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    assert result.stdout.strip() == digest
    assert sums.read_text(encoding="utf-8") == f"{digest}  {output.name}\n"
    assert json.loads(raw)["qualification_marker"] == generator.DEFAULT_MARKER


def test_rejects_invalid_release_or_marker():
    candidate = candidate_manifest()
    with pytest.raises(ValueError, match="release"):
        generator.make_standin_release({**candidate, "release": True})
    with pytest.raises(ValueError, match="marker"):
        generator.make_standin_release(candidate, marker=" ")
