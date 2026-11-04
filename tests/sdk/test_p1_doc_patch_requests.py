"""Validation for the P1 ``doc.patch`` request set and its real fixtures."""

from __future__ import annotations

import copy
import hashlib
import json
import math
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
CORPUS = Path(__file__).resolve().parent / "fixtures" / "p1_feasibility"


def _reject_nonfinite(value: str) -> None:
    raise ValueError(f"non-finite JSON constant: {value}")


def _assert_finite_json(value: Any) -> None:
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite JSON number")
    elif isinstance(value, dict):
        for child in value.values():
            _assert_finite_json(child)
    elif isinstance(value, list):
        for child in value:
            _assert_finite_json(child)


def _load_json(path: Path) -> Any:
    value = json.loads(
        path.read_text(encoding="utf-8"), parse_constant=_reject_nonfinite
    )
    _assert_finite_json(value)
    return value


REQUESTS = _load_json(CORPUS / "doc_patch_requests.json")
MANIFEST = _load_json(CORPUS / "manifest.json")
MANIFEST_CASES = MANIFEST["cases"]


def _fixture(vector: dict[str, Any]) -> dict[str, Any]:
    return _load_json(CORPUS / vector["source"]["fixture"])


def _source_case(vector: dict[str, Any]) -> dict[str, Any]:
    source = vector["source"]
    matches = [
        case
        for case in MANIFEST_CASES
        if case["fixture"] == source["fixture"]
        and case["provenance"] == source["provenance"]
    ]
    assert len(matches) == 1
    return matches[0]


def _internal_payload(fixture: dict[str, Any], timeline_id: str) -> dict[str, Any]:
    for local_id, source in fixture["bundle"]["source_mapping"]["shots"].items():
        if source["internal_timeline_revision_id"] == timeline_id:
            return fixture["bundle"]["shots"][local_id]["internal_timeline"]
    raise AssertionError(f"missing internal timeline {timeline_id}")


def _bundle_clip(bundle: dict[str, Any], occurrence_id: str, clip_id: str) -> dict[str, Any]:
    placement = next(
        item for item in bundle["placements"] if item["occurrence_id"] == occurrence_id
    )
    shot = bundle["shots"][placement["shot_id"]]
    return next(
        clip for clip in shot["internal_timeline"]["clips"] if clip["id"] == clip_id
    )


def _bundle_for_phase(fixture: dict[str, Any], phase: str) -> dict[str, Any]:
    if phase == "input":
        return fixture["bundle"]
    if phase == "output":
        return fixture["after_bundle"]
    raise AssertionError(f"unhandled bundle phase {phase}")


def _root_for_change_at(vector: dict[str, Any], ref: str, phase: str) -> Any:
    fixture = _fixture(vector)
    if ref == "parent":
        document = fixture[phase]
        return document["parent_revision"]["payload"]
    if ref.startswith(("internal:", "internal-revision:")):
        revision = ref.split(":", 1)[1]
        bundle = _bundle_for_phase(fixture, phase)
        for local_id, source in bundle["source_mapping"]["shots"].items():
            if source["internal_timeline_revision_id"] == revision:
                return bundle["shots"][local_id]["internal_timeline"]
        raise AssertionError(f"missing internal revision {revision}")
    if ref.startswith("occurrence:"):
        occurrence, _, clip = ref.removeprefix("occurrence:").partition("/clip:")
        return _bundle_clip(_bundle_for_phase(fixture, phase), occurrence, clip)
    raise AssertionError(f"unhandled fixture root {ref}")


def _root_for_change(vector: dict[str, Any], ref: str) -> Any:
    return _root_for_change_at(vector, ref, "input")


def _walk(root: Any, path: list[str]) -> Any:
    value = root
    for token in path:
        if isinstance(value, list):
            assert token.startswith("id:")
            identity = token.removeprefix("id:")
            value = next(item for item in value if item["id"] == identity)
        else:
            assert isinstance(value, dict)
            assert token in value, token
            value = value[token]
    return value


def _tagged(value: Any) -> dict[str, Any]:
    return {"state": "present", "value": copy.deepcopy(value)}


def _validate_tagged_state(value: Any) -> None:
    if value == {"state": "absent"}:
        return
    if (
        isinstance(value, dict)
        and set(value) == {"state", "value"}
        and value["state"] == "present"
    ):
        return
    raise AssertionError(f"invalid tagged state: {value!r}")


def _state(root: Any, path: list[str]) -> dict[str, Any]:
    try:
        return _tagged(_walk(root, path))
    except (AssertionError, KeyError, StopIteration):
        return {"state": "absent"}


def _expected_change(vector: dict[str, Any], change: dict[str, Any]) -> dict[str, Any]:
    root = _root_for_change(vector, change["ref"])
    if change["op"].startswith("list."):
        if change["op"] == "list.insert":
            return {"state": "absent"}
        collection = _walk(root, change["path"])
        item = next(item for item in collection if item["id"] == change["id"])
        return _tagged(item)
    return _state(root, change["path"])


def _actual_after(vector: dict[str, Any], change: dict[str, Any]) -> dict[str, Any]:
    root = _root_for_change_at(vector, change["ref"], "output")
    if change["op"] == "list.insert":
        collection = _walk(root, change["path"])
        return _tagged(next(item for item in collection if item["id"] == change["id"]))
    return _state(root, change["path"])


def _all_changes(vector: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        change
        for command in vector["request"]["commands"]
        for change in command["args"]["changes"]
    ]


def test_request_set_is_provenanced_to_the_merged_corpus() -> None:
    assert REQUESTS["status"] == (
        "provider-executed on real R1; membership-removal precedence red; "
        "Runtime HTTP/App H3 proof pending"
    )
    assert len(REQUESTS["vectors"]) == 8

    for vector in REQUESTS["vectors"]:
        source = vector["source"]
        case = _source_case(vector)
        canonical = CORPUS / source["fixture"]
        original = ROOT / source["source"]
        assert canonical.is_file()
        assert original.is_file()
        assert hashlib.sha256(canonical.read_bytes()).hexdigest() == source["source_sha256"]
        assert hashlib.sha256(original.read_bytes()).hexdigest() == source["source_sha256"]
        _load_json(original)
        assert case["source_sha256"] == source["source_sha256"]
        assert vector["scope"]["physical_root"]
        assert vector["unexecuted_provider_cases"]

    for vector in REQUESTS["vectors"]:
        if vector["id"] in {
            "params-absent-to-explicit-values",
            "params-null-to-object",
            "params-remove-and-preserve-opaque",
            "disjoint-field-merge",
        }:
            assert vector["scope"]["physical_root"] == "internal-revision:ir"
            assert all(
                change["ref"] == "internal-revision:ir"
                and change["path"][:2] == ["clips", "id:c"]
                for change in _all_changes(vector)
            )


def test_commands_use_the_contract_doc_patch_shape() -> None:
    for vector in REQUESTS["vectors"]:
        request = vector["request"]
        assert request["merge"] in {"fields", "atomic"}
        assert isinstance(request["reads"], list)
        assert request["commands"]
        for command in request["commands"]:
            assert command == {
                key: command[key]
                for key in ("schema_version", "op", "version", "select", "created", "args")
                if key in command
            }
            assert command["schema_version"] == 1
            assert command["op"] == "doc.patch"
            assert command["version"] == "1"
            assert command["select"] is None
            assert isinstance(command.get("created", []), list)
            assert command["args"].keys() == {"changes"}
            assert command["args"]["changes"]
            for change in command["args"]["changes"]:
                assert change["op"] in {"set", "remove", "list.insert", "list.remove", "list.move"}
                assert isinstance(change["ref"], str)
                assert isinstance(change["path"], list)
                assert all(isinstance(token, str) for token in change["path"])
                _validate_tagged_state(change["before"])
                if change["op"] == "set":
                    assert "value" in change
                if change["op"] == "remove":
                    assert change["before"]["state"] == "present"
                if change["op"].startswith("list."):
                    assert "id" in change
                    assert "order_before" in change
                    assert "after_id" in change or change["op"] == "list.remove"
                if change["op"] == "list.insert":
                    assert change["before"] == {"state": "absent"}
                    assert change["value"]["id"] == change["id"]


def test_guards_are_exact_values_from_the_fixture_inputs() -> None:
    for vector in REQUESTS["vectors"]:
        for change in _all_changes(vector):
            assert change["before"] == _expected_change(vector, change), (
                vector["id"],
                change,
            )


def test_expected_authored_deltas_match_the_submitted_operands() -> None:
    for vector in REQUESTS["vectors"]:
        expected = vector["expected"]
        changes = _all_changes(vector)
        authored = expected.get("authored_deltas", [])
        assert len(authored) == sum(change["op"] in {"set", "remove"} for change in changes)
        for delta, change in zip(authored, [c for c in changes if c["op"] in {"set", "remove"}]):
            assert delta["path"] == change["path"]
            assert delta.get("ref", change["ref"]) == change["ref"]
            actual_before = _expected_change(vector, change)
            actual_after = _actual_after(vector, change)
            if isinstance(delta["before"], dict) and "state" in delta["before"]:
                assert delta["before"] == actual_before
            else:
                assert actual_before == {"state": "present", "value": delta["before"]}
            if isinstance(delta["after"], dict) and "state" in delta["after"]:
                assert delta["after"] == actual_after
            else:
                assert actual_after == {"state": "present", "value": delta["after"]}


def test_tagged_state_validator_rejects_illegal_forms() -> None:
    for value in (
        {"state": "missing"},
        {"state": "present"},
        {"state": "absent", "value": None},
        {"state": "present", "value": 1, "extra": True},
        None,
    ):
        with pytest.raises(AssertionError):
            _validate_tagged_state(value)


def test_json_loader_rejects_nonfinite_nested_values() -> None:
    with pytest.raises(ValueError, match="non-finite JSON constant"):
        json.loads('{"nested": {"nan": NaN}}', parse_constant=_reject_nonfinite)
    overflow = json.loads('{"nested": {"overflow": 1e999}}')
    with pytest.raises(ValueError, match="non-finite JSON number"):
        _assert_finite_json(overflow)
    for value in (
        {"nested": [1, {"overflow": float("inf")}]},
        {"nested": {"underflow": float("-inf")}},
        {"nested": [{"nan": float("nan")}]},
    ):
        with pytest.raises(ValueError, match="non-finite"):
            _assert_finite_json(value)


def test_real_opaque_values_and_tagged_states_are_not_normalized() -> None:
    params_absent = next(v for v in REQUESTS["vectors"] if v["id"] == "params-absent-to-explicit-values")
    params_null = next(v for v in REQUESTS["vectors"] if v["id"] == "params-null-to-object")
    params_remove = next(v for v in REQUESTS["vectors"] if v["id"] == "params-remove-and-preserve-opaque")

    for vector in (params_absent, params_null, params_remove):
        fixture = _fixture(vector)
        before = _root_for_change_at(vector, "internal-revision:ir", "input")
        after = _root_for_change_at(vector, "internal-revision:ir", "output")
        before_clip = _walk(before, ["clips", "id:c"])
        after_clip = _walk(after, ["clips", "id:c"])
        for change in _all_changes(vector):
            if change["op"] == "set":
                assert _actual_after(vector, change) == _tagged(change["value"])
            elif change["op"] == "remove":
                assert _actual_after(vector, change) == {"state": "absent"}
        for key in ("app", "at", "from", "speed", "to"):
            assert after_clip[key] == before_clip[key]
        assert fixture["bundle"]["placements"] == fixture["after_bundle"]["placements"]

    assert params_absent["expected"]["tagged_states"] == {
        "params": "absent",
        "params.null": "absent",
        "params.old": "absent",
    }
    absent_input = _root_for_change_at(params_absent, "internal-revision:ir", "input")
    assert _state(absent_input, ["clips", "id:c", "params"]) == {"state": "absent"}
    assert _state(absent_input, ["clips", "id:c", "params", "null"]) == {"state": "absent"}
    assert _state(absent_input, ["clips", "id:c", "params", "old"]) == {"state": "absent"}

    null_change = params_null["request"]["commands"][0]["args"]["changes"][0]
    assert null_change["before"] == {"state": "present", "value": None}
    null_input = _root_for_change_at(params_null, "internal-revision:ir", "input")
    assert _state(null_input, ["clips", "id:c", "params"]) == {"state": "present", "value": None}

    assert params_remove["expected"]["opaque_preserved"] == {
        "params.opaque": {"keep": [False, {}, ""]},
        "params.x": 1,
        "params.y": None,
    }
    remove = params_remove["request"]["commands"][0]["args"]["changes"][0]
    assert remove["op"] == "remove"
    assert remove["before"] == {"state": "present", "value": None}
    remove_output = _root_for_change_at(params_remove, "internal-revision:ir", "output")
    assert _state(remove_output, ["clips", "id:c", "params", "old"]) == {"state": "absent"}
    assert _state(remove_output, ["clips", "id:c", "params", "null"]) == {"state": "present", "value": None}
    for key, value in params_remove["expected"]["opaque_preserved"].items():
        assert _walk(remove_output, ["clips", "id:c", *key.split(".")]) == value


def test_collection_vectors_guard_membership_and_order_without_execution() -> None:
    vector = next(v for v in REQUESTS["vectors"] if v["id"] == "ordered-list-move-and-order-refusal")
    fixture = _fixture(vector)
    parent = fixture["input"]["parent_revision"]["payload"]
    source_collection = parent["config"]["clips"]
    source_members = [item["id"] for item in source_collection]
    read = vector["request"]["reads"][0]
    change = vector["request"]["commands"][0]["args"]["changes"][0]
    assert change["op"] == "list.move"
    assert read["path"] == ["clips"]
    assert read["members"] == source_members
    assert read["order"] == source_members
    assert change["order_before"] == source_members
    assert change["before"] == _tagged(next(item for item in parent["clips"] if item["id"] == "clip-b"))
    moved_order = [change["id"], *[item for item in source_members if item != change["id"]]]
    assert vector["expected"]["order_after"] == moved_order
    assert vector["expected"]["membership_after"] == source_members
    assert vector["scenario"]["status"] == "constructed-unexecuted on real base"
    assert vector["scenario"]["base_fragment"] == {
        "fixture": "canonical_clip_trim.json",
        "phase": "input",
        "path": ["config", "clips"],
    }
    assert vector["expected"]["order_refusal"]["head_order"] == moved_order
    membership_head = [item for item in source_members if item != change["id"]]
    assert vector["expected"]["membership_refusal"]["head_members"] == membership_head
    assert vector["expected"]["order_refusal"]["code"] == "selection_changed"
    assert vector["expected"]["membership_refusal"]["code"] == "selection_changed"


def test_trim_and_created_witnesses_match_real_outputs() -> None:
    trim = next(v for v in REQUESTS["vectors"] if v["id"] == "trim-internal-clip-window")
    trim_fixture = _fixture(trim)
    trim_input = trim_fixture["input"]["parent_revision"]["payload"]
    trim_output = trim_fixture["output"]["parent_revision"]["payload"]
    input_clip = next(item for item in trim_input["clips"] if item["id"] == "clip-a")
    output_clip = next(item for item in trim_output["clips"] if item["id"] == "clip-a")
    effects = trim["expected"]["provider_effects_from_real_edit"]
    for path, expected in effects.items():
        tokens = path.split(".")
        assert _state(input_clip, tokens) == _tagged(expected["before"])
        actual_after = _state(output_clip, tokens)
        expected_after = (
            expected["after"]
            if isinstance(expected["after"], dict)
            else _tagged(expected["after"])
        )
        assert actual_after == expected_after
    assert trim_output["clips"][1] == trim_input["clips"][1]
    assert trim_output["config"]["opaque"] == trim_input["config"]["opaque"]
    for key, value in trim["expected"]["opaque_preserved"].items():
        if key == "config.opaque":
            actual = trim_output["config"]["opaque"]
        else:
            actual = output_clip["app"]["org.example"]
        assert actual == value

    created = next(v for v in REQUESTS["vectors"] if v["id"] == "created-track-and-clip-list-inserts")
    created_fixture = _fixture(created)
    created_input = created_fixture["input"]["parent_revision"]["payload"]
    created_output = created_fixture["output"]["parent_revision"]["payload"]
    assert [item["id"] for item in created_input["config"]["tracks"]] == []
    assert [item["id"] for item in created_input["config"]["clips"]] == []
    assert [item["id"] for item in created_output["config"]["tracks"]] == created["expected"]["orders"]["config.tracks"]
    assert [item["id"] for item in created_output["config"]["clips"]] == created["expected"]["orders"]["config.clips"]
    assert {item["id"] for item in created_output["clips"]} == {"first-clip"}
    assert created["expected"]["created"] == ["parent/track:first-track", "parent/clip:first-clip"]
    assert created["request"]["commands"][0]["created"] == ["parent/track:first-track"]
    assert created["request"]["commands"][1]["created"] == ["parent/clip:first-clip"]
    assert created_output["config"]["unknown"] == created["expected"]["opaque_preserved"]["config.unknown"]
    output_clip = created_output["config"]["clips"][0]
    assert output_clip["app"] == created["expected"]["opaque_preserved"]["config.clips[id:first-clip].app"]
    assert output_clip["nullable"] == created["expected"]["opaque_preserved"]["config.clips[id:first-clip].nullable"]
    assert output_clip["sourceURL"] == created["expected"]["opaque_preserved"]["config.clips[id:first-clip].sourceURL"]
    assert output_clip["from"] == created["expected"]["opaque_preserved"]["config.clips[id:first-clip].from"]


def test_disjoint_rebase_witnesses_match_real_head_and_rebased_fragments() -> None:
    vector = next(v for v in REQUESTS["vectors"] if v["id"] == "disjoint-field-merge")
    fixture = _fixture(vector)
    base = _bundle_clip(fixture["bundle"], "o", "c")
    head = _bundle_clip(fixture["head_bundle"], "o", "c")
    rebased = _bundle_clip(fixture["rebased_bundle"], "o", "c")
    change = vector["request"]["commands"][0]["args"]["changes"][0]
    assert change["before"] == _state(base, ["params", "x"])
    assert vector["expected"]["head_change_preserved"] == {
        "path": ["params", "y"],
        "before": base["params"]["y"],
        "after": head["params"]["y"],
    }
    assert vector["expected"]["exact_expected_fields"] == {
        "params.x": rebased["params"]["x"],
        "params.y": rebased["params"]["y"],
    }
    assert rebased["params"]["x"] == change["value"]
    assert rebased["params"]["y"] == head["params"]["y"]


def test_same_field_refusal_uses_real_base_and_labels_constructed_head() -> None:
    vector = next(v for v in REQUESTS["vectors"] if v["id"] == "same-field-refusal")
    base = _root_for_change(vector, "parent")
    change = vector["request"]["commands"][0]["args"]["changes"][0]
    assert change["before"] == _state(base, change["path"])
    head_change = vector["head"]["changes"][0]
    assert head_change["before"] == change["before"]["value"]
    assert head_change["after"] != head_change["before"]
    assert vector["head"]["provenance"] == "constructed-unexecuted on real base"
    assert vector["expected"]["reason"] == "value_conflict"
    assert vector["expected"]["code"] == "value_conflict"


def test_provider_execution_disposition_is_recorded() -> None:
    assert not any("executor" in vector for vector in REQUESTS["vectors"])
    assert not any("provider_result" in vector for vector in REQUESTS["vectors"])
    assert {vector["expected"]["status"] for vector in REQUESTS["vectors"]} == {
        "accepted",
        "rebased",
        "conflict",
    }
