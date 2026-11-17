"""Real blocking primitives must be visible to the separate counter run."""

import asyncio
import select
import selectors
import socket
import sqlite3
import sys
import threading
import time

from tests._local_speed_runtime import RuntimeObserver, request_path
from tests.core.generation.test_local_speed_process_observer import observer


def test_event_wait_loop_is_counted_once_without_its_internal_condition():
    meter = observer()
    event = threading.Event()
    meter.start()
    try:
        for _ in range(8):
            assert event.wait(0) is False
        row = meter.snapshot()
    finally:
        meter.stop()
    assert row["counts"]["event_wait_attempt"] == 8
    assert row["counts"]["event_wait_success"] == 8
    assert row["counts"].get("condition_wait_attempt", 0) == 0


def test_commit_origin_requires_the_observed_background_owner_and_preserves_http_work():
    connection, meter = sqlite3.connect(":memory:"), observer()
    connection.execute("CREATE TABLE proof (value INTEGER)")
    meter.runtime_connection = connection

    def commit():
        connection.execute("INSERT INTO proof VALUES (1)")
        connection.commit()

    class Server:
        def reap(self):
            commit()

    class Store:
        def reap(self):
            commit()

    server, store = Server(), Store()
    meter.runtime_background_code, meter.runtime_background_owner = Server.reap.__code__, server
    meter.runtime_reap_code, meter.runtime_reap_owner = Store.reap.__code__, store
    meter.start()
    try:
        server.reap()
        Server().reap()  # Same code from another server is not our background work.
        meter.context.route = "GET /v1/tasks/<task>"
        store.reap()
        meter.context.route = "POST /v1/attempts/<attempt>/heartbeat"
        commit()
        meter.context.route = "POST /v1/tasks"
        commit()
        row = meter.snapshot()
    finally:
        meter.stop()
        connection.close()
    assert row["counts"]["runtime_outer_commit_success"] == 5
    assert [span["write_context"] for span in row["write_spans"]] == [
        "background_lifetime_reaper", "unattributed_runtime", "http_lifetime_reap",
        "http_heartbeat", "http_request",
    ]


def test_active_runtime_scope_is_retained_only_through_the_measured_reply():
    runtime = RuntimeObserver.__new__(RuntimeObserver)
    runtime.requests = [{"kind": "runtime_request_span", "monotonic_start_ns": 10,
                         "monotonic_end_ns": 20, "epoch_start_ns": 110, "epoch_end_ns": 120}]
    runtime.background = [{"kind": "runtime_background_span", "monotonic_start_ns": 15,
                           "monotonic_end_ns": None, "epoch_start_ns": 115, "epoch_end_ns": None}]
    spans = runtime.write_context_spans(18, 30, 130)
    assert len(spans) == 2
    assert spans[0]["epoch_end_ns"] == 120
    assert spans[1]["epoch_end_ns"] == 130
    assert spans[1]["clipped_at_reply"] is True
    assert runtime.background[0]["epoch_end_ns"] is None
    assert runtime.write_context_spans(31, 40, 140)[0]["kind"] == "runtime_background_span"


def test_background_wrapper_preserves_service_arguments_results_and_failures(monkeypatch):
    from types import SimpleNamespace

    import pytest

    calls = []

    class Server:
        def _reap_task_lifetimes(self, runtime, *, token=None):
            calls.append((self, runtime, token))
            if isinstance(runtime, Exception):
                raise runtime
            return token

    class Handler:
        def _dispatch(self):
            raise AssertionError("unused dispatch")

    monkeypatch.setitem(sys.modules, "runtime_protocol", SimpleNamespace(platform=SimpleNamespace()))
    monkeypatch.setitem(sys.modules, "runtime_protocol.server",
                        SimpleNamespace(RuntimeHTTPServer=Server, RuntimeHandler=Handler))
    server, other = Server(), Server()
    runtime = RuntimeObserver.__new__(RuntimeObserver)
    runtime.daemon, runtime.process = SimpleNamespace(httpd=server), observer()
    runtime.requests, runtime.background = [], []
    runtime.install()
    try:
        service = object()
        assert server._reap_task_lifetimes(service, token="result") == "result"
        assert other._reap_task_lifetimes(service, token="other") == "other"
        failure = ValueError("preserved failure")
        with pytest.raises(ValueError, match="preserved failure"):
            server._reap_task_lifetimes(failure)
    finally:
        runtime.process.patches.undo()
    assert calls == [(server, service, "result"), (other, service, "other"), (server, failure, None)]
    assert len(runtime.background) == 2
    assert all(span["epoch_end_ns"] is not None for span in runtime.background)


def test_direct_condition_wait_is_visible():
    condition, meter = threading.Condition(), observer()
    meter.start()
    try:
        with condition:
            assert condition.wait(0) is False
        row = meter.snapshot()
    finally:
        meter.stop()
    assert row["counts"]["condition_wait_attempt"] == 1


def test_async_sleep_counts_one_call_not_each_coroutine_resume():
    meter = observer()
    async def work():
        await asyncio.gather(asyncio.sleep(0), asyncio.sleep(0.001))
    meter.start()
    try:
        asyncio.run(work())
        row = meter.snapshot()
    finally:
        meter.stop()
    assert row["counts"]["asyncio_sleep_attempt"] == 2
    assert row["counts"]["asyncio_sleep_success"] == 2
    assert row["counts"]["selector_wait_attempt"] > 0


def test_selector_wait_is_visible_without_double_counting_inheritance():
    meter = observer()
    reader, writer = socket.socketpair()
    with reader, writer, selectors.DefaultSelector() as selector:
        selector.register(reader, selectors.EVENT_READ)
        meter.start()
        try:
            selector.select(0)
            row = meter.snapshot()
        finally:
            meter.stop()
    assert row["counts"]["selector_wait_attempt"] == 1


def test_direct_select_wait_is_visible_even_through_an_existing_alias():
    select_alias = select.select
    meter = observer()
    reader, writer = socket.socketpair()
    with reader, writer:
        meter.start()
        try:
            select_alias([reader], [], [], 0)
            row = meter.snapshot()
        finally:
            meter.stop()
    assert row["counts"]["selector_wait_attempt"] == 1


def test_successful_outer_commit_is_not_a_savepoint_or_noop():
    meter = observer()
    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE proof (n)")
    meter.runtime_connection = connection
    meter.start()
    try:
        connection.commit()  # no transaction
        connection.execute("BEGIN")
        connection.execute("SAVEPOINT nested")
        connection.execute("INSERT INTO proof VALUES (1)")
        connection.execute("RELEASE nested")
        connection.commit()
        row = meter.snapshot()
    finally:
        meter.stop()
        connection.close()
    assert row["counts"]["runtime_outer_commit_success"] == 1
    assert len(row["write_spans"]) == 1


def test_runtime_claim_and_status_are_distinct_paths_with_private_ids_removed():
    assert request_path("POST", "/v1/tasks/claim?actor=secret") == "POST /v1/tasks/claim"
    assert request_path("GET", "/v1/tasks/task-one?cursor=secret") == "GET /v1/tasks/<task>"
    assert request_path("GET", "/v1/tasks/task-two/managed-outputs") == "GET /v1/tasks/<task>/managed-outputs"
    assert request_path("POST", "/v1/attempts/one/settle") == "POST /v1/attempts/<attempt>/settle"
    assert request_path("GET", "/v1/projects/123/objects/sha256%3A" + "f" * 64) == "GET /v1/projects/<project>/objects/<digest>"


def test_wait_already_in_flight_is_reported_separately_from_new_waits():
    meter, waiting, release = observer(), threading.Event(), threading.Event()
    def work():
        waiting.set()
        release.wait()
    meter.start()
    try:
        thread = threading.Thread(target=work)
        thread.start()
        waiting.wait()
        # Synchronize on the observed wait entry; do not guess from a delay.
        deadline = time.monotonic() + 5
        while not any(row.get("tid") == thread.native_id and row["kind"] == "event_wait_attempt" for row in meter.events):
            assert time.monotonic() < deadline
            time.sleep(0.001)
        start = time.monotonic_ns()
        row = meter.snapshot(start)
        assert row["wait_carry_in"]["event_wait"] == 1
        assert row["counts"].get("event_wait_attempt", 0) == 0
    finally:
        release.set()
        thread.join(timeout=5)
        meter.stop()


def test_failed_outer_commit_is_retained_as_error_not_success():
    meter = observer()
    connection = sqlite3.connect(":memory:")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("CREATE TABLE parent(id PRIMARY KEY)")
    connection.execute("CREATE TABLE child(id REFERENCES parent(id) DEFERRABLE INITIALLY DEFERRED)")
    meter.runtime_connection = connection
    meter.runtime_commit_code = sys._getframe().f_code
    meter.start()
    try:
        connection.execute("INSERT INTO child VALUES (1)")
        try:
            connection.commit()
        except sqlite3.IntegrityError:
            pass
        else:
            raise AssertionError("invalid transaction committed")
        row = meter.snapshot()
    finally:
        meter.stop()
        connection.close()
    assert row["counts"]["runtime_outer_commit_error"] == 1
    assert row["counts"].get("runtime_outer_commit_success", 0) == 0
