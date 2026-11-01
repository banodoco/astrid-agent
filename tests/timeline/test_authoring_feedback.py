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
from astrid.sdk.timeline_editing import duplicate_authoring_shot, remove_authoring_shot
from astrid.sdk.workspace_client import WorkspaceClientError
from tests.timeline.test_authoring_bundle import NEW, OLD, _closure, _digest


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
    return open_authoring_bundle(parent, shot_revisions=shots, internal_timeline_revisions=internals)


def replace_images(work):
    shot = next(iter(work["shots"].values()))
    shot["internal_timeline"]["registry"]["assets"]["old-picture"]["media_id"] = NEW
    shot["payload"]["items"][0]["media_id"] = NEW
    return shot


class Writer:
    def __init__(self):
        self.calls = []

    def publish_parent_composition(self, project, timeline, publication, **kwargs):
        self.calls.append(publication)
        return {"data": {"new_head": "saved-revision", "old_head": "parent-1"}}


def publish(work, *, imports=()):
    writer = Writer()
    result = publish_authoring_candidate(work, writer, idempotency_key="feedback-test", media_imports=imports)
    assert len(writer.calls) == 1
    assert result["publication"]["data"]["new_head"] == "saved-revision"
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


def test_unchanged_checkout_reports_no_authored_changes_and_unknown_imports_by_default():
    result = publish_authoring_candidate(bundle(), Writer(), idempotency_key="noop")
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
    work = bundle(images=3)
    replace_images(work)
    path = tmp_path / "batch 'one' edit.json"
    source = json.dumps(work)
    path.write_text(source)
    client = SimpleNamespace(timelines=SimpleNamespace(open_composition=lambda *args: SimpleNamespace(
        ok=True, data={"summary": {"head_revision_id": "parent-1"}},
    )))
    monkeypatch.setattr(helper.AstridClient, "open_from_launcher", lambda **kwargs: nullcontext(client))
    writer = Writer()
    monkeypatch.setattr(helper, "workspace", lambda: writer)
    monkeypatch.setattr("sys.argv", [str(script), "publish", "--file", str(path), "--idempotency-key", "batch-feedback"])
    helper.main()
    output = json.loads(capsys.readouterr().out)
    saved = json.loads(path.with_suffix(".publication.json").read_text())
    assert output["update"].startswith("Updated 3 image selections.")
    for field in ("update", "summary", "next_actions"):
        assert output[field] == saved[field]
    assert output["publication"]["new_head"] == "saved-revision"
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
    writer = Writer()
    client = SimpleNamespace(
        endpoint="http://127.0.0.1:63331",
        list_timelines=lambda *args, **kwargs: ([{"timeline_id": "main"}], None),
        inspect_timeline=lambda *args, **kwargs: {"data": {"is_current_head": True, "revision_id": "parent-1"}},
        get_project_object_location=lambda project, object_id: {"data": {"verified": True, "object_id": object_id}},
        publish_parent_composition=writer.publish_parent_composition,
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

        def list_timelines(self, project_id, **kwargs):
            return ([{"timeline_id": "main"}], None)

        def inspect_timeline(self, project_id, timeline_id, options=None):
            return {"data": {"is_current_head": True, "revision_id": self.current_head}}

        def get_project_object_location(self, project_id, object_id):
            return {"data": {"verified": True, "object_id": object_id}}

        def publish_parent_composition(self, project_id, timeline_id, publication, *, idempotency_key):
            self.publication_calls += 1
            body = json.dumps(publication, sort_keys=True)
            prior = self.requests.get(idempotency_key)
            if prior is not None:
                if prior[0] != body:
                    raise WorkspaceClientError(
                        409, "idempotency_conflict", "idempotency key was reused with a different request"
                    )
                return {"data": prior[1]}
            if publication["expected_head"] != self.current_head:
                raise WorkspaceClientError(409, "conflict", "parent composition head is stale")
            result = {"new_head": "advanced-head", "old_head": self.current_head}
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
