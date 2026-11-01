"""CPU-only contract tests for one prepared worker and one ordinary Runtime task."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.packs.runpod.prepared_task import (
    PreparedRunPodTaskClient,
    PreparedWorkerError,
    _ProductionPreparedTaskRuntime,
    _task_credential_reference,
    release_prepared_worker,
)


def _task(*, state: str = "queued", **changes: object) -> dict[str, object]:
    task: dict[str, object] = {
        "task_id": "task-1",
        "run_id": "run-1",
        "capability_id": "vibecomfy.run",
        "state": state,
        "execution_binding": {"target": {"kind": "runpod", "pod_id": "pod-1"}},
    }
    task.update(changes)
    return task


class _Runtime:
    def __init__(self, events: list[object]) -> None:
        self.events = events

    def ensure_for_task(self, task: dict[str, object]) -> None:
        self.events.append(("activate", deepcopy(task)))

    def retire_after_task(self, task: dict[str, object]) -> None:
        self.events.append(("retire", deepcopy(task)))


class _Client:
    def __init__(self, reads: list[dict[str, object]], *, fail_resume: bool = False) -> None:
        self.events: list[object] = []
        self.reads = iter(reads)
        self.fail_resume = fail_resume
        self.tasks = SimpleNamespace(show=self._show)
        self.first = SimpleNamespace(ok=True, kernel_task_id="task-1", kernel_run_id="run-1")
        self.finished = SimpleNamespace(ok=True, kernel_task_id="task-1", kernel_run_id="run-1")

    def _show(self, task_id: str) -> SimpleNamespace:
        self.events.append(("show", task_id))
        return SimpleNamespace(data=next(self.reads))

    def invoke_result(self, capability_id: str, *, kind: str, **kwargs: object) -> object:
        self.events.append(("invoke", capability_id, kind, deepcopy(kwargs)))
        if kwargs.get("wait") is False:
            return self.first
        if self.fail_resume:
            raise RuntimeError("ordinary SDK wait/readback failed")
        return self.finished


def _kwargs() -> dict[str, object]:
    return {
        "wait": True,
        "recovery_path": "/tmp/ordinary-task-receipt.json",
        "workflow_path": "/tmp/workflow.json",
        "workflow_bundle_path": "/tmp/bundle",
        "workflow_inputs": {"seed": 17},
        "execution_request": {
            "remote_activation_required": True,
            "target": {"kind": "runpod", "pod_id": "pod-1"},
        },
    }


def test_admits_one_ordinary_task_then_activates_exact_task_and_resumes_receipt() -> None:
    client = _Client([_task(), _task(state="succeeded")])
    runtime = _Runtime(client.events)
    wrapper = PreparedRunPodTaskClient(client, runtime)
    original = _kwargs()

    assert wrapper.invoke_result("vibecomfy.run", kind="executor", **original) is client.finished

    first, read, activation, second, settled, retired = client.events
    assert (first[0], first[1:3], first[3]["wait"]) == (
        "invoke", ("vibecomfy.run", "executor"), False,
    )
    assert read == ("show", "task-1")
    assert activation == ("activate", _task())
    assert second[0:3] == ("invoke", "vibecomfy.run", "executor")
    assert second[3]["wait"] is True
    assert second[3]["resume"] is True
    assert settled == ("show", "task-1")
    assert retired == ("retire", _task(state="succeeded"))
    for key, value in original.items():
        if key != "wait":
            assert first[3][key] == second[3][key] == value
    assert "resume" not in first[3]
    assert original["wait"] is True


def test_terminal_admission_receipt_skips_activation_but_retires_after_readback() -> None:
    client = _Client([_task(state="succeeded"), _task(state="succeeded")])
    wrapper = PreparedRunPodTaskClient(client, _Runtime(client.events))

    assert wrapper.invoke_result("vibecomfy.run", kind="executor", **_kwargs()) is client.finished
    assert [event[0] for event in client.events] == [
        "invoke", "show", "invoke", "show", "retire",
    ]


@pytest.mark.parametrize("capability_id,changes", [
    ("pack.render", {}),
    ("vibecomfy.run", {"execution_request": {"remote_activation_required": False}}),
    ("vibecomfy.run", {"wait": False}),
])
def test_non_prepared_or_non_waiting_calls_pass_through(
    capability_id: str, changes: dict[str, object],
) -> None:
    client = _Client([])
    wrapper = PreparedRunPodTaskClient(client, _Runtime(client.events))
    kwargs = {**_kwargs(), **changes}

    result = wrapper.invoke_result(capability_id, kind="executor", **kwargs)

    assert result is (client.first if kwargs["wait"] is False else client.finished)
    assert client.events == [("invoke", capability_id, "executor", kwargs)]


@pytest.mark.parametrize("change", [
    {"task_id": "foreign-task"},
    {"run_id": "foreign-run"},
    {"capability_id": "foreign.capability"},
    {"execution_binding": None},
])
def test_foreign_or_unbound_runtime_task_fails_before_activation(change: dict[str, object]) -> None:
    client = _Client([_task(**change)])
    wrapper = PreparedRunPodTaskClient(client, _Runtime(client.events))

    with pytest.raises(PreparedWorkerError):
        wrapper.invoke_result("vibecomfy.run", kind="executor", **_kwargs())

    assert [event[0] for event in client.events] == ["invoke", "show"]


def test_settled_task_is_retired_when_ordinary_resume_readback_raises() -> None:
    client = _Client([_task(), _task(state="failed")], fail_resume=True)
    wrapper = PreparedRunPodTaskClient(client, _Runtime(client.events))

    with pytest.raises(RuntimeError, match="ordinary SDK wait/readback failed"):
        wrapper.invoke_result("vibecomfy.run", kind="executor", **_kwargs())

    assert [event[0] for event in client.events] == [
        "invoke", "show", "activate", "invoke", "show", "retire",
    ]
    assert client.events[-1] == ("retire", _task(state="failed"))


def test_task_credential_reference_is_private_per_runtime_task() -> None:
    base = "/workspace/runtime/credentials/executor.token"
    first = _task_credential_reference(base, "task-1")
    second = _task_credential_reference("file:" + base, "task-2")
    assert first.startswith("/workspace/runtime/credentials/tasks/")
    assert first.endswith("/executor.token")
    assert second.startswith("file:/workspace/runtime/credentials/tasks/")
    assert first != second


def test_selection_converts_file_backed_local_staging_paths_to_paths(tmp_path: Path) -> None:
    from astrid.packs.runpod.prepared_task import load_prepared_worker_selection

    claim = tmp_path / "claim.json"
    claim.write_text(json.dumps({
        "state": "claimed", "pod_id": "pod-1", "provider_account_ref": "account-1",
        "network_volume_id": "volume-1",
    }), encoding="utf-8")
    release = tmp_path / "release.json"
    release.write_text("{}\n", encoding="utf-8")
    astrid_source = tmp_path / "astrid-source"
    vibecomfy_source = tmp_path / "vibecomfy-source"
    astrid_source.mkdir()
    vibecomfy_source.mkdir()
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps({"schema": "astrid.runpod.deployment-profile.v1"}), encoding="utf-8")
    staging = tmp_path / "staging.json"
    staging.write_text(json.dumps({
        "release_manifest_path": str(release),
        "astrid_source": str(astrid_source),
        "vibecomfy_source": str(vibecomfy_source),
    }), encoding="utf-8")
    selection_path = tmp_path / "selection.json"
    selection_path.write_text(json.dumps({
        "schema": "astrid.runpod.prepared-worker.v1",
        "claim_handle_path": str(claim),
        "deployment_profile_path": str(profile),
        "staging_inputs_path": str(staging),
        "journal_dir": str(tmp_path / "journal"),
    }), encoding="utf-8")

    selection = load_prepared_worker_selection(selection_path)

    assert selection.staging_inputs["release_manifest_path"] == release
    assert selection.staging_inputs["astrid_source"] == astrid_source
    assert selection.staging_inputs["vibecomfy_source"] == vibecomfy_source
    assert all(isinstance(selection.staging_inputs[key], Path) for key in (
        "release_manifest_path", "astrid_source", "vibecomfy_source",
    ))


def test_selection_rejects_aliased_or_missing_local_staging_paths(tmp_path: Path) -> None:
    from astrid.packs.runpod.prepared_task import load_prepared_worker_selection

    claim = tmp_path / "claim.json"
    claim.write_text(json.dumps({
        "state": "claimed", "pod_id": "pod-1", "provider_account_ref": "account-1",
        "network_volume_id": "volume-1",
    }), encoding="utf-8")
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps({"schema": "astrid.runpod.deployment-profile.v1"}), encoding="utf-8")
    source = tmp_path / "source"
    source.mkdir()
    release = tmp_path / "release.json"
    release.write_text("{}\n", encoding="utf-8")
    staging = tmp_path / "staging.json"
    selection_path = tmp_path / "selection.json"
    selection_path.write_text(json.dumps({
        "schema": "astrid.runpod.prepared-worker.v1",
        "claim_handle_path": str(claim),
        "deployment_profile_path": str(profile),
        "staging_inputs_path": str(staging),
        "journal_dir": str(tmp_path / "journal"),
    }), encoding="utf-8")
    staging.write_text(json.dumps({
        "release_manifest_path": str(release.parent) + "/./" + release.name,
        "astrid_source": str(source),
        "vibecomfy_source": str(tmp_path / "missing-source"),
    }), encoding="utf-8")

    with pytest.raises(PreparedWorkerError, match="canonical absolute local path"):
        load_prepared_worker_selection(selection_path)


def test_completed_explicit_release_receipt_resumes_without_provider_access(tmp_path: Path) -> None:
    claim_path = tmp_path / "claim.json"
    selection_path = tmp_path / "selection.json"
    binding = {
        "account_ref": "account-1", "pod_id": "pod-1", "runtime_instance_id": "runtime-1",
        "runtime_epoch": 2, "runtime_session_id": "session-1", "executor_id": "astrid-pack-host",
    }
    process = {
        "binding": binding, "incarnation": "incarnation-1", "pid": 4, "birth_id": "birth-1",
    }
    qualification = {
        "task_id": "task-1", "run_id": "run-1", "activation_id": "activation-1",
        "runtime_session_id": "session-1", "runtime_epoch": 2,
        "executor_incarnation": "incarnation-1",
    }
    fence = {
        "retired": True, "operation_id": "operation-1", "provider": "runpod",
        "account_ref": "account-1", "pod_id": "pod-1", "incarnation": "incarnation-1",
        "release_fence_digest": "sha256:" + "a" * 64,
    }
    quiescence = {
        "schema": "astrid.runpod.worker-quiescence.v1", "operation_id": "operation-1",
        "pod_id": "pod-1", "provider_account_ref": "account-1", "task_id": "task-1",
        "activation_id": "activation-1", "runtime_instance_id": "runtime-1",
        "runtime_session_id": "session-1", "runtime_epoch": 2,
        "drain": {"state": "complete", "task_id": "task-1", "activation_id": "activation-1"},
        "runtime_drain": {"state": "drained", "activation_id": "activation-1"},
        "release_fence": fence,
        "process_stop": {"stopped": True, "pid": 4, "birth_id": "birth-1",
                         "incarnation": "incarnation-1"},
        "credential_remove": {"removed": True},
    }
    released = {
        "operation_id": "operation-1", "pod_id": "pod-1", "provider_account_ref": "account-1",
        "network_volume_id": "volume-1",
        "state": "cleanup_complete",
        "managed_worker": {
            "activation_qualification": qualification,
            "process_handle": process,
        },
        "release_quiescence": quiescence,
        "cleanup": {"pod_id": "pod-1", "backup_volume_id": "volume-1",
                    "backup_volume_preserved": True, "status": "terminated"},
        "cleanup_status": "complete", "reconciliation_required": False,
    }
    claim_path.write_text(json.dumps(released), encoding="utf-8")
    selection_path.write_text(json.dumps({
        "schema": "astrid.runpod.prepared-worker.v1",
        "claim_handle_path": str(claim_path),
        "deployment_profile_path": str(tmp_path / "profile.json"),
        "staging_inputs_path": str(tmp_path / "staging.json"),
        "journal_dir": str(tmp_path / "journal"),
    }), encoding="utf-8")

    class NoProviderClient:
        def __getattr__(self, name: str) -> object:
            pytest.fail(f"completed release recovery unexpectedly accessed {name}")

    assert release_prepared_worker(
        NoProviderClient(), selection_path, "task-1", resume=True,
    ) == released
    with pytest.raises(PreparedWorkerError, match="completed RunPod release"):
        release_prepared_worker(NoProviderClient(), selection_path, "task-1")


def _production_runtime(tmp_path: Path, managed: dict[str, object]):
    claim_path = tmp_path / "claim.json"
    claim_path.write_text(json.dumps({"state": "claimed", "managed_worker": managed}), encoding="utf-8")
    events: list[object] = []

    class Preparer:
        def abort_parked(self, reference: object) -> dict[str, object]:
            events.append("abort-parked")
            return {"stopped": True}

    class Launcher:
        activation_state = "inactive"

        def park(self, reference: object, *, target: object) -> str:
            events.append("park")
            return "parked"

        def activate(self, task: object, reference: object, parked: object) -> dict[str, object]:
            events.append("activate")
            return {"task_id": "task-1", "run_id": "run-1", "activation_id": "new"}

        def restore_prepared_activation(self, task: object, reference: object, qualification: object) -> str:
            events.append("restore-activation")
            return "restored-parked"

        def reconcile_activation(self, task: object, reference: object, parked: object, qualification: object):
            events.append("reconcile-activation")
            return dict(qualification)

    runtime = object.__new__(_ProductionPreparedTaskRuntime)
    runtime.selection = SimpleNamespace(claim_handle_path=claim_path)
    runtime._reference = runtime._preparer = runtime._runtime_owner = runtime._launcher = None
    runtime._parked = runtime._qualification = runtime._local_generation = None
    preparer, launcher = Preparer(), Launcher()
    runtime._components = lambda _task: (  # type: ignore[method-assign]
        SimpleNamespace(effective_target={"kind": "runpod", "pod_id": "pod-1"}),
        preparer, object(), launcher, object(),
    )
    runtime._restore_local_worker = lambda: events.append("restore-local")  # type: ignore[method-assign]
    runtime._relinquish_local_worker = lambda *_args: events.append("relinquish-local")  # type: ignore[method-assign]
    runtime._retire_uncommitted_generation = lambda *_args: events.append("retire-uncommitted")  # type: ignore[method-assign]
    return runtime, events


@pytest.mark.parametrize("state", ["parked_process", "parked"])
def test_restart_replaces_only_exact_unqualified_host_and_restores_local_worker(tmp_path: Path, state: str):
    runtime, events = _production_runtime(tmp_path, {"state": state})
    runtime.ensure_for_task(_task())
    assert events == ["abort-parked", "restore-local", "park", "relinquish-local", "activate"]


def test_restart_retires_pre_readiness_generation_before_replacement(tmp_path: Path):
    managed = {
        "state": "enabled",
        "activation_qualification": {"task_id": "task-1", "run_id": "run-1", "activation_id": "old"},
    }
    runtime, events = _production_runtime(tmp_path, managed)
    runtime.ensure_for_task(_task())
    assert events == ["retire-uncommitted", "park", "relinquish-local", "activate"]


def test_restart_reconciles_ready_generation_without_rotating_or_replacing_it(tmp_path: Path):
    managed = {
        "state": "ready",
        "activation_qualification": {"task_id": "task-1", "run_id": "run-1", "activation_id": "old"},
    }
    runtime, events = _production_runtime(tmp_path, managed)
    runtime.ensure_for_task(_task())
    assert events == ["restore-activation", "reconcile-activation"]


def test_local_worker_is_not_restarted_when_runtime_generation_is_unobservable():
    runtime = object.__new__(_ProductionPreparedTaskRuntime)
    runtime._local_generation = {"profile_id": "local-profile", "workspace_uuid": "workspace-1"}

    class UnavailableRuntime:
        def get_local_worker_generation(self) -> object:
            raise ConnectionError("Runtime unavailable")

        def start_local_worker(self, *_args: object) -> None:
            pytest.fail("must not start a second worker while ownership is unknown")

    runtime.workspace = UnavailableRuntime()
    with pytest.raises(PreparedWorkerError, match="could not be observed"):
        runtime._restore_local_worker()
