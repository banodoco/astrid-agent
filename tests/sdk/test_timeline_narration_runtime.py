"""Opt-in joined C producer / D consumer checks on disposable Runtime stores.

ASTRID_NARRATION_RUNTIME_TESTS names the admitted Runtime's existing offline
fixture directory. No copied producer implementation or live provider is used.
"""
# ruff: noqa: E402
import copy
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

fixture_dir = os.environ.get("ASTRID_NARRATION_RUNTIME_TESTS")
if not fixture_dir:
    pytest.skip("requires an explicitly selected narration Runtime fixture source", allow_module_level=True)
sys.path.append(str(Path(fixture_dir)))

from runtime_protocol import authoring_scene as scene
from runtime_protocol.auth import CredentialStore
from runtime_protocol.errors import ConflictError
from runtime_protocol.narration import narration_projection, validate_artifacts
from test_narration_commit import managed_take
from test_selected_media_commit import snapshot
from test_shot_composition_runtime import _publication, _service
from test_timeline_accept import IDENTITY, SCHEMA, acceptance, wire
from test_timeline_saved_publish import synthetic_scene_answers  # noqa: F401

from astrid.sdk.contracts import DomainResult
from astrid.sdk.timeline import narration
from astrid.sdk.timeline.commit import RuntimeCommitService
from astrid.sdk.timeline_script import read_composition_script

pytestmark = pytest.mark.usefixtures("synthetic_scene_answers")


def valid(value, name):
    import json

    from jsonschema import Draft202012Validator
    from referencing import Registry, Resource
    opcall = json.loads((Path(__file__).parents[2] / "schemas/change-contract/opcall.schema.json").read_text())
    registry = Registry().with_resource("https://astrid.local/schemas/change-contract/opcall.schema.json", Resource.from_contents(opcall))
    Draft202012Validator({"$ref": "#/$defs/" + name, "$defs": SCHEMA["$defs"]}, registry=registry).validate(value)


class PortProxy:
    def __init__(self, port, **overrides):
        self.port = port
        self.__dict__.update(overrides)

    def __getattr__(self, name):
        return getattr(self.port, name)


@pytest.fixture
def runtime(tmp_path, monkeypatch, synthetic_scene_answers):
    service, project = _service(tmp_path)
    service.publish_parent_composition(project, "main", _publication(project), idempotency_key="initial")
    credentials = CredentialStore(tmp_path / "credentials")
    token, _ = credentials.provision("fixture-writer", ["projects:write", "projects:read", "objects:read"])
    identity = dict(actor="fixture-writer", scopes=["projects:write", "objects:read"],
                    _credential_binding=credentials._sha256(token.encode()))
    service._task_credentials = credentials
    calls = []

    class Relay:
        def __init__(self):
            from astrid.core.execution import authoring_scene_protocol as protocol
            self.config = SimpleNamespace(binding={
                key: (1 if key == "owner_generation" else "astrid-v1" if key == "provider_id"
                      else "sha256:" + "a" * 64 if key.endswith("digest") or key == "renderer_closure" else key)
                for key in protocol.CONTEXT_FIELDS
            })
            self.on_reply = lambda: None
            self.closed = False

        def check_live(self):
            if self.closed:
                raise RuntimeError("synthetic narration relay closed")

        def exchange(self, request, *, guard, cancelled):
            from astrid.core.execution import authoring_scene_protocol as protocol
            guard()
            protocol.validate_request(request, self.config.binding)
            calls.append(copy.deepcopy(request))
            self.on_reply()
            return [dict(frame=frame, layers=[], coverage="GAP", content=None, findings=[])
                    for frame in request["frames"]]

        def close(self):
            self.closed = True

    service._authoring_scene_relay = Relay()
    service._timeline_commit_provider = SimpleNamespace(requires_scene_evaluation=True)
    service._narration_test_identity = identity
    from astrid.core.timeline import scene_answers
    synthetic_client = scene_answers._client

    def request_scoped_client():
        return getattr(scene_answers._local, "client", None) or synthetic_client()

    monkeypatch.setattr(scene_answers, "_client", request_scoped_client)
    # Reuse the Runtime's established disposable relay fixture: credentials,
    # request fencing, and transactional acceptance stay real; only renderer
    # binding is the same narrow synthetic seam used by test_authoring_scene.
    monkeypatch.setattr(scene, "check_binding", lambda _service, relay: relay.check_live())
    try:
        yield service, project
    finally:
        service.close()


def identity_for(service):
    return getattr(service, "_narration_test_identity", IDENTITY)


def evaluate_scene(port, candidate):
    from astrid.core.execution import authoring_scene_protocol as protocol
    from test_authoring_scene import projection

    client = port.scene_evaluator(candidate)
    value = projection()
    client.query_scene(revision_digest=protocol.digest(value)[7:], scene=value, frames=[0])
    client.complete()
    client.close()


def script(refs):
    return dict(op="timeline.apply-script", select=None, args=dict(spec=dict(version=1, remove=[], lines=[dict(
        occurrence_id="occurrence-1", clip_id="voice", segment="line-1", text="Hello world!", take=copy.deepcopy(refs))])))


def commit(service, project, body):
    # This exercises the real request-bound port/acceptor. It intentionally does
    # not register a mismatched old generated contract as an installed provider.
    return RuntimeCommitService().commit(body, service._request_commit_port(body, identity_for(service)))


def test_actual_request_candidate_acceptance_projection_reload_and_script_read(runtime):
    s, p = runtime
    refs, manifest, words = managed_take(s, p)
    body = wire(p)
    body["change"]["commands"] = [script(refs)]
    valid(body, "CommitRequest")
    before = snapshot(s)
    result = commit(s, p, body)
    assert result["status"] == "accepted", result
    closure = s._commit_revision_closure(p, "main", result["accepted_revision_id"])
    projection = narration_projection(closure)
    assert result["runtime_receipt"]["narration_state"]["projection"] == projection
    assert projection["lines"][0]["take"] == refs
    assert snapshot(s) != before
    assert s.store.conn.execute("select count(*) from shot_text_bindings").fetchone()[0] == 0
    class Timelines:
        def inspect(self, project, timeline, **options):
            return DomainResult.success(s.inspect_timeline(project, timeline, options))
    class Transport:
        def get_project_timeline_revision(self, project, timeline, revision):
            return s.get_project_timeline_revision(project, timeline, revision)
    read = read_composition_script(Timelines(), Transport(), p, "main")
    assert read.ok, read
    assert read.data["occurrences"][0]["text"] == "Hello world!\n"
    assert read.data["occurrences"][0]["bindings"] == []
    assert read.data["occurrences"][0]["provenance"] == "pinned_internal_timeline"
    assert commit(s, p, body) == result
    valid(result, "CommitResult")


def test_actual_explicit_empty_overrides_stale_binding_and_preserves_exact_old_reads(runtime):
    s, p = runtime
    pin = s.set_project_shot_text_binding(p, dict(shot_id="shot-1", kind="voiceover_script",
        text="Stale legacy narration.", expected_head=0), idempotency_key="legacy-text")["data"]
    legacy = _publication(p, expected_head="parent-1", parent_revision_id="legacy-parent",
                          shot_revision_id="legacy-shot")
    legacy["shot_revisions"][0]["payload"]["text_bindings"] = [pin]
    s.publish_parent_composition(p, "main", legacy, idempotency_key="legacy-publication")

    class Timelines:
        def inspect(self, project, timeline, **options):
            return DomainResult.success(s.inspect_timeline(project, timeline, options))

    object_reads = []

    class Transport:
        def get_project_timeline_revision(self, project, timeline, revision):
            return s.get_project_timeline_revision(project, timeline, revision)

        def get_object(self, object_id):
            object_reads.append(object_id)
            return {"data": s.cas.path_for(object_id.removeprefix("sha256:")).read_bytes()}

    def read(revision):
        result = read_composition_script(Timelines(), Transport(), p, "main", revision_id=revision)
        assert result.ok, result
        return result.data["occurrences"][0]

    legacy_before = read("legacy-parent")
    assert legacy_before["text"] == "Stale legacy narration."
    assert "narration_state" not in s.get_project_timeline_revision(p, "main", "timeline-rev-1")
    object_reads.clear()
    refs, _, _ = managed_take(s, p)
    body = wire(p)
    body.update(base_revision_id="legacy-parent", base_publication_id="legacy-parent")
    body["change"]["commands"] = [script(refs)]
    first = commit(s, p, body)
    assert first["status"] == "accepted", first
    old_before = read(first["accepted_revision_id"])
    assert old_before["text"] == "Hello world!\n"
    delete = wire(p, key="remove-last-narration")
    delete.update(base_revision_id=first["accepted_revision_id"], base_publication_id=first["accepted_publication_id"])
    delete["change"]["commands"] = [dict(op="timeline.apply-script", select=None,
        args=dict(spec=dict(version=1, lines=[], remove=["occurrence:occurrence-1/clip:voice"])))]
    # C's existing offline acceptance fixture retains a stale binding in the
    # immutable candidate. D's ordinary reflow currently clears that binding;
    # the reader must also honor accepted empty state from other producers.
    prepared = acceptance(s, p, request=delete)
    publication = prepared["publication"]
    publication["internal_timeline_revisions"][0]["revision_id"] = "empty-internal"
    shot = publication["shot_revisions"][0]
    shot.update(revision_id="empty-shot", internal_timeline_revision_id="empty-internal")
    shot["payload"]["text_bindings"] = [pin]
    publication["parent_composition"]["occurrences"][0]["shot_revision_id"] = "empty-shot"
    prepared["narration"] = dict(version=1, lines=[])
    prepared["evidence"]["commands"] = copy.deepcopy(delete["change"]["commands"])
    empty_port = s._request_commit_port(delete, identity_for(s))
    evaluate_scene(empty_port, prepared["result"]["canonical_document"])
    empty = empty_port.accept(prepared)
    assert empty["stored_result"]["status"] == "accepted", empty
    inspected = s.inspect_timeline(p, "main", {"revision_id": empty["revision_id"], "detail": True})
    placed = inspected["selected"][0]["occurrence"]
    assert placed["text_bindings"][0]["media_id"] == pin["media_id"]
    exact = s.get_project_timeline_revision(p, "main", placed["internal_timeline_revision_id"])
    assert exact["narration_state"]["projection"] == {"version": 1, "lines": []}
    assert exact["narration_state"]["parent_revision_id"] == empty["revision_id"]
    row = read(empty["revision_id"])
    assert row["status"] == "empty" and row["text"] == "" and row["lines"] == [] and row["bindings"] == []
    assert object_reads == []
    # New accepted state never changes either exact older read.
    assert read(first["accepted_revision_id"]) == old_before
    assert object_reads == []
    assert read("legacy-parent") == legacy_before


@pytest.mark.parametrize("fault", ["parent", "projection", "digest"])
def test_actual_corrupt_origin_receipt_refuses_at_script_reader(runtime, fault):
    from astrid.sdk.workspace_client import WorkspaceClientError

    s, p = runtime
    refs, _, _ = managed_take(s, p)
    body = wire(p)
    body["change"]["commands"] = [script(refs)]
    accepted = commit(s, p, body)
    assert accepted["status"] == "accepted", accepted
    row = s.store.conn.execute("SELECT rowid, result_json FROM command_idempotency "
        "WHERE command_kind='timeline.commit' AND idempotency_key=?", (body["request_id"],)).fetchone()
    stored = json.loads(row["result_json"])
    state = stored["result"]["runtime_receipt"]["narration_state"]
    if fault == "parent":
        state["parent_revision_id"] = "parent-1"
    elif fault == "projection":
        state["projection"] = dict(version=1, lines=[])
        state["digest"] = "sha256:" + narration.digest(state["projection"])
    else:
        state["digest"] = "sha256:" + "0" * 64
    with s.store._transaction():
        s.store.conn.execute("UPDATE command_idempotency SET result_json=? WHERE rowid=?",
                             (json.dumps(stored), row["rowid"]))
    before = snapshot(s)

    class Timelines:
        def inspect(self, project, timeline, **options):
            return DomainResult.success(s.inspect_timeline(project, timeline, options))

    class Transport:
        def get_project_timeline_revision(self, project, timeline, revision):
            try:
                return s.get_project_timeline_revision(project, timeline, revision)
            except ConflictError as exc:
                # The HTTP client translates Runtime's 409 at this boundary.
                raise WorkspaceClientError(409, "conflict", str(exc), {}) from exc

        def get_object(self, *_):
            pytest.fail("corrupt receipt must never read legacy text")

    result = read_composition_script(Timelines(), Transport(), p, "main", revision_id=accepted["accepted_revision_id"])
    assert not result.ok and result.error.code == "conflict", result
    assert snapshot(s) == before


def test_current_c_structural_vectors_are_consumed_without_a_second_tokenizer(runtime):
    s, p = runtime
    refs, manifest, words = managed_take(s, p)
    validate_artifacts(refs, manifest, words)
    narration.validate_take(manifest["text"], s._request_commit_port(
        dict(wire(p), change={"commands": [script(refs)]}), identity_for(s)).narration_take(refs))
    for value in (.1, True, float("nan"), float("inf")):
        bad = copy.deepcopy(words)
        bad["words"][1]["start_s"] = value
        with pytest.raises(Exception):
            validate_artifacts(refs, manifest, bad)


def test_actual_acceptance_dependency_refusal_rolls_back_head_children_and_journal(runtime):
    s, p = runtime
    refs, manifest, words = managed_take(s, p)
    body = wire(p)
    body["change"]["commands"] = [script(refs)]
    port = s._request_commit_port(body, identity_for(s))
    accept = port.accept
    after_external_change = []
    def corrupt_after_preparation(prepared):
        s.store.conn.execute("UPDATE managed_output_lifecycle SET state='reclaimed'")
        after_external_change.append(snapshot(s))
        return accept(prepared)
    port = PortProxy(port, accept=corrupt_after_preparation)
    with pytest.raises(ConflictError):
        RuntimeCommitService().commit(body, port)
    assert snapshot(s) == after_external_change[0]
    assert s.head(p, "main")["revision_id"] == "parent-1"
    assert s.lookup_request(p, "main", body["request_id"]) is None


def test_actual_lost_response_replay_before_any_dependency_consultation(runtime):
    s, p = runtime
    refs, manifest, words = managed_take(s, p)
    body = wire(p)
    body["change"]["commands"] = [script(refs)]
    port = s._request_commit_port(body, identity_for(s))
    accept = port.accept
    def lose(prepared):
        accept(prepared)
        raise TimeoutError("durable acceptance then crash")
    port = PortProxy(port, accept=lose)
    with pytest.raises(TimeoutError):
        RuntimeCommitService().commit(body, port)
    stored = s.lookup_request(p, "main", body["request_id"])["result"]
    port.narration_take = lambda *_: pytest.fail("dependency read before replay")
    assert RuntimeCommitService().commit(body, port) == stored


def test_offline_capability_preparation_reads_actual_managed_bytes_then_commits(runtime, monkeypatch):
    from astrid.sdk.timeline.checkout import Checkout
    s, p = runtime
    refs, manifest, words = managed_take(s, p)
    class Client:
        def get_managed_output(self, association):
            return s.managed_output(association)
        def get_object(self, object_id):
            return {"data": s.cas.path_for(object_id[7:]).read_bytes()}
    invokes = []
    def offline(capability, **kwargs):
        assert capability == "generation.generate_speech"
        assert kwargs["inputs"]["voice"] == "fixture-voice" and kwargs["inputs"]["rate"] == "+0%"
        invokes.append(kwargs)
        return SimpleNamespace(ok=True, outputs={"managed_outputs": [dict(ref, output_port=port) for port, ref in refs.items()]})
    monkeypatch.setattr("astrid.sdk.invocation.invoke", offline)
    body = wire(p)
    base = RuntimeCommitService._load_revision(s._request_commit_port(body, identity_for(s)), body, "parent-1")
    raw = dict(version=1, remove=[], voice="fixture-voice", lines=[dict(occurrence_id="occurrence-1",
        clip_id="voice", segment="line-1", text="Hello world!")])
    command, verified = narration.prepare_script(Checkout(base), raw, client=Client(), preparation_id=body["request_id"])
    assert len(invokes) == 1 and len(verified) == 1
    body["change"]["commands"] = [command]
    assert commit(s, p, body)["status"] == "accepted"


def test_actual_timing_edit_inverse_and_empty_projection_restore_without_generation(runtime, monkeypatch):
    from astrid.sdk.timeline.checkout import Checkout
    from astrid.sdk.timeline.inverse import inverse_command
    s, p = runtime
    refs, manifest, words = managed_take(s, p)
    body = wire(p)
    body["change"]["commands"] = [script(refs)]
    first = commit(s, p, body)
    assert first["status"] == "accepted"
    body = wire(p, key="timing-edit")
    body["base_revision_id"] = first["accepted_revision_id"]
    body["base_publication_id"] = first["accepted_publication_id"]
    base = RuntimeCommitService._load_revision(s._request_commit_port(body, identity_for(s)), body, body["base_revision_id"])
    timing = script(refs)
    timing["args"]["spec"]["lines"][0]["gap_after_s"] = .8
    body["change"]["commands"] = [timing]
    second = commit(s, p, body)
    assert second["status"] == "accepted", second
    inverse = inverse_command(second["change"]["commands"], base, second["canonical_document"],
        second["accepted_publication_id"], consequences=second["change"]["consequences"])
    undo = wire(p, key="undo")
    undo.update(base_revision_id=second["accepted_revision_id"], base_publication_id=second["accepted_publication_id"])
    undo["change"]["commands"] = [inverse]
    monkeypatch.setattr("astrid.sdk.invocation.invoke", lambda *_a, **_k: pytest.fail("undo generated speech"))
    restored = commit(s, p, undo)
    assert restored["status"] == "accepted", restored
    assert narration.line_clips(Checkout(base))["occurrence:occurrence-1/clip:voice"].data == narration.line_clips(
        Checkout(restored["canonical_document"]))["occurrence:occurrence-1/clip:voice"].data
    delete = wire(p, key="remove-all")
    delete.update(base_revision_id=restored["accepted_revision_id"], base_publication_id=restored["accepted_publication_id"])
    delete["change"]["commands"] = [dict(op="timeline.apply-script", select=None,
        args=dict(spec=dict(version=1, lines=[], remove=["occurrence:occurrence-1/clip:voice"])))]
    empty = commit(s, p, delete)
    assert empty["status"] == "accepted", empty
    assert empty["runtime_receipt"]["narration_state"]["projection"] == {"version": 1, "lines": []}


@pytest.mark.parametrize("empty", [False, True])
def test_actual_distinct_child_identity_read_and_later_publication(runtime, empty):
    from test_narration_commit import managed_take, prepared as prepare_acceptance, request_for

    s, p = runtime
    s.create_timeline(p, "inside", idempotency_key="inside")
    pin = s.set_project_shot_text_binding(p, dict(shot_id="shot-1", kind="voiceover_script",
        text="Stale legacy narration.", expected_head=0), idempotency_key="stale-child-text")["data"]
    refs, manifest, words = managed_take(s, p)
    request = request_for(p, refs)
    port = s._request_commit_port(request, identity_for(s))
    port.narration_take(refs)
    prepared = prepare_acceptance(s, p, request, refs, manifest, words)
    publication = prepared["publication"]
    internal = publication["internal_timeline_revisions"][0]
    internal["timeline_id"] = "inside"
    publication["shot_revisions"][0]["payload"]["text_bindings"] = [pin]
    if empty:
        internal["payload"]["clips"] = []
    prepared["narration"] = narration_projection(dict(
        parent_revision=dict(payload=publication["parent_composition"]),
        shot_revisions=publication["shot_revisions"], internal_timeline_revisions=[internal]))
    evaluate_scene(port, prepared["result"]["canonical_document"])
    accepted = port.accept(prepared)
    exact = s.get_project_timeline_revision(p, "inside", internal["revision_id"])
    assert exact["narration_state"] == accepted["receipt"]["narration_state"]
    assert exact["narration_state"]["parent_revision_id"] == accepted["revision_id"]
    assert (exact["narration_state"]["projection"]["lines"] == []) is empty
    reads = []

    class Timelines:
        def inspect(self, project, timeline, **options):
            page = s.inspect_timeline(project, timeline, options)
            assert page["timeline_id"] == "main"
            assert page["selected"][0]["occurrence"]["internal_timeline_id"] == "inside"
            return DomainResult.success(page)

    class Transport:
        def get_project_timeline_revision(self, *args):
            reads.append(args)
            assert args == (p, "inside", internal["revision_id"])
            return s.get_project_timeline_revision(*args)

        def get_object(self, *args):
            pytest.fail("present child narration must never fetch stale legacy text")

    def read():
        result = read_composition_script(Timelines(), Transport(), p, "main", revision_id=accepted["revision_id"])
        assert result.ok, result
        return result.data

    before = read()
    row = before["occurrences"][0]
    assert row["internal_timeline_id"] == "inside"
    assert row["text"] == ("" if empty else "Hello world!\n")
    assert row["status"] == ("empty" if empty else "present")
    assert row["bindings"] == [] and row["provenance"] == "pinned_internal_timeline"
    later = acceptance(s, p, key="later-child-publication")
    later["narration"] = dict(version=1, lines=[])
    s.accept(later, identity=identity_for(s))
    assert read() == before
    assert s.get_project_timeline_revision(p, "inside", internal["revision_id"]) == exact
    assert reads == [(p, "inside", internal["revision_id"])] * 2
