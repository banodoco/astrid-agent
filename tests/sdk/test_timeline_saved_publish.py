"""Disposable producer + real generated encoder + semantic provider fixtures.

The acceptance port is an in-memory boundary double, never installed Runtime,
its database, launcher, catalog, or real timeline data.
"""
from __future__ import annotations

import copy
import json
import multiprocessing
import os
from pathlib import Path
from urllib.parse import unquote

import pytest

from banodoco_workspace_client import WorkspaceClient as GeneratedClient
from banodoco_workspace_client.contract_metadata import PROTOCOL, SCHEMA_DIGEST
from banodoco_workspace_client.generated import ApiError
from astrid.core.timeline.authoring_bundle import compile_authoring_candidate
from astrid.sdk.timeline import commands, journal, saved_publish, store
from astrid.sdk.timeline.checkout import Checkout
from astrid.sdk.timeline.commit import RuntimeCommitService, _digest
from astrid.sdk.timeline.saved_intent import SavedIntentError, read_saved_calls
from astrid.sdk.workspace_client import WorkspaceClient, WorkspaceClientError

FIXTURE = Path(__file__).parents[1] / "fixtures/timeline_editing/tiny_publishable.json"


@pytest.fixture
def synthetic_scene_answers(monkeypatch):
    """Known answers for command/publication proofs, never native renderer proof."""
    from astrid.core.timeline import scene_answers

    class Client:
        def query_scene(self, *, revision_digest, scene, frames):
            return [dict(frame=frame, coverage="COVERED", content="CONTENT",
                         findings=[], layers=[]) for frame in frames]

    # Publication race tests also use threads, so inject at this boundary rather
    # than installing the invocation-scoped client only in the parent thread.
    monkeypatch.setattr(scene_answers, "_client", lambda: Client())
    monkeypatch.setattr(Checkout, "_native_outline_integrity", lambda *a: None)


pytestmark = pytest.mark.usefixtures("synthetic_scene_answers")


class RuntimeFixture:
    def __init__(self, bundle=None):
        published = compile_authoring_candidate(Checkout(
            json.loads(FIXTURE.read_text()) if bundle is None else copy.deepcopy(bundle)).document()).publication
        self.parents = {"rev-0": dict(revision_id="rev-0", project_id="p", timeline_id="t",
                                     payload=published["parent_composition"],
                                     content_digest=_digest(published["parent_composition"]))}
        self.shots = {r["revision_id"]: dict(r, project_id="p") for r in published["shot_revisions"]}
        self.internals = {r["revision_id"]: dict(r, project_id="p") for r in published["internal_timeline_revisions"]}
        self.current = "rev-0"
        self.publications = []
        self.saved = {}
        self.calls = []
        self.reads = []
        self.sessions = 0
        self.principal, self.realm = "publisher", "realm"
        self.revoked = self.lose = self.unavailable = False
        self.history = [[dict(revision_id="rev-0", publication_id="pub-0", project_id="p", timeline_id="t")], None]

    def client(self):
        workspace = WorkspaceClient("http://127.0.0.1:1", "test-current-credential")
        workspace._generated = GeneratedClient(workspace.endpoint, workspace.token, transport=self.transport)
        return workspace

    def closure(self, rid):
        parent = self.parents[rid]
        shots = [self.shots[r] for r in {o["shot_revision_id"] for o in parent["payload"]["occurrences"]}]
        internals = [self.internals[r] for r in {s["internal_timeline_revision_id"] for s in shots}]
        return copy.deepcopy(dict(parent_revision=parent, shot_revisions=shots, internal_timeline_revisions=internals))

    def transport(self, method, path, headers, payload):
        if path == "/v1/health":
            response = dict(status="ok", protocol=PROTOCOL, schema_digest=SCHEMA_DIGEST,
                            runtime_epoch=1, runtime_instance_id="fixture", runtime_session_id="health-session")
        elif path == "/v1/handshake":
            if self.revoked:
                raise ApiError(403, "forbidden", "credential revoked")
            self.sessions += 1
            response = dict(protocol=PROTOCOL, schema_digest=SCHEMA_DIGEST, session_id=f"handshake-{os.getpid()}-{self.sessions}",
                            actor_id=self.principal, realm_id=self.realm,
                            scopes=sorted(__import__('astrid.sdk.client', fromlist=['_REQUIRED_SCOPES'])._REQUIRED_SCOPES),
                            capabilities=["execution_binding.targeted.v1"])
        elif method == "GET":
            self.reads.append(path)
            parts = [unquote(p) for p in path.split("?")[0].split("/")]
            if "composition-revisions" in parts:
                if "?" in path:
                    response = dict(items=self.history[0], next_cursor=self.history[1])
                else:
                    response = self.parents[parts[-1]]
            elif "shots" in parts:
                response = self.shots[parts[-1]]
            else:
                response = self.internals[parts[-1]]
        elif path.endswith("/commit"):
            if self.revoked:
                raise ApiError(403, "forbidden", "current commit authorization revoked")
            assert headers["Authorization"] == "Bearer test-current-credential"
            self.calls.append((bytes(payload), copy.deepcopy(headers)))
            body = json.loads(payload)
            assert headers["X-Astrid-Session"] == body["session_id"]
            assert headers["Idempotency-Key"] == body["request_id"]
            if self.unavailable:
                from astrid.sdk.timeline.commit import _result
                response = _result(body, _digest(body))
                response["error"] = dict(code="runtime_unavailable", message="fixture", hint="retry", next_action="retry")
            else:
                response = RuntimeCommitService().commit(body, self.port(body))
            if self.lose:
                self.lose = False
                raise TimeoutError("accepted fixture response deliberately lost")
        else:
            raise AssertionError((method, path))
        return 200, {}, json.dumps(response).encode()

    def port(self, body):
        runtime = self

        class Port:
            def lookup_request(self):
                return copy.deepcopy(runtime.saved.get(body["request_id"]))

            def head(self):
                return dict(revision_id=runtime.current, publication_id="current-fixture", updated_at="fixture")

            def revision_closure(self, rid):
                return runtime.closure(rid)

            def accept(self, prepared):
                result = copy.deepcopy(prepared["result"])
                previous = runtime.current
                publication = prepared["publication"]
                if publication is not None:
                    rid = f"accepted-{len(runtime.publications) + 1}"
                    for row in publication["shot_revisions"]:
                        runtime.shots[row["revision_id"]] = dict(row, project_id="p")
                    for row in publication["internal_timeline_revisions"]:
                        runtime.internals[row["revision_id"]] = dict(row, project_id="p")
                    parent = publication["parent_composition"]
                    runtime.parents[rid] = dict(revision_id=rid, project_id="p", timeline_id="t",
                                                payload=parent, content_digest=_digest(parent))
                    runtime.current = rid
                    runtime.publications.append(rid)
                    result.update(accepted_revision_id=rid, accepted_publication_id="pub-" + rid)
                result.update(previous_revision_id=previous,
                              runtime_receipt=dict(publisher=runtime.principal, accepted_at="fixture",
                                                   evidence_digest=_digest(prepared["evidence"]), narration_state=None),
                              provenance=dict(publisher=runtime.principal, source=body["source"]),
                              evidence=dict(digest=_digest(prepared["evidence"])))
                runtime.saved[body["request_id"]] = dict(request_hash=_digest(body), result=copy.deepcopy(result))
                return dict(stored_result=result)
        return Port()


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("BANODOCO_LOCAL_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setattr(Checkout, "SAY_SAVED", False)
    runtime = RuntimeFixture()
    client = runtime.client()
    bundle = saved_publish._exact(client, dict(project_id="p", timeline_id="t", base_revision_id="rev-0"), "rev-0")
    monkeypatch.setattr(store, "resolve_ids", lambda *a, **kw: ("p", "t", "rev-0"))
    monkeypatch.setattr(store, "fetch_bundle", lambda *a, **kw: copy.deepcopy(bundle))
    draft = Checkout.draft("p", "t")
    return draft, runtime


def edit(draft, **args):
    return commands.run(draft, dict(op="clip.set", select="c1.rocket", args=args))


def metadata():
    return [e for row in journal.read(journal.journal_path("p", "t")) for e in row.evidence if isinstance(e, dict)]


def test_saved_two_operations_generated_route_and_exact_receipt_finalize(setup):
    draft, runtime = setup
    edit(draft, x=90)
    edit(draft, y=180)
    calls = read_saved_calls(Checkout.load(draft.path))
    result = Checkout.load(draft.path).publish("two edits", client=runtime.client())
    body = json.loads(runtime.calls[0][0])
    assert body["change"]["commands"] == list(calls)
    assert body["session_sequence"] == 1 and body["session_id"].startswith("handshake-")
    assert result["status"] == "accepted" and result["local_state"] == "finalized"
    assert Checkout.load(draft.path).base_revision == result["accepted_revision_id"] == "accepted-1"
    assert read_saved_calls(Checkout.load(draft.path)) == ()
    assert journal.verify(journal.journal_path("p", "t")) == []
    assert body["base_publication_id"] == "pub-0"
    assert "asserted_actor" not in body


def test_lost_acceptance_fresh_handshake_head_advance_retries_identical_bytes(setup):
    draft, runtime = setup
    edit(draft, x=90)
    runtime.lose = True
    with pytest.raises(WorkspaceClientError, match="timed out"):
        draft.publish("first", client=runtime.client())
    assert runtime.publications == ["accepted-1"]
    runtime.current = "advanced-after-acceptance"  # replay must not read this nonexistent head
    result = Checkout.load(draft.path).publish("retry label ignored", client=runtime.client())
    first, second = runtime.calls
    assert first[0] == second[0]
    for key in ("Idempotency-Key", "X-Astrid-Session"):
        assert first[1][key] == second[1][key]
    assert runtime.sessions == 2 and runtime.publications == ["accepted-1"]
    assert result["accepted_revision_id"] == Checkout.load(draft.path).base_revision == "accepted-1"


def test_lost_result_retry_in_a_fresh_process(setup):
    draft, runtime = setup
    edit(draft, x=90)
    runtime.lose = True
    with pytest.raises(WorkspaceClientError):
        draft.publish(client=runtime.client())
    runtime.current = "head-moved"
    context = multiprocessing.get_context("fork")
    queue = context.Queue()

    def retry():
        result = Checkout.load(draft.path).publish(client=runtime.client())
        queue.put((result, runtime.calls[-1], runtime.sessions, runtime.publications))

    process = context.Process(target=retry)
    process.start()
    result, retried, sessions, publications = queue.get(timeout=20)
    process.join(20)
    assert process.exitcode == 0
    assert result["accepted_revision_id"] == "accepted-1" and publications == ["accepted-1"]
    assert sessions == 2 and runtime.calls[0][0] == retried[0]
    assert runtime.calls[0][1]["X-Astrid-Session"] == retried[1]["X-Astrid-Session"]


@pytest.mark.parametrize("boundary", ["file", "directory"])
def test_durability_failure_never_sends_and_can_be_recovered(setup, monkeypatch, boundary):
    draft, runtime = setup
    edit(draft, x=90)
    original = journal.os.fsync
    def fail(fd):
        import stat
        directory = stat.S_ISDIR(os.fstat(fd).st_mode)
        if directory == (boundary == "directory"):
            raise OSError("injected fsync")
        original(fd)
    monkeypatch.setattr(journal.os, "fsync", fail)
    with pytest.raises(OSError, match="fsync"):
        draft.publish(client=runtime.client())
    assert runtime.calls == []
    monkeypatch.setattr(journal.os, "fsync", original)
    result = Checkout.load(draft.path).publish(client=runtime.client())
    assert result["status"] == "accepted" and len(runtime.publications) == 1


def test_two_concurrent_processes_choose_one_envelope_and_session(setup):
    draft, _runtime = setup
    edit(draft, x=90)
    context = multiprocessing.get_context("fork")
    barrier, queue = context.Barrier(2), context.Queue()

    def worker():
        runtime = RuntimeFixture()
        runtime.unavailable = True
        client = runtime.client()
        original = client.list_project_parent_composition_revisions
        def history(*args, **kw):
            result = original(*args, **kw)
            barrier.wait(timeout=20)  # both have prepared before either reserves
            return result
        client.list_project_parent_composition_revisions = history
        result = Checkout.load(draft.path).publish(client=client)
        queue.put((result, runtime.calls[0]))

    processes = [context.Process(target=worker) for _ in range(2)]
    for process in processes:
        process.start()
    results = [queue.get(timeout=25) for _ in processes]
    for process in processes:
        process.join(10)
        assert process.exitcode == 0
    assert results[0][1][0] == results[1][1][0]
    assert results[0][1][1]["X-Astrid-Session"] == results[1][1][1]["X-Astrid-Session"]
    assert len([e for e in metadata() if e["kind"] == "publish-intent-v1"]) == 1


@pytest.mark.parametrize("identity", ["realm", "principal", "revoked"])
def test_changed_or_revoked_identity_refuses_before_commit(setup, identity):
    draft, runtime = setup
    edit(draft, x=90)
    runtime.unavailable = True
    draft.publish(client=runtime.client())
    setattr(runtime, identity, True if identity == "revoked" else "changed")
    with pytest.raises((WorkspaceClientError, saved_publish.TimelineEditError)):
        Checkout.load(draft.path).publish(client=runtime.client())
    assert len(runtime.calls) == 1


@pytest.mark.parametrize("history,expected", [
    ([[dict(revision_id="rev-0", publication_id="exact-old")], None], "exact-old"),
    ([[dict(revision_id="different", publication_id="latest")], None], None),
    ([[dict(revision_id="rev-0", publication_id="a"), dict(revision_id="rev-0", publication_id="b")], None], None),
    ([[dict(revision_id="rev-0", publication_id="maybe")], "more"], None),
])
def test_only_unique_complete_exact_base_mapping_is_frozen(setup, history, expected):
    draft, runtime = setup
    edit(draft, x=90)
    runtime.unavailable, runtime.history = True, history
    draft.publish(client=runtime.client())
    assert json.loads(runtime.calls[0][0])["base_publication_id"] == expected
    runtime.history = [[dict(revision_id="rev-0", publication_id="discovered-later")], None]
    draft.publish(client=runtime.client())
    assert runtime.calls[0][0] == runtime.calls[1][0]


def test_explicit_key_and_pending_suffix_are_preserved_until_resolution(setup):
    draft, runtime = setup
    edit(draft, x=90)
    runtime.unavailable = True
    draft.publish(idempotency_key="Caller-Key:123", client=runtime.client())
    first = runtime.calls[0][0]
    edit(draft, y=180)
    suffix = Path(draft.path).read_bytes()
    runtime.unavailable = False
    result = Checkout.load(draft.path).publish(client=runtime.client())
    assert runtime.calls[-1][0] == first
    assert json.loads(first)["request_id"] == "Caller-Key:123"
    assert result["local_state"] == saved_publish._RETAINED
    assert Path(draft.path).read_bytes() == suffix
    before = len(runtime.calls)
    assert Checkout.load(draft.path).publish(client=runtime.client())["local_state"] == saved_publish._RETAINED
    assert len(runtime.calls) == before


def test_new_base_content_key_after_resolution_and_explicit_conflict(setup):
    draft, runtime = setup
    edit(draft, x=90)
    first = draft.publish(idempotency_key="same-key", client=runtime.client())
    edit(draft, y=180)
    conflict = draft.publish(idempotency_key="same-key", client=runtime.client())
    assert conflict["error"]["code"] == "request_id_conflict"
    assert "RequestIdConflict" in conflict["error"]["message"]
    assert len(runtime.publications) == 1
    result = draft.publish(client=runtime.client())
    assert result["status"] == "accepted" and len(runtime.publications) == 2
    assert json.loads(runtime.calls[-1][0])["request_id"].startswith("edit-v1-")
    assert first["accepted_revision_id"] == "accepted-1"


@pytest.mark.parametrize("boundary", ["result", "checkpoint", "replace"])
def test_crash_after_durable_acceptance_recovers_without_another_commit(setup, monkeypatch, boundary):
    draft, runtime = setup
    edit(draft, x=90)
    append, replace = saved_publish._append, saved_publish._replace
    def crash_append(*args, **kw):
        result = append(*args, **kw)
        kinds = [e["kind"] for e in args[2]]
        if ("publish-result-v1" if boundary == "result" else "publish-checkpoint-v1") in kinds:
            raise OSError("injected crash")
        return result
    def crash_replace(*args):
        replace(*args)
        raise OSError("injected crash")
    if boundary == "replace":
        monkeypatch.setattr(saved_publish, "_replace", crash_replace)
    else:
        monkeypatch.setattr(saved_publish, "_append", crash_append)
    with pytest.raises(OSError, match="crash"):
        draft.publish(client=runtime.client())
    assert len(runtime.calls) == 1
    monkeypatch.setattr(saved_publish, "_append", append)
    monkeypatch.setattr(saved_publish, "_replace", replace)
    result = Checkout.load(draft.path).publish(client=runtime.client())
    assert result["local_state"] == "finalized" and len(runtime.calls) == 1
    assert Checkout.load(draft.path).base_revision == "accepted-1"
    assert read_saved_calls(Checkout.load(draft.path)) == ()


def test_dry_run_reserves_no_intent_and_no_publication(setup):
    draft, runtime = setup
    edit(draft, x=90)
    log = journal.journal_path("p", "t")
    before = log.read_bytes(), Path(draft.path).read_bytes()
    result = draft.publish(dry_run=True, client=runtime.client())
    assert result["status"] == "preview" and not runtime.saved and not runtime.publications
    assert (log.read_bytes(), Path(draft.path).read_bytes()) == before


def test_default_key_exact_algorithm_and_no_process_labels(setup):
    draft, runtime = setup
    edit(draft, x=90)
    digest = journal.digest(Path(draft.path).read_bytes().decode())
    runtime.unavailable = True
    draft.publish("message", client=runtime.client())
    body = json.loads(runtime.calls[0][0])
    raw = dict(realm_id="realm", project_id="p", timeline_id="t", base_revision_id="rev-0",
               scope=body["scope"], saved_draft_digest=digest)
    import hashlib
    expected = "edit-v1-" + hashlib.sha256(json.dumps(raw, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                                                      allow_nan=False).encode()).hexdigest()
    assert body["request_id"] == expected
    assert saved_publish.request_id(dict(realm_id="realm"), dict(body, session_id="different", label="different"), digest) == expected


def test_no_change_is_durable_and_recovers_exact_base(setup):
    draft, runtime = setup
    result = draft.publish(client=runtime.client())
    assert result["status"] == "no_change" and not runtime.publications
    assert result["local_state"] == "finalized" and draft.base_revision == "rev-0"
    again = Checkout.load(draft.path).publish(client=runtime.client())
    assert again == result and len(runtime.calls) == 1


@pytest.mark.parametrize("damage", ["hash", "publisher", "revision", "document"])
def test_unverified_acceptance_remains_pending(setup, monkeypatch, damage):
    draft, runtime = setup
    edit(draft, x=90)
    client = runtime.client()
    submit = client.commit_timeline
    def corrupt(body):
        result = submit(body)
        if damage == "hash":
            result["request_hash"] = "sha256:wrong"
        elif damage == "publisher":
            result["runtime_receipt"]["publisher"] = "other"
        elif damage == "revision":
            result["accepted_revision_id"] = "rev-0"
        else:
            result["canonical_document"]["parent"]["config"]["forged"] = True
        return result
    monkeypatch.setattr(client, "commit_timeline", corrupt)
    with pytest.raises(saved_publish.TimelineEditError):
        draft.publish(client=client)
    assert not any(e["kind"] == "publish-result-v1" for e in metadata())
    assert draft.publish(client=runtime.client())["status"] == "accepted"
    assert len(runtime.publications) == 1


def test_journal_truncation_is_rejected_without_send(setup):
    draft, runtime = setup
    edit(draft, x=90)
    path = journal.journal_path("p", "t")
    path.write_bytes(path.read_bytes().rstrip(b"\n"))
    with pytest.raises(SavedIntentError):
        draft.publish(client=runtime.client())
    assert not runtime.calls


def test_sheet_apply_file_and_duplicate_audits_preserve_order(setup, tmp_path, monkeypatch):
    from astrid.packs.timeline import cli
    from astrid.sdk.timeline_sheet import render_sheet
    draft, runtime = setup
    edit(draft, x=90)
    edit(draft, y=180)
    original = read_saved_calls(Checkout.load(draft.path))
    sheet = render_sheet(draft, detail=True)
    assert "x=90" in sheet
    sheet_file = tmp_path / "edits.sheet"
    sheet_file.write_text(sheet.replace("x=90", "x=95"))
    monkeypatch.setattr(cli, "_edit_checkout", lambda parsed: draft)
    parser = cli.build_parser(runtime.client())
    parsed = parser.parse_args(["apply", "t", str(sheet_file), "--project", "p"])
    assert parsed.handler(parsed) == 0
    log = journal.journal_path("p", "t")
    last = [r for r in journal.read(log) if r.kind == "save"][-1]
    assert last.calls
    # An exact duplicate audit has its own global journal identity.
    journal.append(log, project="p", timeline="t", draft=Path(draft.path).stem,
                   kind="edit", outcome="changed", label="audit", before=last.before,
                   after=last.after, calls=last.calls)
    other = Checkout.draft("p", "t", "other")
    edit(other, y=500)
    expected = original + tuple(last.calls)
    assert read_saved_calls(Checkout.load(draft.path)) == expected
    result = Checkout.load(draft.path).publish(client=runtime.client())
    assert result["status"] == "accepted"
    assert json.loads(runtime.calls[0][0])["change"]["commands"] == list(expected)


def test_default_key_changes_for_new_saved_base_content_after_resolution(setup):
    draft, runtime = setup
    edit(draft, x=90)
    draft.publish(client=runtime.client())
    first = json.loads(runtime.calls[0][0])
    edit(draft, y=180)
    draft.publish(client=runtime.client())
    second = json.loads(runtime.calls[1][0])
    assert first["request_id"] != second["request_id"]
    assert second["base_revision_id"] == "accepted-1"
    assert second["session_id"] != first["session_id"] and second["session_sequence"] == 1
    assert second["base_publication_id"] == "pub-accepted-1"
    assert len(runtime.publications) == 2


def test_saved_result_preserves_unsaved_handle_edits(setup):
    draft, runtime = setup
    edit(draft, x=90)
    runtime.lose = True
    with pytest.raises(WorkspaceClientError):
        draft.publish(client=runtime.client())
    draft.bundle["parent"]["config"]["unsaved"] = "keep"
    disk = Path(draft.path).read_bytes()
    result = draft.publish(client=runtime.client())
    assert result["local_state"] == saved_publish._RETAINED
    assert draft.bundle["parent"]["config"]["unsaved"] == "keep"
    assert Path(draft.path).read_bytes() == disk


def test_cli_publish_lost_response_default_key_and_dry_run(setup, monkeypatch, capsys):
    from astrid.packs.timeline import cli
    draft, runtime = setup
    edit(draft, x=90)
    monkeypatch.setattr("astrid.sdk.timeline_checkout.find_draft", lambda *a, **kw: draft.path)
    runtime.lose = True
    parser = cli.build_parser(runtime.client())
    args = ["publish", "t", "--project", "p", "-m", "edit", "--json"]
    parsed = parser.parse_args(args)
    # Direct handler, never the launcher.
    assert parsed.handler(parsed) != 0
    capsys.readouterr()
    runtime.current = "moved-head"
    parsed = cli.build_parser(runtime.client()).parse_args(args)
    assert parsed.handler(parsed) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] and payload["data"]["accepted_revision_id"] == "accepted-1"
    assert payload["idempotency_key"] == json.loads(runtime.calls[0][0])["request_id"]
    assert runtime.calls[0][0] == runtime.calls[1][0]
    # Preview can use a newer head only when it is an exact available revision.
    runtime.current = "accepted-1"
    edit(Checkout.load(draft.path), y=180)
    log = journal.journal_path("p", "t")
    before = log.read_bytes()
    parsed = cli.build_parser(runtime.client()).parse_args(args + ["--dry-run"])
    assert parsed.handler(parsed) == 0
    assert json.loads(capsys.readouterr().out)["data"]["status"] == "preview"
    assert log.read_bytes() == before and len(runtime.publications) == 1


def test_metadata_fits_legacy_journal_schema(setup):
    from jsonschema import Draft202012Validator
    from referencing import Registry, Resource
    from referencing.jsonschema import DRAFT202012
    draft, runtime = setup
    edit(draft, x=90)
    draft.publish(client=runtime.client())
    root = Path(__file__).parents[2] / "schemas/change-contract"
    schemas = [json.loads(p.read_text()) for p in root.glob("*.schema.json")]
    registry = Registry().with_resources((s["$id"], Resource.from_contents(s, default_specification=DRAFT202012))
                                         for s in schemas)
    validator = Draft202012Validator(json.loads((root / "journal-record.schema.json").read_text()), registry=registry)
    for row in journal.read(journal.journal_path("p", "t")):
        validator.validate(row.to_json())


def test_current_auth_owns_wire_identity_and_no_credentials_are_journaled(setup, monkeypatch):
    draft, runtime = setup
    edit(draft, x=90)
    monkeypatch.setenv("ASTRID_ACTOR", "agent:invented-publisher")
    monkeypatch.setenv("ASTRID_SESSION", "invented-session")
    draft.publish(client=runtime.client())
    body = json.loads(runtime.calls[0][0])
    assert "asserted_actor" not in body and body["session_id"] != "invented-session"
    records = [r for r in journal.read(journal.journal_path("p", "t")) if r.kind == "publish"]
    assert all(r.actor == dict(kind="unknown", id="unknown", session=None) for r in records)
    serialized = json.dumps([r.to_json() for r in records])
    assert "test-current-credential" not in serialized and "invented-publisher" not in serialized


def test_no_draft_lock_is_held_across_network(setup, monkeypatch):
    draft, runtime = setup
    edit(draft, x=90)
    import fcntl
    transport = runtime.transport
    def checked(method, path, headers, payload):
        lock = Path(draft.path).with_name("." + Path(draft.path).name + ".lock")
        with lock.open("a+") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return transport(method, path, headers, payload)
    client = runtime.client()
    client._generated = GeneratedClient(client.endpoint, client.token, transport=checked)
    assert draft.publish(client=client)["status"] == "accepted"


def test_concurrent_accepting_publishers_get_one_runtime_result(setup):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    draft, runtime = setup
    edit(draft, x=90)
    barrier = Barrier(2)
    def worker():
        client = runtime.client()
        original = client.list_project_parent_composition_revisions
        def history(*args, **kw):
            page = original(*args, **kw)
            barrier.wait(timeout=10)
            return page
        client.list_project_parent_composition_revisions = history
        return Checkout.load(draft.path).publish(client=client)
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: worker(), range(2)))
    assert results[0] == results[1] and results[0]["status"] == "accepted"
    assert len(runtime.publications) == 1 and len(runtime.calls) == 2
    assert runtime.calls[0][0] == runtime.calls[1][0]
    assert len([e for e in metadata() if e["kind"] == "publish-intent-v1"]) == 1
    assert len([e for e in metadata() if e["kind"] == "publish-result-v1"]) == 1


def test_pending_finalization_preserves_manual_helper_or_whitespace_edit(setup):
    draft, runtime = setup
    edit(draft, x=90)
    runtime.lose = True
    with pytest.raises(WorkspaceClientError):
        draft.publish(client=runtime.client())
    path = Path(draft.path)
    path.write_bytes(path.read_bytes() + b"\n")
    changed = path.read_bytes()
    # Use the original handle: it has no unsaved in-memory changes, but the
    # pending digest intentionally does not encode whitespace/derived helpers.
    result = draft.publish(client=runtime.client())
    assert result["local_state"] == saved_publish._RETAINED and path.read_bytes() == changed


def test_interleaved_other_draft_append_during_finalization_keeps_global_sequence(setup, monkeypatch):
    draft, runtime = setup
    edit(draft, x=90)
    other = Checkout.draft("p", "t", "other")
    finalize = saved_publish._finalize
    def interleave(*args):
        edit(other, y=321)
        return finalize(*args)
    monkeypatch.setattr(saved_publish, "_finalize", interleave)
    assert draft.publish(client=runtime.client())["status"] == "accepted"
    assert read_saved_calls(Checkout.load(draft.path)) == ()
    assert len(read_saved_calls(Checkout.load(other.path))) == 1


def test_exact_closure_mismatch_refuses_without_envelope_or_file_change(setup):
    draft, runtime = setup
    edit(draft, x=90)
    parent = runtime.parents["rev-0"]
    parent["payload"]["config"]["unexpected"] = True
    parent["content_digest"] = _digest(parent["payload"])
    before = Path(draft.path).read_bytes()
    with pytest.raises(SavedIntentError, match="exact revision"):
        draft.publish(client=runtime.client())
    assert not runtime.calls and Path(draft.path).read_bytes() == before
    assert not any(e["kind"] == "publish-intent-v1" for e in metadata())


def test_pending_request_is_not_replaced_by_an_explicit_new_key(setup):
    draft, runtime = setup
    edit(draft, x=90)
    runtime.unavailable = True
    draft.publish(client=runtime.client())
    edit(draft, y=180)
    with pytest.raises(saved_publish.TimelineEditError, match="original request ID"):
        draft.publish(idempotency_key="replacement", client=runtime.client())
    assert len(runtime.calls) == 1


def test_invalid_local_publish_refuses_before_any_launcher(setup, monkeypatch):
    from astrid.sdk import AstridClient
    draft, _runtime = setup
    monkeypatch.setattr(AstridClient, "open_from_launcher", lambda **kw: pytest.fail("must not launch"))
    with pytest.raises(SavedIntentError, match="canonical draft path"):
        Checkout(copy.deepcopy(draft.bundle)).publish()
    with pytest.raises(saved_publish.TimelineEditError, match="expectations"):
        draft.publish(force=True)
    with pytest.raises(saved_publish.TimelineEditError, match="HTTP header"):
        draft.publish(idempotency_key="bad\nheader")


def test_sdk_transport_composition_and_default_client_boundary(setup, monkeypatch):
    from contextlib import contextmanager
    from astrid.sdk import AstridClient
    from astrid.sdk.remote import RemoteAstridClient
    draft, runtime = setup
    edit(draft, x=90)
    wrapper = AstridClient(remote=RemoteAstridClient(runtime.client()))
    opened = []
    @contextmanager
    def fake_launcher(**options):
        opened.append(options)
        yield wrapper
    monkeypatch.setattr(AstridClient, "open_from_launcher", fake_launcher)
    assert draft.publish()["status"] == "accepted"
    assert opened == [dict(start_pack_host=False)]
    assert len(runtime.calls) == 1 and runtime.sessions == 1


def test_first_journal_file_is_synced_before_its_directory(tmp_path, monkeypatch):
    import stat
    observed = []
    original = journal.os.fsync
    def sync(fd):
        observed.append("directory" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file")
        original(fd)
    monkeypatch.setattr(journal.os, "fsync", sync)
    path = tmp_path / "new-directory" / "journal.jsonl"
    journal.append(path, project="p", timeline="t", draft="main")
    assert observed == ["file", "directory"]
    assert journal.verify(path) == []


def test_suffix_saved_after_interrupted_checkpoint_keeps_acknowledged_result(setup, monkeypatch):
    draft, runtime = setup
    edit(draft, x=90)
    replace = saved_publish._replace
    def interrupt(*args):
        replace(*args)
        raise OSError("crash before checkpoint")
    monkeypatch.setattr(saved_publish, "_replace", interrupt)
    with pytest.raises(OSError, match="checkpoint"):
        draft.publish(client=runtime.client())
    monkeypatch.setattr(saved_publish, "_replace", replace)
    newer = Checkout.load(draft.path)
    edit(newer, y=180)
    suffix = Path(draft.path).read_bytes()
    result = newer.publish(client=runtime.client())
    assert result["local_state"] == saved_publish._RETAINED
    assert result["accepted_revision_id"] == "accepted-1"
    assert len(runtime.calls) == 1 and Path(draft.path).read_bytes() == suffix


def test_saved_session_does_not_bypass_current_commit_authorization(setup, monkeypatch):
    draft, runtime = setup
    edit(draft, x=90)
    client = runtime.client()
    submit = client.commit_timeline
    def revoked_at_submit(body):
        runtime.revoked = True
        return submit(body)
    monkeypatch.setattr(client, "commit_timeline", revoked_at_submit)
    with pytest.raises(WorkspaceClientError, match="authorization revoked"):
        draft.publish(client=client)
    assert not runtime.publications and not runtime.calls
    pending = next(e for e in metadata() if e["kind"] == "publish-intent-v1")
    runtime.revoked = False
    assert draft.publish(client=runtime.client())["status"] == "accepted"
    assert json.loads(runtime.calls[0][0]) == pending["body"]


def test_conclusive_conflict_keeps_saved_draft_and_original_commands(setup):
    draft, runtime = setup
    edit(draft, x=90)
    calls = read_saved_calls(Checkout.load(draft.path))
    before = Path(draft.path).read_bytes()
    competing = Checkout.draft("p", "t", "competing")
    edit(competing, x=500)
    competing.publish(client=runtime.client())
    result = draft.publish(client=runtime.client())
    assert result["status"] == "conflict" and result["runtime_receipt"] is None
    assert Path(draft.path).read_bytes() == before
    assert read_saved_calls(Checkout.load(draft.path)) == calls
    assert len(runtime.publications) == 1
    records = [r for r in journal.read(journal.journal_path("p", "t")) if r.draft == Path(draft.path).stem]
    assert records[-1].outcome == "refused"
    assert records[-1].evidence[0]["result"] == result


def test_collision_preservation_uses_saved_canonical_commit(tmp_path, monkeypatch):
    """Opaque preservation follows the saved producer and actual semantic port."""
    from test_timeline_data_preservation import collision_bundle, assert_opaque

    monkeypatch.setenv("BANODOCO_LOCAL_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setattr(Checkout, "_native_outline_integrity", lambda *a: None)
    expected = collision_bundle()
    runtime = RuntimeFixture(expected)
    client = runtime.client()
    bundle = saved_publish._exact(client, dict(project_id="p", timeline_id="t"), "rev-0")
    monkeypatch.setattr(store, "resolve_ids", lambda *a, **kw: ("p", "t", "rev-0"))
    monkeypatch.setattr(store, "fetch_bundle", lambda *a, **kw: copy.deepcopy(bundle))
    draft = Checkout.draft("p", "t")
    assert_opaque(draft.document(), expected)
    # The public command writer saves and journals this exact canonical draft.
    edit(draft, x=90)
    assert read_saved_calls(Checkout.load(draft.path))
    result = Checkout.load(draft.path).publish("opaque preservation", client=client)
    assert result["status"] == "accepted" and result["local_state"] == "finalized"
    assert len(runtime.publications) == 1
    assert_opaque(Checkout.load(draft.path).document(), expected)
    reopened = saved_publish._exact(client, dict(project_id="p", timeline_id="t"), result["accepted_revision_id"])
    assert_opaque(reopened, expected)
    assert Checkout(reopened).clip("c1.rocket").get("x") == 90
    assert read_saved_calls(Checkout.load(draft.path)) == ()


def test_incomplete_scene_preflight_writes_no_publish_intent_or_request(setup, monkeypatch):
    from astrid.core.timeline import scene_answers
    from astrid.core.execution.remotion_frame_owner import FrameOwnerError

    draft, runtime = setup
    edit(draft, x=90)
    before = list(metadata())
    def unavailable():
        raise FrameOwnerError("synthetic owner unavailable")
    monkeypatch.setattr(scene_answers, "_client", unavailable)
    with pytest.raises(saved_publish.TimelineEditError, match="scene evaluation incomplete"):
        draft.publish(client=runtime.client())
    assert metadata() == before
    assert runtime.calls == [] and runtime.publications == []


def test_pending_accepted_publish_replays_without_new_scene_evaluation(setup, monkeypatch):
    from astrid.core.timeline import scene_answers

    draft, runtime = setup
    edit(draft, x=90)
    runtime.lose = True
    with pytest.raises(WorkspaceClientError):
        draft.publish(client=runtime.client())
    original = runtime.calls[0][0]
    monkeypatch.setattr(scene_answers, "_client", lambda: pytest.fail("accepted retry must replay its original envelope"))
    result = Checkout.load(draft.path).publish(client=runtime.client())
    assert result["status"] == "accepted" and result["local_state"] == "finalized"
    assert runtime.calls[-1][0] == original and len(runtime.publications) == 1
