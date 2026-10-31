from __future__ import annotations

import copy
import hashlib
import json
import uuid
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.sdk.timeline import retry as retry_module
from astrid.sdk.timeline.sync import CommitConflict

pytest_plugins = ("tests.sdk.test_timeline_editing_acceptance_runtime",)


BASE = {"revision_id": "r1", "publication_id": "p1"}
HEAD = {"revision_id": "r2", "publication_id": "p2"}


def test_function_retry_saves_explicit_read_conflicts_in_record_and_packet(
    editing_case, tmp_path, monkeypatch,
):
    daemon, client, project, timeline = editing_case
    monkeypatch.setenv("ASTRID_RUNTIME_DATA_ROOT", str(tmp_path / "explicit-read-data"))

    from astrid.sdk.timeline import Timeline
    from tests.editing_conformance.test_commit_runtime_m1 import open_client

    _, credential = daemon.credentials.provision("explicit-read-peer", [
        "handshake", "projects:read", "projects:write", "objects:read", "objects:write",
        "tasks:read", "tasks:write",
    ])
    peer_client = open_client(daemon, actor="explicit-read-peer", credential=credential)
    initial_name = timeline.bundle["parent"]["name"]
    peer_names = []
    generated = client._remote._transport._generated
    original = generated.commit_timeline

    def advance_read_scope_before_each_attempt(project_id, timeline_id, body, **kwargs):
        if body.get("label") == "Function with explicit parent read":
            value = f"Peer read change {len(peer_names) + 1}"
            peer = Timeline.open(
                project, "film", client=peer_client,
                outbox_path=tmp_path / f"explicit-read-peer-{len(peer_names)}.json",
            )
            peer_result = peer.commit(
                lambda t: t.bundle["parent"].update({"name": value}),
                label=value, key=f"explicit-read-peer-{len(peer_names)}",
            )
            assert peer_result["receipt"]["receipt_id"]
            peer_names.append(value)
        return original(project_id, timeline_id, body, **kwargs)

    monkeypatch.setattr(generated, "commit_timeline", advance_read_scope_before_each_attempt)
    evaluations = []

    def edit_from_read(t):
        evaluations.append(t.bundle["parent"]["name"])
        t.bundle["parent"]["custom"] = {"derived_from": t.bundle["parent"]["name"]}

    dependency = {"kind": "scope", "ref": "parent", "path": []}
    with pytest.raises(CommitConflict) as caught:
        timeline.commit(
            edit_from_read,
            label="Function with explicit parent read",
            key="function-explicit-parent-read",
            merge="fields",
            reads=[dependency],
        )

    assert len(peer_names) == len(evaluations) == 3
    assert evaluations[0] == initial_name
    pending_id = caught.value.pending_id
    assert pending_id
    saved = timeline.pending_show(pending_id)
    assert saved["state"] == "pending"
    assert saved["origin"] == "function"
    assert saved["parts"][0]["commit_options"]["reads"] == [dependency]
    assert saved["failure"]["details"]["status"] == "conflict"
    conflict_rows = saved["failure"]["details"]["conflicts"]
    assert conflict_rows
    assert any(row.get("kind") == "dependency_changed" for row in conflict_rows), conflict_rows
    assert any(row.get("ref") == "parent" and row.get("path") == []
               for row in conflict_rows), conflict_rows
    packet = saved["clash"]
    assert packet["schema"] == "astrid.clash/2"
    assert packet["conflicts"] == conflict_rows
    assert any(row.get("touches_conflict") for row in packet["receipts"]), packet


def test_explicit_read_change_after_refusal_requires_fresh_preview_before_commit(
    editing_case, tmp_path, monkeypatch,
):
    daemon, client, project, timeline = editing_case
    monkeypatch.setenv("ASTRID_RUNTIME_DATA_ROOT", str(tmp_path / "explicit-read-retry-data"))

    from astrid.sdk.timeline import Timeline
    from tests.editing_conformance.test_commit_runtime_m1 import open_client

    _, credential = daemon.credentials.provision("explicit-read-retry-peer", [
        "handshake", "projects:read", "projects:write", "objects:read", "objects:write",
        "tasks:read", "tasks:write",
    ])
    peer_client = open_client(daemon, actor="explicit-read-retry-peer", credential=credential)
    dependency = {"kind": "scope", "ref": "parent", "path": []}
    generated = client._remote._transport._generated
    original = generated.commit_timeline
    peer_values = []

    def advance_read_before_attempt(project_id, timeline_id, body, **kwargs):
        if body.get("label") == "Function with explicit retry read":
            value = f"Saved refusal peer {len(peer_values) + 1}"
            peer = Timeline.open(
                project, "film", client=peer_client,
                outbox_path=tmp_path / f"explicit-retry-peer-{len(peer_values)}.json",
            )
            result = peer.commit(
                lambda t, value=value: t.bundle["parent"].update({"name": value}),
                label=value,
                key=f"explicit-retry-peer-{len(peer_values)}",
            )
            assert result["receipt"]["receipt_id"]
            peer_values.append(value)
        return original(project_id, timeline_id, body, **kwargs)

    monkeypatch.setattr(generated, "commit_timeline", advance_read_before_attempt)
    observed = []

    def edit_from_read(t):
        name = t.bundle["parent"]["name"]
        observed.append(name)
        t.bundle["parent"]["custom"] = {"derived_from": name}

    with pytest.raises(CommitConflict) as caught:
        timeline.commit(
            edit_from_read,
            label="Function with explicit retry read",
            key="function-explicit-retry-read",
            merge="fields",
            reads=[dependency],
        )
    assert len(peer_values) == len(observed) == 3
    pending_id = caught.value.pending_id
    assert pending_id
    saved = timeline.pending_show(pending_id)
    assert saved["parts"][0]["commit_options"]["reads"] == [dependency]
    assert saved["state"] == "pending"

    peer = Timeline.open(
        project, "film", client=peer_client, outbox_path=tmp_path / "explicit-retry-peer-after-refusal.json"
    )
    changed_value = "Read changed after saved refusal"
    peer_result = peer.commit(
        lambda t: t.bundle["parent"].update({"name": changed_value}),
        label=changed_value,
        key="explicit-retry-peer-after-refusal",
    )
    assert peer_result["receipt"]["receipt_id"]

    reopened = Timeline.open(project, "film", client=client, outbox_path=timeline.session.outbox.path)
    monkeypatch.setattr(generated, "commit_timeline", original)
    retry_result = reopened.retry(pending_id)
    assert retry_result["status"] == "re_derive"
    assert retry_result["pending_id"] == pending_id
    assert reopened.pending_show(pending_id)["state"] == "pending"

    updated_observed = []

    def derive_updated_edit(t):
        name = t.bundle["parent"]["name"]
        updated_observed.append(name)
        t.bundle["parent"]["custom"] = {"derived_from": name}

    preview = reopened.commit(
        derive_updated_edit,
        label="Function with explicit retry read",
        dry_run=True,
        merge="fields",
        reads=[dependency],
    )
    assert preview["data"]["status"] == "preview"
    assert updated_observed == [changed_value]
    reviewed_commands = copy.deepcopy(preview["data"]["commands"])
    assert reviewed_commands
    assert any(command.get("op") == "doc.patch"
               and any(change.get("ref") == "parent" and changed_value in json.dumps(change)
                       for change in command.get("args", {}).get("changes", []))
               for command in reviewed_commands)

    committed = reopened.commit(
        reviewed_commands,
        label="Function with explicit retry read",
        key="function-explicit-retry-read-rederived",
        pending=pending_id,
        merge="fields",
        reads=[dependency],
    )
    assert committed["receipt"]["receipt_id"]
    assert committed["data"]["status"] in {"accepted", "merged"}
    assert committed["data"]["commands"] == reviewed_commands
    current = Timeline.open(project, "film", client=client,
                            outbox_path=tmp_path / "explicit-retry-landed.json")
    assert current.bundle["parent"]["name"] == changed_value
    assert current.bundle["parent"]["custom"] == {"derived_from": changed_value}
    assert reopened.pending_show(pending_id)["state"] == "landed"


def _record():
    return {"id": "pc_saved", "state": "pending", "key": "recut", "label": "Recut",
            "origin": "list", "part": 0, "attempts": 2,
            "parts": [{"base": BASE, "commands": [{"op": "clip.move", "select": "clip:a",
                                                       "args": {"on": 4}}],
                       "commit_options": {"label": "Recut", "steps": [{"id": "s1"}],
                                          "merge": "fields"},
                       "group_id": "assembly-1",
                       "rationale": "Keep this edit", "imports": [{"object_id": "media-1"}],
                       "evaluation": None}],
            "failure": {"details": {"status": "conflict", "conflicts": []}},
            "head": HEAD, "closed_by": None}


class FakeSession:
    def __init__(self, path):
        self.outbox = SimpleNamespace(path=Path(path), locked=lambda: nullcontext(),
                                      read=lambda: self.state, write=lambda state: setattr(self, "state", state))
        self.identity = {"actor_id": "actor-1"}
        self.state = {"pending_changes": {"pc_saved": _record()}, "base": HEAD,
                      "pending": [], "flight": None}
        self.client = SimpleNamespace()
        self.project_id, self.timeline_id = "project", "timeline"
        self.submissions = []

    def _state(self):
        return self.state

    def submit(self, *args, **kwargs):
        self.submissions.append((args, kwargs))
        pending_id = kwargs.get("pending_id")
        if pending_id:
            from astrid.sdk.timeline.sync import _settle_pending

            record = self.state["pending_changes"][pending_id]
            _settle_pending(record, expected_part=kwargs.get("pending_part", record.get("part", 0)),
                            actor_id=self.identity["actor_id"],
                            publication_id="pub-accepted", how=kwargs["pending_how"],
                            base=HEAD)
        return {"status": "accepted", "receipt": {"receipt_id": "pub-accepted"}}


class FakeTimeline:
    def __init__(self, path):
        self.session = FakeSession(path)

    def _pending_records_with_paths(self, actors):
        return [(self.session.outbox.path, copy.deepcopy(record))
                for record in self.session.state["pending_changes"].values()]

    def _accepted(self, result):
        return result


def test_list_retry_previews_original_base_with_saved_options(monkeypatch, tmp_path):
    timeline = FakeTimeline(tmp_path / "outbox.json")
    calls = []

    def probe(session, request):
        calls.append(copy.deepcopy(request))
        return {"would_status": "merged", "evaluated_head": HEAD, "canonical_document": {}}

    monkeypatch.setattr(retry_module, "_probe", probe)
    monkeypatch.setattr(retry_module, "_preview_diff", lambda *args: {"changes": [{"ref": "parent/clip:a"}]})
    preview = retry_module.retry(timeline, "recut")

    assert preview["status"] == "preview"
    assert preview["next"] == "tl.commit(r)"
    assert calls == [{"base_revision_id": "r1", "base_publication_id": "p1",
                       "commands": [{"op": "clip.move", "select": "clip:a", "args": {"on": 4}}],
                       "label": "Recut", "steps": [{"id": "s1"}], "merge": "fields",
                       "group_id": "assembly-1", "reads": [], "rationale": "Keep this edit",
                       "imports": [{"object_id": "media-1"}], "source": {"via": "python"}}]


def test_commit_preview_submits_identical_base_and_closes_pending(monkeypatch, tmp_path):
    timeline = FakeTimeline(tmp_path / "outbox.json")
    monkeypatch.setattr(retry_module, "_probe", lambda *args: {
        "would_status": "accepted", "evaluated_head": HEAD, "canonical_document": {}})
    monkeypatch.setattr(retry_module, "_preview_diff", lambda *args: {"changes": []})
    preview = retry_module.retry(timeline, "pc_saved")

    result = retry_module.commit_preview(timeline, preview)

    assert result["status"] == "accepted"
    args, kwargs = timeline.session.submissions[0]
    assert args[0] == _record()["parts"][0]["commands"]
    assert kwargs["base"] == BASE
    assert kwargs["pending_id"] == "pc_saved"
    assert kwargs["pending_path"] == str(timeline.session.outbox.path)
    assert kwargs["pending_how"] == "retry"
    assert str(uuid.UUID(kwargs["idempotency_key"])) == kwargs["idempotency_key"]
    assert kwargs["logical_key"] == "recut"
    assert kwargs["group_id"] == "assembly-1"
    assert kwargs["rationale"] == "Keep this edit"
    assert kwargs["imports"] == [{"object_id": "media-1"}]


def test_commit_preview_uses_pending_id_as_default_group(monkeypatch, tmp_path):
    timeline = FakeTimeline(tmp_path / "outbox.json")
    record = timeline.session.state["pending_changes"]["pc_saved"]
    record["parts"][0].pop("group_id")
    monkeypatch.setattr(retry_module, "_probe", lambda *args: {
        "would_status": "accepted", "evaluated_head": HEAD, "canonical_document": {}})
    monkeypatch.setattr(retry_module, "_preview_diff", lambda *args: {"changes": []})
    preview = retry_module.retry(timeline, "pc_saved")

    retry_module.commit_preview(timeline, preview)

    assert timeline.session.submissions[0][1]["group_id"] == "pc_saved"


def test_retry_preserves_original_group_id_from_part(monkeypatch, tmp_path):
    timeline = FakeTimeline(tmp_path / "outbox.json")
    requests = []
    monkeypatch.setattr(retry_module, "_probe", lambda session, request: requests.append(request) or {
        "would_status": "accepted", "evaluated_head": HEAD, "canonical_document": {}})
    monkeypatch.setattr(retry_module, "_preview_diff", lambda *args: {"changes": []})

    preview = retry_module.retry(timeline, "pc_saved")
    retry_module.commit_preview(timeline, preview)

    assert requests[0]["group_id"] == "assembly-1"
    assert timeline.session.submissions[0][1]["group_id"] == "assembly-1"


def test_retry_returns_redrive_state_for_conflict(monkeypatch, tmp_path):
    timeline = FakeTimeline(tmp_path / "outbox.json")
    monkeypatch.setattr(retry_module, "_probe", lambda *args: {
        "status": "conflict", "details": {"status": "conflict", "conflicts": []}})
    monkeypatch.setattr(retry_module.clash, "explain", lambda *args: {"receipts": []})

    result = retry_module.retry(timeline, "pc_saved")

    assert result["status"] == "re_derive"
    assert 'pending="pc_saved"' in result["summary"]
    assert "make_edit is your function" in result["next"]
    assert "\n" in result["next"]
    assert "idempotency_key='recut'" in result["next"]
    assert "          key=" not in result["next"]


def test_pending_conflict_string_does_not_repeat_its_summary():
    error = CommitConflict({"details": {"status": "conflict"}},
                           pending_id="pc-1", label="Move clip")

    message = str(error)

    assert message.count('nothing was committed') == 1
    assert 'tl.retry("pc-1")' in message


def test_function_retry_returns_commit_result_and_reuses_label_key(monkeypatch, tmp_path):
    timeline = FakeTimeline(tmp_path / "outbox.json")
    calls = []
    def function(working):
        return None

    committed = {"status": "accepted", "data": {"publication_id": "pub9"},
                 "receipt": {"receipt_id": "pub9"}}
    def commit(*args, **kwargs):
        calls.append((args, kwargs))
        assert timeline.session.state["base"] == HEAD
        return committed

    timeline.commit = commit
    monkeypatch.setattr(retry_module.clash, "_history", lambda *args: (HEAD, []))

    result = retry_module.retry(timeline, "pc_saved", fn=function)

    assert calls == [((function,), {"label": "Recut", "key": "recut", "pending": "pc_saved"})]
    assert result is committed
    assert result["status"] == "accepted"
    assert result["receipt"] == {"receipt_id": "pub9"}
    assert result["summary"] == (
        'Committed "Recut" as pub9 after re-running it on the newest timeline; '
        'pc_saved is closed.'
    )
    assert result["data"]["summary"] == result["summary"]


def test_function_origin_retry_previews_saved_list_with_guards_for_approval(monkeypatch, tmp_path):
    timeline = FakeTimeline(tmp_path / "outbox.json")
    record = timeline.session.state["pending_changes"]["pc_saved"]
    record["origin"] = "function"
    dependency = {"kind": "scope", "ref": "timeline", "path": ["clips"]}
    record["parts"][0]["commit_options"] = {
        "label": "Recut", "merge": "fields", "reads": [dependency],
    }
    requests = []

    def probe(session, request):
        requests.append(copy.deepcopy(request))
        return {"would_status": "merged", "evaluated_head": HEAD, "canonical_document": {}}

    monkeypatch.setattr(retry_module, "_probe", probe)
    monkeypatch.setattr(retry_module, "_preview_diff", lambda *args: {"changes": [{"ref": "parent/clip:a"}]})
    monkeypatch.setattr(retry_module.clash, "_history", lambda *args: (HEAD, []))

    preview = retry_module.retry(timeline, "pc_saved")

    assert preview["status"] == "preview"
    assert preview["data"]["commands"] == record["parts"][0]["commands"]
    assert preview["data"]["captured_list_requires_approval"] is True
    assert "captured command list" in preview["summary"]
    assert "explicitly commit" in preview["summary"]
    assert requests[0]["base_revision_id"] == BASE["revision_id"]
    assert requests[0]["base_publication_id"] == BASE["publication_id"]
    assert requests[0]["merge"] == "fields"
    assert requests[0]["reads"] == [dependency]
    assert timeline.session.submissions == []

    result = retry_module.commit_preview(timeline, preview)

    assert result["status"] == "accepted"
    assert timeline.session.submissions[0][1]["base"] == BASE
    assert timeline.session.submissions[0][1]["merge"] == "fields"
    assert timeline.session.submissions[0][1]["reads"] == [dependency]


def test_function_retry_reruns_saved_part_and_retains_suffix(monkeypatch, tmp_path):
    timeline = FakeTimeline(tmp_path / "outbox.json")
    record = timeline.session.state["pending_changes"]["pc_saved"]
    record["origin"] = "function"
    suffix = copy.deepcopy(record["parts"][0])
    suffix["base"] = HEAD
    suffix["commands"] = [{"op": "clip.move", "select": "clip:b", "args": {"on": 8}}]
    record["parts"].append(suffix)
    before_suffix = copy.deepcopy(suffix)
    function = lambda working: None
    calls = []
    committed = {"status": "accepted", "receipt": {"receipt_id": "pub-retry"}}

    monkeypatch.setattr(retry_module.clash, "_history", lambda *args: (HEAD, []))

    def commit(*args, **kwargs):
        calls.append((args, kwargs))
        return committed

    timeline.commit = commit

    result = retry_module.retry(timeline, "pc_saved", fn=function)

    assert result is committed
    assert calls == [((function,), {"label": "Recut", "key": "recut", "pending": "pc_saved"})]
    saved = timeline.session.state["pending_changes"]["pc_saved"]
    assert saved["parts"][1] == before_suffix


def test_function_overwrite_by_pending_id_closes_saved_record(monkeypatch, tmp_path):
    timeline = FakeTimeline(tmp_path / "outbox.json")
    record = timeline.session.state["pending_changes"]["pc_saved"]
    record["origin"] = "function"
    function = lambda working: None
    from astrid.sdk.timeline.commit_session import Timeline

    timeline._retry_functions = {"pc_saved": function}
    committed = {"status": "accepted", "receipt": {"receipt_id": "pub-accepted"}}
    calls = []

    def commit(*args, **kwargs):
        calls.append((args, kwargs))
        timeline.session.submit([], label=kwargs["label"], steps=[],
                                 pending_id=kwargs["pending"], pending_how="rerun")
        return committed

    timeline.commit = commit
    monkeypatch.setattr(retry_module.clash, "_history", lambda *args: (HEAD, []))

    result = Timeline.retry(timeline, "pc_saved", overwrite=True)

    assert calls == [((function,), {"label": "Recut", "key": "recut",
                                    "pending": "pc_saved", "_overwrite": True})]
    assert result is committed
    assert result["status"] == "rebased"
    saved = timeline.session.state["pending_changes"]["pc_saved"]
    assert saved["state"] == "landed"
    assert saved["closed_by"] == {
        "actor_id": "actor-1", "publication_id": "pub-accepted", "how": "rerun"}


def test_function_overwrite_requires_original_function_and_keeps_record_pending(monkeypatch, tmp_path):
    timeline = FakeTimeline(tmp_path / "outbox.json")
    record = timeline.session.state["pending_changes"]["pc_saved"]
    record["origin"] = "function"
    before = copy.deepcopy(record)
    monkeypatch.setattr(retry_module.clash, "_history", lambda *args: (HEAD, []))

    with pytest.raises(ValueError, match="overwrite requires the original edit function"):
        retry_module.retry(timeline, "pc_saved", overwrite=True)

    assert timeline.session.state["pending_changes"]["pc_saved"] == before
    assert timeline.session.submissions == []


def test_function_retry_leaves_unknown_flight_for_commit_reconciliation(monkeypatch, tmp_path):
    timeline = FakeTimeline(tmp_path / "outbox.json")
    timeline.session.state["flight"] = {"key": "possibly-sent"}
    before = copy.deepcopy(timeline.session.state)
    committed = {"status": "accepted", "receipt": {"receipt_id": "pub9"}}
    timeline.commit = lambda *args, **kwargs: committed

    history_calls = []

    def history(*args):
        history_calls.append(args)
        return HEAD, []

    monkeypatch.setattr(retry_module.clash, "_history", history)

    result = retry_module.retry(timeline, "pc_saved", fn=lambda working: None)

    assert result is committed
    assert timeline.session.state == before
    assert len(history_calls) == 1  # Only the normal landed check ran.


def test_function_record_probe_preserves_original_base_and_guards(monkeypatch, tmp_path):
    timeline = FakeTimeline(tmp_path / "outbox.json")
    part = timeline.session.state["pending_changes"]["pc_saved"]["parts"][0]
    part["commit_options"] = {
        "label": "Recut", "steps": [{"id": "s1", "reads": [
            {"kind": "scope", "ref": "timeline", "path": []}]}], "merge": "atomic"
    }
    request = retry_module._request(
        timeline.session.state["pending_changes"]["pc_saved"], part
    )
    captured = {}

    class Timelines:
        def commit(self, project, timeline_id, body, *, idempotency_key):
            captured.update(body)
            return SimpleNamespace(ok=True, as_dict=lambda: {
                "data": {"status": "preview", "would_status": "conflict"}})

    timeline.session.client.timelines = Timelines()
    retry_module._probe(timeline.session, request)

    assert captured["base_revision_id"] == BASE["revision_id"]
    assert captured["base_publication_id"] == BASE["publication_id"]
    assert captured["merge"] == "atomic"
    assert captured["steps"] == part["commit_options"]["steps"]
    assert "reads" not in captured

    part["rationale"] = None
    assert "rationale" not in retry_module._request(
        timeline.session.state["pending_changes"]["pc_saved"], part
    )


def test_function_retry_preserves_synthetic_scope_and_saved_record(tmp_path):
    record = _record()
    part = record["parts"][0]
    part["evaluation"] = {"kind": "function", "synthetic_function_scope": True}
    part["commit_options"] = {
        "merge": "atomic",
        "steps": [
            {"id": "s1", "reads": [{"kind": "scope", "ref": "timeline", "path": []}]},
            {"id": "s2", "reads": [{"kind": "scope", "ref": "timeline", "path": []}]},
        ],
    }
    before = copy.deepcopy(record)

    request = retry_module._request(record, part)

    assert request["base_revision_id"] == BASE["revision_id"]
    assert request["base_publication_id"] == BASE["publication_id"]
    assert request["merge"] == "atomic"
    assert "reads" not in request
    assert request["steps"] == part["commit_options"]["steps"]
    assert record == before


def test_function_retry_preserves_all_step_reads_and_omits_batch_reads(tmp_path):
    record = _record()
    part = record["parts"][0]
    caller_read = {"kind": "scope", "ref": "clip:a", "path": ["start"]}
    synthetic_scope = {"kind": "scope", "ref": "timeline", "path": []}
    part["evaluation"] = {"kind": "function", "synthetic_function_scope": True}
    part["commit_options"] = {
        "merge": "atomic",
        "steps": [
            {"id": "s1", "reads": [caller_read, synthetic_scope]},
            {"id": "s2", "reads": [synthetic_scope]},
        ],
    }
    before = copy.deepcopy(record)

    request = retry_module._request(record, part)

    assert request["merge"] == "atomic"
    assert "reads" not in request
    assert request["steps"] == part["commit_options"]["steps"]
    assert record == before


def test_function_retry_preserves_caller_reads_and_explicit_atomic(tmp_path):
    record = _record()
    part = record["parts"][0]
    caller_reads = [{"kind": "scope", "ref": "timeline", "path": ["clips"]}]
    part["evaluation"] = {"kind": "function", "synthetic_function_scope": True}
    part["commit_options"] = {
        "merge": "atomic",
        "reads": caller_reads,
        "steps": [{"id": "s1", "reads": [{"kind": "scope", "ref": "timeline", "path": []}]}],
    }

    request = retry_module._request(record, part)

    assert request["merge"] == "atomic"
    assert request["reads"] == caller_reads
    assert request["steps"] == part["commit_options"]["steps"]


def test_explicit_atomic_function_commit_stays_atomic_without_reads(tmp_path):
    record = _record()
    part = record["parts"][0]
    part["evaluation"] = {"kind": "function", "synthetic_function_scope": False}
    part["commit_options"] = {
        "merge": "atomic",
        "steps": [{"id": "s1", "reads": [
            {"kind": "scope", "ref": "timeline", "path": []}
        ]}],
    }

    request = retry_module._request(record, part)

    assert request["merge"] == "atomic"
    assert "reads" not in request
    assert request["steps"] == part["commit_options"]["steps"]


def test_legacy_function_record_without_provenance_keeps_ambiguous_atomic_scope(tmp_path):
    record = _record()
    part = record["parts"][0]
    part["evaluation"] = {"kind": "function"}
    part["commit_options"] = {
        "merge": "atomic",
        "steps": [{"id": "s1", "reads": [
            {"kind": "scope", "ref": "timeline", "path": []}
        ]}],
    }

    request = retry_module._request(record, part)

    assert request["merge"] == "atomic"
    assert "reads" not in request
    assert request["steps"] == part["commit_options"]["steps"]


def test_probe_reads_conflict_preview_before_generated_dto_rejects_it(tmp_path):
    timeline = FakeTimeline(tmp_path / "outbox.json")
    captured = {}

    class Generated:
        @staticmethod
        def _json(payload):
            return json.loads(payload)

        @staticmethod
        def _request(method, path, *, body, headers):
            captured.update(method=method, path=path, body=json.loads(body), headers=headers)
            return 200, {}, json.dumps({"data": {
                "status": "preview", "would_status": "conflict", "conflicts": []
            }}).encode()

    timeline.session.client._remote = SimpleNamespace(
        _transport=SimpleNamespace(_generated=Generated)
    )
    part = timeline.session.state["pending_changes"]["pc_saved"]["parts"][0]
    result = retry_module._probe(
        timeline.session,
        retry_module._request(timeline.session.state["pending_changes"]["pc_saved"], part),
    )

    assert result["would_status"] == "conflict"
    assert captured["method"] == "POST"
    assert captured["path"] == "/v1/projects/project/timelines/timeline/commit"


def test_saved_function_reads_turn_stale_retry_preview_into_rederive(monkeypatch, tmp_path):
    timeline = FakeTimeline(tmp_path / "outbox.json")
    record = timeline.session.state["pending_changes"]["pc_saved"]
    record["origin"] = "function"
    part = record["parts"][0]
    dependency = {"kind": "scope", "ref": "timeline", "path": ["clips"]}
    part["evaluation"] = {"kind": "function", "count": 1}
    part["commit_options"]["reads"] = [dependency]
    requests = []

    def probe(session, request):
        requests.append(copy.deepcopy(request))
        assert request["reads"] == [dependency]
        return {"would_status": "conflict", "details": {"conflicts": [dependency]}}

    monkeypatch.setattr(retry_module, "_probe", probe)
    monkeypatch.setattr(retry_module, "_preview_diff",
                        lambda *args: pytest.fail("a stale function result must not be previewed as safe"))
    monkeypatch.setattr(retry_module.clash, "explain", lambda *args: {"receipts": []})

    result = retry_module.retry(timeline, "pc_saved")

    assert requests[0]["reads"] == [dependency]
    assert result["status"] == "re_derive"
    assert result["data"]["captured_list_requires_approval"] is True
    assert "not replayed" in result["summary"]
    assert "original function" in result["next"]
    assert timeline.session.submissions == []


def test_atomic_probe_omits_unset_reads(monkeypatch, tmp_path):
    timeline = FakeTimeline(tmp_path / "outbox.json")
    record = timeline.session.state["pending_changes"]["pc_saved"]
    record["parts"][0]["commit_options"]["merge"] = "atomic"
    record["parts"][0]["commit_options"].pop("reads", None)
    calls = []

    class Client:
        class Timelines:
            def commit(self, project, timeline_id, body, *, idempotency_key):
                calls.append(body)
                return SimpleNamespace(ok=True, as_dict=lambda: {
                    "data": {"status": "refused", "message": "Runtime refusal"}})
        timelines = Timelines()
    timeline.session.client = Client()

    result = retry_module.retry(timeline, "pc_saved")

    assert "reads" not in calls[0]
    assert "Runtime refusal" in result["summary"]
    assert "Inspect the saved edit and fix it or drop it." in result["summary"]
    assert result["next"] == "tl.pending_show('pc_saved')"


def test_preview_rejects_stale_record_and_token(monkeypatch, tmp_path):
    timeline = FakeTimeline(tmp_path / "outbox.json")
    monkeypatch.setattr(retry_module, "_probe", lambda *args: {
        "would_status": "accepted", "evaluated_head": HEAD, "canonical_document": {}})
    monkeypatch.setattr(retry_module, "_preview_diff", lambda *args: {"changes": []})
    preview = retry_module.retry(timeline, "pc_saved")
    timeline.session.state["pending_changes"]["pc_saved"]["rev"] = 3

    with pytest.raises(ValueError, match="changed after this retry preview"):
        retry_module.commit_preview(timeline, preview)

    timeline.session.state["pending_changes"]["pc_saved"]["rev"] = preview["_retry_record_rev"]
    preview["data"]["retry"]["token"] = "stale"
    with pytest.raises(ValueError, match="token is stale or invalid"):
        retry_module.commit_preview(timeline, preview)


def test_preview_rejects_tampered_request(monkeypatch, tmp_path):
    timeline = FakeTimeline(tmp_path / "outbox.json")
    monkeypatch.setattr(retry_module, "_probe", lambda *args: {
        "would_status": "accepted", "evaluated_head": HEAD, "canonical_document": {}})
    monkeypatch.setattr(retry_module, "_preview_diff", lambda *args: {"changes": []})
    preview = retry_module.retry(timeline, "pc_saved")
    preview["_retry_request"]["commands"][0]["args"]["on"] = 999

    with pytest.raises(ValueError, match="matches its saved part"):
        retry_module.commit_preview(timeline, preview)


def test_commit_preview_adopts_successful_submit(monkeypatch, tmp_path):
    timeline = FakeTimeline(tmp_path / "outbox.json")
    monkeypatch.setattr(retry_module, "_probe", lambda *args: {
        "would_status": "accepted", "evaluated_head": HEAD, "canonical_document": {}})
    monkeypatch.setattr(retry_module, "_preview_diff", lambda *args: {"changes": []})
    preview = retry_module.retry(timeline, "pc_saved")
    accepted = {"status": "accepted", "receipt": {"receipt_id": "pub-accepted"}}
    timeline.session.submit = lambda *args, **kwargs: accepted
    adopted = []
    timeline._accepted = lambda result: adopted.append(result) or {"adopted": result}

    result = retry_module.commit_preview(timeline, preview)

    assert adopted == [accepted]
    assert result == {"adopted": accepted}


def test_preview_diff_uses_evaluated_head_and_canonical_document(monkeypatch, tmp_path):
    timeline = FakeTimeline(tmp_path / "outbox.json")
    captured = {}
    import astrid.sdk.timeline.commit as commit_module
    import astrid.sdk.timeline.emitter as emitter
    import astrid.sdk.timeline.store as store

    monkeypatch.setattr(store, "fetch_bundle", lambda *args, **kwargs: captured.update(kwargs) or "before")
    monkeypatch.setattr(commit_module, "bundle_from_document", lambda document: (captured.update(document=document) or "after"))
    monkeypatch.setattr(emitter, "_root_pairs", lambda before, after: [("parent", "old", "new")])
    monkeypatch.setattr(emitter, "_payload_for_emit", lambda ref, value: value)
    changes = []
    monkeypatch.setattr(emitter, "_diff", lambda *args: changes.append(args))

    diff = retry_module._preview_diff(timeline, {"canonical_document": {"revision": "candidate"}}, HEAD)

    assert captured["revision_id"] == HEAD["revision_id"]
    assert captured["publication_id"] == HEAD["publication_id"]
    assert captured["document"] == {"revision": "candidate"}
    assert diff == {"changes": []}
    assert changes[0][:4] == ("old", "new", "parent", ())


def test_group_id_conflict_means_this_actor_already_landed(monkeypatch, tmp_path):
    timeline = FakeTimeline(tmp_path / "outbox.json")
    monkeypatch.setattr(retry_module, "_probe", lambda *args: {
        "would_status": "accepted", "evaluated_head": HEAD, "canonical_document": {}})
    monkeypatch.setattr(retry_module, "_preview_diff", lambda *args: {"changes": []})
    preview = retry_module.retry(timeline, "pc_saved")
    timeline.session.submit = lambda *args, **kwargs: (_ for _ in ()).throw(
        CommitConflict({"details": {"status": "conflict"}}))
    entry = {"group_id": "pc_saved", "publication_id": "pub-retry", "revision_id": "r3",
             "publisher": "actor-2", "receipt": {"receipt_id": "receipt-retry"}}
    monkeypatch.setattr(retry_module.clash, "_history", lambda *args: (HEAD, [entry]))

    result = retry_module.commit_preview(timeline, preview)

    assert result["status"] == "landed"
    assert result["summary"].endswith("(by actor-2); pc_saved is closed. Nothing was sent.")


def test_commit_preview_resolves_history_landed_before_stale_record_check(monkeypatch, tmp_path):
    timeline = FakeTimeline(tmp_path / "outbox.json")
    monkeypatch.setattr(retry_module, "_probe", lambda *args: {
        "would_status": "accepted", "evaluated_head": HEAD, "canonical_document": {}})
    monkeypatch.setattr(retry_module, "_preview_diff", lambda *args: {"changes": []})
    preview = retry_module.retry(timeline, "pc_saved")
    timeline.session.state["pending_changes"]["pc_saved"]["rev"] = 99
    entry = {"group_id": "pc_saved", "publication_id": "pub-other", "revision_id": "r3",
             "publisher": "actor-2", "receipt": {"receipt_id": "receipt-other"}}
    monkeypatch.setattr(retry_module.clash, "_history", lambda *args: (HEAD, [entry]))

    result = retry_module.commit_preview(timeline, preview)

    assert result["status"] == "landed"
    assert timeline.session.submissions == []
    saved = timeline.session.state["pending_changes"]["pc_saved"]
    assert saved["state"] == "landed"
    assert saved["closed_by"]["actor_id"] == "actor-2"


def test_multi_part_history_does_not_close_whole_record(monkeypatch, tmp_path):
    timeline = FakeTimeline(tmp_path / "outbox.json")
    record = timeline.session.state["pending_changes"]["pc_saved"]
    record["parts"].append(copy.deepcopy(record["parts"][0]))
    entry = {"group_id": "pc_saved", "publication_id": "pub-part-1", "revision_id": "r3",
             "publisher": "actor-2", "receipt": {"receipt_id": "receipt-part-1"}}
    monkeypatch.setattr(retry_module.clash, "_history", lambda *args: (HEAD, [entry]))

    result = retry_module.retry(timeline, "pc_saved")

    saved = timeline.session.state["pending_changes"]["pc_saved"]
    assert result["status"] == "part_landed"
    assert saved["state"] == "pending"
    assert saved["part"] == 1
    assert saved["parts"][1]["base"] == {"revision_id": "r3", "publication_id": "pub-part-1"}
    assert saved["receipts"] == ["receipt-part-1"]


def test_multi_part_history_uses_current_part_order(monkeypatch, tmp_path):
    timeline = FakeTimeline(tmp_path / "outbox.json")
    record = timeline.session.state["pending_changes"]["pc_saved"]
    record["parts"].extend(copy.deepcopy(record["parts"][0]) for _ in range(2))
    record["part"] = 1
    record["receipts"] = ["receipt-part-0"]
    entries = [
        {"group_id": "pc_saved", "publication_id": "pub-part-0", "revision_id": "r2",
         "publisher": "actor-2", "receipt": {"receipt_id": "receipt-part-0"}},
        {"group_id": "pc_saved", "publication_id": "pub-part-1", "revision_id": "r3",
         "publisher": "actor-3", "receipt": {"receipt_id": "receipt-part-1"}},
    ]
    monkeypatch.setattr(retry_module.clash, "_history", lambda *args: (HEAD, entries))

    result = retry_module.retry(timeline, "pc_saved")

    saved = timeline.session.state["pending_changes"]["pc_saved"]
    assert result["status"] == "part_landed"
    assert saved["part"] == 2
    assert saved["parts"][2]["base"] == {"revision_id": "r3", "publication_id": "pub-part-1"}
    assert saved["receipts"] == ["receipt-part-0", "receipt-part-1"]


def test_close_ignores_stale_part_after_another_actor_advances_record(tmp_path):
    timeline = FakeTimeline(tmp_path / "outbox.json")
    saved = timeline.session.state["pending_changes"]["pc_saved"]
    saved["parts"].extend(copy.deepcopy(saved["parts"][0]) for _ in range(2))
    stale = copy.deepcopy(saved)
    saved["part"] = 1
    saved["receipts"] = ["receipt-part-0"]
    before = copy.deepcopy(saved)
    path = timeline.session.outbox.path

    duplicate = {"publication_id": "pub-part-0", "revision_id": "r3",
                 "publisher": "actor-2", "receipt": {"receipt_id": "receipt-part-0"}}
    retry_module._close(path, stale, timeline.session, duplicate, "retry")
    assert saved == before

    stale_history_entry = {"publication_id": "pub-stale", "revision_id": "r4",
                           "publisher": "actor-3", "receipt": {"receipt_id": "receipt-stale"}}
    retry_module._close(path, stale, timeline.session, stale_history_entry, "retry")
    assert saved == before


@pytest.mark.parametrize("part", [0, 1])
def test_legacy_ack_without_pending_part_leaves_record_for_history_recovery(
    monkeypatch, tmp_path, part,
):
    timeline = FakeTimeline(tmp_path / "outbox.json")
    saved = timeline.session.state["pending_changes"]["pc_saved"]
    if part:
        saved["parts"].append(copy.deepcopy(saved["parts"][0]))
        saved["part"] = 1
        saved["receipts"] = ["pub-prior"]
        saved["parts"][1]["base"] = {"revision_id": "r3", "publication_id": "pub-prior"}
    before = copy.deepcopy(saved)

    # Older durable flights have no pending_part, so their ack cannot identify
    # which saved part it belongs to. Leave recovery to commit history.
    from astrid.sdk.timeline.sync import _settle_pending

    legacy_flight = {"pending_id": "pc_saved"}
    assert not _settle_pending(
        saved, expected_part=legacy_flight.get("pending_part"), actor_id="actor-1",
        publication_id="pub-accepted", how="retry", base=HEAD,
    )
    assert saved == before

    entries = [
        {"group_id": "pc_saved", "publication_id": "pub-prior", "revision_id": "r3",
         "publisher": "actor-1", "receipt": {"receipt_id": "pub-prior"}},
        {"group_id": "pc_saved", "publication_id": "pub-accepted", "revision_id": "r4",
         "publisher": "actor-1", "receipt": {"receipt_id": "pub-accepted"}},
    ] if part else [
        {"group_id": "pc_saved", "publication_id": "pub-accepted", "revision_id": "r3",
         "publisher": "actor-1", "receipt": {"receipt_id": "pub-accepted"}},
    ]
    monkeypatch.setattr(retry_module.clash, "_history", lambda *args: (HEAD, entries))

    result = retry_module.retry(timeline, "pc_saved")

    recovered = timeline.session.state["pending_changes"]["pc_saved"]
    assert result["status"] == "landed"
    assert recovered["state"] == "landed"
    assert recovered["receipts"] == [*before.get("receipts", []), "pub-accepted"]
    assert recovered["closed_by"]["publication_id"] == "pub-accepted"


def _legacy_ack_history_setup(timeline, *, flight_changes=None, entry_changes=None):
    path = timeline.session.outbox.path
    record = timeline.session.state["pending_changes"]["pc_saved"]
    record["parts"][0]["group_id"] = "assembly-existing"
    flight = {
        "key": "flight-idempotency-key",
        "pending_id": record["id"],
        "pending_path": str(path),
        "pending_part": 0,
    }
    flight.update(flight_changes or {})
    timeline.session.state["acknowledged"] = {"logical-key": flight}
    entry = {
        "group_id": "assembly-existing",
        "publication_id": "pub-history",
        "revision_id": "r3",
        "publisher": "actor-1",
        "receipt": {"receipt_id": "receipt-history",
                    "idempotency_key": "flight-idempotency-key"},
    }
    entry.update(entry_changes or {})
    return path, record, entry


def _legacy_ack_request_body(timeline, record):
    from astrid.sdk.timeline.sync import _native_command

    part = record["parts"][record.get("part", 0)]
    request = retry_module._request(record, part)
    body = {
        "api_version": 1,
        "project_id": timeline.session.project_id,
        "timeline_id": timeline.session.timeline_id,
        "base_revision_id": request["base_revision_id"],
        "base_publication_id": request["base_publication_id"],
        "commands": [_native_command(command) for command in request["commands"]],
        "source": {"via": "python"},
        "dry_run": False,
        "label": request["label"],
        "steps": request.get("steps", []),
        "merge": request["merge"],
        "group_id": request.get("group_id", record["id"]),
    }
    if "reads" in request:
        body["reads"] = request["reads"]
    if request.get("rationale") is not None:
        body["rationale"] = request["rationale"]
    return body


def _legacy_ack_entry(timeline, body, *, key="flight-idempotency-key", **changes):
    from astrid.core.receipts.canonical import canonical_json

    path = f"/v1/projects/{timeline.session.project_id}/timelines/{timeline.session.timeline_id}/commit"
    invocation = {"operation": "commitTimeline", "path": path,
                  "publisher": timeline.session.identity["actor_id"], "request": body}
    digest = hashlib.sha256(canonical_json(invocation).encode()).hexdigest()
    entry = {
        "group_id": "assembly-existing",
        "publication_id": "pub-history",
        "revision_id": "r3",
        "publisher": "actor-1",
        "receipt": {"receipt_id": "pub-history", "idempotency_key": key,
                    "request_hash": digest, "command_kind": "timeline.commit",
                    "project_id": "project",
                    "result": {"status": "accepted", "publisher": "actor-1",
                               "accepted_revision_id": "r3"}},
    }
    entry.update(changes)
    return entry


def _legacy_acknowledged_setup(timeline, *, part_changes=None, flight_changes=None):
    path, record, _ = _legacy_ack_history_setup(timeline)
    part = record["parts"][0]
    part["commands"][0].update(schema_version=1, version="1", select="parent/clip:a")
    part.update(part_changes or {})
    request_path = f"/v1/projects/{timeline.session.project_id}/timelines/{timeline.session.timeline_id}/commit"
    timeline.session.path = request_path
    body = _legacy_ack_request_body(timeline, record)
    flight = {"key": "flight-idempotency-key", "pending_id": record["id"],
              "pending_path": str(path), "pending_part": None, "path": request_path,
              "body": json.dumps(body)}
    flight.update(flight_changes or {})
    timeline.session.state["acknowledged"] = {"logical-key": flight}
    return path, record, body


def test_legacy_ack_without_pending_part_requires_exact_saved_request_and_receipt(
    monkeypatch, tmp_path,
):
    timeline = FakeTimeline(tmp_path / "outbox.json")
    path, record, body = _legacy_acknowledged_setup(timeline)
    # The assembly group existed before this pending record; it cannot identify
    # which grouped commit landed. Only the flight's exact request receipt can.
    earlier = {"group_id": "assembly-existing", "publication_id": "pub-earlier",
               "revision_id": "r2", "publisher": "actor-1",
               "receipt": {"receipt_id": "receipt-earlier"}}
    landed_entry = _legacy_ack_entry(timeline, body)
    monkeypatch.setattr(retry_module.clash, "_history", lambda *args: (HEAD, [earlier, landed_entry]))

    landed = retry_module._landed(timeline, path, record)

    saved = timeline.session.state["pending_changes"]["pc_saved"]
    assert landed["status"] == "landed"
    assert landed["data"]["publication_id"] == "pub-history"
    assert landed["data"]["receipt"] == landed_entry["receipt"]
    assert saved["state"] == "landed"
    assert saved["receipts"] == ["pub-history"]


@pytest.mark.parametrize("damage", [
    "ambiguous_parts", "ambiguous_receipts", "request_mismatch", "request_hash_mismatch",
    "receipt_key_mismatch", "receipt_owner_mismatch", "receipt_type_mismatch",
    "receipt_project_mismatch", "publication_mismatch", "result_status_mismatch",
    "result_publisher_mismatch", "result_revision_mismatch",
])
def test_legacy_ack_without_pending_part_refuses_ambiguous_or_mismatched_evidence(
    monkeypatch, tmp_path, damage,
):
    timeline = FakeTimeline(tmp_path / "outbox.json")
    path, record, body = _legacy_acknowledged_setup(timeline)
    entry = _legacy_ack_entry(timeline, body)
    entries = [entry]
    if damage == "ambiguous_parts":
        record["parts"].append(copy.deepcopy(record["parts"][0]))
    elif damage == "ambiguous_receipts":
        entries.append(copy.deepcopy(entry))
    elif damage == "request_mismatch":
        changed = copy.deepcopy(body)
        changed["label"] = "Different saved request"
        timeline.session.state["acknowledged"]["logical-key"]["body"] = json.dumps(changed)
    elif damage == "request_hash_mismatch":
        entry["receipt"]["request_hash"] = "wrong-request-hash"
    elif damage == "receipt_key_mismatch":
        entry["receipt"]["idempotency_key"] = "another-key"
    elif damage == "receipt_owner_mismatch":
        entry["publisher"] = "actor-2"
    elif damage == "receipt_type_mismatch":
        entry["receipt"]["command_kind"] = "timeline.other"
    elif damage == "receipt_project_mismatch":
        entry["receipt"]["project_id"] = "another-project"
    elif damage == "publication_mismatch":
        entry["receipt"]["receipt_id"] = "different-publication"
    elif damage == "result_status_mismatch":
        entry["receipt"]["result"]["status"] = "conflict"
    elif damage == "result_publisher_mismatch":
        entry["receipt"]["result"]["publisher"] = "actor-2"
    elif damage == "result_revision_mismatch":
        entry["receipt"]["result"]["accepted_revision_id"] = "r4"
    before = copy.deepcopy(timeline.session.state["pending_changes"]["pc_saved"])
    monkeypatch.setattr(retry_module.clash, "_history", lambda *args: (HEAD, entries))

    landed = retry_module._landed(timeline, path, record)

    assert landed is None
    assert timeline.session.state["pending_changes"]["pc_saved"] == before


def test_landed_recovers_legacy_cross_actor_flight_by_exact_history_identity(
    monkeypatch, tmp_path,
):
    timeline = FakeTimeline(tmp_path / "outbox.json")
    path, record, entry = _legacy_ack_history_setup(timeline)
    monkeypatch.setattr(retry_module.clash, "_history", lambda *args: (HEAD, [entry]))

    landed = retry_module._landed(timeline, path, record)

    saved = timeline.session.state["pending_changes"]["pc_saved"]
    assert landed["status"] == "landed"
    assert landed["data"]["publication_id"] == "pub-history"
    assert saved["state"] == "landed"
    assert saved["closed_by"] == {
        "actor_id": "actor-1", "publication_id": "pub-history", "how": "retry"}


@pytest.mark.parametrize("flight_changes, entry_changes", [
    ({"key": "different-flight-key"}, {}),
    ({}, {"publisher": "actor-2"}),
    ({}, {"receipt": {"receipt_id": "receipt-history",
                      "idempotency_key": "unrelated-flight-key"}}),
    ({"pending_path": "/another/outbox.json"}, {}),
    ({"pending_id": "another-pending-id"}, {}),
    ({"pending_part": 1}, {}),
])
def test_landed_does_not_close_from_unrelated_or_unowned_history(
    monkeypatch, tmp_path, flight_changes, entry_changes,
):
    timeline = FakeTimeline(tmp_path / "outbox.json")
    path, record, entry = _legacy_ack_history_setup(
        timeline, flight_changes=flight_changes, entry_changes=entry_changes,
    )
    before = copy.deepcopy(timeline.session.state["pending_changes"]["pc_saved"])
    monkeypatch.setattr(retry_module.clash, "_history", lambda *args: (HEAD, [entry]))

    landed = retry_module._landed(timeline, path, record)

    assert landed is None
    assert timeline.session.state["pending_changes"]["pc_saved"] == before


def test_multi_part_preview_ack_advances_without_closing(monkeypatch, tmp_path):
    timeline = FakeTimeline(tmp_path / "outbox.json")
    record = timeline.session.state["pending_changes"]["pc_saved"]
    record["parts"].append(copy.deepcopy(record["parts"][0]))
    monkeypatch.setattr(retry_module, "_probe", lambda *args: {
        "would_status": "accepted", "evaluated_head": HEAD, "canonical_document": {}})
    monkeypatch.setattr(retry_module, "_preview_diff", lambda *args: {"changes": []})
    preview = retry_module.retry(timeline, "pc_saved")

    retry_module.commit_preview(timeline, preview)

    saved = timeline.session.state["pending_changes"]["pc_saved"]
    assert saved["state"] == "pending"
    assert saved["part"] == 1
    assert timeline.session.submissions[0][1]["pending_id"] == "pc_saved"


def test_retry_after_repeated_refusal_uses_rederived_commands_before_suffix(monkeypatch, tmp_path):
    timeline = FakeTimeline(tmp_path / "outbox.json")
    record = timeline.session.state["pending_changes"]["pc_saved"]
    record["origin"] = "function"
    record["failure"]["details"].update(status="conflict", reason="guard_changed")
    current = [{"op": "clip.move", "select": "clip:b", "args": {"on": 9}}]
    suffix = copy.deepcopy(record["parts"][0])
    suffix["base"] = HEAD
    suffix["commands"] = [{"op": "clip.move", "select": "clip:a", "args": {"on": 12}}]
    suffix["commit_options"] = {"label": "Follow-up", "steps": [], "merge": "fields"}
    record["parts"][0]["commands"] = copy.deepcopy(current)
    record["parts"].append(suffix)
    stable_id = record["id"]
    monkeypatch.setattr(retry_module, "_probe", lambda *args: {
        "would_status": "accepted", "evaluated_head": HEAD, "canonical_document": {}})
    monkeypatch.setattr(retry_module, "_preview_diff", lambda *args: {"changes": []})

    preview = retry_module.retry(timeline, stable_id)

    assert preview["data"]["commands"] == current
    result = retry_module.commit_preview(timeline, preview)

    assert result["status"] == "part_landed"
    args, kwargs = timeline.session.submissions[0]
    assert args[0] == current
    assert kwargs["pending_id"] == stable_id
    saved = timeline.session.state["pending_changes"][stable_id]
    assert saved["id"] == stable_id
    assert saved["state"] == "pending"
    assert saved["part"] == 1
    assert saved["receipts"] == ["pub-accepted"]
    assert saved["parts"][1]["commands"] == suffix["commands"]
    assert [part["commands"] for part in saved["parts"]] == [current, suffix["commands"]]
    suffix_preview = retry_module.retry(timeline, stable_id)
    assert suffix_preview["status"] == "preview"
    assert suffix_preview["data"]["commands"] == suffix["commands"]


def test_commit_preview_accepts_records_without_saved_steps(monkeypatch, tmp_path):
    timeline = FakeTimeline(tmp_path / "outbox.json")
    timeline.session.state["pending_changes"]["pc_saved"]["parts"][0]["commit_options"].pop("steps")
    monkeypatch.setattr(retry_module, "_probe", lambda *args: {
        "would_status": "accepted", "evaluated_head": HEAD, "canonical_document": {}})
    monkeypatch.setattr(retry_module, "_preview_diff", lambda *args: {"changes": []})

    preview = retry_module.retry(timeline, "pc_saved")
    retry_module.commit_preview(timeline, preview)

    assert timeline.session.submissions[0][1]["steps"] == []


def test_unknown_outcome_is_not_converted_to_refusal(monkeypatch, tmp_path):
    timeline = FakeTimeline(tmp_path / "outbox.json")
    monkeypatch.setattr(retry_module, "_probe", lambda *args: {
        "would_status": "accepted", "evaluated_head": HEAD, "canonical_document": {}})
    monkeypatch.setattr(retry_module, "_preview_diff", lambda *args: {"changes": []})
    preview = retry_module.retry(timeline, "pc_saved")
    timeline.session.submit = lambda *args, **kwargs: (_ for _ in ()).throw(TimeoutError("unknown"))

    with pytest.raises(TimeoutError, match="unknown"):
        retry_module.commit_preview(timeline, preview)


def test_retry_detects_prior_group_id_and_closes_cross_actor_record(monkeypatch, tmp_path):
    timeline = FakeTimeline(tmp_path / "outbox.json")
    entry = {"group_id": "pc_saved", "publication_id": "pub-retry", "revision_id": "r3",
             "publisher": "actor-2", "receipt": {"receipt_id": "receipt-retry"}}
    monkeypatch.setattr(retry_module.clash, "_history", lambda *args: (HEAD, [entry]))

    result = retry_module.retry(timeline, "pc_saved")

    saved = timeline.session.state["pending_changes"]["pc_saved"]
    assert result["status"] == "landed"
    assert saved["state"] == "landed"
    assert saved["closed_by"] == {"actor_id": "actor-2", "publication_id": "pub-retry", "how": "retry"}
    assert saved["receipts"] == ["receipt-retry"]


def test_retry_returns_history_receipt_when_another_actor_already_landed(monkeypatch, tmp_path):
    timeline = FakeTimeline(tmp_path / "outbox.json")
    entry = {"group_id": "pc_saved", "publication_id": "pub-retry", "revision_id": "r3",
             "publisher": "actor-2", "receipt": {"receipt_id": "receipt-retry"}}
    monkeypatch.setattr(retry_module.clash, "_history", lambda *args: (HEAD, [entry]))

    result = retry_module.retry(timeline, "pc_saved")

    assert result["data"] == {
        "publication_id": "pub-retry",
        "receipt": {"receipt_id": "receipt-retry"},
    }
    assert result["summary"] == (
        '"Recut" already landed as pub-retry (by actor-2); '
        'pc_saved is closed. Nothing was sent.'
    )
    assert set(result) == {"status", "pending_id", "data", "summary", "next", "record"}
    assert timeline.session.submissions == []


def test_two_actors_retry_owner_record_and_refused_loser_releases_flight(
    editing_case, tmp_path, monkeypatch,
):
    daemon, client, project, _ = editing_case
    monkeypatch.setenv("ASTRID_RUNTIME_DATA_ROOT", str(tmp_path / "two-actor-data"))

    from astrid.sdk.timeline import Timeline
    from astrid.sdk.timeline.sync import CommitConflict
    from tests.editing_conformance.test_commit_runtime_m1 import open_client
    from tests.editing_conformance.test_timeline_clash_back_acceptance_runtime import (
        _move_commands, _seed_clip,
    )
    from astrid_runtime_client import ApiError

    owner = Timeline.open(project, "film", client=client)
    _seed_clip(owner, tmp_path)
    commands = _move_commands(owner, on=4, label="Two actor retry")
    _, credential = daemon.credentials.provision("retry-peer", [
        "handshake", "projects:read", "projects:write", "objects:read", "objects:write",
        "tasks:read", "tasks:write",
    ])
    peer_client = open_client(daemon, actor="retry-peer", credential=credential)
    peer = Timeline.open(project, "film", client=peer_client)
    owner_transport = client._remote._transport._generated
    original = owner_transport.commit_timeline
    initial = ApiError(409, "conflict", "initial conflict", details={
        "status": "conflict", "reason": "guard_changed", "conflicts": [],
    })

    def refuse_initial(project_id_arg, timeline_id_arg, body, **kwargs):
        if body.get("label") == "Two actor retry":
            raise initial
        return original(project_id_arg, timeline_id_arg, body, **kwargs)

    monkeypatch.setattr(owner_transport, "commit_timeline", refuse_initial)
    with pytest.raises(CommitConflict) as caught:
        owner.commit(commands, label="Two actor retry", key="two-actor-retry")
    pending_id = caught.value.pending_id
    assert pending_id
    monkeypatch.setattr(owner_transport, "commit_timeline", original)

    owner_preview = owner.retry(pending_id)
    peer_preview = peer.retry(pending_id)
    assert owner_preview["status"] == peer_preview["status"] == "preview"
    owner_path = owner.session.outbox.path
    assert peer_preview["_retry_path"] == str(owner_path)

    nested = []
    refused = ApiError(409, "refused", "losing retry refused", details={"status": "refused"})

    def peer_wins_then_refuse_owner(*args, **kwargs):
        assert json.loads(owner_path.read_text())["flight"] is not None
        nested.append(peer.commit(peer_preview))
        assert nested[0]["receipt"]["receipt_id"]
        # The peer settles the shared owner record while this owner's flight is
        # still on disk; this refusal must release only that flight.
        raise refused

    monkeypatch.setattr(owner.session.client.timelines, "commit", peer_wins_then_refuse_owner)
    loser = owner.commit(owner_preview)

    state = json.loads(owner_path.read_text())
    saved = state["pending_changes"][pending_id]
    assert state["flight"] is None
    assert saved["state"] == "landed"
    assert saved["receipts"] == [nested[0]["receipt"]["receipt_id"]]
    monkeypatch.setattr(owner_transport, "commit_timeline", original)
    assert loser["status"] == "landed"
    assert loser["data"]["receipt"]["receipt_id"] == nested[0]["receipt"]["receipt_id"]


def test_retry_replays_landed_multipart_prefix_then_lands_suffix(
    editing_case, tmp_path, monkeypatch,
):
    daemon, client, project, _ = editing_case
    monkeypatch.setenv("ASTRID_RUNTIME_DATA_ROOT", str(tmp_path / "multipart-data"))

    from astrid.sdk.timeline import Timeline
    from astrid.sdk.timeline.capture import capture_move
    from tests.editing_conformance.test_commit_runtime_m1 import open_client
    from tests.editing_conformance.test_timeline_clash_back_acceptance_runtime import _media_file, _seed_clip

    owner = Timeline.open(project, "film", client=client)
    _seed_clip(owner, tmp_path)
    owner.commit(lambda t: t.add(_media_file(tmp_path / "c.png"), at=3, hold=3,
                                 track="V1", id="c"), label="Seed c", key="seed-c")
    _, credential = daemon.credentials.provision("multipart-peer", [
        "handshake", "projects:read", "projects:write", "objects:read", "objects:write",
        "tasks:read", "tasks:write",
    ])
    peer = Timeline.open(project, "film", client=open_client(
        daemon, actor="multipart-peer", credential=credential))
    bundle = owner.bundle
    commands = [capture_move(bundle, "parent/clip:b", 4),
                capture_move(bundle, "parent/clip:c", 6)]
    record = {
        "id": "multipart-retry", "state": "pending", "key": "multipart-retry",
        "label": "Multipart retry", "origin": "list", "part": 0, "attempts": 1,
        "parts": [
            {"base": copy.deepcopy(owner.session._state()["base"]), "commands": [commands[0]],
             "commit_options": {"label": "Multipart retry", "merge": "fields", "steps": [
                 {"id": "part-1", "label": "Multipart retry", "intent": "", "tags": [],
                  "command_ordinals": [0], "patch": []},
             ]},
             "group_id": "multipart-retry"},
            {"base": copy.deepcopy(owner.session._state()["base"]), "commands": [commands[1]],
             "commit_options": {"label": "Multipart retry", "merge": "fields", "steps": [
                 {"id": "part-2", "label": "Multipart retry", "intent": "", "tags": [],
                  "command_ordinals": [0], "patch": []},
             ]},
             "group_id": "multipart-retry"},
        ],
        "failure": {"details": {"status": "conflict", "reason": "guard_changed", "conflicts": []}},
        "head": copy.deepcopy(owner.session._state()["base"]), "closed_by": None,
    }
    state = owner.session._state()
    state["pending"] = []
    state.setdefault("pending_changes", {})[record["id"]] = record
    owner.session.outbox.write(state)

    prefix = peer.session.submit(
        [commands[0]], label="Multipart retry",
        steps=[{"id": "part-1", "label": "Multipart retry", "intent": "", "tags": [],
                "command_ordinals": [0], "patch": []}],
        expected_state=peer.session._state(), base=record["parts"][0]["base"],
        group_id=record["id"],
    )
    assert prefix["receipt"]["receipt_id"]
    recovered = owner.retry(record["id"])
    assert recovered["status"] == "part_landed"
    assert recovered["record"]["part"] == 1

    suffix_preview = owner.retry(record["id"])
    assert suffix_preview["status"] == "preview"
    suffix = owner.commit(suffix_preview)
    assert suffix["status"] == "accepted", suffix
    saved = owner.session._state()["pending_changes"][record["id"]]
    assert saved["state"] == "landed"
    assert saved["part"] == 1
    assert saved["receipts"] == [prefix["receipt"]["receipt_id"], suffix["receipt"]["receipt_id"]]


def test_retry_returns_uniform_result_for_saved_landed_record(monkeypatch, tmp_path):
    timeline = FakeTimeline(tmp_path / "outbox.json")
    record = timeline.session.state["pending_changes"]["pc_saved"]
    record["state"] = "landed"
    record["closed_by"] = {"actor_id": "actor-2", "publication_id": "pub-retry", "how": "retry"}
    record["receipts"] = ["receipt-retry"]
    monkeypatch.setattr(retry_module.clash, "_history", lambda *args: pytest.fail("history not needed"))

    result = retry_module.retry(timeline, "pc_saved")

    assert result["status"] == "landed"
    assert result["data"] == {
        "publication_id": "pub-retry", "receipt": {"receipt_id": "receipt-retry"}}
    assert set(result) == {"status", "pending_id", "data", "summary", "next", "record"}
    assert result["record"] == record
    assert result["next"] is None
    assert timeline.session.submissions == []


def test_landed_state_wins_over_old_refusal_after_rederive(monkeypatch, tmp_path):
    timeline = FakeTimeline(tmp_path / "outbox.json")
    record = timeline.session.state["pending_changes"]["pc_saved"]
    record["failure"]["details"]["status"] = "refused"

    refused = retry_module.retry(timeline, "pc_saved")
    assert refused["status"] == "refused"

    record["failure"]["details"]["status"] = "conflict"
    monkeypatch.setattr(retry_module, "_probe", lambda *args: {
        "would_status": "conflict", "details": {"conflicts": []}})
    monkeypatch.setattr(retry_module.clash, "explain", lambda *args: {"receipts": []})
    rederived = retry_module.retry(timeline, "pc_saved")
    assert rederived["status"] == "re_derive"

    sent = timeline.session.submit([], pending_id="pc_saved", pending_how="retry", base=HEAD)
    assert sent["status"] == "accepted"
    record["failure"]["details"]["status"] = "refused"
    submissions_before_retry = list(timeline.session.submissions)
    monkeypatch.setattr(retry_module.clash, "_history",
                        lambda *args: pytest.fail("saved landed state should be authoritative"))

    landed = retry_module.retry(timeline, "pc_saved")

    assert landed["status"] == "landed"
    assert landed["data"] == {
        "publication_id": "pub-accepted", "receipt": {"receipt_id": "pub-accepted"}}
    assert landed["summary"].endswith("Nothing was sent.")
    assert timeline.session.submissions == submissions_before_retry


def _same_value_conflict_setup(monkeypatch, timeline, *, partial=False):
    record = timeline.session.state["pending_changes"]["pc_saved"]
    record["failure"]["details"]["conflicts"] = [
        {"ref": "parent/clip:a", "path": ["at"]},
        {"ref": "parent/clip:b", "path": ["at"]},
    ]
    record["diff"] = [
        {"op": "set", "ref": "parent/clip:a", "path": ["at"], "value": 4},
        {"op": "set", "ref": "parent/clip:b", "path": ["at"], "value": 6},
    ]
    entry = {"publication_id": "pub-peer", "revision_id": "r3", "publisher": "actor-2",
             "receipt": {"receipt_id": "receipt-peer"}}
    packet = {"receipts": [{"publication_id": "pub-peer", "publisher": "actor-2",
                            "touches_conflict": True}]}
    monkeypatch.setattr(retry_module.clash, "_history", lambda *args: (HEAD, [entry]))
    monkeypatch.setattr(retry_module.clash, "_closures", lambda *args: (
        SimpleNamespace(), SimpleNamespace(images={"parent": {"payload": {}}})))
    monkeypatch.setattr(retry_module, "canonical_site",
                        lambda _closure, ref, path: ("parent", [ref, *path]))
    monkeypatch.setattr(retry_module, "value_at", lambda _payload, _ref, _path:
                        {"state": "present", "value": 4 if _path[-2] == "parent/clip:a"
                         else 5 if partial else 6})
    monkeypatch.setattr(retry_module.clash, "_target",
                        lambda conflict, *args: f"clip {conflict['ref'][-1]} start time")
    monkeypatch.setattr(retry_module.clash, "explain", lambda *args: copy.deepcopy(packet))
    probe_calls = []

    def probe(*args):
        probe_calls.append(args)
        return {"would_status": "conflict",
                "details": {"conflicts": record["failure"]["details"]["conflicts"]}}

    monkeypatch.setattr(retry_module, "_probe", probe)
    return probe_calls


def test_retry_closes_when_every_conflict_already_has_desired_value(monkeypatch, tmp_path):
    timeline = FakeTimeline(tmp_path / "outbox.json")
    probe_calls = _same_value_conflict_setup(monkeypatch, timeline)

    result = retry_module.retry(timeline, "pc_saved")

    saved = timeline.session.state["pending_changes"]["pc_saved"]
    assert result["status"] == "landed"
    assert result["data"] == {
        "publication_id": "pub-peer", "receipt": {"receipt_id": "receipt-peer"}}
    assert set(result) == {"status", "pending_id", "data", "record", "summary", "next"}
    assert result["summary"] == (
        "Nothing to do: clip a start time, clip b start time already has your value "
        "(set by actor-2); pc_saved is closed.")
    assert saved["state"] == "landed"
    assert saved["closed_by"] == {
        "actor_id": "actor-2", "publication_id": "pub-peer", "how": "already_applied"}
    assert probe_calls == []
    assert timeline.session.submissions == []


def test_retry_partial_same_value_conflict_stays_pending_and_rederive(monkeypatch, tmp_path):
    timeline = FakeTimeline(tmp_path / "outbox.json")
    probe_calls = _same_value_conflict_setup(monkeypatch, timeline, partial=True)

    result = retry_module.retry(timeline, "pc_saved")

    saved = timeline.session.state["pending_changes"]["pc_saved"]
    assert result["status"] == "re_derive"
    assert saved["state"] == "pending"
    assert saved["closed_by"] is None
    assert probe_calls
    assert timeline.session.submissions == []


def test_retry_already_applied_is_idempotent(monkeypatch, tmp_path):
    timeline = FakeTimeline(tmp_path / "outbox.json")
    probe_calls = _same_value_conflict_setup(monkeypatch, timeline)
    first = retry_module.retry(timeline, "pc_saved")
    closed_by = copy.deepcopy(timeline.session.state["pending_changes"]["pc_saved"]["closed_by"])
    receipts = list(timeline.session.state["pending_changes"]["pc_saved"]["receipts"])

    second = retry_module.retry(timeline, "pc_saved")

    assert first["status"] == second["status"] == "landed"
    assert timeline.session.state["pending_changes"]["pc_saved"]["closed_by"] == closed_by
    assert timeline.session.state["pending_changes"]["pc_saved"]["receipts"] == receipts
    assert probe_calls == []
    assert timeline.session.submissions == []


def test_refused_function_retry_recovers_saved_suffix_once_in_receipt_order(monkeypatch, tmp_path):
    timeline = FakeTimeline(tmp_path / "outbox.json")
    record = timeline.session.state["pending_changes"]["pc_saved"]
    record["origin"] = "function"
    record["failure"]["details"].update(status="conflict", reason="guard_changed")
    suffix = copy.deepcopy(record["parts"][0])
    suffix["base"] = HEAD
    suffix["commands"] = [{"op": "clip.move", "select": "clip:b", "args": {"on": 8}}]
    suffix["commit_options"] = {"label": "Follow-up", "steps": [], "merge": "fields"}
    record["parts"].append(suffix)
    history = lambda *args: (HEAD, [])
    monkeypatch.setattr(retry_module.clash, "_history", history)
    monkeypatch.setattr(retry_module, "_probe", lambda *args: {
        "would_status": "accepted", "evaluated_head": HEAD, "canonical_document": {}})
    monkeypatch.setattr(retry_module, "_preview_diff", lambda *args: {"changes": []})

    accepted = [
        {"status": "accepted", "receipt": {"receipt_id": "receipt-function"}},
        {"status": "accepted", "receipt": {"receipt_id": "receipt-suffix"}},
    ]
    publications = ["pub-function", "pub-suffix"]

    def submit(*args, **kwargs):
        timeline.session.submissions.append((args, kwargs))
        from astrid.sdk.timeline.sync import _settle_pending

        saved = timeline.session.state["pending_changes"][kwargs["pending_id"]]
        index = len(timeline.session.submissions) - 1
        publication = publications[index]
        base = {"revision_id": f"r{index + 3}", "publication_id": publication}
        timeline.session.state["base"] = base
        _settle_pending(saved, expected_part=kwargs.get("pending_part", saved.get("part", 0)),
                        actor_id="actor-1", publication_id=publication,
                        how=kwargs["pending_how"], base=base)
        return accepted[index]

    timeline.session.submit = submit

    def commit(function, **kwargs):
        assert function is edit_again
        assert kwargs == {"label": "Recut", "key": "recut", "pending": "pc_saved"}
        return timeline.session.submit([], pending_id=kwargs["pending"], pending_how="rerun")

    def edit_again(working):
        return None

    timeline.commit = commit

    first = retry_module.retry(timeline, "pc_saved", fn=edit_again)

    saved = timeline.session.state["pending_changes"]["pc_saved"]
    assert first["receipt"]["receipt_id"] == "receipt-function"
    assert saved["state"] == "pending"
    assert saved["part"] == 1
    assert saved["receipts"] == ["pub-function"]
    assert saved["parts"][1]["base"] == {"revision_id": "r3", "publication_id": "pub-function"}

    preview = retry_module.retry(timeline, "pc_saved")
    second = retry_module.commit_preview(timeline, preview)

    saved = timeline.session.state["pending_changes"]["pc_saved"]
    assert second["receipt"]["receipt_id"] == "receipt-suffix"
    assert saved["state"] == "landed"
    assert saved["part"] == 1
    assert saved["receipts"] == ["pub-function", "pub-suffix"]
    assert saved["closed_by"] == {
        "actor_id": "actor-1", "publication_id": "pub-suffix", "how": "retry"}
    assert [kwargs["pending_how"] for _, kwargs in timeline.session.submissions] == ["rerun", "retry"]

    durable = copy.deepcopy(saved)
    final = retry_module.retry(timeline, "pc_saved")

    assert final["status"] == "landed"
    assert timeline.session.state["pending_changes"]["pc_saved"] == durable
    assert len(timeline.session.submissions) == 2
