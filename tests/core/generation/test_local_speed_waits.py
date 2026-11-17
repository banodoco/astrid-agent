"""Real blocking primitives must be visible to the separate counter run."""

import asyncio
import selectors
import sqlite3
import threading

from tests.core.generation.test_local_speed_process_observer import observer
from tests._local_speed_runtime import request_path


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
    with selectors.DefaultSelector() as selector:
        meter.start()
        try:
            selector.select(0)
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
