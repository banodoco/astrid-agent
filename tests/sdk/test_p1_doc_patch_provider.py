"""Execute the collected P1 ``doc.patch`` vectors against the real R1 provider."""

from __future__ import annotations

import copy
import json
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

from astrid.sdk.timeline.commands import execute
from astrid.sdk.timeline.commit import (
    _digest,
    _manifest,
    bundle_from_document,
    create_runtime_commit_service,
    document_from_bundle,
)
from tests.sdk.test_timeline_commit_provider import (
    CommitRejected,
    DetachedPort,
    DetachedRealm,
    inverse,
)
from tests.sdk.test_timeline_commit_provider import (
    runtime_abi as runtime_abi,
)
from tests.sdk.test_timeline_commit_provider_document import document_fixture

CORPUS = Path(__file__).resolve().parent / "fixtures" / "p1_feasibility"
REQUESTS = json.loads((CORPUS / "doc_patch_requests.json").read_text(encoding="utf-8"))
VECTORS = {vector["id"]: vector for vector in REQUESTS["vectors"]}


def _runtime_document_for_bundle(bundle: dict[str, Any]) -> dict[str, Any]:
    """Project the fixture's complete pinned bundle into Runtime's document ABI."""
    source_shots = bundle["source_mapping"]["shots"]
    parent = copy.deepcopy(bundle["parent"])
    parent["occurrences"] = []
    for placement in bundle["placements"]:
        source = source_shots[placement["shot_id"]]
        parent["occurrences"].append(
            {
                **copy.deepcopy(placement),
                "shot_id": source["shot_id"],
                "shot_revision_id": source["revision_id"],
            }
        )

    seen_shots: set[str] = set()
    shots: list[dict[str, Any]] = []
    internals: list[dict[str, Any]] = []
    for local_id, source in source_shots.items():
        if source["revision_id"] in seen_shots:
            continue
        seen_shots.add(source["revision_id"])
        local = bundle["shots"][local_id]
        internal_revision = source["internal_timeline_revision_id"]
        shots.append(
            {
                "shot_id": source["shot_id"],
                "revision_id": source["revision_id"],
                "internal_timeline_revision_id": internal_revision,
                "content_digest": _digest(local["payload"]),
                "payload": copy.deepcopy(local["payload"]),
            }
        )
        internals.append(
            {
                "timeline_id": source["internal_timeline_id"],
                "revision_id": internal_revision,
                "content_digest": _digest(local["internal_timeline"]),
                "payload": copy.deepcopy(local["internal_timeline"]),
            }
        )

    return {
        "schema_version": 1,
        "project_id": bundle["project_id"],
        "timeline_id": bundle["timeline_id"],
        "parent_revision": {
            "revision_id": bundle["base_parent"]["revision_id"],
            "content_digest": _digest(parent),
            "payload": parent,
        },
        "shot_revisions": sorted(shots, key=lambda row: (row["shot_id"], row["revision_id"])),
        "internal_timeline_revisions": sorted(
            internals, key=lambda row: (row["timeline_id"], row["revision_id"])
        ),
        "dependency_manifest": {
            "shots": [
                {
                    key: row[key]
                    for key in (
                        "shot_id",
                        "revision_id",
                        "internal_timeline_revision_id",
                        "content_digest",
                    )
                }
                for row in sorted(shots, key=lambda row: (row["shot_id"], row["revision_id"]))
            ],
            "internal_timelines": [
                {
                    key: row[key]
                    for key in ("timeline_id", "revision_id", "content_digest")
                }
                for row in sorted(
                    internals, key=lambda row: (row["timeline_id"], row["revision_id"])
                )
            ],
            "media": [],
            "documents": [],
        },
    }


def _fixture(vector_id: str) -> dict[str, Any]:
    vector = VECTORS[vector_id]
    return json.loads(
        (CORPUS / vector["source"]["fixture"]).read_text(encoding="utf-8")
    )


def _bundle(vector_id: str) -> dict[str, Any]:
    fixture = _fixture(vector_id)
    if "input" in fixture:
        return bundle_from_document(fixture["input"])
    return bundle_from_document(_runtime_document_for_bundle(copy.deepcopy(fixture["bundle"])))


def _service_call(
    realm: DetachedRealm,
    commands: list[dict[str, Any]],
    *,
    controls: dict[str, Any],
    base: dict[str, str] | None = None,
) -> dict[str, Any]:
    context = realm.context(commands, base=base)
    current_document = realm.documents[realm.head["revision_id"]]
    context["request"].update(
        project_id=current_document["project_id"],
        timeline_id=current_document["timeline_id"],
    )
    context["request"].update(copy.deepcopy(controls))
    context["invocation"]["body"] = copy.deepcopy(context["request"])
    context["request_hash"] = _digest(context["invocation"])
    return create_runtime_commit_service().commit(context, DetachedPort(realm, context))


def _realm_for(vector_id: str) -> DetachedRealm:
    document = document_from_bundle(_bundle(vector_id))
    return DetachedRealm(document)


def _round_trip(value: Any) -> Any:
    return json.loads(json.dumps(value, allow_nan=False))


@pytest.mark.parametrize("vector_id", list(VECTORS))
def test_collected_vectors_execute_against_real_r1(vector_id: str) -> None:
    """Keep every collected success as a real provider assertion."""
    vector = VECTORS[vector_id]
    fixture = _fixture(vector_id)
    plan = execute(
        _bundle(vector_id),
        vector["request"]["commands"],
        commit_request=vector["request"],
    )

    assert plan.records
    assert _round_trip(plan.bundle) == plan.bundle

    if vector_id in {
        "trim-internal-clip-window",
        "created-track-and-clip-list-inserts",
    }:
        actual = document_from_bundle(plan.bundle)
        expected = fixture["output"]
        assert actual["parent_revision"]["payload"] == expected["parent_revision"]["payload"]
    elif vector_id in {
        "params-absent-to-explicit-values",
        "params-null-to-object",
        "params-remove-and-preserve-opaque",
        "disjoint-field-merge",
    }:
        expected_clip = next(
            clip
            for clip in next(iter(fixture["after_bundle"]["shots"].values()))[
                "internal_timeline"
            ]["clips"]
            if clip["id"] == "c"
        )
        actual_clips = [
            next(clip for clip in shot["internal_timeline"]["clips"] if clip["id"] == "c")
            for shot in plan.bundle["shots"].values()
        ]
        assert actual_clips == [expected_clip] * len(actual_clips)
    elif vector_id == "ordered-list-move-and-order-refusal":
        actual = document_from_bundle(plan.bundle)["parent_revision"]["payload"]
        assert [row["id"] for row in actual["clips"]] == ["clip-b", "clip-a"]


@pytest.mark.parametrize(
    "vector_id",
    ["trim-internal-clip-window", "created-track-and-clip-list-inserts"],
)
def test_parent_vectors_use_runtime_commit_service_and_inverse(
    vector_id: str, runtime_abi: Any
) -> None:
    vector = VECTORS[vector_id]
    fixture = _fixture(vector_id)
    realm = _realm_for(vector_id)
    result = _service_call(
        realm,
        vector["request"]["commands"],
        controls=vector["request"],
    )

    assert result["data"]["status"] == vector["expected"]["status"]
    actual = result["data"]["canonical_document"]
    assert actual["parent_revision"]["payload"] == fixture["output"]["parent_revision"]["payload"]
    assert _round_trip(actual) == actual

    if vector_id == "created-track-and-clip-list-inserts":
        undone = _service_call(
            realm,
            [inverse([result["receipt"]["receipt_id"]])],
            controls={},
        )
        original = fixture["input"]["parent_revision"]["payload"]
        assert undone["data"]["canonical_document"]["parent_revision"]["payload"] == original


@pytest.mark.parametrize(
    "vector_id",
    [
        "params-absent-to-explicit-values",
        "params-null-to-object",
        "params-remove-and-preserve-opaque",
    ],
)
def test_child_vectors_use_runtime_commit_service_and_preserve_shared_alias(
    vector_id: str, runtime_abi: Any
) -> None:
    vector = VECTORS[vector_id]
    fixture = _fixture(vector_id)
    realm = _realm_for(vector_id)
    result = _service_call(realm, vector["request"]["commands"], controls=vector["request"])

    assert result["data"]["status"] == vector["expected"]["status"]
    document = result["data"]["canonical_document"]
    expected_clip = next(
        clip
        for clip in next(iter(fixture["after_bundle"]["shots"].values()))["internal_timeline"][
            "clips"
        ]
        if clip["id"] == "c"
    )
    internal_clips = [
        next(clip for clip in row["payload"]["clips"] if clip["id"] == "c")
        for row in document["internal_timeline_revisions"]
    ]
    assert internal_clips == [expected_clip]
    assert _round_trip(document) == document


def test_disjoint_child_merge_executes_through_service_on_complete_closure(
    runtime_abi: Any,
) -> None:
    vector = VECTORS["disjoint-field-merge"]
    realm = _realm_for("disjoint-field-merge")
    original_head = copy.deepcopy(realm.head)
    competing = _doc_patch(
        {
            "op": "set",
            "ref": "internal-revision:ir",
            "path": ["clips", "id:c", "params", "y"],
            "before": _present(None),
            "value": False,
        }
    )
    competing_result = _service_call(
        realm, [competing], controls={"merge": "fields", "reads": []}
    )
    result = _service_call(
        realm,
        vector["request"]["commands"],
        controls=vector["request"],
        base=original_head,
    )

    assert competing_result["data"]["status"] == "accepted"
    assert result["data"]["status"] == vector["expected"]["status"]
    assert result["data"]["rebased_onto"]["publication_id"] == (
        competing_result["receipt"]["receipt_id"]
    )
    document = result["data"]["canonical_document"]
    clip = next(
        clip
        for row in document["internal_timeline_revisions"]
        for clip in row["payload"]["clips"]
        if clip["id"] == "c"
    )
    assert clip["params"]["x"] == 2
    assert clip["params"]["y"] is False
    assert clip["params"]["opaque"] == {"keep": [False, {}, ""]}
    assert _round_trip(document) == document


@pytest.mark.parametrize(
    ("change", "created"),
    [
        (
            {
                "op": "list.insert",
                "ref": "internal-revision:ir",
                "path": ["clips"],
                "id": "new",
                "before": {"state": "absent"},
                "order_before": ["c"],
                "after_id": "c",
                "value": {
                    "id": "new",
                    "track": "picture",
                    "at": 0,
                    "from": 0,
                    "to": 1,
                    "app": {"astrid": {"on": "new-anchor"}},
                },
            },
            ["occurrence:o/clip:new", "occurrence:sibling/clip:new"],
        ),
        (
            {
                "op": "set",
                "ref": "internal-revision:ir",
                "path": ["clips", "id:c", "app", "astrid", "on"],
                "before": {"state": "present", "value": "old-anchor"},
                "value": "new-anchor",
            },
            [],
        ),
        (
            {
                "op": "set",
                "ref": "internal-revision:ir",
                "path": ["clips", "id:c", "from"],
                "before": {"state": "present", "value": 2},
                "value": 3,
            },
            [],
        ),
    ],
    ids=["create", "modify", "invalidate-by-target-change"],
)
def test_new_or_invalidated_legacy_timing_is_422_and_atomic(
    change: dict[str, Any], created: list[str],
) -> None:
    realm = _realm_for("params-absent-to-explicit-values")
    before_head = copy.deepcopy(realm.head)
    before_entries = len(realm.entries)
    command = _doc_patch(change)
    if created:
        command["created"] = created

    with pytest.raises(CommitRejected) as error:
        _service_call(realm, [command], controls={"merge": "fields", "reads": []})

    assert error.value.failure["http_status"] == 422
    assert error.value.failure["details"]["reason"] == "invalid_document"
    assert realm.head == before_head
    assert len(realm.entries) == before_entries


def test_unchanged_unresolved_legacy_timing_is_retained_for_unrelated_edit() -> None:
    realm = _realm_for("params-absent-to-explicit-values")
    result = _service_call(
        realm,
        VECTORS["params-absent-to-explicit-values"]["request"]["commands"],
        controls=VECTORS["params-absent-to-explicit-values"]["request"],
    )
    assert result["data"]["status"] == "accepted"
    clips = [
        clip
        for row in result["data"]["canonical_document"]["internal_timeline_revisions"]
        for clip in row["payload"]["clips"]
        if clip["id"] == "c"
    ]
    assert clips and all(
        clip["app"]["astrid"]["on"] == "old-anchor"
        and clip["app"]["astrid"]["until"] == "end"
        for clip in clips
    )


def test_unchanged_valid_legacy_child_timing_uses_rational_compiler_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from astrid.sdk.timeline import _commit_timing_validation as timing_validation

    bundle = bundle_from_document(document_fixture(shared=False))
    clips = bundle["shots"]["chapter-shot"]["internal_timeline"]["clips"]
    clips[0]["app"]["astrid"] = {"cut": "c1"}
    clips[1]["app"]["astrid"] = {"cut": "c1", "on": "c1"}
    document = document_from_bundle(bundle)
    internal_revision = document["internal_timeline_revisions"][0]["revision_id"]
    realm = DetachedRealm(document)
    original_compile = timing_validation.compile_timing
    compiled = []

    def capture(inputs):
        result = original_compile(inputs)
        compiled.append((inputs, result))
        return result

    monkeypatch.setattr(timing_validation, "compile_timing", capture)
    result = _service_call(
        realm,
        [_doc_patch({
            "op": "set",
            "ref": f"internal-revision:{internal_revision}",
            "path": ["clips", "id:clip-b", "params"],
            "before": {"state": "absent"},
            "value": {"note": "unrelated"},
        })],
        controls={"merge": "fields", "reads": []},
    )

    assert result["data"]["status"] == "accepted"
    inputs, timing = compiled[-1]
    assert inputs.legacy_declarations == ()
    assert timing.timings[("occ-a", "chapter-shot", "clip-b")]["start"] == Fraction(29029, 30000)


@pytest.mark.parametrize(
    ("carrier", "declaration"),
    [
        ({"beats": [0, 1, 2]}, "beat 1 after c1"),
        ({"beats": {"downbeats": [0, 1, 2]}}, "downbeat 1 after c1"),
    ],
)
def test_shared_physical_shots_keep_valid_beat_inputs_in_provider_timing(
    carrier: dict[str, Any], declaration: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from astrid.sdk.timeline import _commit_timing_validation as timing_validation
    from astrid.sdk.timeline._commit_timing_validation import _legacy_rows

    document = document_fixture(shared=True)
    internal = document["internal_timeline_revisions"][0]
    internal["payload"]["tracks"].append({"id": "audio", "kind": "audio"})
    internal["payload"]["clips"][0]["track"] = "audio"
    internal["payload"]["clips"][0]["app"]["astrid"] = {"cut": "c1", **carrier}
    internal["payload"]["clips"][1]["app"]["astrid"] = {
        "cut": "c1", "on": declaration,
    }
    internal["content_digest"] = _digest(internal["payload"])

    second_shot = copy.deepcopy(document["shot_revisions"][0])
    second_shot.update(revision_id="shot-2", internal_timeline_revision_id="internal-1")
    second_shot["payload"]["internal_timeline_revision_id"] = "internal-1"
    second_shot["payload"]["name"] = "Another shot revision"
    second_shot["content_digest"] = _digest(second_shot["payload"])
    document["shot_revisions"].append(second_shot)
    document["parent_revision"]["payload"]["occurrences"][1]["shot_revision_id"] = "shot-2"
    document["parent_revision"]["content_digest"] = _digest(
        document["parent_revision"]["payload"]
    )
    document["dependency_manifest"] = _manifest(
        document["shot_revisions"], document["internal_timeline_revisions"],
        document["dependency_manifest"]["media"], [],
    )

    captured = []
    original_compile = timing_validation.compile_timing

    def capture(inputs):
        captured.append(inputs)
        return original_compile(inputs)

    monkeypatch.setattr(timing_validation, "compile_timing", capture)
    realm = DetachedRealm(document)
    result = _service_call(
        realm,
        [_doc_patch({
            "op": "set",
            "ref": "parent",
            "path": ["name"],
            "before": {"state": "present", "value": "Renamed Film"},
            "value": "Unrelated parent name",
        })],
        controls={"merge": "fields", "reads": []},
    )

    assert result["data"]["status"] == "accepted"
    assert all(
        declaration not in [row[2] for row in inputs.legacy_declarations]
        for inputs in captured
    )
    assert any(
        any(
            clip.get("app", {}).get("astrid", {}).get("on") == declaration
            for _, clip in _legacy_rows(inputs.bundle)
        )
        for inputs in captured
    )


def _doc_patch(change: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "op": "doc.patch",
        "version": "1",
        "select": None,
        "args": {"changes": [change]},
    }


def _present(value: Any) -> dict[str, Any]:
    return {"state": "present", "value": copy.deepcopy(value)}


@pytest.mark.parametrize(
    ("vector_id", "intervening", "expected_reason"),
    [
        (
            "same-field-refusal",
            {
                "op": "set",
                "ref": "parent",
                "path": ["clips", "id:clip-a", "from"],
                "before": _present(2),
                "value": 3,
            },
            "value_conflict",
        ),
        (
            "ordered-list-move-and-order-refusal",
            {
                "op": "list.move",
                "ref": "parent",
                "path": ["clips"],
                "id": "clip-b",
                "before": _present(
                    {
                        "id": "clip-b",
                        "track": "picture",
                        "asset": "picture",
                        "clipType": "media",
                        "at": 5,
                        "hold": 2,
                        "app": {"org.example": {"literal": "clip-a", "clear": None}},
                    }
                ),
                "order_before": ["clip-a", "clip-b"],
                "after_id": None,
            },
            "selection_changed",
        ),
        (
            "ordered-list-move-and-order-refusal",
            {
                "op": "list.remove",
                "ref": "parent",
                "path": ["clips"],
                "id": "clip-b",
                "before": _present(
                    {
                        "id": "clip-b",
                        "track": "picture",
                        "asset": "picture",
                        "clipType": "media",
                        "at": 5,
                        "hold": 2,
                        "app": {"org.example": {"literal": "clip-a", "clear": None}},
                    }
                ),
                "order_before": ["clip-a", "clip-b"],
            },
            "selection_changed",
        ),
    ],
)
def test_provider_refuses_stale_same_field_membership_and_order(
    vector_id: str,
    intervening: dict[str, Any],
    expected_reason: str,
    runtime_abi: Any,
) -> None:
    vector = VECTORS[vector_id]
    realm = _realm_for(vector_id)
    original_head = copy.deepcopy(realm.head)
    _service_call(realm, [_doc_patch(intervening)], controls={"merge": "fields", "reads": []})
    head_after_intervening = copy.deepcopy(realm.head)
    entries_after_intervening = len(realm.entries)

    with pytest.raises(CommitRejected) as error:
        _service_call(
            realm,
            vector["request"]["commands"],
            controls=vector["request"],
            base=original_head,
        )

    assert error.value.failure["details"]["reason"] == expected_reason
    assert realm.head == head_after_intervening
    assert len(realm.entries) == entries_after_intervening
