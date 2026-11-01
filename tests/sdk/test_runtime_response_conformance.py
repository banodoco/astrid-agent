"""Astrid vs the Runtime's recorded responses (testing plan C2.2, Astrid side).

``tests/fixtures/runtime-responses/`` is the Runtime's ``conformance/responses/`` vendored under its own lock.
Two questions, both answered from those bytes:

* Does Astrid's own client path (generated client -> ``WorkspaceClient`` -> ``RemoteTasks``) parse each recorded
  body, send the recorded request, and map the recorded errors?
* Does each in-repo Runtime fake that unit tests use in place of a daemon answer with the recorded status and
  the recorded JSON shape (keys recursively, value types)?  A fake that drifts from the wire makes every test
  using it prove the wrong thing.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Any

import pytest

from astrid.sdk.exceptions import (
    _SERVICE_ERROR_CLASSES,
    ServiceConflictError,
    ServiceError,
    ServiceNotFoundError,
    ServiceValidationError,
)
from astrid.sdk.remote import RemoteTasks
from astrid.sdk.workspace_client import WorkspaceClient, WorkspaceClientError
from astrid_runtime_client import WorkspaceClient as GeneratedWorkspaceClient
from astrid_runtime_client import generated

TESTS = Path(__file__).resolve().parents[1]
FIXTURES = TESTS / "fixtures" / "runtime-responses"
CASES = sorted(path.stem for path in FIXTURES.glob("*.json") if path.name != "vendor.lock.json")

# Free-form members: the contract fixes that they are objects, not what is inside them.
FREE_FORM = {"payload", "spec", "details", "result"}
# The Runtime leaves a null task field out of the body (a queued task has no result or binding), so a fake may
# carry any other field the generated Task declares.
OPTIONAL = {field.name for field in dataclasses.fields(generated.Task)}


def fixture(case: str) -> dict:
    return json.loads((FIXTURES / f"{case}.json").read_text(encoding="utf-8"))


def _kind(value: Any) -> str:
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "number"
    return type(value).__name__


def shape_diff(actual: Any, expected: Any, path: str = "$") -> list[str]:
    """Differences in keys (recursively) and value types. ``null`` on either side matches any type (optional fields)."""
    if actual is None or expected is None:
        return []
    if _kind(actual) != _kind(expected):
        return [f"{path}: {_kind(actual)}, recorded {_kind(expected)}"]
    if isinstance(expected, dict):
        found = [f"{path}.{key}: missing" for key in expected.keys() - actual.keys()]
        found += [f"{path}.{key}: not in the recorded response" for key in actual.keys() - expected.keys() - OPTIONAL]
        for key in expected.keys() & actual.keys():
            if key not in FREE_FORM:
                found += shape_diff(actual[key], expected[key], f"{path}.{key}")
        return found
    if isinstance(expected, list) and actual and expected:
        return [d for index, item in enumerate(actual) for d in shape_diff(item, expected[0], f"{path}[{index}]")]
    return []


def _load(relative: str):
    path = TESTS / relative
    spec = importlib.util.spec_from_file_location("fixture_conformance_" + path.stem, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _concrete(value: Any, **ids: str) -> Any:
    """Replace ``<name>`` placeholders by the ids a test chose."""
    return json.loads(re.sub(r"<(\w+)>", lambda m: ids.get(m.group(1), m.group(0)), json.dumps(value)))


# -- the shape comparator itself ---------------------------------------------------------------------

def test_shape_diff_names_missing_extra_and_retyped_keys() -> None:
    recorded = {"a": 1, "b": {"c": "x"}, "d": [{"e": True}], "payload": {"anything": 1}, "n": None}
    assert shape_diff({"a": 2, "b": {"c": "y"}, "d": [{"e": False}], "payload": {}, "n": "set"}, recorded) == []
    assert sorted(shape_diff({"a": "1", "b": {}, "d": [{"e": 1, "f": 2}], "payload": {}, "n": None, "z": 1}, recorded)) == [
        "$.a: str, recorded number", "$.b.c: missing", "$.d[0].e: number, recorded bool",
        "$.d[0].f: not in the recorded response", "$.z: not in the recorded response",
    ]


def test_every_recorded_case_is_present() -> None:
    assert CASES == [
        "events_page_1_200", "events_page_2_200", "invalid_cursor_400", "managed_outputs_200",
        "not_found_404", "publish_conflict_409", "task_admission_201", "task_read_200", "validation_error_422",
    ]


# -- Astrid's own path over the recorded bytes ------------------------------------------------------

def _client(case: str) -> tuple[WorkspaceClient, list[tuple[str, str, bytes | None]]]:
    recorded = fixture(case)
    calls: list[tuple[str, str, bytes | None]] = []

    def transport(method, path, headers, body):
        calls.append((method, path, body))
        return recorded["status"], {"content-type": "application/json"}, json.dumps(recorded["body"]).encode()

    client = object.__new__(WorkspaceClient)
    client._generated = GeneratedWorkspaceClient("http://runtime", transport=transport)
    client.actor_id = None
    client._last_handshake = None
    return client, calls


def _sent_the_recorded_request(case: str, calls: list, **ids: str) -> None:
    recorded = _concrete(fixture(case)["request"], **ids)
    method, path, body = calls[-1]
    assert method == recorded["method"]
    sent_path, _, sent_query = path.partition("?")
    recorded_path, _, recorded_query = recorded["path"].partition("?")
    assert sent_path == recorded_path
    assert set(recorded_query.split("&")) - {""} <= set(sent_query.split("&"))  # Astrid may add defaults (limit)
    if recorded["body"] is not None:
        sent = json.loads(body)
        assert {k: v for k, v in sent.items() if k in recorded["body"] and k != "project"} == {
            k: v for k, v in recorded["body"].items() if k != "project"}


def test_task_admission_201_parses_to_data_and_receipt() -> None:
    client, calls = _client("task_admission_201")
    request = fixture("task_admission_201")["request"]["body"]
    result = RemoteTasks(client).dispatch_admission({
        "capability_id": request["capability_id"], "capability_digest": request["capability_digest"],
        "input_object_ids": request["input_object_ids"], "idempotency_key": "task",
        "project_id": request["project"], "spec": request["spec"],
    })
    assert result.ok, result.error
    assert result.data["task_id"] == "<task_id>" and result.data["state"] == "queued"
    assert result.receipt is not None and result.receipt.command_kind == "task.create"
    _sent_the_recorded_request("task_admission_201", calls)


def test_task_read_200_parses_to_a_task() -> None:
    client, calls = _client("task_read_200")
    result = RemoteTasks(client).show("task-1")
    assert result.ok, result.error
    assert (result.data["task_id"], result.data["state"], result.data["capability_id"]) == ("<task_id>", "queued", "render.recorded")
    _sent_the_recorded_request("task_read_200", calls, task_id="task-1")


@pytest.mark.parametrize(("case", "next_cursor"), [("events_page_1_200", "<cursor>"), ("events_page_2_200", None)])
def test_event_pages_parse_with_their_cursor(case: str, next_cursor: str | None) -> None:
    client, calls = _client(case)
    cursor = None if case == "events_page_1_200" else "<cursor>"
    result = RemoteTasks(client).events("<task_id>", cursor=cursor, limit=2)
    assert result.ok, result.error
    events, got_cursor = result.data
    assert got_cursor == next_cursor
    assert [e["sequence"] for e in events] == [e["sequence"] for e in fixture(case)["body"]["items"]]
    assert all(e["aggregate_id"] == "<task_id>" for e in events)
    method, path, _ = calls[-1]
    assert (method, path.partition("?")[0]) == ("GET", "/v1/events")  # Astrid scopes by aggregate_id, adds no other route


def test_managed_outputs_200_parse_to_items() -> None:
    client, calls = _client("managed_outputs_200")
    result = RemoteTasks(client).list_managed_outputs("task-1")
    assert result.ok, result.error
    items, next_cursor = result.data
    assert next_cursor is None and items[0]["association_id"] == "<association_id>"
    assert items[0]["lifecycle"]["state"] == "available" and items[0]["producer"] == {"capability_id": "render.recorded"}
    _sent_the_recorded_request("managed_outputs_200", calls, task_id="task-1")


def test_not_found_404_maps_to_not_found() -> None:
    client, calls = _client("not_found_404")
    result = RemoteTasks(client).show("task-does-not-exist")
    assert not result.ok and result.error.code == "not_found"
    assert _SERVICE_ERROR_CLASSES[result.error.code] is ServiceNotFoundError
    _sent_the_recorded_request("not_found_404", calls)


def test_validation_error_422_maps_to_validation_error() -> None:
    client, calls = _client("validation_error_422")
    request = fixture("validation_error_422")["request"]["body"]
    result = RemoteTasks(client).dispatch_admission({
        "capability_id": request["capability_id"], "capability_digest": request["capability_digest"],
        "input_object_ids": request["input_object_ids"], "idempotency_key": "task",
    })
    assert not result.ok and result.error.code == "validation_error"
    assert result.error.message == "capability is required"
    assert _SERVICE_ERROR_CLASSES[result.error.code] is ServiceValidationError
    assert calls[-1][:2] == ("POST", "/v1/tasks")


def test_publish_conflict_409_maps_to_conflict_and_keeps_its_details() -> None:
    client, calls = _client("publish_conflict_409")
    recorded = fixture("publish_conflict_409")
    with pytest.raises(WorkspaceClientError) as raised:
        client.publish_parent_composition("project-1", "main", recorded["request"]["body"], idempotency_key="k")
    error = raised.value
    assert (error.status, error.code) == (409, "conflict")
    assert error.details == recorded["body"]["details"] and error.request_id == "<request_id>"
    assert _SERVICE_ERROR_CLASSES[error.code] is ServiceConflictError
    _sent_the_recorded_request("publish_conflict_409", calls, project_id="project-1")


def test_invalid_cursor_400_keeps_its_status_and_code() -> None:
    # The Runtime's "invalid_request" is not in Astrid's closed nine-code service taxonomy: callers get the
    # generic ServiceError and read the code from it (astrid/sdk/remote.py read_bytes does exactly that).
    client, calls = _client("invalid_cursor_400")
    result = RemoteTasks(client).events("<task_id>", cursor="not-a-cursor")
    assert not result.ok and result.error.code == "invalid_request" and result.error.message == "cursor is invalid"
    assert _SERVICE_ERROR_CLASSES.get("invalid_request", ServiceError) is ServiceError
    with pytest.raises(WorkspaceClientError) as raised:
        client.list_events(cursor="not-a-cursor")
    assert raised.value.status == 400


# -- the in-repo Runtime fakes against the same bytes ----------------------------------------------
# tests/test_generic_host.py FakeRuntime (the most used, 41 references) has no response of its own for these
# operations: ``task()`` returns whatever dict a test stored, so its shape lives in the tests (see the report).

THUMBNAIL = _load("core/test_thumbnail_backfill.py")
ACTIVATION = _load("core/execution/test_remote_activation_owner.py")
MANAGED_OUTPUT = _load("sdk/test_rrp_managed_output_consumer.py")
JOURNEY = _load("journeys/test_journey_plumbing.py")


def _thumbnail_runtime():
    return THUMBNAIL.FakeRuntime(generations=[], variants={}, source_bytes=b"")


def _thumbnail_admission():
    request = fixture("task_admission_201")["request"]["body"]
    return _thumbnail_runtime().admit_task(
        capability_id=request["capability_id"], capability_digest=request["capability_digest"],
        input_object_ids=request["input_object_ids"], idempotency_key="task", project_id=request["project"],
        spec=request["spec"], settlement_effect={"target_id": "generation-1"},
    )


def test_thumbnail_backfill_fake_admission_has_the_recorded_shape() -> None:
    # The fake returns the body only: the generated client takes the 201 from ``expected=(200, 201)``.
    assert shape_diff(_thumbnail_admission(), fixture("task_admission_201")["body"]) == []


def test_thumbnail_backfill_fake_task_read_has_the_recorded_shape() -> None:
    runtime = _thumbnail_runtime()
    request = fixture("task_admission_201")["request"]["body"]
    task_id = runtime.admit_task(
        capability_id="c", capability_digest="d", input_object_ids=[], idempotency_key="k",
        project_id=request["project"], spec={}, settlement_effect={"target_id": "generation-1"},
    )["data"]["task_id"]
    assert shape_diff(runtime.get_task(task_id), fixture("task_read_200")["body"]) == []


def test_remote_activation_fake_task_read_has_the_recorded_shape() -> None:
    task = ACTIVATION.FakeRuntime().get_task("task-a")  # a SimpleNamespace, not the generated Task or its JSON
    assert shape_diff(vars(task), fixture("task_read_200")["body"]) == []


def test_managed_output_fake_listing_has_the_recorded_shape() -> None:
    runtime = MANAGED_OUTPUT.ManagedOutputRuntime(use_get=True)
    items, next_cursor = runtime.list_managed_outputs("task-1")
    recorded = fixture("managed_outputs_200")["body"]
    assert shape_diff({"items": items, "next_cursor": next_cursor}, recorded) == []


def test_managed_output_fake_task_read_has_the_recorded_shape() -> None:
    assert shape_diff(MANAGED_OUTPUT.ManagedOutputRuntime(use_get=True).get_task("task-1"), fixture("task_read_200")["body"]) == []


@pytest.mark.parametrize(("method", "path", "case"), [
    ("POST", "/v1/tasks", "task_admission_201"),
    ("GET", "/v1/tasks/t9", "task_read_200"),
])
def test_journey_loopback_runtime_answers_with_the_recorded_status_and_shape(method: str, path: str, case: str) -> None:
    request = fixture(case)["request"]["body"]
    status, body = JOURNEY._runtime_routes("queued")[(method, path)](path, request)
    recorded = fixture(case)
    assert status == recorded["status"]
    assert shape_diff(body, recorded["body"]) == []
