"""PCM preparation inside the existing render lock and managed asset lifetime."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from astrid.core.rendering.assets import AssetMaterializer, AssetURLResolver
from astrid.core.rendering.contracts import FrameWindow
from astrid.core.rendering.remotion_runtime import RemotionRuntimeTools, probe_context
from astrid.core.rendering.service import RenderService


def prepare_normal_audio_sources(
    props: Mapping[str, Any], *, materializer: AssetMaterializer,
    asset_server: AssetURLResolver | None, attempt_directory: Path,
    project_dir: Path, runtime_tools: RemotionRuntimeTools,
    fps: float | None = None,
    original_clips: Mapping[str, Mapping[str, Any]] | None = None,
    window: FrameWindow | None = None, tracks: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Copy Root props and prepare each audio clip from its managed original.

    ``original_clips`` maps current clip IDs to original compiled occurrences,
    retained by the trusted source owner before a window/track transform. It is
    internal input, not authority. Never construct it from caller app metadata.
    Omitting it keeps the original clip clock, as the native window path does.
    Alternatively, ``window`` takes that unsliced input and retains the originals
    across the existing host slicer here; ``tracks`` optionally selects a layer.
    Neither form validates source authority.

    The caller owns an attempt directory under materializer.serving_root and
    keeps it and the server alive through consumption, usually via ExitStack.
    Successful WAVs live there until caller cleanup; failed batches remove only
    their own directory. Authored files and the original registry are untouched.
    """
    if window is not None:
        if original_clips is not None:
            raise ValueError("window preparation requires the unsliced compiled input")
        # Match the disposable IDs emitted by the host slicer. Retain full
        # compiled occurrences; caller app.astrid_slice_origin is not authority.
        original_clips = {
            f"{clip.get('id', 'clip')}_{window.start_frame}_{window.end_frame}": copy.deepcopy(clip)
            for clip in props["timeline"]["clips"]
        }
        props = {**props, "timeline": RenderService._window_timeline(
            props["timeline"], window, tracks=tracks,
        )}
    elif tracks is not None:
        raise ValueError("track selection requires a window")

    def encode(value: Any) -> str:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)

    expected = materializer.resolved_registry(asset_server)
    if encode(props["assets"]) != encode(expected):
        raise ValueError("audio Root registry differs from materialized source")
    audio_tracks = {row["id"] for row in props["timeline"]["tracks"] if row["kind"] == "audio"}
    indexed = []
    for index, clip in enumerate(props["timeline"]["clips"]):
        if "render_source" in (clip.get("audio") or {}):
            raise ValueError("audio source already prepared")
        if clip.get("track") in audio_tracks:
            indexed.append((index, clip))
    if not indexed:
        return copy.deepcopy(dict(props))
    if asset_server is None:
        raise ValueError("audio preparation requires the managed asset server")
    project = project_dir.resolve(strict=True)
    helper = (project / "normal-audio-sources.mjs").resolve(strict=True)
    helper.relative_to(project)
    attempt = attempt_directory.resolve(strict=True)
    attempt.relative_to(materializer.serving_root)
    clips = []
    for _, clip in indexed:
        asset = materializer.assets[clip["asset"]]
        if asset.local_path is None:
            raise ValueError("audio source has no verified materialized path")
        # Recheck the selected bytes, including reused runtime materializations.
        metadata = asset.metadata
        digest = (metadata.get("digest") or metadata.get("content_sha256")
                  or metadata.get("sha256") or metadata.get("hash"))
        with asset.local_path.open("rb") as source:
            if hashlib.file_digest(source, "sha256").hexdigest() != digest.removeprefix("sha256:"):
                raise ValueError("audio managed source failed integrity check")
        row = {"clip": clip, "source": str(asset.local_path)}
        if original_clips is not None:
            row["originalClip"] = original_clips[clip["id"]]
        clips.append(row)
    environment = probe_context(environment=os.environ, python_executable=sys.executable)["node_env"]
    directory = Path(tempfile.mkdtemp(prefix="normal-audio-", dir=attempt))
    try:
        result = subprocess.run(
            [str(runtime_tools.node_executable), str(helper)],
            input=json.dumps({"clips": clips, "fps": fps if fps is not None else props["compiledTransitions"]["fps"],
                              "attemptDirectory": str(directory)}, allow_nan=False),
            cwd=project, env=environment, capture_output=True, text=True, timeout=30, check=False,
        )
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or "audio PCM preparation unavailable")
        prepared = json.loads(result.stdout)
        if not isinstance(prepared, list) or len(prepared) != len(indexed):
            raise ValueError("invalid audio PCM preparation response")
        output = copy.deepcopy(dict(props))
        for (index, _), row in zip(indexed, prepared):
            if row is None:
                continue
            if set(row) != {"path", "envelope_key", "source_from", "playback_rate", "envelope"}:
                raise ValueError("invalid audio PCM preparation fields")
            path = Path(row["path"]).resolve(strict=True)
            path.relative_to(directory)
            clip = output["timeline"]["clips"][index]
            audio = clip.setdefault("audio", {})
            audio["envelope"] = row["envelope"]
            audio["render_source"] = {
                "file": asset_server.local_url(path),
                **{key: row[key] for key in ("envelope_key", "source_from", "playback_rate")},
            }
        if not any(row is not None for row in prepared):
            shutil.rmtree(directory)
        return output
    except BaseException:
        shutil.rmtree(directory)
        raise
