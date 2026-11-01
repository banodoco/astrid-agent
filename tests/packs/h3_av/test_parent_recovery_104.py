from pathlib import Path

import pytest

from astrid.core.contracts.errors import AstridError
from astrid.packs.h3_av.actions.transform import run
from astrid.packs.h3_av.actions.transform.operation import OperationJournal
from astrid.sdk.results import InvocationResult


def test_same_parent_recovery_reuses_stage_key_and_rejects_changed_inputs(tmp_path, monkeypatch):
    from astrid.sdk import invocation
    calls = []
    def submit(capability_id, **kwargs):
        calls.append(kwargs)
        return InvocationResult(capability_id=capability_id, capability_type="executor", native_kind="action", ok=True,
                                kernel_run_id="run", kernel_task_id="task", kernel_attempt_id="attempt")
    monkeypatch.setattr(invocation, "invoke_result", submit)
    client = run.ManagedParentClient({"stages": [{"name": "prepare", "inputs": []}]}, tmp_path)
    journal = OperationJournal(tmp_path / "journal.json", request_digest="request")
    args = dict(client=client, capability_id="h3_av.prepare", inputs={}, out=tmp_path / "out", project="project",
                saved_result=tmp_path / "saved.json", journal=journal, phase="prepare")
    first = run._invoke_stage(**args, resume=False)
    second = run._invoke_stage(**args, resume=True)
    assert first.kernel_task_id == second.kernel_task_id == "task"
    assert [call["child_key"] for call in calls] == ["prepare", "prepare"]
    assert [call["stage"] for call in calls] == ["prepare", "prepare"]
    with pytest.raises(AstridError, match="inputs changed"):
        run._invoke_stage(**{**args, "inputs": {"changed": True}}, resume=True)
    assert len(calls) == 2


@pytest.mark.parametrize(
    "status_boundary, expected_code",
    [("read_exception", "task_status_unavailable"), ("failed_child", "task_failed")],
)
def test_prepare_status_boundary_preserves_uncertainty_and_recovery_key(
    tmp_path, monkeypatch, status_boundary, expected_code,
):
    """Synthetic status boundaries distinguish unreadable state from child failure.

    This exercises the existing parent and SDK path, not a replay or explanation
    of session 60468. Admission/status alone are boundary doubles; no Runtime,
    host, transport, preparation implementation, or media is executed.
    """
    from astrid.core.execution.executor.actions import action_executor_definition
    from astrid.core.execution.executor.registry import ExecutorRegistry
    from astrid.core.pack.loader import load_pack_manifest
    from astrid.sdk import invocation
    from astrid.sdk.discovery import _capability_from_executor
    from astrid.sdk.exceptions import CapabilityInvocationError

    pack = load_pack_manifest(
        Path(__file__).resolve().parents[3] / "astrid/packs/h3_av/pack.yaml"
    )
    definition = action_executor_definition(pack, "prepare", pack.actions["prepare"])
    registry = ExecutorRegistry([definition])
    capability = _capability_from_executor(definition, registry)
    admitted = {"run_id": "prepare-run", "task_id": "prepare-task",
                "attempt_id": None, "state": "queued"}
    requests, status_calls, sdk_results = [], [], []

    class StatusBoundaryDouble:
        def submit(self, request):
            requests.append(request)
            return dict(admitted)

        def status(self, child_key, task_id, *, timeout_seconds):
            status_calls.append((child_key, task_id))
            if status_boundary == "read_exception":
                raise CapabilityInvocationError("injected status read boundary failure")
            return {**admitted, "attempt_id": "prepare-attempt", "state": "failed", "result": {
                "error": {"message": "injected observed child failure"}}}

        def outputs(self, *args, **kwargs):
            pytest.fail("an unreadable or failed prepare must not read successful outputs")

        def close(self):
            pytest.fail("this diagnostic did not inject a foreign task resource")

    bridge = StatusBoundaryDouble()

    def invoke_result(capability_id, **kwargs):
        assert capability_id == "h3_av.prepare"
        assert kwargs["kind"] == "executor" and kwargs["read_managed_outputs"] is True
        result = invocation._invoke_bridge_child(
            capability, registries=(registry, None, None), bridge=bridge,
            **{name: kwargs[name] for name in (
                "inputs", "child_key", "wait", "timeout_seconds", "poll_seconds",
                "stage", "input_refs",
            )},
        )
        sdk_results.append(result)
        return result

    monkeypatch.setattr(invocation, "invoke_result", invoke_result)
    inputs = {
        "request": {"object_id": "sha256:" + "a" * 64, "filename": "request.json"},
        "input_bundle": {"object_id": "sha256:" + "b" * 64, "filename": "bundle.zip"},
    }
    plan = {"stages": [{"name": "prepare", "inputs": [
        {"name": name, "root_object_id": value["object_id"]}
        for name, value in inputs.items()
    ]}]}
    journal_path, saved_result = tmp_path / "journal.json", tmp_path / "saved.json"

    for resume in (False, True):
        client = run.ManagedParentClient(plan, tmp_path)
        journal = OperationJournal(journal_path, request_digest="request")
        with pytest.raises(AstridError, match=expected_code) as caught:
            run._invoke_stage(
                client=client, capability_id="h3_av.prepare", inputs=inputs,
                out=tmp_path / "out", project="project", saved_result=saved_result,
                journal=journal, phase="prepare", resume=resume,
            )
        assert caught.value.h3_stage == "prepare"
        assert caught.value.h3_child_correlation == {
            "run_id": admitted["run_id"], "task_id": admitted["task_id"],
            "attempt_id": "prepare-attempt" if status_boundary == "failed_child" else "unavailable",
        }
        assert client.results == {}
        assert not saved_result.exists()
        assert [(event["phase"], event["status"]) for event in journal.events] == [
            ("prepare", "started"), ("prepare", "uncertain"),
        ]
        if not resume:
            assert caught.value.h3_journal_status == "succeeded"

    assert requests[0] == requests[1]
    assert [request["child_key"] for request in requests] == ["prepare", "prepare"]
    assert [request["stage"] for request in requests] == ["prepare", "prepare"]
    assert all(request["child"]["capability_id"] == "h3_av.prepare" for request in requests)
    assert requests[0]["input_refs"] == plan["stages"][0]["inputs"]
    assert status_calls == [("prepare", "prepare-task"), ("prepare", "prepare-task")]
    for result in sdk_results:
        assert result.ok is False and result.error["code"] == expected_code
        if status_boundary == "read_exception":
            assert result.error["details"] == {"error_type": "CapabilityInvocationError"}
            assert "task" not in result.raw_result  # No child state was observed.
        else:
            assert result.raw_result["task"]["state"] == "failed"


def test_selected_raw_uses_declared_tuple_with_same_port_auxiliary_audio(tmp_path):
    selector = {"output_port": "shared", "ordinal": 0, "variant_key": "original"}
    client = run.ManagedParentClient({"publications": {"raw": {"effect": {"payload": {
        "groups": [{"group_key": "main", "selectors": [selector]}]}}}}}, tmp_path)
    video = {"association_id": "video", "output_port": "shared", "ordinal": 0,
             "group_key": "main", "variant_key": "original"}
    audio = {"association_id": "audio", "output_port": "shared", "ordinal": 1,
             "group_key": "aux", "variant_key": "sound"}
    assert client.selected_raw([audio, video]) == [video]
    with pytest.raises(AstridError, match="exactly one"):
        client.selected_raw([audio])
    with pytest.raises(AstridError, match="exactly one"):
        client.selected_raw([video, {**video, "association_id": "other"}])
