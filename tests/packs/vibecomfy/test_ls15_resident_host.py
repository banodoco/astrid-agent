"""LS-15: actual admitted host jobs, held loopback peer and durable outputs."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.request import Request

import pytest

from astrid.core.execution.generic_host import GenericPackHost
from astrid.core.generation.backends.comfy_target import open_comfy_http
from tests.helpers.ls5_comfy_peer import ComfyPeer
from tests.packs.vibecomfy.test_local_server_run import comfy as comfy
from tests.test_generic_host import FakeRuntime

pytestmark = pytest.mark.integration


@pytest.fixture
def resident_comfy(comfy, tmp_path, monkeypatch):
    monkeypatch.setenv("ASTRID_SOURCE_STATE", str(tmp_path / "source-state.json"))
    monkeypatch.setenv("ASTRID_RUNTIME_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.delenv("ASTRID_PACKS_PATH", raising=False)
    workflow = json.loads(comfy.workflow.read_text())
    workflow["nodes"][0].update(type="EmptyImage", widgets_values=[16, 16, 1, 0],
        properties={"Node name for S&R": "EmptyImage"})
    comfy.workflow.write_text(json.dumps(workflow))
    comfy.schema["EmptyImage"] = {
        "input": {"required": {"width": ["INT", {"default": 16}], "height": ["INT", {"default": 16}],
                               "batch_size": ["INT", {"default": 1}], "color": ["INT", {"default": 0}]}},
        "output": ["IMAGE"], "output_name": ["IMAGE"], "output_node": False, "python_module": "nodes",
    }
    peer_adapter = SimpleNamespace(history=comfy.histories, outputs={}, views=[], input_files=set(),
                                   object_info=lambda: comfy.schema)
    def submit(body):
        request = Request(comfy.url + "/prompt", data=json.dumps(body).encode(),
                          headers={"Content-Type": "application/json"}, method="POST")
        with open_comfy_http(request, timeout=5) as response:
            result = json.load(response)
        for path in comfy.output.iterdir():
            peer_adapter.outputs[path.name] = (path.read_bytes(), "image/png")
        return 200, result
    peer_adapter.submit = submit

    class Runtime(FakeRuntime):
        def get_object(self, digest):
            assert digest == hashlib.sha256(comfy.workflow.read_bytes()).hexdigest()
            return comfy.workflow.read_bytes()

        def heartbeat(self, task_id, lease_token, *, attempt_id, fence, progress=None):
            super().heartbeat(task_id, lease_token, attempt_id=attempt_id, fence=fence, progress=progress)
            return {"task_id": task_id, "lease_id": lease_token, "attempt_id": attempt_id,
                    "fence": fence, "runtime_epoch": 1,
                    "lease_expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()}

    runtime = Runtime()
    pack = Path(__file__).resolve().parents[3] / "astrid/packs/vibecomfy"
    host = GenericPackHost(pack_roots=[pack], client=runtime, attempt_base=tmp_path / "attempts")
    host.discover()
    assert "vibecomfy.run" in host.capabilities
    host.register()
    with ComfyPeer(peer_adapter) as peer:
        try:
            yield host, runtime, peer, comfy
        finally:
            host.shutdown()


def run_job(world, number, *, values=None):
    host, runtime, peer, comfy = world
    digest = hashlib.sha256(comfy.workflow.read_bytes()).hexdigest()
    task_id, attempt_id = f"ls15-task-{number}", f"ls15-attempt-{number}"
    task = {"task": {"id": task_id, "attempt_id": attempt_id, "fence": number,
        "capability": "vibecomfy.run", "project_id": "ls15-project", "runtime_epoch": 1,
        "status": "running", "input_object_ids": ["sha256:" + digest],
        "spec": {"spec": {"inputs": {"workflow": {"digest": digest, "filename": "workflow.json"},
                "workflow_inputs": json.dumps(values or {}), "comfy_url": peer.url}}}}}
    runtime.tasks[task_id] = task
    assert host.run_task(task, lease_token=f"ls15-lease-{number}", keep_attempt=True)["task"]["status"] == "completed"
    result = next(row[2]["result"] for row in runtime.settlements if row[0] == task_id)
    receipt = next((host.attempt_base).glob(f"**/{task_id}-{attempt_id}/outputs/manifest.json"))
    manifest = json.loads(receipt.read_bytes())
    assert manifest["managed_generation_result"]["task_id"] == task_id
    produced = [row for row in manifest["outputs"] if row["role"] == "result"]
    assert len(produced) == 1
    output = receipt.parent / produced[0]["path"]
    assert "sha256:" + hashlib.sha256(output.read_bytes()).hexdigest() == produced[0]["content_hash"]
    journal = next(receipt.parent.glob(".vibecomfy-runtime/out/runs/*/attempt.json"))
    record = json.loads(journal.read_bytes())
    native_result = json.loads(Path(record["managed_generation_result"]["path"]).read_bytes())
    assert native_result["task_id"] == task_id and native_result["attempt_id"] == attempt_id
    assert record["queue_acceptance"]["status"] == "accepted"
    assert record["artifact_retrieval"]["connection"]["mode"] == "held"
    return result, manifest, record


def test_second_admitted_job_reuses_worker_template_and_target(resident_comfy):
    host, runtime, peer, comfy = resident_comfy
    first = run_job(resident_comfy, 1)
    process = host.managed_tool_session.current_adapter.process
    before = peer.counts()
    second = run_job(resident_comfy, 2)
    assert host.managed_tool_session.current_adapter.process is process
    assert first[0]["process_id"] == second[0]["process_id"] == process.pid
    assert peer.counts()["GET /object_info"] == before["GET /object_info"]
    assert peer.counts()["GET /ws"] == before["GET /ws"] == 1
    assert len(peer.posts) == 2
    assert first[2]["queue_acceptance"]["prompt_id"] != second[2]["queue_acceptance"]["prompt_id"]
    prefixes = [body["prompt"]["2"]["inputs"]["filename_prefix"] for body in peer.posts]
    assert len(set(prefixes)) == 2
    assert all(f"ls15-attempt-{index}" in prefix for index, prefix in enumerate(prefixes, 1))
