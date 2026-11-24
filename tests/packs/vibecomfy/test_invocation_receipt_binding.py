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


def _wire_file(path: Path, name: str, client, project_id: str, *, media_type: str, filename: str) -> dict:
    data = path.read_bytes()
    digest = _digest(data)
    response = client.ingest_project_object(
        project_id, data, filename=filename, media_type=media_type,
        idempotency_key="ra6-object-" + name + "-" + digest.removeprefix("sha256:"),
    )
    assert response["object_id"] == digest
    return {"object_id": digest, "digest": digest, "filename": filename}


def _wire_inputs(inputs: dict[str, str], client, project_id: str) -> dict:
    return {
        name: _wire_file(
            Path(inputs[name]), name, client, project_id, filename=filename,
            media_type="text/x-python" if name == "python" else "application/json",
        )
        for name, filename in MEMBERS.items()
    }


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
            "image": InputSpec(node="1", field="image", default="initial.png", type="COMBO", media_semantics="image", infer_type=False),
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


@pytest.fixture
def attached_case(comfy, tmp_path, monkeypatch, real_runtime):
    """Real registered pack host and Runtime, with only Comfy HTTP faked."""
    from types import SimpleNamespace

    import vibecomfy
    from astrid_runtime_client.generated import WorkspaceClient as RawRuntimeClient
    from astrid.core.execution.generic_host import GenericPackHost, RuntimeProtocolClient
    from runtime_protocol.daemon import WORKER_SCOPES

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
    daemon = real_runtime()
    # Same credential-verified machine fixture as test_runpod_slot_path_cpu.
    worker_token, _ = daemon.credentials.provision(
        "astrid-pack-host", list(WORKER_SCOPES), rotate=True,
        metadata={"execution_binding": {
            "actual": {"kind": "machine", "id": "ra6-fixture-machine"},
            "executor_incarnation": "ra6-fixture",
            "verification": {"method": "credential_claim", "verified": True,
                             "evidence_digest": _digest(b"ra6-fixture-machine")},
        }},
    )
    raw = RawRuntimeClient(daemon.endpoint, daemon.token)
    host = GenericPackHost(
        pack_roots=[Path(__file__).resolve().parents[3] / "astrid/packs/vibecomfy"],
        client=RuntimeProtocolClient(daemon.endpoint, worker_token),
        credential_source={}, attempt_base=tmp_path / "attempts", max_concurrency=2,
    )
    try:
        host.discover()
        host.preflight()
        host.register()
        project_id = raw.create_project("RA6 direct POST", slug="ra6-direct", idempotency_key="ra6-project")["project_id"]
        yield SimpleNamespace(host=host, raw=raw, project_id=project_id, comfy=comfy,
                              inputs=inputs, daemon=daemon)
    finally:
        host.shutdown()
        daemon.stop()


def _admit(case, spec, wire, *, key="ra6-direct-post"):
    admitted = case.raw.admit_task(
        capability_id="vibecomfy.run",
        capability_digest=case.host.capabilities["vibecomfy.run"].capability_digest,
        input_object_ids=[row["object_id"] for row in wire.values()],
        idempotency_key=key, project_id=case.project_id, spec=spec,
    )
    task = case.raw.get_task(admitted["task_id"])
    assert task.state == "queued"
    assert task.spec["spec"].get("invocation_preflight") == spec.get("invocation_preflight")
    return task.task_id


def _canonical_spec(case, inputs=None):
    wire = _wire_inputs(inputs or case.inputs, case.raw, case.project_id)
    return {"inputs": {**wire, "comfy_url": case.comfy.url, "workflow_inputs": "{}"}}, wire


def _assert_refused(case, task_id, field, run):
    with pytest.raises(InvocationReceiptMismatch, match=field) as caught:
        run()
    assert caught.value.field == field and caught.value.code == CODE
    failed = case.raw.get_task(task_id)
    assert failed.state == "failed"
    assert failed.result is not None
    assert CODE in json.dumps(failed.result) and field in json.dumps(failed.result)
    assert case.comfy.calls == []  # Includes object_info, system_stats, and every POST.


@pytest.mark.parametrize("field", ["api_digest", "revision_id", "workflow_content_digest", "invocation_preflight", None],
                         ids=["api", "revision", "content", "missing", "matching"])
def test_direct_runtime_post_reaches_host_gate(field, attached_case):
    """Raw POST /v1/tasks, no SDK preflight; removing the gate makes refusals go red."""
    case = attached_case
    receipt = _receipt(case.inputs)
    if field not in (None, "invocation_preflight"):
        receipt[field] = _wrong_digest(field)
    spec, wire = _canonical_spec(case)
    if field != "invocation_preflight":
        spec["invocation_preflight"] = receipt
    assert case.comfy.calls == []
    task_id = _admit(case, spec, wire)
    assert case.comfy.calls == []
    if field is None:
        case.host.run(once=True)
        assert case.raw.get_task(task_id).state == "succeeded"
        assert len(case.comfy.prompts) == 1
    else:
        _assert_refused(case, task_id, field, lambda: case.host.run(once=True))


@pytest.mark.parametrize("with_receipt", [False, True], ids=["legacy", "receipt"])
def test_legacy_ui_host_compatibility(attached_case, with_receipt):
    case = attached_case
    # The published EmptyImage's UI JSON is a real legacy workflow input.
    wire = {"workflow": _wire_file(
        Path(case.inputs["source"]), "workflow", case.raw, case.project_id,
        filename="workflow.json", media_type="application/json",
    )}
    spec = {"inputs": {**wire, "comfy_url": case.comfy.url}}
    if with_receipt:
        spec["invocation_preflight"] = _receipt(case.inputs)
    task_id = _admit(case, spec, wire)
    if with_receipt:
        _assert_refused(case, task_id, "invocation_preflight", lambda: case.host.run(once=True))
    else:
        case.host.run(once=True)
        assert case.raw.get_task(task_id).state == "succeeded"
        assert len(case.comfy.prompts) == 1


def test_direct_runtime_post_cannot_supply_delegated_parent(attached_case):
    from astrid_runtime_client.generated import ApiError

    case = attached_case
    spec, wire = _canonical_spec(case)
    spec["delegated_parent"] = {"parent_task_id": "forged"}
    with pytest.raises(ApiError, match="delegation fields must use Runtime admission authority"):
        _admit(case, spec, wire)
    assert case.comfy.calls == []


def test_inner_forged_delegated_parent_does_not_bypass_host(attached_case):
    # Runtime rejects the immediate inner key at admission (above). Inject a
    # forged inner field into a real claim snapshot to exercise the host's
    # independent distinction between outer Runtime authority and caller data.
    from dataclasses import replace

    case = attached_case
    spec, wire = _canonical_spec(case)
    task_id = _admit(case, spec, wire)
    claim = case.host.client.claim_next(
        executor_id=case.host.executor_id, capability_ids=["vibecomfy.run"],
        idempotency_key="ra6-claim-forgery",
    )
    assert claim.task_id == task_id
    forged = replace(claim, spec={**claim.spec, "spec": {
        **claim.spec["spec"], "delegated_parent": {"parent_task_id": "forged"},
    }})
    _assert_refused(case, task_id, "invocation_preflight",
                    lambda: case.host._execute_claimed_task(forged))


@pytest.mark.parametrize("bad_receipt", [False, True], ids=["absent", "bad"])
def test_real_delegated_child_host_compatibility(attached_case, bad_receipt):
    from astrid.core.execution.generic_host import _run_source_digest

    case = attached_case
    host = case.host
    spec, wire = _canonical_spec(case)
    ids = [row["object_id"] for row in wire.values()]
    cap = host.capabilities["vibecomfy.run"]
    parent_cap = host.capabilities["vibecomfy.validate"]
    child_cap = {"capability_id": cap.id, "capability_digest": cap.capability_digest}
    parent_task = case.raw.admit_task(
        capability_id=parent_cap.id, capability_digest=parent_cap.capability_digest,
        project_id=case.project_id, input_object_ids=ids, idempotency_key="ra6-parent",
        spec={"inputs": wire}, child_delegation={
            "capabilities": [child_cap], "targets": [{"kind": "default"}],
            "input_object_ids": ids, "limits": {"max_children": 2},
        },
    )
    parent = host.client.claim_next(
        executor_id=host.executor_id, capability_ids=[parent_cap.id], idempotency_key="ra6-claim-parent",
    )
    assert parent.task_id == parent_task["task_id"]
    fence = {"task_id": parent.task_id, "attempt_id": parent.attempt_id,
             "lease_id": parent.lease_id, "fence": parent.fence, "runtime_epoch": parent.runtime_epoch}
    try:
        # HostChildBridge normally adds this witness before child admission.
        spec["admitted_source_digest"] = _run_source_digest(cap)
        if bad_receipt:
            spec["invocation_preflight"] = {**_receipt(case.inputs), "api_digest": _wrong_digest("api_digest")}
        authority = host.client.generated.issue_child_authority(
            parent.attempt_id, lease_id=parent.lease_id, fence=parent.fence, runtime_epoch=parent.runtime_epoch,
        )
        child = host.client.generated.admit_delegated_task(
            authority=authority["authority"], idempotency_key="ra6-child",
            task={**child_cap, "input_object_ids": ids, "spec": spec},
        )
        child_id = child["task_id"]
        queued = case.raw.get_task(child_id)
        assert queued.state == "queued"
        assert queued.spec["delegated_parent"]["parent_task_id"] == parent.task_id
        def run():
            return host.run_delegated_child(
                {"task_id": child_id, "capability_id": cap.id}, parent=fence,
                authority_client=host.client, target={"kind": "default"}, live=lambda: None,
            )
        if bad_receipt:
            _assert_refused(case, child_id, "api_digest", run)
        else:
            run()
            assert case.raw.get_task(child_id).state == "succeeded"
            assert len(case.comfy.prompts) == 1
    finally:
        host.client.fail(parent.task_id, parent.lease_id, "fixture parent complete", retryable=False,
                         attempt_id=parent.attempt_id, fence=parent.fence)


def test_import_time_input_mutation_refuses_before_target(attached_case):
    from astrid.core.execution.generic_host import HostError
    from astrid.core.execution.host_naming import _managed_input_staging_name

    case = attached_case
    source = Path(case.inputs["source"])
    staged_name = _managed_input_staging_name("source", _digest(source.read_bytes())[7:], "source.json")
    python = Path(case.inputs["python"])
    # Execute only when the admitted Python is imported from the receipt's
    # private staging directory, not when the SDK builds its real receipt.
    with python.open("a", encoding="utf-8") as stream:
        stream.write(
            "\nfrom pathlib import Path as _ReceiptAttackPath\n"
            "if _ReceiptAttackPath(__file__).resolve().parents[1].name == 'workflow-receipt':\n"
            f"    (_ReceiptAttackPath(__file__).resolve().parents[2] / 'inputs' / {staged_name!r}).write_bytes(b'tampered')\n"
        )
    receipt = _receipt(case.inputs)
    spec, wire = _canonical_spec(case)
    spec["invocation_preflight"] = receipt
    task_id = _admit(case, spec, wire)
    with pytest.raises(HostError, match="admitted input bytes changed during preparation"):
        case.host.run(once=True)
    assert case.raw.get_task(task_id).state == "failed"
    assert case.comfy.calls == []


def test_stale_both_generation_markers_and_receipt_refused(canonical, tmp_path):
    stale = _receipt(canonical)
    changed = _z_image(tmp_path / "both-stale", changed=True)
    old_id = json.loads(Path(canonical["companion"]).read_text())["bind"]["generation_id"]
    companion = Path(changed["companion"])
    new_id = json.loads(companion.read_text())["bind"]["generation_id"]
    assert new_id != old_id
    for member in ("python", "companion"):
        path = Path(changed[member])
        payload = path.read_bytes()
        assert new_id.encode() in payload
        path.write_bytes(payload.replace(new_id.encode(), old_id.encode()))
    with pytest.raises(InvocationReceiptMismatch, match="workflow_content_digest"):
        verify_admitted_invocation_receipt(stale, changed, tmp_path / "host")
    # Ruling 3: with a fresh receipt, both stale generation markers are a
    # byte-identity cache defect for P82-05, not a generation-id derivation here.
    verify_admitted_invocation_receipt(_receipt(changed), changed, tmp_path / "fresh")


def test_workspace_no_execution_request_preserves_receipt(attached_case):
    from astrid.sdk.workspace_client import WorkspaceClient

    case = attached_case
    spec, wire = _canonical_spec(case)
    workspace = WorkspaceClient(case.daemon.endpoint, case.daemon.token)
    admitted = workspace.admit_task(
        capability_id="vibecomfy.run",
        capability_digest=case.host.capabilities["vibecomfy.run"].capability_digest,
        input_object_ids=[row["object_id"] for row in wire.values()],
        idempotency_key="ra6-workspace", project_id=case.project_id, spec=spec,
    )
    task = case.raw.get_task(admitted["data"]["task_id"])
    assert task.state == "queued"
    assert task.spec["spec"]["invocation_preflight"]["api_digest"] == _receipt(case.inputs)["api_digest"]
    case.host.run(once=True)
    assert case.raw.get_task(task.task_id).state == "succeeded"
    assert len(case.comfy.prompts) == 1


def _video_workflow(root):
    from vibecomfy.nodes.core import GetVideoComponents, LoadVideo, SaveImage
    from vibecomfy.templates import InputSpec, OutputSpec, ReadyMetadata, new_workflow

    with new_workflow(ReadyMetadata.build(template_id="ra6-source-video", capability="image")) as workflow:
        video = LoadVideo(_id="99", file="initial.mp4")
        parts = GetVideoComponents(_id="2", video=video.out(0))
        save = SaveImage(_id="3", images=parts.out(0), filename_prefix="astrid")
        workflow.finalize({
            "source_video": InputSpec(node="99", field="file", default="initial.mp4",
                                      type="COMBO", media_semantics="video", infer_type=False),
        }, outputs=[OutputSpec(node=save)])
    return _publish(workflow, root)


def test_sdk_standalone_source_video_host_round_trip(attached_case, tmp_path, monkeypatch):
    import subprocess

    from astrid.core.execution.generic_host import HostError
    from astrid.core.execution.host_naming import _managed_input_staging_name
    from astrid.sdk.remote import RemoteTasks
    from astrid.sdk.workspace_client import WorkspaceClient

    case = attached_case
    video = tmp_path / "clip.mp4"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                    "color=black:s=16x16:r=24", "-t", "1", "-pix_fmt", "yuv420p", str(video)],
                   check=True, timeout=15)
    inputs = _video_workflow(tmp_path / "video-bundle")
    wire = _wire_inputs(inputs, case.raw, case.project_id)
    wire["source_video"] = _wire_file(video, "source_video", case.raw, case.project_id,
                                      filename=video.name, media_type="video/mp4")
    expected = _managed_input_staging_name("source_video", _digest(video.read_bytes())[7:], video.name)
    spec = {"inputs": {**wire, "placement_policy": json.dumps({"kind": "runpod", "engine": "comfyui"})}}
    tasks = RemoteTasks(WorkspaceClient(case.daemon.endpoint, case.daemon.token))
    admitted = tasks.create(
        project_id=case.project_id, capability="vibecomfy.run", spec=spec,
        input_manifest=[row["object_id"] for row in wire.values()], idempotency_key="ra6-video-sdk",
    )
    assert admitted.ok, admitted.error
    task = case.raw.get_task(admitted.data["task_id"])
    assert task.state == "queued"
    receipt = task.spec["spec"]["invocation_preflight"]
    assert receipt["run_inputs"]["source_video"] == expected
    # The standalone file port cannot execute on the attached local-server
    # path. Exercise the actual retained-host rented route up to its target
    # acquisition, using a sentinel in place of a paid provider.
    reached = []
    class TargetBoundary(HostError):
        pass
    def target_boundary(kind):
        reached.append(kind)
        raise TargetBoundary("matching source reached rented target acquisition")
    monkeypatch.setattr(case.host, "_host_target_bindings", target_boundary)
    with pytest.raises(TargetBoundary, match="matching source reached rented target acquisition"):
        case.host.run(once=True)
    assert reached == ["runpod"]
    reached.clear()
    stale = {**task.spec["spec"], "invocation_preflight": {
        **receipt, "api_digest": _wrong_digest("api_digest"),
    }}
    stale_id = _admit(case, stale, wire, key="ra6-video-stale")
    _assert_refused(case, stale_id, "api_digest", lambda: case.host.run(once=True))
    assert reached == []
