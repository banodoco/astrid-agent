from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import urllib.request
from dataclasses import replace
from pathlib import Path

import jsonschema
import pytest

from astrid.packs.h3_av.shared.request import H3RequestError, normalize_request
from astrid.packs.h3_av.shared.spec import (
    H3Spec,
    H3SpecError,
    Operation,
    Reference,
    Region,
    Source,
    normalize_spec,
)

PACK = Path(__file__).resolve().parents[3] / "astrid/packs/h3_av"
CASES = json.loads((Path(__file__).parent / "fixtures/spec_min.json").read_text())

# Generated with the original normalizers read with git show from Agent base
# 1784da3e903cbe2943039e0c2c9f95cab9827e7d, before adding the canonical view.
LEGACY_DIGESTS = {
    "source_free_generate": (
        "de3ab4d9992d03c5b5005fa6134810f878a8f2e9d259d08d6e8e0f08f9bb4b12",
        "cf04863d1a24e127553eb718b6c456e55368c60fb9795dbc6b110cc3758bda0b",
    ),
    "source_backed_regenerate": (
        "c5017b87fe2a1fba981c067cfe3d57b70824f7f9268a3cd094e27aa257d3919e",
        "cc6f04c3bc780933687a0e5e39ad7c472185d70a7842bf1964609aeb7e47ad0d",
    ),
    "source_backed_extend": (
        "0207316b50a13d1e573cd0f5dc13c9d8a963688076b226d272020a6f6e97009b",
        "ca502f4cc8da4ef03435a75dc64e21e748b22c3115dac5086fbd5588a6ca1698",
    ),
}
SCHEMA_DIGESTS = {
    1: "3eb17c74f0c9788f4077896603c7918918aa62cf321dc26e394c31e4aca65403",
    2: "ee22c452912dfe56c29b101dc7c6facd5a16508d352a791a23d1cf375c7b136e",
}


def _typed_case(name: str) -> H3Spec:
    common = {"prompt": "A quiet café scene.", "refs": (Reference("look", "look.png"),)}
    if name == "source_free_generate":
        return H3Spec(
            **common, duration=5, ops=(Operation("generate", (0, 5), refs=("look",), stream="av"),)
        )
    if name == "source_backed_regenerate":
        return H3Spec(
            **common,
            duration=4,
            source=Source("source.mp4", (0, 4)),
            regions=(Region("subject", box=(8, 12, 32, 24)),),
            ops=(Operation("regenerate", (1, 2), region="subject", refs=("look",)),),
        )
    return H3Spec(
        **common,
        duration=5,
        source=Source("source.mp4", (0, 2)),
        ops=(Operation("extend", (2, 5), refs=("look",), stream="av"),),
    )


def _edit(*ops: Operation, regions: tuple[Region, ...] = ()) -> H3Spec:
    return H3Spec(
        "Change the declared pixels.", 4, Source("source.mp4", (0, 4)), regions=regions, ops=ops
    )


def _assert_g21(error: BaseException, code: str | None = None) -> None:
    assert isinstance(error, H3RequestError)
    envelope = error.to_envelope()
    for key in ("code", "message", "field", "fix"):
        assert isinstance(envelope[key], str) and envelope[key]
    if code is not None:
        assert envelope["code"] == f"h3.spec.{code}"


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["name"])
def test_v1_v2_and_typed_spec_have_equal_semantics_without_changing_identity(case: dict) -> None:
    requests = [normalize_request(case[f"v{version}"]) for version in (1, 2)]
    snapshots = [copy.deepcopy(request.value) for request in requests]
    semantics = [request.canonical for request in requests]
    expected = normalize_spec(_typed_case(case["name"]))

    assert semantics == [expected, expected]
    assert all(spec.sampling_passes() == expected.sampling_passes() for spec in semantics)
    assert normalize_spec(expected) == expected
    assert [request.value for request in requests] == snapshots
    assert tuple(request.digest for request in requests) == LEGACY_DIGESTS[case["name"]]
    assert requests[0].digest != requests[1].digest
    assert [request.value["version"] for request in requests] == [1, 2]
    assert expected.refs == (Reference("ref-1", "look.png"),)
    if case["name"] == "source_free_generate":
        assert expected.source is None
        assert expected.ops == (Operation("generate", (0, 5), refs=("ref-1",), stream="av"),)
    elif case["name"] == "source_backed_regenerate":
        assert expected.source == Source("source.mp4", (0, 4))
        assert expected.regions == (Region("region-1", box=(8, 12, 32, 24)),)
        assert expected.ops == (Operation("regenerate", (1, 2), "region-1", ("ref-1",)),)
    else:
        assert expected.source == Source("source.mp4", (0, 2))
        assert expected.ops == (Operation("extend", (2, 5), refs=("ref-1",), stream="av"),)


@pytest.mark.parametrize("version", (1, 2))
def test_versioned_schema_bytes_and_legacy_fixture_validation(version: int) -> None:
    data = (PACK / f"schemas/request.v{version}.json").read_bytes()
    assert hashlib.sha256(data).hexdigest() == SCHEMA_DIGESTS[version]
    schema = json.loads(data)
    for case in CASES:
        jsonschema.validate(case[f"v{version}"], schema)


def test_canonical_view_preserves_v1_prompt_bytes_and_legacy_digest() -> None:
    raw = copy.deepcopy(CASES[0]["v1"])
    raw["content"]["prompt"] = " \tA café scene.  \n"
    request = normalize_request(raw)
    digest = request.digest
    assert request.canonical.prompt == raw["content"]["prompt"]
    assert request.digest == digest
    assert request.digest != normalize_request(CASES[0]["v1"]).digest


def test_legacy_edit_uses_native_model_and_keeps_sampling_defaults_and_explicit_settings() -> None:
    raw = copy.deepcopy(CASES[1]["v1"])
    raw["overrides"] = {}
    legacy = normalize_request(raw).canonical
    native = normalize_request(CASES[1]["v2"]).canonical
    assert legacy.settings["model"] == native.settings["model"]
    assert legacy.settings["model"] == "minimax_h3_ref2va_pruned_int8_convrot.safetensors"
    assert legacy.settings["steps"] == 20
    assert legacy.settings["seed"] == 0
    assert legacy != native
    raw["overrides"] = {"model": native.settings["model"], "steps": 12, "seed": 42}
    customized = normalize_request(raw).canonical
    assert customized.settings["model"] == native.settings["model"]
    assert customized.settings["steps"] == 12
    assert customized.settings["seed"] == 42
    assert customized != native


def test_typed_input_rejects_more_than_one_source_with_a_coded_error() -> None:
    spec = replace(
        _edit(Operation("regenerate", (0, 1))),
        source=(Source("a.mp4", (0, 4)), Source("b.mp4", (0, 4))),
    )
    with pytest.raises(H3SpecError) as error:
        normalize_spec(spec)
    _assert_g21(error.value, "source_count")


def test_legacy_v2_more_than_one_source_fails_canonical_import() -> None:
    raw = copy.deepcopy(CASES[1]["v2"])
    source = copy.deepcopy(raw["media"][0])
    source.update(id="second-source", at={"frame": 1}, edit=[])
    raw["media"].append(source)
    with pytest.raises(H3SpecError) as error:
        _ = normalize_request(raw).canonical
    _assert_g21(error.value, "source_count")


@pytest.mark.parametrize(
    ("operation", "code"),
    [
        (Operation("inpaint_future", (0, 1)), "unsupported_op"),
        (Operation("regenerate", (0, 1), region="missing"), "dangling_region"),
        (Operation("regenerate", (0, 1), refs=("missing",)), "dangling_reference"),
        (Operation("regenerate", (0, 1), stream="depth"), "unsupported_op"),
    ],
)
def test_unknown_operations_and_dangling_names_fail_closed(operation: Operation, code: str) -> None:
    with pytest.raises(H3SpecError) as error:
        normalize_spec(_edit(operation))
    _assert_g21(error.value, code)


def test_legacy_v2_dangling_guides_have_the_g21_error_fields() -> None:
    raw = copy.deepcopy(CASES[1]["v2"])
    raw["media"][0]["edit"][0]["guides"] = ["missing"]
    with pytest.raises(H3RequestError) as error:
        normalize_request(raw)
    _assert_g21(error.value)
    assert "dangling" in str(error.value)


@pytest.mark.parametrize("version", (1, 2))
def test_legacy_import_rejects_overlapping_edit_and_preservation(version: int) -> None:
    raw = copy.deepcopy(CASES[1][f"v{version}"])
    edits = raw["changes"]["video"] if version == 1 else raw["media"][0]["edit"]
    preserve = copy.deepcopy(edits[0])
    preserve["action"] = "preserve"
    preserve.pop("guides", None)
    edits.append(preserve)
    with pytest.raises(H3SpecError) as error:
        _ = normalize_request(raw).canonical
    _assert_g21(error.value, "conflicting_permissions")


def test_mask_file_v1_v2_and_typed_spec_are_equivalent() -> None:
    v1, v2 = (copy.deepcopy(CASES[1][f"v{version}"]) for version in (1, 2))
    v1["changes"]["video"][0]["area"] = {"mask_asset": "area.png"}
    v2["media"][0]["edit"][0]["mask"] = {"asset": "area.png"}
    typed = replace(
        _typed_case(CASES[1]["name"]), regions=(Region("subject", mask_asset="area.png"),)
    )
    assert (
        normalize_request(v1).canonical == normalize_request(v2).canonical == normalize_spec(typed)
    )


@pytest.mark.parametrize("operation", ("generate", "preserve"))
def test_unrepresentable_reference_conditioning_is_not_silently_dropped(operation: str) -> None:
    refs = (Reference("first", "first.png"), Reference("second", "second.png"))
    if operation == "generate":
        spec = H3Spec(
            "Guidance.",
            4,
            refs=refs,
            ops=(Operation(operation, (0, 4), refs=("second",), stream="av"),),
        )
    else:
        spec = replace(_edit(Operation(operation, (0, 1), refs=("first",))), refs=refs)
    with pytest.raises(H3SpecError) as error:
        normalize_spec(spec)
    _assert_g21(error.value, "unsupported_reference")


@pytest.mark.parametrize(
    "regions",
    [
        (Region("edit", box=(0, 0, 10, 10)), Region("keep", box=(0, 0, 10, 10))),
        (Region("edit", box=(0, 0, 10, 10)), Region("keep", box=(9, 9, 10, 10))),
        (Region("edit", mask_asset="edit.png"), Region("keep", mask_asset="keep.png")),
        (Region("edit", mask_asset="edit.png"), Region("keep", box=(100, 100, 10, 10))),
    ],
    ids=("same-box", "overlapping-boxes", "ambiguous-files", "ambiguous-file-and-box"),
)
def test_explicit_preservation_conflicts_fail_without_inspecting_mask_pixels(
    regions: tuple[Region, ...],
) -> None:
    with pytest.raises(H3SpecError) as error:
        normalize_spec(
            _edit(
                Operation("regenerate", (0, 2), "edit"),
                Operation("preserve", (1, 3), "keep"),
                regions=regions,
            )
        )
    _assert_g21(error.value, "conflicting_permissions")


def test_full_frame_preservation_conflicts_with_regional_edit() -> None:
    with pytest.raises(H3SpecError) as error:
        normalize_spec(
            _edit(
                Operation("regenerate", (0, 1), "edit"),
                Operation("preserve", (0, 1)),
                regions=(Region("edit", box=(0, 0, 10, 10)),),
            )
        )
    _assert_g21(error.value, "conflicting_permissions")


@pytest.mark.parametrize(
    ("edit", "preserve", "regions"),
    [
        (
            Operation("regenerate", (0, 1), "edit"),
            Operation("preserve", (0, 1), "keep"),
            (Region("edit", box=(0, 0, 10, 10)), Region("keep", box=(10, 0, 10, 10))),
        ),
        (Operation("regenerate", (0, 1)), Operation("preserve", (1, 2)), ()),
        (Operation("regenerate", (0, 1)), Operation("preserve", (0, 1), stream="audio"), ()),
        (
            Operation("regenerate", (0, 1), "edit"),
            Operation("preserve", (1, 2), "keep"),
            (Region("edit", mask_asset="edit.png"), Region("keep", mask_asset="keep.png")),
        ),
    ],
    ids=("touching-boxes", "half-open-time", "independent-streams", "files-at-disjoint-times"),
)
def test_disjoint_permissions_are_allowed(
    edit: Operation, preserve: Operation, regions: tuple[Region, ...]
) -> None:
    spec = normalize_spec(_edit(edit, preserve, regions=regions))
    assert [op.op for op in spec.ops] == ["regenerate", "preserve"]
    assert len(spec.sampling_passes()) == 1


def test_two_overlapping_edits_keep_order_and_read_preceding_verified_output() -> None:
    spec = normalize_spec(
        _edit(
            Operation("regenerate", (0, 2), "first"),
            Operation("regenerate", (1, 3), "second"),
            regions=(Region("first", box=(0, 0, 10, 10)), Region("second", box=(5, 5, 10, 10))),
        )
    )
    passes = spec.sampling_passes()
    assert [item.operation for item in passes] == list(spec.ops)
    assert [item.reads for item in passes] == ["source", "previous_verified_output"]
    assert [item.operation.during for item in passes] == [(0, 2), (1, 3)]
    assert [spec.regions[index].box for index in range(2)] == [(0, 0, 10, 10), (5, 5, 10, 10)]


def test_preserve_is_original_source_constraint_without_a_sampling_pass() -> None:
    spec = normalize_spec(
        _edit(
            Operation("preserve", (0, 4), "original"),
            Operation("regenerate", (0, 2), "editable"),
            Operation("regenerate", (1, 3), "editable"),
            regions=(
                Region("original", box=(0, 0, 10, 10)),
                Region("editable", box=(20, 0, 10, 10)),
            ),
        )
    )
    preservation = spec.ops[0]
    passes = spec.sampling_passes()
    assert len(passes) == 2
    assert all(item.operation.op == "regenerate" for item in passes)
    assert all(item.preserves == (preservation,) for item in passes)
    assert all(item.preserve_reads == "original_source" for item in passes)
    assert [item.reads for item in passes] == ["source", "previous_verified_output"]
    assert normalize_spec(_edit(Operation("preserve", (0, 4)))).sampling_passes() == ()


def test_static_mask_file_is_represented_without_opening_the_asset() -> None:
    spec = normalize_spec(
        _edit(
            Operation("regenerate", (0, 1), "subject"),
            regions=(Region("subject", mask_asset="missing-mask.png"),),
        )
    )
    assert spec.regions == (Region("region-1", mask_asset="missing-mask.png"),)


def test_inspected_video_mask_is_not_a_static_region() -> None:
    spec = _edit(
        Operation("regenerate", (0, 1), "subject"),
        regions=(Region("subject", mask_asset="animated-mask"),),
    )
    with pytest.raises(H3SpecError) as error:
        normalize_spec(spec, asset_modalities={"animated-mask": "video"})
    _assert_g21(error.value, "unsupported_region")


@pytest.mark.parametrize(
    "mask",
    [
        {"rectangle": [0, 0, 10, 10], "track": {"kind": "future-frame"}},
        {"polygon": [[0, 0], [10, 0], [10, 10]]},
    ],
    ids=("tracked", "polygon"),
)
def test_legacy_regions_outside_static_subset_fail_explicitly(mask: dict) -> None:
    raw = copy.deepcopy(CASES[1]["v2"])
    raw["media"][0]["edit"][0]["mask"] = mask
    with pytest.raises(H3SpecError) as error:
        _ = normalize_request(raw).canonical
    _assert_g21(error.value, "unsupported_region")


def test_nonzero_source_placement_requires_unsupported_timeline_semantics() -> None:
    raw = copy.deepcopy(CASES[1]["v2"])
    raw["media"][0]["at"] = {"frame": 1}
    with pytest.raises(H3SpecError) as error:
        _ = normalize_request(raw).canonical
    _assert_g21(error.value, "unsupported_source")


@pytest.mark.parametrize(
    "spec",
    [
        H3Spec(
            "Future-frame edits.",
            5,
            Source("source.mp4", (0, 2)),
            ops=(Operation("regenerate", (2, 3)),),
        ),
        H3Spec(
            "Multiple sampling windows.",
            5,
            Source("source.mp4", (0, 2)),
            ops=(Operation("regenerate", (0, 1)), Operation("extend", (2, 5), stream="av")),
        ),
        H3Spec(
            "Source-free regional edit.",
            4,
            regions=(Region("box", box=(0, 0, 10, 10)),),
            ops=(Operation("regenerate", (0, 1), "box"),),
        ),
        _edit(Operation("regenerate", (0, 1), stream="audio")),
    ],
    ids=(
        "future-source-region",
        "regenerate-and-extend",
        "source-free-regenerate",
        "audio-regenerate",
    ),
)
def test_unimplemented_sampling_combinations_fail_closed(spec: H3Spec) -> None:
    with pytest.raises(H3SpecError) as error:
        normalize_spec(spec)
    _assert_g21(error.value)


@pytest.mark.parametrize(
    "spec",
    [
        replace(_edit(), refs=(Reference("", "look.png"),)),
        replace(_edit(), refs=(Reference(None, "look.png"),)),
        replace(_edit(), regions=(Region([], box=(0, 0, 10, 10)),)),
        replace(_edit(), ops=(Operation("regenerate", (0, 1), region=[]),)),
        replace(_edit(), ops=(Operation("regenerate", (0, 1), refs=(None,)),)),
    ],
    ids=(
        "blank-ref",
        "nonstring-ref",
        "unhashable-region",
        "unhashable-region-use",
        "nonstring-ref-use",
    ),
)
def test_malformed_typed_names_have_coded_errors(spec: H3Spec) -> None:
    with pytest.raises(H3SpecError) as error:
        normalize_spec(spec)
    _assert_g21(error.value)


@pytest.mark.parametrize(
    "name", ("source_free_generate", "source_backed_regenerate", "source_backed_extend")
)
def test_normalization_and_planning_do_no_media_or_provider_work_and_keep_capabilities_unavailable(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("CPU canonical normalization attempted media or provider work")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    monkeypatch.setattr(Path, "read_bytes", forbidden)
    spec = normalize_spec(_typed_case(name))
    passes = spec.sampling_passes()
    assert len(passes) == 1
    assert passes[0].reads == ("none" if spec.source is None else "source")
    assert set(spec.capabilities) == {"generate", "extend", "regenerate", "preserve"}
    assert all(capability["available"] is False for capability in spec.capabilities.values())
    assert all("GPU proof" in capability["reason"] for capability in spec.capabilities.values())
    changed = spec.capabilities
    changed["generate"]["available"] = True
    assert spec.capabilities["generate"]["available"] is False
