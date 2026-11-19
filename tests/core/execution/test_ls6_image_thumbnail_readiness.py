"""Image previews use the installed action even when ffmpeg is unavailable."""
from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
from pathlib import Path

import pytest
from PIL import Image

from astrid.core.execution.generic_host import GenericPackHost, RuntimeProtocolClient
from astrid.core.execution.thumbnails import ThumbnailError, extract_thumbnail
from astrid.pack_api import VERSION as PACK_API_VERSION
from astrid_runtime_client import WorkspaceClient


pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[3]
THUMBNAIL = "generation.thumbnail_backfill"
MAIN = "ls6_image.generate"


def _without_ffmpeg(tmp_path, monkeypatch):
    """Retain host tools while removing every PATH directory with ffmpeg."""
    original = os.environ.get("PATH", "").split(os.pathsep)
    suffixes = [""] if os.name != "nt" else os.environ.get("PATHEXT", ".EXE").split(os.pathsep)
    available = [directory for directory in original if directory and not any(
        (Path(directory) / ("ffmpeg" + suffix)).is_file() for suffix in suffixes
    )]
    tools = tmp_path / "host-tools"
    tools.mkdir()
    if os.name == "posix":
        # Native custody and source capture keep their ordinary tools. No
        # decoder, extraction function, readiness check or host is replaced.
        for name in ("ps", "git", "uname", "sh", "getconf", "sandbox-exec"):
            executable = shutil.which(name)
            if executable:
                (tools / name).symlink_to(executable)
    monkeypatch.setenv("PATH", os.pathsep.join([str(tools), *available]))
    monkeypatch.delenv("ASTRID_PACKS_PATH", raising=False)
    assert shutil.which("ffmpeg") is None


def _image_pack(tmp_path):
    pack = tmp_path / "ls6_image"
    pack.mkdir()
    script = pack / "generate.py"
    script.write_text(
        "import argparse, shutil\nfrom datetime import datetime, timezone\nfrom pathlib import Path\n"
        "from astrid.pack_api import build_manifest, write_manifest\n"
        "assert shutil.which('ffmpeg') is None\n"
        "parser = argparse.ArgumentParser()\n"
        "parser.add_argument('--source', type=Path, required=True)\n"
        "parser.add_argument('--out', type=Path, required=True)\n"
        "args = parser.parse_args()\nargs.out.mkdir(parents=True, exist_ok=True)\n"
        "(args.out / 'image.png').write_bytes(args.source.read_bytes())\n"
        "write_manifest(args.out / 'manifest.json', build_manifest(kind='ls6-image',"
        " inputs={'source': str(args.source)}, outputs=[{'name': 'image', 'path': 'image.png',"
        " 'ordinal': 0, 'role': 'result', 'group_key': 'main', 'variant_key': 'original',"
        " 'output_port': 'image', 'is_primary': True, 'media_type': 'image/png'}],"
        " created=datetime.now(timezone.utc).isoformat()))\n",
        encoding="utf-8",
    )
    (pack / "pack.yaml").write_text(json.dumps({
        "schema_version": 3, "id": "ls6_image", "name": "LS6 image fixture", "version": "1.0.0",
        "pack_api": "%d.%d" % PACK_API_VERSION, "status": "experimental", "domain": "integration",
        "stability": "experimental", "support": "community", "visibility": "visible",
        "description": "A real image generation result without an external decoder.",
        "permissions": [{"id": "subprocess", "reason": "Runs the CPU image fixture."},
                        {"id": "project_files", "reason": "Copies the declared image."}],
        "actions": {"generate": {
            "name": "Generate image", "description": "Copy an admitted PNG.", "version": "1.0",
            "cache": {"mode": "none"},
            "inputs": [{"name": "source", "type": "file", "required": True}],
            "outputs": [{"name": "image", "type": "file", "mode": "create_or_replace",
                         "path_template": "{out}/image.png", "artifact_type": "image/png"}],
            "isolation": {"mode": "subprocess", "network": False},
            "metadata": {"adapter_family": "cpu", "output_result_manifest": True,
                         "runtime_file": "generate.py"},
            "invocation": {"kind": "command", "command": {"argv": [
                "{python_exec}", str(script), "--source", "{source}", "--out", "{out}"]}},
            "resources": [{"kind": "implementation", "path": "generate.py"}],
        }},
    }), encoding="utf-8")
    shipped = json.loads((ROOT / "config/astrid-beta-capabilities.json").read_text(encoding="utf-8"))
    thumbnail = next(row for row in shipped["capabilities"] if row["id"] == THUMBNAIL)
    matrix = tmp_path / "matrix.json"
    matrix.write_text(json.dumps({"schema_version": 1, "capabilities": [thumbnail, {
        "id": MAIN, "disposition": "required", "evidence_reason": "LS6 image readiness fixture",
        "adapter_family": "cpu", "resource_keys": [], "required_env": [], "required_binaries": [],
        "required_packages": [], "estimated_scratch_bytes": 2097152, "estimated_output_bytes": 524288,
    }]}), encoding="utf-8")
    return pack, matrix


def test_image_intent_completes_without_ffmpeg(tmp_path, monkeypatch, real_runtime):
    _without_ffmpeg(tmp_path, monkeypatch)
    pack, matrix = _image_pack(tmp_path)
    buffer = io.BytesIO()
    Image.new("RGBA", (640, 400), (20, 80, 140, 160)).save(buffer, format="PNG")
    png = buffer.getvalue()
    source_digest = "sha256:" + hashlib.sha256(png).hexdigest()
    # The same missing decoder still refuses video extraction.
    video = tmp_path / "source.mp4"
    video.write_bytes(b"video")
    with pytest.raises(ThumbnailError, match="ffmpeg is required for video thumbnail extraction"):
        extract_thumbnail(video, tmp_path / "video-preview.jpg", "video/mp4")
    assert not (tmp_path / "video-preview.jpg").exists()
    daemon = real_runtime()
    api = WorkspaceClient(daemon.endpoint, daemon.token)
    host = GenericPackHost(
        pack_roots=[pack, ROOT / "astrid/packs/generation"], capability_matrix=matrix,
        client=RuntimeProtocolClient(daemon.endpoint, daemon.worker_token),
        executor_id="astrid-pack-host", attempt_base=tmp_path / "attempts",
    )
    try:
        registration = host.register()["registration"]
        record = host.capabilities[THUMBNAIL]
        assert record.ready, record.preflight
        published = next(row for row in registration.capabilities if row.capability_id == THUMBNAIL)
        assert published.definition_digest == record.capability_digest
        project = api.create_project("LS6 image preview", idempotency_key="image-project")
        source = api.ingest_project_object(project.project_id, png, media_type="image/png",
            idempotency_key="image-input", filename="source.png")
        assert source["digest"] == source_digest
        main = api.admit_task(
            capability_id=MAIN, capability_digest=host.capabilities[MAIN].capability_digest,
            input_object_ids=[source_digest], project_id=project.project_id, idempotency_key="image-main",
            spec={"inputs": {"source": {"object_id": source_digest, "digest": source_digest,
                "sha256": source_digest.removeprefix("sha256:"), "filename": "source.png",
                "size": len(png), "media_type": "image/png"}}},
            generation_intent={"version": 1, "modality": "image", "partial_success_policy": "reject",
                "groups": [{"group_key": "main", "selectors": [
                    {"selector": "image", "ordinal": 0, "variant_key": "original"}]}]},
            settlement_effect={"effect_type": "generation.publish_v1", "target_id": project.project_id,
                "payload": {"version": 1, "modality": "image", "partial_success_policy": "reject",
                    "generation_type": MAIN, "metadata": {}, "groups": [{"group_key": "main", "selectors": [
                        {"selector": "image", "ordinal": 0, "variant_key": "original", "output_port": "image"}]}]}},
            storage_estimate={"scratch_bytes": 2097152, "output_bytes": 524288},
        )
        assert [row.state for row in host.run(once=True)] == ["succeeded"]
        completed_main = api.get_task(main.task_id)
        intent, = completed_main.result["thumbnail_intents"]
        assert intent["source_object_id"] == source_digest
        assert intent["project_id"] == project.project_id
        assert [row.state for row in host.run(once=True)] == ["succeeded"]
        tasks, cursor = api.list_project_tasks(project.project_id)
        assert cursor is None and len(tasks) == 2
        thumbnail, = [row for row in tasks if row.capability_id == THUMBNAIL]
        assert thumbnail.state == "succeeded" and thumbnail.attempt_id and thumbnail.lease_fence
        assert thumbnail.input_object_ids == [source_digest]
        generations, cursor = api.list_generations(project.project_id)
        assert cursor is None and len(generations) == 1
        descriptor = generations[0].metadata["thumbnail"]
        assert descriptor["source_object_id"] == source_digest and descriptor["recipe_version"] == 1
        data = api.get_object(descriptor["object_id"]).data
        assert descriptor["object_id"] == "sha256:" + hashlib.sha256(data).hexdigest()
        with Image.open(io.BytesIO(data)) as preview:
            preview.load()
            assert preview.format == "JPEG" and preview.size == (320, 200)
        assert api.get_object(source_digest).data == png
        assert api.get_task(main.task_id).attempt_id == completed_main.attempt_id
        assert host.run(once=True) == []
        assert host.client.pending_thumbnail_intents(executor_id=host.executor_id)["items"] == []
        assert shutil.which("ffmpeg") is None
    finally:
        host.shutdown()
        daemon.stop()
