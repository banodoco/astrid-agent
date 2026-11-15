from __future__ import annotations

from types import SimpleNamespace

import pytest

from astrid.sdk import invocation, observe_task_invocation


CAPABILITY = "fixture.observe"
TASK_ID = "task-1"
RUN_ID = "run-1"


def _task(state: str, *, attempt_id: str | None = None, worker_state: str = "gone"):
    value = {
        "task_id": TASK_ID,
        "run_id": RUN_ID,
        "state": state,
        "worker_state": worker_state,
        "result": {"outputs": [{"name": "video", "digest": "sha256:abc", "size": 3}]},
    }
    if attempt_id is not None:
        value["attempt_id"] = attempt_id
    return value


def _result(value, *, ok: bool = True, error=None):
    return SimpleNamespace(ok=ok, data=value, error=error)


def _metadata(*, read_managed_outputs: bool = False):
    return {
        "capability_id": CAPABILITY,
        "capability_type": "executor",
        "native_kind": "executor",
        "executor_version": "sha256:version",
        "read_managed_outputs": read_managed_outputs,
        "out": None,
        "project": "demo",
        "authority_context": None,
    }


class _Receipt:
    def __init__(self, task, *, read_managed_outputs: bool = False):
        self.data = {
            "metadata": _metadata(read_managed_outputs=read_managed_outputs),
            "locator": {"task_id": TASK_ID, "run_id": RUN_ID},
        }
        self.task = task

    def recover_task(self):
        return _result(self.task)


def _observe(client, task, *, wait: bool, **kwargs):
    return invocation._observe_task_invocation(
        client,
        capability_id=CAPABILITY,
        capability_type="executor",
        native_kind="executor",
        task_id=TASK_ID,
        run_id=RUN_ID,
        initial_raw_result={"ok": True, "task": task, "run_id": RUN_ID},
        executor_version="sha256:version",
        wait=wait,
        timeout_seconds=1,
        poll_seconds=0.001,
        **kwargs,
    )


def test_fresh_wait_and_receipt_resume_share_result_shape_without_attempt_id():
    task = _task("succeeded")
    output_rows = [{"name": "video", "digest": "sha256:abc", "size": 3}]
    client = SimpleNamespace(
        tasks=SimpleNamespace(
            show=lambda _task_id: _result(task),
            list_managed_outputs=lambda _task_id: _result(output_rows),
        )
    )

    fresh = _observe(client, task, wait=True, read_managed_outputs=True)
    resumed = invocation._resume_receipt_invocation(
        _Receipt(task, read_managed_outputs=True),
        client=client,
        wait=True,
        timeout_seconds=1,
        poll_seconds=0.001,
    )

    assert fresh.ok and resumed.ok
    assert fresh.raw_result == resumed.raw_result
    assert fresh.outputs == resumed.outputs
    assert fresh.kernel_task_id == resumed.kernel_task_id == TASK_ID
    assert fresh.kernel_run_id == resumed.kernel_run_id == RUN_ID
    assert fresh.kernel_attempt_id == resumed.kernel_attempt_id == ""
    assert fresh.raw_result["managed_outputs"] == output_rows


def test_waited_composed_capture_materializes_authorized_bundle(monkeypatch):
    client = object()
    materialization = {}
    authority = {
        "mode": "composed_capture",
        "capture_snapshot": {"project_slug": "demo"},
    }
    monkeypatch.setattr(
        invocation,
        "_wait_for_kernel_task",
        lambda *_args, **_kwargs: ({"ok": True, "outputs": {"artifacts": []}}, True, "attempt-1"),
    )

    def materialize(raw_result, observed_client, *, project):
        materialization.update(raw_result=raw_result, client=observed_client, project=project)
        return "/cache/demo/composed-capture/manifest.json"

    monkeypatch.setattr(invocation, "_materialize_filmstrip_outputs", materialize)
    result = invocation._observe_task_invocation(
        client,
        capability_id="rendering.timeline_visualize",
        capability_type="executor",
        native_kind="executor",
        task_id=TASK_ID,
        run_id=RUN_ID,
        initial_raw_result={"ok": True},
        wait=True,
        authority_context=authority,
        project="demo",
    )

    assert result.ok
    assert result.manifest_path == "/cache/demo/composed-capture/manifest.json"
    assert materialization == {
        "raw_result": result.raw_result,
        "client": client,
        "project": "demo",
    }


def test_queued_timeout_keeps_locator_and_resume_observes_same_task(monkeypatch):
    queued = _task("queued")
    succeeded = _task("succeeded", attempt_id="attempt-1")
    observed = iter((queued, succeeded))
    client = SimpleNamespace(tasks=SimpleNamespace(show=lambda _task_id: _result(next(observed))))
    with monkeypatch.context() as scoped:
        scoped.setattr(invocation.time, "monotonic", iter((0.0, 2.0)).__next__)
        scoped.setattr(invocation.time, "sleep", lambda _seconds: None)
        timed_out = _observe(client, queued, wait=True)
    assert not timed_out.ok
    assert timed_out.error["code"] == "task_wait_timeout"
    assert timed_out.kernel_task_id == TASK_ID
    assert timed_out.kernel_run_id == RUN_ID
    assert timed_out.kernel_attempt_id == ""

    resumed = invocation._resume_receipt_invocation(
        _Receipt(queued), client=client, wait=True, timeout_seconds=1, poll_seconds=0.001
    )
    assert resumed.ok
    assert resumed.kernel_task_id == TASK_ID
    assert resumed.kernel_run_id == RUN_ID
    assert resumed.kernel_attempt_id == "attempt-1"


def test_transient_status_read_loss_returns_locator_for_later_observation():
    task = _task("succeeded")
    calls = 0

    def show(_task_id):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise TimeoutError("Runtime response interrupted")
        return _result(task)

    client = SimpleNamespace(tasks=SimpleNamespace(show=show))
    first = _observe(client, task, wait=True)
    assert not first.ok
    assert first.error["code"] == "task_status_unavailable"
    assert first.kernel_task_id == TASK_ID and first.kernel_run_id == RUN_ID

    resumed = invocation._resume_receipt_invocation(
        _Receipt(task), client=client, wait=True, timeout_seconds=1, poll_seconds=0.001
    )
    assert resumed.ok
    assert resumed.kernel_task_id == TASK_ID and resumed.kernel_run_id == RUN_ID


def test_managed_output_readback_can_be_repeated_after_transient_failure_and_worker_exit():
    task = _task("succeeded", worker_state="gone")
    output_rows = [{"output_id": "output-1", "name": "video", "digest": "sha256:abc", "size": 3}]
    reads = 0

    def read_outputs(_task_id):
        nonlocal reads
        reads += 1
        if reads == 1:
            return _result(None, ok=False, error=SimpleNamespace(as_dict=lambda: {"code": "transport_error"}))
        return _result(output_rows)

    client = SimpleNamespace(
        tasks=SimpleNamespace(show=lambda _task_id: _result(task), list_managed_outputs=read_outputs)
    )
    first = _observe(client, task, wait=True, read_managed_outputs=True)
    assert not first.ok
    assert first.error["code"] == "managed_output_readback_unavailable"
    assert first.kernel_task_id == TASK_ID and first.kernel_run_id == RUN_ID

    resumed = invocation._resume_receipt_invocation(
        _Receipt(task, read_managed_outputs=True),
        client=client,
        wait=True,
        timeout_seconds=1,
        poll_seconds=0.001,
    )
    assert resumed.ok
    assert reads == 2
    assert resumed.outputs["managed_outputs"] == output_rows
    assert resumed.kernel_attempt_id == ""


@pytest.mark.parametrize("state", ["failed", "cancelled"])
def test_terminal_failure_and_cancellation_are_returned_without_retry(state):
    task = _task(state)
    client = SimpleNamespace(tasks=SimpleNamespace(show=lambda _task_id: _result(task)))

    result = _observe(client, task, wait=True)

    assert not result.ok
    assert result.error["code"] == f"task_{state}"
    assert result.raw_result["state"] == state
    assert result.kernel_task_id == TASK_ID and result.kernel_run_id == RUN_ID


def test_legacy_locator_can_read_task_without_saved_result_dto():
    task = _task("succeeded", attempt_id="attempt-1")
    client = SimpleNamespace(tasks=SimpleNamespace(show=lambda _task_id: _result(task)))

    result = invocation._observe_task_invocation(
        client,
        capability_id=CAPABILITY,
        capability_type="executor",
        native_kind="executor",
        task_id=TASK_ID,
        run_id=RUN_ID,
    )

    assert result.ok
    assert result.raw_result["task"] == task
    assert result.kernel_task_id == TASK_ID
    assert result.kernel_run_id == RUN_ID
    assert result.kernel_attempt_id == "attempt-1"


def test_public_locator_observer_reads_settled_outputs_without_admission():
    task = _task("succeeded", attempt_id="attempt-1")
    output_rows = [{"output_id": "output-1", "name": "video"}]
    calls = []

    class Tasks:
        def show(self, task_id):
            calls.append(("show", task_id))
            return _result(task)

        def list_managed_outputs(self, task_id):
            calls.append(("outputs", task_id))
            return _result(output_rows)

    class Client:
        tasks = Tasks()

        def invoke_result(self, *_args, **_kwargs):
            raise AssertionError("locator observation must never admit a task")

    result = observe_task_invocation(
        Client(),
        capability_id=CAPABILITY,
        capability_type="executor",
        native_kind="executor",
        task_id=TASK_ID,
        run_id=RUN_ID,
        attempt_id="attempt-1",
        wait=False,
        read_managed_outputs=True,
    )

    assert result.ok
    assert result.raw_result["managed_outputs"] == output_rows
    assert calls == [("show", TASK_ID), ("outputs", TASK_ID)]


def test_public_locator_observer_refuses_a_different_runtime_identity():
    task = {**_task("succeeded", attempt_id="attempt-1"), "run_id": "other-run"}
    reads = []
    client = SimpleNamespace(
        tasks=SimpleNamespace(
            show=lambda _task_id: _result(task),
            list_managed_outputs=lambda task_id: reads.append(task_id) or _result([]),
        ),
        invoke_result=lambda *_args, **_kwargs: pytest.fail("observer must not admit"),
    )

    result = observe_task_invocation(
        client,
        capability_id=CAPABILITY,
        capability_type="executor",
        native_kind="executor",
        task_id=TASK_ID,
        run_id=RUN_ID,
        wait=False,
        read_managed_outputs=True,
    )

    assert not result.ok
    assert result.error["code"] == "task_identity_mismatch"
    assert reads == []


@pytest.mark.parametrize("requested", [False, True])
def test_opt_in_invocation_persists_explicit_managed_output_readback_choice(
    monkeypatch, tmp_path, requested
):
    capability = SimpleNamespace(
        id=CAPABILITY,
        capability_type="executor",
        native_kind="executor",
        inputs=(),
        outputs=(),
        definition={"metadata": {"project_scope": "optional"}},
    )
    registry = SimpleNamespace(get=lambda _capability_id: {"id": CAPABILITY})
    sdk = SimpleNamespace(
        _load_registries=lambda **_kwargs: (registry, None, None),
        get_capability=lambda *_args, **_kwargs: capability,
    )
    monkeypatch.setattr(invocation, "_sdk_module", lambda: sdk)
    monkeypatch.setattr("astrid.core.foundation.hash.executor_definition_digest", lambda _definition: "sha256:version")

    class Receipt:
        def __init__(self, *_args, **_kwargs):
            self.data = {}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    monkeypatch.setattr("astrid.sdk.recovery.AdmissionReceipt", Receipt)
    captured = {}

    def kernel(_capability, **kwargs):
        captured.update(kwargs["_recovery_metadata"])
        return RUN_ID, TASK_ID, "", None, {
            "ok": True,
            "run_id": RUN_ID,
            "kernel_run_id": RUN_ID,
            "kernel_task_id": TASK_ID,
            "task": _task("succeeded"),
        }, True, None

    monkeypatch.setattr(invocation, "_kernel_invoke", kernel)
    output_rows = [{"output_id": "output-1", "name": "video"}]
    reads = []

    def read_outputs(task_id):
        reads.append(task_id)
        return _result(output_rows)

    client = SimpleNamespace(
        tasks=SimpleNamespace(
            show=lambda _task_id: _result(_task("succeeded")),
            list_managed_outputs=read_outputs,
        )
    )
    result = invocation.invoke(
        CAPABILITY,
        kind="executor",
        project="demo",
        client=client,
        wait=True,
        recovery_path=tmp_path / "receipt.json",
        read_managed_outputs=requested,
    )

    assert result.ok
    assert captured["read_managed_outputs"] is requested
    assert reads == ([TASK_ID] if requested else [])
    assert result.outputs.get("managed_outputs") == (output_rows if requested else None)


def test_wait_reports_state_and_waiting_reason_changes_to_stderr(monkeypatch, capsys):
    tasks = iter([
        {**_task("queued"), "waiting_reason": "insufficient_storage", "realm_root": "/realm"},
        {**_task("queued"), "waiting_reason": "waiting_for_worker", "holder_task_id": "other-task"},
        _task("running"),
        _task("succeeded"),
    ])
    client = SimpleNamespace(tasks=SimpleNamespace(show=lambda _: _result(next(tasks))))
    monkeypatch.setattr(invocation.time, "sleep", lambda _: None)
    result, ok, _ = invocation._wait_for_kernel_task_loop(
        client, task_id=TASK_ID, run_id=RUN_ID, timeout_seconds=10, poll_seconds=0.001
    )
    captured = capsys.readouterr()
    assert ok and result["state"] == "completed"
    assert captured.out == ""
    assert "insufficient_storage" in captured.err and "/realm" in captured.err and "1 GiB" in captured.err
    assert "waiting_for_worker" in captured.err and "other-task" in captured.err
    assert "running" in captured.err


def test_wait_heartbeat_for_non_runpod(monkeypatch, capsys):
    now = [0.0]
    tasks = iter([{**_task("queued"), "waiting_reason": "waiting_for_gpu"},
                  {**_task("queued"), "waiting_reason": "waiting_for_gpu"}, _task("succeeded")])
    client = SimpleNamespace(tasks=SimpleNamespace(show=lambda _: _result(next(tasks))))
    monkeypatch.setattr(invocation.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(invocation.time, "sleep", lambda _: now.__setitem__(0, 61.0))
    invocation._wait_for_kernel_task_loop(client, task_id=TASK_ID, run_id=RUN_ID, timeout_seconds=120, poll_seconds=60)
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "60s" in captured.err or "1m" in captured.err
    assert "waiting_for_gpu" in captured.err


def test_runpod_policy_uses_runtime_task_envelope():
    policy = {"kind": "runpod", "wait_for_capacity": True}
    task = {"spec": {"spec": {"capability_id": "vibecomfy.run", "inputs": {"placement_policy": policy}}}}
    assert invocation._runpod_generation_policy(task) == policy
    assert invocation._runpod_generation_policy({"spec": task["spec"]["spec"]}) is None
