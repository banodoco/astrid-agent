"""Non-skipped fake-runtime proofs for the managed media handoff boundary."""

from __future__ import annotations

import base64
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from astrid.core.execution.executor.registry import load_default_registry
from astrid.core.execution.executor.runner import ExecutorRunRequest, run_executor
from astrid.core.execution.generic_host import GenericPackHost
from astrid.core.rendering.contracts import SCHEMA_VERSION, RenderRequest
from astrid.core.theme import builtin_theme
from astrid.core.timeline.resolution import AssetIntegrity, classify_asset
from astrid.packs.rendering.actions.timeline_visualize.assets import verify_now
from astrid.packs.rendering.actions.timeline_visualize.thumbnails import sample_filmstrip
from tests.helpers.remotion_runtime import (
    REMOTION,
    configure_remotion_runtime,
    remotion_install_problems,
)
from tests.packs.rendering._helpers import _execution_env


class _Runtime:
    def __init__(self, payload: bytes | dict[str, bytes]) -> None:
        self.payloads = (
            payload if isinstance(payload, dict) else {hashlib.sha256(payload).hexdigest(): payload}
        )
        self.payload = next(iter(self.payloads.values()))
        self.digest = hashlib.sha256(self.payload).hexdigest()
        self.fetches: list[str] = []

    def get_object(self, digest: str) -> bytes:
        self.fetches.append(digest)
        return self.payloads[digest]


def _media_timeline() -> dict:
    clips = [
        {
            "id": "source",
            "at": 0,
            "track": "video",
            "clipType": "media",
            "asset": "source",
            "from": 0,
            "to": 1,
            "speed": 1,
            "volume": 0,
        }
    ]
    return {
        "theme": "banodoco-default",
        "theme_overrides": {"visual": {"canvas": {"width": 1280, "height": 720, "fps": 24}}},
        "tracks": [{"id": "video", "kind": "visual", "label": "Video"}],
        "clips": clips,
    }


def test_generic_host_materializes_registry_ids_once_under_attempt(tmp_path: Path) -> None:
    payload = b"managed media"
    runtime = _Runtime(payload)
    registry = tmp_path / "assets.json"
    registry.write_text(
        json.dumps(
            {
                "assets": {
                    "source": {"object_id": "obj-1", "digest": runtime.digest},
                    "alias": {"object_id": "obj-1", "digest": runtime.digest},
                }
            }
        ),
        encoding="utf-8",
    )
    host = GenericPackHost(pack_roots=[tmp_path], client=runtime)
    attempt = tmp_path / "attempt"
    values = host._materialize_inputs(
        {
            "input_object_ids": [runtime.digest],
            "spec": {"inputs": {"assets_registry": str(registry)}},
        },
        attempt,
    )

    root = Path(values["materialized_root"])
    assert root == attempt / "managed-objects"
    staged = Path(values["materialized_objects"]["obj-1"])
    assert staged.is_relative_to(root) and staged.read_bytes() == payload
    derived = json.loads(Path(values["assets_registry"]).read_text(encoding="utf-8"))
    assert Path(derived["assets"]["source"]["file"]).resolve() == staged.resolve()
    assert derived["assets"]["source"]["object_id"] == "obj-1"
    assert Path(derived["assets"]["alias"]["file"]).resolve() == staged.resolve()
    assert runtime.fetches == [runtime.digest]


def test_render_request_handoff_roundtrips_and_visualizer_verifies_it(tmp_path: Path) -> None:
    payload = b"attempt-local bytes"
    digest = hashlib.sha256(payload).hexdigest()
    root = tmp_path / "attempt" / "managed-objects"
    root.mkdir(parents=True)
    path = root / "object"
    path.write_bytes(payload)
    request = RenderRequest(
        schema_version=SCHEMA_VERSION,
        timeline_path=str(tmp_path / "timeline.json"),
        output_name="video.mp4",
        materialized_root=str(root.parent),
        materialized_objects={"object-1": str(path), digest: str(path)},
    )
    roundtrip = RenderRequest.from_dict(request.to_dict())
    assert roundtrip.materialized_root == str(root.parent)
    integrity = AssetIntegrity(
        asset_key="source",
        role="source",
        state="unsupported",
        expected_sha256=digest,
        observed_sha256=None,
        reason="not yet checked",
        source_id="object-1",
        source_version=None,
    )
    checked = verify_now(
        integrity,
        materialized_objects=roundtrip.materialized_objects,
        materialized_root=roundtrip.materialized_root,
    )
    assert checked.state == "verified_original"
    assert checked.observed_sha256 == digest


def test_visualizer_samples_only_host_materialized_original(tmp_path: Path) -> None:
    # A tiny valid PNG keeps this proof independent of ffmpeg while exercising
    # the supported static-image filmstrip path.
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
    )
    materialized_root = tmp_path / "attempt" / "managed-objects"
    materialized_root.mkdir(parents=True)
    source = materialized_root / "0000-object-1"
    source.write_bytes(png)
    digest = hashlib.sha256(png).hexdigest()
    integrity = classify_asset(
        "hero",
        {"object_id": "object-1", "digest": digest},
        project_ref="demo",
        media_snapshot=[
            {"object_id": "object-1", "digest": digest, "project_slug": "demo"}
        ],
    )
    frames = sample_filmstrip(
        source,
        n_candidates=1,
        n_frames=1,
        out_dir=tmp_path / "frames",
        page_id="TL01_AS01",
        media_type="image",
        integrity=integrity,
        project_root=tmp_path,
        materialized_root=materialized_root,
        materialized_objects={"object-1": str(source), digest: str(source)},
    )
    assert frames == [source]


def test_host_derived_registry_executes_canonical_ffmpeg_executor(tmp_path: Path, monkeypatch) -> None:
    """The real executor process consumes only the host's object-id handoff."""

    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    assert ffmpeg and ffprobe, "the canonical FFmpeg proof requires ffmpeg/ffprobe"
    source = tmp_path / "source.mp4"
    subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
         "-i", "color=c=black:s=1280x720:r=24:d=1", "-c:v", "libx264", "-pix_fmt",
         "yuv420p", str(source)],
        check=True,
    )
    payload = source.read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    registry = tmp_path / "registry.json"
    registry.write_text(json.dumps({"assets": {"source": {
        "media_id": "media-ffmpeg", "content_sha256": digest, "type": "video/mp4"
    }}}), encoding="utf-8")
    runtime = _Runtime(payload)
    attempt = tmp_path / "attempt"
    host = GenericPackHost(
        pack_roots=[Path(__file__).parents[2] / "astrid" / "packs" / "rendering" / "actions" / "render"],
        client=runtime,
        attempt_root=attempt,
    )
    values = host._materialize_inputs(
        {
            "input_object_ids": [runtime.digest],
            "spec": {"inputs": {"assets_registry": str(registry)}},
        },
        attempt,
    )
    project_root = tmp_path / "projects" / "demo"
    project_root.mkdir(parents=True)
    timeline_path = project_root / "timeline.json"
    timeline_path.write_text(json.dumps(_media_timeline()), encoding="utf-8")
    assets_path = project_root / "assets.json"
    shutil.copy2(values["assets_registry"], assets_path)
    monkeypatch.setenv("ASTRID_PROJECTS_ROOT", str(project_root.parent))
    result = run_executor(
        ExecutorRunRequest(
            executor_id="rendering.render",
            out=attempt,
            project="demo",
            inputs={
                "timeline": str(timeline_path),
                "timeline_ref": "timeline-ffmpeg",
                "assets_registry": str(assets_path),
                "selector": "rendering.ffmpeg",
                "output_name": "result.mp4",
                "materialized_root": values["materialized_root"],
                "materialized_objects": values["materialized_objects"],
                "keep_previous_renders": True,
            },
            project_was_auto_resolved=True,
            projects_root=project_root.parent,
            invocation="stage1-media-handoff-proof",
        ),
        load_default_registry(),
    )
    assert result.ok, result.payload
    output = attempt / "result.mp4"
    assert output.is_file() and output.read_bytes()[4:8] == b"ftyp"
    assert runtime.fetches == [digest]


@pytest.fixture
def remotion_launch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The provisioned Remotion install plus the launch-owned validation its runtime requires."""

    problems = remotion_install_problems()
    if problems:
        pytest.fail(
            "a real render needs the provisioned Remotion install (run `make remotion-install`): " + "; ".join(problems),
            pytrace=False,
        )
    with _execution_env():
        configure_remotion_runtime(monkeypatch)
        yield REMOTION


@pytest.mark.integration
def test_host_derived_registry_reaches_canonical_remotion_executor_process(tmp_path: Path, remotion_launch: Path) -> None:
    """A host-materialized registry traverses the real executor into a real Remotion render process."""

    runtime = remotion_launch
    source = tmp_path / "bounded.mp4"
    ffmpeg = shutil.which("ffmpeg")
    assert ffmpeg, "the Remotion proof needs ffmpeg to emit a valid artifact"
    subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "color=c=red:s=1280x720:r=24:d=1", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-shortest", "-vf", "scale=out_color_matrix=bt709:out_range=tv,format=yuv420p", "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", "-color_range", "tv", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-video_track_timescale", "90000", str(source)], check=True)
    payload = source.read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    theme_payload = json.dumps(builtin_theme(), sort_keys=True).encode("utf-8")
    theme_digest = hashlib.sha256(theme_payload).hexdigest()
    registry = tmp_path / "assets.json"
    registry.write_text(json.dumps({"assets": {"source": {
        "media_id": "media-remotion", "content_sha256": digest, "type": "video/mp4",
        "duration": 1.0, "resolution": "1280x720", "fps": 24,
    }}}), encoding="utf-8")
    project_root = tmp_path / "projects" / "demo"
    project_root.mkdir(parents=True)
    timeline_path = project_root / "timeline.json"
    timeline_path.write_text(json.dumps(_media_timeline()), encoding="utf-8")
    attempt = tmp_path / "attempt"
    runtime_client = _Runtime({digest: payload, theme_digest: theme_payload})
    host = GenericPackHost(
        pack_roots=[Path(__file__).parents[2] / "astrid" / "packs" / "rendering" / "actions" / "render"],
        client=runtime_client, attempt_root=attempt,
    )
    values = host._materialize_inputs(
        {
            "input_object_ids": [theme_digest, digest],
            "spec": {"inputs": {
                "assets_registry": str(registry),
                "theme": {"object_id": "theme-default", "digest": theme_digest},
            }}
        },
        attempt,
    )
    assets_path = project_root / "assets.json"
    shutil.copy2(values["assets_registry"], assets_path)
    result = run_executor(
        ExecutorRunRequest(
            executor_id="rendering.render", out=attempt, project="demo",
            inputs={"timeline": str(timeline_path), "timeline_ref": "timeline-remotion",
                    "assets_registry": str(assets_path), "selector": "rendering.remotion",
                    "output_name": "result.mp4", "keep_previous_renders": True,
                    "theme": values["theme"],
                    "backend_config": {"rendering.remotion": {"project_dir": str(runtime)}},
                    "materialized_root": values["materialized_root"],
                    "materialized_objects": values["materialized_objects"]},
            project_was_auto_resolved=True, projects_root=project_root.parent,
            invocation="stage1-remotion-process-proof",
        ),
        load_default_registry(),
    )
    assert result.ok, result.payload
    output = attempt / "result.mp4"
    assert output.is_file() and output.read_bytes()[4:8] == b"ftyp"
    assert sorted(runtime_client.fetches) == sorted([theme_digest, digest])
    # A valid-but-unrelated MP4 must not satisfy this proof.  The render
    # re-encodes the handed-off red source, so its decoded first frame
    # must match the source frame.
    source_frame = subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-i", str(source), "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"],
        check=True,
        capture_output=True,
    ).stdout
    output_frame = subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-i", str(output), "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"],
        check=True,
        capture_output=True,
    ).stdout
    assert source_frame and output_frame
    assert len(source_frame) == len(output_frame)
    # Remotion's own encode and colour conversion shift each level slightly (pure red 254,0,0 comes back as
    # 254,24,0), so compare the mean level of each channel, not bytes. An unrelated video is far outside this.
    for channel in range(3):
        source_mean = sum(source_frame[channel::3]) / len(source_frame[channel::3])
        output_mean = sum(output_frame[channel::3]) / len(output_frame[channel::3])
        assert abs(source_mean - output_mean) <= 4, (channel, source_mean, output_mean)
