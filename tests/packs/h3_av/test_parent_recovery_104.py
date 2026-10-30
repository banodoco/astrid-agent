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
