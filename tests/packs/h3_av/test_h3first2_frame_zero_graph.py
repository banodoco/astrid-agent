"""CPU graph contracts for protected first-frame latents and timed image guides."""
from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest
from PIL import Image

from astrid.core.contracts.errors import AstridError
from astrid.core.generation import entries
from astrid.packs.h3_av.shared.compile import compile_preparation
from astrid.packs.h3_av.shared.engine_facts import facts
from astrid.packs.h3_av.shared.prepare import prepare_request
from astrid.packs.h3_av.shared.request import H3RequestError, normalize_request

PACK = Path(__file__).resolve().parents[3] / "astrid/packs/h3_av"
# Frozen by the original compiler at compose 941fca2a4, with this module's
# deterministic image/prompt/settings; filenames are content-addressed.
GOLDEN = Path(__file__).parent / "fixtures/h3first2-reference.api.json"


def _image(tmp_path):
    image = tmp_path / "first.png"
    Image.new("RGB", (1024, 576), (32, 64, 128)).save(image)
    return image


def _request(*references, frames=24):
    return {"version": 1, "operation": "generate", "output": {"duration": frames / 24},
            "content": {"prompt": "A gentle camera push."}, "changes": {"video": [], "audio": []},
            "references": list(references), "overrides": {"steps": 1, "seed": 42}}


def _compile(raw, tmp_path, name="compiled"):
    from vibecomfy.cli_loader import load_workflow_any

    image = _image(tmp_path)
    request = normalize_request(raw)
    asset_ids = ([item["asset"] for item in raw["media"]] if raw["version"] == 2
                 else [item["asset"] for item in raw["references"]])
    preparation = prepare_request(request, asset_map={asset: str(image) for asset in asset_ids})
    manifest = compile_preparation(preparation, out_dir=tmp_path / name)
    graph = load_workflow_any(manifest["workflow"]["workflow.py"]["path"])
    return manifest, graph.compile("api", run_inputs=manifest["workflow_inputs"])


def _one(api, cls):
    matches = [(key, row["inputs"]) for key, row in api.items() if row["class_type"] == cls]
    assert len(matches) == 1, (cls, matches)
    return matches[0]


def _assert_protected_zero(api):
    anchor_id, anchor = _one(api, "MiniMaxH3CustomKeyframesMasked")
    assert json.loads(anchor["keyframe_state"]) == {"count": 1, "positions": [0]}
    assert anchor["indexing"] == "0-based"
    assert anchor["crop"] == "disabled"
    assert api[anchor["keyframe_image_1"][0]]["class_type"] == "LoadImage"
    target_id, target = _one(api, "MiniMaxH3ReferenceToVideo")
    assert anchor["latent"] == [target_id, 1]
    assert anchor["vae"] == target["vae"]
    assert not any(key.startswith("ref_images.") for key in target)
    _, sampled = _one(api, "SamplerCustomAdvanced")
    assert sampled["latent_image"] == [anchor_id, 0]
    _, guider = _one(api, "BasicGuider")
    assert guider["conditioning"] == [target_id, 0]
    assert not any(row["class_type"] in {"MiniMaxH3ImageToVideo", "MiniMaxH3AddGuide"} for row in api.values())


def test_bare_i2v_image_protects_frame_zero_in_the_compiled_graph(tmp_path):
    entry = entries.find_entry("h3")
    built = entries.build_entry(entry, {"prompt": "A gentle camera push.", "image_ref": str(_image(tmp_path)),
                                        "frames": 24, "steps": 1, "seed": 42}, tmp_path / "job")
    api = built["workflow"].compile("api", run_inputs=built["bindings"])
    _assert_protected_zero(api)


def test_explicit_first_frame_contract_protects_latent_and_declares_locked_node(tmp_path):
    raw = _request({"asset": "first", "purpose": "first_frame"})
    jsonschema.validate(raw, json.loads((PACK / "schemas/request.v1.json").read_text()))
    manifest, api = _compile(raw, tmp_path)
    _assert_protected_zero(api)
    assert manifest["capabilities"]["first_frame"]["preservation"] == "encoded video latent; zero denoise mask"
    from vibecomfy.cli_loader import load_workflow_any

    graph = load_workflow_any(manifest["workflow"]["workflow.py"]["path"])
    requirement = next(row for row in graph.metadata["requirements"]["custom_node_refs"]
                       if row["slug"] == "ComfyUI-H3-Motion-Context-MultiRef")
    assert requirement["commit"] == "361624fb406b63eb6694442eac6c895fc1533a70"
    assert "MiniMaxH3CustomKeyframesMasked" in requirement["classes"]
    _, legacy = _compile(_request({"asset": "first", "purpose": "reference"}), tmp_path, "look")
    assert {row["inputs"].get("unet_name") for row in api.values() if row["class_type"] == "UNETLoader"} == {
        row["inputs"].get("unet_name") for row in legacy.values() if row["class_type"] == "UNETLoader"}
    assert any("decoded pixels" in line for line in manifest["limitations"])


@pytest.mark.parametrize("purpose", ["appearance", "reference"])
def test_explicit_reference_graph_matches_compose_byte_for_byte(tmp_path, purpose):
    _, api = _compile(_request({"asset": "first", "purpose": purpose}), tmp_path)
    assert json.dumps(api, sort_keys=True, indent=2) + "\n" == GOLDEN.read_text()


@pytest.mark.parametrize("position", [0, 3, 17, 23])
def test_keyframe_conditions_requested_frame_and_reaches_guider(tmp_path, position):
    raw = _request({"asset": "guide", "purpose": "keyframe", "position": position})
    jsonschema.validate(raw, json.loads((PACK / "schemas/request.v1.json").read_text()))
    _, api = _compile(raw, tmp_path)
    guide_id, guide = _one(api, "MiniMaxH3AddGuide")
    target_id, target = _one(api, "MiniMaxH3ReferenceToVideo")
    assert guide["frame_idx"] == position
    assert guide["positive"] == [target_id, 0]
    assert guide["latent"] == [target_id, 1]
    assert guide["vae"] == target["vae"]
    assert api[guide["image"][0]]["class_type"] == "LoadImage"
    assert not any(key.startswith("ref_images.") for key in target)
    assert _one(api, "BasicGuider")[1]["conditioning"] == [guide_id, 0]
    assert _one(api, "SamplerCustomAdvanced")[1]["latent_image"] == [target_id, 1]


def test_anchor_guides_and_look_references_have_distinct_routes(tmp_path):
    raw = _request({"asset": "first", "purpose": "first_frame"},
                   {"asset": "look", "purpose": "reference"},
                   {"asset": "guide-a", "purpose": "keyframe", "position": 3},
                   {"asset": "guide-b", "purpose": "keyframe", "position": 23})
    _, api = _compile(raw, tmp_path)
    _, target = _one(api, "MiniMaxH3ReferenceToVideo")
    assert [key for key in target if key.startswith("ref_images.")] == ["ref_images.ref_image_0"]
    assert target["ref_images.ref_image_0"] == ["reference_1", 0]
    guides = {row["inputs"]["frame_idx"]: (key, row["inputs"]) for key, row in api.items()
              if row["class_type"] == "MiniMaxH3AddGuide"}
    assert set(guides) == {3, 23}
    assert guides[23][1]["positive"] == [guides[3][0], 0]
    assert _one(api, "BasicGuider")[1]["conditioning"] == [guides[23][0], 0]
    assert _one(api, "SamplerCustomAdvanced")[1]["latent_image"] == [_one(api, "MiniMaxH3CustomKeyframesMasked")[0], 0]


@pytest.mark.parametrize("position", [-1, 24, 39, 1.0, 0.5, "3", "last", True, None])
def test_invalid_keyframe_position_has_typed_actionable_refusal(position):
    raw = _request({"asset": "guide", "purpose": "keyframe", "position": position})
    with pytest.raises(H3RequestError) as caught:
        normalize_request(raw)
    error = facts().astrid_error(caught.value)
    assert isinstance(error, AstridError)
    assert error.code == "h3.keyframe.position"
    assert error.field == "references[0].position"
    assert "0..23" in error.fix


@pytest.mark.parametrize("references", [
    [{"asset": "a", "purpose": "first_frame", "position": 1}],
    [{"asset": "a", "purpose": "appearance", "position": 0}],
    [{"asset": "a", "purpose": "first_frame"}, {"asset": "b", "purpose": "first_frame"}],
])
def test_ambiguous_or_unsupported_anchor_contract_is_typed(references):
    with pytest.raises(H3RequestError) as caught:
        normalize_request(_request(*references))
    assert facts().astrid_error(caught.value).code == "h3.keyframe.unsupported"
    assert caught.value.fix


def _native(purpose=None):
    image = {"id": "first_frame", "asset": "first", "role": "reference", "modality": "image"}
    if purpose:
        image["purpose"] = purpose
    return {"version": 2, "prompt": "A gentle camera push.", "duration": 1,
            "media": [image], "settings": {"steps": 1, "seed": 42}}


def test_native_single_image_default_and_explicit_reference(tmp_path):
    _, api = _compile(_native(), tmp_path, "default")
    _assert_protected_zero(api)
    raw = _native("reference")
    jsonschema.validate(raw, json.loads((PACK / "schemas/request.v2.json").read_text()))
    _, api = _compile(raw, tmp_path, "reference")
    assert json.dumps(api, sort_keys=True, indent=2) + "\n" == GOLDEN.read_text()


def test_only_look_images_receive_picture_tags():
    raw = _native("first_frame")
    raw["media"].extend([
        {"id": "guide", "asset": "pose", "role": "reference", "modality": "image",
         "purpose": "keyframe", "position": 17},
        {"id": "look", "asset": "look", "role": "reference", "modality": "image",
         "purpose": "reference"},
    ])
    request = normalize_request(raw)
    assert request.value["model_tags"] == {"look": "<Picture 1>"}
    assert "model_tag" not in request.value["media"][0]
    assert "model_tag" not in request.value["media"][1]


def test_native_keyframe_contract_and_canonical_round_trip(tmp_path):
    from astrid.packs.h3_av.shared.spec import sampling_request_document

    raw = _native("keyframe")
    raw["media"][0]["position"] = 23
    _, api = _compile(raw, tmp_path, "original")
    assert _one(api, "MiniMaxH3AddGuide")[1]["frame_idx"] == 23
    document = sampling_request_document(normalize_request(raw), 0)
    _, replay = _compile(document, tmp_path, "replay")
    assert replay == api


@pytest.mark.parametrize("position", [-1, 24, 1.0, "last", True])
def test_native_invalid_guide_positions_are_typed(position):
    raw = _native("keyframe")
    raw["media"][0]["position"] = position
    with pytest.raises(H3RequestError) as caught:
        normalize_request(raw)
    assert caught.value.code == "h3.keyframe.position"
    assert caught.value.field == "media[0].position"


@pytest.mark.parametrize("purpose", ["first_frame", "keyframe"])
def test_native_timed_image_requires_resolvable_image_modality(purpose):
    raw = _native(purpose)
    raw["media"][0].pop("modality")
    raw["media"][0]["asset"] = "untyped-asset"
    if purpose == "keyframe":
        raw["media"][0]["position"] = 3
    with pytest.raises(H3RequestError) as caught:
        normalize_request(raw)
    assert caught.value.code == "h3.keyframe.unsupported"
    assert caught.value.field == "media[0].modality"
    assert "modality" in caught.value.fix and "image" in caught.value.fix


def test_guide_position_refusal_reaches_the_public_generation_boundary(tmp_path):
    from astrid.sdk.generation import GenerationFacade

    raw = _request({"asset": "guide", "purpose": "keyframe", "position": -1})
    request = tmp_path / "request.json"
    request.write_text(json.dumps(raw))
    assets = tmp_path / "asset-map.json"
    assets.write_text(json.dumps({"guide": str(_image(tmp_path))}))
    with pytest.raises(AstridError) as caught:
        GenerationFacade().video(entry="h3", placement="runpod", dry_run=True,
                                 inputs={"request": str(request), "asset_map": str(assets)})
    assert caught.value.code == "h3.keyframe.position"
    assert caught.value.field == "references[0].position"


def test_old_sealed_native_look_request_retains_the_baseline_graph(tmp_path):
    import hashlib
    from astrid.packs.h3_av.shared.request_v2 import read_prepared_request
    from vibecomfy.cli_loader import load_workflow_any

    # The pre-change normalized request had no purpose discriminator.
    value = normalize_request(_native()).value
    value["media"][0].pop("purpose")
    digest = hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                       separators=(",", ":")).encode()).hexdigest()
    sealed = read_prepared_request(value, digest, require_normalized_v2=True)
    assert "purpose" not in sealed.value["media"][0]
    prepared = prepare_request(sealed, asset_map={"first": str(_image(tmp_path))})
    manifest = compile_preparation(prepared, out_dir=tmp_path / "sealed")
    api = load_workflow_any(manifest["workflow"]["workflow.py"]["path"]).compile(
        "api", run_inputs=manifest["workflow_inputs"])
    assert json.dumps(api, sort_keys=True, indent=2) + "\n" == GOLDEN.read_text()


def test_first_frame_canonical_round_trip_keeps_latent_protection(tmp_path):
    from astrid.packs.h3_av.shared.spec import sampling_request_document

    raw = _request({"asset": "first", "purpose": "first_frame"})
    document = sampling_request_document(normalize_request(raw), 0)
    _, api = _compile(document, tmp_path)
    _assert_protected_zero(api)


def test_native_source_backed_keyframe_refused_before_compilation():
    raw = _native("keyframe")
    raw["media"][0]["position"] = 3
    raw["media"].append({"id": "source", "asset": "source.mp4", "role": "timeline", "modality": "video",
                         "at": {"frame": 0}, "range": [0, 1],
                         "edit": [{"stream": "video", "during": [0, 1]}]})
    with pytest.raises(H3RequestError) as caught:
        normalize_request(raw)
    assert caught.value.code == "h3.keyframe.unsupported"


def test_documented_i2v_request_admitted_with_protected_frame_zero(tmp_path):
    # Verbatim JSON from r13/h3-i2v-inputs (22d6decff), with the same asset-map keys.
    raw = {"version": 2,
           "prompt": "Animate the supplied image with a gentle camera push. Quiet ambience, no speech or music.",
           "duration": "5/24", "media": [{"id": "first_frame", "asset": "first-frame.png",
                                            "role": "reference", "modality": "image"}],
           "settings": {"steps": 1, "seed": 42}}
    manifest, api = _compile(raw, tmp_path)
    _assert_protected_zero(api)
    assert manifest["capabilities"]["generation_timing"]["requested_frames"] == 5
