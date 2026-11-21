"""LS-15 public SDK dispatch and Runtime-owned records across two real jobs."""
from __future__ import annotations

import hashlib
import io
import json
import threading
from pathlib import Path

import pytest
from PIL import Image

from astrid.core.execution.generic_host import GenericPackHost, RuntimeProtocolClient
from astrid.sdk import AstridClient
from astrid.sdk.remote import RemoteAstridClient
from astrid.sdk.workspace_client import WorkspaceClient
from astrid_runtime_client import WorkspaceClient as GeneratedWorkspaceClient
from tests.packs.vibecomfy import test_ls15_resident_host

comfy = test_ls15_resident_host.comfy
resident_comfy = test_ls15_resident_host.resident_comfy
pytestmark = pytest.mark.integration


def test_public_sdk_second_job_has_independent_runtime_record_and_verified_output(
    resident_comfy, real_runtime, tmp_path,
):
    original, _, peer, fixture = resident_comfy
    original.shutdown()
    test_ls15_resident_host.canonical_inputs(resident_comfy, tmp_path / "authored-runtime")
    daemon = real_runtime()
    workspace = WorkspaceClient(daemon.endpoint, daemon.token)
    sdk = AstridClient(remote=RemoteAstridClient(workspace))
    project = sdk.projects.create(name="LS-15 resident proof", slug="ls15", idempotency_key="ls15-project")
    assert project.ok
    project_id = project.data["project_id"]
    workspace.select_project(project_id, idempotency_key="ls15-select")
    reader = GeneratedWorkspaceClient(daemon.endpoint, daemon.token)
    reader.handshake("ls15-record-proof", "1", [])
    pack = Path(__file__).resolve().parents[2] / "astrid/packs/vibecomfy"
    host = GenericPackHost(pack_roots=[pack], client=RuntimeProtocolClient(daemon.endpoint, daemon.worker_token),
                           attempt_base=tmp_path / "runtime-attempts")
    host.discover()
    host.preflight()
    host.register()
    worker = threading.Thread(target=host.run, kwargs={"poll_seconds": 0.02}, daemon=True)
    worker.start()
    try:
        from astrid.sdk.generation import _import_generation_member
        imported = {name: _import_generation_member(path, {"client": sdk, "project": project_id})
                    for name, path in fixture.members.items()}
        results, records = [], []
        process = None
        for index in (1, 2):
            result = sdk.invoke_result("vibecomfy.run", kind="action", project=project_id,
                inputs={**imported, "comfy_url": peer.url, "workflow_inputs": json.dumps({"color": index}),
                        "generation_intent": {"version": 1, "modality": "image", "partial_success_policy": "reject",
                            "groups": [{"group_key": "main", "selectors": [{"selector": "main-0", "ordinal": 0,
                                "variant_key": "original", "required": True}]}],
                            "metadata": {"model": "workflow", "align": "check"}}},
                wait=True, timeout_seconds=120, poll_seconds=0.05)
            assert result.ok, result.to_dict()
            with host._session_task_lock:
                if process is None:
                    process = host.managed_tool_session.current_adapter.process
                assert host.managed_tool_session.current_adapter.process is process
            task = reader.get_task(result.kernel_task_id)
            assert task.state == "succeeded" and task.project_id == project_id
            images = []
            for output in task.result["outputs"]:
                data = reader.get_object(output["digest"]).data
                assert "sha256:" + hashlib.sha256(data).hexdigest() == output["digest"]
                if data.startswith(b"\x89PNG\r\n\x1a\n"):
                    Image.open(io.BytesIO(data)).verify()
                    images.append(output["digest"])
            assert len(images) == 1
            records.append({"task_id": task.task_id, "attempt_id": result.kernel_attempt_id,
                            "state": task.state, "images": images, "result": task.result})
            results.append(result)
        (tmp_path / "runtime-records.json").write_text(json.dumps(records, indent=2, default=str))
        assert results[0].kernel_task_id != results[1].kernel_task_id
        assert results[0].kernel_attempt_id != results[1].kernel_attempt_id
        assert not any(result.replayed for result in results)
        assert len(peer.posts) == 2 and peer.counts()["GET /ws"] == 1
        prefixes = [next(node["inputs"]["filename_prefix"] for node in body["prompt"].values()
                         if node["class_type"] == "SaveImage") for body in peer.posts]
        assert len(set(prefixes)) == 2
        assert all(result.kernel_attempt_id in prefix for result, prefix in zip(results, prefixes, strict=True))
    finally:
        host._shutdown.set()
        worker.join(timeout=35)
        host.shutdown()
        daemon.stop()
