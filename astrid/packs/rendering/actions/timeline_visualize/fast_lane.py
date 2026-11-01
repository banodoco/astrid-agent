"""Bounded pixel verification through Runtime-owned V3 render and filmstrip actions.

The planner is pure. A capture uses the existing managed candidate-preview and
``rendering.timeline_visualize`` routes, then compares frames decoded from the
admitted managed videos. This module does not own a renderer or a second
timeline authority.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .provenance import snapshot_label

FAST_ROOT = Path(tempfile.gettempdir()) / "astrid-v3-fast-lane"
KEEP_RUNS = 6
MAX_MOMENTS = 4
_FFMPEG_TIMEOUT_S = 240


class FastLaneError(RuntimeError):
    """The bounded managed capture cannot proceed; the message gives the cause."""


@dataclass(frozen=True)
class Moment:
    t: float
    label: str
    clip: str | None = None


def frame_of(t: float, fps: float) -> int:
    """Map a timeline moment to its decoded frame using the editor's epsilon."""
    return max(0, int(math.floor(float(t) * float(fps) + 0.02)))


def moments_from_changes(
    changes: Iterable[Mapping[str, Any]], fps: float, *, limit: int = MAX_MOMENTS,
    names: Mapping[str, str] | None = None,
) -> list[Moment]:
    """Choose frames that expose added, removed, moved, or restyled clips."""
    if not math.isfinite(float(fps)) or float(fps) <= 0:
        raise ValueError("fps must be a positive finite number")
    if type(limit) is not int or limit < 0:
        raise ValueError("limit must be a non-negative integer")
    found: list[Moment] = []
    step = 1.0 / float(fps)
    for change in changes:
        raw = str(change.get("clip_id") or "?")
        short = raw.rsplit(":", 1)[-1]
        name = (names or {}).get(raw) or (names or {}).get(short) or short
        before, after = change.get("before"), change.get("after")
        fields = set(change.get("fields") or ())
        if after is None and before is not None:
            found.append(Moment(float(before[0]), f"{name} removed (was from {before[0]:.2f} s)", raw))
            continue
        if after is None:
            continue
        start, end = float(after[0]), float(after[1])
        if before is None or "start" in fields:
            found.extend((
                Moment(start - step, f"{name}: the frame before it starts", raw),
                Moment(start, f"{name} starts", raw),
            ))
        if before is not None and "end" in fields:
            found.extend((
                Moment(end - step, f"{name}: its last frame", raw),
                Moment(end, f"{name}: the first frame without it", raw),
            ))
        if before is not None and not fields & {"start", "end"}:
            found.append(Moment((start + end) / 2, f"{name} ({', '.join(sorted(fields)) or 'changed'}) mid-clip", raw))
    unique: dict[int, Moment] = {}
    for moment in found:
        if moment.t >= 0:
            unique.setdefault(frame_of(moment.t, fps), moment)
    return [unique[frame] for frame in sorted(unique)][:limit]


def _base_revision(draft: Any) -> str:
    bundle = getattr(draft, "bundle", None)
    parent = bundle.get("base_parent") if isinstance(bundle, Mapping) else None
    if not isinstance(parent, Mapping):
        raise FastLaneError("pixel verification needs the checkout's exact base parent revision")
    revision = parent.get("revision_id") or parent.get("parent_revision_id") or parent.get("head_revision_id")
    if not isinstance(revision, str) or not revision:
        raise FastLaneError("pixel verification needs the checkout's exact base parent revision")
    return revision


def _require_clean_base_render(client: Any, run_id: str, draft: Any) -> dict[str, Any]:
    """Verification supports normal BASE renders with matching authored canvas."""
    shown = client.runs.show(run_id)
    run = shown.data if getattr(shown, "ok", None) is True else None
    if not isinstance(run, Mapping) or (run.get("run_id") or run.get("id")) != run_id or run.get("status") != "completed":
        raise FastLaneError("verification BASE needs its exact completed managed render")
    task_ids = run.get("task_ids")
    if not isinstance(task_ids, list) or len(task_ids) != 1 or not isinstance(task_ids[0], str):
        raise FastLaneError("verification BASE needs one exact original render task")
    observed = client.tasks.show(task_ids[0])
    task = observed.data if getattr(observed, "ok", None) is True else None
    if (not isinstance(task, Mapping) or task.get("task_id") != task_ids[0]
            or task.get("run_id") != run_id or task.get("state") != "succeeded"
            or task.get("capability_id") != "rendering.render"):
        raise FastLaneError("verification BASE needs its successful original render task")
    envelope = task.get("spec")
    spec = envelope.get("spec") if isinstance(envelope, Mapping) else None
    inputs = spec.get("inputs") if isinstance(spec, Mapping) else None
    if not isinstance(inputs, Mapping):
        raise FastLaneError("verification BASE render input envelope is unavailable")
    if inputs.get("review", False) is not False or inputs.get("profile") is not None:
        raise FastLaneError("verification BASE must be a normal non-review render without a custom profile")
    snapshot = inputs.get("timeline_snapshot")
    config = snapshot.get("config") if isinstance(snapshot, Mapping) else None
    if not isinstance(config, Mapping):
        raise FastLaneError("verification BASE frozen canvas is unavailable")
    candidate = draft.document()
    parent = candidate.get("parent") if isinstance(candidate, Mapping) else None
    working_config = parent.get("config") if isinstance(parent, Mapping) else None
    if not isinstance(working_config, Mapping):
        raise FastLaneError("verification WORKING frozen canvas is unavailable")
    def canvas(value: Mapping[str, Any]) -> Any:
        theme = value.get("theme_overrides", {})
        visual = theme.get("visual", {}) if isinstance(theme, Mapping) else {}
        return visual.get("canvas", {}) if isinstance(visual, Mapping) else {}
    base_canvas, working_canvas = canvas(config), canvas(working_config)
    if not isinstance(base_canvas, Mapping) or not isinstance(working_canvas, Mapping) or dict(base_canvas) != dict(working_canvas):
        raise FastLaneError("verification BASE and WORKING must have the same frozen canvas dimensions and frame rate")
    return dict(base_canvas)


def prepare_pair(client: Any, project: str, timeline: str, draft: Any, *, media_root: Path) -> dict[str, Any]:
    """Pin the base render and freeze/render the working candidate through V3."""
    from astrid.sdk.authoring_bundle import render_authoring_candidate_preview
    from astrid.sdk.timeline_filmstrip import prepare_filmstrip

    remote = getattr(client, "_remote", client)
    base = prepare_filmstrip(
        {"timeline_slug": timeline, "revision_id": _base_revision(draft),
         "composed_capture": True, "include_media": True, "show": ["output"]},
        project=project, client=remote,
    )
    base_run = base.get("render_run_id")
    if not isinstance(base_run, str) or not base_run:
        raise FastLaneError("no exact managed render is available for the checkout's base revision")
    _require_clean_base_render(client, base_run, draft)

    # Resolve the exact managed BASE video without executing a frame capture.
    # The initial composed authority above only selects the saved BASE revision.
    base_authority = prepare_filmstrip(
        {"timeline_slug": timeline, "render_run": base_run, "show": ["output"], "include_media": True},
        project=project, client=remote,
    )
    base_snapshot = base_authority.get("filmstrip_snapshot")
    base_video, base_media = _base_video(base_authority, client=client, project=project,
                                         run_id=base_run, media_root=media_root)

    candidate = render_authoring_candidate_preview(
        draft.document(), client, project=project, timeline_ref=timeline, wait=True, review=False,
    )
    if getattr(candidate, "ok", None) is not True:
        error = getattr(candidate, "error", None)
        raise FastLaneError(f"candidate preview failed: {error or 'managed render did not succeed'}")
    working_run = getattr(candidate, "run_id", None)
    if not isinstance(working_run, str) or not working_run:
        raise FastLaneError("candidate preview returned no managed render identity")

    working_authority = prepare_filmstrip(
        {"timeline_slug": timeline, "render_run": working_run, "show": ["output"], "include_media": True},
        project=project, client=remote,
    )
    working_snapshot = working_authority.get("filmstrip_snapshot")
    working_video, working_media = _candidate_video(candidate, working_authority, client=client, project=project, media_root=media_root)
    if not isinstance(base_snapshot, Mapping) or not isinstance(working_snapshot, Mapping):
        raise FastLaneError("the exact managed renders did not yield filmstrip snapshots")
    return {"base_run": base_run, "working_run": working_run,
            "base_snapshot": base_snapshot, "working_snapshot": working_snapshot,
            "base_video": base_video, "base_media": base_media,
            "working_video": working_video, "working_media": working_media}


def _base_video(authority: Mapping[str, Any], *, client: Any, project: str,
                run_id: str, media_root: Path) -> tuple[Path, dict[str, Any]]:
    """Materialize the exact admitted saved BASE render, without Chrome capture."""
    snapshot = authority.get("filmstrip_snapshot")
    if (authority.get("render_run_id") != run_id or not isinstance(snapshot, Mapping)
            or snapshot.get("render_run_id") != run_id
            or (snapshot.get("metadata") or {}).get("selection") not in
                {"explicit_current_render", "explicit_historical_render"}
            or authority.get("include_media") is not True):
        raise FastLaneError("BASE media is not an admitted exact saved render")
    expected = str(authority.get("video_digest") or "").removeprefix("sha256:")
    if (len(expected) != 64 or any(char not in "0123456789abcdef" for char in expected)
            or snapshot.get("video_digest") != authority.get("video_digest")):
        raise FastLaneError("BASE managed-video digest authority is invalid")
    observed = (client.runs.show(run_id), client.projects.show(project))
    if any(getattr(value, "ok", None) is not True or not isinstance(getattr(value, "data", None), Mapping)
           for value in observed):
        raise FastLaneError("BASE original run or project is unavailable")
    run, project_row = (value.data for value in observed)
    project_id = project_row.get("project_id") or project_row.get("id")
    task_ids = run.get("task_ids")
    if (not project_id or authority.get("project_id") != project_id
            or (run.get("run_id") or run.get("id")) != run_id or run.get("project_id") != project_id
            or run.get("status") != "completed" or not isinstance(task_ids, list)
            or len(task_ids) != 1 or not isinstance(task_ids[0], str)):
        raise FastLaneError("BASE media does not belong to its exact original render and project")
    observed_task = client.tasks.show(task_ids[0])
    task = observed_task.data if getattr(observed_task, "ok", None) is True else None
    if (not isinstance(task, Mapping) or (task.get("task_id") or task.get("id")) != task_ids[0]
            or task.get("run_id") != run_id or task.get("state") != "succeeded"
            or task.get("capability_id") != "rendering.render"):
        raise FastLaneError("BASE media has no successful exact original render task")
    result = task.get("result")
    outputs = result.get("outputs") if isinstance(result, Mapping) else None
    videos = [output for output in (outputs if isinstance(outputs, list) else [])
              if isinstance(output, Mapping)
              and (output.get("name") or output.get("output_port") or output.get("port")) == "video"]
    if len(videos) != 1 or str(videos[0].get("digest") or "").removeprefix("sha256:") != expected:
        raise FastLaneError("BASE media digest does not identify its original task video")
    object_id = authority.get("video_object_id")
    read_bytes = getattr(getattr(client, "media", None), "read_bytes", None)
    if not isinstance(object_id, str) or not object_id or not callable(read_bytes):
        raise FastLaneError("BASE media has no admitted object byte materialization route")
    data = read_bytes(object_id)
    if not isinstance(data, bytes) or hashlib.sha256(data).hexdigest() != expected:
        raise FastLaneError("original BASE video does not match its admitted managed digest")
    owned = Path(tempfile.mkdtemp(prefix="astrid-base-render-", dir=media_root))
    video = owned / "video.mp4"
    try:
        with video.open("xb") as stream:
            stream.write(data)
        with video.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != expected:
                raise FastLaneError("materialized BASE video changed before decoding")
    except BaseException:
        shutil.rmtree(owned)
        raise
    return video, {"render_run_id": run_id, "digest": "sha256:" + expected,
                   "selection": (snapshot.get("metadata") or {})["selection"], "verified": True}


def _candidate_video(candidate: Any, authority: Mapping[str, Any], *, client: Any, project: str, media_root: Path) -> tuple[Path, dict[str, Any]]:
    """Check the original managed preview artifact without saved-parent capture."""
    run_id = getattr(candidate, "run_id", None)
    snapshot = authority.get("filmstrip_snapshot")
    if (not isinstance(run_id, str) or not run_id
            or authority.get("render_run_id") != run_id
            or not isinstance(snapshot, Mapping) or snapshot.get("render_run_id") != run_id):
        raise FastLaneError("candidate media authority does not identify the exact managed preview")
    if (snapshot.get("metadata") or {}).get("selection") != "explicit_candidate_preview":
        raise FastLaneError("working media is not an admitted explicit candidate preview")
    expected = str(authority.get("video_digest") or "").removeprefix("sha256:")
    if (len(expected) != 64 or any(char not in "0123456789abcdef" for char in expected)
            or snapshot.get("video_digest") != authority.get("video_digest")):
        raise FastLaneError("candidate managed-video digest authority is invalid")
    # InvocationResult carries durable output identities, not local video paths.
    # Revalidate the original task in the exact successful, frozen preview run.
    task_id = getattr(candidate, "kernel_task_id", None)
    if (getattr(candidate, "ok", None) is not True
            or getattr(candidate, "kernel_run_id", None) != run_id
            or not isinstance(task_id, str) or not task_id):
        raise FastLaneError("candidate preview has no exact original kernel run and task")
    observed = (client.runs.show(run_id), client.tasks.show(task_id), client.projects.show(project))
    if any(getattr(result, "ok", None) is not True or not isinstance(getattr(result, "data", None), Mapping)
           for result in observed):
        raise FastLaneError("candidate original run, task or project is unavailable")
    run, task, project_row = (result.data for result in observed)
    project_id = project_row.get("project_id") or project_row.get("id")
    if (not project_id or authority.get("project_id") != project_id
            or (run.get("id") or run.get("run_id")) != run_id
            or run.get("project_id") != project_id
            or task_id not in (run.get("task_ids") or [])
            or (task.get("task_id") or task.get("id")) != task_id
            or run.get("status") != "completed"
            or (task.get("state") or task.get("status")) != "succeeded"
            or authority.get("include_media") is not True):
        raise FastLaneError("candidate media does not belong to its successful original task and project")
    object_id = authority.get("video_object_id")
    if not isinstance(object_id, str) or not object_id:
        raise FastLaneError("candidate preview returned no admitted managed video reference")
    read_bytes = getattr(getattr(client, "media", None), "read_bytes", None)
    if not callable(read_bytes):
        raise FastLaneError("the connected SDK client has no object byte materialization route")
    data = read_bytes(object_id)
    if not isinstance(data, bytes) or hashlib.sha256(data).hexdigest() != expected:
        raise FastLaneError("original candidate video does not match its admitted managed digest")
    # Materialize only to a new owned directory; rehash before decoding.
    owned = Path(tempfile.mkdtemp(prefix="astrid-working-preview-", dir=media_root))
    video = owned / "video.mp4"
    try:
        with video.open("xb") as stream:
            stream.write(data)
        with video.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != expected:
                raise FastLaneError("materialized candidate video changed before decoding")
    except BaseException:
        shutil.rmtree(owned)
        raise
    return video, {"render_run_id": run_id, "digest": "sha256:" + expected,
                   "selection": "explicit_candidate_preview", "verified": True}


def _invoke_video(client: Any, project: str, timeline: str, run_id: str, frame: int) -> Path:
    """Ask the existing V3 filmstrip action for the exact managed video and frame."""
    visualize = getattr(getattr(client, "timelines", None), "visualize", None)
    if not callable(visualize):
        raise FastLaneError("the connected SDK client has no canonical timeline visualization route")
    result = visualize(
        project, timeline, mode="composed", formats=("png",),
        options={"render_run": run_id, "view": "filmstrip",
                 "show": ["output"], "frame": int(frame), "include_media": True},
    )
    if getattr(result, "ok", None) is not True:
        error = getattr(result, "error", None)
        raise FastLaneError(f"managed filmstrip for render {run_id} failed: {error or 'invocation failed'}")
    outputs = getattr(result, "outputs", None)
    media = outputs.get("media") if isinstance(outputs, Mapping) else None
    if not isinstance(media, str) or not Path(media).is_file():
        raise FastLaneError(f"managed filmstrip for render {run_id} returned no verified video attachment")
    index_path = outputs.get("frame_index") if isinstance(outputs, Mapping) else None
    if not isinstance(index_path, str) or not Path(index_path).is_file():
        raise FastLaneError(f"managed filmstrip for render {run_id} returned no frame-index receipt")
    try:
        index = json.loads(Path(index_path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise FastLaneError("managed filmstrip frame-index receipt is unreadable") from exc
    # Capture provenance identifies composition frames; managed media has its own exact run.
    media_record = index.get("media") if isinstance(index, Mapping) else None
    if not isinstance(media_record, Mapping) or str(media_record.get("render_run_id")) != run_id:
        raise FastLaneError("filmstrip frame-index does not identify the requested managed render")
    if not isinstance(media_record, Mapping) or media_record.get("verified") is not True:
        raise FastLaneError("filmstrip frame-index does not attest its managed video attachment")
    expected = str(media_record.get("digest") or "").removeprefix("sha256:")
    if len(expected) != 64 or any(ch not in "0123456789abcdef" for ch in expected):
        raise FastLaneError("filmstrip managed-video digest is invalid")
    with Path(media).open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    if digest != expected:
        raise FastLaneError("materialized managed video does not match its filmstrip digest")
    return Path(media)


def decode_frames(video: Path, frames: Sequence[int], out_dir: Path) -> dict[int, Path]:
    """Decode selected full-resolution PNG frames from a verified managed video."""
    unique = sorted({int(frame) for frame in frames})
    if not unique:
        return {}
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise FastLaneError("ffmpeg is required to compare decoded managed-render pixels")
    if any(frame < 0 for frame in unique):
        raise FastLaneError("frame numbers must be non-negative")
    out_dir.mkdir(parents=True, exist_ok=True)
    expression = "+".join(f"eq(n\\,{frame})" for frame in unique)
    pattern = out_dir / "frame-%05d.png"
    command = [ffmpeg, "-v", "error", "-i", str(video), "-vf", f"select={expression}",
               "-fps_mode:v", "passthrough", "-frames:v", str(len(unique)), "-y", str(pattern)]
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=_FFMPEG_TIMEOUT_S, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise FastLaneError(f"ffmpeg could not decode managed video frames: {exc}") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or "ffmpeg failed").strip().splitlines()
        raise FastLaneError(f"ffmpeg could not decode managed video frames: {detail[-1] if detail else 'unknown error'}")
    paths = sorted(out_dir.glob("frame-*.png"))
    if len(paths) != len(unique):
        raise FastLaneError(f"managed video yielded {len(paths)} of {len(unique)} requested decoded frames")
    try:
        from PIL import Image

        for path in paths:
            with Image.open(path) as image:
                image.verify()
    except Exception as exc:  # PIL reports several decode exception types
        raise FastLaneError(f"a decoded managed frame is not a valid PNG: {exc}") from exc
    return dict(zip(unique, paths))


# ---------------------------------------------------------------- what each side shows, in words

def _short(clip_id: Any) -> str:
    return str(clip_id or "?").rsplit(":", 1)[-1]


def clip_address(clip: Mapping[str, Any]) -> str:
    """``c41.sparks`` (cut.layer, the address edits print), else the clip's short id."""
    from astrid.core.timeline.astrid_ns import read_app

    app = read_app(clip)
    cut, layer = app.get("cut"), app.get("layer")
    return f"{cut}.{layer}" if cut and layer else _short(clip.get("id"))


def _clip_span_frames(clip: Mapping[str, Any], fps: float) -> tuple[int, int] | None:
    from astrid.core.timeline.duration import clip_end_frame, clip_start_frame

    try:
        return clip_start_frame(clip, fps), clip_end_frame(clip, fps)
    except (TypeError, ValueError):
        return None


def _visual_clips(config: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    audio = {str(t.get("id")) for t in config.get("tracks") or () if isinstance(t, Mapping) and t.get("kind") == "audio"}
    return {_short(c.get("id")): c for c in config.get("clips") or ()
            if isinstance(c, Mapping) and str(c.get("track")) not in audio}


def _look(clip: Mapping[str, Any]) -> str:
    """What decides how a clip looks, besides time: its asset, params and track."""
    return json.dumps([clip.get("asset"), clip.get("params"), clip.get("track")], sort_keys=True, default=str)


def _on(clip: Mapping[str, Any] | None, frame: int, fps: float) -> bool:
    span = _clip_span_frames(clip, fps) if clip is not None else None
    return bool(span) and span[0] <= frame < span[1]


def describe_sides(moment: Moment, frame: int, fps: float, published: Mapping[str, Any],
                   working: Mapping[str, Any] | None) -> tuple[str, str]:
    """Plain words for what each side shows on this frame (``published: …``, ``working copy: …``).

    For an edited clip: on screen, not yet (with its start) or no longer (with its end), on
    each side. For a bare moment: the layers only one side has, and the ones that changed.
    """
    pub, work = _visual_clips(published), _visual_clips(working or {})
    if moment.clip:
        key = _short(moment.clip)
        clip = work.get(key) or pub.get(key)
        name = clip_address(clip) if clip else key

        def side(mine: Mapping[str, Any] | None, other: Mapping[str, Any] | None, *, yours: bool) -> str:
            if mine is None:
                return f"no {name} (not in this version)"
            span = _clip_span_frames(mine, fps)
            if span and frame < span[0]:
                return f"no {name} yet (it starts at {span[0] / fps:.2f} s)"
            if span and frame >= span[1]:
                return f"no {name} any more (it ended at {span[1] / fps:.2f} s)"
            fields = [f for f in ("asset", "params", "track") if other is not None and mine.get(f) != other.get(f)]
            if not fields:
                return f"{name} on screen"
            return f"{name} on screen with your change ({', '.join(fields)})" if yours else f"{name} on screen, before your change"

        return (side(pub.get(key), work.get(key), yours=False),
                side(work.get(key), pub.get(key), yours=True) if working is not None else "—")
    if working is None:
        return "what is published", "—"
    on_pub = {k for k, c in pub.items() if _on(c, frame, fps)}
    on_work = {k for k, c in work.items() if _on(c, frame, fps)}
    gone = sorted(clip_address(pub[k]) for k in on_pub - on_work)
    new = sorted(clip_address(work[k]) for k in on_work - on_pub)
    changed = sorted(clip_address(work[k]) for k in on_pub & on_work if _look(pub[k]) != _look(work[k])
                     or _clip_span_frames(pub[k], fps) != _clip_span_frames(work[k], fps))
    left = f"{', '.join(gone)} (not in the working copy here)" if gone else "the same layers"
    parts = ([f"{', '.join(new)} (new here)"] if new else []) + ([f"{', '.join(changed)} changed"] if changed else [])
    return left, "; ".join(parts) if parts else "the same layers, unchanged"


def _changed_on_screen(configs: Mapping[str, Mapping[str, Any]], frame: int, fps: float) -> list[str]:
    """Short ids of the clips on screen on both sides whose look or timing differs."""
    pub, work = _visual_clips(configs.get("published") or {}), _visual_clips(configs.get("working") or {})
    return [k for k in sorted(set(pub) & set(work)) if _on(pub[k], frame, fps) and _on(work[k], frame, fps)
            and (_look(pub[k]) != _look(work[k]) or _clip_span_frames(pub[k], fps) != _clip_span_frames(work[k], fps))]


OPAQUE_KINDS = ("plate", "panel", "card")  # boxes that hide what is under them (sprites and type have holes)


def hidden_under(snapshot: Mapping[str, Any], key: str, t: float, fps: float, *, cover: float = 0.9) -> str | None:
    """The address of a higher, opaque layer covering ``cover`` of this clip's box at ``t``, if any.

    Stacking is by track, top first in the timeline's track list (chrome > type > fx > sprite >
    plate). Plates, panels and cards count as opaque; sprites and type do not.
    """
    from .motion import model
    from .motion.sheet import snapshot_elements

    order = [str(tr.get("id")) for tr in (snapshot.get("config") or {}).get("tracks") or snapshot.get("tracks") or ()
             if isinstance(tr, Mapping)]
    rank = {track: index for index, track in enumerate(order)}
    _occurrences, elements = snapshot_elements(snapshot)
    moment = t + 1e-6
    target = next((e for e in elements if _short(e.clip.get("id") or e.id) == _short(key) and not e.audio
                   and e.start - 1e-6 <= moment < e.end - 1e-6), None)
    if target is None or target.track not in rank:
        return None
    rects = [box.rect for box in model.boxes_at(target, moment, fps)]
    if not rects:
        return None
    mine = (min(r[0] for r in rects), min(r[1] for r in rects), max(r[2] for r in rects), max(r[3] for r in rects))
    if model.area(mine) <= 0:
        return None
    for element in sorted(elements, key=lambda e: rank.get(e.track, 99)):
        if element is target or element.audio or rank.get(element.track, 99) >= rank[target.track]:
            continue
        for box in model.boxes_at(element, moment, fps):
            if box.kind in OPAQUE_KINDS and model.intersection(box.rect, mine) >= cover * model.area(mine):
                return clip_address(element.clip) if element.clip else element.short_id
    return None


# ---------------------------------------------------------------- zoom: a layer at full resolution

ZOOM_WIDTH = 984  # the page's inner width: a zoom crop shows at 1x (or a whole-number enlargement) up to this


def zoom_scale(size: tuple[int, int], *, width: int = ZOOM_WIDTH, height: int = 420) -> float:
    """1x when it fits (full resolution), a whole-number enlargement when small, a reduction only when wider."""
    if size[0] > width:
        return width / float(size[0])
    return float(max(1, min(6, int(width // size[0]), int(height // max(1, size[1])))))


def _zoom_tile(path: str | Path, scale: float) -> Any:
    from PIL import Image

    with Image.open(path) as source:
        image = source.convert("RGB")
        size = (max(1, round(image.size[0] * scale)), max(1, round(image.size[1] * scale)))
        return image.resize(size, Image.NEAREST if scale >= 1 else Image.LANCZOS)


def difference(a: Path, b: Path) -> tuple[float, int, tuple[int, int, int, int] | None, tuple[int, int]]:
    """Return the fraction, count, bounds, and size of pixels that differ."""
    from PIL import Image, ImageChops

    with Image.open(a) as left, Image.open(b) as right:
        left, right = left.convert("RGB"), right.convert("RGB")
        if left.size != right.size:
            raise FastLaneError(f"managed renders have different decoded sizes: {left.size} and {right.size}")
        delta = ImageChops.difference(left, right).convert("L").point(lambda value: 255 if value > 8 else 0)
        box, count, size = delta.getbbox(), delta.histogram()[255], left.size
    return count / float(size[0] * size[1]), count, box, size


def parse_region(value: Sequence[int] | str | None) -> tuple[int, int, int, int] | None:
    if value is None:
        return None
    try:
        if isinstance(value, str):
            x, y, width, height = (int(round(float(part))) for part in value.split(","))
        else:
            x, y, width, height = (int(part) for part in value)
    except (TypeError, ValueError) as exc:
        raise FastLaneError(f"--region takes x,y,w,h in canvas px, got {value!r}") from exc
    if width <= 0 or height <= 0:
        raise FastLaneError(f"--region needs a positive width and height, got {value!r}")
    return x, y, x + width, y + height


def zoom_region(snapshots: Sequence[Mapping[str, Any]], address: str, t: float, fps: float,
                canvas: tuple[int, int]) -> tuple[int, int, int, int] | None:
    """Find a padded V3 motion-box union for the requested layer on the sampled frame."""
    from astrid.core.timeline.astrid_ns import read_app
    from astrid.packs.rendering.actions.timeline_visualize.motion import model
    from astrid.packs.rendering.actions.timeline_visualize.motion.sheet import snapshot_elements

    rects: list[tuple[float, float, float, float]] = []
    for snapshot in snapshots:
        _occurrences, elements = snapshot_elements(snapshot)
        for element in elements:
            app = read_app(element.clip)
            target = f"{app.get('cut')}.{app.get('layer')}"
            if target != address.strip() and element.short_id != address.strip() and not str(element.id).endswith(address.strip()):
                continue
            boxes = [box.rect for box in model.boxes_at(element, t + 1e-6, fps)]
            anchor = element.params.get("anchor") if isinstance(element.params.get("anchor"), Mapping) else None
            if boxes and anchor and all(isinstance(anchor.get(key), (int, float)) for key in ("x", "y")):
                boxes.append((anchor["x"], anchor["y"], anchor["x"] + 1, anchor["y"] + 1))
            rects.extend(boxes)
    if not rects:
        return None
    x0, y0 = min(rect[0] for rect in rects), min(rect[1] for rect in rects)
    x1, y1 = max(rect[2] for rect in rects), max(rect[3] for rect in rects)
    width, height = max(x1 - x0, 1.0), max(y1 - y0, 1.0)
    grow_x, grow_y = max(width * 0.25, (160 - width) / 2), max(height * 0.25, (160 - height) / 2)
    return (max(0, int(x0 - grow_x)), max(0, int(y0 - grow_y)),
            min(canvas[0], int(math.ceil(x1 + grow_x))), min(canvas[1], int(math.ceil(y1 + grow_y))))


def _crop(path: Path, box: tuple[int, int, int, int], out: Path) -> Path:
    from PIL import Image

    with Image.open(path) as image:
        image.convert("RGB").crop(box).save(out)
    return out


def _canvas(snapshot: Mapping[str, Any]) -> tuple[int, int]:
    config = snapshot.get("config") if isinstance(snapshot.get("config"), Mapping) else {}
    canvas = (((config.get("theme_overrides") or {}).get("visual") or {}).get("canvas") or {})
    try:
        return int(canvas.get("width") or 1920), int(canvas.get("height") or 1080)
    except (TypeError, ValueError):
        return 1920, 1080


def _canvas_bounds(box: tuple[int, int, int, int] | None, size: tuple[int, int],
                   canvas: tuple[int, int], *,
                   offset: tuple[int, int] = (0, 0)) -> tuple[int, int, int, int] | None:
    """Map a raw decoded-frame rectangle to authored canvas coordinates."""
    if box is None:
        return None
    sx, sy = (1.0, 1.0) if offset != (0, 0) else (canvas[0] / float(size[0]), canvas[1] / float(size[1]))
    x0, y0 = round(offset[0] + box[0] * sx), round(offset[1] + box[1] * sy)
    x1, y1 = round(offset[0] + box[2] * sx), round(offset[1] + box[3] * sy)
    return x0, y0, x1, y1


def pixel_line(share: float, count: int, box: tuple[int, int, int, int] | None,
               size: tuple[int, int], canvas: tuple[int, int], *,
               offset: tuple[int, int] = (0, 0)) -> str:
    """``pixels: 3.04% differ, changed area (old ∪ new) 558,522–1527,723 (…)``; tiny changes say ``<0.1% (12 px)``."""
    if box is None:
        return "pixels: identical to published"
    amount = f"{share * 100:.2f}%" if share >= 0.001 else f"<0.1% ({count} px)"
    canvas_bounds = _canvas_bounds(box, size, canvas, offset=offset)
    if canvas_bounds is None:
        return "pixels: identical to published"
    x0, y0, x1, y1 = canvas_bounds
    return (f"pixels: {amount} differ, changed area (old ∪ new) {x0},{y0}–{x1},{y1} "
            f"({x1 - x0}x{y1 - y0} canvas px; where either side differs, not a layer's box)")


_pixel_line = pixel_line


TILE = (480, 270)


def _compose_page(rows: Sequence[Mapping[str, Any]], path: Path, *, title: str,
                  footer: Sequence[str] = ()) -> Path:
    """One block per moment: published | working copy, what each shows, what differs; a zoom pair when asked."""
    from PIL import Image, ImageDraw

    from .layers.base import PALETTE, draw_text

    tile_w, tile_h = TILE
    width = 24 + 2 * (tile_w + 12)

    def zoom_height(row: Mapping[str, Any]) -> int:
        zoom = row.get("zoom") or {}
        if not zoom.get("published"):
            return 26 if zoom else 0
        region = zoom["region"]
        scale = zoom_scale((region[2] - region[0], region[3] - region[1]))
        return 2 * (round((region[3] - region[1]) * scale) + 24) + 26

    heights = [tile_h + 70 + zoom_height(row) for row in rows]
    height = 64 + sum(heights) + 22 * len(footer) + 16
    page = Image.new("RGB", (width, max(height, 120)), PALETTE["bg"])
    draw = ImageDraw.Draw(page)
    draw_text(draw, (12, 10), title, 20, "white")
    draw_text(draw, (12, 38),
              "left: published · right: working copy · the same frame, decoded from each version's managed render",
              13, PALETTE["muted"])
    top = 64
    for row, block in zip(rows, heights):
        for column, key in enumerate(("published", "working")):
            x = 12 + column * (tile_w + 12)
            shown = (row.get("labelled") or {}).get(key) or row.get(key)
            if shown and Path(str(shown)).is_file():
                with Image.open(shown) as source:
                    page.paste(source.convert("RGB").resize((tile_w, tile_h), Image.Resampling.LANCZOS), (x, top))
            else:
                draw.rectangle((x, top, x + tile_w, top + tile_h), outline=PALETTE["grid_strong"])
                draw_text(draw, (x + 8, top + 8), "no frame", 13, PALETTE["muted"])
        box, size = row.get("box"), row.get("size")
        if box and size:
            sx, sy = tile_w / float(size[0]), tile_h / float(size[1])
            x = 12 + tile_w + 12
            draw.rectangle((x + box[0] * sx - 2, top + box[1] * sy - 2, x + box[2] * sx + 2, top + box[3] * sy + 2),
                           outline="#f59e0b", width=2)
        y = top + tile_h + 6
        draw_text(draw, (12, y), f"{row['label']}  ·  {float(row['t']):.2f} s, frame {row['frame']}", 13, PALETTE["ink"])
        if row.get("published_shows") is not None:
            draw_text(draw, (12, y + 20),
                      f"published: {row['published_shows']}   ·   working copy: {row['working_shows']}", 12,
                      PALETTE["ink"])
        draw_text(draw, (12, y + 38), str(row["pixels"])[:170], 12, PALETTE["muted"])
        zoom = row.get("zoom")
        if zoom:
            ztop = top + tile_h + 64
            draw_text(draw, (12, ztop), str(zoom["label"])[:160], 12, PALETTE["muted"])
            ztop += 22
            if zoom.get("published"):
                region = zoom["region"]
                scale = zoom_scale((region[2] - region[0], region[3] - region[1]))
                for key, name in (("published", "published"), ("working", "working copy")):
                    if not zoom.get(key):
                        continue
                    crop = _zoom_tile(zoom[key], scale)
                    draw_text(draw, (12, ztop), f"{name} · shown at {scale:g}x (1x = full resolution)", 12, PALETTE["ink"])
                    page.paste(crop, (12, ztop + 18))
                    change = zoom.get("box") if key == "working" else None
                    if change:
                        draw.rectangle((12 + change[0] * scale - 2, ztop + 18 + change[1] * scale - 2,
                                        12 + change[2] * scale + 2, ztop + 18 + change[3] * scale + 2),
                                       outline="#f59e0b", width=2)
                    ztop += crop.size[1] + 24
        top += block
    y = top + 4
    for line in footer:
        draw_text(draw, (12, y), str(line)[:150], 13, PALETTE["ink"])
        y += 22
    path.parent.mkdir(parents=True, exist_ok=True)
    page.save(path)
    return path


def _labelled_tiles(
    snapshots: Mapping[str, Mapping[str, Any]], frame: int, fps: float,
    paths: Mapping[str, str | Path | None], out_dir: Path, index: int,
) -> dict[str, str]:
    """Create labeled display copies; retain the decoded files for pixel comparison."""
    from PIL import Image

    from .overlays.labels import overlay_labels_frame

    labelled: dict[str, str] = {}
    for side, path in paths.items():
        snapshot = snapshots.get(side)
        if not path or not isinstance(snapshot, Mapping):
            continue
        config = snapshot.get("config")
        if not isinstance(config, Mapping):
            continue
        with Image.open(path) as source:
            tile = source.convert("RGB")
        target = out_dir / f"labelled-{index}-{side}.png"
        overlay_labels_frame(tile, config, frame, fps).save(target)
        labelled[side] = str(target)
    return labelled


def _moment_label(moment: Moment) -> str:
    label = str(getattr(moment, "label", "") or "").strip()
    return label or f"{moment.t:.2f}"


def _prune_runs(root: Path, keep: int = KEEP_RUNS) -> None:
    """Keep the newest ``keep`` verify/capture run directories (crash leftovers included)."""
    for pattern in ("verify-*", "capture-*"):
        runs = sorted((p for p in root.glob(pattern) if p.is_dir() and not p.is_symlink()),
                      key=lambda p: p.stat().st_mtime, reverse=True)
        for stale in runs[keep:]:
            shutil.rmtree(stale, ignore_errors=True)


def verify_lines(result: Mapping[str, Any]) -> list[str]:
    """What the CLI prints: per moment what each side shows and what differs; the page; where the time went."""
    lines = []
    for row in result["rows"]:
        lines.append(f"verify  {row['label']} · {row['t']:.2f} s (f{row['frame']})")
        lines.append(f"        published: {row['published_shows']} · working copy: {row['working_shows']}")
        lines.append(f"        {row['pixels']}")
        if row.get("zoom"):
            lines.append(f"        {row['zoom']['label']}")
    lines.append(f"page    {result['page']}   (left published, right working copy; read it)")
    timing = result["timing"]
    lines.append(f"time    {timing['total_s']:.1f} s (decoded from the two managed renders; no capture)")
    labels = result.get("snapshot_labels") or {}
    if labels:
        lines.append(f"snapshot published: {(labels.get('published') or {}).get('label', 'revision unavailable')}")
        lines.append(f"snapshot working: {(labels.get('working') or {}).get('label', 'revision unavailable')}")
    lines.extend(str(note) for note in result.get("notes") or ())
    return lines


def verify(
    client: Any, project: str, timeline: str, moments: Sequence[Moment], *, draft: Any = None,
    footer: Sequence[str] = (), resolution: Sequence[int] = (640, 360), root: Path | None = None,
    served: Mapping[str, Any] | None = None, zoom: str | None = None,
    region: Sequence[int] | str | None = None, labels: bool = False,
) -> dict[str, Any]:
    """Compare published and candidate pixels from their exact managed videos."""
    del resolution, served  # V3 compares native decoded frames; page scaling never affects the diff.
    if not moments:
        raise FastLaneError("no moment to verify")
    if draft is None:
        raise FastLaneError("pixel verification needs the checkout working copy")
    if zoom and region:
        raise FastLaneError("choose zoom or region, not both")
    root = root or FAST_ROOT
    root.mkdir(parents=True, exist_ok=True)
    try:  # the CLI start stamp (astrid.core.gateway.main): what the check cost before this call began
        startup_s = max(0.0, time.monotonic() - float(os.environ.get("ASTRID_CLI_START_MONO", "")))
    except ValueError:
        startup_s = 0.0
    _prune_runs(root)
    started = time.monotonic()
    run_dir = Path(tempfile.mkdtemp(prefix="verify-", dir=root))
    pair = None
    try:
        pair = prepare_pair(client, project, timeline, draft, media_root=run_dir)
        rational = pair["base_snapshot"].get("fps_rational", [30, 1])
        fps = float(Fraction(int(rational[0]), int(rational[1])))
        requested = list(moments)
        selected, skipped = requested[:MAX_MOMENTS], requested[MAX_MOMENTS:]
        frames = sorted({frame_of(moment.t, fps) for moment in selected})
        published_video = pair["base_video"]
        working_video = pair["working_video"]
        published = decode_frames(published_video, frames, run_dir / "published")
        working = decode_frames(working_video, frames, run_dir / "working")
        canvas = _canvas(pair["base_snapshot"])
        fixed_region = parse_region(region)
        rows = []
        base_config = pair["base_snapshot"].get("config") or {}
        working_config = pair["working_snapshot"].get("config") or {}
        for index, moment in enumerate(selected):
            frame = frame_of(moment.t, fps)
            pub_path, work_path = published[frame], working[frame]
            crop = fixed_region or (zoom_region(
                (pair["base_snapshot"], pair["working_snapshot"]), zoom, moment.t, fps, canvas,
            ) if zoom else None)
            compare_pub, compare_work = pub_path, work_path
            if crop:
                compare_pub = _crop(pub_path, crop, run_dir / f"compare-{index}-published.png")
                compare_work = _crop(work_path, crop, run_dir / f"compare-{index}-working.png")
            share, count, box, size = difference(compare_pub, compare_work)
            offset = (crop[0], crop[1]) if crop else (0, 0)
            line = pixel_line(share, count, box, size, canvas, offset=offset)
            shows = describe_sides(moment, frame, fps, base_config, working_config)
            if box is None and not crop:
                # the same picture: say so only if the change could have been seen
                keys = [moment.clip] if moment.clip else _changed_on_screen(
                    {"published": base_config, "working": working_config}, frame, fps)
                for key in keys:
                    cover = hidden_under(pair["working_snapshot"], key, moment.t, fps)
                    if cover:
                        name = clip_address(_visual_clips(working_config).get(_short(key)) or {"id": key})
                        line = (f"pixels: identical to published, but {name} changed and is hidden on this "
                                f"frame (under {cover})")
                        break
            zoom_row = None
            if zoom and crop is None:
                line = f"zoom {zoom}: not on screen at this moment · {line}"
                zoom_row = {"label": f"zoom {zoom}: not on screen at this moment (try --region x,y,w,h)"}
            elif crop:
                what = zoom or "region"
                zoom_row = {"published": str(compare_pub), "working": str(compare_work), "region": crop,
                            "box": box, "label": f"zoom {what}: {crop[0]},{crop[1]}–{crop[2]},{crop[3]} canvas px "
                                                 f"at full resolution · {line}"}
            row = {"t": moment.t, "frame": frame, "label": moment.label,
                   "published": str(pub_path), "working": str(work_path),
                   "published_shows": shows[0], "working_shows": shows[1],
                   "pixels": line, "share": share, "different_pixels": count,
                   "bounds": box, "canvas_bounds": _canvas_bounds(box, size, canvas, offset=offset),
                   "box": box if not crop else None, "size": size if not crop else None,
                   "size_of_compare": size, "region": crop, "zoom": zoom_row,
                   "verdict": f"published: {shows[0]} · working copy: {shows[1]} · {line}"}
            if labels:
                row["labelled"] = _labelled_tiles(
                    {"published": pair["base_snapshot"], "working": pair["working_snapshot"]},
                    frame, fps, {"published": pub_path, "working": work_path}, run_dir, index,
                )
                for side, labelled_path in row["labelled"].items():
                    row[f"{side}_display"] = labelled_path
            rows.append(row)
        snapshot_labels = {
            "published": snapshot_label(pair.get("base_snapshot")),
            "working": snapshot_label(pair.get("working_snapshot")),
        }
        page = _compose_page(
            rows,
            run_dir / "verify.png",
            title=f"verify {timeline} · {len(rows)} moment(s)",
            footer=(
                f"published snapshot: {snapshot_labels['published']['label']}",
                f"working snapshot: {snapshot_labels['working']['label']}",
                *footer,
            ),
        )
        notes: list[str] = []
        if skipped:  # one page holds MAX_MOMENTS; say what was left out instead of dropping it silently
            notes.append(f"verified the first {len(selected)} of {len(requested)} moments (a page holds {MAX_MOMENTS}); "
                         f"not shown: {', '.join(_moment_label(m) for m in skipped)}: check again with those --at")
        result = {"page": str(page), "rows": rows,
                  "timing": {"total_s": round(time.monotonic() - started, 1), "startup_s": round(startup_s, 1),
                             "end_to_end_s": round(startup_s + time.monotonic() - started, 1)},
                  "notes": notes, "render_runs": {"published": pair["base_run"], "working": pair["working_run"]},
                  "base_media": pair["base_media"], "working_media": pair["working_media"],
                  "snapshot_labels": snapshot_labels}
        result["lines"] = verify_lines(result)
        return result
    except FastLaneError:
        shutil.rmtree(run_dir, ignore_errors=True)
        raise
    except Exception as exc:  # SDK and image errors should retain an actionable verify boundary.
        shutil.rmtree(run_dir, ignore_errors=True)
        raise FastLaneError(str(exc) or type(exc).__name__) from exc

    finally:
        # Only this invocation's exclusive materialization is disposable;
        # comparison PNGs/page remain as the returned inspection artifacts.
        if pair is not None:
            for key in ("base_video", "working_video"):
                video = pair.get(key)
                if isinstance(video, Path) and video.parent.parent == run_dir and video.parent.is_dir():
                    shutil.rmtree(video.parent)
