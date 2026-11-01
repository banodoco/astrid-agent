"""Focused HTTP admission/receipt recovery; provider activation is synthetic.

This is deliberately not the native H3 journey. Runtime's owner credential and
private cleanup tests separately cover activation authority and native ACK.
"""
import hashlib
import json
import urllib.request

import pytest

from astrid.packs.runpod import prepared_task as prepared
from astrid.sdk import AstridClient, invocation
from astrid.sdk.remote import RemoteAstridClient
from astrid.sdk.recovery import AdmissionReceipt
from astrid.sdk.workspace_client import WorkspaceClient
from tests.sdk.test_h3_prepared_owner_107 import selection

pytestmark = pytest.mark.integration


def test_real_http_parent_receipt_recovery_reattaches_one_stage_child(tmp_path, monkeypatch, real_runtime):
    daemon = real_runtime()
    owner = WorkspaceClient(daemon.endpoint, daemon.token)
    worker = WorkspaceClient(daemon.endpoint, daemon.worker_token)
    client = AstridClient(remote=RemoteAstridClient(owner))
    def http(token, method, path, body, key):
        request = urllib.request.Request(daemon.endpoint + path, data=json.dumps(body).encode(), method=method,
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json", "Idempotency-Key": key})
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.loads(response.read())
    digest = lambda value: "sha256:" + hashlib.sha256(value.encode()).hexdigest()
    capabilities = [{"capability_id": name, "definition_digest": digest(name), "status": "ready",
        "required_resource_keys": [], "estimated_scratch_bytes": 0, "estimated_output_bytes": 1}
        for name in ("h3_av.transform", "vibecomfy.run")]
    worker.register_executor({"executor_id": "astrid-pack-host", "capabilities": capabilities,
        "max_concurrency": 8, "resource_keys": [], "protocol": "workspace.v1"}, idempotency_key="register")
    project_result = client.projects.create(name="Prepared receipt", slug="prepared-receipt", idempotency_key="project")
    assert project_result.ok
    project = project_result.data["project_id"]
    selected = selection(tmp_path)
    request = selected.execution_request(None)
    blobs = {name: name.encode() for name in ("python", "companion", "source")}
    roots = {}
    for name, data in blobs.items():
        result = client.media.import_bytes(project=project, data=data, filename=name + ".txt", media_type="text/plain",
            idempotency_key="input-" + name)
        assert result.ok
        roots[name] = {"object_id": digest(name), "filename": name + ".txt"}
    policy = {"capabilities": [{"capability_id": "vibecomfy.run", "capability_digest": digest("vibecomfy.run")}],
        "targets": [{"kind": "default"}, selected.target], "input_object_ids": [value["object_id"] for value in roots.values()],
        "stages": [{"name": "run", "capability_id": "vibecomfy.run", "capability_digest": digest("vibecomfy.run"),
            "target": selected.target, "execution_request": request,
            "inputs": [{"name": name, "root_object_id": value["object_id"]} for name, value in roots.items()]}]}
    plan = {"controls": {"prepared_selection_digest": prepared.prepared_selection_digest(selected)}}
    admission = client.tasks.prepare_admission(project_id=project, capability="h3_av.transform",
        capability_digest=digest("h3_av.transform"), spec={"inputs": {**roots, "plan": plan}},
        input_manifest=[value["object_id"] for value in roots.values()],
        child_delegation=policy, idempotency_key="same-parent")
    assert admission.ok, admission.error
    receipt_path = tmp_path / "parent-receipt.json"
    receipt_request = {"capability": "h3_av.transform", "project": project}
    with AdmissionReceipt(receipt_path, request=receipt_request, client=client, resume=False) as receipt:
        receipt.freeze(admission.data, metadata={"capability_id": "h3_av.transform", "capability_type": "executor",
            "native_kind": "action", "read_managed_outputs": False, "discover_managed_outputs": False})
        result = client.tasks.dispatch_admission(admission.data)
        assert result.ok
        parent = result.data
        assert prepared._task_inputs(parent)["plan"] == plan
        # Lost admission response: the receipt deliberately remains unlocated.
    claim = worker.claim_task(executor_id="astrid-pack-host", capability_ids=["h3_av.transform"], idempotency_key="claim")
    lease = {key: claim[key] for key in ("lease_id", "fence", "runtime_epoch")}
    authority = http(daemon.worker_token, "POST", f"/v1/attempts/{claim['attempt_id']}/child-authority", lease, "authority")["authority"]
    child_body = {"authority": authority, "task": {"capability_id": "vibecomfy.run", "capability_digest": digest("vibecomfy.run"),
        "stage": "run", "input_refs": policy["stages"][0]["inputs"], "execution_request": request,
        "spec": {"inputs": {**roots, "workflow_inputs": "{}"}}}}
    child = http(daemon.worker_token, "POST", "/v1/delegated-tasks", child_body, "same-child")["data"]
    events = []
    class ProviderBoundary:
        def ensure_for_task(self, task):
            events.append(("ensure", task["task_id"]))
            if len(events) == 1:
                raise InterruptedError("synthetic owner interruption after exact child discovery")
            # No provider or worker is launched. Terminate the synthetic child
            # through its real authenticated owner, then fail the synthetic
            # parent honestly; cancelled children cannot authorize success.
            http(daemon.token, "POST", f"/v1/tasks/{task['task_id']}/cancel", {}, "cancel-synthetic-child")
            worker.fail_attempt(claim["attempt_id"], **lease,
                error={"message": "synthetic provider intentionally cancelled", "retryable": False},
                idempotency_key="fail-parent")
        def retire_after_task(self, task):
            events.append(("retire", task["task_id"]))
    selected = prepared.PreparedWorkerSelection(**{**selected.__dict__, "composition_factory": lambda **kw: ProviderBoundary()})
    assert prepared.prepared_selection_digest(selected) == plan["controls"]["prepared_selection_digest"]
    monkeypatch.setattr(prepared, "load_prepared_worker_selection", lambda path: selected)
    with AdmissionReceipt(receipt_path, request=receipt_request, client=client, resume=True) as receipt:
        with pytest.raises(InterruptedError):
            invocation._resume_receipt_invocation(receipt, client=client, wait=True, timeout_seconds=5,
                poll_seconds=0.01, owner_tick=prepared.PreparedH3OwnerMonitor(client, "selection"))
    replay = http(daemon.worker_token, "POST", "/v1/delegated-tasks", child_body, "same-child")["data"]
    assert replay["task_id"] == child["task_id"]
    with AdmissionReceipt(receipt_path, request=receipt_request, client=client, resume=True) as receipt:
        result = invocation._resume_receipt_invocation(receipt, client=client, wait=True, timeout_seconds=5,
            poll_seconds=0.01, owner_tick=prepared.PreparedH3OwnerMonitor(client, "selection"))
    assert not result.ok and result.kernel_task_id == parent["task_id"]
    assert result.raw_result["state"] == "failed"
    assert events == [("ensure", child["task_id"]), ("ensure", child["task_id"]), ("retire", child["task_id"])]
    listed, cursor = owner.list_project_tasks(project, limit=100)
    assert cursor is None
    assert sum(task["capability_id"] == "h3_av.transform" for task in listed) == 1
    assert sum(task["capability_id"] == "vibecomfy.run" for task in listed) == 1
