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

from astrid.packs.h3_av.shared.stages import _generation_intent
from astrid.packs.h3_av.shared.compile import compile_preparation
from astrid.packs.h3_av.shared.compose import compose_candidate
from astrid.packs.h3_av.shared.prepare import prepare_request
from astrid.packs.h3_av.shared.request import normalize_request
from astrid.packs.h3_av.shared.verify import VerificationError, _verify_provenance, verify_candidate
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
    with pytest.raises(ValueError, match="missing public_generation"):
        _generation_intent({"capabilities": {"output_contract": "muxed_av_full_timeline"}})




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




# Parent coverage retained during the Release-1 + Timeline merge.
