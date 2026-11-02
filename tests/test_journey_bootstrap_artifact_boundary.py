"""Exercise capped retention for duplicated Release-0 bootstrap evidence."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts import journey_artifact_boundary as boundary


def _stage(root: Path, phase: str, bootstrap_bytes: int, *, install_bytes: int = 14) -> None:
    folder = root / phase
    folder.mkdir(parents=True)
    log = folder / "release0-bootstrap.log"
    with log.open("wb") as stream:
        stream.truncate(bootstrap_bytes)
    digest = hashlib.sha256(log.read_bytes()).hexdigest()
    receipt = {
        "status": "fail",
        "failure_diagnostics": {
            phase: {
                "release0_bootstrap": {
                    "status": "captured",
                    "path": f"{phase}/release0-bootstrap.log",
                    "exit_code": 37,
                    "sha256": digest,
                }
            }
        },
    }
    (folder / "journey-result.json").write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    with (folder / "install.log").open("wb") as stream:
        stream.truncate(install_bytes)


def _inventory(root: Path) -> dict[str, int]:
    return {path.relative_to(root).as_posix(): path.stat().st_size
            for path in root.rglob("*") if path.is_file()}


def _receipt(root: Path, phase: str) -> dict:
    return json.loads((root / phase / "journey-result.json").read_text())


def test_duplicated_bootstrap_logs_keep_receipts_and_named_diagnostics_under_cap(tmp_path):
    root = tmp_path / "evidence-upload"
    # Both cleanup snapshots independently carry the same verbose bootstrap log.
    for phase in ("before-cleanup", "after-cleanup"):
        _stage(root, phase, 25_000_000, install_bytes=777)

    estimate = boundary.prepare(root)

    assert estimate == sum(size + boundary.ENTRY_OVERHEAD for size in _inventory(root).values())
    assert estimate < boundary.MAX_ARTIFACT_BYTES
    retained_sizes = []
    for phase in ("before-cleanup", "after-cleanup"):
        folder = root / phase
        log = folder / "release0-bootstrap.log"
        metadata = json.loads((folder / "release0-bootstrap.log.metadata.json").read_text())
        entry = _receipt(root, phase)["failure_diagnostics"][phase]["release0_bootstrap"]
        assert (folder / "journey-result.json").is_file()
        assert (folder / "install.log").stat().st_size == 777
        assert 0 < log.stat().st_size < 25_000_000
        assert entry["status"] == "truncated"
        assert entry["truncated"] is True
        assert int(entry["captured_bytes"]) == 25_000_000
        assert int(entry["retained_bytes"]) == log.stat().st_size
        assert entry["sha256"] == hashlib.sha256(log.read_bytes()).hexdigest()
        assert entry["original_sha256"] == metadata["original_sha256"]
        assert metadata["truncated"] is True
        assert int(metadata["captured_bytes"]) == 25_000_000
        assert int(metadata["retained_bytes"]) == log.stat().st_size
        retained_sizes.append(log.stat().st_size)
    # Equal-share allocation can give one remaining byte to the later sorted
    # phase; neither duplicate is preferentially discarded.
    assert max(retained_sizes) - min(retained_sizes) <= 1


def test_exact_boundary_is_retained_without_false_truncation(tmp_path):
    root = tmp_path / "evidence-upload"
    _stage(root, "before-cleanup", 1024, install_bytes=31)
    exact = sum(size + boundary.ENTRY_OVERHEAD for size in _inventory(root).values())

    assert boundary.prepare(root, max_bytes=exact + 1) == exact
    entry = _receipt(root, "before-cleanup")["failure_diagnostics"]["before-cleanup"]["release0_bootstrap"]
    assert entry["status"] == "captured"
    assert entry["sha256"] == hashlib.sha256(
        (root / "before-cleanup" / "release0-bootstrap.log").read_bytes()).hexdigest()
    assert not (root / "before-cleanup" / "release0-bootstrap.log.metadata.json").exists()


def test_exact_cap_truncates_bootstrap_and_stays_strictly_below_it(tmp_path):
    root = tmp_path / "evidence-upload"
    _stage(root, "before-cleanup", 4096, install_bytes=31)
    exact_cap = sum(size + boundary.ENTRY_OVERHEAD for size in _inventory(root).values())

    estimate = boundary.prepare(root, max_bytes=exact_cap)

    entry = _receipt(root, "before-cleanup")["failure_diagnostics"]["before-cleanup"]["release0_bootstrap"]
    assert estimate == sum(size + boundary.ENTRY_OVERHEAD for size in _inventory(root).values())
    assert estimate < exact_cap
    assert entry["status"] == "truncated"
    assert entry["truncated"] is True
    assert (root / "before-cleanup" / "release0-bootstrap.log").stat().st_size < 4096


def test_minimum_named_evidence_refuses_without_mutating_when_bootstrap_cannot_fit(tmp_path, capsys, monkeypatch):
    root = tmp_path / "evidence-upload"
    _stage(root, "before-cleanup", 100)
    # The compact receipt and useful named diagnostic alone are over the cap.
    with (root / "before-cleanup" / "install.log").open("wb") as stream:
        stream.truncate(boundary.MAX_ARTIFACT_BYTES)
    before = _inventory(root)

    with pytest.raises(ValueError, match="Minimum named journey evidence"):
        boundary.prepare(root)
    assert _inventory(root) == before
    monkeypatch.setattr(boundary.sys, "argv", ["boundary", str(root)])
    assert boundary.main() == 1
    assert "Minimum named journey evidence" in capsys.readouterr().err
    assert _inventory(root) == before


def test_missing_or_mismatched_bootstrap_receipt_refuses_without_truncating(tmp_path):
    root = tmp_path / "evidence-upload"
    _stage(root, "before-cleanup", boundary.MAX_ARTIFACT_BYTES)
    receipt = _receipt(root, "before-cleanup")
    receipt["failure_diagnostics"]["before-cleanup"]["release0_bootstrap"]["sha256"] = "0" * 64
    (root / "before-cleanup" / "journey-result.json").write_text(json.dumps(receipt))
    before = _inventory(root)

    with pytest.raises(ValueError, match="hash does not match"):
        boundary.prepare(root)
    assert _inventory(root) == before


def test_existing_allowlist_and_receipt_requirements_still_fail_closed(tmp_path):
    root = tmp_path / "evidence-upload"
    _stage(root, "before-cleanup", 100)
    unexpected = root / "before-cleanup" / "not-allowed.txt"
    unexpected.write_text("not lifecycle evidence")

    with pytest.raises(ValueError, match="Unexpected staged evidence filename"):
        boundary.prepare(root)
    unexpected.unlink()
    (root / "before-cleanup" / "journey-result.json").unlink()
    with pytest.raises(ValueError, match="No staged journey-result.json"):
        boundary.prepare(root)
