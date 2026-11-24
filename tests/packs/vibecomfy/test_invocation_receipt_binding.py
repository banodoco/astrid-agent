"""RA-6: the admitted receipt is evidence, never an input authority."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from astrid.packs.vibecomfy.invocation_preflight import (
    InvocationReceiptMismatch,
    preflight_invocation,
    verify_admitted_invocation_receipt,
)
from tests.packs.vibecomfy.test_local_server_run import comfy as comfy  # noqa: F401

pytestmark = pytest.mark.integration

CODE = "VIBECOMFY_INVOCATION_RECEIPT_MISMATCH"
MEMBERS = {"python": "workflow.py", "companion": "workflow.vibe.json", "source": "source.json"}


def _digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _publish(workflow, root: Path) -> dict[str, str]:
    from vibecomfy.security.provenance import Provenance
    from vibecomfy.workflow_bundle import emit_bundle_with_candidate, load_bundle

    root.mkdir(parents=True)
    source = json.dumps(load_bundle(workflow).materialize_ui(), sort_keys=True).encode()
    emit_bundle_with_candidate(
        workflow, root / "workflow.py", Provenance.USER_CONFIRMED, None,
        extra_members={root / "source.json": source},
    )
    return {name: str(root / filename) for name, filename in MEMBERS.items()}


def _z_image(root: Path, *, changed: bool = False) -> dict[str, str]:
    from vibecomfy.registry.ready import workflow_from_ready

    workflow = workflow_from_ready("image/z_image_turbo")
    if changed:
        for name, value in {"prompt": "a blue circle", "seed": 42, "width": 768, "height": 512}.items():
            workflow.set_input(name, value)
    return _publish(workflow, root)


def _empty_image(root: Path) -> dict[str, str]:
    from vibecomfy.nodes.core import EmptyImage, SaveImage
    from vibecomfy.templates import InputSpec, OutputSpec, ReadyMetadata, new_workflow

    with new_workflow(ReadyMetadata.build(template_id="ra6-empty", capability="image")) as workflow:
        image = EmptyImage(_id="1", width=16, height=16, batch_size=1, color=0)
        save = SaveImage(_id="2", images=image.out("IMAGE"), filename_prefix="astrid")
        workflow.finalize(
            {"width": InputSpec(node="1", field="width", default=16, type="INT")},
            outputs=[OutputSpec(node=save)],
        )
    return _publish(workflow, root)


def _receipt(inputs: dict[str, str]) -> dict:
    return preflight_invocation(
        inputs["python"], run_inputs=json.loads(inputs.get("workflow_inputs", "{}")),
    )


def _wrong_digest(field: str) -> str:
    return ("sha256:" if field != "revision_id" else "") + "f" * 64


@pytest.fixture
def canonical(tmp_path, monkeypatch):
    monkeypatch.setenv("VIBECOMFY_HEADLESS", "1")
    return _z_image(tmp_path / "admitted")


@pytest.mark.parametrize("field", ["api_digest", "revision_id", "workflow_content_digest"])
def test_receipt_digest_mismatch(canonical, tmp_path, field):
    receipt = _receipt(canonical)
    actual = receipt[field]
    receipt[field] = _wrong_digest(field)
    with pytest.raises(InvocationReceiptMismatch, match=field) as caught:
        verify_admitted_invocation_receipt(receipt, canonical, tmp_path / "host")
    assert caught.value.code == CODE and caught.value.field == field
    assert actual.removeprefix("sha256:")[:12] in str(caught.value)
    assert "ffffffffffff" in str(caught.value)
    assert actual not in str(caught.value) and receipt[field] not in str(caught.value)


def test_edited_companion_generation_id_is_content_mismatch(canonical, tmp_path):
    receipt = _receipt(canonical)
    companion = Path(canonical["companion"])
    body = json.loads(companion.read_text())
    body["bind"]["generation_id"] = "f" * 64
    companion.write_text(json.dumps(body))
    with pytest.raises(InvocationReceiptMismatch, match="workflow_content_digest") as caught:
        verify_admitted_invocation_receipt(receipt, canonical, tmp_path / "host")
    assert caught.value.field == "workflow_content_digest"


def test_matching_receipt_is_accepted(canonical, tmp_path):
    receipt = _receipt(canonical)
    verify_admitted_invocation_receipt(receipt, canonical, tmp_path / "host")


def test_stale_prompt_seed_and_size_bytes_refused_fresh_receipt_passes(canonical, tmp_path):
    stale = _receipt(canonical)
    changed = _z_image(tmp_path / "changed", changed=True)
    fresh = _receipt(changed)
    for field in ("revision_id", "workflow_content_digest", "api_digest"):
        assert stale[field] != fresh[field], field
    with pytest.raises(InvocationReceiptMismatch, match="workflow_content_digest"):
        verify_admitted_invocation_receipt(stale, changed, tmp_path / "stale")
    verify_admitted_invocation_receipt(fresh, changed, tmp_path / "fresh")


@pytest.mark.parametrize("field", ["api_digest", "revision_id", "workflow_content_digest", "run_inputs"])
def test_partial_receipt_refused(canonical, tmp_path, field):
    receipt = _receipt(canonical)
    del receipt[field]
    with pytest.raises(InvocationReceiptMismatch) as caught:
        verify_admitted_invocation_receipt(receipt, canonical, tmp_path / "host")
    assert caught.value.code == CODE


def _wire_inputs(inputs: dict[str, str], client, project_id: str) -> dict:
    wire = {}
    for name, filename in MEMBERS.items():
        data = Path(inputs[name]).read_bytes()
        digest = _digest(data)
        response = client.ingest_project_object(
            project_id, data, filename=filename,
            media_type="text/x-python" if name == "python" else "application/json",
            idempotency_key="ra6-object-" + name + "-" + digest,
        )
        assert response["object_id"] == digest
        wire[name] = {"object_id": digest, "digest": digest, "filename": filename}
    return wire


def test_sdk_no_archive_public_inputs_round_trip(canonical, tmp_path, real_runtime):
    from astrid_runtime_client.generated import WorkspaceClient as RawRuntimeClient
    from astrid.core.execution.generic_host import GenericPackHost, RuntimeProtocolClient
    from astrid.sdk.remote import RemoteTasks
    from astrid.sdk.workspace_client import WorkspaceClient

    daemon = real_runtime()
    raw = RawRuntimeClient(daemon.endpoint, daemon.token)
    host = GenericPackHost(
        pack_roots=[Path(__file__).resolve().parents[3] / "astrid/packs/vibecomfy"],
        client=RuntimeProtocolClient(daemon.endpoint, daemon.worker_token),
        credential_source={}, attempt_root=tmp_path / "attempt",
    )
    try:
        host.discover()
        host.preflight()
        host.register()
        project_id = raw.create_project("RA6 SDK round trip", slug="ra6-sdk", idempotency_key="ra6-project")["project_id"]
        wire = _wire_inputs(canonical, raw, project_id)
        bindings = {"prompt": "a new blue circle", "seed": 29, "width": 768, "height": 512}
        inputs = {**canonical, "workflow_inputs": json.dumps(bindings)}
        tasks = RemoteTasks(WorkspaceClient(daemon.endpoint, daemon.token))
        admitted = tasks.create(
            project_id=project_id, capability="vibecomfy.run",
            spec={"inputs": {**wire, "workflow_inputs": inputs["workflow_inputs"]}},
            input_manifest=[row["object_id"] for row in wire.values()],
            idempotency_key="ra6-sdk-round-trip",
        )
        assert admitted.ok, admitted.error
        task = raw.get_task(admitted.data["task_id"])
        assert task.state == "queued"
        receipt = task.spec["spec"]["invocation_preflight"]
    finally:
        host.shutdown()
        daemon.stop()
    assert receipt["run_inputs"] == bindings
    assert receipt["api_digest"] != _receipt(canonical)["api_digest"]
    verify_admitted_invocation_receipt(receipt, inputs, tmp_path / "host")
    stale = {**receipt, "api_digest": _receipt(canonical)["api_digest"]}
    with pytest.raises(InvocationReceiptMismatch, match="api_digest"):
        verify_admitted_invocation_receipt(stale, inputs, tmp_path / "stale")
    # Alter an actual admitted binding, not the receipt's binding dictionary.
    with pytest.raises(InvocationReceiptMismatch, match="api_digest"):
        verify_admitted_invocation_receipt(
            receipt, {**inputs, "workflow_inputs": json.dumps({**bindings, "seed": 30})},
            tmp_path / "changed",
        )


def test_managed_asset_manifest_bindings_are_recomputed(tmp_path, monkeypatch):
    from PIL import Image
    from vibecomfy.nodes.core import LoadImage, SaveImage
    from vibecomfy.templates import InputSpec, OutputSpec, ReadyMetadata, new_workflow
    from astrid.packs.vibecomfy.asset_manifest import resolve_inputs, write_archive

    monkeypatch.setenv("VIBECOMFY_HEADLESS", "1")
    with new_workflow(ReadyMetadata.build(template_id="ra6-load", capability="image")) as workflow:
        image = LoadImage(_id="1", image="initial.png")
        save = SaveImage(_id="2", images=image.out("IMAGE"), filename_prefix="astrid")
        workflow.finalize({
            "image": InputSpec(node="1", field="image", default="initial.png", type="STRING", media_semantics="image"),
            "save_name": InputSpec(node="2", field="filename_prefix", default="astrid", type="STRING"),
        }, outputs=[OutputSpec(node=save)])
    canonical = _publish(workflow, tmp_path / "admitted")
    source = tmp_path / "portrait.png"
    Image.new("RGB", (16, 16)).save(source)
    archive = tmp_path / "bindings.zip"
    manifest = write_archive({"image": source}, archive, workflow_inputs={"save_name": "first"})
    receipt = preflight_invocation(canonical["python"], run_inputs=resolve_inputs(manifest))
    inputs = {**canonical, "managed_assets": str(archive)}
    verify_admitted_invocation_receipt(receipt, inputs, tmp_path / "host")
    changed = tmp_path / "changed.zip"
    write_archive({"image": source}, changed, workflow_inputs={"save_name": "second"})
    with pytest.raises(InvocationReceiptMismatch, match="api_digest"):
        verify_admitted_invocation_receipt(
            receipt, {**inputs, "managed_assets": str(changed)}, tmp_path / "changed",
        )


@pytest.mark.parametrize("field", ["api_digest", "revision_id", "workflow_content_digest", "invocation_preflight", None],
                         ids=["api", "revision", "content", "missing", "matching"])
def test_direct_runtime_post_reaches_host_gate(field, comfy, tmp_path, monkeypatch, real_runtime):
    """Raw generated Runtime client; only the Comfy HTTP/GPU edge is synthetic.

    Removing the host's receipt check makes the mismatch cases fail: the
    Runtime still queues the task and the host does not raise this refusal.
    """
    import vibecomfy
    from astrid_runtime_client.generated import WorkspaceClient as RawRuntimeClient
    from astrid.core.execution.generic_host import GenericPackHost, RuntimeProtocolClient

    monkeypatch.setattr(
        "astrid.core.execution.generic_host._dependency_pythonpath",
        lambda: (str(Path(vibecomfy.__file__).resolve().parents[1]),),
    )
    comfy.schema["EmptyImage"] = {
        "input": {"required": {"width": ["INT", {"default": 16}],
                               "height": ["INT", {"default": 16}],
                               "batch_size": ["INT", {"default": 1}],
                               "color": ["INT", {"default": 0}]}},
        "output": ["IMAGE"], "output_name": ["IMAGE"], "output_node": False,
    }
    index_path = tmp_path / "node_index.json"
    index = json.loads(index_path.read_text())
    index.append({
        "class_type": "EmptyImage", "pack": "core",
        "inputs": {name: {"type": "INT", "required": True}
                   for name in ("width", "height", "batch_size", "color")},
        "outputs": [{"type": "IMAGE", "name": "IMAGE"}],
    })
    index_path.write_text(json.dumps(index))
    inputs = _empty_image(tmp_path / "bundle")
    receipt = _receipt(inputs)
    if field not in (None, "invocation_preflight"):
        receipt[field] = _wrong_digest(field)
    daemon = real_runtime()
    raw = RawRuntimeClient(daemon.endpoint, daemon.token)
    host = GenericPackHost(
        pack_roots=[Path(__file__).resolve().parents[3] / "astrid/packs/vibecomfy"],
        client=RuntimeProtocolClient(daemon.endpoint, daemon.worker_token),
        credential_source={}, attempt_root=tmp_path / "attempt",
    )
    try:
        host.discover()
        host.preflight()
        host.register()
        project_id = raw.create_project("RA6 direct POST", slug="ra6-direct", idempotency_key="ra6-project")["project_id"]
        wire = _wire_inputs(inputs, raw, project_id)
        spec = {"inputs": {**wire, "comfy_url": comfy.url, "workflow_inputs": "{}"}}
        if field != "invocation_preflight":
            spec["invocation_preflight"] = receipt
        assert comfy.calls == []
        # This generated method sends POST /v1/tasks without SDK preflight.
        admitted = raw.admit_task(
            capability_id="vibecomfy.run", capability_digest=host.capabilities["vibecomfy.run"].capability_digest,
            input_object_ids=[row["object_id"] for row in wire.values()],
            idempotency_key="ra6-direct-post", project_id=project_id, spec=spec,
        )
        task_id = admitted["task_id"]
        queued = raw.get_task(task_id)
        assert queued.state == "queued"
        assert queued.spec["spec"].get("invocation_preflight") == spec.get("invocation_preflight")
        assert comfy.calls == []
        if field is None:
            host.run(once=True)
            assert raw.get_task(task_id).state == "succeeded"
            assert len(comfy.prompts) == 1
        else:
            with pytest.raises(InvocationReceiptMismatch, match=field) as caught:
                host.run(once=True)
            assert caught.value.field == field and caught.value.code == CODE
            failed = raw.get_task(task_id)
            assert failed.state == "failed"
            assert failed.result is not None
            assert CODE in json.dumps(failed.result) and field in json.dumps(failed.result)
            assert comfy.calls == []  # Includes object_info, system_stats, and every POST.
    finally:
        host.shutdown()
        daemon.stop()
