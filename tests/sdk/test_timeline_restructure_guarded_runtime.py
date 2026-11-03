"""Bounded real-Runtime qualification for restructure's guarded inverse."""
from __future__ import annotations

import copy
import inspect
import json
import sys

import pytest

from astrid.sdk import timeline
from astrid.sdk.timeline._common import TimelineEditError
from astrid.sdk.timeline.checkout import Checkout
from astrid.sdk.timeline.sync import CommitConflict
from astrid.sdk.workspace_client import WorkspaceClientError
from tests.sdk.test_timeline_restructure import restructure_runtime  # noqa: F401


@pytest.fixture
def restructure_inverse_runtime(restructure_runtime, monkeypatch):  # noqa: F811
    """Use the production authenticated revision/publication-pair reader for this test."""
    from astrid.sdk.remote import RemoteTimelines

    env = restructure_runtime
    env.reader_calls = []

    # Bind the public inspection/history calls at the same boundary as the
    # generated client.  The inherited fixture's two-argument inspection
    # stand-in predates the revision-pinned route and cannot exercise it.
    def list_timelines(project_id, *, cursor=None, limit=50):
        env.reader_calls.append({"method": "list_timelines", "project_id": project_id,
                                 "cursor": cursor, "limit": limit})
        page = env.service.list_timelines(project_id, cursor=cursor, limit=limit)
        return page["items"], page.get("next_cursor")

    def inspect_timeline(project_id, timeline_id, options=None):
        env.reader_calls.append({"method": "inspect_timeline", "project_id": project_id,
                                 "timeline_id": timeline_id, "options": copy.deepcopy(options)})
        return env.service.inspect_timeline(project_id, timeline_id, options)

    def list_timeline_commits(project_id, timeline_id, *, cursor=None, limit=50):
        env.reader_calls.append({"method": "list_timeline_commits", "project_id": project_id,
                                 "timeline_id": timeline_id, "cursor": cursor, "limit": limit})
        return env.service.list_timeline_commits(
            project_id, timeline_id, cursor=cursor, limit=limit, authorize=lambda: None
        )

    monkeypatch.setattr(env.generated, "list_timelines", list_timelines, raising=False)
    monkeypatch.setattr(env.generated, "inspect_timeline", inspect_timeline, raising=False)
    monkeypatch.setattr(env.generated, "list_timeline_commits", list_timeline_commits, raising=False)
    monkeypatch.setattr(
        env.client.timelines,
        "_resolve_timeline",
        RemoteTimelines._resolve_timeline.__get__(env.client.timelines),
    )
    monkeypatch.setattr(
        env.client.timelines,
        "open_composition",
        RemoteTimelines.open_composition.__get__(env.client.timelines),
    )
    monkeypatch.setattr(
        env.generated,
        "get_project_parent_composition_revision",
        lambda project, timeline_id, revision: env.service.get_project_parent_composition_revision(
            project, timeline_id, revision
        ),
        raising=False,
    )
    monkeypatch.setattr(
        env.generated,
        "get_project_shot_revision",
        lambda project, shot_id, revision: env.service.get_project_shot_revision(project, shot_id, revision),
        raising=False,
    )
    monkeypatch.setattr(
        env.generated,
        "get_project_timeline_revision",
        lambda project, timeline_id, revision: env.service.get_project_timeline_revision(
            project, timeline_id, revision
        ),
        raising=False,
    )
    monkeypatch.setattr(
        env.client.timelines,
        "open_bundle",
        RemoteTimelines.open_bundle.__get__(env.client.timelines),
    )

    def document():
        head = env.service.get_project_timeline(env.project, "t")
        opened = env.client.timelines.open_bundle(
            env.project,
            "t",
            revision_id=head["head_revision_id"],
            publication_id=head["head_publication_id"],
        )
        assert opened.ok, opened.as_dict()
        return copy.deepcopy(opened.data["bundle"]["source_mapping"]["_runtime_document"])

    env.document = document
    return env


def test_fixture_restructure_public_call_hydrates_exact_current_closure(
    restructure_inverse_runtime, tmp_path, monkeypatch
):
    from astrid.sdk.timeline import publish as publish_module

    env = restructure_inverse_runtime
    reads = []
    public_open = env.client.timelines.open_bundle

    def open_bundle(project, ref, *, revision_id=None, publication_id=None):
        result = public_open(project, ref, revision_id=revision_id, publication_id=publication_id)
        reads.append({"args": {"project": project, "ref": ref, "revision_id": revision_id,
                               "publication_id": publication_id}, "result": result.as_dict()})
        return result

    monkeypatch.setattr(env.client.timelines, "open_bundle", open_bundle)
    source = timeline.open(env.project, "t", client=env.client, outbox_path=tmp_path / "name.json")
    source.bundle["shots"][source._shot_ids()[0]]["payload"]["name"] = "02 TWO"
    named = source.commit("Name source chapter", idempotency_key="inverse-name")
    checkout = Checkout.open(env.project, "t", client=env.client)
    detached = copy.deepcopy(checkout.bundle)
    history = copy.deepcopy(env.client.timelines.history(env.project, "t", limit=200).data)
    capture = {}
    lines, start = inspect.getsourcelines(publish_module._PublishMixin._commit_restructure)
    guard_line = start + next(i for i, line in enumerate(lines)
                             if 'if (local["base_parent"] != base["base_parent"]' in line)
    passed_line = start + next(i for i, line in enumerate(lines)
                              if 'candidate = copy.deepcopy(base)' in line)

    class ClosureVerified(Exception):
        """Stop this read-only smoke after the real closure guard has passed."""

    def observe(frame, event, arg):
        if (frame.f_code is publish_module._PublishMixin._commit_restructure.__code__
                and event == "line" and frame.f_lineno == guard_line):
            capture.update(local=copy.deepcopy(frame.f_locals["local"]),
                           base=copy.deepcopy(frame.f_locals["base"]),
                           state=copy.deepcopy(frame.f_locals["state"]))
        if (frame.f_code is publish_module._PublishMixin._commit_restructure.__code__
                and event == "line" and frame.f_lineno == passed_line):
            raise ClosureVerified
        return observe

    previous_trace = sys.gettrace()
    before_store = tuple(env.service.store.conn.iterdump())
    before_calls = copy.deepcopy(env.calls)
    try:
        sys.settrace(observe)
        with pytest.raises(ClosureVerified):
            checkout.publish("Restructure", restructure=True, client=env.client,
                             idempotency_key="inverse-cut")
    finally:
        sys.settrace(previous_trace)
        report = {"detached": detached, "detached_document": checkout.document(),
                  "guard": capture, "public_reads": reads, "history_before": history,
                  "named_reply": named, "reader_calls": env.reader_calls}
        (tmp_path / "closure-preflight.json").write_text(json.dumps(report, sort_keys=True))
        print(f"Full closure preflight: {tmp_path / 'closure-preflight.json'}")
    assert capture["local"] == capture["base"]
    assert capture["local"]["source_mapping"]["_runtime_document"] == named["data"]["canonical_document"]
    assert capture["local"]["base_parent"]["publication_id"] == named["receipt"]["receipt_id"]
    assert env.calls == before_calls
    assert env.client.timelines.history(env.project, "t", limit=200).data == history
    assert env.document() == named["data"]["canonical_document"]
    assert tuple(env.service.store.conn.iterdump()) == before_store


@pytest.mark.parametrize("damage", ["digest", "pair", "historical", "scope"])
def test_fixture_restructure_caller_refuses_mismatched_original_closure_without_writes(
    restructure_inverse_runtime, damage, tmp_path
):
    env = restructure_inverse_runtime
    checkout = Checkout.open(env.project, "t", client=env.client)
    if damage == "historical":
        current = timeline.open(env.project, "t", client=env.client,
                                outbox_path=tmp_path / "advance.json")
        current.bundle["shots"][current._shot_ids()[0]]["payload"]["name"] = "02 TWO"
        current.commit("Advance head", idempotency_key="advance-head")
    history = copy.deepcopy(env.client.timelines.history(env.project, "t", limit=200).data)
    historical = next(item for item in history["items"]
                      if item["revision_id"] != history["head"]["revision_id"])
    if damage == "digest":
        checkout.bundle["base_parent"]["content_digest"] = "sha256:" + "0" * 64
    elif damage == "pair":
        checkout.bundle["base_parent"]["publication_id"] = historical["publication_id"]
    elif damage == "scope":
        checkout.bundle["timeline_id"] = "missing-timeline"
    document = copy.deepcopy(env.document())
    stored = tuple(env.service.store.conn.iterdump())
    calls = copy.deepcopy(env.calls)
    with pytest.raises(TimelineEditError) as refused:
        checkout.publish("Restructure", restructure=True, client=env.client,
                         idempotency_key="bad-closure-" + damage)
    if damage == "digest":
        assert str(refused.value) == "restructure requires its exact current closure; reopen the timeline"
    elif damage in {"pair", "historical"}:
        assert "original revision/publication pair changed" in str(refused.value)
    else:
        assert "could not open timeline 'missing-timeline'" in str(refused.value)
    assert env.calls == calls
    assert env.client.timelines.history(env.project, "t", limit=200).data == history
    assert env.document() == document
    assert tuple(env.service.store.conn.iterdump()) == stored


def test_fixture_reader_authorizes_exact_pair_and_refuses_wrong_scope_without_writes(
    restructure_inverse_runtime, tmp_path, monkeypatch
):  # noqa: F811
    env = restructure_inverse_runtime
    head = env.service.get_project_timeline(env.project, "t")
    before_history = copy.deepcopy(env.client.timelines.history(env.project, "t", limit=200).data)
    before_document = copy.deepcopy(env.document())
    before_store = tuple(env.service.store.conn.iterdump())
    historical = next(
        item
        for item in before_history["items"]
        if item["revision_id"] != head["head_revision_id"]
        and isinstance(item.get("receipt", {}).get("result", {}).get("canonical_document"), dict)
    )

    opened = timeline.open(
        env.project, "t", client=env.client, outbox_path=tmp_path / "fixture-reader.json"
    )
    assert opened.head["revision_id"] == head["head_revision_id"]
    opened_bundle = opened.session._bundle(opened.session._state())
    assert opened_bundle["base_parent"]["publication_id"] == opened.head["publication_id"]
    base = {"revision_id": historical["revision_id"], "publication_id": historical["publication_id"]}
    exact = env.client.timelines.open_bundle(
        env.project,
        "t",
        revision_id=base["revision_id"],
        publication_id=base["publication_id"],
    )
    assert exact.ok, exact.as_dict()
    assert exact.data["project_id"] == env.project
    assert exact.data["timeline_id"] == "t"
    assert exact.data["revision_id"] == base["revision_id"] != head["head_revision_id"]
    assert exact.data["head_revision_id"] == head["head_revision_id"]
    assert exact.data["is_current_head"] is False
    assert exact.data["bundle"]["base_parent"]["revision_id"] == base["revision_id"]
    assert exact.data["bundle"]["base_parent"]["publication_id"] == base["publication_id"]
    runtime_document = exact.data["bundle"]["source_mapping"]["_runtime_document"]
    assert runtime_document["parent_revision"]["revision_id"] == base["revision_id"]
    assert runtime_document["parent_revision"]["content_digest"]
    assert historical["receipt"]["receipt_id"] == base["publication_id"]
    assert runtime_document == historical["receipt"]["result"]["canonical_document"]

    wrong_pair = env.client.timelines.open_bundle(
        env.project,
        "t",
        revision_id=base["revision_id"],
        publication_id=head["head_publication_id"],
    )
    assert not wrong_pair.ok
    assert wrong_pair.error.code == "protocol_error"

    wrong_scope = env.client.timelines.open_bundle(
        env.project,
        "missing-timeline",
        revision_id=base["revision_id"],
        publication_id=base["publication_id"],
    )
    assert not wrong_scope.ok
    assert wrong_scope.error.code == "not_found"

    read_parent = env.generated.get_project_parent_composition_revision

    def wrong_digest(project, timeline_id, revision):
        parent = copy.deepcopy(read_parent(project, timeline_id, revision))
        parent["content_digest"] = "sha256:" + "0" * 64
        return parent

    with monkeypatch.context() as damaged:
        damaged.setattr(env.generated, "get_project_parent_composition_revision", wrong_digest)
        refused = env.client.timelines.open_bundle(
            env.project, "t", revision_id=base["revision_id"], publication_id=base["publication_id"]
        )
        assert not refused.ok
        assert refused.error.code == "protocol_error"

    assert env.client.timelines.history(env.project, "t", limit=200).data == before_history
    assert env.service.get_project_timeline(env.project, "t")["head_revision_id"] == head["head_revision_id"]
    assert env.document() == before_document
    assert tuple(env.service.store.conn.iterdump()) == before_store


def test_fixture_native_restructure_accepts_only_known_exact_pin_fields(
    restructure_inverse_runtime,
):
    from astrid.sdk.timeline._command_state import CommandError
    from astrid.sdk.timeline.commands import execute
    from astrid.sdk.timeline.commit import bundle_from_document

    env = restructure_inverse_runtime
    head = env.client.timelines.history(env.project, "t", limit=1).data["head"]
    opened = env.client.timelines.open_bundle(env.project, "t", **head)
    assert opened.ok, opened.as_dict()
    bundle = opened.data["bundle"]
    original = copy.deepcopy(bundle)
    stored = tuple(env.service.store.conn.iterdump())
    history = copy.deepcopy(env.client.timelines.history(env.project, "t", limit=200).data)
    command = {"schema_version": 1, "op": "timeline.restructure", "version": "1",
               "select": "parent", "args": {}}
    legacy = bundle_from_document(bundle["source_mapping"]["_runtime_document"])
    assert set(legacy["base_parent"]) == {"revision_id", "content_digest"}
    assert set(bundle["base_parent"]) == {"revision_id", "content_digest", "publication_id"}
    prepared = execute(bundle, [command])
    assert prepared.records == execute(legacy, [command]).records
    assert prepared.bundle["base_parent"] == original["base_parent"]
    assert prepared.bundle["source_mapping"] == original["source_mapping"]

    for damage in ("revision", "digest", "project", "timeline", "unknown", "publication", "closure"):
        damaged = copy.deepcopy(bundle)
        if damage == "revision":
            damaged["base_parent"]["revision_id"] = "wrong-revision"
        elif damage == "digest":
            damaged["base_parent"]["content_digest"] = "sha256:" + "0" * 64
        elif damage in {"project", "timeline"}:
            damaged[damage + "_id"] = "wrong-scope"
        elif damage == "unknown":
            damaged["base_parent"]["other_publication_id"] = head["publication_id"]
        elif damage == "publication":
            damaged["base_parent"]["publication_id"] = None
        else:
            damaged["source_mapping"]["_runtime_document"]["parent_revision"]["content_digest"] = (
                "sha256:" + "0" * 64
            )
        with pytest.raises(CommandError) as refused:
            execute(damaged, [command])
        assert refused.value.kind == "history_unavailable", damage
    assert bundle == original
    assert env.client.timelines.history(env.project, "t", limit=200).data == history
    assert tuple(env.service.store.conn.iterdump()) == stored


def test_fixture_public_restructure_preview_forward_and_exact_retry(
    restructure_inverse_runtime, tmp_path, monkeypatch
):
    import runtime_protocol

    from astrid.sdk.timeline import native_restructure
    from astrid.sdk.timeline.commit import bundle_from_document
    from astrid.sdk.timeline_restructure import assert_equivalent

    env = restructure_inverse_runtime
    source = timeline.open(env.project, "t", client=env.client, outbox_path=tmp_path / "name.json")
    source.bundle["shots"][source._shot_ids()[0]]["payload"]["name"] = "02 TWO"
    named = source.commit("Name source chapter", idempotency_key="inverse-name")
    before = copy.deepcopy(env.document())
    history_before = copy.deepcopy(env.client.timelines.history(env.project, "t", limit=200).data)
    env.calls.clear()
    env.replies.clear()
    native_inputs = []
    public_reads = []
    public_open = env.client.timelines.open_bundle

    def open_bundle(project, ref, *, revision_id=None, publication_id=None):
        result = public_open(project, ref, revision_id=revision_id, publication_id=publication_id)
        public_reads.append({"project": project, "timeline": ref, "revision_id": revision_id,
                             "publication_id": publication_id, "result": result.as_dict()})
        return result

    def observe(frame, event, arg):
        if frame.f_code is native_restructure.restructure.__code__ and event == "call":
            caller = frame.f_back.f_locals
            native_inputs.append(copy.deepcopy({
                "bundle": frame.f_locals["ctx"].state.bundle,
                "request": caller.get("commit_request"),
                "evaluated_head": caller.get("evaluated_head"),
            }))
        return None

    lost = False

    def send(project, ref, body, *, idempotency_key):
        nonlocal lost
        if not lost:
            preview_body = {**copy.deepcopy(body), "dry_run": True,
                            "rationale": "Preview the chapter cut", "group_id": "chapter-cut"}
            stored = tuple(env.service.store.conn.iterdump())
            preview = env.send(project, ref, preview_body, idempotency_key="pin-preview")
            assert preview["receipt"] is None
            assert preview["data"]["status"] == "preview"
            assert preview["data"]["would_status"] == "accepted"
            assert preview["data"]["evaluated_head"] == history_before["head"]
            assert env.document() == before
            assert env.client.timelines.history(env.project, "t", limit=200).data == history_before
            assert tuple(env.service.store.conn.iterdump()) == stored
        result = env.send(project, ref, body, idempotency_key=idempotency_key)
        if not lost:
            lost = True
            raise WorkspaceClientError(503, "unavailable", "lost restructure ACK")
        return result

    def no_narration_media(**_kwargs):
        raise TimelineEditError("qualification keeps the restructure as one publication")

    monkeypatch.setattr(env.client.timelines, "open_bundle", open_bundle)
    monkeypatch.setattr(env.generated, "commit_timeline", send)
    monkeypatch.setattr(env.client.media, "import_bytes", no_narration_media)
    previous_trace = sys.gettrace()
    try:
        sys.settrace(observe)
        checkout = Checkout.open(env.project, "t", client=env.client)
        with pytest.raises(WorkspaceClientError, match="lost restructure ACK"):
            checkout.publish("Restructure", restructure=True, client=env.client,
                             idempotency_key="inverse-cut")
        after = copy.deepcopy(env.document())
        history_after = copy.deepcopy(env.client.timelines.history(env.project, "t", limit=200).data)
        cut = Checkout.open(env.project, "t", client=env.client).publish(
            "Restructure", restructure=True, client=env.client, idempotency_key="inverse-cut"
        )
    finally:
        sys.settrace(previous_trace)
        capture = {"before": before, "after": env.document(), "named": named,
                   "history_before": history_before,
                   "history_after": env.client.timelines.history(env.project, "t", limit=200).data,
                   "calls": env.calls, "replies": env.replies, "native_inputs": native_inputs,
                   "public_reads": public_reads, "reader_calls": env.reader_calls,
                   "origins": {"native": native_restructure.__file__, "test": __file__,
                               "runtime": runtime_protocol.__file__}}
        (tmp_path / "public-forward.json").write_text(json.dumps(capture, sort_keys=True))
        print(f"Full public forward capture: {tmp_path / 'public-forward.json'}")
    assert len(env.calls) == len(env.replies) == 3
    assert env.calls[1] == env.calls[2]
    assert env.replies[1] == env.replies[2]
    request, key = env.calls[1]
    assert key == "inverse-cut"
    assert request == {"api_version": 1, "project_id": env.project, "timeline_id": "t",
                       "base_revision_id": history_before["head"]["revision_id"],
                       "base_publication_id": history_before["head"]["publication_id"],
                       "commands": request["commands"], "label": "Restructure", "steps": request["steps"],
                       "merge": "atomic", "reads": [{"kind": "scope", "ref": "timeline", "path": []}],
                       "dry_run": False, "source": {"via": "python"}}
    assert request["commands"][-1]["op"] == "timeline.restructure"
    assert request["commands"][-1]["select"] == "parent"
    assert request["commands"][-1]["args"] == {}
    assert request["steps"] == [{"id": f"restructure-{i}", "label": "Restructure",
        "intent": "Store chapters as shots", "tags": ["restructure"], "command_ordinals": [i],
        "patch": copy.deepcopy(call["args"]["changes"]) if call["op"] == "doc.patch" else []}
        for i, call in enumerate(request["commands"])]
    accepted = env.replies[1]
    assert accepted["data"]["status"] == "accepted"
    assert accepted["data"]["canonical_document"] == after
    assert accepted["data"]["document_digest"] == env.replies[0]["data"]["document_digest"]
    assert env.replies[0]["data"]["canonical_document"] == after
    assert accepted["data"]["commands"] == request["commands"]
    assert accepted["data"]["merge_mode"] == "atomic"
    assert cut["receipt"] == accepted["receipt"] and cut["narration_error"]
    assert len(history_after["items"]) == len(history_before["items"]) + 1
    entry = next(row for row in history_after["items"]
                 if row["publication_id"] == accepted["receipt"]["receipt_id"])
    assert entry["receipt"] == accepted["receipt"]
    assert entry["previous"] == history_before["head"]
    assert {field: entry[field] for field in ("label", "source", "reads")} == {
        field: request[field] for field in ("label", "source", "reads")
    }
    assert entry["steps"] == entry["records"]["steps"] == accepted["data"]["steps"]
    assert entry["records"]["undo_of"] == [] and entry["records"]["undo_steps"] is None
    assert entry["records"]["steps"][0]["dependencies"][0] == request["reads"][0]
    assert entry["records"]["steps"][0]["patch"]
    assert [record["command"] for record in entry["records"]["commands"]] == request["commands"]
    assert entry["records"]["commands"][0]["bindings"]
    assert entry["records"]["commands"][0]["writes"]
    assert_equivalent(checkout.bundle, bundle_from_document(after))
    assert len(after["shot_revisions"]) == len(after["internal_timeline_revisions"]) == 3
    assert after["parent_revision"]["payload"]["opaque"] == before["parent_revision"]["payload"]["opaque"]
    assert env.document() == after
    assert env.client.timelines.history(env.project, "t", limit=200).data == history_after
    assert any(row["request"] is None and "publication_id" in row["bundle"]["base_parent"]
               for row in native_inputs)
    assert any(row["request"] == request and row["evaluated_head"] == history_before["head"]
               for row in native_inputs)


def test_fixture_restructure_request_authority_and_same_byte_stale_refuse_without_writes(
    restructure_inverse_runtime, tmp_path
):
    from runtime_protocol.errors import RuntimeErrorBase

    from astrid.sdk.timeline.commands import execute

    env = restructure_inverse_runtime
    history = env.client.timelines.history(env.project, "t", limit=200).data
    head = history["head"]
    prior = next(row for row in history["items"] if row["publication_id"] != head["publication_id"])
    opened = env.client.timelines.open_bundle(env.project, "t", **head)
    assert opened.ok, opened.as_dict()
    command = execute(opened.data["bundle"], [{"schema_version": 1, "op": "timeline.restructure",
        "version": "1", "select": "parent", "args": {}}]).commands[0]
    body = {"api_version": 1, "project_id": env.project, "timeline_id": "t",
        "base_revision_id": head["revision_id"], "base_publication_id": head["publication_id"],
        "commands": [command], "label": "Restructure", "merge": "atomic",
        "reads": [{"kind": "scope", "ref": "timeline", "path": []}],
        "steps": [{"id": "cut", "label": "Restructure", "intent": "Store chapters as shots",
            "tags": ["restructure"], "command_ordinals": [0], "patch": [], "reads": []}],
        "source": {"via": "python"}, "rationale": "Preserve the film", "group_id": "chapter-cut",
        "dry_run": False}
    before = copy.deepcopy(env.document())
    for damage in ("revision", "publication", "scope", "digest", "unknown"):
        request = copy.deepcopy(body)
        if damage == "revision":
            request["base_revision_id"] = prior["revision_id"]
        elif damage == "publication":
            request["base_publication_id"] = prior["publication_id"]
        elif damage == "scope":
            request["timeline_id"] = "wrong-scope"
        elif damage == "digest":
            read = next(read for read in request["commands"][0]["reads"] if read["ref"] == "timeline")
            read["state"]["value"]["content_digest"] = "sha256:" + "0" * 64
        else:
            request["commands"][0]["args"]["source_document"] = before
        stored = tuple(env.service.store.conn.iterdump())
        with pytest.raises(RuntimeErrorBase):
            env.send(env.project, "t", request, idempotency_key="pin-bad-" + damage)
        assert env.document() == before
        assert env.client.timelines.history(env.project, "t", limit=200).data == history
        assert tuple(env.service.store.conn.iterdump()) == stored

    current = timeline.open(env.project, "t", client=env.client, outbox_path=tmp_path / "aba.json")
    current.bundle["parent"]["opaque"]["concurrent"] = True
    current.commit("Intervening content", idempotency_key="pin-aba-b")
    del current.bundle["parent"]["opaque"]["concurrent"]
    restored = current.commit("Restore original bytes", idempotency_key="pin-aba-a")
    assert restored["data"]["canonical_document"] == before
    stale_history = copy.deepcopy(env.client.timelines.history(env.project, "t", limit=200).data)
    assert stale_history["head"]["publication_id"] != head["publication_id"]
    stored = tuple(env.service.store.conn.iterdump())
    with pytest.raises(RuntimeErrorBase):
        env.send(env.project, "t", body, idempotency_key="pin-stale-same-bytes")
    assert env.document() == before
    assert env.client.timelines.history(env.project, "t", limit=200).data == stale_history
    assert tuple(env.service.store.conn.iterdump()) == stored


def test_fixture_restructure_refuses_forged_current_closure_without_writes(
    restructure_inverse_runtime, monkeypatch
):
    env = restructure_inverse_runtime
    head = env.client.timelines.history(env.project, "t", limit=1).data["head"]
    history = copy.deepcopy(env.client.timelines.history(env.project, "t", limit=200).data)
    document = copy.deepcopy(env.document())
    stored = tuple(env.service.store.conn.iterdump())
    calls = copy.deepcopy(env.calls)
    checkout = Checkout.open(env.project, "t", client=env.client)
    read_parent = env.generated.get_project_parent_composition_revision

    def forged_parent(project, ref, revision):
        parent = copy.deepcopy(read_parent(project, ref, revision))
        assert revision == head["revision_id"]
        parent["payload"]["opaque"]["forged"] = True
        return parent

    with monkeypatch.context() as damaged:
        damaged.setattr(env.generated, "get_project_parent_composition_revision", forged_parent)
        refused = env.client.timelines.open_bundle(env.project, "t", **head)
        assert not refused.ok and refused.error.code == "protocol_error"
        with pytest.raises(TimelineEditError, match="parent_revision.content_digest does not match the pinned payload"):
            checkout.publish("Restructure", restructure=True, client=env.client,
                             idempotency_key="pin-forged-current")
    assert env.calls == calls
    assert env.document() == document
    assert env.client.timelines.history(env.project, "t", limit=200).data == history
    assert tuple(env.service.store.conn.iterdump()) == stored


def test_restructure_whole_inverse_redo_and_intervening_overlap_refusal(
    restructure_inverse_runtime, tmp_path, monkeypatch
):  # noqa: F811
    env = restructure_inverse_runtime

    source = timeline.open(env.project, "t", client=env.client, outbox_path=tmp_path / "name.json")
    source.bundle["shots"][source._shot_ids()[0]]["payload"]["name"] = "02 TWO"
    source.commit("Name source chapter", idempotency_key="inverse-name")
    env.calls.clear()
    env.replies.clear()
    before = copy.deepcopy(env.document())
    before_head = env.client.timelines.history(env.project, "t", limit=1).data["head"]

    def no_narration_media(**_kwargs):
        raise TimelineEditError("qualification keeps the restructure as one publication")

    monkeypatch.setattr(env.client.media, "import_bytes", no_narration_media)
    cut = Checkout.open(env.project, "t", client=env.client).publish(
        "Restructure", restructure=True, client=env.client, idempotency_key="inverse-cut"
    )
    after = copy.deepcopy(env.document())
    restructure_reply = env.replies[0]
    assert cut["narration_error"]
    request = env.calls[0][0]
    assert {key: request[key] for key in (
        "api_version", "project_id", "timeline_id", "base_revision_id", "base_publication_id",
        "commands", "label", "steps", "merge", "reads", "dry_run", "source",
    )} == {
        "api_version": 1, "project_id": env.project, "timeline_id": "t",
        "base_revision_id": before_head["revision_id"], "base_publication_id": before_head["publication_id"],
        "commands": request["commands"], "label": "Restructure", "steps": request["steps"],
        "merge": "atomic", "reads": [{"kind": "scope", "ref": "timeline", "path": []},
                                    ], "dry_run": False, "source": {"via": "python"},
    }
    assert request["commands"][0]["op"] == "timeline.restructure"
    assert "rationale" not in request and "group_id" not in request
    assert request["steps"][-1] == {
        "id": request["steps"][-1]["id"], "label": "Restructure", "intent": "Store chapters as shots",
        "tags": ["restructure"], "command_ordinals": [len(request["commands"]) - 1], "patch": [],
    }
    assert len(after["shot_revisions"]) == len(after["internal_timeline_revisions"]) == 3
    assert len(after["parent_revision"]["payload"]["occurrences"]) == 3
    assert before["parent_revision"]["payload"]["opaque"] == {"null": None, "ordered": [None, False, 0]}
    assert after["parent_revision"]["payload"]["opaque"] == before["parent_revision"]["payload"]["opaque"]
    accepted = restructure_reply["data"]
    assert accepted["status"] == "accepted"
    assert accepted["canonical_document"] == after
    assert accepted["accepted_revision_id"] == after["parent_revision"]["revision_id"]
    assert accepted["merge_mode"] == "atomic"
    assert accepted["document_digest"]
    assert restructure_reply["receipt"]["receipt_id"]
    stored = next(item for item in env.client.timelines.history(env.project, "t", limit=200).data["items"]
                  if item["publication_id"] == restructure_reply["receipt"]["receipt_id"])
    assert stored["records"]["undo_of"] == [] and stored["records"]["undo_steps"] is None

    reopened = timeline.open(env.project, "t", client=env.client, outbox_path=tmp_path / "inverse.json")
    try:
        undone = reopened.session.undo(
            [restructure_reply["receipt"]["receipt_id"]], idempotency_key="inverse-undo"
        )
    except WorkspaceClientError as exc:
        import runtime_protocol

        from astrid.sdk.timeline import publish as publish_module

        print(json.dumps({
            "kind": "g5-runtime-inverse-failure",
            "error": {"type": type(exc).__name__, "message": str(exc),
                      "code": getattr(exc, "code", None), "details": getattr(exc, "details", None)},
            "request": env.calls[-1][0],
            "prepared_input_history": env.client.timelines.history(env.project, "t", limit=200).data,
            "canonical_before": before,
            "canonical_after": after,
            "origins": {
                "agent_publish": publish_module.__file__,
                "agent_test": __file__,
                "runtime_protocol": runtime_protocol.__file__,
            },
        }, sort_keys=True))
        raise
    undo_document = undone["data"]["canonical_document"]
    assert undo_document["parent_revision"]["payload"] == before["parent_revision"]["payload"]
    assert [row["payload"] for row in undo_document["shot_revisions"]] == [
        row["payload"] for row in before["shot_revisions"]
    ]
    assert [row["payload"] for row in undo_document["internal_timeline_revisions"]] == [
        row["payload"] for row in before["internal_timeline_revisions"]
    ]
    assert undo_document["dependency_manifest"] == before["dependency_manifest"]

    redone = reopened.session.undo([undone["receipt"]["receipt_id"]], idempotency_key="inverse-redo")
    redo_document = redone["data"]["canonical_document"]
    assert redo_document["parent_revision"]["payload"] == after["parent_revision"]["payload"]
    assert [row["payload"] for row in redo_document["shot_revisions"]] == [
        row["payload"] for row in after["shot_revisions"]
    ]
    assert [row["payload"] for row in redo_document["internal_timeline_revisions"]] == [
        row["payload"] for row in after["internal_timeline_revisions"]
    ]
    assert redo_document["dependency_manifest"] == after["dependency_manifest"]

    later = timeline.open(env.project, "t", client=env.client, outbox_path=tmp_path / "later.json")
    bed = next(clip for clip in later.bundle["parent"]["clips"] if clip["id"] == "bed")
    bed["volume"] = 0.25
    overlap = later.commit("Later authored overlap", merge="fields", reads=[], idempotency_key="inverse-overlap")
    assert overlap["data"]["status"] == "accepted"

    refusing = timeline.open(env.project, "t", client=env.client, outbox_path=tmp_path / "refusing.json")
    head_before_refusal = env.client.timelines.history(env.project, "t", limit=1).data["head"]
    history_before_refusal = copy.deepcopy(env.client.timelines.history(env.project, "t", limit=200).data)
    canonical_before_refusal = copy.deepcopy(env.document())
    try:
        refusing.session.undo([redone["receipt"]["receipt_id"]], idempotency_key="inverse-refuse")
    except CommitConflict as exc:
        assert "intervening" in str(exc)
    else:
        raise AssertionError("the overlapping authored write must refuse the guarded inverse")
    assert env.client.timelines.history(env.project, "t", limit=1).data["head"] == head_before_refusal
    assert env.client.timelines.history(env.project, "t", limit=200).data == history_before_refusal
    assert env.document() == canonical_before_refusal
