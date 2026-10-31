from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import importlib
import zipfile
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.packs.h3_av.actions.transform.run import (
    _execution_request_for_child,
    _generation_intent,
    _invoke,
    _invoke_stage,
    _materialize_output,
    _qualified_execution_request,
    _retrieve_compiled_workflow,
    _import_runtime_file,
    _receipt_target,
)
from astrid.packs.h3_av.actions.transform.operation import OperationJournal
from astrid.packs.h3_av.actions.compile.compile import compile_preparation
from astrid.packs.h3_av.shared.compose import compose_candidate
from astrid.packs.h3_av.actions.prepare.prepare import prepare_request
from astrid.packs.h3_av.shared.request import normalize_request
from astrid.packs.h3_av.actions.verify.verify import VerificationError, _verify_provenance, verify_candidate
from astrid.packs.h3_av.shared.input_bundle import (
    build_input_bundle, bundle_digest, materialize_input_bundle, resolve_preparation_assets,
)
from astrid.packs.vibecomfy.asset_manifest import AssetManifestError, read_archive
from astrid.sdk.results import InvocationResult


def _request():
    return normalize_request(
        {
            "version": 1,
            "operation": "edit",
            "source": {"asset": "source", "range": [0, 1]},
            "output": {"duration": 2},
            "content": {"prompt": "preserve the protected source"},
            "changes": {
                "video": [{"during": [1, 2], "area": {"full_frame": True}, "action": "generate"}],
                "audio": [{"during": [1, 2], "action": "generate"}],
            },
            "references": [],
            "overrides": {},
        }
    )


def test_transform_invokes_children_through_the_connected_client(tmp_path: Path) -> None:
    calls: list[dict[str, object]] = []

    class Client:
        def invoke_result(self, capability_id: str, **kwargs: object):
            calls.append({"capability_id": capability_id, **kwargs})
            return SimpleNamespace(ok=True, capability_id=capability_id)

    result = _invoke(Client(), "h3_av.prepare", inputs={}, out=tmp_path, project="project-1")
    assert result.ok is True
    assert calls[0]["capability_id"] == "h3_av.prepare"
    assert calls[0]["wait"] is True


def test_finalizer_resume_reobserves_same_identity_after_reply_loss(tmp_path: Path) -> None:
    """A lost finalizer reply resumes by Runtime readback, never resubmission."""

    request_digest = "sha256:" + "1" * 64
    journal = OperationJournal(tmp_path / "operation-state.json", request_digest=request_digest)
    calls: list[str] = []
    settled = {"outputs": []}
    task = {
        "task_id": "final-task",
        "run_id": "final-run",
        "attempt_id": "final-attempt",
        "state": "succeeded",
        "result": settled,
    }

    class Tasks:
        def show(self, task_id: str):
            assert task_id == "final-task"
            return SimpleNamespace(ok=True, data=task)

        def list_managed_outputs(self, task_id: str):
            assert task_id == "final-task"
            return SimpleNamespace(ok=True, data=[])

    class Client:
        tasks = Tasks()

        def invoke_result(self, capability_id: str, **kwargs: object):
            calls.append(capability_id)
            return InvocationResult(
                capability_id=capability_id,
                capability_type="executor",
                native_kind="executor",
                ok=True,
                raw_result={
                    "ok": True,
                    "state": "completed",
                    "kernel_run_id": "final-run",
                    "kernel_task_id": "final-task",
                    "kernel_attempt_id": "final-attempt",
                },
                kernel_run_id="final-run",
                kernel_task_id="final-task",
                kernel_attempt_id="final-attempt",
            )

    client = Client()
    inputs = {
        "candidate": {
            "object_id": "sha256:" + "2" * 64,
            "digest": "sha256:" + "2" * 64,
            "filename": "candidate.media",
            "required": True,
        },
        "generation_intent": {"sealed": True},
    }
    first = _invoke_stage(
        client,
        "h3_av.publication_finalizer",
        inputs=inputs,
        out=tmp_path / "finalizer",
        project="project-1",
        saved_result=tmp_path / "finalizer" / "invocation-result.json",
        resume=False,
        journal=journal,
        phase="finalizer",
    )
    second = _invoke_stage(
        client,
        "h3_av.publication_finalizer",
        inputs=inputs,
        out=tmp_path / "finalizer",
        project="project-1",
        saved_result=tmp_path / "finalizer" / "invocation-result.json",
        resume=True,
        journal=journal,
        phase="finalizer",
    )

    assert calls == ["h3_av.publication_finalizer"]
    assert (second.kernel_run_id, second.kernel_task_id, second.kernel_attempt_id) == (
        first.kernel_run_id, first.kernel_task_id, first.kernel_attempt_id,
    )
    assert second.raw_result["task"] == task


def test_nested_execution_request_does_not_inherit_the_parent_input_set() -> None:
    parent = {
        "schema_version": 1,
        "inputs": [{
            "name": "request",
            "object_id": "sha256:" + "a" * 64,
            "digest": "sha256:" + "a" * 64,
            "filename": "request.json",
            "required": True,
        }],
        "target": {"kind": "runpod", "pod_id": "pod-1"},
    }
    child = _execution_request_for_child(parent)
    assert child is not None
    assert "inputs" not in child
    assert child["target"] == parent["target"]


def test_receipt_target_preserves_nested_storage_identity() -> None:
    target = {
        "kind": "runpod",
        "pod_id": "pod-1",
        "storage": {"network_volume_id": "volume-1"},
        "ignored": "field",
    }

    assert _receipt_target(target) == {
        "kind": "runpod",
        "pod_id": "pod-1",
        "storage": {"network_volume_id": "volume-1"},
    }


def test_qualified_execution_request_preserves_existing_nested_storage() -> None:
    from tests.core.execution.test_worker_qualification import _receipt

    qualification = _receipt()
    storage = {"network_volume_id": "volume-1", "mount_path": "/workspace"}
    qualification["target"]["storage"] = storage
    request = {
        "target": {
            "kind": "runpod",
            "pod_id": "pod-1",
            "provider_account_ref": "runpod",
            "storage": {"network_volume_id": "volume-1", "mount_path": "/workspace"},
        },
        "lifecycle": {"mode": "leave_running"},
    }

    admitted, qualified_target = _qualified_execution_request(qualification, request)

    assert admitted["target"]["storage"] == request["target"]["storage"]
    assert admitted["target"]["storage"] is storage
    assert qualified_target["storage"] is storage


def test_qualified_execution_request_rejects_existing_storage_disagreement() -> None:
    from tests.core.execution.test_worker_qualification import _receipt

    qualification = _receipt()
    qualification["target"]["storage"] = {"network_volume_id": "qualified-volume"}
    request = {
        "target": {
            "kind": "runpod",
            "pod_id": "pod-1",
            "provider_account_ref": "runpod",
            "storage": {"network_volume_id": "request-volume"},
        }
    }

    with pytest.raises(ValueError, match="target disagrees"):
        _qualified_execution_request(qualification, request)


def test_generation_intent_comes_only_from_the_sealed_compilation_contract() -> None:
    compilation = {
        "capabilities": {
            "public_generation": {
                "modality": "video",
                "selectors": [{
                    "selector": "main-0",
                    "ordinal": 0,
                    "variant_key": "original",
                    "required": True,
                }],
            }
        }
    }
    intent = _generation_intent(compilation)
    assert intent["metadata"]["compiled_generation_contract"] is True
    assert intent["groups"][0]["selectors"] == compilation["capabilities"]["public_generation"]["selectors"]


def test_generation_intent_rejects_a_manifest_without_a_sealed_contract() -> None:
    with pytest.raises(RuntimeError, match="missing public_generation"):
        _generation_intent({"capabilities": {"output_contract": "muxed_av_full_timeline"}})


def test_managed_output_retrieval_reads_object_and_verifies_digest(tmp_path: Path) -> None:
    payload = b"settled-output"
    digest = "sha256:" + hashlib.sha256(payload).hexdigest()

    class Media:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def read_bytes(self, object_id: str) -> bytes:
            self.calls.append(object_id)
            return payload

    media = Media()
    result = SimpleNamespace(
        capability_id="vibecomfy.run",
        outputs={"managed_outputs": [{"name": "vibecomfy_run", "object_id": digest, "size": len(payload), "filename": "candidate.mp4", "role": "result"}]},
        raw_result={},
    )
    path, row = _materialize_output(SimpleNamespace(media=media), result, "vibecomfy_run", tmp_path)
    assert path.read_bytes() == payload
    assert row["object_id"] == digest
    assert media.calls == [digest]


def _native_edit_preparation(tmp_path: Path) -> dict[str, object]:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source-video")
    request = normalize_request(
        {
            "version": 1,
            "operation": "edit",
            "source": {"asset": "source", "range": [0, 4]},
            "output": {"duration": 4},
            "content": {"prompt": "Replace the selected picture region."},
            "changes": {
                "video": [{"during": [1, 3], "area": {"full_frame": True}, "action": "generate"}],
                "audio": [],
            },
            "references": [],
            "overrides": {"seed": 7, "steps": 12},
        }
    )
    return prepare_request(request, asset_map={"source": str(source)})


class _FakeManagedMedia:
    def __init__(self, objects: dict[str, bytes]) -> None:
        self.objects = objects
        self.reads: list[str] = []

    def read_bytes(self, object_id: str) -> bytes:
        self.reads.append(object_id)
        return self.objects[object_id]


def _compile_result_with_managed_bundle(tmp_path: Path):
    compiled = compile_preparation(_native_edit_preparation(tmp_path), out_dir=tmp_path / "compiled")
    objects: dict[str, bytes] = {}
    rows: list[dict[str, object]] = []
    outputs = {
        "compilation": (Path(compiled["manifest_path"]).read_bytes(), "compilation.json"),
        "managed_assets": (Path(compiled["managed_assets"]["path"]).read_bytes(), "managed-assets.zip"),
        "python": (Path(compiled["workflow"]["workflow.py"]["path"]).read_bytes(), "workflow.py"),
        "companion": (Path(compiled["workflow"]["workflow.vibe.json"]["path"]).read_bytes(), "workflow.vibe.json"),
        "source": (Path(compiled["workflow"]["source.json"]["path"]).read_bytes(), "source.json"),
    }
    for name, (payload, filename) in outputs.items():
        digest = "sha256:" + hashlib.sha256(payload).hexdigest()
        objects[digest] = payload
        rows.append({"name": name, "object_id": digest, "digest": digest, "size": len(payload), "filename": filename, "role": "result"})
    return compiled, SimpleNamespace(capability_id="h3_av.compile", outputs={"managed_outputs": rows}, raw_result={}), _FakeManagedMedia(objects)


def test_connected_client_uses_selected_native_edit_bundle_and_attests_provenance(tmp_path: Path) -> None:
    compiled, result, media = _compile_result_with_managed_bundle(tmp_path)

    inputs = _retrieve_compiled_workflow(
        SimpleNamespace(media=media),
        result,
        json.loads(Path(compiled["manifest_path"]).read_text(encoding="utf-8")),
        tmp_path / "retrieved",
    )

    assert set(inputs) == {"python", "companion", "source"}
    assert b"H3V2VGranularFractionalDenoise" in media.objects[inputs["python"]["object_id"]]
    assert media.reads == [
        inputs["python"]["object_id"],
        inputs["companion"]["object_id"],
        inputs["source"]["object_id"],
    ]


@pytest.mark.parametrize("mismatch", ["payload", "manifest"])
def test_connected_client_fails_closed_on_workflow_hash_or_provenance_mismatch(tmp_path: Path, mismatch: str) -> None:
    compiled, result, media = _compile_result_with_managed_bundle(tmp_path)
    compilation = json.loads(Path(compiled["manifest_path"]).read_text(encoding="utf-8"))
    if mismatch == "payload":
        python_digest = next(row["object_id"] for row in result.outputs["managed_outputs"] if row["name"] == "python")
        media.objects[python_digest] = b"tampered-native-edit-workflow"
    else:
        compilation["workflow"]["workflow.py"]["sha256"] = "0" * 64

    with pytest.raises(RuntimeError, match="digest|hash/provenance"):
        _retrieve_compiled_workflow(SimpleNamespace(media=media), result, compilation, tmp_path / "retrieved")


def test_interval_lists_cannot_certify_a_copied_non_media_candidate(tmp_path: Path) -> None:
    source = tmp_path / "source.bin"
    candidate = tmp_path / "candidate.bin"
    source.write_bytes(b"source-bytes")
    candidate.write_bytes(source.read_bytes())
    preparation = prepare_request(_request(), asset_map={"source": str(source)})
    composition = compose_candidate(
        preparation=preparation,
        generated=candidate,
        source=source,
        out_dir=tmp_path / "composition",
        preservation_evidence={
            "unchanged_permissions": {
                "video": preparation["mask_schedule"]["video"]["protected_intervals"],
                "audio": preparation["mask_schedule"]["audio"]["protected_intervals"],
            }
        },
    )
    with pytest.raises(VerificationError, match="ffprobe|byte-identical|decodable media|protected_samples"):
        verify_candidate(preparation=preparation, composition=composition, source=source)


@pytest.mark.ffmpeg
def test_composition_carries_provenance_and_decoded_protected_samples(tmp_path: Path) -> None:
    source = tmp_path / "source.mp4"
    generated = tmp_path / "generated.mp4"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "color=c=blue:s=1024x576:r=24", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-t", "1", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(source)],
        check=True,
    )
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "color=c=red:s=1024x576:r=24", "-f", "lavfi", "-i", "sine=frequency=880:sample_rate=48000", "-t", "2", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(generated)],
        check=True,
    )
    preparation = prepare_request(_request(), asset_map={"source": str(source)})
    preparation["provenance"] = {
        "request_digest": preparation["request_digest"],
        "assets": preparation["assets"],
        "graph": {"compilation_digest": "graph-digest"},
    }
    composition = compose_candidate(preparation=preparation, generated=generated, source=source, out_dir=tmp_path / "composition")
    report = verify_candidate(preparation=preparation, composition=composition, source=source)
    assert report["preservation"]["status"] == "protected_sample_evidence"
    assert composition["provenance"]["graph"]["compilation_digest"] == "graph-digest"
    assert composition["preservation_evidence"]["protected_samples"]["video"]


@pytest.mark.ffmpeg
def test_full_replacement_composition_trims_generated_av_to_request_duration(tmp_path: Path) -> None:
    source = tmp_path / "silent-source.mp4"
    generated = tmp_path / "overlong-generated.mp4"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "color=c=blue:s=1024x576:r=24:d=2", "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(source)],
        check=True,
    )
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "color=c=red:s=1024x576:r=24:d=2.083333333", "-f", "lavfi", "-i", "sine=frequency=880:sample_rate=48000:duration=2.1", "-map", "0:v:0", "-map", "1:a:0", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(generated)],
        check=True,
    )
    request = normalize_request({
        "version": 1,
        "operation": "edit",
        "source": {"asset": "source", "range": [0, 2]},
        "output": {"duration": 2},
        "content": {"prompt": "Replace the full source frame and audio."},
        "changes": {
            "video": [{"during": [0, 2], "area": {"full_frame": True}, "action": "generate"}],
            "audio": [{"during": [0, 2], "action": "generate"}],
        },
        "references": [],
        "overrides": {},
    })
    preparation = prepare_request(request, asset_map={"source": str(source)})
    assert not any(
        preparation["mask_schedule"][domain]["protected_intervals"]
        for domain in ("video", "audio")
    )

    composition = compose_candidate(
        preparation=preparation,
        generated=generated,
        source=source,
        out_dir=tmp_path / "full-replacement",
    )

    assert composition["composition"]["method"] == "h3-full-replacement-trim-v1"
    assert composition["candidate"]["sha256"] != hashlib.sha256(generated.read_bytes()).hexdigest()
    assert composition["coverage"]["candidate"]["video"]["frames"] == 48
    assert composition["coverage"]["candidate"]["audio"]["duration"] == pytest.approx(2, abs=0.05)


@pytest.mark.ffmpeg
def test_protected_audio_witness_tolerates_aac_to_pcm_roundtrip(tmp_path: Path) -> None:
    """AAC source audio must verify after concat emits PCM in another container."""
    source = tmp_path / "source-32k-aac.mp4"
    generated = tmp_path / "generated-32k-aac.mp4"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "color=c=blue:s=1024x576:r=24", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=32000", "-t", "1", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(source)],
        check=True,
    )
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "color=c=red:s=1024x576:r=24", "-f", "lavfi", "-i", "sine=frequency=880:sample_rate=32000", "-t", "2", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(generated)],
        check=True,
    )
    preparation = prepare_request(_request(), asset_map={"source": str(source)})
    composition = compose_candidate(
        preparation=preparation,
        generated=generated,
        source=source,
        out_dir=tmp_path / "composition",
    )
    report = verify_candidate(preparation=preparation, composition=composition, source=source)

    audio_evidence = composition["preservation_evidence"]["protected_samples"]["audio"][0]
    assert audio_evidence["method"] == "ffmpeg-decoded-audio-similarity-v1"
    assert audio_evidence["similarity"] >= 0.985
    assert report["status"] == "verified"


@pytest.mark.ffmpeg
def test_composition_normalizes_generated_working_resolution_to_source(tmp_path: Path) -> None:
    source = tmp_path / "source.mp4"
    generated = tmp_path / "generated.mp4"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "color=c=blue:s=1024x576:r=24", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-t", "1", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(source)],
        check=True,
    )
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "color=c=red:s=32x32:r=24", "-f", "lavfi", "-i", "sine=frequency=880:sample_rate=48000", "-t", "2", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(generated)],
        check=True,
    )
    preparation = prepare_request(_request(), asset_map={"source": str(source)})
    composition = compose_candidate(
        preparation=preparation,
        generated=generated,
        source=source,
        out_dir=tmp_path / "composition",
    )
    probe = json.loads(
        subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height", "-of", "json", composition["candidate"]["path"]],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    )
    stream = probe["streams"][0]
    assert (stream["width"], stream["height"]) == (1024, 576)


def test_input_bundle_contains_only_declared_source_refs_and_masks(tmp_path: Path):
    raw = _request().value
    raw["references"] = [{"asset": "reference", "purpose": "appearance"}]
    raw["changes"]["video"][0]["area"] = {"mask_asset": "region"}
    raw["changes"]["video"][0]["mask_asset"] = "temporal"
    raw["changes"]["audio"][0]["mask_asset"] = "audio"
    request = normalize_request(raw)
    assets = {}
    for name in ("source", "reference", "region", "temporal", "audio", "unused"):
        path = tmp_path / f"{name}.bin"
        path.write_bytes(name.encode())
        assets[name] = str(path)
    bundle = build_input_bundle(request, assets, tmp_path / "inputs.zip")
    paths, identities = materialize_input_bundle(request, bundle, tmp_path / "attempt")
    assert set(paths) == {"source", "reference", "region", "temporal", "audio"}
    assert all(Path(path).read_bytes() == name.encode() for name, path in paths.items())
    assert all("path" not in row for row in identities)
    preparation = {"request": request.value, "assets": identities, "input_bundle_sha256": bundle_digest(bundle)}
    preparation["assets"][0]["sha256"] = "0" * 64
    with pytest.raises(ValueError, match="asset evidence"):
        resolve_preparation_assets(preparation, bundle, tmp_path / "compile")


def test_managed_v2_mask_asset_is_rehydrated_for_composition(tmp_path: Path):
    from PIL import Image
    from astrid.packs.h3_av.shared.compose import _spatial_video_masks

    raw = {
        "version": 2,
        "prompt": "Edit the declared region.",
        "duration": 1,
        "media": [{
            "id": "baseline",
            "asset": "baseline.mp4",
            "role": "timeline",
            "modality": "video",
            "at": {"frame": 0},
            "range": [0, 1],
            "edit": [{
                "stream": "video",
                "during": [0, 0.5],
                "mask": {"asset": "region.png"},
            }],
        }],
        "settings": {},
    }
    request = normalize_request(raw)
    source = tmp_path / "baseline.mp4"
    source.write_bytes(b"timeline fixture")
    mask = tmp_path / "region.png"
    Image.new("L", (8, 8), 255).save(mask)
    bundle = build_input_bundle(
        request,
        {"baseline.mp4": str(source), "region.png": str(mask)},
        tmp_path / "input-bundle.zip",
    )

    preparation = prepare_request(
        request,
        asset_map={"baseline.mp4": str(source), "region.png": str(mask)},
    )
    _, preparation["assets"] = materialize_input_bundle(
        request, bundle, tmp_path / "prepare-assets"
    )
    preparation["input_bundle_sha256"] = bundle_digest(bundle)
    restored = resolve_preparation_assets(preparation, bundle, tmp_path / "compose-assets")

    masks = _spatial_video_masks(restored)
    assert len(masks) == 1
    assert masks[0]["asset"] == "region.png"
    assert Path(masks[0]["path"]).read_bytes() == mask.read_bytes()


def test_verification_asset_provenance_survives_attempt_local_bundle_rebinding(tmp_path: Path) -> None:
    request = _request()
    source = tmp_path / "source.mp4"
    source.write_bytes(b"portable source identity")
    bundle = build_input_bundle(request, {"source": str(source)}, tmp_path / "input-bundle.zip")
    _, identities = materialize_input_bundle(request, bundle, tmp_path / "initial-materialization")
    preparation = prepare_request(request, asset_map={"source": str(source)})
    preparation["assets"] = identities
    preparation["input_bundle_sha256"] = bundle_digest(bundle)
    composition = {
        "request_digest": preparation["request_digest"],
        "schedule_digest": preparation["mask_schedule"]["digest"],
        "provenance": {
            "request_digest": preparation["request_digest"],
            "assets": identities,
        },
    }

    compose_attempt = resolve_preparation_assets(
        preparation, bundle, tmp_path / "compose-attempt" / "request-assets"
    )
    verify_attempt = resolve_preparation_assets(
        preparation, bundle, tmp_path / "verify-attempt" / "request-assets"
    )
    assert compose_attempt["assets"][0]["path"] != verify_attempt["assets"][0]["path"]
    _verify_provenance(compose_attempt, composition)
    _verify_provenance(verify_attempt, composition)

    tampered = json.loads(json.dumps(composition))
    tampered["provenance"]["assets"][0]["sha256"] = "0" * 64
    with pytest.raises(VerificationError, match="asset provenance does not match"):
        _verify_provenance(verify_attempt, tampered)


def test_missing_dependency_stops_transform_before_any_import_or_submission(tmp_path: Path, monkeypatch):
    from astrid.packs.h3_av.actions.transform import run as transform
    from unittest.mock import Mock

    request_path = tmp_path / "request.json"
    request_path.write_text(json.dumps(_request().value))
    asset_map = tmp_path / "asset-map.json"
    asset_map.write_text("{}")
    execution_request = tmp_path / "execution-request.json"
    execution_request.write_text(json.dumps({"target": {"kind": "runpod", "pod_id": "pod-test"}}))
    client = Mock()
    monkeypatch.setattr(transform.AstridClient, "open_from_launcher", lambda **kwargs: nullcontext(client))
    with pytest.raises(ValueError, match="needs a file in asset_map"):
        transform.run_transform(transform.build_parser().parse_args([
            "--request", str(request_path), "--asset-map", str(asset_map), "--out", str(tmp_path / "out"),
            "--execution-request", str(execution_request),
        ]))
    client.invoke_result.assert_not_called()
    client.media.import_file.assert_not_called()


def test_import_rejects_object_identity_different_from_uploaded_bytes(tmp_path: Path):
    path = tmp_path / "request.json"
    path.write_bytes(b"{}")
    client = SimpleNamespace(media=SimpleNamespace(import_file=lambda **kwargs: SimpleNamespace(
        ok=True, data={"object_id": "sha256:" + "0" * 64}
    )))
    with pytest.raises(RuntimeError, match="different digest"):
        _import_runtime_file(client, project="project", path=path, filename=path.name)


@pytest.mark.parametrize("mutation", ["payload", "traversal", "undeclared"])
def test_input_bundle_rejects_tampering_before_materialization(tmp_path: Path, mutation: str):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    bundle = build_input_bundle(_request(), {"source": str(source)}, tmp_path / "inputs.zip")
    resolved = read_archive(bundle)
    manifest = resolved.manifest
    member = manifest["assets"][0]["member"]
    payload = b"changed" if mutation == "payload" else b"source"
    if mutation == "traversal":
        member = "../escaped"
        manifest["assets"][0]["member"] = member
    with zipfile.ZipFile(bundle, "w") as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        archive.writestr(member, payload)
        if mutation == "undeclared":
            archive.writestr("extra", b"undeclared")
    with pytest.raises(AssetManifestError):
        materialize_input_bundle(_request(), bundle, tmp_path / "attempt")
    assert not (tmp_path / "attempt").exists()


def _synthetic_av(path: Path, duration: float, *, color: str, muxed=True):
    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", f"color=c={color}:s=1024x576:r=24",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
        "-t", str(duration), "-c:v", "libx264", "-pix_fmt", "yuv420p",
        *(["-c:a", "aac"] if muxed else ["-an"]), str(path),
    ], check=True)


@pytest.mark.parametrize("reference_count", [1, 9])
@pytest.mark.integration
def test_generation_prompt_socket_type_survives_canonical_reload(tmp_path: Path, reference_count: int):
    from astrid.packs.h3_av.shared.generation import build_generation_workflow
    from vibecomfy.security.provenance import Provenance
    from vibecomfy.workflow_bundle import emit_bundle, load_bundle

    workflow = build_generation_workflow(
        references=[f"reference-{index}.png" for index in range(reference_count)],
        prompt="Preserve the reference identity.", frames=124,
        model="minimax_h3_ref2va_pruned_int8_convrot.safetensors", steps=8, seed=7,
    )
    path = tmp_path / "workflow.py"
    emit_bundle(workflow, path, provenance=Provenance.AGENT_AUTHORED)
    reloaded = load_bundle(path, trust=Provenance.USER_CONFIRMED).workflow

    expected_types = {
        "clip": "CLIP", "vae": "VAE", "audio_vae": "VAE", "prompt": "STRING",
        "width": "INT", "height": "INT", "length": "INT", "ref_image_size": "COMBO",
        **{f"ref_images.ref_image_{index}": "IMAGE" for index in range(reference_count)},
    }
    for graph in (workflow, reloaded):
        target = graph.nodes["target"]
        assert dict(zip(target.native_input_names, target.native_input_types, strict=True)) == expected_types
        assert target.native_output_names == ["positive", "LATENT"]
        assert target.native_output_types == ["CONDITIONING", "LATENT"]
        assert {edge.to_input for edge in graph.edges if edge.to_node == "target" and edge.to_input.startswith("ref_images.")} == {
            f"ref_images.ref_image_{index}" for index in range(reference_count)
        }
        assert graph.inputs["prompt"].type == "STRING"
        assert graph.inputs["prompt"].node_id == "target"
        assert graph.inputs["prompt"].field == "prompt"


@pytest.mark.parametrize(("operation", "finalizer_state", "placement", "verification_fault"), [
    ("continue", "passed", "runpod", None),
    ("edit", "passed", "runpod", None),
    ("generate", "passed", "runpod", None),
    ("continue", "rejected", "runpod", None),
    ("continue", "unclaimed", "runpod", None),
    ("character", "passed", "runpod", None),
    ("generate", "passed", "local", None),
    *[("generate", "passed", "local", fault) for fault in (
        "ffprobe_missing", "candidate",
        "forged_verify_status", "forged_verify_request", "forged_verify_candidate",
        "candidate_after_verify",
    )],
    *[("continue", "passed", "local", fault) for fault in (
        "ffprobe_missing", "ffprobe_exit", "ffprobe_empty", "ffprobe_garbled", "ffprobe_unusable",
        "forged_witness_method", "forged_witness_digest", "schedule", "candidate",
    )],
    ("edit", "passed", "local", "mask"),
    ("ordered", "passed", "local", None),
    ("ordered-overlap", "passed", "local", None),
    ("ordered", "passed", "local", "candidate"),
])
@pytest.mark.integration
@pytest.mark.ffmpeg
def test_transform_managed_handoffs_survive_removal_of_every_previous_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str, finalizer_state: str,
    placement: str, verification_fault: str | None, request: pytest.FixtureRequest,
):
    """Run the real H3 stages through admission/materialization; fake only CAS
    transport and GPU generation. No preceding directory survives a handoff.
    """
    import yaml
    from astrid.core.contracts.errors import AstridError
    from astrid.core.execution.generic_host import GenericPackHost
    from astrid.packs.h3_av.actions.transform import run as transform
    from astrid.sdk.invocation import _kernel_invoke

    caller = tmp_path / "caller"
    caller.mkdir()
    source = caller / "source.mp4"
    ordered = operation.startswith("ordered")
    _synthetic_av(source, 2 if operation == "edit" or ordered else 1, color="blue")
    source_bytes = source.read_bytes()
    raw = _request().value
    raw["operation"] = "edit" if ordered else operation
    if operation == "edit":
        raw["source"]["range"] = [0, 2]
        raw["changes"]["audio"] = []
    asset_map = {"source": str(source)}
    if ordered:
        raw["source"]["range"] = [0, 2]
        raw["changes"] = {"video": [
            {"during": [0.5, 1], "area": {"rectangle": [0, 0, 256, 288]}, "action": "generate"},
            {"during": [0, 0.5], "area": {"full_frame": True}, "action": "preserve"},
            {"during": [1, 2], "area": {"rectangle": [512, 288, 256, 288]}, "action": "generate"},
        ], "audio": []}
        if operation == "ordered-overlap":
            raw["changes"]["video"][2].update(during=[0.5, 2], area={"rectangle": [128, 144, 640, 432]})
    if verification_fault == "mask":
        from PIL import Image
        mask = caller / "region.png"
        Image.new("L", (1024, 576), 255).save(mask)
        raw["changes"]["video"][0].update(
            area={"rectangle": [0, 0, 1024, 576]}, mask_asset=mask.name,
        )
        asset_map[mask.name] = str(mask)
    if operation == "character":
        from PIL import Image
        _synthetic_av(source, 15, color="blue")
        source_bytes = source.read_bytes()
        performance = caller / "performance.mp4"
        _synthetic_av(performance, 15, color="green")
        replacement = caller / "replacement.png"
        Image.new("RGB", (32, 32), (200, 40, 80)).save(replacement)
        raw = {
            "version": 2, "duration": 15,
            "prompt": "Replace the person in <Video 1> with the person in <Picture 1>.",
            "media": [
                {"id": "baseline", "asset": "baseline.mp4", "role": "timeline", "modality": "video",
                 "at": {"frame": 0}, "range": [0, 15],
                 "edit": [{"stream": "video", "during": [0, 15], "mask": {"full_frame": True}}]},
                {"id": "performance", "asset": "performance.mp4", "role": "reference",
                 "modality": "video", "range": [0, 15], "audio": False},
                {"id": "replacement", "asset": "replacement.png", "role": "reference", "modality": "image"},
            ],
            "settings": {"adapter": {"id": "character_swap_v1"}, "turbo": {"enabled": True}},
        }
        asset_map = {"baseline.mp4": str(source), "performance.mp4": str(performance),
                     "replacement.png": str(replacement)}
    if operation == "generate":
        from PIL import Image
        raw["source"] = None
        raw["changes"] = {"video": [], "audio": []}
        raw["references"] = []
        raw["output"] = {"duration": 15}
        asset_map = {}
        for index in range(4):
            path = caller / f"reference-{index}.png"
            Image.new("RGB", (32, 32), (index * 50, 0, 0)).save(path)
            asset_map[path.name] = str(path)
            raw["references"].append({"asset": path.name, "purpose": "appearance"})
    (caller / "request.json").write_text(json.dumps(raw))
    (caller / "asset-map.json").write_text(json.dumps(asset_map))
    objects: dict[str, bytes] = {}
    calls = []
    attempts = []
    pass_sources, pass_verifications, fake_submissions, pass_preparations = [], [], [], []
    pack = Path(transform.__file__).resolve().parents[2]

    # The real validator now requires the worker-attested model-root binding
    # for model-bearing H3 bundles. Keep this CPU test offline by making a
    # small nonempty fixture for every model family used by the packaged H3
    # workflows; no model service or provider operation is involved.
    from astrid.core.generation.model_root import (
        MODEL_ROOT_BINDING_ENV,
        MODEL_ROOT_ENV,
        canonical_model_inventory_digest,
    )

    model_root = tmp_path / "models"
    model_files = {
        "diffusion_models/minimax_h3_ref2va_pruned_int8_convrot.safetensors": b"h3-ref2va",
        "diffusion_models/minimax_h3_fl2va_pruned_fp8_scaled.safetensors": b"h3-fl2va",
        "text_encoders/qwen3vl_32b_minimax_h3_int8_convrot.safetensors": b"h3-text",
        "vae/minimax_h3_video_vae_int8_convrot.safetensors": b"h3-video-vae",
        "vae/minimax_h3_video_vae_fp16.safetensors": b"h3-video-vae-fp16",
        "vae/minimax_h3_audio_vae_fp32.safetensors": b"h3-audio-vae",
        "loras/minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors": b"h3-lora",
        "loras/h3_character_swap_pro4500_1000.safetensors": b"h3-character-lora",
    }
    inventory = []
    for relative, payload in sorted(model_files.items()):
        path = model_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        inventory.append({
            "name": path.name,
            "sha256": "sha256:" + hashlib.sha256(payload).hexdigest(),
            "size": len(payload),
            "subdir": path.parent.relative_to(model_root).as_posix(),
        })
    binding = {
        "schema_version": 1,
        "path": str(model_root),
        "inventory": inventory,
        "inventory_digest": canonical_model_inventory_digest(inventory),
    }
    monkeypatch.setenv(MODEL_ROOT_ENV, str(model_root))
    monkeypatch.setenv(MODEL_ROOT_BINDING_ENV, json.dumps(binding, sort_keys=True))
    if operation == "character":
        # Use the existing, strict, source-bound synthetic CPU model policy.
        # This is test-fixture provenance, not real GPU/T9 readiness evidence;
        # no validator or model digest function is mocked.
        from astrid.packs.h3_av.shared import generation
        source_root = Path(generation.__file__).resolve().parents[4]
        fixture_relative = "tests/fixtures/h3_cpu/approved_character_swap_cpu_boundary.py"
        pinned = [fixture_relative, "astrid/packs/h3_av/shared/generation.py",
                  "astrid/packs/h3_av/actions/compile/compile.py"]
        source_pins = {relative: hashlib.sha256((source_root / relative).read_bytes()).hexdigest()
                       for relative in pinned}
        candidate = tmp_path / "synthetic-only-candidate.json"
        candidate.write_text(json.dumps({
            "schema": "astrid-h3-candidate-source-manifest/v1",
            "synthetic_inference": True,
            "synthetic_inference_classification": "synthetic-inference-only",
            "source_files": source_pins,
        }, sort_keys=True))
        profile = tmp_path / "synthetic-only-model-profile.json"
        profile.write_text(json.dumps({
            "schema_version": "hc03-worker-readiness.v1", "status": "ready",
            "launch": {"model_root": binding},
            "t9_model_substitute": {
                "approved": True, "mode": "deterministic_cpu_model_boundary_v1",
                "purpose": "h3_character_swap_public_runtime_cpu_acceptance_v1",
                "candidate_manifest_path": str(candidate.resolve()),
                "candidate_manifest_sha256": hashlib.sha256(candidate.read_bytes()).hexdigest(),
                "source_path": str((source_root / fixture_relative).resolve()),
                "source_sha256": "sha256:" + source_pins[fixture_relative],
                "model_asset": next(row for row in inventory if row["name"] == generation.CHARACTER_SWAP_FILENAME),
            },
        }, sort_keys=True))
        monkeypatch.setenv("ASTRID_H3_CANDIDATE_MANIFEST", str(candidate))
        monkeypatch.setenv("ASTRID_H3_T9_MODEL_SUBSTITUTE_PROFILE", str(profile))
        expected = hashlib.sha256(model_files["loras/" + generation.CHARACTER_SWAP_FILENAME]).hexdigest()
        assert generation.character_swap_asset_sha256() == expected

    def settle(path, name, **extra):
        payload = Path(path).read_bytes()
        digest = "sha256:" + hashlib.sha256(payload).hexdigest()
        objects[digest] = payload
        return {"name": name, "filename": Path(path).name, "object_id": digest,
                "digest": digest, "size": len(payload), **extra}

    class Media:
        def import_file(self, *, project, path, idempotency_key):
            assert project == "cpu-test"
            return SimpleNamespace(ok=True, data=settle(path, "input"))

        def read_bytes(self, digest):
            return objects[digest]

    class Tasks:
        def create(self, **kwargs):
            self.admitted = kwargs
            return SimpleNamespace(ok=True, data={"task_id": "task-cpu", "run_id": "run-cpu"})

    class Client:
        media = Media()
        tasks = Tasks()
        compilation = None

        def invoke_result(self, capability_id, *, inputs, **kwargs):
            calls.append((capability_id, inputs))
            attempt = tmp_path / f"attempt-{len(calls)}-{capability_id}"
            attempts.append(attempt)
            attempt.mkdir()
            out = attempt / "outputs"
            out.mkdir()
            if capability_id == "h3_av.publication_finalizer":
                if finalizer_state == "rejected":
                    shutil.rmtree(attempt)
                    raise RuntimeError("final publication rejected")
                assert set(inputs) == {
                    "candidate", "verification", "expected_request_digest", "generation_intent"
                }
                intent = inputs["generation_intent"]
                candidate = inputs["candidate"]
                verification = inputs["verification"]
                assert isinstance(intent, dict)
                assert isinstance(candidate, dict)
                assert isinstance(verification, dict)
                assert inputs["expected_request_digest"] == intent["metadata"]["h3_av"]["request_digest"]
                candidate_id = candidate["object_id"]
                candidate_bytes = objects[candidate_id]
                finalized = None
                if operation == "character":
                    # Fake only transport/publication settlement; execute the
                    # actual finalizer over freshly localized managed inputs.
                    declaration = yaml.safe_load((pack / "pack.yaml").read_text())["actions"]["publication_finalizer"]
                    from astrid import sdk
                    from astrid.sdk.invocation import get_capability
                    from astrid.core.execution.generic_host import _generation_selector_declarations
                    registries = sdk._load_registries(include_elements=False)
                    capability = get_capability(capability_id, kind="executor", include_elements=False, _registries=registries)
                    ports = capability.inputs
                    _kernel_invoke(capability, kind="executor", project="cpu-test", inputs=inputs, outputs={},
                                   generation_intent=intent, _client=self)
                    port, declarations = _generation_selector_declarations(
                        SimpleNamespace(definition=registries[0].get(capability_id)), intent)
                    assert port == "verified_candidate"
                    assert declarations[0]["output_port"] == port
                    assert self.tasks.admitted["settlement_effect"]["payload"]["groups"][0]["selectors"][0]["output_port"] == port
                    assert [output.name for output in capability.outputs] == ["verified_candidate", "manifest"]
                    host = GenericPackHost(pack_roots=[], client=SimpleNamespace(
                        get_object=lambda digest: objects["sha256:" + digest.removeprefix("sha256:")]))
                    localized = host._materialize_inputs(
                        {"spec": self.tasks.admitted["spec"], "input_object_ids": self.tasks.admitted["input_manifest"]}, attempt,
                        file_input_names=frozenset(port.name for port in ports if port.type == "file"))
                    module = importlib.import_module("astrid.packs.h3_av.actions.publication_finalizer.run")
                    argv = [value.format_map({**localized, "out": str(out)})
                            for value in declaration["invocation"]["command"]["argv"][3:]]
                    assert module.main(argv) == 0
                    finalized = json.loads((out / "manifest.json").read_text())["outputs"][0]
                    assert (out / finalized["path"]).read_bytes() == candidate_bytes
                    assert finalized["content_hash"] == candidate_id
                    assert candidate_bytes.startswith(b"\x1aE\xdf\xa3")
                    assert finalized["role"] == "result"
                    assert finalized["media_type"] == "video/x-matroska"
                final_row = {
                    "name": "verified_candidate",
                    "filename": "verified-candidate.mp4",
                    "kind": "object",
                    "role": "result",
                    "media_type": "video/mp4",
                    "size": len(candidate_bytes),
                    "digest": candidate_id,
                    "object_id": candidate_id,
                    "output_port": "verified_candidate",
                    "group_key": "main",
                    "variant_key": "final-composition",
                    "ordinal": 0,
                }
                if finalized is not None:
                    final_row["filename"] = finalized["path"]
                    final_row["media_type"] = finalized["media_type"]
                    assert final_row["size"] == finalized["bytes"]
                    assert final_row["output_port"] == finalized["output_port"]
                selector = {
                    **intent["groups"][0]["selectors"][0],
                    "output_port": "verified_candidate",
                }
                effect = {
                    "effect_type": "generation.publish_v1",
                    "target_id": "cpu-test",
                    "payload": {
                        "version": 1,
                        "modality": intent["modality"],
                        "generation_type": "h3_av.publication_finalizer",
                        "metadata": intent["metadata"],
                        "partial_success_policy": intent["partial_success_policy"],
                        "groups": [{"group_key": "main", "selectors": [selector]}],
                    },
                }
                publication = {
                    "effect_type": "generation.publish_v1",
                    "publications": [{
                        "group_key": "main",
                        "generation_id": "generation-final",
                        "missing_selectors": [],
                        "variants": [{
                            "generation_id": "generation-final",
                            "variant_id": "variant-final",
                            "object_id": candidate_id,
                            "output_port": "verified_candidate",
                            "ordinal": 0,
                            "variant_key": "final-composition",
                        }],
                    }],
                }
                settled = {"outputs": [final_row], "generation_publish_v1": publication}
                managed = {
                    **final_row,
                    "association_id": "association-final",
                    "task_id": "task-final",
                    "run_id": "run-final",
                    "attempt_id": "attempt-final",
                    "project_id": "cpu-test",
                    "generation_id": "generation-final",
                }
                task = {
                    "task_id": "task-final",
                    "run_id": "run-final",
                    "attempt_id": "attempt-final",
                    "project_id": "cpu-test",
                    "state": "succeeded",
                    "generation_intent": intent,
                    "expected_effect": effect,
                    "result": settled,
                }
                raw_result = {
                    "ok": True,
                    "state": "completed",
                    "kernel_run_id": "run-final",
                    "kernel_task_id": "task-final",
                    "kernel_attempt_id": "attempt-final",
                    "task": task,
                    "result": settled,
                    "outputs": {"artifacts": [final_row]},
                    "managed_outputs": [managed],
                }
                if verification_fault == "candidate_after_verify":
                    candidates = [path for path in (tmp_path / "parent/05-compose/retrieved").iterdir()
                                  if path.suffix != ".json"]
                    assert len(candidates) == 1
                    candidates[0].write_bytes(b"changed after successful verification")
                shutil.rmtree(attempt)
                return InvocationResult(
                    capability_id=capability_id,
                    capability_type="executor",
                    native_kind="executor",
                    ok=True,
                    outputs={"artifacts": [final_row], "managed_outputs": [managed]},
                    raw_result=raw_result,
                    kernel_task_id="task-final",
                    kernel_run_id="run-final",
                    kernel_attempt_id="attempt-final",
                )
            if capability_id.startswith("h3_av."):
                stage = capability_id.split(".")[1]
                manifest = yaml.safe_load((pack / "pack.yaml").read_text())["actions"][stage]
                ports = [SimpleNamespace(**port) for port in manifest["inputs"]]
                capability = SimpleNamespace(id=capability_id, capability_type="executor", inputs=ports)
                _kernel_invoke(capability, kind="executor", project="cpu-test", inputs=inputs, outputs={}, _client=self)
                admission = self.tasks.admitted
                # This tests the actual SDK envelope and host materializer.
                host = GenericPackHost(pack_roots=[], client=SimpleNamespace(
                    get_object=lambda digest: objects["sha256:" + digest.removeprefix("sha256:")]
                ))
                localized = host._materialize_inputs(
                    {"spec": admission["spec"], "input_object_ids": admission["input_manifest"]}, attempt,
                    file_input_names=frozenset(port.name for port in ports if port.type == "file"),
                )
                assert "/Users/" not in json.dumps(inputs)
                assert all(isinstance(value, dict) and value.get("digest") for value in inputs.values())
                if stage == "prepare":
                    if caller.exists():
                        shutil.rmtree(caller)
                command = manifest["invocation"]["command"]
                bindings = {**localized, "out": str(out), "python_exec": "unused"}
                argv = [value.format_map(bindings) for value in command["argv"][3:]]
                for arg in command.get("input_args", []):
                    if arg["input"] in localized:
                        argv.extend([arg["flag"], str(localized[arg["input"]])])
                module = importlib.import_module(f"astrid.packs.h3_av.actions.{stage}.run")
                if stage == "verify" and verification_fault and verification_fault.startswith("ffprobe_"):
                    # Break only the external probe at the real verification
                    # boundary; compose and the verifier itself remain real.
                    from astrid.packs.h3_av.actions.verify import verify as verifier
                    real_which, real_run = shutil.which, subprocess.run
                    with monkeypatch.context() as probe_patch:
                        if verification_fault == "ffprobe_missing":
                            probe_patch.setattr(verifier.shutil, "which", lambda name: (
                                None if name == "ffprobe" else real_which(name)
                            ))
                        else:
                            def failed_probe(command, *args, **kwargs):
                                if Path(command[0]).name != "ffprobe":
                                    return real_run(command, *args, **kwargs)
                                if verification_fault == "ffprobe_exit":
                                    raise subprocess.CalledProcessError(1, command)
                                output = {
                                    "ffprobe_empty": "", "ffprobe_garbled": "{not json",
                                    "ffprobe_unusable": '{"streams": []}',
                                }[verification_fault]
                                return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")
                            probe_patch.setattr(verifier.subprocess, "run", failed_probe)
                        module.main(argv)
                    pytest.fail("verification accepted an unusable ffprobe")
                assert module.main(argv) == 0
                if stage == "verify" and verification_fault and verification_fault.startswith("forged_verify_"):
                    report_path = out / "verification.json"
                    report = json.loads(report_path.read_text())
                    field, value = {
                        "forged_verify_status": ("status", "failed"),
                        "forged_verify_request": ("request_digest", "sha256:" + "0" * 64),
                        "forged_verify_candidate": ("candidate_sha256", "0" * 64),
                    }[verification_fault]
                    report[field] = value
                    report_path.write_text(json.dumps(report))
                if stage == "compose" and verification_fault:
                    composition_path = out / "composition-manifest.json"
                    composition = json.loads(composition_path.read_text())
                    if verification_fault.startswith("forged_witness_"):
                        witness = composition["preservation_evidence"]["protected_samples"]["video"][0]
                        if verification_fault == "forged_witness_method":
                            witness.update(method="custom-self-attested-v1", witness_digest="deadbeef")
                        else:
                            witness["source_sha256"] = witness["candidate_sha256"] = "0" * 64
                    elif verification_fault == "schedule":
                        composition["schedule_digest"] = "0" * 64
                    elif verification_fault == "candidate":
                        composition["candidate"]["sha256"] = "0" * 64
                    elif verification_fault == "mask":
                        segment = next(item for item in composition["composition"]["video_segments"]
                                       if item.get("mask_asset") == "region.png")
                        segment["mask_sha256"] = "0" * 64
                    composition_path.write_text(json.dumps(composition))
                if stage == "prepare":
                    preparation = json.loads((out / "preparation.json").read_text())
                    assert all("path" not in row for row in preparation["assets"])
                    if ordered and preparation["request_digest"] != normalize_request(raw).digest:
                        from astrid.packs.h3_av.shared.spec import canonical_spec
                        from astrid.packs.h3_av.shared.request_v2 import read_prepared_request
                        spec = canonical_spec(read_prepared_request(
                            preparation["request"], preparation["request_digest"], require_normalized_v2=True,
                        ))
                        assert len(spec.sampling_passes()) == 1
                        assert len(spec.sampling_passes()[0].preserves) == 1
                        pass_preparations.append(spec)
                if stage == "compile":
                    self.compilation = json.loads((out / "compilation.json").read_text())
                if stage == "verify" and ordered:
                    pass_verifications.append(json.loads((out / "verification.json").read_text()))
                if stage in {"compose", "verify"}:
                    if ordered:
                        current_source = Path(localized["source"]).read_bytes()
                        original_digest = normalize_request(raw).digest
                        cumulative = json.loads(Path(localized["preparation"]).read_text())["request_digest"] == original_digest
                        if cumulative:
                            assert current_source == source_bytes
                        elif stage == "compose":
                            if pass_sources:
                                assert pass_verifications[-1]["status"] == "verified"
                                assert hashlib.sha256(current_source).hexdigest() == pass_verifications[-1]["candidate_sha256"]
                            else:
                                assert current_source == source_bytes
                            pass_sources.append(current_source)
                    elif operation == "generate":
                        assert "source" not in localized
                    else:
                        assert Path(localized["source"]).read_bytes() == source_bytes
                rows = [settle(Path(port["path_template"].format(out=out)), port["name"])
                        for port in manifest["outputs"]]
            elif capability_id == "vibecomfy.validate":
                assert set(inputs) == {
                    "python", "companion", "source", "python_execution_consent",
                    "workflow_inputs",
                }
                assert inputs["python_execution_consent"] == "confirmed"
                # Exercise the real canonical validator at the same transport
                # seam as the staged H3 executors.  The workflow projection is
                # CPU-safe; only the later sampling branch remains synthetic.
                validator_inputs = {
                    name: inputs[name]
                    for name in ("python", "companion", "source")
                }
                staged: dict[str, Path] = {}
                for name, filename in (
                    ("python", "workflow.py"),
                    ("companion", "workflow.vibe.json"),
                    ("source", "source.json"),
                ):
                    descriptor = validator_inputs[name]
                    assert isinstance(descriptor, dict)
                    object_id = descriptor["object_id"]
                    staged_path = out / filename
                    staged_path.write_bytes(objects[object_id])
                    staged[name] = staged_path
                validator = importlib.import_module(
                    "astrid.packs.vibecomfy.actions.validate.run"
                )
                validation_out = out
                assert validator.main([
                    "validate", "",
                    "--python", str(staged["python"]),
                    "--companion", str(staged["companion"]),
                    "--source", str(staged["source"]),
                    "--python-execution-consent", "confirmed",
                    "--workflow-inputs", inputs["workflow_inputs"],
                    "--out", str(validation_out),
                ]) == 0
                report_path = validation_out / "validation-report.json"
                report = json.loads(report_path.read_text())
                assert report["concrete_workflow_inputs_projected"] is True
                rows = [settle(report_path, "validation")]
            else:
                assert capability_id == "vibecomfy.run"
                if placement == "local" and (operation == "generate" or ordered):
                    from astrid.packs.vibecomfy.shared.python_execution_consent import confirmed_python_execution_scope
                    from vibecomfy import workflow_bundle
                    for name, filename in (("python", "workflow.py"), ("companion", "workflow.vibe.json"),
                                           ("source", "source.json")):
                        (out / filename).write_bytes(objects[inputs[name]["object_id"]])
                    with confirmed_python_execution_scope("confirmed"):
                        bundle = workflow_bundle.load_bundle(out / "workflow.py")
                        graph = bundle.workflow.compile("api", run_inputs=json.loads(inputs["workflow_inputs"]))
                    expected = json.loads(fake_comfy.expected.read_text())
                    if ordered:
                        import io
                        archive = zipfile.ZipFile(io.BytesIO(objects[inputs["managed_assets"]["object_id"]]))
                        expected = {"native_masked_edit": True, "asset_files": {}}
                        for member in archive.namelist():
                            if member.startswith("assets/") and not member.endswith("/"):
                                payload = archive.read(member)
                                basename = Path(member).name
                                (fake_comfy.input_dir / basename).write_bytes(payload)
                                expected["asset_files"][basename] = {
                                    "sha256": hashlib.sha256(payload).hexdigest(), "size": len(payload),
                                }
                        source_node = next(node for node in graph.values() if node["class_type"] == "VHS_LoadVideoFFmpeg")
                        submitted_source = (fake_comfy.input_dir / source_node["inputs"]["video"]).read_bytes()
                        if not fake_submissions:
                            assert submitted_source == source_bytes
                        if fake_submissions:
                            assert len(pass_verifications) == 1 and pass_verifications[0]["status"] == "verified"
                            assert hashlib.sha256(submitted_source).hexdigest() == pass_verifications[0]["candidate_sha256"]
                        fake_submissions.append(source_node["inputs"]["video"])
                    expected["generated_seconds"] = self.compilation["capabilities"]["generation_timing"]["raw_frames"] / 24
                    fake_comfy.expected.write_text(json.dumps(expected))
                    status, body = fake_comfy.post(graph)
                    assert status == 200 and body["node_errors"] == {}, body
                    prompt_id = body["prompt_id"]
                    history = json.loads(fake_comfy.get(f"/history/{prompt_id}"))[prompt_id]
                    assert history["status"]["status_str"] == "success"
                    (output,) = history["outputs"].values()
                    (item,) = output["gifs"]
                    generated = out / "generated.mp4"
                    generated.write_bytes(fake_comfy.get(
                        f"/view?filename={item['filename']}&subfolder=&type=output"
                    ))
                    rows = [settle(generated, "video", media_type="video/mp4")]
                elif self.compilation["capabilities"]["output_contract"] == "muxed_av_full_timeline" and operation == "edit":
                    # B6's native edit graph returns one muxed result. Keep the
                    # synthetic worker faithful to its declared output contract.
                    generated = out / "generated.mp4"
                    _synthetic_av(generated, 2, color="red")
                    rows = [settle(generated, "video", media_type="video/mp4")]
                elif operation in {"continue", "generate", "character"}:
                    duration = (self.compilation["generation_timing"]["raw_frames"] / 24
                                if operation == "character" else
                                self.compilation["capabilities"]["generation_timing"]["raw_frames"] / 24
                                if operation == "generate" else
                                self.compilation["continuation_timing"]["expected_graph_output_duration"])
                    generated = out / "generated.mp4"
                    _synthetic_av(generated, duration, color="red")
                    rows = [settle(generated, "video", media_type="video/mp4")]
                else:
                    generated = out / "generated.mp4"
                    _synthetic_av(generated, 2, color="red", muxed=False)
                    audio = out / "generated.wav"
                    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                                    "sine=frequency=880:sample_rate=48000", "-t", "2", str(audio)], check=True)
                    rows = [settle(generated, "video", media_type="video/mp4"),
                            settle(audio, "audio", media_type="audio/wav")]
            shutil.rmtree(attempt)
            if capability_id == "vibecomfy.run":
                intent = inputs["generation_intent"]
                public_row = next(
                    row for row in rows
                    if row.get("media_type") == "video/mp4"
                )
                public_row.update({
                    "kind": "object",
                    "role": "result",
                    "output_port": "vibecomfy_run",
                    "group_key": "main",
                    "variant_key": "original",
                    "ordinal": 0,
                })
                selector = {
                    **intent["groups"][0]["selectors"][0],
                    "output_port": "vibecomfy_run",
                }
                effect = {
                    "effect_type": "generation.publish_v1",
                    "target_id": "cpu-test",
                    "payload": {
                        "version": 1,
                        "modality": intent["modality"],
                        "generation_type": "vibecomfy.run",
                        "metadata": intent["metadata"],
                        "partial_success_policy": intent["partial_success_policy"],
                        "groups": [{"group_key": "main", "selectors": [selector]}],
                    },
                }
                publication = {
                    "effect_type": "generation.publish_v1",
                    "publications": [{
                        "group_key": "main",
                        "generation_id": "generation-cpu",
                        "missing_selectors": [],
                        "variants": [{
                            "generation_id": "generation-cpu",
                            "variant_id": "variant-cpu",
                            "object_id": public_row["object_id"],
                            "output_port": "vibecomfy_run",
                            "ordinal": 0,
                            "variant_key": "original",
                        }],
                    }],
                }
                settled = {
                    "outputs": rows,
                    "generation_publish_v1": publication,
                }
                if finalizer_state == "unclaimed":
                    settled.pop("generation_publish_v1")
                managed_rows = []
                for index, row in enumerate(rows):
                    is_public = row is public_row
                    managed_rows.append({
                        **row,
                        "association_id": f"association-{index}",
                        "task_id": "task-cpu",
                        "run_id": "run-cpu",
                        "attempt_id": "attempt-cpu",
                        "project_id": "cpu-test",
                        "generation_id": "generation-cpu" if is_public else None,
                    })
                task = {
                    "task_id": "task-cpu",
                    "run_id": "run-cpu",
                    "attempt_id": "attempt-cpu",
                    "project_id": "cpu-test",
                    "state": "succeeded",
                    "generation_intent": intent,
                    "expected_effect": effect,
                    "result": settled,
                }
                raw_result = {
                    "ok": True,
                    "state": "completed",
                    "kernel_run_id": "run-cpu",
                    "kernel_task_id": "task-cpu",
                    "kernel_attempt_id": "attempt-cpu",
                    "task": task,
                    "result": settled,
                    "outputs": {"artifacts": rows},
                    "managed_outputs": managed_rows,
                }
                return InvocationResult(
                    capability_id=capability_id,
                    capability_type="executor",
                    native_kind="executor",
                    ok=True,
                    outputs={"artifacts": rows, "managed_outputs": managed_rows},
                    raw_result=raw_result,
                    kernel_task_id="task-cpu",
                    kernel_run_id="run-cpu",
                    kernel_attempt_id="attempt-cpu",
                )
            return SimpleNamespace(
                ok=True,
                capability_id=capability_id,
                outputs={"artifacts": rows},
                raw_result={},
                kernel_task_id="task-cpu",
                kernel_run_id="run-cpu",
                kernel_attempt_id="attempt-cpu",
            )

    from unittest.mock import Mock
    workflow_facade = Mock(wraps=transform.generate.workflow)
    monkeypatch.setattr(transform, "generate", SimpleNamespace(workflow=workflow_facade))
    client = Client()
    monkeypatch.setattr(transform.AstridClient, "open_from_launcher", lambda **kwargs: nullcontext(client))
    if placement == "local":
        from astrid.core.generation.engine_contracts import HealthEvidence, LocalAttestation
        health = HealthEvidence("healthy", "2026-10-28T00:00:00Z", "ab" * 32, True)
        attestation = LocalAttestation("local-engine.v1", "/cpu/install.json", "cd" * 32, health, "ab" * 32)
        engine = SimpleNamespace(
            endpoint=SimpleNamespace(url="http://127.0.0.1:8188", attestation=attestation),
            comfy_output_dir="/cpu/ComfyUI/output",
        )
        if operation == "generate" or ordered:
            from tests.packs.h3_av.test_cpu_rehearsal import _FakeComfy
            server_root = tmp_path / "fake-comfy"
            server_root.mkdir()
            fake_comfy = _FakeComfy(server_root, source=source, basename=source.name)
            request.addfinalizer(fake_comfy.close)
            engine.endpoint.url = fake_comfy.url
            engine.comfy_output_dir = str(fake_comfy.output_dir)
        monkeypatch.setattr(transform, "_resolve_local_engine", lambda: engine)
    execution_request = tmp_path / "execution-request.json"
    execution_request.write_text(json.dumps({"target": {
        "kind": "runpod", "pod_id": "pod-cpu-test", "provider_account_ref": "account-cpu-test",
        "storage": {"network_volume_id": "volume-cpu-test"},
    }}))
    arguments = [
        "--request", str(caller / "request.json"), "--asset-map", str(caller / "asset-map.json"),
        "--out", str(tmp_path / "parent"), "--project", "cpu-test",
        "--placement", placement,
    ]
    if placement == "runpod":
        arguments.extend(["--execution-request", str(execution_request)])
    if operation == "generate" and placement == "local" and verification_fault is None:
        # Exercise planning and execution in the same operation directory.
        # Planning must leave execution and its durable journal untouched.
        plan = transform.run_transform(transform.build_parser().parse_args([
            *arguments, "--dry-run",
        ]))
        assert plan["status"] == "planned"
        assert plan["runtime_submission"] == "eligible"
        assert plan["request_digest"] == normalize_request(raw).digest
        assert plan["pass_count"] == 1
        assert json.loads((tmp_path / "parent/plan.json").read_text())["request_digest"] == plan["request_digest"]
        assert calls == []
        assert not (tmp_path / "parent/operation-state.json").exists()
        assert not (tmp_path / "parent/result.json").exists()
    if verification_fault:
        expected_error = {
            "forged_witness_method": "cannot be recomputed",
            "forged_witness_digest": "does not match candidate",
            "schedule": "schedule provenance",
            "candidate": "candidate digest",
            "mask": "regional mask provenance",
            "forged_verify_status": "does not certify",
            "forged_verify_request": "does not certify",
            "forged_verify_candidate": "does not certify",
            "candidate_after_verify": "candidate digest",
        }.get(verification_fault, "ffprobe")
        parent_fault = verification_fault.startswith("forged_verify_") or verification_fault == "candidate_after_verify"
        with pytest.raises(AstridError if parent_fault else VerificationError, match=expected_error):
            transform.run_transform(transform.build_parser().parse_args(arguments))
        assert calls[-1][0] == ("h3_av.publication_finalizer" if verification_fault == "candidate_after_verify"
                                else "h3_av.verify")
        if verification_fault != "candidate_after_verify":
            assert not any(name == "h3_av.publication_finalizer" for name, _ in calls)
        assert not (tmp_path / "parent/08-final-receipt.json").exists()
        if not parent_fault:
            assert not (attempts[-1] / "outputs/verification.json").exists()
            assert not (attempts[-1] / "outputs/manifest.json").exists()
        if ordered:
            assert [name for name, _ in calls].count("vibecomfy.run") == 1
            assert [name for name, _ in calls].count("h3_av.compile") == 1
            assert len(fake_submissions) == 1
            terminal = json.loads((tmp_path / "parent/result.json").read_text())
            assert terminal["status"] == "failed" and terminal["ok"] is False
            assert terminal["request_digest"] == normalize_request(raw).digest
            assert terminal["outputs"]["artifacts"] == []
            journal = json.loads((tmp_path / "parent/operation-state.json").read_text())
            assert journal["events"][-1]["status"] == "failed"
            assert not pass_verifications
            ledger = json.loads((tmp_path / "parent/ordered-passes.json").read_text())
            assert ledger["pass_count"] == 2
            assert len(ledger["passes"]) == 1
            assert ledger["passes"][0]["status"] != "verified"
            return
        # A settled child generation is independent of final composition success.
        child_result = json.loads((tmp_path / "parent/04-run/run-result.json").read_text())["result"]
        assert child_result["ok"] is True
        assert child_result["capability_id"] == "vibecomfy.run"
        terminal = json.loads((tmp_path / "parent/result.json").read_text())
        assert terminal["capability_id"] == "h3_av.transform"
        assert terminal["status"] == "failed" and terminal["ok"] is False
        assert terminal["request_digest"] == normalize_request(raw).digest
        assert terminal["resolved_options"]["placement"] == "local"
        assert terminal["outputs"]["artifacts"] == []
        assert terminal["manifest_path"] is None
        assert expected_error in terminal["error"]["message"]
        journal = json.loads((tmp_path / "parent/operation-state.json").read_text())
        assert journal["events"][-1]["phase"] == "operation"
        assert journal["events"][-1]["status"] == "failed"
        if not parent_fault:
            assert journal["events"][-2]["phase"] == "verify"
            assert journal["events"][-2]["status"] == "uncertain"
        return
    result = transform.run_transform(transform.build_parser().parse_args(arguments))
    assert result["status"] == "candidate_verified"
    assert Path(result["candidate"]).is_file()
    assert Path(result["final_receipt"]).is_file()
    final_receipt = json.loads(Path(result["final_receipt"]).read_text())
    assert final_receipt["overall_status"] == "candidate_verified"
    if ordered:
        names = [name for name, _ in calls]
        pass_stages = ["h3_av.prepare", "h3_av.compile", "vibecomfy.validate", "vibecomfy.run", "h3_av.compose", "h3_av.verify"]
        # Cumulative compose/verify measures original->final preservation and
        # never creates an additional modifying invocation.
        assert names.count("h3_av.prepare") == 3
        assert [name for name in names if name != "h3_av.prepare"] == [
            name for name in pass_stages * 2 if name != "h3_av.prepare"
        ] + ["h3_av.compose", "h3_av.verify", "h3_av.publication_finalizer"]
        assert [spec.sampling_passes()[0].operation.during for spec in pass_preparations] == [
            (0.5, 1.0), (0.5 if operation == "ordered-overlap" else 1.0, 2.0),
        ]
        assert workflow_facade.call_count == len(fake_submissions) == 2
        ledger = json.loads((tmp_path / "parent/ordered-passes.json").read_text())
        assert ledger["pass_count"] == len(ledger["passes"]) == 2
        first, second = ledger["passes"]
        assert first["status"] == second["status"] == "verified"
        assert first["output"]["sha256"] == second["input"]["sha256"]
        assert len(first["preserves"]) == len(second["preserves"]) == 1
        for entry in ledger["passes"]:
            events = json.loads(Path(entry["journal"]).read_text())["events"]
            assert [row["phase"] for row in events if row["status"] == "completed"] == [
                "prepare", "compile", "validate", "run", "pullback", "compose", "verify",
            ]
        assert ledger["cumulative"]["verification_result"]["status"] == "verified"
        assert len(pass_sources) == 2
        assert pass_sources[0] == source_bytes
        assert pass_sources[1] != source_bytes
        assert len(pass_verifications) == 3
        assert all(row["status"] == "verified" for row in pass_verifications)
        assert hashlib.sha256(pass_sources[1]).hexdigest() == pass_verifications[0]["candidate_sha256"]
        for report in pass_verifications:
            assert report["preservation"]["spatial_complement"]
            assert report["preservation"]["audio"] == [[0, 2]]
        assert pass_verifications[-1]["request_digest"] == normalize_request(raw).digest
        assert pass_verifications[-1]["candidate_sha256"] == hashlib.sha256(Path(result["candidate"]).read_bytes()).hexdigest()
        # Independent decoded samples confirm both edits survive chaining,
        # the explicit preserve interval and pixels outside masks stay blue.
        import numpy as np
        def pixel(seconds, x, y):
            decoded = subprocess.run([
                "ffmpeg", "-v", "error", "-ss", str(seconds), "-i", result["candidate"],
                "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
            ], capture_output=True, check=True).stdout
            return np.frombuffer(decoded, dtype=np.uint8).reshape(576, 1024, 3)[y, x]
        for seconds, x, y in [(0.25, 100, 100), (0.75, 800, 100), (1.5, 100, 100)]:
            red, green, blue = pixel(seconds, x, y)
            assert blue > 200 and red < 20 and green < 20
        for seconds, x, y in [(0.75, 100, 100), (1.5, 600, 400)]:
            red, green, blue = pixel(seconds, x, y)
            assert red > 200 and blue < 20 and green < 20
        assert not caller.exists() and all(not attempt.exists() for attempt in attempts)
        terminal = json.loads((tmp_path / "parent/result.json").read_text())
        assert terminal["status"] == "succeeded" and terminal["ok"] is True
        assert terminal["request_digest"] == normalize_request(raw).digest
        assert terminal["outputs"]["artifacts"][0]["digest"] == "sha256:" + pass_verifications[-1]["candidate_sha256"]
        return
    assert final_receipt["states"]["raw_managed_publication"]["status"] == ("failed" if finalizer_state == "unclaimed" else "passed")
    if finalizer_state == "passed":
        assert final_receipt["states"]["raw_managed_publication"]["status"] == "passed"
        assert final_receipt["states"]["final_composition_publication"]["status"] == "passed"
        assert final_receipt["publication_contract"]["published_scope"] == "final_composition"
        assert final_receipt["publication_contract"]["final_composition_publication"] == "verified"
    else:
        assert final_receipt["states"]["final_composition_publication"]["status"] == "not_claimed"
        assert final_receipt["publication_contract"]["published_scope"] == "raw_internal_lineage"
        assert final_receipt["publication_contract"]["final_composition_publication"] == "required"
    journal = json.loads((tmp_path / "parent/operation-state.json").read_text())
    assert journal["events"][-1]["status"] == result["status"] == final_receipt["overall_status"]
    if finalizer_state != "passed":
        events = [row for row in journal["events"] if row["phase"] == "finalizer"]
        assert events[-1]["status"] == ("unproven" if finalizer_state == "rejected" else "skipped")
    assert not caller.exists() and all(not attempt.exists() for attempt in attempts)
    expected_calls = ["h3_av.prepare", "h3_av.compile", "vibecomfy.validate", "vibecomfy.run", "h3_av.compose", "h3_av.verify"]
    if finalizer_state != "unclaimed":
        expected_calls.append("h3_av.publication_finalizer")
    assert [name for name, _ in calls] == expected_calls
    call_inputs = {name: inputs for name, inputs in calls}
    workflow_facade.assert_called_once()
    forwarded = workflow_facade.call_args.kwargs
    assert forwarded["client"] is client
    assert forwarded["placement"] == placement
    assert {name: forwarded[name] for name in call_inputs["vibecomfy.run"]} == call_inputs["vibecomfy.run"]
    assert forwarded["workflow_inputs"] == json.dumps(client.compilation["workflow_inputs"], sort_keys=True, separators=(",", ":"))
    assert all(event["workflow_route"] == f"generation.workflow.v1/{placement}"
               for event in journal["events"] if event["phase"] == "run")
    if placement == "local":
        assert forwarded["comfy_url"] == engine.endpoint.url
        assert forwarded["comfy_output_dir"] == engine.comfy_output_dir
        assert final_receipt["placement"] == "local"
        evidence = json.loads(fake_comfy.evidence.read_text())
        assert evidence["prompt_id"] and evidence["node_id"] == ""
        assert (fake_comfy.output_dir / evidence["output_filename"]).is_file()
    # VibeComfy's source port is workflow provenance, not the source video.
    assert call_inputs["vibecomfy.run"]["source"]["filename"] == "source.json"
    intent = call_inputs["vibecomfy.run"]["generation_intent"]
    assert intent["groups"] == [{
        "group_key": "main",
        "selectors": client.compilation["capabilities"]["public_generation"]["selectors"],
    }]
    assert len(intent["groups"][0]["selectors"]) == 1
    assert intent["metadata"]["h3_av"]["graph"]["compilation_digest"] == client.compilation["compilation_digest"]
    bundle_id = call_inputs["h3_av.prepare"]["input_bundle"]["object_id"]
    assert call_inputs["h3_av.compile"]["input_bundle"]["object_id"] == bundle_id
    assert call_inputs["h3_av.compose"]["input_bundle"]["object_id"] == bundle_id
    assert call_inputs["h3_av.verify"]["input_bundle"]["object_id"] == bundle_id
    assert ("source" in call_inputs["h3_av.compose"]) == (operation != "generate")
    assert ("source" in call_inputs["h3_av.verify"]) == (operation != "generate")
    report = json.loads(Path(result["verification"]).read_text())
    if operation == "generate":
        composition = json.loads(Path(result["composition"]).read_text())
        assert composition["coverage"]["candidate"]["video"]["frames"] == 360
        assert composition["coverage"]["candidate"]["requested_duration"] == 15
        assert composition["source"] is None
    else:
        assert report["preservation"]["status"] == "protected_sample_evidence"
    if operation == "character":
        preparation = json.loads(Path(result["preparation"]).read_text())
        assert preparation["request"]["version"] == 2
        assert preparation["request"]["settings"]["adapter"]["id"] == "character_swap_v1"
        assert preparation["request"]["duration"] == 15
        assert {row["id"] for row in preparation["request"]["media"]} == {"baseline", "performance", "replacement"}
        assert call_inputs["vibecomfy.validate"]["python_execution_consent"] == "confirmed"
        assert client.compilation["generation_timing"]["requested_frames"] == 360
        assert final_receipt["states"]["raw_managed_publication"]["status"] == "passed"
        assert final_receipt["states"]["final_composition_publication"]["status"] == "passed"
    # Relocation cannot weaken the candidate hash check.
    composition = json.loads(Path(result["composition"]).read_text())
    preparation = json.loads(Path(result["preparation"]).read_text())
    if placement == "local":
        terminal = json.loads((tmp_path / "parent/result.json").read_text())
        serialized_keys = InvocationResult(
            capability_id="h3_av.transform", capability_type="orchestrator",
            native_kind="orchestrator", ok=True,
        ).to_dict().keys()
        assert terminal.keys() == set(serialized_keys) | {
            "status", "task_id", "attempt_id", "parent_task_id", "parent_attempt_id",
            "request_digest", "resolved_options", "cost", "warnings", "publication",
        }
        assert terminal["capability_id"] == "h3_av.transform"
        assert terminal["capability_type"] == terminal["native_kind"] == "orchestrator"
        assert terminal["status"] == "succeeded" and terminal["ok"] is True
        assert terminal["raw_result"] == result
        assert terminal["request_digest"] == preparation["request_digest"]
        assert terminal["run_root"] == str(tmp_path / "parent")
        assert terminal["manifest_path"] is None
        assert all(terminal[key] is None for key in (
            "task_id", "attempt_id", "parent_task_id", "parent_attempt_id",
            "kernel_task_id", "kernel_run_id", "kernel_attempt_id",
        ))
        assert terminal["resolved_options"]["placement"] == "local"
        assert terminal["resolved_options"]["project"] == "cpu-test"
        assert terminal["cost"] == "unknown" and terminal["warnings"] == []
        assert terminal["publication"]["status"] == "not_settled"
        candidate = terminal["outputs"]["artifacts"][0]
        assert candidate["name"] == "candidate" and candidate["role"] == "result"
        assert candidate["ordinal"] == 0
        assert candidate["digest"] == "sha256:" + composition["candidate"]["sha256"]
        assert candidate["digest"] == "sha256:" + hashlib.sha256(Path(result["candidate"]).read_bytes()).hexdigest()
        assert candidate["size"] == Path(result["candidate"]).stat().st_size
        assert candidate["path"] == result["candidate"]
        assert candidate["media_type"] == "video/mp4"
    Path(result["candidate"]).write_bytes(b"tampered")
    with pytest.raises(VerificationError, match="digest"):
        verify_candidate(preparation=preparation, composition=composition, candidate=result["candidate"])
