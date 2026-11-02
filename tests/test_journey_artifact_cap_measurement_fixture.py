"""Focused coverage for the dispatch-only R-01 artifact-cap fixture."""
from __future__ import annotations

import hashlib
import json

from scripts import journey_artifact_boundary as boundary
from scripts import journey_artifact_cap_measurement_fixture as fixture


def test_fixture_creates_two_truthful_non_qualifying_snapshots_and_boundary_retains_diagnostics(tmp_path):
    root = tmp_path / "evidence-upload"
    fixture.build(root, bootstrap_bytes=100_000)

    assert fixture.DEFAULT_BOOTSTRAP_BYTES * len(fixture.PHASES) > boundary.MAX_ARTIFACT_BYTES
    diagnostics = {}
    originals = {}
    for phase in fixture.PHASES:
        folder = root / phase
        bootstrap = folder / "release0-bootstrap.log"
        receipt = json.loads((folder / "journey-result.json").read_text())
        assert bootstrap.stat().st_size == 100_000
        assert receipt["evidence_class"] == "synthetic-artifact-cap-measurement"
        assert receipt["qualification"]["eligible"] is False
        assert receipt["failure_diagnostics"][phase]["release0_bootstrap"]["sha256"] == (
            hashlib.sha256(bootstrap.read_bytes()).hexdigest())
        originals[phase] = hashlib.sha256(bootstrap.read_bytes()).hexdigest()
        diagnostics[phase] = {
            name: (folder / name).read_bytes()
            for name in ("install.log", "manager-state-before-cleanup.json",
                         "runtime-service-service-seed-0001-stderr.log")
        }

    assert len(set(originals.values())) == 1

    estimate = boundary.prepare(root, max_bytes=100_000)

    assert estimate < 100_000
    for phase in fixture.PHASES:
        folder = root / phase
        bootstrap = folder / "release0-bootstrap.log"
        receipt = json.loads((folder / "journey-result.json").read_text())
        entry = receipt["failure_diagnostics"][phase]["release0_bootstrap"]
        metadata = json.loads((folder / "release0-bootstrap.log.metadata.json").read_text())
        assert 0 < bootstrap.stat().st_size < 100_000
        assert entry["status"] == "truncated"
        assert entry["truncated"] is True
        assert entry["original_sha256"] == originals[phase]
        assert entry["sha256"] == hashlib.sha256(bootstrap.read_bytes()).hexdigest()
        assert metadata["original_sha256"] == originals[phase]
        assert metadata["retained_sha256"] == entry["sha256"]
        assert receipt["qualification"]["eligible"] is False
        for name, content in diagnostics[phase].items():
            assert (folder / name).read_bytes() == content
