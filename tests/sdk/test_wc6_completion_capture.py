"""Offline delegates exercise the real coordinator; no daemon or original body."""
from __future__ import annotations

import ast
import hashlib
import json
import sys
import threading
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from runtime_protocol.timeline_commit_execution import CommitExecutionCoordinator
from runtime_protocol.timeline_commit_store import request_identity

from tests.helpers.wc6_completion_capture import CompletionCapture
from tests.sdk import test_timeline_sync

connected = test_timeline_sync.connected


def context(key: Any = "wc6-undo") -> dict[str, Any]:
    request = {
        "api_version": 1, "project_id": "p", "timeline_id": "t",
        "base_revision_id": "r0", "base_publication_id": "pub0",
        "commands": [{"schema_version": 1, "op": "timeline.inverse", "version": "1",
                      "select": None, "args": {"publications": ["pub1"]}}],
        "label": "Undo", "steps": [], "merge": "fields", "reads": [],
        "dry_run": False, "source": {"via": "python", "detail": "offline"},
    }
    ctx = {"request": request, "invocation": {"operation": "commitTimeline",
           "path": "/v1/projects/p/timelines/t/commit", "body": request},
           "idempotency_key": key, "publisher": "principal", "realm_id": "realm"}
    ctx["request_hash"] = request_identity(ctx)
    return ctx


class Store:
    def __init__(self, calls: list[str], final_error: BaseException | None = None):
        self.calls = calls
        self.final_error = final_error
        self.winner: Any = None
        self.lookups = 0
        self.replay_memo: set[Any] = set()
        self.history_state: dict[str, dict[Any, Any]] = {}
        self.replay_generation = 0

    def clear_replay_memo(self) -> None:
        self.replay_memo.clear()
        self.history_state = {key: {} for key in (
            "records", "entries", "closures", "heads", "chains", "chain_tails", "events", "objects",
        )}
        self.replay_generation += 1

    @contextmanager
    def verification_scope(self) -> Any:
        yield

    @contextmanager
    def history_read_scope(self) -> Any:
        yield

    def lookup(self, ctx: Any, *, authorize: Any) -> Any:
        self.lookups += 1
        self.calls.append("lookup")
        authorize()
        if self.lookups == 2 and self.final_error:
            raise self.final_error
        return self.winner


class Port:
    def __init__(self, calls: list[str], close_error: BaseException | None = None):
        self.calls = calls
        self.close_error = close_error

    def close(self) -> None:
        self.calls.append("close")
        if self.close_error:
            raise self.close_error


class Provider:
    def __init__(self, calls: list[str], store: Store, error: BaseException | None = None):
        self.calls, self.store, self.error = calls, store, error

    def commit(self, ctx: Any, port: Any) -> Any:
        self.calls.append("provider")
        if self.error:
            raise self.error
        # An offline delegate's value, never evidence of durable acceptance.
        self.store.winner = {"data": {"status": "accepted"}, "receipt": {"receipt_id": "offline"}}
        return self.store.winner


def capture_at(tmp_path: Path, monkeypatch: Any) -> CompletionCapture:
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    return CompletionCapture(tmp_path / "private.jsonl")


def records(tmp_path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in (tmp_path / "private.jsonl").read_text().splitlines()]


@pytest.mark.parametrize("failure", ["none", "provider", "construction", "close", "masked"],
                         ids=["success", "provider-error", "port-construction-error", "close-error", "lookup-masks-error"])
def test_real_coordinator_delegates_once_and_preserves_outcome(tmp_path: Path, monkeypatch: Any, failure: str) -> None:
    from runtime_protocol.errors import AuthorizationError

    calls: list[str] = []
    primary = RuntimeError("private message: Bearer must-not-escape")
    final = AuthorizationError("private authentication detail") if failure == "masked" else None
    store = Store(calls, final)
    provider: Any = Provider(calls, store, primary if failure in ("provider", "masked") else None)
    port = Port(calls, primary if failure == "close" else None)
    if failure == "close":
        # No durable result: demonstrate the close error raised by the real coordinator.
        def commit(ctx: Any, selected_port: Any) -> Any:
            calls.append("provider")
            return {"details": {"status": "invalid"}}
        class Refusal:
            def commit(self, ctx: Any, selected_port: Any) -> Any:
                return commit(ctx, selected_port)
        provider = Refusal()

    def port_factory(ctx: Any) -> Port:
        calls.append("construct")
        if failure == "construction":
            raise primary
        return port

    def authorize() -> None:
        calls.append("authorize")

    capture = capture_at(tmp_path, monkeypatch)
    capture.start()
    try:
        coordinator = CommitExecutionCoordinator(store)
        if failure == "none":
            result = coordinator.execute(context(), provider, port_factory, authorize=authorize)
            assert result == store.winner
        else:
            with pytest.raises(BaseException) as caught:
                coordinator.execute(context(), provider, port_factory, authorize=authorize)
            assert caught.value is (final if final else primary)
            traceback = caught.value.__traceback__
            names = []
            while traceback:
                names.append(traceback.tb_frame.f_code.co_name)
                traceback = traceback.tb_next
            assert "execute" in names
            assert ("port_factory" if failure == "construction" else "close" if failure == "close" else
                    "lookup" if failure == "masked" else "commit") in names
    finally:
        capture.stop()
    expected = ["lookup", "authorize", "construct"]
    if failure != "construction":
        expected += ["provider", "close"]
    assert calls == expected + ["lookup", "authorize"]
    rows = records(tmp_path)
    phases = [row["phase"] for row in rows]
    assert phases.count("coordinator.call") == phases.count("port_factory.call") == 1
    assert phases.count("lookup.2.call") == 1
    if failure != "construction":
        assert phases.index("provider.return") < phases.index("close.call") < phases.index("lookup.2.call")
    if failure == "masked":
        assert any(row["phase"] == "coordinator.saved" and row["error"] == {
            "type": "builtins.RuntimeError", "object_id": id(primary)} for row in rows)
        assert next(row for row in rows if row["phase"] == "lookup.2.return")["completion"] == "raised"
    assert capture.failures == []
    assert "must-not-escape" not in (tmp_path / "private.jsonl").read_text()
    assert (tmp_path / "private.jsonl").stat().st_mode & 0o777 == 0o600


def test_sink_failure_preserves_original_exception(tmp_path: Path, monkeypatch: Any) -> None:
    calls: list[str] = []
    store = Store(calls)
    primary = ValueError("original")
    provider = Provider(calls, store, primary)
    def port_factory(ctx: Any) -> Port:
        return Port(calls)
    capture = capture_at(tmp_path, monkeypatch)
    capture.stream.close()
    capture.start()
    try:
        with pytest.raises(ValueError) as caught:
            CommitExecutionCoordinator(store).execute(context(), provider, port_factory, authorize=lambda: None)
        assert caught.value is primary
    finally:
        capture.stop()
    assert capture.failures and set(capture.failures) == {"ValueError"}


@pytest.mark.parametrize("key", [None, "other"])
def test_absent_selected_key_is_explicit(tmp_path: Path, monkeypatch: Any, key: Any) -> None:
    calls: list[str] = []
    store = Store(calls)
    def port_factory(ctx: Any) -> Port:
        return Port(calls)
    capture = capture_at(tmp_path, monkeypatch)
    capture.start()
    try:
        CommitExecutionCoordinator(store).execute(context(key), Provider(calls, store), port_factory, authorize=lambda: None)
    finally:
        capture.stop()
    assert not capture.selected
    assert any(row.get("boundary") == "coordinator.call" for row in records(tmp_path))
    assert not any(row["phase"] == "request" for row in records(tmp_path))


def test_credentials_excluded_and_second_invocation_not_observed(tmp_path: Path, monkeypatch: Any) -> None:
    calls: list[str] = []
    store = Store(calls)
    def port_factory(ctx: Any) -> Port:
        return Port(calls)
    ctx = context()
    ctx["request"]["source"] = {"detail": "Bearer private-value", "access_token": "opaque-private",
                                "api_key": "private-api-value"}
    ctx["request_hash"] = request_identity(ctx)
    capture = capture_at(tmp_path, monkeypatch)
    capture.start()
    try:
        coordinator = CommitExecutionCoordinator(store)
        for _ in range(2):
            coordinator.execute(ctx, Provider(calls, store), port_factory, authorize=lambda: None)
    finally:
        capture.stop()
    content = (tmp_path / "private.jsonl").read_text()
    assert "private-value" not in content and "opaque-private" not in content
    assert "private-api-value" not in content
    assert len([r for r in records(tmp_path) if r["phase"] == "coordinator.call"]) == 1
    assert sys.gettrace() is None and threading.gettrace() is None


def test_preserved_original_and_mounted_source_attachment() -> None:
    from runtime_protocol.service import RuntimeService
    from runtime_protocol.timeline_commit_port import RequestCommitPort

    from astrid.sdk.timeline.commit import RuntimeCommitService
    from astrid.sdk.timeline.sync import TimelineSession

    root = Path(__file__).parents[2]
    original = root / "tests/timeline/test_authoring_media.py"
    content = original.read_bytes()
    blob = hashlib.sha1(f"blob {len(content)}\0".encode() + content).hexdigest()
    assert blob == "adc6fb3946de7c0fc75dc5bf440c8bdc73b59e17"
    tree = ast.parse(content)
    body = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name ==
                "test_real_runtime_document_script_same_alias_rebind_reload_undo_retry_and_aba")
    assert sum(isinstance(n, ast.Assert) for n in ast.walk(body)) == 59
    # Observe these existing code objects; the plugin neither installs a provider
    # nor substitutes service, session, port, authorization or coordinator methods.
    for method, name in [(TimelineSession.undo, "TimelineSession.undo"),
                         (RuntimeService._execute_timeline_commit, "_clear_replay_on_refusal.<locals>.wrapped"),
                         (RuntimeCommitService.commit, "RuntimeCommitService.commit"),
                         (RequestCommitPort.close, "RequestCommitPort.close")]:
        assert method.__code__.co_qualname == name
    source = Path(RuntimeService._execute_timeline_commit.__code__.co_filename).read_text()
    assert "coordinator.execute(context, provider, port_factory, authorize=check," in source
    assert "diagnostic_request_id=request_id)" in source
    assert "return RequestCommitPort(self, commits, bound_context, require_active)" in source
    assert "undo_session.undo([publication['receipt']['receipt_id']], idempotency_key='wc6-undo')" in content.decode()


def test_session_calls_mounted_service_real_provider_and_port_offline(
    connected: Any, tmp_path: Path, monkeypatch: Any,
) -> None:
    from runtime_protocol.service import RuntimeService

    from astrid.sdk.timeline.commit import create_runtime_commit_service
    from astrid.sdk.workspace_client import WorkspaceClientError

    calls: list[str] = []
    primary = RuntimeError("offline boundary; no source closure or live store")

    class OfflineStore(Store):
        def revision_closure(self, *args: Any) -> Any:
            calls.append("revision_closure")
            raise primary

        def history_entry(self, *args: Any) -> Any:
            calls.append("history_entry")
            raise primary

    commits = OfflineStore(calls)
    # Only storage and admission dependencies are delegates. The mounted service,
    # authorization wrapper, coordinator, provider and request port execute their
    # existing bodies. No registration, daemon, socket or fixture realm is used.
    service = object.__new__(RuntimeService)
    service.realm = {"id": "realm"}
    service.store = SimpleNamespace(
        _mutex=threading.RLock(),
        get_project=lambda project: {"id": project, "realm_id": "realm"},
        conn=SimpleNamespace(execute=lambda *args: SimpleNamespace(
            fetchone=lambda: {"publication_id": "pub0"})),
    )
    service._timeline_commit_store = commits
    service._timeline_commit_coordinator = CommitExecutionCoordinator(commits)
    service._timeline_commit_provider = create_runtime_commit_service()
    monkeypatch.setattr(service, "_assert_mutation_admitted", lambda: None)

    _, generated, _ = connected
    service_errors: list[BaseException] = []
    def transport(project: str, timeline: str, body: Any, *, idempotency_key: str) -> Any:
        try:
            return service.commit_timeline(
                project, timeline, body, idempotency_key=idempotency_key,
                publisher="offline-principal", invocation_path=f"/v1/projects/{project}/timelines/{timeline}/commit",
                authorize=lambda: calls.append("authorize"),
            )
        except Exception as exc:
            service_errors.append(exc)
            raise
    monkeypatch.setattr(generated, "commit_timeline", transport)
    session = test_timeline_sync.open_session(connected)
    capture = capture_at(tmp_path, monkeypatch)
    capture.start()
    try:
        with pytest.raises(WorkspaceClientError) as caught:
            session.undo(["pub1"], idempotency_key="wc6-undo")
        assert caught.value.code == "not_ready"
        assert service_errors[0].__cause__ is primary
    finally:
        capture.stop()
    rows = records(tmp_path)
    sources = {r["phase"]: r["source"] for r in rows if "source" in r}
    assert sources["session.undo.call"]["qualname"] == "TimelineSession.undo"
    assert sources["service.call"]["qualname"] == "RuntimeService._execute_timeline_commit"
    assert sources["port_factory.call"]["qualname"] == "RuntimeService._execute_timeline_commit.<locals>.port_factory"
    assert sources["provider.call"]["qualname"] == "RuntimeCommitService.commit"
    assert sources["close.call"]["qualname"] == "RequestCommitPort.close"
    assert next(r for r in rows if r["phase"] == "port.identity")["type"] == "runtime_protocol.timeline_commit_port.RequestCommitPort"
    assert capture.failures == []


def test_protocol_sink_setup_failure_yields_unchanged_test(tmp_path: Path, monkeypatch: Any) -> None:
    from tests.helpers.wc6_completion_capture import ORIGINAL, pytest_runtest_protocol

    monkeypatch.setenv("TMPDIR", str(tmp_path))
    messages: list[str] = []
    item = SimpleNamespace(nodeid=ORIGINAL, config=SimpleNamespace(
        getoption=lambda name: str(tmp_path / "missing-parent" / "sink.jsonl"),
        pluginmanager=SimpleNamespace(get_plugin=lambda name: SimpleNamespace(write_line=messages.append)),
    ))
    protocol = pytest_runtest_protocol(item)
    next(protocol)  # Test setup/body/teardown remains reachable.
    with pytest.raises(StopIteration):
        next(protocol)
    assert messages == ["WC6 capture unavailable or incomplete: diagnostic sink/observer failure; original outcome preserved"]


def test_completion_projection_excludes_nested_documents() -> None:
    from tests.helpers.wc6_completion_capture import _result

    document = {"canonical_document": {"private-payload": "must-not-capture"}}
    for field in ("data", "receipt", "result", "head", "details", "previous", "rebased_onto",
                  "event_ids", "project_seq", "created_at", "status", "items"):
        for value in (document, [document]):
            projected = _result({field: value})
            assert "must-not-capture" not in json.dumps(projected)
    assert _result({"receipt": {"receipt_id": "pub1", "result": {
        "status": "accepted", **document}}}) == {
            "receipt": {"receipt_id": "pub1", "result": {"status": "accepted"}}}
