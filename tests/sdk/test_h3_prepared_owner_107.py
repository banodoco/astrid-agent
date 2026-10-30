"""Focused producer/control and monitor tests; Runtime authentication is tested in Runtime."""
import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.packs.h3_av.actions.transform import run as h3
from astrid.packs.runpod import prepared_task as prepared
from astrid.sdk import invocation
from astrid.sdk.generation import GenerationFacade
from astrid.sdk.exceptions import CapabilityValidationError


def selection(tmp_path):
    (tmp_path / "claim.json").write_text(json.dumps({"state": "claimed", "operation_id": "operation",
        "pod_id": "pod", "provider_account_ref": "account", "network_volume_id": "volume",
        "network_volume_name": "volume-name", "network_volume_size_gb": 10,
        "volume_mount_path": "/workspace", "network_volume_datacenter_id": "EU"}))
    return prepared.PreparedWorkerSelection(
        tmp_path / "claim.json", tmp_path / "profile.json", tmp_path / "staging.json", tmp_path / "journal",
        {"kind": "runpod", "pod_id": "pod", "provider_account_ref": "account", "storage": {"network_volume_id": "volume"}},
        {"schema": "profile"}, {}, None)


@pytest.mark.parametrize("name", ["require_worker_qualification", "resume", "dry_run"])
def test_public_boolean_controls_reject_non_boolean(name):
    with pytest.raises(Exception, match="explicit boolean"):
        h3.public_controls({name: "false"})


def test_controls_require_qualification():
    with pytest.raises(Exception, match="required"):
        h3.public_controls({"require_worker_qualification": True})


def test_execution_and_cleanup_controls_are_loaded_before_admission(tmp_path):
    execution = tmp_path / "execution.json"
    cleanup = tmp_path / "cleanup.json"
    execution.write_text(json.dumps({"schema_version": 1, "target": {"kind": "default"}}))
    cleanup.write_text(json.dumps({"owned_resources": []}))
    controls = h3.public_controls({"execution_request": execution, "cleanup_receipt": cleanup})
    assert controls["run_execution_request"]["target"] == {"kind": "default"}
    assert controls["cleanup_receipt"] == {"owned_resources": []}
    with pytest.raises(Exception, match="derived"):
        h3.public_controls({"execution_request": {"schema_version": 1, "target": {"kind": "default"},
            "inputs": [{"name": "caller", "object_id": "sha256:" + "a" * 64, "filename": "x", "required": True}]}})


def test_public_generate_retains_all_producer_controls(monkeypatch):
    import astrid.sdk as sdk
    from astrid.core.generation import entries
    description = {"id": "h3", "kind": "video", "source": "action", "action": "h3_av.transform",
                   "mode": "transform", "modes": ["transform"], "inputs": [
                       {"name": "request", "type": "file", "required": True},
                       {"name": "asset_map", "type": "file", "required": True}]}
    monkeypatch.setattr(entries, "describe_entry", lambda *a, **kw: description)
    monkeypatch.setattr(sdk, "_load_model_registry", lambda **kw: object())
    calls = []
    client = SimpleNamespace(invoke_result=lambda *a, **kw: calls.append(kw) or "preview")
    values = {"execution_request": "execution.json", "prepared_worker": "prepared.json",
              "cleanup_receipt": "cleanup.json", "require_worker_qualification": True}
    assert GenerationFacade().video(entry="h3", request="request.json", asset_map="map.json", client=client,
        resume=True, dry_run=True, **values) == "preview"
    assert all(calls[0]["inputs"][key] == value for key, value in values.items())
    assert calls[0]["resume"] and calls[0]["dry_run"]
    GenerationFacade().video(entry="h3", request="request.json", asset_map="map.json", client=client,
        worker_qualification="qualification.json")
    assert calls[1]["inputs"]["worker_qualification"] == "qualification.json"


@pytest.mark.parametrize("wait,recovery", [(False, "receipt.json"), (True, None)])
def test_prepared_nonwaiting_or_receiptless_call_fails_before_admission(wait, recovery):
    with pytest.raises(CapabilityValidationError, match="parent recovery receipt"):
        invocation.invoke("h3_av.transform", kind="action", inputs={"prepared_worker": "no-open.json"},
                          wait=wait, recovery_path=recovery)


def monitor_case(tmp_path, monkeypatch):
    selected = selection(tmp_path)
    monkeypatch.setattr(prepared, "load_prepared_worker_selection", lambda path: selected)
    events = []
    runtime = SimpleNamespace(ensure_for_task=lambda task: events.append(("ensure", task["task_id"])),
                              retire_after_task=lambda task: events.append(("retire", task["task_id"])))
    selected = prepared.PreparedWorkerSelection(**{**selected.__dict__, "composition_factory": lambda **kw: runtime})
    monkeypatch.setattr(prepared, "load_prepared_worker_selection", lambda path: selected)
    blobs = {name: name.encode() for name in ("python", "companion", "source")}
    bindings = {name: {"object_id": "sha256:" + hashlib.sha256(data).hexdigest()} for name, data in blobs.items()}
    parent = {"task_id": "parent", "run_id": "parent-run", "project_id": "project", "attempt_id": "attempt",
              "runtime_epoch": 1, "lease_fence": 2, "state": "running", "capability_id": "h3_av.transform",
              "spec": {"spec": {"inputs": {"plan": {"controls": {"prepared_selection_digest": prepared.prepared_selection_digest(selected)}}}}}}
    child = {"task_id": "child", "run_id": "child-run", "project_id": "project", "state": "queued",
             "capability_id": "vibecomfy.run", "execution_binding": {},
             "execution_request": {"remote_activation_required": True, "target": selected.target},
             "spec": {"delegated_stage": "run", "delegated_parent": {"parent_task_id": "parent",
                "parent_attempt_id": "attempt", "parent_fence": 2, "runtime_epoch": 1, "project_id": "project"},
                "spec": {"inputs": {**bindings, "workflow_inputs": "{}"}}}}
    children = [child]
    workspace = SimpleNamespace(list_project_tasks=lambda *a, **kw: (children, None))
    by_id = {binding["object_id"]: blobs[name] for name, binding in bindings.items()}
    client = SimpleNamespace(_remote=SimpleNamespace(_transport=workspace),
        tasks=SimpleNamespace(show=lambda task_id: SimpleNamespace(data=copy.deepcopy(child))),
        media=SimpleNamespace(read_bytes=lambda oid: by_id[oid]))
    return prepared.PreparedH3OwnerMonitor(client, "selected"), parent, child, children, events


def test_owner_monitor_interruption_replay_reuses_child_and_retires_once(tmp_path, monkeypatch):
    monitor, parent, child, children, events = monitor_case(tmp_path, monkeypatch)
    monitor(parent)
    monitor(parent)
    replay = prepared.PreparedH3OwnerMonitor(monitor.client, "selected")
    replay(parent)
    assert monitor.child_id == replay.child_id == "child"
    child["state"] = "succeeded"
    replay(parent)
    replay(parent)
    assert events == [("ensure", "child"), ("ensure", "child"), ("retire", "child")]
    assert parent["state"] == "running"


@pytest.mark.parametrize("field,value", [("parent_attempt_id", "stale"), ("parent_fence", 99),
    ("runtime_epoch", 99), ("project_id", "foreign")])
def test_owner_monitor_rejects_foreign_or_stale_parent_before_activation(tmp_path, monkeypatch, field, value):
    monitor, parent, child, children, events = monitor_case(tmp_path, monkeypatch)
    child["spec"]["delegated_parent"][field] = value
    with pytest.raises(prepared.PreparedWorkerError, match="stale or foreign"):
        monitor(parent)
    assert events == []


def test_owner_monitor_rejects_duplicate_child_and_changed_owner_selection(tmp_path, monkeypatch):
    monitor, parent, child, children, events = monitor_case(tmp_path, monkeypatch)
    children.append(copy.deepcopy(child))
    with pytest.raises(prepared.PreparedWorkerError, match="ambiguous"):
        monitor(parent)
    children.pop()
    prepared._task_inputs(parent)["plan"]["controls"]["prepared_selection_digest"] = "changed"
    with pytest.raises(prepared.PreparedWorkerError, match="owner selection"):
        monitor(parent)
    assert events == []


def test_prepared_child_does_not_relinquish_or_restore_live_parent(tmp_path):
    runtime = object.__new__(prepared._ProductionPreparedTaskRuntime)
    runtime._task = {"spec": {"delegated_parent": {"parent_task_id": "parent"}}}
    runtime._relinquish_local_worker(None, None)
    runtime._restore_local_worker()


def test_staged_host_request_preserves_admitted_target_without_worker_control():
    from astrid.core.execution._child_bridge import _staged_child_task
    task = _staged_child_task(child={"child_id": "run", "capability_id": "vibecomfy.run", "capability_digest": "sha256:" + "a" * 64},
        inputs={}, ordered_inputs=[], stage="run", input_refs=[], execution_request={"schema_version": 1,
        "target": {"kind": "runpod", "pod_id": "pod", "provider_account_ref": "account", "storage": {"network_volume_id": "volume"}},
        "remote_activation_required": True})
    assert task["execution_request"]["remote_activation_required"]
    assert task["execution_request"]["target"]["pod_id"] == "pod"
    assert "inputs" not in task["execution_request"]


def test_matching_prepared_and_qualified_controls_preserve_existing_composition(tmp_path, monkeypatch):
    selected = selection(tmp_path)
    monkeypatch.setattr(h3, "load_worker_qualification", lambda path: {"qualified": True})
    monkeypatch.setattr(h3, "qualification_digest", lambda value: "qualified-digest")
    monkeypatch.setattr(h3, "_qualified_execution_request", lambda qualification, request: ({"target": selected.target}, selected.target))
    monkeypatch.setattr(prepared, "load_prepared_worker_selection", lambda path: selected)
    controls = h3.public_controls({"prepared_worker": "prepared", "worker_qualification": "qualified",
                                   "require_worker_qualification": True})
    assert controls["qualification_digest"] == "qualified-digest"
    assert controls["run_execution_request"]["remote_activation_required"]
    supplied = {"target": {**selected.target, "pod_id": "other"}}
    with pytest.raises(prepared.PreparedWorkerError, match="differs"):
        selected.execution_request(supplied)


def test_selection_digest_pins_immutable_claim_but_allows_activation_journal_progress(tmp_path):
    selected = selection(tmp_path)
    original = prepared.prepared_selection_digest(selected)
    claim = json.loads(selected.claim_handle_path.read_text())
    claim["managed_worker"] = {"state": "committed", "activation_id": "activation"}
    selected.claim_handle_path.write_text(json.dumps(claim))
    assert prepared.prepared_selection_digest(selected) == original
    claim["operation_id"] = "replacement"
    selected.claim_handle_path.write_text(json.dumps(claim))
    assert prepared.prepared_selection_digest(selected) != original


def test_public_manifest_declares_seven_controls_without_worker_defaults():
    import yaml
    document = yaml.safe_load((Path(h3.__file__).parents[2] / "pack.yaml").read_text())
    action = document["actions"]["transform"]
    ports = {item["name"]: item for item in action["inputs"]}
    assert h3.PUBLIC_CONTROLS <= ports.keys()
    assert h3.PUBLIC_CONTROLS <= set(action["metadata"]["generation_entry"]["inputs"])
    assert all("default" not in ports[name] for name in h3.PUBLIC_CONTROLS)


def test_h3_dry_run_input_previews_ordered_stages_without_owner_or_admission(monkeypatch):
    capability = SimpleNamespace(id="h3_av.transform", capability_type="executor", native_kind="action",
        inputs=[], outputs=[], definition={"inputs": []})
    sdk = SimpleNamespace(_load_registries=lambda **kw: (object(),), get_capability=lambda *a, **kw: capability)
    monkeypatch.setattr(invocation, "_sdk_module", lambda: sdk)
    monkeypatch.setattr(invocation, "_kernel_invoke", lambda *a, **kw: pytest.fail("preview admitted a parent"))
    result = invocation.invoke("h3_av.transform", kind="action", inputs={"dry_run": True})
    assert result.ok and result.raw_result["status"] == "planned"
    assert [item["stage"] for item in result.raw_result["stages"]] == [name for name, capability in h3.STAGES]
    assert result.kernel_task_id is None


def test_generate_without_client_keeps_one_existing_owner_context(monkeypatch):
    import astrid.sdk as sdk
    from astrid.core.generation import entries
    from contextlib import contextmanager
    description = {"id": "h3", "kind": "video", "source": "action", "action": "h3_av.transform",
        "mode": "transform", "modes": ["transform"], "inputs": [
            {"name": "request", "type": "file", "required": True},
            {"name": "asset_map", "type": "file", "required": True}]}
    monkeypatch.setattr(entries, "describe_entry", lambda *a, **kw: description)
    monkeypatch.setattr(sdk, "_load_model_registry", lambda **kw: object())
    events = []
    @contextmanager
    def open_owner(**kwargs):
        assert kwargs == {"start_pack_host": False}
        events.append("open")
        yield SimpleNamespace(invoke_result=lambda *a, **kw: events.append("invoke") or "one-parent")
        events.append("close")
    monkeypatch.setattr(sdk.AstridClient, "open_from_launcher", open_owner)
    assert GenerationFacade().video(entry="h3", request="request.json", asset_map="map.json") == "one-parent"
    assert events == ["open", "invoke", "close"]
