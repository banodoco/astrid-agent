from __future__ import annotations

import hashlib
import json
from fractions import Fraction
from pathlib import Path

import pytest
from PIL import Image

from astrid.packs.h3_av.shared.compile import compile_preparation
from astrid.packs.h3_av.shared.request import H3RequestError, normalize_request
from astrid.packs.h3_av.shared.request_v2 import read_prepared_request
from astrid.packs.h3_av.shared.prepare import write_preparation


TIME_CASES = [
    pytest.param("5/24", Fraction(5, 24), 5, id="rational-string"),
    pytest.param(Fraction(5, 24), Fraction(5, 24), 5, id="fraction-object"),
    pytest.param(0.25, Fraction(1, 4), 6, id="decimal-number"),
]


def _request(mode: str, time: object) -> dict:
    if mode == "i2v":
        return {
            "version": 2,
            "prompt": "Animate the supplied image gently.",
            "duration": time,
            "media": [
                {"id": "first-frame", "asset": "first-frame.png", "role": "reference",
                 "modality": "image"},
            ],
            "settings": {"steps": 1, "seed": 42},
        }

    # These mirror the live v2v and video-edit request shapes. The video-edit
    # rectangle exercises exact window handling through the edit-mask path.
    mask = {"full_frame": True} if mode == "v2v" else {"rectangle": [0, 0, 64, 64]}
    return {
        "version": 2,
        "prompt": "Keep the calm scene and camera steady.",
        "duration": time,
        "settings": {"steps": 1, "seed": 42},
        "media": [
            {
                "id": "source", "asset": "source.mp4", "role": "timeline",
                "modality": "video", "at": {"frame": 0},
                "range": [0, time],
                "edit": [{"stream": "video", "during": [0, time], "mask": mask}],
            },
            {"id": "look", "asset": "look.png", "role": "reference", "modality": "image"},
        ],
    }


def _prepared(raw: dict, tmp_path: Path) -> dict:
    request = normalize_request(raw)
    assets = []
    for media in raw["media"]:
        asset = media["asset"]
        path = tmp_path / f"{asset}.bin"
        if asset.endswith(".png"):
            Image.new("RGB", (128, 128), color=(32, 64, 128)).save(path, format="PNG")
        else:
            path.write_bytes(f"fixture {asset}".encode())
        assets.append({
            "asset": asset,
            "status": "resolved",
            "path": str(path),
            "size": path.stat().st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        })
    return {
        "kind": "h3_av_preparation",
        "status": "prepared",
        "runtime_submission": "eligible",
        "request": request.value,
        "request_digest": request.digest,
        "assets": assets,
    }


@pytest.mark.parametrize("mode", ["i2v", "v2v", "video-edit"])
@pytest.mark.parametrize("raw_time,expected,frames", TIME_CASES)
def test_exact_times_survive_normalization_preparation_and_vibe_compile(
    mode: str, raw_time: object, expected: Fraction, frames: int, tmp_path: Path,
) -> None:
    raw = _request(mode, raw_time)
    request = normalize_request(raw)
    assert isinstance(request.value["duration"], Fraction)
    assert request.value["duration"] == expected
    if mode != "i2v":
        timeline = request.value["media"][0]
        assert isinstance(timeline["range"][1], Fraction)
        assert isinstance(timeline["edit"][0]["during"][1], Fraction)
    spec = request.canonical

    assert isinstance(spec.duration, Fraction)
    assert spec.duration == expected
    if mode == "i2v":
        assert spec.source is None
        assert len(spec.ops) == 1
        assert tuple(type(value) for value in spec.ops[0].during) == (Fraction, Fraction)
        assert spec.ops[0].during == (Fraction(0), expected)
    else:
        assert spec.source is not None
        assert tuple(type(value) for value in spec.source.range) == (Fraction, Fraction)
        assert spec.source.range == (Fraction(0), expected)
        assert len(spec.ops) == 1
        assert tuple(type(value) for value in spec.ops[0].during) == (Fraction, Fraction)
        assert spec.ops[0].during == (Fraction(0), expected)

    prepared = _prepared(raw, tmp_path)
    preparation_path = write_preparation(tmp_path / "preparation.json", prepared)
    stored = json.loads(preparation_path.read_text(encoding="utf-8"))
    restored = read_prepared_request(stored["request"], stored["request_digest"], require_normalized_v2=True)
    restored_spec = restored.canonical
    assert isinstance(restored_spec.duration, Fraction)
    assert restored_spec.duration == expected
    assert restored.digest == request.digest
    if mode != "i2v":
        assert restored_spec.source is not None
        assert restored_spec.source.range == (Fraction(0), expected)
        assert restored_spec.ops[0].during == (Fraction(0), expected)

    compilation = compile_preparation(prepared, out_dir=tmp_path / "compiled")
    assert compilation["capabilities"]["generation_timing"]["requested_frames"] == frames
    if mode != "i2v":
        assert compilation["capabilities"]["edit_binding"]["during_frames"] == [0, frames]

    from vibecomfy.cli_loader import load_workflow_any

    workflow_path = compilation["workflow"]["workflow.py"]["path"]
    workflow = load_workflow_any(workflow_path)
    api = workflow.compile("api", run_inputs=compilation["workflow_inputs"])
    assert isinstance(api, dict) and api

    # The compiler must also consume the persisted exact representation.
    recompiled = compile_preparation(stored, out_dir=tmp_path / "recompiled")
    assert recompiled["compilation_digest"] == compilation["compilation_digest"]


@pytest.mark.parametrize("mode", ["i2v", "v2v", "video-edit"])
def test_one_seventh_is_rejected_with_native_clock_message(mode: str) -> None:
    raw = _request(mode, 0.25)
    if mode == "i2v":
        raw["duration"] = "1/7"
        path = "duration"
    else:
        raw["media"][0]["edit"][0]["during"][1] = "1/7"
        path = r"media\[0\].edit\[0\].during\[1\]"
    with pytest.raises(H3RequestError, match=path + " is not representable on the native clock"):
        normalize_request(raw)


@pytest.mark.parametrize("field", ["duration", "during"])
def test_sub_picosecond_offset_from_frame_boundary_is_not_rounded(field: str) -> None:
    raw = _request("i2v" if field == "duration" else "v2v", "5/24")
    perturbed = Fraction(5, 24) + Fraction(1, 10**12)
    if field == "duration":
        raw["duration"] = perturbed
    else:
        raw["media"][0]["edit"][0]["during"][1] = perturbed
    with pytest.raises(H3RequestError, match="is not representable on the native clock"):
        normalize_request(raw)


def test_exact_request_survives_build_state_for_after_outputs(tmp_path: Path) -> None:
    from astrid.packs.h3_av.shared.stages import load_build_state, save_build_state

    document = _request("v2v", "5/24")
    request = normalize_request(document)
    preparation = _prepared(document, tmp_path)
    save_build_state(tmp_path, request=request, document=document,
                     preparation=preparation, source="source.mp4", plan={})
    state = load_build_state(tmp_path)
    assert state["request_object"].digest == request.digest
    assert state["preparation"]["request"] == request.value
    assert isinstance(state["preparation"]["request"]["duration"], Fraction)


def test_native_timing_keeps_rationals_exact() -> None:
    from astrid.packs.h3_av.shared.timing import ContinuationTimingError, frame_index

    assert frame_index(Fraction(5, 24), "duration") == 5
    with pytest.raises(ContinuationTimingError, match="24 fps frame grid"):
        frame_index(Fraction(5, 24) + Fraction(1, 10**12), "duration")
