from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import jsonschema
import pytest

from astrid.core.contracts.errors import AstridValueError
from astrid.packs.h3_av.shared.request import normalize_request
from astrid.packs.h3_av.shared.spec import normalize_spec


FIXTURES = Path(__file__).with_name("fixtures") / "spec"
SCHEMAS = Path(__file__).resolve().parents[3] / "astrid/packs/h3_av/schemas"


def _fixture(name: str = "static_edit") -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text())


@pytest.mark.parametrize("name", ["static_edit", "generate", "extend"])
def test_legacy_and_spec_imports_have_identical_semantics_and_unchanged_digests(name: str) -> None:
    fixture = _fixture(name)
    views = [normalize_spec(fixture[key]) for key in ("v1", "v2", "spec")]
    assert views[0].value == views[1].value == views[2].value
    for index, version in enumerate((1, 2)):
        schema_bytes = (SCHEMAS / f"request.v{version}.json").read_bytes()
        assert hashlib.sha256(schema_bytes).hexdigest() == fixture[f"v{version}_schema_sha256"]
        raw = fixture[f"v{version}"]
        jsonschema.validate(raw, json.loads(schema_bytes))
        sealed = normalize_request(raw)
        assert sealed.digest == fixture[f"v{version}_digest"]
        assert sealed.value == fixture[f"v{version}_normalized"]
        assert views[index].requests == (sealed,)
        assert normalize_spec(sealed).value == views[index].value
    assert views[0].value["capability"]["available"] is False


def test_source_free_generation_remains_source_free() -> None:
    view = normalize_spec(_fixture("generate")["spec"]).value
    assert view["source"] is None
    assert view["operations"] == [{"op": "generate", "input": None, "sampling": True}]
    assert not any(op["input"] == "original" for op in view["operations"])


def test_ordered_edits_chain_verified_outputs_and_preserve_never_samples() -> None:
    raw = _fixture()["spec"]
    raw["regions"] = {"left": {"rectangle": [0, 0, 30, 20]}, "overlap": {"rectangle": [20, 0, 30, 20]}}
    raw["operations"] = [
        {"op": "regenerate", "during": [0, 2], "region": "left"},
        {"op": "preserve", "stream": "audio", "during": [0, 4]},
        {"op": "regenerate", "during": [1, 3], "region": "overlap"},
        {"op": "regenerate", "during": [1, 3], "region": "left"},
    ]
    view = normalize_spec(raw).value
    first, preserve, second, third = view["operations"]
    assert first["input"] == "original"
    assert preserve == {"op": "preserve", "stream": "audio", "during": [0, 4], "input": "original", "sampling": False}
    assert second["input"] == {"verified_output_of": 0}
    assert third["input"] == {"verified_output_of": 2}
    assert view["overlap_policy"] == "later_edit_wins"
    assert view["preserve_input"] == "original"


def test_preserve_only_has_no_sampling_pass() -> None:
    raw = _fixture()["spec"]
    raw["operations"] = [{"op": "preserve", "during": [0, 4]}]
    assert not any(op["sampling"] for op in normalize_spec(raw).value["operations"])


def test_named_references_resolve_without_reordering_operations() -> None:
    raw = _fixture()["spec"]
    raw["refs"] = {"look": {"asset": "look.png", "modality": "image"}}
    raw["operations"][0]["refs"] = ["look"]
    view = normalize_spec(raw).value
    assert view["refs"] == {"ref-1": {"asset": "look.png", "modality": "image"}}
    assert view["operations"][0]["refs"] == ["ref-1"]


@pytest.mark.parametrize("mutate, code, field", [
    (lambda raw: raw["operations"][0].update(region="missing"), "dangling_region", "operations[0].region"),
    (lambda raw: raw["operations"][0].update(refs=["missing"]), "dangling_ref", "operations[0].refs"),
    (lambda raw: raw["operations"][0].update(op="track"), "unsupported_operation", "operations[0].op"),
    (lambda raw: raw.update(source=[raw["source"], raw["source"]]), "source_count", "source"),
    (lambda raw: raw.update(source=None), "source_required", "operations[0]"),
    (lambda raw: raw["operations"][0].update(stream="audio"), "conflict", "operations[0].region"),
    (lambda raw: raw["operations"][0].update(during=[0, 5]), "conflict", "operations[0].during"),
    (lambda raw: raw.update(regions={"watermark": {"rectangle": [0, 0, -1, 2]}}), "invalid_region", "regions.watermark"),
])
def test_semantic_errors_use_the_g21_envelope(mutate, code: str, field: str) -> None:
    raw = _fixture()["spec"]
    mutate(raw)
    with pytest.raises(AstridValueError) as caught:
        normalize_spec(raw)
    error = caught.value.to_envelope()
    assert {key: error[key] for key in ("code", "field")} == {"code": f"h3.spec.{code}", "field": field}
    assert error["message"] and error["fix"]


@pytest.mark.parametrize("selector", [{"mask_asset": "watermark.png"}, {"rectangle": [0, 0, 30, 20]}])
@pytest.mark.parametrize("preserve_first", [False, True])
def test_preserve_conflicts_with_overlapping_edits_independent_of_order(selector: dict, preserve_first: bool) -> None:
    raw = _fixture()["spec"]
    raw["regions"] = {"watermark": selector}
    raw["operations"] = [
        {"op": "regenerate", "during": [0, 2], "region": "watermark"},
        {"op": "preserve", "during": [1, 3], "region": "watermark"},
    ]
    if preserve_first:
        raw["operations"].reverse()
    with pytest.raises(AstridValueError) as caught:
        normalize_spec(raw)
    assert caught.value.code == "h3.spec.conflict"


def test_disjoint_boxes_and_touching_intervals_can_be_preserved() -> None:
    raw = _fixture()["spec"]
    raw["regions"] = {"left": {"rectangle": [0, 0, 10, 10]}, "right": {"rectangle": [10, 0, 10, 10]}}
    raw["operations"] = [
        {"op": "regenerate", "during": [0, 2], "region": "left"},
        {"op": "preserve", "during": [0, 4], "region": "right"},
        {"op": "preserve", "during": [2, 4], "region": "left"},
    ]
    assert len(normalize_spec(raw).value["operations"]) == 3


def test_preserve_requires_original_frames_and_cannot_replace_content() -> None:
    raw = _fixture()["spec"]
    raw["source"]["range"] = [1, 3]
    raw["operations"] = [{"op": "preserve", "during": [0, 3]}]
    with pytest.raises(AstridValueError, match="absent from the original"):
        normalize_spec(raw)
    raw["operations"] = [{"op": "preserve", "during": [0, 1], "text": "new text"}]
    with pytest.raises(AstridValueError, match="cannot request conditioning"):
        normalize_spec(raw)


def test_multiple_timeline_sources_are_rejected_only_by_the_canonical_view() -> None:
    raw = _fixture()["v2"]
    second = copy.deepcopy(raw["media"][0])
    second.update(id="another", at={"frame": 24})
    raw["media"].append(second)
    assert len(normalize_request(raw).value["media"]) == 2
    with pytest.raises(AstridValueError) as caught:
        normalize_spec(raw)
    assert caught.value.code == "h3.spec.source_count"


@pytest.mark.parametrize("operation", ["generate", "extend"])
def test_single_window_generation_and_extension_cannot_be_mixed_with_edits(operation: str) -> None:
    raw = _fixture(operation)["spec"]
    raw["operations"].append({"op": "preserve", "stream": "audio", "during": [0, 1]})
    with pytest.raises(AstridValueError) as caught:
        normalize_spec(raw)
    assert caught.value.code == "h3.spec.unsupported_combination"


def test_unsupported_legacy_regions_are_not_silently_dropped() -> None:
    raw = _fixture()["v1"]
    raw["changes"]["video"][0]["area"] = {"subjects": ["person"]}
    assert normalize_request(raw)
    with pytest.raises(AstridValueError) as caught:
        normalize_spec(raw)
    assert caught.value.code == "h3.spec.unsupported_region"


def test_malformed_request_validation_is_wrapped_without_a_second_parser() -> None:
    raw = _fixture()["spec"]
    raw["operations"][0]["during"] = [0, "1/25"]
    with pytest.raises(AstridValueError) as caught:
        normalize_spec(raw)
    assert caught.value.code == "h3.spec.invalid_request"
    assert "native clock" in caught.value.message


def test_regenerate_does_not_resolve_future_source_frames() -> None:
    raw = _fixture()["spec"]
    raw["source"]["range"] = [0, 1]
    with pytest.raises(AstridValueError) as caught:
        normalize_spec(raw)
    assert caught.value.code == "h3.spec.unsupported_combination"
    assert "absent from the original" in caught.value.message


def test_canonical_prompt_whitespace_does_not_rewrite_legacy_identity() -> None:
    raw = _fixture()["v1"]
    initial = normalize_request(raw)
    raw["content"]["prompt"] = "  " + raw["content"]["prompt"] + "\n"
    assert normalize_request(raw).digest != initial.digest
    assert normalize_spec(raw).value == normalize_spec(initial).value


@pytest.mark.parametrize("changes", [
    {"video": [{"during": [4, 8], "area": {"full_frame": True}, "action": "generate"}], "audio": []},
    {"video": [], "audio": []},
    {"video": [{"during": [4, 8], "area": {"full_frame": True}, "action": "generate"}] * 2,
     "audio": [{"during": [4, 8], "action": "generate"}]},
])
def test_extend_does_not_discard_stream_permissions_or_duplicate_operations(changes: dict) -> None:
    raw = _fixture("extend")["v1"]
    raw["changes"] = changes
    assert normalize_request(raw)
    with pytest.raises(AstridValueError) as caught:
        normalize_spec(raw)
    assert caught.value.code == "h3.spec.unsupported_combination"


def test_explicit_native_extend_needs_both_stream_permissions() -> None:
    raw = _fixture("extend")["v2"]
    raw["media"][0]["edit"] = [{"stream": "video", "during": [4, 8]}]
    with pytest.raises(AstridValueError) as caught:
        normalize_spec(raw)
    assert caught.value.code == "h3.spec.unsupported_combination"
    raw["media"][0]["edit"].append({"stream": "audio", "during": [4, 8]})
    assert normalize_spec(raw).value == normalize_spec(_fixture("extend")["spec"]).value
