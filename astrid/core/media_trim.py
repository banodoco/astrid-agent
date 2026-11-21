"""Trim a managed video to a time range into a NEW managed media item.

``astrid media trim <media> --project P --range START,END`` reads one project
video, cuts exactly ``[START, END)`` seconds, imports the result through the same
``import_file`` path as ``astrid media import``, and records provenance as a
``derived_from`` relation from the new media to the source. The source is never
modified.

The cut is frame-exact: both ends are converted to frame indices at the
source's frame rate, and the audio is cut at the matching sample indices. Stream
copy cannot cut on arbitrary frames, so the video is re-encoded and the audio is
re-encoded to AAC. ``dry_run`` reports the bounds without importing anything.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path
from typing import Any

import shutil
import subprocess
from fractions import Fraction

from astrid.core.media import MediaProbe, MediaProbeError, ffprobe_metadata_strict
from astrid.core.media_crop import _kind_for
from astrid.sdk.contracts import DomainResult, ErrorObject
from astrid.sdk.exceptions import ServiceError

_MAX_RELATION_PAGES = 100
_TIME_DENOMINATOR = 1_000_000


def _seconds(value: float) -> Fraction:
    # The printed decimal, so 3.25 is exactly 13/4 and not a float artifact.
    return Fraction(repr(float(value))).limit_denominator(_TIME_DENOMINATOR)


def frame_bounds(start: float, end: float, fps: Fraction) -> tuple[int, int]:
    """Frame indices ``[first, after-last)``; both ends must land on a frame."""
    first, after = _seconds(start) * fps, _seconds(end) * fps
    if first.denominator != 1 or after.denominator != 1:
        raise ValueError(f"range {start}..{end} does not fall on a frame boundary at {fps} fps")
    return int(first), int(after)


def sample_bounds(start: float, end: float, sample_rate: int) -> tuple[int, int]:
    return (int(round(_seconds(start) * sample_rate)), int(round(_seconds(end) * sample_rate)))


def trim_plan(probe: MediaProbe, start: float, end: float) -> dict[str, Any]:
    """Exact frame and sample bounds for a cut, validated against the probed media."""
    if probe.fps_rational is None:
        raise ValueError("the video has no frame rate; cannot trim it frame-exactly")
    fps = Fraction(*probe.fps_rational)
    if probe.duration_seconds is not None and end > probe.duration_seconds + 1e-6:
        raise ValueError(f"range end {end} is past the video duration {probe.duration_seconds:.3f} s")
    first, after = frame_bounds(start, end, fps)
    bounds: dict[str, Any] = {"start_frame": first, "end_frame": after, "frames": after - first,
                              "fps": [fps.numerator, fps.denominator]}
    if probe.audio_codec is not None and probe.audio_sample_rate:
        a_first, a_after = sample_bounds(start, end, probe.audio_sample_rate)
        bounds.update({"start_sample": a_first, "end_sample": a_after, "samples": a_after - a_first,
                       "sample_rate": probe.audio_sample_rate})
    return {"bounds": bounds, "fps": fps}


def clip_args(src: Path, dst: Path, *, start: float, end: float, fps: Fraction,
              sample_rate: int | None, has_audio: bool) -> list[str]:
    """ffmpeg command for a frame-exact clip (video and audio cut by index)."""
    first, after = frame_bounds(start, end, fps)
    graph = f"[0:v:0]trim=start_frame={first}:end_frame={after},setpts=PTS-STARTPTS[v]"
    args = ["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(src)]
    maps = ["-map", "[v]"]
    if has_audio and sample_rate:
        a_first, a_after = sample_bounds(start, end, sample_rate)
        graph += f";[0:a:0]atrim=start_sample={a_first}:end_sample={a_after},asetpts=PTS-STARTPTS[a]"
        maps += ["-map", "[a]", "-c:a", "aac", "-b:a", "192k"]
    args += ["-filter_complex", graph, *maps,
             "-c:v", "libx264", "-preset", "medium", "-crf", "16", "-pix_fmt", "yuv420p",
             "-movflags", "+faststart", str(dst)]
    return args


def clip_file(src: Path, dst: Path, *, start: float, end: float, fps: Fraction,
              sample_rate: int | None, has_audio: bool) -> None:
    """Write the frame-exact clip to ``dst``; raises RuntimeError when ffmpeg fails."""
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("ffmpeg is required to trim video and was not found")
    args = clip_args(src, dst, start=start, end=end, fps=fps, sample_rate=sample_rate, has_audio=has_audio)
    proc = subprocess.run(args, capture_output=True, text=True, timeout=3600)
    if proc.returncode != 0 or not dst.is_file() or dst.stat().st_size == 0:
        raise RuntimeError(f"ffmpeg failed to trim: {(proc.stderr or '').strip()[-400:]}")


def parse_range(text: str) -> tuple[float, float]:
    """Parse ``START,END`` seconds; END must be after START and START at least 0."""
    parts = [part.strip() for part in str(text).split(",")]
    if len(parts) != 2:
        raise ValueError("range must be START,END in seconds")
    try:
        start, end = (float(part) for part in parts)
    except ValueError as exc:
        raise ValueError("range values must be numbers of seconds") from exc
    if not (start >= 0 and end > start):
        raise ValueError("range must satisfy 0 <= START < END")
    return start, end


def _failure(code: str, message: str, key: str = "", **details: Any) -> DomainResult[Any]:
    return DomainResult.failure(ErrorObject(code, message, dict(details)), idempotency_key=key)


def _default_key(project: str, metadata: dict[str, Any]) -> str:
    raw = json.dumps({"project": project, "metadata": metadata}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def trim_media(
    client: Any,
    *,
    project: str,
    ref: str,
    start: float,
    end: float,
    dry_run: bool = False,
    idempotency_key: str | None = None,
) -> DomainResult[Any]:
    """Cut ``[start, end)`` seconds of one project video into a new media item."""
    if project is None:
        return _failure("validation_error", "media trim requires a project", idempotency_key or "", field="project")
    shown = client.media.show(project, ref)
    if not shown.ok or not isinstance(shown.data, dict):
        return shown
    source = shown.data
    source_object_id = source.get("object_id")
    source_digest = source.get("digest")
    filename = str(source.get("filename") or source.get("stored_filename") or ref)
    if not isinstance(source_object_id, str) or not isinstance(source_digest, str):
        return _failure("protocol_error", "media row lacks object_id or digest", idempotency_key or "")
    if _kind_for(str(source.get("media_type") or ""), filename) != "video":
        return _failure("unsupported_operation", "trim supports video only", idempotency_key or "",
                        media_type=source.get("media_type"))
    try:
        data = client.media.read_bytes(source_object_id)
    except (ServiceError, OSError) as exc:
        return _failure("unavailable", f"media bytes are unavailable: {exc}", idempotency_key or "")
    try:
        with tempfile.TemporaryDirectory(prefix="astrid-trim-") as tmp:
            workdir = Path(tmp)
            src = workdir / f"source{Path(filename).suffix.lower() or '.mp4'}"
            src.write_bytes(data)
            probe = ffprobe_metadata_strict(src)
            plan = trim_plan(probe, start, end)
            result = {"dry_run": dry_run, "range": {"start": start, "end": end}, "bounds": plan["bounds"],
                      "source": {"object_id": source_object_id, "digest": source_digest}}
            if dry_run:
                return DomainResult.success(result, idempotency_key="")
            out = workdir / f"{Path(filename).stem}-trim.mp4"
            clip_file(src, out, start=start, end=end, fps=plan["fps"], sample_rate=probe.audio_sample_rate,
                      has_audio=probe.audio_codec is not None)
            payload = out.read_bytes()
            metadata = {"operation": "trim", "source_object_id": source_object_id, "source_digest": source_digest,
                        "range": {"start": start, "end": end}, "bounds": plan["bounds"]}
            key = idempotency_key or _default_key(project, metadata)
            imported = client.media.import_file(project=project, path=out, realm="managed_local",
                                                idempotency_key=key)
            if not imported.ok:
                return imported
        media = dict(imported.data) if isinstance(imported.data, dict) else {"result": imported.data}
        new_object_id = media.get("object_id")
        if not isinstance(new_object_id, str):
            return _failure("protocol_error", "imported trim has no object_id", key, media=media)
        related = client.media.relate(project, from_object_id=new_object_id, to_object_id=source_object_id,
                                      kind="derived_from", metadata=metadata, idempotency_key=key)
        if not related.ok and not _identical_relation_exists(client, project, new_object_id, source_object_id, metadata):
            error = related.error
            reason = error.message if error is not None else "no error detail"
            return _failure(error.code if error is not None else "protocol_error",
                            f"trim was imported as {new_object_id} but its provenance could not be recorded. "
                            f"Re-run with --idempotency-key {key} to record it. Reason: {reason}",
                            key, imported_object_id=new_object_id, idempotency_key=key)
        result.update({"media": media, "provenance": metadata, "provenance_recorded": "new" if related.ok else "existing"})
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
            if (row.get("from_object_id") == from_object_id and row.get("to_object_id") == to_object_id
                    and row.get("kind") == "derived_from"
                    and (row.get("metadata") or {}).get("operation") == "trim"
                    and (row.get("metadata") or {}).get("range") == metadata["range"]):
                return True
        cursor = page.data.get("next_cursor")
        if not cursor:
            return False
    return False


__all__ = ["clip_args", "clip_file", "frame_bounds", "parse_range", "sample_bounds", "trim_media", "trim_plan"]
