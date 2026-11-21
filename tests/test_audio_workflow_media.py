"""Audio inputs use the same container rule in transcription dry runs and uploads."""

import hashlib
import io
import json
import subprocess

import pytest

from astrid.core.generation.backends.comfy_target import ComfyTarget, ComfyTargetClient
from astrid.packs.generation.workflows.transcription.workflow import build
from astrid.sdk.generation import _offline_invocation_preflight


@pytest.fixture(params=["mp4-with-audio", "mp4-without-audio", "wav"])
def source(request, tmp_path):
    if request.param == "wav":
        path = tmp_path / "take.wav"
        # Native audio MIME types have historically needed no stream probe.
        path.write_bytes(b"native audio placeholder")
    else:
        path = tmp_path / "take.mp4"
        command = ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=size=16x16"]
        if request.param == "mp4-with-audio":
            command.extend(["-f", "lavfi", "-i", "anullsrc", "-c:a", "aac"])
        subprocess.run([*command, "-t", "0.1", "-c:v", "mpeg4", str(path)], check=True)
    return path, request.param != "mp4-without-audio"


def test_transcription_dry_preflight(source):
    path, accepted = source

    def preflight():
        _offline_invocation_preflight(build(), plan=None, assets={"audio_ref": path},
                                      comfy_url=None, lineage=None, source_video=True)

    if accepted:
        preflight()
    else:
        with pytest.raises(ValueError, match="has no audio stream"):
            preflight()


def test_managed_audio_upload(source):
    path, accepted = source
    data = path.read_bytes()

    def receive(request, *, timeout):
        assert accepted
        assert data in request.data
        if path.suffix == ".mp4":
            assert b"Content-Type: video/mp4\r\n" in request.data
        return io.BytesIO(json.dumps({
            "name": path.name, "subfolder": "astrid/test", "type": "input",
        }).encode())

    client = ComfyTargetClient(ComfyTarget("http://localhost:8188", "attached"), opener=receive)
    kwargs = dict(task_id="test", kind="audio", expected_sha256=hashlib.sha256(data).hexdigest())
    if accepted:
        assert client.upload_media(path, **kwargs) == f"astrid/test/{path.name}"
    else:
        with pytest.raises(ValueError, match="has no audio stream"):
            client.upload_media(path, **kwargs)
