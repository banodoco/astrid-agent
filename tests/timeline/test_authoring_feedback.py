from __future__ import annotations

import argparse
import copy
import importlib.util
import json
import shlex
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.core.timeline.authoring_bundle import open_authoring_bundle, publish_authoring_candidate
from astrid.core.timeline.authoring_feedback import authoring_change_summary, publication_feedback
from astrid.packs.timeline.cli import _configure_diff, _configure_show, _configure_visualize
from astrid.sdk.authoring_remote import AuthoringRemoteError, TargetBoundAuthoringBundle
from astrid.sdk.timeline.commit import bundle_from_document, document_from_bundle
from astrid.sdk.timeline.emitter import DocumentPatchError, emit_document_commands
from astrid.sdk.timeline_editing import duplicate_authoring_shot, remove_authoring_shot
from astrid.sdk.workspace_client import WorkspaceClientError
from tests.editing_conformance.test_document_patch_emitter_engine import source as emitter_bundle
from tests.sdk.test_timeline_commit_provider import DetachedRealm
from tests.sdk.test_timeline_commit_provider_document import document_fixture
from tests.timeline.test_authoring_bundle import NEW, OLD, _closure, _digest, _runtime_document


def bundle(*, images=1, shared=False):
    parent, shots, internals = _closure(shared=shared)
    internal = internals[0]["payload"]
    payload = shots[0]["payload"]
    for index in range(2, images + 1):
        clip = copy.deepcopy(internal["clips"][0])
        clip.update(id=f"picture-{index}", at=index - 1)
        internal["clips"].append(clip)
    internals[0]["content_digest"] = _digest(internal)
    shots[0]["content_digest"] = _digest(payload)
    work = open_authoring_bundle(parent, shot_revisions=shots, internal_timeline_revisions=internals)
    work["source_mapping"]["_runtime_document"] = _runtime_document(parent, shots, internals)
    return work


def replace_images(work):
    shot = next(iter(work["shots"].values()))
    shot["internal_timeline"]["registry"]["assets"]["old-picture"]["media_id"] = NEW
    shot["payload"]["items"][0]["media_id"] = NEW
    return shot


class Writer:
    def __init__(self, document):
        self.calls = []
        self.document = document

    def _scoped_document(self, project_id, timeline_id):
        document = copy.deepcopy(self.document)
        document["project_id"] = project_id
        document["timeline_id"] = timeline_id
        return document

    def _history(self, project_id, timeline_id):
        document = self._scoped_document(project_id, timeline_id)
        return [{
            "publication_id": "publication-1",
            "revision_id": document["parent_revision"]["revision_id"],
            "receipt": {
                "receipt_id": "publication-1",
                "result": {"canonical_document": document},
            },
        }]

    def list_timeline_commits(self, project_id, timeline_id, *, cursor=None, limit=100):
        assert cursor is None and limit == 100
        return {"data": {"items": self._history(project_id, timeline_id), "next_cursor": None}}

    def get_project_parent_composition_revision(self, project_id, timeline_id, revision):
        assert revision == "parent-1"
        document = self._scoped_document(project_id, timeline_id)
        return {"data": {
            **copy.deepcopy(document["parent_revision"]),
            "project_id": project_id,
            "timeline_id": timeline_id,
            "dependency_manifest": copy.deepcopy(document["dependency_manifest"]),
        }}

    def get_project_shot_revision(self, project_id, shot_id, revision):
        row = next(row for row in self.document["shot_revisions"] if row["revision_id"] == revision)
        assert row["shot_id"] == shot_id
        return {"data": {**copy.deepcopy(row), "project_id": project_id}}

    def get_project_timeline_revision(self, project_id, timeline_id, revision):
        row = next(row for row in self.document["internal_timeline_revisions"] if row["revision_id"] == revision)
        assert row["timeline_id"] == timeline_id
        return {"data": {**copy.deepcopy(row), "project_id": project_id}}

    def get_project_media_selection(self, project_id, selection):
        for row in self.document["internal_timeline_revisions"]:
            for asset, value in row["payload"].get("registry", {}).get("assets", {}).items():
                if (asset == selection.get("asset")
                        and value.get("generationId") == selection.get("generation_id")
                        and value.get("variantId") == selection.get("variant_id")):
                    return {"data": {
                        "asset": asset,
                        "generation_id": selection["generation_id"],
                        "variant_id": selection["variant_id"],
                        "object_id": value["media_id"],
                        "catalog_digest": value.get("catalog_digest", "catalog-fixture"),
                    }}
        raise AssertionError(f"missing fixture media selection: {selection}")

    def commit_timeline(self, project_id, timeline_id, body, *, idempotency_key):
        self.calls.append((project_id, timeline_id, body, idempotency_key))
        assert body["base_revision_id"] == "parent-1"
        return {"data": {
            "status": "accepted",
            "accepted_revision_id": "saved-revision",
            "previous_revision_id": "parent-1",
            "canonical_document": copy.deepcopy(self.document),
        }, "receipt": {"receipt_id": "saved-receipt"}}


def publish(work, *, imports=()):
    document = work["source_mapping"]["_runtime_document"]
    document["project_id"] = work["project_id"]
    document["timeline_id"] = work["timeline_id"]
    writer = Writer(document)
    result = publish_authoring_candidate(work, writer, idempotency_key="feedback-test", media_imports=imports)
    assert len(writer.calls) == 1
    assert result["publication"]["new_head"] == "saved-revision"
    return result


def test_three_changed_image_selections_with_no_catalog_imports():
    work = bundle(images=3)
    replace_images(work)
    result = publish(work)
    assert result["update"].startswith("Updated 3 image selections.")
    assert result["summary"]["media_selections"]["image"] == {
        "updated": 3, "replaced": 3, "added": 0, "removed": 0,
    }
    assert result["summary"]["catalog_media"] == {"imported": 0, "reused": 0}
    assert result["summary"]["new_head"] == "saved-revision"


def test_saved_pair_action_quotes_receipt_ids_and_parses_exact_supported_flags():
    feedback = publication_feedback(
        authoring_change_summary(bundle()),
        {"old_head": "old head; $(no)", "new_head": "new 'head'"},
        project_id="project 'one'", timeline_id="timeline; two",
    )
    action = next(row for row in feedback["next_actions"]
                  if row["scope"] == "saved_revision_pair")
    parser = argparse.ArgumentParser()
    _configure_diff(parser)
    parsed = parser.parse_args(shlex.split(action["command"])[5:])
    assert parsed.project == "project 'one'"
    assert parsed.ref == "timeline; two"
    assert parsed.from_revision == "old head; $(no)"
    assert parsed.to_revision == "new 'head'"
    assert parsed.format == "readable"


def test_receipt_without_old_head_does_not_fabricate_saved_diff_pair():
    feedback = publication_feedback(authoring_change_summary(bundle()),
                                    {"new_head": "saved"}, project_id="project-1", timeline_id="main")
    assert all(row["scope"] != "saved_revision_pair" for row in feedback["next_actions"])
    assert "--from-revision" not in feedback["update"]


def test_registry_payload_and_selector_mirrors_count_one_selection():
    work = bundle()
    shot = replace_images(work)
    timeline = shot["internal_timeline"]
    timeline["registry"]["assets"]["new-picture"] = timeline["registry"]["assets"].pop("old-picture")
    timeline["clips"][0]["asset"] = "new-picture"
    result = publish(work)
    assert result["summary"]["media_selections"]["image"]["updated"] == 1
    assert result["update"].startswith("Updated 1 image selection.")


def test_new_ids_are_additions_and_array_shifts_do_not_replace_surviving_clips():
    work = bundle()
    shot = next(iter(work["shots"].values()))
    timeline = shot["internal_timeline"]
    previous = timeline["clips"].pop(0)
    timeline["registry"]["assets"]["new-picture"] = {"media_id": NEW, "type": "image"}
    for index in range(3):
        clip = dict(previous, id=f"new-{index}", asset="new-picture", at=index, hold=1)
        timeline["clips"].insert(index, clip)
    shot["payload"]["items"][0]["media_id"] = NEW
    result = publish(work)
    assert result["update"].startswith("Updated 3 image selections. Removed 1 image selection.")
    assert result["summary"]["media_selections"]["image"] == {
        "updated": 3, "replaced": 0, "added": 3, "removed": 1,
    }
    assert result["summary"]["media_selections"]["audio"]["updated"] == 0


def test_alias_equivalence_and_clip_reorder_do_not_claim_new_images():
    work = bundle()
    timeline = next(iter(work["shots"].values()))["internal_timeline"]
    timeline["registry"]["assets"]["alias"] = {"media_id": OLD.removeprefix("sha256:"), "type": "image"}
    timeline["clips"][0]["asset"] = "alias"
    timeline["clips"].reverse()
    result = publish(work)
    assert result["summary"]["media_selections"]["image"]["updated"] == 0
    assert result["update"].startswith("Other properties changed.")


def test_timing_fields_on_one_clip_count_one_adjustment_and_no_images():
    work = bundle()
    clip = next(iter(work["shots"].values()))["internal_timeline"]["clips"][0]
    clip.update(at=0.01, hold=0.99)
    result = publish(work)
    assert result["update"].startswith("Adjusted timing on 1 clip.")
    assert result["summary"]["timing_changes"] == {"clips": 1, "occurrences": 0}
    assert result["summary"]["media_selections"]["image"]["updated"] == 0


def test_document_emitter_emits_guarded_parent_timing_patch():
    work = bundle()
    base = copy.deepcopy(work)
    work["parent"]["clips"][0]["at"] = 4
    work["parent"]["config"]["clips"][0]["at"] = 4

    commands = emit_document_commands(base, work)

    assert [command["op"] for command in commands] == ["doc.patch"]
    assert commands[0]["args"]["changes"] == [{
        "op": "set", "ref": "parent", "path": ["clips", "id:direct", "at"],
        "before": {"state": "present", "value": 3}, "value": 4,
    }]


def test_document_emitter_emits_narration_and_media_patches():
    work = emitter_bundle()
    base = copy.deepcopy(work)
    shot = next(iter(work["shots"].values()))
    shot["payload"]["text_bindings"][0]["text"] = "changed"
    commands = emit_document_commands(base, work)
    assert commands == [{
        "schema_version": 1, "op": "doc.patch", "version": "1", "select": None,
        "args": {"changes": [{
            "op": "set", "ref": "shot-revision:shot-1", "path": ["text_bindings"],
            "before": {"state": "present", "value": [{"slot": "narration", "text": "A chapter"}]},
            "value": [{"slot": "narration", "text": "changed"}],
        }]},
    }]

    work = emitter_bundle()
    base = copy.deepcopy(work)
    shot = next(iter(work["shots"].values()))
    shot["internal_timeline"]["registry"]["assets"]["picture"]["media_id"] = NEW
    commands = emit_document_commands(base, work)
    assert commands[0]["args"]["changes"] == [{
        "op": "set", "ref": "internal-revision:internal-1",
        "path": ["registry", "assets", "picture", "media_id"],
        "before": {"state": "present", "value": OLD}, "value": NEW,
    }]


@pytest.mark.parametrize("invalid", ["param", "unknown", "media"])
def test_document_emitter_provider_refuses_invalid_param_and_unverified_media(invalid):
    document = document_fixture(shared=False)
    before = bundle_from_document(document)
    if invalid in {"param", "unknown"}:
        for rows in (before["parent"]["clips"], before["parent"]["config"]["clips"]):
            rows[0].update(
                clipType="am-type" if invalid == "param" else "am-flap",
                params=(
                    {"text": "hello"}
                    if invalid == "param" else {"values": [{"text": "hello", "at": 0}]}
                ),
            )
    after = copy.deepcopy(before)
    if invalid == "param":
        for rows in (after["parent"]["clips"], after["parent"]["config"]["clips"]):
            rows[0]["params"]["text"] = None
        expected = {
            "wire_code": "validation_error", "code": "invalid_document", "reason": "invalid_document",
            "hint": "Read the timeline and submit a valid canonical document change",
            "suggested_command": "timeline edit",
        }
    elif invalid == "unknown":
        for rows in (after["parent"]["clips"], after["parent"]["config"]["clips"]):
            rows[0]["params"]["null"] = None
        expected = {
            "wire_code": "validation_error", "code": "invalid_document", "reason": "invalid_document",
            "hint": "Read the timeline and submit a valid canonical document change",
            "suggested_command": "timeline edit",
        }
    else:
        after["parent"]["registry"]["assets"]["picture"]["media_id"] = "sha256:" + "9" * 64
        expected = {
            "wire_code": "conflict", "code": "dependency_changed", "reason": "dependency_changed",
            "hint": "Read the current dependency and reapply the intended change",
            "suggested_command": "timeline show",
        }

    commands = emit_document_commands(before, after)
    assert commands and all(command["op"] == "doc.patch" for command in commands)
    realm = DetachedRealm(document_from_bundle(before))
    head = copy.deepcopy(realm.head)
    entries = copy.deepcopy(realm.entries)
    documents = copy.deepcopy(realm.documents)
    from runtime_protocol.timeline_commit import CommitRejected

    with pytest.raises(CommitRejected) as refusal:
        realm.call(commands)
    failure = refusal.value.failure
    details = failure["details"]
    assert failure["code"] == expected["wire_code"]
    assert details["code"] == expected["code"]
    assert details["reason"] == expected["reason"]
    assert details["path"] == []
    assert details["hint"] == expected["hint"]
    assert details["suggested_command"] == expected["suggested_command"]
    assert realm.head == head
    assert realm.entries == entries
    assert realm.documents == documents


def test_document_emitter_refuses_a_noncanonical_pinned_base():
    work = bundle()
    base = bundle()
    base["parent"]["clips"][0]["at"] = 9

    with pytest.raises(DocumentPatchError, match="contradictory parent mirrors"):
        emit_document_commands(base, work)


@pytest.mark.parametrize(
    "family",
    [
        "parent_clip",
        "parent_track",
        "parent_config_clip",
        "child_clip",
        "child_track",
        "parent_asset",
        "child_asset",
    ],
)
def test_document_emitter_refuses_matched_row_presence_changes(family):
    work = bundle(shared=False)
    base = copy.deepcopy(work)
    probe = "presence_probe"
    shot_id = next(iter(work["shots"]))

    if family == "parent_clip":
        rows = [base["parent"]["clips"][0], work["base_parent_payload"]["clips"][0],
                work["parent"]["clips"][0]]
        candidate_row = work["parent"]["clips"][0]
    elif family == "parent_track":
        rows = [base["parent"]["config"]["tracks"][0],
                work["base_parent_payload"]["config"]["tracks"][0],
                work["parent"]["config"]["tracks"][0]]
        candidate_row = work["parent"]["config"]["tracks"][0]
    elif family == "parent_config_clip":
        rows = [base["parent"]["config"]["clips"][0],
                work["base_parent_payload"]["config"]["clips"][0],
                work["parent"]["config"]["clips"][0]]
        candidate_row = work["parent"]["config"]["clips"][0]
    elif family == "child_clip":
        rows = [base["shots"][shot_id]["internal_timeline"]["clips"][0],
                work["shots"][shot_id]["base_internal_timeline"]["clips"][0],
                work["shots"][shot_id]["internal_timeline"]["clips"][0]]
        candidate_row = work["shots"][shot_id]["internal_timeline"]["clips"][0]
    elif family == "child_track":
        rows = [base["shots"][shot_id]["internal_timeline"]["tracks"][0],
                work["shots"][shot_id]["base_internal_timeline"]["tracks"][0],
                work["shots"][shot_id]["internal_timeline"]["tracks"][0]]
        candidate_row = work["shots"][shot_id]["internal_timeline"]["tracks"][0]
    elif family == "parent_asset":
        rows = [base["parent"]["registry"]["assets"]["direct"],
                work["base_parent_payload"]["registry"]["assets"]["direct"],
                work["parent"]["registry"]["assets"]["direct"]]
        candidate_row = work["parent"]["registry"]["assets"]["direct"]
    else:
        rows = [base["shots"][shot_id]["internal_timeline"]["registry"]["assets"]["old-picture"],
                work["shots"][shot_id]["base_internal_timeline"]["registry"]["assets"]["old-picture"],
                work["shots"][shot_id]["internal_timeline"]["registry"]["assets"]["old-picture"]]
        candidate_row = work["shots"][shot_id]["internal_timeline"]["registry"]["assets"]["old-picture"]

    for row in rows:
        row[probe] = None
    candidate_row.pop(probe)
    before, pinned = copy.deepcopy(work), copy.deepcopy(base)

    with pytest.raises(DocumentPatchError):
        emit_document_commands(base, work)
    assert work == before
    assert base == pinned


def test_document_emitter_refuses_presence_loss_masked_by_a_valid_move():
    work = bundle(shared=False)
    base = copy.deepcopy(work)
    probe = "presence_probe"
    for row in (base["parent"]["config"]["clips"][0],
                work["base_parent_payload"]["config"]["clips"][0],
                work["parent"]["config"]["clips"][0]):
        row[probe] = None
    work["parent"]["clips"][0]["at"] = 4
    work["parent"]["config"]["clips"][0].update(at=4)
    work["parent"]["config"]["clips"][0].pop(probe)
    before, pinned = copy.deepcopy(work), copy.deepcopy(base)

    with pytest.raises(DocumentPatchError):
        emit_document_commands(base, work)
    assert work == before
    assert base == pinned


def test_document_emitter_clears_present_null_hold_without_zero_command():
    work = bundle()
    base = copy.deepcopy(work)
    for parent in (base["parent"], work["parent"]):
        for rows in (parent["clips"], parent["config"]["clips"]):
            rows[0].update({"from": 0, "to": 1, "hold": None})
    for rows in (work["parent"]["clips"], work["parent"]["config"]["clips"]):
        rows[0].pop("hold")

    commands = emit_document_commands(base, work)

    assert [command["op"] for command in commands] == ["doc.patch"]
    assert commands[0]["args"]["changes"] == [{
        "op": "remove", "ref": "parent", "path": ["clips", "id:direct", "hold"],
        "before": {"state": "present", "value": None},
    }]
    assert "hold" not in work["parent"]["clips"][0]
    assert "hold" not in work["parent"]["config"]["clips"][0]


def test_document_emitter_clears_a_present_nonnull_param():
    work = emitter_bundle()
    base = copy.deepcopy(work)
    shot = next(iter(work["shots"].values()))
    base_shot = next(iter(base["shots"].values()))
    for timeline in (base_shot["internal_timeline"], shot["internal_timeline"]):
        timeline["clips"][0]["clipType"] = "am-type"
        timeline["clips"][0]["params"] = {"text": "hello"}
    shot["internal_timeline"]["clips"][0]["params"] = {}

    commands = emit_document_commands(base, work)
    internal_ref = "internal-revision:" + document_from_bundle(base)[
        "internal_timeline_revisions"
    ][0]["revision_id"]
    clip_id = base["shots"][next(iter(base["shots"]))]["internal_timeline"]["clips"][0]["id"]

    assert [command["op"] for command in commands] == ["doc.patch"]
    assert commands[0]["args"]["changes"] == [{
        "op": "remove", "ref": internal_ref,
        "path": ["clips", f"id:{clip_id}", "params", "text"],
        "before": {"state": "present", "value": "hello"},
    }]
    assert work["shots"][shot["shot_id"]]["internal_timeline"]["clips"][0]["params"] == {}


def test_document_emitter_validates_parent_track_rename_against_captured_result():
    work = bundle()
    base = copy.deepcopy(work)
    work["parent"]["config"]["tracks"][0]["label"] = "Renamed"

    commands = emit_document_commands(base, work)

    assert [command["op"] for command in commands] == ["doc.patch"]
    assert commands[0]["args"]["changes"] == [{
        "op": "set", "ref": "parent", "path": ["config", "tracks", "id:picture", "label"],
        "before": {"state": "absent"}, "value": "Renamed",
    }]
    assert work["parent"]["config"]["tracks"][0]["label"] == "Renamed"


def test_document_emitter_refuses_candidate_only_mirror_field_matching_move_result():
    work = bundle()
    base = copy.deepcopy(work)
    work["parent"]["clips"][0]["at"] = 4
    mirror = work["parent"]["config"]["clips"][0]
    mirror["at"] = 4
    mirror["app"] = {"astrid": {"fixed_start": True, "for": 1.0}}

    before, pinned = copy.deepcopy(work), copy.deepcopy(base)
    with pytest.raises(DocumentPatchError, match="source-bound|closure|managed"):
        emit_document_commands(base, work)
    assert work == before and base == pinned


def test_unchanged_checkout_reports_no_authored_changes_and_unknown_imports_by_default():
    work = bundle()
    result = publish_authoring_candidate(
        work, Writer(work["source_mapping"]["_runtime_document"]), idempotency_key="noop"
    )
    assert result["update"].startswith("No authored changes. Saved revision saved-revision.")
    assert result["summary"]["authored_change_count"] == 0
    assert result["summary"]["catalog_media"] is None


def test_catalog_imports_and_reuse_are_separate_from_selection_counts():
    work = bundle()
    replace_images(work)
    result = publish(work, imports=[{"asset_id": NEW}, {"asset_id": OLD, "reused": True}])
    assert result["summary"]["catalog_media"] == {"imported": 1, "reused": 1}
    assert result["summary"]["media_selections"]["image"]["updated"] == 1
    assert "Imported 1 catalog media item. Reused 1 catalog media item." in result["update"]


def test_removed_shot_without_its_baseline_does_not_claim_noop_or_invent_media_counts():
    work = bundle()
    remove_authoring_shot(work, work["placements"][0]["shot_id"])
    result = publish(work)
    assert result["summary"]["occurrence_changes"]["removed"] == 1
    assert result["summary"]["unavailable_selection_baselines"] == ["occ-1"]
    assert "Removed 1 shot occurrence." in result["update"]
    assert "No authored changes" not in result["update"]
    assert "Selection counts are partial" in result["update"]


def test_repeated_occurrences_count_independent_selections_and_skip_unplaced_shots():
    work = bundle(shared=True)
    for shot in work["shots"].values():
        shot["internal_timeline"]["registry"]["assets"]["old-picture"]["media_id"] = NEW
        shot["payload"]["items"][0]["media_id"] = NEW
    result = publish(work)
    assert result["summary"]["media_selections"]["image"]["updated"] == 2
    work = bundle()
    source_id = work["placements"][0]["shot_id"]
    duplicate_authoring_shot(work, source_id, new_shot_id="unplaced", occurrence_id="unplaced-occ")
    work["placements"].pop()
    orphan = work["shots"]["unplaced"]
    orphan["internal_timeline"]["registry"]["assets"]["old-picture"]["media_id"] = NEW
    assert publish(work)["summary"]["media_selections"]["image"]["updated"] == 0


def test_next_actions_quote_authoritative_scope_and_parse_supported_options():
    work = bundle()
    work["project_id"] = "project '$(touch example)' `quoted`"
    work["timeline_id"] = 'timeline "special"; echo example'
    result = publish(work)
    current_actions = [action for action in result["next_actions"] if action["scope"] == "current_head"]
    for action, configure in zip(current_actions, (_configure_show, _configure_visualize)):
        argv = shlex.split(action["command"])
        assert argv[:4] == ["python3", "-m", "astrid", "timelines"]
        parser = argparse.ArgumentParser()
        configure(parser)
        parsed = parser.parse_args(argv[5:])
        assert parsed.project == work["project_id"]
        assert getattr(parsed, "ref", getattr(parsed, "timeline_ref", None)) == work["timeline_id"]
        assert action["scope"] == "current_head"
    assert "Use timelines show to view the timeline" in result["update"]
    assert "Use timelines visualize to visualize" in result["update"]
    assert "--mode inputs" in result["update"]


def test_document_publish_stdout_and_durable_receipt_contain_shared_feedback(tmp_path, monkeypatch, capsys):
    script = Path(__file__).resolve().parents[2] / "astrid/packs/rendering/docs/scripts/timeline_document.py"
    spec = importlib.util.spec_from_file_location("timeline_document_feedback_test", script)
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    work = bundle()
    work["base_parent"]["publication_id"] = "publication-1"
    work["parent"]["clips"][0]["at"] = 4
    work["parent"]["config"]["clips"][0]["at"] = 4
    path = tmp_path / "batch 'one' edit.json"
    source = json.dumps(work)
    path.write_text(source)
    commits = []

    class Session:
        def _state(self):
            return {"base": {"revision_id": "parent-1", "publication_id": "publication-1"}}

        def _capture_bundle(self, _state):
            original = bundle()
            original["base_parent"]["publication_id"] = "publication-1"
            return original

        def commit_many(self, commands, **kwargs):
            commits.append(commands)
            # C0/G3: detached document publication emits one generic doc.patch;
            # the remaining assertions verify its durable feedback contract.
            assert [command["op"] for command in commands] == ["doc.patch"]
            assert kwargs["idempotency_key"] == "batch-feedback"
            return {"data": {
                "status": "accepted", "accepted_revision_id": "saved-revision",
                "previous_revision_id": "parent-1",
            }}

    client = SimpleNamespace(timelines=SimpleNamespace(history=lambda *args, **kwargs: None))
    monkeypatch.setattr(helper.AstridClient, "open_from_launcher", lambda **kwargs: nullcontext(client))
    monkeypatch.setattr(helper, "workspace", lambda: SimpleNamespace())
    monkeypatch.setattr(helper.TimelineSession, "open", lambda *args, **kwargs: Session())
    monkeypatch.setattr("sys.argv", [str(script), "publish", "--file", str(path), "--idempotency-key", "batch-feedback"])
    helper.main()
    output = json.loads(capsys.readouterr().out)
    saved = json.loads(path.with_suffix(".publication.json").read_text())
    assert output["update"].startswith("Adjusted timing on 1 clip.")
    for field in ("update", "summary", "next_actions"):
        assert output[field] == saved[field]
    assert output["publication"]["data"]["accepted_revision_id"] == "saved-revision"
    assert saved["media_imports"] == []
    assert output["artifacts"] == saved["artifacts"]
    for key, suffix in (("check", ".check.json"), ("prepared", ".prepared.json"), ("publication", ".publication.json")):
        assert output["artifacts"][key] == str(path.with_suffix(suffix))
        assert Path(output["artifacts"][key]).is_file()
    check_action = next(action for action in output["next_actions"] if action["scope"] == "candidate_preflight")
    assert shlex.split(check_action["command"]) == ["python3", "-m", "json.tool", str(path.with_suffix(".check.json"))]
    diff_actions = [action for action in output["next_actions"] if action["scope"] == "saved_revision_pair"]
    assert [action["format"] for action in diff_actions] == ["readable", "details"]
    for diff_action in diff_actions:
        assert diff_action["from_revision"] == "parent-1"
        assert diff_action["to_revision"] == "saved-revision"
        parser = argparse.ArgumentParser()
        _configure_diff(parser)
        parsed = parser.parse_args(shlex.split(diff_action["command"])[5:])
        assert parsed.format == diff_action["format"]
    assert "--from-revision parent-1 --to-revision saved-revision" in output["update"]
    assert path.read_text() == source


def test_target_bound_sdk_forwards_shared_publication_feedback():
    work = bundle()
    replace_images(work)
    writer = Writer(work["source_mapping"]["_runtime_document"])
    client = SimpleNamespace(
        endpoint="http://127.0.0.1:63331",
        list_timelines=lambda *args, **kwargs: ([{"timeline_id": "main"}], None),
        inspect_timeline=lambda *args, **kwargs: {"data": {"is_current_head": True, "revision_id": "parent-1"}},
        get_project_object_location=lambda project, object_id: {"data": {"verified": True, "object_id": object_id}},
        list_timeline_commits=writer.list_timeline_commits,
        get_project_parent_composition_revision=writer.get_project_parent_composition_revision,
        get_project_shot_revision=writer.get_project_shot_revision,
        get_project_timeline_revision=writer.get_project_timeline_revision,
        get_project_media_selection=writer.get_project_media_selection,
        commit_timeline=writer.commit_timeline,
    )
    target = {"endpoint": client.endpoint, "project_id": "project-1", "timeline_id": "main",
              "head_revision_id": "parent-1", "capabilities": {"edit": {
                  "status": "available", "route": "authoring-bundle validate/commit",
              }}}
    result = TargetBoundAuthoringBundle(client, target).publish(work, idempotency_key="sdk-feedback")
    assert result["update"].startswith("Updated 1 image selection.")
    assert result["summary"]["new_head"] == "saved-revision"
    assert len(result["next_actions"]) == 4


def test_target_bound_sdk_defers_replay_and_stale_decisions_to_runtime():
    class ReplayRuntime:
        endpoint = "http://127.0.0.1:63331"

        def __init__(self):
            self.current_head = "parent-1"
            self.requests = {}
            self.publication_calls = 0
            self.document = bundle()["source_mapping"]["_runtime_document"]

        def list_timelines(self, project_id, **kwargs):
            return ([{"timeline_id": "main"}], None)

        def inspect_timeline(self, project_id, timeline_id, options=None):
            return {"data": {"is_current_head": True, "revision_id": self.current_head}}

        def get_project_object_location(self, project_id, object_id):
            return {"data": {"verified": True, "object_id": object_id}}

        def list_timeline_commits(self, project_id, timeline_id, *, cursor=None, limit=100):
            return {"data": {"items": [{
                "publication_id": "publication-1",
                "revision_id": "parent-1",
                "receipt": {
                    "receipt_id": "publication-1",
                    "result": {"canonical_document": copy.deepcopy(self.document)},
                },
            }], "next_cursor": None}}

        def get_project_parent_composition_revision(self, project_id, timeline_id, revision):
            return {"data": {
                **copy.deepcopy(self.document["parent_revision"]),
                "project_id": project_id,
                "timeline_id": timeline_id,
                "dependency_manifest": copy.deepcopy(self.document["dependency_manifest"]),
            }}

        def get_project_shot_revision(self, project_id, shot_id, revision):
            row = next(row for row in self.document["shot_revisions"] if row["revision_id"] == revision)
            return {"data": {**copy.deepcopy(row), "project_id": project_id}}

        def get_project_timeline_revision(self, project_id, timeline_id, revision):
            row = next(row for row in self.document["internal_timeline_revisions"] if row["revision_id"] == revision)
            return {"data": {**copy.deepcopy(row), "project_id": project_id}}

        def get_project_media_selection(self, project_id, selection):
            raise AssertionError(f"unexpected fixture media selection: {selection}")

        def commit_timeline(self, project_id, timeline_id, request, *, idempotency_key):
            self.publication_calls += 1
            body = json.dumps(request, sort_keys=True)
            prior = self.requests.get(idempotency_key)
            if prior is not None:
                if prior[0] != body:
                    raise WorkspaceClientError(
                        409, "idempotency_conflict", "idempotency key was reused with a different request"
                    )
                return {"data": prior[1]}
            if request["base_revision_id"] != self.current_head:
                raise WorkspaceClientError(409, "conflict", "parent composition head is stale")
            result = {
                "status": "accepted",
                "accepted_revision_id": "advanced-head",
                "previous_revision_id": self.current_head,
                "canonical_document": copy.deepcopy(self.document),
            }
            self.requests[idempotency_key] = (body, result)
            self.current_head = "advanced-head"
            return {"data": result}

    runtime = ReplayRuntime()
    target = {
        "endpoint": runtime.endpoint,
        "project_id": "project-1",
        "timeline_id": "main",
        "head_revision_id": "parent-1",
        "capabilities": {"edit": {
            "status": "available", "route": "authoring-bundle validate/commit",
        }},
    }
    remote = TargetBoundAuthoringBundle(runtime, target)
    first = remote.publish(bundle(), idempotency_key="replay-key")
    replay = remote.publish(bundle(), idempotency_key="replay-key")
    assert replay["publication"] == first["publication"]

    changed = bundle()
    changed["placements"][0]["duration_ms"] += 1
    with pytest.raises(AuthoringRemoteError, match="idempotency key was reused"):
        remote.publish(changed, idempotency_key="replay-key")
    with pytest.raises(AuthoringRemoteError, match="parent composition head is stale"):
        remote.publish(bundle(), idempotency_key="new-stale-key")
    assert runtime.publication_calls == 4
