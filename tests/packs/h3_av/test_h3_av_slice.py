from __future__ import annotations

import json
from pathlib import Path

import pytest

from astrid.packs.h3_av.shared.compose import CompositionError, compose_candidate
from astrid.packs.h3_av.actions.prepare.masks import (
    MaskScheduleError,
    build_mask_schedule,
    interval_difference,
    merge_intervals,
)
from astrid.packs.h3_av.actions.prepare.prepare import prepare_request
from astrid.packs.h3_av.shared.request import normalize_request
from astrid.packs.h3_av.actions.verify.verify import VerificationError, verify_candidate


def request(**overrides):
    value = {
        "version": 1,
        "operation": "edit",
        "source": {"asset": "source.mp4", "range": [0, 8]},
        "output": {"duration": 8},
        "content": {"prompt": "End state: the seated speaker delivers the replacement line."},
        "changes": {"video": [], "audio": []},
        "references": [],
        "overrides": {},
    }
    value.update(overrides)
    return normalize_request(value)


def test_intervals_merge_and_subtract_half_open_ranges() -> None:
    assert merge_intervals([[0, 1], [1, 2], [4, 5], [3, 4]]) == [[0, 2], [3, 5]]
    assert interval_difference([0, 10], [[2, 4], [6, 8]]) == [[0, 2], [4, 6], [8, 10]]


def test_mask_schedule_preserves_unmentioned_source_domain_and_marks_subjects() -> None:
    prepared = request(
        changes={
            "video": [
                {
                    "during": [2, 4],
                    "area": {"subjects": ["speaker.mouth"]},
                    "action": "generate",
                }
            ],
            "audio": [],
        }
    )
    schedule = build_mask_schedule(prepared)
    assert schedule["status"] == "requires_resolution"
    assert schedule["requires_resolution"] == ["changes.video[0].area"]
    assert schedule["video"]["generated_intervals"] == [[2.0, 4.0]]
    assert schedule["audio"]["protected_intervals"] == [[0.0, 8.0]]
    assert schedule["video"]["protected_intervals"] == [[0.0, 2.0], [4.0, 8.0]]


def test_full_frame_area_and_duration_bounds_are_deterministic() -> None:
    prepared = request(
        changes={
            "video": [{"during": [0, 8], "area": {"full_frame": True}, "action": "generate"}],
            "audio": [],
        }
    )
    schedule = build_mask_schedule(prepared)
    assert schedule["status"] == "ready"
    assert schedule["video"]["changes"][0]["area"]["resolution"] == "exact"
    with pytest.raises(MaskScheduleError, match="output.duration"):
        build_mask_schedule(
            request(
                source={"asset": "source.mp4"},
                output={},
                changes={"video": [], "audio": []},
            )
        )


def test_prepare_resolves_and_hashes_declared_assets(tmp_path: Path) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source-bytes")
    prepared = prepare_request(request(), asset_map={"source.mp4": str(source)})
    assert prepared["status"] == "prepared"
    assert prepared["unresolved_assets"] == []
    assert prepared["assets"][0]["sha256"]
    assert prepared["runtime_submission"] == "eligible"


def test_prepare_does_not_claim_unmapped_managed_assets_are_local() -> None:
    prepared = prepare_request(request())
    assert prepared["assets"] == [{"asset": "source.mp4", "kind": "managed_asset", "status": "unresolved"}]
    assert prepared["status"] == "prepared"


def test_wholly_protected_candidate_requires_exact_source_match(tmp_path: Path) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"same")
    generated = tmp_path / "generated.bin"
    generated.write_bytes(b"same")
    prepared = prepare_request(request(), asset_map={"source.mp4": str(source)})
    composition = compose_candidate(preparation=prepared, generated=generated, source=source, out_dir=tmp_path / "out")
    report = verify_candidate(preparation=prepared, composition=composition, source=source)
    assert report["status"] == "verified"
    assert report["preservation"]["status"] == "exact_whole_file_match"


def test_wholly_protected_candidate_rejects_changed_bytes(tmp_path: Path) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"source")
    generated = tmp_path / "generated.bin"
    generated.write_bytes(b"changed")
    prepared = prepare_request(request(), asset_map={"source.mp4": str(source)})
    composition = compose_candidate(preparation=prepared, generated=generated, source=source, out_dir=tmp_path / "out")
    # The composer correctly selects source bytes for wholly protected media.
    # A changed candidate, even with self-consistent declared hash, must fail.
    import hashlib
    candidate = Path(composition["candidate"]["path"])
    candidate.write_bytes(b"changed")
    composition["candidate"]["sha256"] = hashlib.sha256(candidate.read_bytes()).hexdigest()
    composition["candidate"]["size"] = candidate.stat().st_size
    with pytest.raises(VerificationError, match="byte-identical"):
        verify_candidate(preparation=prepared, composition=composition, source=source)


def test_partial_preservation_requires_complete_permission_evidence(tmp_path: Path) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"source")
    generated = tmp_path / "generated.bin"
    generated.write_bytes(b"changed")
    prepared = prepare_request(
        request(
            changes={
                "video": [{"during": [2, 4], "area": {"full_frame": True}, "action": "generate"}],
                "audio": [],
            }
        ),
        asset_map={"source.mp4": str(source)},
    )
    composition = compose_candidate(preparation=prepared, generated=generated, source=source, out_dir=tmp_path / "out")
    with pytest.raises(VerificationError, match="decodable media|protected_samples"):
        verify_candidate(preparation=prepared, composition=composition, source=source)


def test_compose_rejects_unresolved_schedule(tmp_path: Path) -> None:
    generated = tmp_path / "generated.bin"
    generated.write_bytes(b"candidate")
    prepared = prepare_request(
        request(
            changes={
                "video": [{"during": [0, 1], "area": {"subjects": ["speaker"]}, "action": "generate"}],
                "audio": [],
            }
        )
    )
    with pytest.raises(CompositionError, match="requires resolution"):
        compose_candidate(preparation=prepared, generated=generated, out_dir=tmp_path / "out")


def test_manifest_is_json_serializable_and_lifecycle_is_explicit(tmp_path: Path) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"same")
    generated = tmp_path / "generated.bin"
    generated.write_bytes(b"same")
    prepared = prepare_request(request(), asset_map={"source.mp4": str(source)})
    composed = compose_candidate(preparation=prepared, generated=generated, source=source, out_dir=tmp_path / "out")
    report = verify_candidate(preparation=prepared, composition=composed, source=source)
    json.dumps({"preparation": prepared, "composition": composed, "verification": report})
    assert report["lifecycle"] == {"prepared": True, "composed": True, "verified": True}
