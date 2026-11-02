"""Exercise the decision used by the hosted lifecycle artifact upload."""
import json
from pathlib import Path
import re

import pytest

from scripts import journey_artifact_boundary as boundary

REPO = Path(__file__).resolve().parents[1]
WORKFLOW = REPO / ".github" / "workflows" / "journey.yml"


def test_journey_upload_is_allowlisted_staged_evidence_under_size_limit():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    guard = workflow.split("      - name: Validate the named lifecycle evidence artifact boundary", 1)[1].split(
        "      - name: Retain only the named lifecycle evidence", 1)[0]
    upload = workflow.split("      - name: Retain only the named lifecycle evidence", 1)[1].split("  release2:", 1)[0]
    assert "id: evidence_boundary" in guard and "if: always()" in guard
    assert "agent/scripts/journey_artifact_boundary.py $evidenceRoot" in guard
    assert "if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }" in guard
    assert "if: always() && steps.evidence_boundary.outcome == 'success'" in upload
    assert re.search(r"path: .*?/evidence-upload", upload)
    assert "compression-level: 0" in upload
    assert boundary.MAX_ARTIFACT_BYTES == 49_000_000


def stage(root, phase, sizes):
    folder = root / phase
    folder.mkdir(parents=True)
    (folder / "journey-result.json").write_text('{"status":"fail"}')
    (folder / "install.log").write_bytes(b"named evidence")
    streams = {}
    for kind, size in sizes.items():
        path = folder / f"runtime-service-service-seed-0001-{kind}.log"
        with path.open("wb") as output:
            output.truncate(size)
        streams[kind] = {"total_bytes": size, "retained_bytes": size, "truncated": False}
    (folder / "runtime-service-service-seed-0001-exit.log").write_text(json.dumps({"streams": streams}))


def inventory(root):
    return {p.relative_to(root).as_posix(): p.stat().st_size for p in root.rglob("*") if p.is_file()}


def test_single_over_limit_stream_preserves_receipt_and_named_evidence(tmp_path):
    root = tmp_path / "evidence-upload"
    stage(root, "before-cleanup", {"stderr": 50_000_000})
    estimate = boundary.prepare(root)
    phase = root / "before-cleanup"
    stream = phase / "runtime-service-service-seed-0001-stderr.log"
    meta = json.loads(stream.with_name(stream.name + ".metadata.log").read_text())
    assert int(meta["original_bytes"]) == 50_000_000
    assert int(meta["retained_bytes"]) == stream.stat().st_size < 50_000_000
    assert meta["truncated"] == "1"
    assert (phase / "journey-result.json").is_file() and (phase / "install.log").read_bytes() == b"named evidence"
    assert estimate == sum(size + 1024 for size in inventory(root).values()) < 49_000_000


def test_combined_streams_and_both_cleanup_copies_are_deterministic(tmp_path):
    roots = [tmp_path / "a", tmp_path / "b"]
    for root in roots:
        for phase in ("before-cleanup", "after-cleanup"):
            stage(root, phase, {"stdout": 13_000_000, "stderr": 13_000_000})
    estimates = [boundary.prepare(root) for root in roots]
    assert estimates[0] == estimates[1] == 48_999_999
    assert inventory(roots[0]) == inventory(roots[1])
    for phase in ("before-cleanup", "after-cleanup"):
        folder = roots[0] / phase
        assert (folder / "journey-result.json").is_file() and (folder / "install.log").is_file()
        for kind in ("stdout", "stderr"):
            stream = folder / f"runtime-service-service-seed-0001-{kind}.log"
            meta = json.loads(stream.with_name(stream.name + ".metadata.log").read_text())
            assert stream.is_file() and 0 < stream.stat().st_size < 13_000_000
            assert meta["truncated"] == "1" and int(meta["retained_bytes"]) == stream.stat().st_size


def test_minimum_set_refuses_without_discarding_evidence(tmp_path, capsys, monkeypatch):
    root = tmp_path / "evidence-upload"
    stage(root, "before-cleanup", {"stdout": 200})
    with (root / "before-cleanup" / "install.log").open("wb") as output:
        output.truncate(49_000_000)
    before = inventory(root)
    with pytest.raises(ValueError, match="Minimum named journey evidence"):
        boundary.prepare(root)
    assert inventory(root) == before
    monkeypatch.setattr(boundary.sys, "argv", ["boundary", str(root)])
    assert boundary.main() == 1
    assert "Minimum named journey evidence" in capsys.readouterr().err
    assert inventory(root) == before


def test_boundary_refuses_unexpected_names_and_missing_receipt(tmp_path):
    root = tmp_path / "evidence-upload"
    stage(root, "before-cleanup", {"stdout": 10})
    phase = root / "before-cleanup"
    unexpected = phase / "secret.txt"
    unexpected.write_text("not allowlisted")
    with pytest.raises(ValueError, match="Unexpected staged evidence filename"):
        boundary.prepare(root)
    unexpected.unlink()
    (phase / "journey-result.json").unlink()
    with pytest.raises(ValueError, match="No staged journey-result.json"):
        boundary.prepare(root)
