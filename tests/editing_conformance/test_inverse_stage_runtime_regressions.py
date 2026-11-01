"""Authenticated Runtime proof for the five retained R2 inverse classes.

These are new named regressions.  They do not recreate or claim the lost
58-case probe; each case is a durable forward commit followed by the public
inverse path against the pinned Runtime store.
"""
from __future__ import annotations

import copy
import hashlib
import importlib
import inspect
from pathlib import Path

import pytest

from astrid.sdk.timeline._clip_commands import capture_clip_add
from astrid.sdk.timeline._command_state import ABSENT
from astrid.sdk.timeline.capture import capture_track_create
from astrid.sdk.timeline.commit import bundle_from_document, create_runtime_commit_service
from tests.editing_conformance.test_doc_patch_mirrors import (
    change,
    declared_reads,
    patch,
    split_patch,
)
from tests.editing_conformance.test_doc_patch_v2 import step
from tests.helpers.runtime import use_runtime

pytestmark = pytest.mark.integration

RUNTIME_COMMIT = "6634a17f3ffd885567ba3c91b6c7a560df7375a4"
AGENT_COMMIT = "a30e74e901a28b39c28facba3deb00d76f186e23"
AGENT_COMMIT_FILE_HASHES = {
    "astrid/sdk/timeline/commit.py": "2b81e46ac0f3553d1de812cf0533be2a9aecbd8cdfcaab118ac671eff75107b3",
    "astrid/sdk/timeline/inverse.py": "6c794734cb763c21cec386f764bc63b94f3a74b4dd9f8464136a285c628d0423",
}
RUNTIME_FILE_HASHES = {
    "runtime_protocol/timeline_commit_auth.py": "4ac2b8792fe0389afb6eb710a348c35fa196131a13cab175bd3646f7d2cc0aeb",
    "runtime_protocol/timeline_commit_store.py": "2a68990b180326585b7f0abaf1ee54c8ff9340e83663f17cd1efff1b2bd78a5c",
}

CASES = (
    pytest.param("4", ["clips"], False, id="case-4-two-step-clips-before"),
    pytest.param("16", ["clips"], True, id="case-16-two-step-clips-after"),
    pytest.param("28", ["config", "clips"], False, id="case-28-two-step-config-clips-before"),
    pytest.param("40", ["config", "clips"], True, id="case-40-two-step-config-clips-after"),
    pytest.param("49", None, None, id="case-49-native-whole"),
)


def _public_inverse(publications):
    """Build the public timeline.inverse wire command; no history is supplied."""
    return {
        "schema_version": 1,
        "op": "timeline.inverse",
        "version": "1",
        "select": None,
        "args": {"publications": list(publications)},
    }


def _origin_snapshot():
    names = (
        "astrid.sdk.timeline.commit",
        "astrid.sdk.timeline.inverse",
        "runtime_protocol.timeline_commit_auth",
        "runtime_protocol.timeline_commit_store",
    )
    result = {}
    for name in names:
        module = importlib.import_module(name)
        origin = Path(inspect.getsourcefile(module) or module.__file__).resolve()
        result[name] = (str(origin), hashlib.sha256(origin.read_bytes()).hexdigest())
    return result


def _assert_source_identity(snapshot):
    agent_commit, agent_inverse = snapshot["astrid.sdk.timeline.commit"], snapshot["astrid.sdk.timeline.inverse"]
    assert agent_commit[1] == AGENT_COMMIT_FILE_HASHES["astrid/sdk/timeline/commit.py"]
    assert agent_inverse[1] == AGENT_COMMIT_FILE_HASHES["astrid/sdk/timeline/inverse.py"]
    assert Path(agent_commit[0]).name == "commit.py"
    assert Path(agent_inverse[0]).name == "inverse.py"
    for module, relative in (
        ("runtime_protocol.timeline_commit_auth", "runtime_protocol/timeline_commit_auth.py"),
        ("runtime_protocol.timeline_commit_store", "runtime_protocol/timeline_commit_store.py"),
    ):
        origin, digest = snapshot[module]
        assert digest == RUNTIME_FILE_HASHES[relative]
        assert origin.endswith(relative)


def _durable_snapshot(service):
    return tuple(service.store.conn.iterdump())


def _full_request(service, project, commands, *, steps, label, reads, key, source, rationale, group_id):
    head = service.get_project_timeline(project, "main")
    return {
        "api_version": 1,
        "project_id": project,
        "timeline_id": "main",
        "base_revision_id": head["head_revision_id"],
        "base_publication_id": head["head_publication_id"],
        "commands": copy.deepcopy(commands),
        "label": label,
        "steps": copy.deepcopy(steps),
        "merge": "fields",
        "reads": copy.deepcopy(reads),
        "dry_run": False,
        "source": copy.deepcopy(source),
        "rationale": rationale,
        "group_id": group_id,
    }


def _submit(service, project, request, key):
    from runtime_protocol.timeline_commit_port import route_path

    return service.commit_timeline(
        project,
        "main",
        request,
        idempotency_key=key,
        publisher="r2-test-author",
        invocation_path=route_path(project, "main"),
        authorize=lambda: None,
    )


def _inverse_request(service, project, publication, *, label, group_id):
    command = _public_inverse([publication])
    return _full_request(
        service,
        project,
        [command],
        steps=[{"id": "whole-inverse", "label": label, "intent": "", "tags": [],
                "command_ordinals": [0], "patch": []}],
        label=label,
        reads=[],
        key="unused",
        source={"via": "python"},
        rationale="Guarded whole inverse regression",
        group_id=group_id,
    )


@pytest.fixture
def runtime_case(tmp_path, monkeypatch):
    runtime = use_runtime(commit=RUNTIME_COMMIT)
    import tests

    monkeypatch.syspath_prepend(str(runtime / "tests"))
    monkeypatch.setattr(tests, "__path__", [*tests.__path__, str(runtime / "tests")])
    from runtime_protocol.service import RuntimeService
    from runtime_protocol.store import RealmStore
    from runtime_protocol.timeline_commit_store import TimelineCommitStore
    from test_timeline_commit_acceptance import validate_prepared

    root = tmp_path / "realm"
    RealmStore.initialize(root).close()
    service = RuntimeService(root)
    project = service.create_project({"slug": "r2-known", "name": "R2 known cases"})["id"]
    service.create_timeline(project, "main", idempotency_key="timeline")
    commits = TimelineCommitStore(service.store, validate_prepared=validate_prepared)
    commits.bootstrap(project, "main", authorize=lambda: None)
    service.register_timeline_commit_provider(create_runtime_commit_service())

    # Build the baseline through the public provider/store path.  These are
    # ordinary authored births from the real empty bootstrap, not a fabricated
    # closure or directly accepted prepared history.
    document = commits.revision_closure(project, "main", commits.head(project, "main")["revision_id"])
    track = capture_track_create(
        bundle_from_document(document), "parent",
        {"id": "picture", "kind": "visual", "role": "output"},
    )
    tracked = _submit(
        service, project,
        _full_request(
            service, project, [track], steps=[step("create track", 0, track)],
            label="R2 baseline track", reads=[], key="unused",
            source={"via": "python"}, rationale="Ordinary baseline creation",
            group_id="r2-baseline-track",
        ),
        "baseline-track",
    )
    document = tracked["data"]["canonical_document"]
    clip_a = {"id": "clip-a", "clipType": "text", "track": "picture", "at": 1, "hold": 2}
    add_a = capture_clip_add(bundle_from_document(document), "parent", clip_a)
    added_a = _submit(
        service, project,
        _full_request(
            service, project, [add_a], steps=[step("create clip a", 0, add_a)],
            label="R2 baseline clip a", reads=[], key="unused",
            source={"via": "python"}, rationale="Ordinary baseline creation",
            group_id="r2-baseline-clip-a",
        ),
        "baseline-clip-a",
    )
    document = added_a["data"]["canonical_document"]
    clip_b = {"id": "clip-b", "clipType": "text", "track": "picture", "at": 5, "hold": 2}
    add_b = capture_clip_add(bundle_from_document(document), "parent", clip_b)
    added_b = _submit(
        service, project,
        _full_request(
            service, project, [add_b], steps=[step("create clip b", 0, add_b)],
            label="R2 baseline clip b", reads=[], key="unused",
            source={"via": "python"}, rationale="Ordinary baseline creation",
            group_id="r2-baseline-clip-b",
        ),
        "baseline-clip-b",
    )
    document = added_b["data"]["canonical_document"]
    output = patch(
        change(["config", "output"], ABSENT, {"file": "out.mp4"}),
        change(["unknown"], ABSENT, {}),
    )
    output_accepted = _submit(
        service, project,
        _full_request(
            service, project, [output], steps=[step("create output", 0, output)],
            label="R2 baseline output", reads=[], key="unused",
            source={"via": "python"}, rationale="Ordinary baseline creation",
            group_id="r2-baseline-output",
        ),
        "baseline-output",
    )
    document = output_accepted["data"]["canonical_document"]
    origin = _origin_snapshot()
    _assert_source_identity(origin)
    try:
        yield service, project, document, origin
    finally:
        service.close()


def _forward_case(document, path, output_first):
    if path is None:
        split = split_patch(document, ["config", "clips"], False)
        move = {
            "schema_version": 1, "op": "clip.move", "version": "1",
            "select": "parent/clip:clip-right", "args": {"on": 2.25},
        }
        return [split, move], [step("split", 0, split), step("move", 1, move)], []
    split = split_patch(document, path, output_first)
    note = {
        "schema_version": 1, "op": "doc.patch", "version": "1", "select": None,
        "args": {"changes": [{"op": "set", "ref": "parent", "path": ["unknown", "note"],
                                "before": ABSENT, "value": "kept"}]},
    }
    reads = declared_reads()
    return [split, note], [
        step("split", 0, split, reads=reads), step("note", 1, note),
    ], reads


@pytest.mark.parametrize("case_id,path,output_first", CASES)
def test_known_whole_inverse_authenticates_forward_history_and_exact_redo(
    runtime_case, case_id, path, output_first,
):
    service, project, document, origin = runtime_case
    assert origin == _origin_snapshot()
    _assert_source_identity(origin)
    commands, steps, reads = _forward_case(document, path, output_first)
    request = _full_request(
        service, project, commands, steps=steps, label=f"R2 known case {case_id}",
        reads=reads, key="unused", source={"via": "python"},
        rationale="Durable R2 known inverse regression", group_id=f"r2-known-{case_id}",
    )
    accepted = _submit(service, project, request, f"forward-{case_id}")
    assert set(accepted) == {"data", "receipt"}
    assert accepted["receipt"]["result"] == accepted["data"]
    assert accepted["data"]["status"] == "accepted"
    assert accepted["data"]["base_revision_id"] == request["base_revision_id"]
    assert accepted["data"]["base_publication_id"] == request["base_publication_id"]
    # Runtime returns the provider's canonical command copy (including its
    # explicit created=[] claim); the durable history and receipt must agree
    # exactly with that accepted copy.
    assert [command["op"] for command in accepted["data"]["commands"]] == [
        command["op"] for command in commands
    ]
    assert accepted["data"]["label"] == request["label"]
    assert accepted["data"]["merge_mode"] == "fields"
    assert accepted["data"].get("reads", request["reads"]) == reads
    assert accepted["data"]["source"] == request["source"]
    assert accepted["data"]["rationale"] == request["rationale"]
    assert accepted["data"]["group_id"] == request["group_id"]
    assert accepted["data"]["publisher"] == "r2-test-author"
    assert origin == _origin_snapshot()

    publication = accepted["receipt"]["receipt_id"]
    store = service._timeline_commits()[0]
    history = store.history_entry(project, "main", publication)
    assert history["publication_id"] == publication
    assert accepted["data"]["commands"] == [
        record["command"] for record in history["records"]["commands"]
    ]
    assert history["records"]["steps"] == accepted["data"]["steps"]
    assert history["records"]["label"] == request["label"]
    assert history["records"]["merge_mode"] == "fields"
    assert history["records"]["commands"][0]["reads"]
    assert history["records"]["commands"][0]["derived_writes"]
    assert history["records"]["commands"][0]["bindings"]
    assert history["records"]["steps"][0]["patch"]
    assert history["records"]["steps"][0]["dependencies"]
    assert any("write_range" in binding or "boundary" in binding
               for binding in history["records"]["commands"][0]["bindings"])
    assert history["records"]["commands"][0]["derived_writes"]
    forward_document = accepted["data"]["canonical_document"]
    payload = forward_document["parent_revision"]["payload"]
    assert payload["clips"] == payload["config"]["clips"]
    assert payload["clips"][1]["id"] == "clip-right"
    assert origin == _origin_snapshot()

    undo_request = _inverse_request(
        service, project, publication, label=f"R2 undo known case {case_id}",
        group_id=f"r2-known-{case_id}-undo",
    )
    undone = _submit(service, project, undo_request, f"undo-{case_id}")
    assert set(undone) == {"data", "receipt"}
    assert undone["receipt"]["result"] == undone["data"]
    assert undone["data"]["status"] == "accepted"
    assert undone["data"]["canonical_document"] == document
    assert undone["data"]["commands"][0]["op"] == undo_request["commands"][0]["op"]
    assert undone["data"]["steps"]
    undo_record = store.history_entry(project, "main", undone["receipt"]["receipt_id"])["records"]
    assert undo_record["undo_of"] == [publication]
    assert undo_record["commands"][0]["writes"]
    assert undo_record["commands"][0]["derived_writes"]
    assert undo_record["commands"][0]["bindings"]
    assert origin == _origin_snapshot()

    redo_request = _inverse_request(
        service, project, undone["receipt"]["receipt_id"], label=f"R2 redo known case {case_id}",
        group_id=f"r2-known-{case_id}-redo",
    )
    redone = _submit(service, project, redo_request, f"redo-{case_id}")
    assert redone["data"]["status"] == "accepted"
    assert redone["data"]["canonical_document"] == forward_document
    assert redone["receipt"]["result"] == redone["data"]
    assert origin == _origin_snapshot()


def _accepted_case(runtime_case):
    service, project, document, origin = runtime_case
    commands, steps, reads = _forward_case(document, ["clips"], False)
    request = _full_request(
        service, project, commands, steps=steps, label="R2 guard source", reads=reads,
        key="unused", source={"via": "python"},
        rationale="Guard counterpart", group_id="r2-guard",
    )
    result = _submit(service, project, request, "guard-forward")
    assert origin == _origin_snapshot()
    return service, project, result, origin


def test_known_inverse_refuses_forged_source_publication_and_step_without_writes(runtime_case):
    service, project, accepted, origin = _accepted_case(runtime_case)
    before = service.get_project_timeline(project, "main")
    durable_before = _durable_snapshot(service)
    forged = _inverse_request(service, project, "not-the-accepted-publication", label="forged source",
                             group_id="r2-forged-source")
    with pytest.raises(Exception) as error:
        _submit(service, project, forged, "forged-source")
    assert getattr(error.value, "details", {}).get("reason") in {"history_unavailable", "invalid_edit_command"}
    assert service.get_project_timeline(project, "main")["head_publication_id"] == before["head_publication_id"]
    assert _durable_snapshot(service) == durable_before
    actual = accepted["receipt"]["receipt_id"]
    forged_step = _inverse_request(service, project, actual, label="forged step", group_id="r2-forged-step")
    forged_step["commands"][0]["args"]["steps"] = {actual: ["not-a-real-step"]}
    before = service.get_project_timeline(project, "main")
    durable_before = _durable_snapshot(service)
    with pytest.raises(Exception) as error:
        _submit(service, project, forged_step, "forged-step")
    assert getattr(error.value, "details", {}).get("reason") == "invalid_steps"
    assert service.get_project_timeline(project, "main")["head_publication_id"] == before["head_publication_id"]
    assert _durable_snapshot(service) == durable_before
    assert origin == _origin_snapshot()


def test_known_inverse_refuses_native_write_and_aba_without_writes(runtime_case):
    service, project, accepted, origin = _accepted_case(runtime_case)
    from astrid.sdk.timeline.capture import capture_move
    from astrid.sdk.timeline.commit import bundle_from_document

    head = service.get_project_timeline(project, "main")
    current = service._timeline_commits()[0].revision_closure(project, "main", head["head_revision_id"])
    native = capture_move(bundle_from_document(current), "parent/clip:clip-right", 3)
    native_request = _full_request(
        service, project, [native], steps=[step("native", 0, native)], label="R2 native guard",
        reads=[], key="unused", source={"via": "python"}, rationale="Native guard counterpart",
        group_id="r2-native-guard",
    )
    _submit(service, project, native_request, "native-intervening")
    head = service.get_project_timeline(project, "main")
    current = service._timeline_commits()[0].revision_closure(project, "main", head["head_revision_id"])
    native_return = capture_move(bundle_from_document(current), "parent/clip:clip-right", 2)
    native_return_request = _full_request(
        service, project, [native_return], steps=[step("native-return", 0, native_return)], label="R2 native ABA",
        reads=[], key="unused", source={"via": "python"}, rationale="Native ABA counterpart",
        group_id="r2-native-aba",
    )
    _submit(service, project, native_return_request, "native-aba-return")
    before = service.get_project_timeline(project, "main")
    durable_before = _durable_snapshot(service)
    inverse_request = _inverse_request(service, project, accepted["receipt"]["receipt_id"], label="native refusal",
                                       group_id="r2-native-refusal")
    with pytest.raises(Exception) as error:
        _submit(service, project, inverse_request, "native-refusal")
    assert getattr(error.value, "details", {}).get("reason") == "intervening_write"
    assert service.get_project_timeline(project, "main")["head_publication_id"] == before["head_publication_id"]
    assert _durable_snapshot(service) == durable_before
    assert origin == _origin_snapshot()
