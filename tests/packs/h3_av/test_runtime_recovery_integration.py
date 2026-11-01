"""Local Runtime/SDK receipt recovery through H3's canonical run handoff.

The worker is deliberately a CPU fake. Runtime admission, generated HTTP,
settlement, managed-output readback, and H3 composition/verification remain
real; this is not GPU or provider qualification.
"""
from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

import pytest

from astrid.core.foundation.hash import executor_definition_digest
from astrid.packs.h3_av.actions.prepare.prepare import prepare_request
from astrid.packs.h3_av.actions.transform.operation import OperationJournal
from astrid.packs.h3_av.actions.transform.receipt import (
    attest_runtime_managed_publication,
    build_final_receipt,
    write_final_receipt,
)
from astrid.packs.h3_av.actions.transform.run import (
    _invoke_canonical_run,
    _materialize_output,
)
from astrid.packs.h3_av.actions.verify.verify import verify_candidate
from astrid.packs.h3_av.shared.compose import compose_candidate
from astrid.packs.h3_av.shared.request import normalize_request
from astrid.sdk.client import AstridClient
from astrid.sdk.invocation import _sdk_module
from astrid.sdk.remote import RemoteAstridClient
from astrid.sdk.workspace_client import WorkspaceClient
from tests.helpers.runtime import executor_token

ASTRID_ROOT = Path(__file__).resolve().parents[3]

pytestmark = pytest.mark.integration  # needs the Runtime from a second checkout (real_runtime fixture)


def _synthetic_av(path: Path, *, color: str) -> None:
    import subprocess

    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", f"color=c={color}:s=32x32:r=24",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
            "-t", "2", "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-c:a", "aac", str(path),
        ],
        check=True,
    )


def _connected(daemon) -> tuple[AstridClient, WorkspaceClient]:
    transport = WorkspaceClient(daemon.endpoint, daemon.token)
    transport.handshake(
        "h3-recovery-integration", "1",
        ["projects:read", "projects:write", "objects:read", "objects:write", "tasks:read", "tasks:write"],
    )
    sdk_client = AstridClient(remote=RemoteAstridClient(transport))
    worker = WorkspaceClient(daemon.endpoint, executor_token(daemon, "h3-cpu-recovery-worker"))
    return sdk_client, worker


def _descriptor(transport: WorkspaceClient, project_id: str, payload: bytes, *, filename: str, key: str) -> dict[str, object]:
    imported = transport.ingest_project_object(
        project_id,
        payload,
        media_type="application/octet-stream",
        filename=filename,
        idempotency_key=key,
    )
    data = imported.get("data") if isinstance(imported, dict) else getattr(imported, "data", imported)
    object_id = data.get("object_id") if isinstance(data, dict) else None
    assert isinstance(object_id, str) and object_id.startswith("sha256:")
    return {"object_id": object_id, "digest": object_id, "filename": filename, "required": True}


@pytest.mark.ffmpeg
def test_runtime_response_loss_resumes_canonical_run_into_h3_verified_receipt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, real_runtime) -> None:
    daemon = real_runtime(tmp_path / "realm")
    try:
        sdk_client, worker = _connected(daemon)
        transport = sdk_client._remote._transport
        created = transport.create_project("H3 receipt recovery", slug="h3-recovery", idempotency_key="h3-recovery-project")
        created_data = created.get("data") if isinstance(created, dict) else getattr(created, "data", None)
        project = created_data.get("project_id") if isinstance(created_data, dict) else None
        assert isinstance(project, str) and project

        sdk_module = _sdk_module()
        registries = sdk_module._load_registries(
            project_root=ASTRID_ROOT,
            extra_pack_roots=(),
            banodoco_config=None,
            include_missing_roots=False,
            include_elements=False,
        )
        executor_registry = registries[0]
        definition = executor_registry.get("vibecomfy.run")
        digest = executor_definition_digest(definition)
        daemon.service.register_capability({
            "capability_id": "vibecomfy.run",
            "definition_digest": digest,
            "status": "ready",
        })
        daemon.service.register_executor(
            {"executor_id": "h3-cpu-recovery-worker", "capabilities": ["vibecomfy.run"]},
            idempotency_key="h3-cpu-recovery-worker",
        )

        source = tmp_path / "source.mp4"
        generated = tmp_path / "generated.mp4"
        _synthetic_av(source, color="blue")
        _synthetic_av(generated, color="red")
        raw_request = {
            "version": 1,
            "operation": "edit",
            "source": {"asset": "source", "range": [0, 2]},
            "output": {"duration": 2},
            "content": {"prompt": "Replace the spoken line."},
            "changes": {
                "video": [{"during": [1, 2], "area": {"full_frame": True}, "action": "generate"}],
                "audio": [{"during": [1, 2], "action": "generate"}],
            },
            "references": [],
            "overrides": {},
        }
        request = normalize_request(raw_request)
        preparation = prepare_request(request, asset_map={"source": str(source)})
        generation_intent = {
            "version": 1,
            "modality": "video",
            "partial_success_policy": "reject",
            "groups": [{
                "group_key": "main",
                "selectors": [{
                    "selector": "main-0", "ordinal": 0,
                    "variant_key": "original", "required": True,
                }],
            }],
            "metadata": {
                "compiled_generation_contract": True,
                "h3_av": {"request_digest": preparation["request_digest"]},
            },
        }
        from vibecomfy.workflow import VibeWorkflow, WorkflowSource
        from vibecomfy.workflow_bundle import emit_bundle

        workflow_path = tmp_path / "workflow.py"
        workflow = VibeWorkflow("cpu-recovery", WorkflowSource("cpu-recovery"))
        workflow.add_node("Integer", uid="integer-node", value=7)
        emit_bundle(workflow, workflow_path, {"operation": "authored"})
        workflow_source_path = tmp_path / "source.json"
        workflow_source_path.write_text('{"nodes":[],"links":[]}\n', encoding="utf-8")
        descriptors = {
            "python": _descriptor(transport, project, workflow_path.read_bytes(), filename="workflow.py", key="workflow-py"),
            "companion": _descriptor(transport, project, workflow_path.with_suffix(".vibe.json").read_bytes(), filename="workflow.vibe.json", key="workflow-companion"),
            "source": _descriptor(transport, project, workflow_source_path.read_bytes(), filename="source.json", key="workflow-source"),
        }
        inputs = {
            **descriptors,
            "workflow_inputs": "{}",
            "generation_intent": generation_intent,
        }

        def bound_client(client: AstridClient):
            class BoundClient:
                media = client.media
                tasks = client.tasks

                def invoke_result(self, capability_id: str, **kwargs: object):
                    from astrid.sdk.invocation import invoke_result

                    kwargs.pop("kind", None)
                    kwargs.setdefault("project_root", ASTRID_ROOT)
                    kwargs.setdefault("timeout_seconds", 5)
                    kwargs.setdefault("poll_seconds", 0.005)
                    return invoke_result(capability_id, kind="executor", client=client, **kwargs)

            return BoundClient()

        first_client = bound_client(sdk_client)
        out = tmp_path / "operation" / "04-run"
        saved_result = out / "run-result.json"
        journal_path = tmp_path / "operation" / "operation-state.json"
        journal = OperationJournal(journal_path, request_digest=request.digest)
        context = {"h3_submission_id": journal.submission_id}

        original_request = transport._generated._request
        lost = {"once": False}

        def drop_committed_admission(method: str, url: str, **kwargs: object):
            response = original_request(method, url, **kwargs)
            if method == "POST" and url == "/v1/tasks" and not lost["once"]:
                lost["once"] = True
                raise ConnectionResetError("simulated lost Runtime admission response")
            return response

        monkeypatch.setattr(transport._generated, "_request", drop_committed_admission)
        with pytest.raises(RuntimeError) as first_failure:
            _invoke_canonical_run(
                first_client,
                inputs=inputs,
                execution_request=None,
                out=out,
                project=project,
                saved_result=saved_result,
                journal=journal,
                resume=False,
                idempotency_context=context,
            )
        monkeypatch.setattr(transport._generated, "_request", original_request)
        assert lost["once"], str(first_failure.value)
        assert daemon.service.store.conn.execute("SELECT COUNT(*) FROM tasks WHERE capability='vibecomfy.run'").fetchone()[0] == 1
        assert not saved_result.exists()

        claim = worker.claim_task(
            executor_id="h3-cpu-recovery-worker",
            capability_ids=["vibecomfy.run"],
            idempotency_key="h3-run-claim",
            runtime_epoch=worker.health()["runtime_epoch"],
        )
        assert claim is not None
        generated_bytes = generated.read_bytes()
        output_digest = "sha256:" + hashlib.sha256(generated_bytes).hexdigest()
        settled = worker.settle_attempt(
            claim["attempt_id"],
            {
                "lease_id": claim["lease_id"],
                "fence": claim["fence"],
                "runtime_epoch": claim["runtime_epoch"],
                "effect": claim["expected_effect"],
                "outputs": [{
                    "name": "vibecomfy_run",
                    "filename": "generated.mp4",
                    "kind": "object",
                    "output_port": "vibecomfy_run",
                    "digest": output_digest,
                    "media_type": "video/mp4",
                    "size": len(generated_bytes),
                    "data_base64": base64.b64encode(generated_bytes).decode("ascii"),
                    "role": "result",
                    "selector": {"group_key": "main", "variant_key": "original"},
                    "ordinal": 0,
                }],
            },
            idempotency_key="h3-run-settle",
        )
        assert settled.get("data", {}).get("state") == "succeeded"

        # Simulate a process restart: reopen the operation journal and client,
        # then ask the same canonical stage receipt to observe the settled run.
        resumed_journal = OperationJournal(journal_path, request_digest=request.digest)
        sdk_client, _reconnected_worker = _connected(daemon)
        recovered = _invoke_canonical_run(
            bound_client(sdk_client),
            inputs=inputs,
            execution_request=None,
            out=out,
            project=project,
            saved_result=saved_result,
            journal=resumed_journal,
            resume=True,
            idempotency_context={"h3_submission_id": resumed_journal.submission_id},
        )
        assert recovered.ok
        assert recovered.kernel_task_id == claim["task_id"]
        assert recovered.kernel_run_id == claim["run_id"]
        assert recovered.kernel_attempt_id == claim["attempt_id"]
        assert recovered.raw_result["task"]["expected_effect"] == claim["expected_effect"]
        assert len(recovered.outputs["managed_outputs"]) == 1
        assert recovered.outputs["managed_outputs"][0]["object_id"] == output_digest

        read_bytes = sdk_client.media.read_bytes
        corrupt_read_ids: list[str] = []

        def corrupt_first_managed_read(object_id: str) -> bytes:
            corrupt_read_ids.append(object_id)
            if len(corrupt_read_ids) == 1:
                return generated_bytes + b"corruption"
            return read_bytes(object_id)

        monkeypatch.setattr(sdk_client.media, "read_bytes", corrupt_first_managed_read)
        with pytest.raises(RuntimeError, match="digest verification"):
            _materialize_output(
                sdk_client,
                recovered,
                "vibecomfy_run",
                tmp_path / "retrieved-corrupt",
                managed_only=True,
            )
        assert corrupt_read_ids == [output_digest]

        monkeypatch.setattr(sdk_client.media, "read_bytes", read_bytes)
        generated_path, managed_row = _materialize_output(
            sdk_client, recovered, "vibecomfy_run", tmp_path / "retrieved", managed_only=True,
        )
        assert hashlib.sha256(generated_path.read_bytes()).hexdigest() == output_digest.removeprefix("sha256:")
        assert generated_path.stat().st_size == managed_row["size"] == len(generated_bytes)
        assert managed_row["role"] == "result"
        assert managed_row["output_port"] == "vibecomfy_run"
        assert managed_row["object_id"] == output_digest

        raw_publication = attest_runtime_managed_publication(
            runtime_result=recovered,
            request_digest=preparation["request_digest"],
            generation_intent=generation_intent,
            retrieved_outputs=[managed_row],
        )
        assert raw_publication.status == "passed", (raw_publication.evidence.get("validation_error"), recovered.raw_result.get("task"))
        assert raw_publication.evidence["publication"]["object_id"] == output_digest

        composition = compose_candidate(
            preparation=preparation,
            generated=generated_path,
            source=source,
            out_dir=tmp_path / "composition",
        )
        verification = verify_candidate(
            preparation=preparation,
            composition=composition,
            source=source,
        )
        receipt = build_final_receipt(
            request_digest=preparation["request_digest"],
            task_succeeded={
                "run_id": recovered.kernel_run_id,
                "task_id": recovered.kernel_task_id,
                "attempt_id": recovered.kernel_attempt_id,
            },
            candidate_verified={"verification": verification},
            raw_managed_publication=raw_publication,
        )
        receipt_path = write_final_receipt(tmp_path / "final-receipt.json", receipt)
        saved_receipt = json.loads(receipt_path.read_text())
        assert saved_receipt["overall_status"] == "candidate_verified"
        assert saved_receipt["states"]["raw_managed_publication"]["status"] == "passed"
        assert saved_receipt["states"]["candidate_verified"]["status"] == "passed"
        assert daemon.service.store.conn.execute("SELECT COUNT(*) FROM tasks WHERE capability='vibecomfy.run'").fetchone()[0] == 1
        assert daemon.service.store.conn.execute(
            "SELECT COUNT(*) FROM attempts WHERE task_id=?", (claim["task_id"],)
        ).fetchone()[0] == 1
        assert daemon.service.store.conn.execute("SELECT COUNT(*) FROM generations").fetchone()[0] == 1
        assert daemon.service.store.conn.execute("SELECT COUNT(*) FROM generation_variants").fetchone()[0] == 1
    finally:
        daemon.stop()
