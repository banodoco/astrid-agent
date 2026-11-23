"""Turn one still image into a short video with a silent audio track.

``astrid media silent-video <still> --project P --duration 2.5`` loops one project
still for an exact number of frames, adds a track of digital silence, imports the
result through the same ``import_file`` path as ``astrid media import``, and
records a ``derived_from`` relation from the new video to the still. The still
is never modified.

The result is a real source for a source-backed edit: H3 can animate a listener
from it without inventing speech, because the audio is fixed at silence and the
picture starts from the still. Describe visible action only in the prompt.

Frames are exact: ``duration`` must land on a frame boundary at ``fps`` (2.5 s at
24 fps is 60 frames), and the audio has exactly the same length in samples.
``dry_run`` reports the plan without importing anything.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import tempfile
from fractions import Fraction
from pathlib import Path
from typing import Any

from astrid.core.media import MediaProbeError, ffprobe_metadata_strict
from astrid.core.media_crop import _kind_for
from astrid.sdk.contracts import DomainResult, ErrorObject
from astrid.sdk.exceptions import ServiceError

DEFAULT_FPS = 24
DEFAULT_SAMPLE_RATE = 48000
_MAX_RELATION_PAGES = 100
_TIME_DENOMINATOR = 1_000_000


def parse_size(text: str) -> tuple[int, int]:
    """Parse ``WIDTHxHEIGHT``; both must be positive even integers (4:2:0 video)."""
    match = re.fullmatch(r"\s*(\d+)\s*[xX,]\s*(\d+)\s*", str(text))
    if match is None:
        raise ValueError("size must be WIDTHxHEIGHT in pixels, for example 1024x576")
    width, height = int(match.group(1)), int(match.group(2))
    if width < 2 or height < 2 or width % 2 or height % 2:
        raise ValueError("size must be positive even numbers (yuv420p needs even width and height)")
    return width, height


def frame_count(duration: float, fps: int) -> int:
    """Whole frames in ``duration`` seconds; the duration must land on a frame."""
    if not (duration > 0):
        raise ValueError("duration must be greater than 0 seconds")
    if fps < 1:
        raise ValueError("fps must be a positive whole number")
    frames = Fraction(repr(float(duration))).limit_denominator(_TIME_DENOMINATOR) * fps
    if frames.denominator != 1:
        raise ValueError(f"duration {duration} s does not fall on a frame boundary at {fps} fps")
    return int(frames)


def silent_plan(width: int, height: int, *, duration: float, fps: int, sample_rate: int,
                size: tuple[int, int] | None) -> dict[str, Any]:
    """Exact frames, samples and output size for the clip."""
    frames = frame_count(duration, fps)
    out_w, out_h = size if size is not None else (width - width % 2, height - height % 2)
    if out_w < 2 or out_h < 2:
        raise ValueError("the still is too small to make a video from; give a --size")
    return {"frames": frames, "fps": fps, "duration": frames / fps, "sample_rate": sample_rate,
            "samples": int(round(frames * sample_rate / fps)), "width": out_w, "height": out_h,
            "fit": "cover" if size is not None else "source_size"}


def silent_args(still: Path, dst: Path, plan: dict[str, Any]) -> list[str]:
    """ffmpeg command: loop one image for ``frames`` frames and add silent stereo AAC."""
    w, h = plan["width"], plan["height"]
    if plan["fit"] == "cover":
        vf = f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},setsar=1,format=yuv420p"
    else:
        vf = f"scale={w}:{h},setsar=1,format=yuv420p"
    return ["ffmpeg", "-nostdin", "-v", "error", "-y",
            "-loop", "1", "-framerate", str(plan["fps"]), "-i", str(still),
            "-f", "lavfi", "-i", f"anullsrc=r={plan['sample_rate']}:cl=stereo",
            "-vf", vf, "-r", str(plan["fps"]), "-frames:v", str(plan["frames"]),
            "-af", f"atrim=end_sample={plan['samples']},asetpts=PTS-STARTPTS",
            "-c:v", "libx264", "-preset", "medium", "-crf", "16", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(dst)]


def silent_file(still: Path, dst: Path, plan: dict[str, Any]) -> None:
    """Write the clip to ``dst``; raises RuntimeError when ffmpeg fails."""
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("ffmpeg is required to make a silent video and was not found")
    proc = subprocess.run(silent_args(still, dst, plan), capture_output=True, text=True, timeout=600)
    if proc.returncode != 0 or not dst.is_file() or dst.stat().st_size == 0:
        raise RuntimeError(f"ffmpeg failed to make the silent video: {(proc.stderr or '').strip()[-400:]}")


def _failure(code: str, message: str, key: str = "", **details: Any) -> DomainResult[Any]:
    return DomainResult.failure(ErrorObject(code, message, dict(details)), idempotency_key=key)


def _default_key(project: str, metadata: dict[str, Any]) -> str:
    raw = json.dumps({"project": project, "metadata": metadata}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def silent_video(
    client: Any,
    *,
    project: str,
    ref: str,
    duration: float,
    fps: int = DEFAULT_FPS,
    size: tuple[int, int] | None = None,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
    dry_run: bool = False,
    idempotency_key: str | None = None,
) -> DomainResult[Any]:
    """Make a ``duration``-second video with a silent track from one project still."""
    if project is None:
        return _failure("validation_error", "media silent-video requires a project", idempotency_key or "",
                        field="project")
    shown = client.media.show(project, ref)
    if not shown.ok or not isinstance(shown.data, dict):
        return shown
    source = shown.data
    source_object_id = source.get("object_id")
    source_digest = source.get("digest")
    filename = str(source.get("filename") or source.get("stored_filename") or ref)
    if not isinstance(source_object_id, str) or not isinstance(source_digest, str):
        return _failure("protocol_error", "media row lacks object_id or digest", idempotency_key or "")
    if _kind_for(str(source.get("media_type") or ""), filename) != "image":
        return _failure("unsupported_operation", "silent-video supports still images only", idempotency_key or "",
                        media_type=source.get("media_type"))
    try:
        data = client.media.read_bytes(source_object_id)
    except (ServiceError, OSError) as exc:
        return _failure("unavailable", f"media bytes are unavailable: {exc}", idempotency_key or "")
    try:
        with tempfile.TemporaryDirectory(prefix="astrid-silent-") as tmp:
            workdir = Path(tmp)
            still = workdir / f"still{Path(filename).suffix.lower() or '.png'}"
            still.write_bytes(data)
            probe = ffprobe_metadata_strict(still)
            if not probe.width or not probe.height:
                raise ValueError("the still has no readable size")
            plan = silent_plan(int(probe.width), int(probe.height), duration=duration, fps=fps,
                               sample_rate=sample_rate, size=size)
            result = {"dry_run": dry_run, "plan": plan,
                      "source": {"object_id": source_object_id, "digest": source_digest}}
            if dry_run:
                return DomainResult.success(result, idempotency_key="")
            out = workdir / f"{Path(filename).stem}-silent.mp4"
            silent_file(still, out, plan)
            metadata = {"operation": "silent-video", "source_object_id": source_object_id,
                        "source_digest": source_digest, "plan": plan}
            key = idempotency_key or _default_key(project, metadata)
            imported = client.media.import_file(project=project, path=out, realm="managed_local",
                                                idempotency_key=key)
            if not imported.ok:
                return imported
        media = dict(imported.data) if isinstance(imported.data, dict) else {"result": imported.data}
        new_object_id = media.get("object_id")
        if not isinstance(new_object_id, str):
            return _failure("protocol_error", "imported silent video has no object_id", key, media=media)
        related = client.media.relate(project, from_object_id=new_object_id, to_object_id=source_object_id,
                                      kind="derived_from", metadata=metadata, idempotency_key=key)
        if not related.ok and not _identical_relation_exists(client, project, new_object_id, source_object_id,
                                                              metadata):
            error = related.error
            reason = error.message if error is not None else "no error detail"
            return _failure(error.code if error is not None else "protocol_error",
                            f"the silent video was imported as {new_object_id} but its provenance could not be "
                            f"recorded. Re-run with --idempotency-key {key} to record it. Reason: {reason}",
                            key, imported_object_id=new_object_id, idempotency_key=key)
        result.update({"media": media, "provenance": metadata,
                       "provenance_recorded": "new" if related.ok else "existing"})
        return DomainResult.success(result, idempotency_key="")
    except (ValueError, MediaProbeError) as exc:
        return _failure("validation_error", str(exc), idempotency_key or "")
    except RuntimeError as exc:
        return _failure("unavailable", str(exc), idempotency_key or "")


def _identical_relation_exists(client: Any, project: str, from_object_id: str, to_object_id: str,
                               metadata: dict[str, Any]) -> bool:
    cursor: str | None = None
    for _ in range(_MAX_RELATION_PAGES):
        page = client.media.list_relations(project, cursor=cursor, limit=200)
        if not page.ok or not isinstance(page.data, dict):
            return False
        for row in page.data.get("items") or []:
            relation = row.get("metadata") or {}
            if (row.get("from_object_id") == from_object_id and row.get("to_object_id") == to_object_id
                    and row.get("kind") == "derived_from" and relation.get("operation") == "silent-video"
                    and relation.get("plan") == metadata["plan"]):
                return True
        cursor = page.data.get("next_cursor")
        if not cursor:
            return False
    return False


__all__ = ["DEFAULT_FPS", "DEFAULT_SAMPLE_RATE", "frame_count", "parse_size", "silent_args", "silent_file",
           "silent_plan", "silent_video"]
