"""Exact native-clock times survive normalization, persistence and CPU compilation."""

from fractions import Fraction
import json
from pathlib import Path

import pytest
from PIL import Image

from astrid.packs.h3_av.shared.compile import compile_preparation
from astrid.packs.h3_av.shared.planner import plan_request
from astrid.packs.h3_av.shared.prepare import prepare_request, write_preparation
from astrid.packs.h3_av.shared.request import H3RequestError, normalize_request
from astrid.packs.h3_av.shared.request_v2 import read_prepared_request
from astrid.packs.h3_av.shared.spec import sampling_request_document
from astrid.packs.h3_av.shared.stages import load_build_state, save_build_state


def _document(mode, duration):
    reference = {"id": "look", "asset": "look.png", "role": "reference", "modality": "image"}
    media = [reference]
    if mode != "i2v":
        mask = {"full_frame": True} if mode == "v2v" else {"rectangle": [0, 0, 256, 288]}
        media.insert(0, {
            "id": "source", "asset": "source.mp4", "role": "timeline", "modality": "video",
            "at": {"seconds": 0}, "range": [0, duration],
            "edit": [{"stream": "video", "during": [0, duration], "mask": mask}],
        })
    return {"version": 2, "prompt": "Use exact native frame windows.",
            "duration": duration, "media": media, "settings": {"steps": 1}}


def _assets(tmp_path):
    image = tmp_path / "look.png"
    Image.new("RGB", (32, 32), "blue").save(image)
    source = tmp_path / "source.mp4"
    source.write_bytes(b"CPU-only source fixture")
    return {"look.png": str(image), "source.mp4": str(source)}


@pytest.mark.parametrize("mode", ["i2v", "v2v", "video-edit"])
@pytest.mark.parametrize("duration", ["5/24", Fraction(5, 24), 0.25])
def test_h3dur_exact_windows_validate_persist_and_compile(tmp_path: Path, mode, duration):
    request = normalize_request(_document(mode, duration))
    expected = Fraction(1, 4) if duration == 0.25 else Fraction(5, 24)
    assert isinstance(request.value["duration"], Fraction)
    assert request.value["duration"] == expected
    spec = request.canonical
    assert isinstance(spec.duration, Fraction)
    assert spec.duration == expected
    assert all(isinstance(t, Fraction) for op in spec.ops for t in op.during)
    assert spec.ops[-1].during == (0, expected)
    if spec.source:
        assert spec.source.range == (0, expected)
        assert all(isinstance(t, Fraction) for t in spec.source.range)

    assets = _assets(tmp_path)
    preparation = prepare_request(request, asset_map=assets)
    path = write_preparation(tmp_path / "preparation.json", preparation)
    persisted = json.loads(path.read_text())
    replay = read_prepared_request(persisted["request"], request.digest, require_normalized_v2=True)
    assert replay == request
    assert isinstance(replay.value["duration"], Fraction)
    save_build_state(tmp_path / "state", request=request, document=_document(mode, duration),
                     preparation=preparation, source=assets["source.mp4"], plan={})
    state = load_build_state(tmp_path / "state")
    assert state["request_object"] == request
    assert state["preparation"]["request"] == request.value
    assert isinstance(state["preparation"]["request"]["duration"], Fraction)
    compiled = compile_preparation(persisted, out_dir=tmp_path / "compiled")
    assert compiled["status"] == "compiled"
    timing = compiled["capabilities"]["generation_timing"]
    assert timing["requested_frames"] == expected * 24
    if mode != "i2v":
        assert compiled["capabilities"]["edit_binding"]["during_frames"] == [0, expected * 24]


@pytest.mark.parametrize("mode", ["v2v", "video-edit"])
@pytest.mark.parametrize("field", ["range", "during"])
def test_h3dur_nonrepresentable_windows_keep_native_clock_refusal(mode, field):
    document = _document(mode, 0.25)
    timeline = document["media"][0]
    window = timeline["range"] if field == "range" else timeline["edit"][0]["during"]
    window[1] = "1/7"
    with pytest.raises(H3RequestError, match="is not representable on the native clock"):
        normalize_request(document)


def test_h3dur_sampling_documents_keep_exact_windows():
    document = _document("v2v", 0.5)
    first = document["media"][0]["edit"][0]
    first["during"] = [0, "5/24"]
    document["media"][0]["edit"].append({**first, "during": ["5/24", 0.5]})
    request = normalize_request(document)
    for index, expected in enumerate([(0, Fraction(5, 24)), (Fraction(5, 24), Fraction(1, 2))]):
        sampling = sampling_request_document(request, index, previous_verified_output=index > 0)
        restored = normalize_request(json.loads(json.dumps(sampling)))
        assert restored.canonical.ops[0].during == expected


def test_h3dur_plan_persists_exact_spec_as_json(tmp_path):
    request = normalize_request(_document("video-edit", "5/24"))
    plan = plan_request(request, asset_map=_assets(tmp_path), out_dir=tmp_path / "plan")
    assert plan["compilation"]["status"] == "compiled"
    assert plan["timing"]["requested"]["frames"] == 5
    persisted = json.loads(Path(plan["plan_path"]).read_text())
    assert persisted == json.loads(json.dumps(plan))
    assert Fraction(persisted["spec"]["duration"]) == Fraction(5, 24)
    assert request.value["duration"] == Fraction(5, 24)
