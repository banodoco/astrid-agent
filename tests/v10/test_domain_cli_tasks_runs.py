"""Supported product CLI contracts for the canonical tasks/runs API.

The retired ``runs retry-failed`` spelling and project arguments on per-id
operations are intentionally not compatibility surfaces.
"""

from __future__ import annotations

import argparse
import json
from typing import Any

import pytest

from astrid.core.cli.domain_product import run_product_family
from astrid.core.receipts.contract import CommandReceipt
from astrid.sdk.contracts import DomainResult, ErrorObject

ENVELOPE_KEYS = {"ok", "data", "error", "receipt", "idempotency_key"}


def _receipt(kind: str, key: str) -> CommandReceipt:
    return CommandReceipt(receipt_id=f"R-{kind}", command_kind=kind,
        idempotency_key=key, request_hash="hash", project_id="P-1",
        project_seq=(1, 1), event_ids=("E-1",), result={"ok": True},
        created_at="2026-01-01T00:00:00Z")


class _Tasks:
    def __init__(self, owner: "_Client") -> None:
        self.owner = owner

    def create(self, *, project_id, capability, spec, input_manifest=None,
               generation_intent=None, settlement_effect=None, idempotency_key=None):
        call = {"project_id": project_id, "capability": capability, "spec": spec,
                "input_manifest": input_manifest, "idempotency_key": idempotency_key}
        if generation_intent is not None:
            call["generation_intent"] = generation_intent
        if settlement_effect is not None:
            call["settlement_effect"] = settlement_effect
        self.owner.calls.append(("tasks.create", call))
        key = idempotency_key or "generated-key"
        return DomainResult.success({"id": "T-1", "project_id": project_id, "status": "queued"},
            receipt=_receipt("core.task.create", key), idempotency_key=key)

    def list(self, project_id, **_page):  # limit/cursor: one page here
        self.owner.calls.append(("tasks.list", {"project_id": project_id}))
        return DomainResult.success([{"id": "T-1", "status": "queued"}])

    def show(self, task_id):
        self.owner.calls.append(("tasks.show", {"task_id": task_id}))
        return DomainResult.success({"id": task_id, "status": "queued"})

    def cancel(self, task_id, *, idempotency_key=None):
        self.owner.calls.append(("tasks.cancel", {"task_id": task_id, "idempotency_key": idempotency_key}))
        key = idempotency_key or "generated-key"
        return DomainResult.success({"id": task_id, "status": "cancelled"},
            receipt=_receipt("core.task.cancel", key), idempotency_key=key)

    def retry(self, task_id, *, idempotency_key=None):
        self.owner.calls.append(("tasks.retry", {"task_id": task_id, "idempotency_key": idempotency_key}))
        key = idempotency_key or "generated-key"
        return DomainResult.success({"id": task_id, "status": "queued"},
            receipt=_receipt("core.task.retry", key), idempotency_key=key)

    def events(self, task_id):
        self.owner.calls.append(("tasks.events", {"task_id": task_id}))
        return DomainResult.success([{"seq": 1, "kind": "core.task.created", "task_id": task_id}])


class _Runs:
    def __init__(self, owner: "_Client") -> None:
        self.owner = owner

    def list(self, project_id, **_page):  # limit/cursor: one page here
        self.owner.calls.append(("runs.list", {"project_id": project_id}))
        return DomainResult.success([{"id": "R-1", "project_id": project_id, "status": "running"}])

    def show(self, run_id):
        self.owner.calls.append(("runs.show", {"run_id": run_id}))
        return DomainResult.success({"id": run_id, "status": "failed", "progress": {"failed": 1}})

    def cancel(self, run_id, *, idempotency_key=None):
        self.owner.calls.append(("runs.cancel", {"run_id": run_id, "idempotency_key": idempotency_key}))
        key = idempotency_key or "generated-key"
        return DomainResult.success({"run_id": run_id, "status": "cancelled"},
            receipt=_receipt("run.cancel", key), idempotency_key=key)

    def retry(self, run_id, *, selected_task_ids=None, idempotency_key=None):
        self.owner.calls.append(("runs.retry", {"run_id": run_id,
            "selected_task_ids": selected_task_ids, "idempotency_key": idempotency_key}))
        key = idempotency_key or "generated-key"
        return DomainResult.success({"run_id": run_id,
            "retried_task_ids": list(selected_task_ids or ["T-2"])},
            receipt=_receipt("run.retry", key), idempotency_key=key)

    def events(self, run_id):
        self.owner.calls.append(("runs.events", {"run_id": run_id}))
        return DomainResult.success([{"seq": 1, "kind": "core.run.created", "run_id": run_id}])

    def open(self, run_id=None, *, project=None):
        self.owner.calls.append(("runs.open", {"project": project, "run_id": run_id}))
        return DomainResult.success(
            {"project_id": project or "P-current", "run_id": run_id or "R-latest", "opened": True}
        )


class _Client:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.tasks, self.runs = _Tasks(self), _Runs(self)


def _run(family: str, args: list[str], client: _Client | None = None) -> int:
    return run_product_family(family, args, client=client or _Client())


def _state_of(line: str) -> str:
    """The state token of a piped progress line such as ``[1h 2m] running 42%``."""
    return line.split("] ", 1)[1].split()[0]


def _choices(parser: argparse.ArgumentParser) -> set[str]:
    action = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    return set(action.choices)


def test_tasks_and_runs_expose_only_canonical_verbs() -> None:
    from astrid.core.cli.domain_tasks import COMMANDS as TASK_COMMANDS, build_parser as task_parser
    from astrid.core.cli.domain_runs import COMMANDS as RUN_COMMANDS, build_parser as run_parser
    assert tuple(c.name for c in TASK_COMMANDS) == ("create", "list", "show", "cancel", "retry", "events", "follow", "logs")
    assert _choices(task_parser(_Client())) == {"create", "list", "show", "cancel", "retry", "events", "follow", "logs"}
    assert tuple(c.name for c in RUN_COMMANDS) == ("list", "show", "cancel", "retry", "events", "open")
    assert _choices(run_parser(_Client())) == {"list", "show", "cancel", "retry", "events", "open"}


def test_tasks_and_runs_default_to_human_and_keep_json_explicit() -> None:
    from astrid.core.cli.domain_tasks import build_parser as task_parser
    from astrid.core.cli.domain_runs import build_parser as run_parser

    task = task_parser(_Client()).parse_args(["list", "--project", "P-1"])
    run = run_parser(_Client()).parse_args(["show", "--project", "P-1", "R-1"])
    assert task.json is False
    assert run.json is False
    assert task_parser(_Client()).parse_args(["list", "--project", "P-1", "--json"]).json is True


def test_runs_open_defaults_to_latest_in_current_project(capsys) -> None:
    client = _Client()

    assert _run("runs", ["open", "--json"], client) == 0

    assert client.calls == [("runs.open", {"project": None, "run_id": None})]
    assert json.loads(capsys.readouterr().out)["data"]["run_id"] == "R-latest"


def test_runs_open_accepts_exact_run_and_project_override(capsys) -> None:
    client = _Client()

    assert _run("runs", ["open", "R-23", "--project", "demo", "--json"], client) == 0

    assert client.calls == [("runs.open", {"project": "demo", "run_id": "R-23"})]
    assert json.loads(capsys.readouterr().out)["data"]["run_id"] == "R-23"


@pytest.mark.parametrize("family,verb,identifier,call", [
    ("tasks", "show", "T-1", "tasks.show"), ("tasks", "events", "T-1", "tasks.events"),
    ("runs", "show", "R-1", "runs.show"), ("runs", "events", "R-1", "runs.events"),
])
def test_per_id_reads_use_generated_client_shape(family, verb, identifier, call, capsys) -> None:
    client = _Client()
    assert _run(family, [verb, "--project", "P-1", identifier, "--json"], client) == 0
    key = "task_id" if family == "tasks" else "run_id"
    assert client.calls == [(call, {key: identifier})]
    assert json.loads(capsys.readouterr().out)["ok"] is True


def test_mutations_forward_only_canonical_arguments(capsys) -> None:
    client = _Client()
    assert _run("tasks", ["cancel", "--project", "P-1", "T-1", "--idempotency-key", "k", "--json"], client) == 0
    assert client.calls[-1] == ("tasks.cancel", {"task_id": "T-1", "idempotency_key": "k"})
    assert _run("runs", ["retry", "--project", "P-1", "R-1", "--task", "T-2", "--json"], client) == 0
    assert client.calls[-1] == ("runs.retry", {"run_id": "R-1", "selected_task_ids": ["T-2"], "idempotency_key": None})
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["receipt"]["command_kind"] == "run.retry"


def test_tasks_create_forwards_generated_contract(capsys) -> None:
    client = _Client()
    generation_intent = {
        "version": 1,
        "modality": "video",
        "partial_success_policy": "reject",
        "groups": [{"group_key": "main", "selectors": []}],
        "metadata": {"shot_id": "shot-1"},
    }
    settlement_effect = {
        "effect_type": "generation.publish_v1",
        "target_id": "P-1",
        "payload": {"version": 1, "modality": "video", "groups": []},
    }
    assert _run("tasks", ["create", "--project", "P-1", "--capability", "cap.a",
        "--spec", '{"x": 1}', "--input-manifest", '[{"media_id":"M-1"}]',
        "--generation-intent", json.dumps(generation_intent),
        "--settlement-effect", json.dumps(settlement_effect), "--json"], client) == 0
    assert client.calls == [("tasks.create", {"project_id": "P-1", "capability": "cap.a",
        "spec": {"x": 1}, "input_manifest": [{"media_id": "M-1"}],
        "generation_intent": generation_intent, "settlement_effect": settlement_effect,
        "idempotency_key": None})]
    envelope = json.loads(capsys.readouterr().out)
    assert set(envelope) == ENVELOPE_KEYS and envelope["receipt"] is not None
    assert envelope["data"]["handoff"]["follow"] == (
        "python3 -m astrid tasks follow T-1 --project P-1"
    )


def test_tasks_follow_prints_only_durable_changes_and_exits_on_success(capsys) -> None:
    client = _Client()
    snapshots = [
        {"task_id": "T-1", "run_id": "R-1", "state": "queued", "version": 1,
         "created_at": "2026-09-04T10:00:00Z", "updated_at": "2026-09-04T10:00:00Z"},
        {"task_id": "T-1", "run_id": "R-1", "state": "queued", "version": 1,
         "created_at": "2026-09-04T10:00:00Z", "updated_at": "2026-09-04T10:00:00Z"},
        {"task_id": "T-1", "run_id": "R-1", "state": "running", "version": 2,
         "attempt_id": "A-1", "created_at": "2026-09-04T10:00:00Z",
         "updated_at": "2026-09-04T10:00:01Z"},
        {"task_id": "T-1", "run_id": "R-1", "state": "succeeded", "version": 2,
         "attempt_id": "A-1", "created_at": "2026-09-04T10:00:00Z",
         "updated_at": "2026-09-04T10:00:02Z", "result": {"outputs": []}},
    ]

    def show(task_id):
        client.calls.append(("tasks.show", {"task_id": task_id}))
        return DomainResult.success(snapshots.pop(0))

    client.tasks.show = show
    assert _run(
        "tasks",
        ["follow", "T-1", "--project", "P-1", "--poll-seconds", "0.001"],
        client,
    ) == 0

    # Piped (captured) output: one line per durable observation, then one summary line.
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 4
    assert [_state_of(line) for line in lines[:3]] == ["queued", "running", "succeeded"]
    assert lines[-1] == "completed task=T-1 run=R-1 output=-"


def test_tasks_follow_json_is_jsonl_one_record_per_line(capsys) -> None:
    client = _Client()
    snapshots = [
        {"task_id": "T-1", "run_id": "R-1", "state": "queued", "version": 1,
         "waiting_reason": "waiting_for_gpu", "updated_at": "2026-09-04T10:00:00Z"},
        {"task_id": "T-1", "run_id": "R-1", "state": "succeeded", "version": 2,
         "attempt_id": "A-2", "updated_at": "2026-09-04T10:00:01Z",
         "result": {"outputs": [{"url": "https://example.test/render.mp4"}]}},
    ]
    client.tasks.show = lambda _task_id: DomainResult.success(snapshots.pop(0))

    assert _run(
        "tasks",
        ["follow", "T-1", "--project", "P-1", "--poll-seconds", "0.001", "--json"],
        client,
    ) == 0

    captured = capsys.readouterr()
    assert captured.err == ""
    records = [json.loads(line) for line in captured.out.splitlines()]
    assert len(records) == 3
    assert [row["state"] for row in records[:2]] == ["queued", "succeeded"]
    assert [row["kind"] for row in records[:2]] == ["observed", "changed"]
    assert records[0]["waiting_reason"] == "waiting_for_gpu"
    final = records[-1]
    assert set(final) >= {"kind", "ok", "state", "outputs", "handoff", "observation_count", "error"}
    assert "observations" not in final and "task" not in final
    assert final["kind"] == "final" and final["ok"] is True and final["error"] is None
    assert final["observation_count"] == 2
    assert final["outputs"][0]["location"] == "https://example.test/render.mp4"
    assert final["handoff"]["open"] == "python3 -m astrid runs open R-1 --project P-1"

def test_tasks_follow_reports_defensible_progress_speed_and_eta(capsys) -> None:
    client = _Client()
    client.tasks.show = lambda _task_id: DomainResult.success({
        "task_id": "T-1", "run_id": "R-1", "state": "succeeded", "version": 2,
        "attempt_id": "A-1", "progress": {
            "phase": "encoding", "completed_units": 75, "total_units": 100,
            "current_speed": 5, "speed_unit": "units/s",
        },
    })

    assert _run("tasks", ["follow", "T-1", "--project", "P-1", "--json"], client) == 0
    observation = json.loads(capsys.readouterr().out.splitlines()[0])
    assert observation["phase"] == "encoding"
    assert observation["progress_percent"] == 75.0
    assert observation["current_speed"] == 5.0
    assert observation["eta_seconds"] == 5
    assert observation["eta_source"] == "remaining/current_speed"

def test_tasks_follow_does_not_invent_queue_progress_or_eta(capsys) -> None:
    client = _Client()
    client.tasks.show = lambda _task_id: DomainResult.success(
        {"task_id": "T-1", "state": "succeeded", "version": 2}
    )

    assert _run("tasks", ["follow", "T-1", "--project", "P-1", "--json"], client) == 0
    observation = json.loads(capsys.readouterr().out.splitlines()[0])
    for key in ("queue_position", "progress_percent", "current_speed", "eta_seconds"):
        assert observation[key] is None
    assert observation["queue_position_unavailable_reason"] == "runtime did not report queue position"

def test_tasks_follow_terminal_failure_is_exit_one(capsys) -> None:
    client = _Client()
    client.tasks.show = lambda _task_id: DomainResult.success(
        {"task_id": "T-1", "state": "failed", "version": 3, "attempt_id": "A-2"}
    )

    assert _run("tasks", ["follow", "T-1", "--project", "P-1", "--json"], client) == 1
    final = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert final["kind"] == "final" and final["ok"] is False
    assert final["error"]["code"] == "task_failed"

@pytest.mark.parametrize("flag,value", [("--poll-seconds", "0"), ("--timeout-seconds", "nan")])
def test_tasks_follow_rejects_non_positive_or_non_finite_intervals(flag, value) -> None:
    client = _Client()
    with pytest.raises(SystemExit) as exc:
        _run("tasks", ["follow", "T-1", "--project", "P-1", flag, value], client)
    assert exc.value.code == 2
    assert client.calls == []


def test_tasks_create_does_not_advertise_unsupported_runtime_admission_fields() -> None:
    from astrid.core.cli.domain_tasks import build_parser

    parser = build_parser(_Client())
    help_text = parser.format_help()
    assert all(flag not in help_text for flag in ("--priority", "--available-at", "--max-attempts", "--dependencies"))
    for flag in ("--priority", "--available-at", "--max-attempts", "--dependencies"):
        with pytest.raises(SystemExit) as exc:
            parser.parse_args(["create", "--project", "P-1", "--capability", "cap.a", "--spec", "{}", flag, "1"])
        assert exc.value.code == 2


def test_runs_show_preserves_runtime_progress_and_optional_evidence(capsys) -> None:
    client = _Client()
    client.runs.show = lambda run_id: DomainResult.success({
        "run_id": run_id,
        "status": "failed",
        "progress": {"total_children": 3, "succeeded": 1, "failed": 2, "cancelled": 0},
        "evidence": [{"evidence_id": "EV-1", "kind": "preview"}],
    })
    assert _run("runs", ["show", "--project", "P-1", "R-1", "--evidence", "--json"], client) == 0
    envelope = json.loads(capsys.readouterr().out)
    assert set(envelope) == ENVELOPE_KEYS
    assert envelope["data"]["progress"]["failed"] == 2
    assert envelope["data"]["evidence"][0]["evidence_id"] == "EV-1"


def test_tasks_and_runs_help_explain_supported_envelope_and_event_scope(capsys) -> None:
    client = _Client()
    with pytest.raises(SystemExit) as tasks_help:
        _run("tasks", ["create", "--help"], client)
    assert tasks_help.value.code == 0
    assert "ok/data/error/receipt/idempotency_key" in capsys.readouterr().out
    with pytest.raises(SystemExit) as runs_help:
        _run("runs", ["events", "--help"], client)
    assert runs_help.value.code == 0
    assert "Child task transitions" in capsys.readouterr().out


def test_project_list_is_one_sdk_call(capsys) -> None:
    client = _Client()
    assert _run("tasks", ["list", "--project", "slug", "--json"], client) == 0
    assert _run("runs", ["list", "--project", "slug", "--json"], client) == 0
    assert client.calls == [("tasks.list", {"project_id": "slug"}), ("runs.list", {"project_id": "slug"})]
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["ok"] is True


def test_typed_failure_renders_json_and_exit_one(capsys) -> None:
    class FailingTasks(_Tasks):
        def retry(self, task_id, *, idempotency_key=None):
            return DomainResult.failure(ErrorObject(code="stale_version", message="m", details={}),
                idempotency_key=idempotency_key or "generated-key")
    client = _Client(); client.tasks = FailingTasks(client)
    assert _run("tasks", ["retry", "--project", "P-1", "T-1", "--json"], client) == 1
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["ok"] is False and envelope["error"]["code"] == "stale_version"


def test_missing_project_is_usage_error_without_sdk_call() -> None:
    client = _Client()
    with pytest.raises(SystemExit) as exc:
        _run("runs", ["show", "R-1"], client)
    assert exc.value.code == 2 and client.calls == []


def test_retired_retry_failed_spelling_is_rejected() -> None:
    with pytest.raises(SystemExit) as exc:
        _run("runs", ["retry-failed", "--project", "P-1", "R-1"], _Client())
    assert exc.value.code == 2


def test_gateway_registers_plural_families() -> None:
    from astrid.core.gateway import dispatch
    assert {"tasks", "runs"} <= dispatch._top_level_commands()


def _rows(count: int) -> list[dict[str, Any]]:
    return [
        {"id": f"{index:032x}", "project_id": "P-1", "capability": "rendering.render",
         "status": "completed" if index % 2 == 0 else "running",
         "created_at": f"2026-10-10T08:0{index}:00+00:00",
         "updated_at": f"2026-10-10T08:1{index}:30+00:00",
         "task_ids": [f"T-{index}"], "spec": {"big": "x" * 5000}}
        for index in range(count)
    ]


def test_runs_list_human_view_is_a_short_table_not_json(capsys) -> None:
    client = _Client()
    client.runs.list = lambda project_id, **_kw: DomainResult.success(_rows(5))
    client.tasks.show = lambda task_id: DomainResult.success({
        "task_id": task_id, "state": "succeeded",
        "result": {"outputs": [{"name": "video", "filename": "hype.mp4", "is_primary": True}]},
    })

    assert _run("runs", ["list", "--project", "P-1", "--capability", "rendering.render", "--limit", "2"], client) == 0
    out = capsys.readouterr().out
    lines = out.splitlines()
    # The MACHINE column (the machine identity on each attempt) sits before OUTPUT, by design.
    assert lines[0].split() == ["ID", "CAPABILITY", "STATE", "CREATED", "(UTC)", "DURATION", "MACHINE", "OUTPUT"]
    assert len(lines) == 4  # header, two rows, footer
    assert "spec" not in out and "{" not in out
    assert lines[1].split()[0] == f"{4:032x}"[:12]
    # These rows ran on no recorded machine, so the column is the compact '-', and the output name ends the line.
    assert "completed" in lines[1] and lines[1].split()[-2:] == ["-", "hype.mp4"]
    assert "running" in lines[2] and "so far" in lines[2] and lines[2].split()[-2:] == ["-", "-"]
    assert lines[-1].startswith("3 more: --limit N")


def test_runs_list_json_stays_the_exact_envelope(capsys) -> None:
    client = _Client()
    client.runs.list = lambda project_id, **_kw: DomainResult.success(_rows(3))
    assert _run("runs", ["list", "--project", "P-1", "--json"], client) == 0
    envelope = json.loads(capsys.readouterr().out)
    assert set(envelope) == ENVELOPE_KEYS
    assert len(envelope["data"]) == 3
    assert envelope["data"][0]["spec"]["big"] == "x" * 5000


def test_tasks_list_human_view_is_a_short_table(capsys) -> None:
    client = _Client()
    client.tasks.list = lambda project_id, **_kw: DomainResult.success([{
        "task_id": "T-9", "capability_id": "rendering.render", "state": "failed",
        "created_at": "2026-10-10T08:00:00+00:00", "updated_at": "2026-10-10T08:00:45+00:00",
        "result": {"outputs": []}, "spec": {"big": "x" * 5000},
    }])
    assert _run("tasks", ["list", "--project", "P-1"], client) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[1].split()[:3] == ["T-9", "rendering.render", "failed"]
    assert lines[1].split()[-1] == "-"  # no output for a failed task
    assert len(lines) == 2


@pytest.mark.parametrize("family", ["tasks", "runs"])
def test_plain_list_shows_the_newest_rows_from_the_last_page(family, capsys) -> None:
    # The Runtime pages oldest first; reading only the first page showed old work as "newest".
    client = _Client()
    rows = _rows(9)
    # Distinct displayed prefixes and dates make this detect sorting page one
    # as if it were the newest page, rather than merely counting the rows.
    for index, row in enumerate(rows):
        row["id"] = f"{index:012x}" + "a" * 20
    pages = {None: (rows[:5], "page-2"), "page-2": (rows[5:], None)}
    cursors = []
    def paged(project_id, cursor=None, **_kw):
        cursors.append(cursor)
        rows, nxt = pages[cursor]
        return DomainResult.success([rows, nxt] if nxt else rows)
    getattr(client, family).list = paged
    client.tasks.show = lambda task_id: DomainResult.success({"task_id": task_id, "state": "succeeded", "result": {"outputs": []}})
    assert _run(family, ["list", "--project", "P-1"], client) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[1].split()[0] == rows[8]["id"][:12]
    assert cursors == [None, "page-2"]
    assert len(lines) == 10  # header and all nine rows


FULL_ID = "89a4381ad6654c0a9f3e2b1c7d8e9f0a"
OTHER_ID = "89a4381ad6650000000000000000aaaa"


def _with_listing(client: _Client, family: str, ids: list[str]) -> None:
    """Give the family's list verb one row per id, recording each list read."""
    owner = client.tasks if family == "tasks" else client.runs

    def list_rows(project_id, **_page):
        client.calls.append((f"{family}.list", {"project_id": project_id}))
        return DomainResult.success([{"id": ident, "status": "queued"} for ident in ids])

    owner.list = list_rows


def test_full_id_is_used_as_given_without_listing(capsys) -> None:
    client = _Client()
    assert len(FULL_ID) == 32
    assert _run("tasks", ["show", "--project", "P-1", FULL_ID, "--json"], client) == 0
    assert client.calls == [("tasks.show", {"task_id": FULL_ID})]
    assert json.loads(capsys.readouterr().out)["ok"] is True


@pytest.mark.parametrize("family,verb,call,key", [
    ("tasks", "show", "tasks.show", "task_id"), ("runs", "show", "runs.show", "run_id"),
    ("runs", "cancel", "runs.cancel", "run_id"),
])
def test_unique_prefix_resolves_to_the_full_id(family, verb, call, key, capsys) -> None:
    client = _Client()
    _with_listing(client, family, [FULL_ID, "1f0e2d3c4b5a69788796a5b4c3d2e1f0"])
    assert _run(family, [verb, "--project", "P-1", FULL_ID[:12], "--json"], client) == 0
    assert client.calls[0] == (f"{family}.list", {"project_id": "P-1"})
    assert client.calls[-1][0] == call and client.calls[-1][1][key] == FULL_ID
    assert json.loads(capsys.readouterr().out)["ok"] is True


def test_ambiguous_prefix_is_a_usage_error_naming_the_matches(capsys) -> None:
    client = _Client()
    _with_listing(client, "runs", [FULL_ID, OTHER_ID])
    assert _run("runs", ["cancel", "--project", "P-1", FULL_ID[:12]], client) == 2
    err = capsys.readouterr().err
    assert FULL_ID in err and OTHER_ID in err
    assert [name for name, _ in client.calls] == ["runs.list"]


def test_unmatched_prefix_is_not_found_and_short_refs_pass_through(capsys) -> None:
    client = _Client()
    _with_listing(client, "tasks", [FULL_ID])
    assert _run("tasks", ["show", "--project", "P-1", "ffffffff00", "--json"], client) == 1
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "not_found"
    assert client.calls == [("tasks.list", {"project_id": "P-1"})]

    client = _Client()
    assert _run("tasks", ["show", "--project", "P-1", "abc123", "--json"], client) == 0
    assert client.calls == [("tasks.show", {"task_id": "abc123"})]


PER_ID_VERBS = [
    (family, verb) for family in ("tasks", "runs")
    for verb in ("show", "cancel", "retry", "events")
] + [("tasks", "follow")]


def _follow_spy(client, monkeypatch):
    def follow(_client, task_id, **_kwargs):
        client.calls.append(("tasks.follow", {"task_id": task_id}))
        return DomainResult.success({"task_id": task_id, "state": "succeeded"})
    monkeypatch.setattr("astrid.core.cli.domain_tasks.follow_task", follow)


@pytest.mark.parametrize("family,verb", PER_ID_VERBS)
@pytest.mark.parametrize("full_id", [False, True])
def test_per_id_verbs_find_later_page_or_bypass_list_for_full_id(
    family, verb, full_id, monkeypatch, capsys,
) -> None:
    client = _Client()
    _follow_spy(client, monkeypatch)
    def paged(project_id, cursor=None, **_kwargs):
        client.calls.append((f"{family}.list", {"cursor": cursor}))
        if cursor is None:
            return DomainResult.success([[{"id": "f" * 32}], "page-2"])
        assert cursor == "page-2"
        return DomainResult.success([[{"id": FULL_ID}], None])
    getattr(client, family).list = paged
    ref = FULL_ID if full_id else FULL_ID[:12]
    assert _run(family, [verb, ref, "--project", "P-1", "--json"], client) == 0
    expected_reads = [] if full_id else [
        (f"{family}.list", {"cursor": None}),
        (f"{family}.list", {"cursor": "page-2"}),
    ]
    assert client.calls[:-1] == expected_reads
    assert client.calls[-1][0] == f"{family}.{verb}"
    assert client.calls[-1][1]["task_id" if family == "tasks" else "run_id"] == FULL_ID
    assert json.loads(capsys.readouterr().out)["ok"] is True


@pytest.mark.parametrize("family,verb", PER_ID_VERBS)
def test_later_page_collision_is_json_usage_error_without_dispatch(family, verb, monkeypatch, capsys) -> None:
    client = _Client()
    _follow_spy(client, monkeypatch)
    def paged(project_id, cursor=None, **_kwargs):
        client.calls.append((f"{family}.list", {"cursor": cursor}))
        return DomainResult.success(
            [[{"id": FULL_ID}], "page-2"] if cursor is None else [[{"id": OTHER_ID}], None]
        )
    getattr(client, family).list = paged
    assert _run(family, [verb, FULL_ID[:12], "--project", "P-1", "--json"], client) == 2
    captured = capsys.readouterr()
    envelope = json.loads(captured.out)
    assert captured.err == ""
    assert set(envelope) == ENVELOPE_KEYS and envelope["ok"] is False
    assert envelope["error"]["code"] == "ambiguous_id"
    assert envelope["error"]["details"]["matches"] == sorted([FULL_ID, OTHER_ID])
    assert [name for name, _ in client.calls] == [f"{family}.list"] * 2


@pytest.mark.parametrize("family,verb", PER_ID_VERBS + [("tasks", "list"), ("runs", "list")])
@pytest.mark.parametrize("problem,expected_calls,error_code", [
    ("later-error", 2, "unavailable"),
    ("cap", 60, "incomplete_list"),
    ("repeated", 2, "incomplete_list"),
    ("cycle", 3, "incomplete_list"),
])
def test_incomplete_traversal_never_dispatches_or_prints_success(
    family, verb, problem, expected_calls, error_code, monkeypatch, capsys,
) -> None:
    client = _Client()
    _follow_spy(client, monkeypatch)
    upstream = DomainResult.failure(ErrorObject("unavailable", "later page failed", {"page": 2}))
    def paged(project_id, cursor=None, **_kwargs):
        client.calls.append((f"{family}.list", {"cursor": cursor}))
        n = len(client.calls)
        if problem == "later-error" and n == 2:
            return upstream
        nxt = "again" if problem == "repeated" else ("a" if n % 2 else "b") if problem == "cycle" else f"page-{n + 1}"
        return DomainResult.success([[{"id": FULL_ID}], nxt])
    getattr(client, family).list = paged
    args = [verb, "--project", "P-1", "--json"]
    if verb != "list":
        args.append(FULL_ID[:12])
    assert _run(family, args, client) == 1
    captured = capsys.readouterr()
    envelope = json.loads(captured.out)
    assert captured.err == ""
    assert set(envelope) == ENVELOPE_KEYS and envelope["ok"] is False
    assert envelope["data"] is None
    assert envelope["error"]["code"] == error_code
    if problem == "later-error":
        assert envelope == upstream.as_dict()
    assert [name for name, _ in client.calls] == [f"{family}.list"] * expected_calls


def test_all_pages_at_cap_succeeds_only_if_last_page_is_terminal() -> None:
    from astrid.core.cli.list_narrow import all_pages

    calls = []
    def paged(project, cursor=None, **_kwargs):
        calls.append(cursor)
        return DomainResult.success([[{"id": str(len(calls))}], "last" if cursor is None else None])
    result = all_pages(paged, "P-1", max_pages=2)
    assert result.ok and result.data == [{"id": "1"}, {"id": "2"}]
    assert calls == [None, "last"]


def test_output_labels_ignore_logs() -> None:
    from astrid.core.cli.domain_tasks import _completed_line
    from astrid.core.cli.list_narrow import primary_output_name

    log = {"role": "log", "filename": "job.stderr"}
    product = {"role": "result", "filename": "report.txt"}
    assert primary_output_name({"result": {"outputs": [log, product]}}) == "report.txt"
    assert primary_output_name({"result": {"outputs": [log]}}) is None
    assert _completed_line({"outputs": [{"resource": log}, {"resource": product}]}) == (
        "completed task=- run=- output=report.txt"
    )
    assert _completed_line({"outputs": [{"resource": log}]}) == "completed task=- run=- output=-"
