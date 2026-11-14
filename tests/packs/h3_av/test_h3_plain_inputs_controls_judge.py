"""Automatic hook selection honors exactly the entry's declared controls."""
import json
from pathlib import Path

import pytest
from astrid.core.generation import entries
from astrid.core.generation.preflight import GenerationReadinessError
from astrid.sdk.generation import generate

@pytest.fixture(autouse=True)
def _h3_candidate_only(monkeypatch):
    """Keep unrelated catalog/ready discovery outside these admission regressions.

    Real default discovery is exercised separately through the CPU CLI proofs.
    """
    import astrid.sdk
    from astrid.core.generation import defaults
    from astrid.core.model_catalog.registry import ModelRegistry

    entry = entries.find_entry("h3")
    monkeypatch.setattr(astrid.sdk, "_load_model_registry", lambda **kwargs: ModelRegistry([]))
    monkeypatch.setattr(entries, "user_entries", lambda: [entry])
    monkeypatch.setattr(entries, "ready_entries", lambda: [])
    monkeypatch.setattr(defaults, "ordered_ids", lambda *a, **k: ("h3",))



@pytest.mark.parametrize("controls", [{"fps": 12}, {"width": 640, "height": 360},
                                    {"negative_prompt": "blur"}])
def test_h3_automatic_selection_rejects_undeclared_controls(tmp_path, monkeypatch, controls):
    monkeypatch.setattr(entries, "build_entry", lambda *a, **k: pytest.fail("undeclared inputs reached build"))
    with pytest.raises(GenerationReadinessError, match="unsupported entry inputs") as caught:
        generate.video(placement="runpod", dry_run=True,
                       prompt="a red bicycle", image_ref=str(tmp_path / "image.png"), **controls)
    assert caught.value.code == "input_rejected"
    assert caught.value.to_envelope()["entry"]["id"] == "h3"



def _plain_judge_hooks():
    entry = entries.find_entry("h3")
    hooks = entries.entry_hooks(entry)
    return entry, hooks, hooks["build"].__globals__


def _judge_image_inputs(tmp_path):
    from PIL import Image

    image = tmp_path / "look.png"
    Image.new("RGB", (1024, 576), "blue").save(image)
    return {"prompt": "a kite", "image_ref": str(image), "frames": 5}


@pytest.mark.parametrize("reference", ["image_ref", "video_ref"])
def test_h3_plain_request_has_equivalent_v2_intent(reference):
    import jsonschema

    _entry, _hooks, namespace = _plain_judge_hooks()

    class Facts:
        @staticmethod
        def measure_source_av(_path):
            return {"video_frames": 6}

    plain = {"prompt": "a kite", "steps": 1, "seed": 42}
    if reference == "image_ref":
        plain["frames"] = 6
    document, assets = namespace["_plain_request"](
        plain,
        {reference: f"/x/{reference}"}, Facts)
    pack = Path("astrid/packs/h3_av")
    jsonschema.validate(document, json.loads((pack / "schemas/request.v1.json").read_text()))
    media = ({"id": "look", "asset": reference, "role": "reference", "modality": "image"}
             if reference == "image_ref" else
             {"id": "source", "asset": reference, "role": "timeline", "modality": "video",
              "at": {"frame": 0}, "range": [0, 0.25],
              "edit": [{"stream": "video", "during": [0, 0.25], "mask": {"full_frame": True}}]})
    native = {"version": 2, "prompt": "a kite", "duration": 0.25,
              "media": [media], "settings": {"steps": 1, "seed": 42}}
    jsonschema.validate(native, json.loads((pack / "schemas/request.v2.json").read_text()))
    normalize = namespace["_shared"]("request").normalize_request
    assert normalize(document).canonical == normalize(native).canonical
    assert assets == {reference: f"/x/{reference}"}


def test_h3_explicit_request_precedes_plain_inputs(tmp_path):
    inputs = _judge_image_inputs(tmp_path)
    image = inputs.pop("image_ref")
    document = {"version": 2, "prompt": "explicit prompt", "duration": "5/24",
                "media": [{"id": "look", "asset": "look", "role": "reference", "modality": "image"}],
                "settings": {"steps": 1, "seed": 42}}
    request = tmp_path / "request.json"
    request.write_text(json.dumps(document))
    supplied = {"request": str(request), "asset_map": {"look": image}}
    entry = entries.find_entry("h3")
    original = entries.build_entry(entry, supplied, tmp_path / "original")
    with_plain = entries.build_entry(entry, {**supplied, "prompt": "ignored",
                                            "image_ref": "/missing.png", "video_ref": "/missing.mp4"},
                                     tmp_path / "with-plain")
    assert original["plan"]["request_digest"] == with_plain["plan"]["request_digest"]
    assert original["bindings"] == with_plain["bindings"]
