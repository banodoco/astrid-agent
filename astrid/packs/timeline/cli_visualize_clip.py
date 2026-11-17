"""``timelines visualize`` views for understanding one clip, drawn on the client from captured frames.

- ``--region X,Y,W,H`` (or a named region) with ``--cut N`` / ``--range A..B`` / ``--shot S``: the region at
  moments across the span at source resolution, and how far its content drifts.
- ``--mask <media>``: the mask's edge over those pictures, and how much of the motion falls inside it;
  with ``--shot`` the shot plan's ``inputs.masks`` is used when ``--mask`` is not given.
- ``--compare-media <take>`` (``--against cut|<media>``): the take beside its source at the same moments,
  with where they differ, inside/outside the mask and along its edge (the seam).

The Runtime captures the composed frames (at the canvas size, so a crop is source resolution); everything
else here is arithmetic on those PNGs and on media bytes read through the client.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Mapping, Sequence

STRIP_MOMENTS = 8
COMPARE_MOMENTS = 4
SHORT_DIGEST = re.compile(r"(?:sha256:)?([0-9a-f]{6,63})")


class ClipError(ValueError):
    """A clip view asked for wrongly, or media it needs is unreadable; the message says what to do."""


# ---------------------------------------------------------------- request

def wants_clip_view(parsed: argparse.Namespace) -> bool:
    """True when this run draws a clip page: a mask or take comparison, or a region over a span."""
    if getattr(parsed, "mask", None) or getattr(parsed, "compare_media", None):
        return True
    region = getattr(parsed, "region", None) or getattr(parsed, "zoom", None)
    single = getattr(parsed, "at", None) not in (None, "") or getattr(parsed, "frame", None) is not None
    return bool(region) and not single


def prepare(parsed: argparse.Namespace) -> dict[str, Any]:
    """Validate the clip flags and settle the span; ``--shot`` alone becomes that shot's ``--range``.

    Returns the request (stored on ``parsed._clip``); raises ClipError with a plain message."""
    region, zoom = getattr(parsed, "region", None), getattr(parsed, "zoom", None)
    if region and zoom:
        raise ClipError("choose --region or --zoom, not both")
    against = getattr(parsed, "against", None)
    if against and not getattr(parsed, "compare_media", None):
        raise ClipError("--against goes with --compare-media <take> (what to compare the take with)")
    has_span = any(getattr(parsed, name, None) not in (None, "") for name in ("cut", "range", "at", "frame"))
    shot_row = None
    if getattr(parsed, "shot", None):
        shot_row = _shot_row(parsed, str(parsed.shot))
        if not has_span:
            parsed.range = f"{shot_row['start']:.3f}..{shot_row['end']:.3f}"
            has_span = True
        parsed.shot = None  # the span (and its plan) is what the clip view needs, not a filmstrip focus
    if not has_span:
        raise ClipError("--region/--mask/--compare-media need a time: --at T (one frame), --cut N, "
                        "--range A..B or --shot S")
    plan = (shot_row or {}).get("plan") or {}
    mask, mask_from = getattr(parsed, "mask", None), "--mask"
    if not mask and shot_row is not None:
        masks = list(((plan.get("inputs") or {}).get("masks")) or [])
        if masks:
            mask, mask_from = str(masks[0]), f"the shot plan's inputs.masks ({shot_row.get('name')})"
    take, take_from = getattr(parsed, "compare_media", None), "--compare-media"
    if take in ("take", "latest-take"):
        takes = [t for t in plan.get("takes") or [] if isinstance(t, Mapping) and t.get("media")]
        if not takes:
            raise ClipError("--compare-media take: the shot plan lists no take with a media id "
                            "(give the take's media id instead)")
        take, take_from = str(takes[-1]["media"]), f"the shot plan's latest take ({shot_row.get('name')})"
    for ref in (mask, take, against if against != "cut" else None):
        if ref:
            resolve_media(parsed, str(ref))  # fail now, before any capture, when an id does not resolve
    request = {"region": region, "zoom": zoom, "mask": mask, "mask_from": mask_from, "take": take,
               "take_from": take_from, "against": against or "cut", "shot": shot_row}
    parsed._clip = request
    return request


def capture_inputs(parsed: argparse.Namespace, inputs: dict[str, Any], canvas: tuple[int, int]) -> None:
    """Capture at the canvas size (one page px = one canvas px) and densely enough to measure motion."""
    if not getattr(parsed, "size", None) and not getattr(parsed, "resolution", None):
        inputs["resolution"] = f"{canvas[0]}x{canvas[1]}"
    if inputs.get("range") and not inputs.get("cut") and inputs.get("every") is None \
            and inputs.get("every_frames") is None:
        lo, _, hi = str(inputs["range"]).partition("..")
        try:
            span = max(0.1, float(hi) - float(lo))
        except ValueError:
            span = 4.0
        inputs["every"] = round(max(1 / 12, span / 40), 3)  # ~40 frames over the span


def _shot_row(parsed: argparse.Namespace, wanted: str) -> dict[str, Any]:
    """The placement row (start, end, name, plan) of ``--shot`` by id, exact name, name prefix or ordinal."""
    from astrid.sdk.shot_plan import plan_rows

    bundle = getattr(parsed, "_bundle", None)
    if not isinstance(bundle, Mapping):
        opened = parsed.client.timelines.open_bundle(
            parsed.project, getattr(parsed, "timeline_slug", None) or getattr(parsed, "timeline_ref", None),
            **({"revision_id": parsed.revision_id} if getattr(parsed, "revision_id", None) else {}))
        bundle = opened.data.get("bundle") if getattr(opened, "ok", False) else None
        parsed._bundle = bundle
    rows = plan_rows(bundle) if isinstance(bundle, Mapping) else []
    if not rows:
        raise ClipError(f"--shot {wanted}: could not read the timeline's shots")
    if wanted.isdigit() and 1 <= int(wanted) <= len(rows):
        return rows[int(wanted) - 1]
    for test in (lambda r: r["shot_id"] == wanted, lambda r: r["name"] == wanted,
                 lambda r: str(r["name"]).lower().startswith(wanted.lower())):
        found = [r for r in rows if test(r)]
        if found:
            return found[0]
    raise ClipError(f"--shot {wanted}: no shot with that id, name or number (timelines show lists them)")


# ---------------------------------------------------------------- media through the client

def resolve_media(parsed: argparse.Namespace, ref: str) -> dict[str, Any]:
    """The project media row for an id (``sha256:…``, a short ``sha256:0a1b2c3d`` as pages print, or
    ``ref:<name>``)."""
    cache = parsed.__dict__.setdefault("_media_rows", {})
    if str(ref) in cache:
        return cache[str(ref)]
    cache[str(ref)] = row = _resolve_media(parsed, str(ref))
    return row


def _resolve_media(parsed: argparse.Namespace, ref: str) -> dict[str, Any]:
    client, project = parsed.client, parsed.project
    ref = str(ref).strip()
    if ref.startswith(("ref:", "run:")):
        from astrid.sdk.media_handles import MediaHandleError, resolve_media_handle

        try:
            descriptor, _lineage = resolve_media_handle(client, project, ref)
        except MediaHandleError as exc:
            raise ClipError(str(exc)) from None
        ref = descriptor["digest"]
    match = SHORT_DIGEST.fullmatch(ref)
    if match and len(match.group(1)) < 64:
        found = [r for r in _all_media(client, project) if isinstance(r, Mapping)
                 and str(r.get("object_id") or r.get("digest") or "")[7:].startswith(match.group(1))]
        if len(found) != 1:
            raise ClipError(f"{ref}: {'no' if not found else 'more than one'} media item in {project} starts with "
                            "that id (astrid media list shows them)")
        ref = str(found[0].get("object_id") or found[0].get("digest"))
    elif match:
        ref = "sha256:" + match.group(1)
    shown = client.media.show(project, ref)
    if not getattr(shown, "ok", False) or not isinstance(shown.data, Mapping):
        error = getattr(shown, "error", None)
        raise ClipError(f"{ref}: {getattr(error, 'message', None) or 'not a media item in this project'}")
    return dict(shown.data)


def _all_media(client: Any, project: str, *, pages: int = 40) -> list[Any]:
    """Every media row of the project (the list is paged: ``[rows, next cursor]``)."""
    rows: list[Any] = []
    cursor = None
    for _ in range(pages):
        listed = client.media.list(project, **({"cursor": cursor} if cursor else {}))
        data = listed.data if getattr(listed, "ok", False) else None
        if isinstance(data, list) and len(data) == 2 and isinstance(data[0], list):
            page, cursor = data
        else:
            page, cursor = (data if isinstance(data, list) else []), None
        rows.extend(page)
        if not cursor:
            break
    return rows


def media_file(parsed: argparse.Namespace, row: Mapping[str, Any], cache: Path) -> Path:
    """The media bytes in a local file (read through the client once, then reused)."""
    object_id = str(row.get("object_id") or row.get("digest"))
    suffix = Path(str(row.get("filename") or "")).suffix or _suffix(str(row.get("media_type") or ""))
    path = cache / f"media-{object_id.split(':')[-1][:16]}{suffix}"
    if not path.is_file():
        cache.mkdir(parents=True, exist_ok=True)
        data = parsed.client.media.read_bytes(object_id)
        tmp = path.with_suffix(path.suffix + ".part")
        tmp.write_bytes(data)
        tmp.replace(path)
    return path


def _suffix(media_type: str) -> str:
    return {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "video/mp4": ".mp4",
            "video/quicktime": ".mov", "video/webm": ".webm"}.get(media_type, ".bin")


def is_video(row: Mapping[str, Any]) -> bool:
    return str(row.get("media_type") or "").startswith("video/")


def media_frames(path: Path, row: Mapping[str, Any], seconds: Sequence[float], size: tuple[int, int]) -> list[Any]:
    """One picture per second given (a still repeats), fitted to ``size`` (the canvas) the way a ``cover`` clip
    is: scaled to fill, centred, the overflow cropped (no stretching when the aspect differs)."""
    from PIL import Image, ImageOps

    if not is_video(row):
        with Image.open(path) as image:
            still = ImageOps.fit(image.convert("RGB"), size, Image.LANCZOS)
        return [still.copy() for _ in seconds]
    binary = shutil.which("ffmpeg")
    if binary is None:
        raise ClipError("reading a video take needs ffmpeg on PATH")
    out = []
    for second in seconds:
        argv = [binary, "-v", "error", "-nostdin", "-ss", f"{max(0.0, second):.3f}", "-i", str(path),
                "-frames:v", "1", "-vf",
                f"scale={size[0]}:{size[1]}:force_original_aspect_ratio=increase,crop={size[0]}:{size[1]}", "-f", "image2pipe", "-vcodec", "png", "-"]
        proc = subprocess.run(argv, capture_output=True, timeout=120, check=False)
        if proc.returncode != 0 or not proc.stdout:
            raise ClipError(f"could not read a frame at {second:.2f} s of {row.get('filename')}: "
                            f"{proc.stderr.decode(errors='replace').strip()[:200]}")
        import io

        with Image.open(io.BytesIO(proc.stdout)) as image:
            out.append(image.convert("RGB"))
    return out


# ---------------------------------------------------------------- captured frames

def captured(outputs: Mapping[str, Any]) -> tuple[Path, list[tuple[float, Path]], dict[str, Any]]:
    """(pack dir, [(time s, frame png)] in time order, render snapshot) of the view that ran."""
    pages = [str(p) for p in outputs.get("pages") or [] if p] or [str(outputs.get("primary_page") or "")]
    root = Path(pages[0]).parent
    index = json.loads((root / "frame-index.json").read_text(encoding="utf-8"))
    frames = sorted((float(c.get("time_seconds") or 0.0), root / str(c["image"]))
                    for c in index.get("cards") or [] if isinstance(c, Mapping) and c.get("image"))
    snapshot_path = root / "render-snapshot.json"
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8")) if snapshot_path.is_file() else {}
    return root, frames, snapshot


def canvas_of(snapshot: Mapping[str, Any]) -> tuple[int, int]:
    config = snapshot.get("config") if isinstance(snapshot.get("config"), Mapping) else {}
    canvas = (((config.get("theme_overrides") or {}).get("visual") or {}).get("canvas") or {})
    return int(canvas.get("width") or 1920), int(canvas.get("height") or 1080)


def span_of(outputs: Mapping[str, Any], frames: Sequence[tuple[float, Path]]) -> tuple[float, float]:
    window = outputs.get("window") if isinstance(outputs.get("window"), Mapping) else {}
    if window.get("start") is not None and window.get("end") is not None:
        return float(window["start"]), float(window["end"])
    if frames:
        return frames[0][0], frames[-1][0] + 1e-3
    return 0.0, 0.0


def pick(frames: Sequence[tuple[float, Path]], count: int) -> list[tuple[float, Path]]:
    """``count`` frames spread evenly (first and last included)."""
    if len(frames) <= count:
        return list(frames)
    step = (len(frames) - 1) / float(count - 1)
    return [frames[round(i * step)] for i in range(count)]


def picture_clip(snapshot: Mapping[str, Any], t: float) -> dict[str, Any] | None:
    """The topmost visual media clip playing at ``t`` with its media id (``from``/``speed`` map time)."""
    assets = ((snapshot.get("registry") or {}).get("assets") or {}) if isinstance(snapshot, Mapping) else {}
    best = None
    for clip in snapshot.get("clips") or []:
        if not isinstance(clip, Mapping) or clip.get("kind") != "visual" or clip.get("clipType") != "media":
            continue
        at, duration = float(clip.get("at") or 0.0), float(clip.get("duration") or 0.0)
        if at - 1e-6 <= t < at + duration:
            best = dict(clip)
            best["media_id"] = (assets.get(str(clip.get("asset"))) or {}).get("media_id")
    return best


# ---------------------------------------------------------------- the pages

def draw(outputs: Mapping[str, Any], request: Mapping[str, Any], *, parsed: argparse.Namespace,
         inputs: Mapping[str, Any]) -> dict[str, Any]:
    """Draw the clip page(s) from the captured frames; returns {page, pages, findings, box, suffix}."""
    from PIL import Image

    from astrid.packs.rendering.actions.timeline_visualize import clip_views as cv
    from astrid.packs.rendering.actions.timeline_visualize.fast_lane import FastLaneError, parse_region

    root, frames, snapshot = captured(outputs)
    canvas = canvas_of(snapshot)
    start, end = span_of(outputs, frames)
    inside = [(t, p) for t, p in frames if start - 1e-6 <= t < end - 1e-6] or list(frames)
    if not inside:
        raise ClipError("the view captured no frames to look at")
    title = str(inputs.get("title") or inputs.get("timeline_slug") or "timeline")
    cut = inputs.get("cut")
    where = f"cut {cut} · {start:.2f}–{end:.2f} s" if cut else f"{start:.2f}–{end:.2f} s"
    shot = request.get("shot") or {}
    if shot.get("name"):
        where += f" · {shot['name']}"
    findings: list[str] = []
    pages: list[str] = []
    worst_t = None
    mask_img, mask_row = None, None
    if request.get("mask"):
        mask_row = resolve_media(parsed, str(request["mask"]))
        path = media_file(parsed, mask_row, root.parent / "media")
        mask_img = cv.load_mask(media_frames(path, mask_row, [0.0], canvas)[0] if is_video(mask_row)
                                else Image.open(path), canvas)
        if mask_img.getbbox() is None:
            raise ClipError(f"the mask {_short(mask_row)} is all black: nothing is marked as edited (white = edited)")
    box = None
    region_name = request.get("region")
    if region_name:
        name = str(region_name).strip().lower()
        if name == "mask":
            if mask_img is None:
                raise ClipError("--region mask needs --mask <media> (or a shot plan with inputs.masks)")
            box = cv.mask_box(mask_img)
        else:
            box = cv.named_region(name, canvas)
            if box is None:
                try:
                    box = parse_region(str(region_name))
                except FastLaneError as exc:
                    raise ClipError(f"{exc}; or a name: mask, {', '.join(sorted(cv.NAMED_REGIONS))}") from None
    elif request.get("zoom"):
        from fractions import Fraction

        from astrid.packs.rendering.actions.timeline_visualize.fast_lane import zoom_region

        fps = float(Fraction(*snapshot.get("fps_rational") or [30, 1]))
        mid = inside[len(inside) // 2][0]
        box = zoom_region((snapshot,), str(request["zoom"]), mid, fps, canvas)
        if box is None:
            raise ClipError(f"{request['zoom']} is not on screen at {mid:.2f} s (try --region X,Y,W,H)")
    elif mask_img is not None:
        box = cv.mask_box(mask_img)
    if box is not None:
        box = (max(0, box[0]), max(0, box[1]), min(canvas[0], box[2]), min(canvas[1], box[3]))
        if box[2] <= box[0] or box[3] <= box[1]:
            raise ClipError(f"the region lies outside the {canvas[0]}x{canvas[1]} canvas")
    images = cv.as_images(p for _t, p in inside)
    times = [t for t, _p in inside]
    freq, steps = cv.change_map(images)
    mask_note = (f"mask {_short(mask_row)} from {request['mask_from']} (cyan line = its edge; white in the mask = "
                 "edited)") if mask_row else None
    page_stem = _stem(cut, start, end, box, mask_row, request)
    primary = None
    # 1) the take against its source
    if request.get("take"):
        primary = _compare(parsed, request, root=root, inside=inside, images=images, snapshot=snapshot,
                           canvas=canvas, box=box, mask=mask_img, title=title, where=where, mask_note=mask_note,
                           findings=findings, stem=page_stem)
        pages.append(primary)
    # 2) the region (or the mask's box) across the span
    if box is not None and (len(inside) > 1 or not request.get("take")):
        chosen = pick(list(zip(times, images)), STRIP_MOMENTS)
        crops = [img.crop(_scaled(box, img.size, canvas)) for _t, img in chosen]
        weight = cv.edge_band(mask_img, 16).crop(box) if mask_img is not None else None
        drift = cv.region_drift(crops, weight=weight) if len(crops) > 1 else []
        verdicts = []
        if drift:
            level, sentence, _most = cv.drift_verdict(drift, [t for t, _i in chosen])
            worst_t = chosen[max(range(len(drift)), key=lambda i: abs(drift[i][0]) + abs(drift[i][1]))][0]
            verdicts.append((level, sentence))
            findings.append(f"DRIFT  {level} {sentence}")
        if mask_img is not None:
            fit = cv.mask_fit(freq, mask_img, canvas)
            verdicts.extend(_fit_verdicts(fit, canvas))
            findings.append(f"MASK   {fit.inside:.0%} of the motion falls inside the mask (it covers "
                            f"{fit.mask_area:.0%} of the frame); edge: {fit.edge_out:.0%} of frames change just "
                            f"outside it, {fit.edge_in:.0%} just inside")
        fx0, fy0, fx1, fy1 = _scaled(box, (freq.shape[1], freq.shape[0]), canvas)
        region_freq = freq[fy0:max(fy1, fy0 + 1), fx0:max(fx1, fx0 + 1)]
        heat = cv.heat_overlay(images[len(images) // 2].crop(_scaled(box, images[0].size, canvas)), region_freq) \
            if steps else None
        if heat is not None and mask_img is not None:
            heat = cv.draw_outline(heat, mask_img.crop(box), width=2)
        label = (f"region {region_name}" if region_name else f"zoom {request['zoom']}" if request.get("zoom")
                 else "the mask's area")
        what = (f"{label} at {len(chosen)} moments across the span, side by side; the cyan line is the mask, "
                "fixed in place, so anything that slides off it is drift" if mask_img is not None else
                f"{label} at {len(chosen)} moments across the span, side by side, to see whether it moves")
        follows = "the mask's edge" if mask_img is not None else "the picture in the box"
        sub = ([mask_note] if mask_note else []) + [
            f"{len(inside)} frames captured over {end - start:.2f} s; 'moved' = how far {follows} sits from the "
            "first moment (edge matching, about 1-2 px)"]
        page = cv.region_strip_page(title=f"{title} · {where} · {label}", what=what, sub=sub,
                                    frames=[i for _t, i in chosen], times=[t for t, _i in chosen], box=box,
                                    canvas=canvas, mask=mask_img, verdicts=verdicts, drift=drift, heat=heat)
        path = root / f"view-clip-region-{page_stem}.png"
        page.save(path)
        pages.append(str(path))
        primary = primary or str(path)
    # 3) where the picture moves (always, as a second page; first when nothing else was asked)
    if steps:
        base = images[len(images) // 2]
        hot = cv.hotspot(freq, canvas)
        verdicts = [("INFO", (f"the strongest change (dashed box) is in {cv.where_words(hot, canvas)} (canvas px x {hot[0]}–{hot[2]}, "
                              f"y {hot[1]}–{hot[3]})") if hot else "almost nothing changes over the span")]
        if mask_img is not None:
            verdicts.extend(_fit_verdicts(cv.mask_fit(freq, mask_img, canvas), canvas))
        page = cv.heat_page(
            title=f"{title} · {where} · where it moves",
            what=("the middle frame, greyed, coloured where pixels change over the span"
                  + (" (cyan = mask edge)" if mask_img is not None else "")),
            sub=[f"{len(inside)} frames over {end - start:.2f} s; grey = still, violet = changes now and then, "
                 "yellow = changes every frame"]
                + ([mask_note] if mask_note else []),
            base=base, freq=freq, mask=mask_img, verdicts=verdicts, box=box, canvas=canvas)
        path = root / f"view-clip-heat-{page_stem}.png"
        page.save(path)
        pages.append(str(path))
        primary = primary or str(path)
        findings.append(f"HEAT   {verdicts[0][1]}")
    if primary is None:
        raise ClipError("nothing to draw: the span has a single frame and no region or take")
    kind = Path(primary).stem.split("-")[2]
    label = {"compare": "take vs source", "region": "region over time", "heat": "motion map"}.get(kind, "clip view")
    return {"page": primary, "pages": pages, "findings": findings, "box": list(box) if box else None,
            "suffix": page_stem, "label": label, "worst_t": worst_t if worst_t is not None else times[len(times) // 2]}


def _fit_verdicts(fit: Any, canvas: tuple[int, int]) -> list[tuple[str, str]]:
    from astrid.packs.rendering.actions.timeline_visualize import clip_views as cv

    outside = 1 - fit.inside
    lines = [("INFO", f"{fit.inside:.0%} of the motion falls inside the mask, {outside:.0%} outside"
                      + (f" (mostly {cv.where_words(fit.outside_box, canvas)})" if outside >= 0.05 and fit.outside_box
                         else "") + f"; the mask covers {fit.mask_area:.0%} of the frame")]
    if fit.edge_out >= 0.2:
        lines.append(("MOVES", f"just outside the mask edge pixels change in {fit.edge_out:.0%} of frames: "
                               "what is masked crosses its edge"))
    elif fit.edge_out >= 0.08:
        lines.append(("TIGHT", f"just outside the mask edge pixels change in {fit.edge_out:.0%} of frames: "
                               "some movement crosses the edge"))
    return lines


def _compare(parsed, request, *, root, inside, images, snapshot, canvas, box, mask, title, where, mask_note,
             findings, stem) -> str:
    from astrid.packs.rendering.actions.timeline_visualize import clip_views as cv

    take_row = resolve_media(parsed, str(request["take"]))
    take_path = media_file(parsed, take_row, root.parent / "media")
    start = inside[0][0]
    chosen_idx = [inside.index(item) for item in pick(inside, COMPARE_MOMENTS)]
    moments = [inside[i][0] for i in chosen_idx]
    take_imgs = media_frames(take_path, take_row, [t - start for t in moments], canvas)
    against = str(request.get("against") or "cut")
    if against == "cut":
        source_imgs = [images[i] for i in chosen_idx]
        source_label = "the cut (composed frame)"
        mapping = f"take second 0 = {start:.2f} s on the timeline (the span's start)"
    else:
        source_row = resolve_media(parsed, against)
        source_path = media_file(parsed, source_row, root.parent / "media")
        clip = picture_clip(snapshot, moments[len(moments) // 2])
        source_id = str(source_row.get("object_id") or "")
        if clip and clip.get("media_id") == source_id and is_video(source_row):
            speed, offset = float(clip.get("speed") or 1.0), float(clip.get("from") or 0.0)
            seconds = [offset + (t - float(clip["at"])) * speed for t in moments]
            mapping = (f"source = the cut's picture media at its own time ({offset:.2f} s + timeline offset x "
                       f"{speed:.2f}); take second 0 = {start:.2f} s")
        else:
            seconds = [t - start for t in moments]
            mapping = f"take and source both from their second 0 = {start:.2f} s on the timeline"
        source_imgs = media_frames(source_path, source_row, seconds, canvas)
        source_label = f"source {_short(source_row)} {source_row.get('filename') or ''}".strip()
    rows = []
    for t, source, take in zip(moments, source_imgs, take_imgs):
        crop_mask = mask
        if box is not None:
            source, take = source.crop(box), take.crop(box)
            crop_mask = mask.crop(box) if mask is not None else None
        rows.append((t, source, take, cv.difference(source, take, crop_mask, canvas=canvas)))
    verdicts = cv.compare_verdicts([r[3] for r in rows], masked=mask is not None)
    for level, sentence in verdicts:
        findings.append(f"COMPARE {level} {sentence}")
    sub = [f"{source_label} | take {_short(take_row)} {take_row.get('filename') or ''} (from {request['take_from']})",
           mapping + (f" · cropped to canvas px {box[0]},{box[1]}–{box[2]},{box[3]} at source resolution"
                      if box else "")]
    if mask_note:
        sub.append(mask_note)
    page = cv.compare_page(title=f"{title} · {where} · take vs source", sub=sub, rows=rows, verdicts=verdicts,
                           what="the source, the take and where they differ, at the same moments, one row each",
                           labels=(source_label, f"take {_short(take_row)}"))
    path = root / f"view-clip-compare-{stem}.png"
    page.save(path)
    return str(path)


def _scaled(box, size, canvas):
    kx, ky = size[0] / canvas[0], size[1] / canvas[1]
    return (round(box[0] * kx), round(box[1] * ky), round(box[2] * kx), round(box[3] * ky))


def _short(row: Mapping[str, Any] | None) -> str:
    if not row:
        return ""
    return "sha256:" + str(row.get("object_id") or row.get("digest") or "").split(":")[-1][:12]


def _stem(cut, start, end, box, mask_row, request) -> str:
    parts = [f"cut-{int(cut):02d}" if str(cut or "").isdigit() else f"{start:.2f}-{end:.2f}s"]
    if box is not None:
        parts.append(f"region-{box[0]}-{box[1]}-{box[2] - box[0]}x{box[3] - box[1]}")
    if mask_row:
        parts.append("mask-" + _short(mask_row)[7:15])
    if request.get("take"):
        parts.append("take")
        against = str(request.get("against") or "cut")
        if against != "cut":
            parts.append("vs-" + re.sub(r"[^0-9a-z]+", "", against.split(":")[-1].lower())[:8])
    return "-".join(parts)


# ---------------------------------------------------------------- next commands

def next_commands(base: list[str], request: Mapping[str, Any], inputs: Mapping[str, Any],
                  drawn: Mapping[str, Any]) -> list[tuple[str, list[str]]]:
    """What to look at next from a clip page, copyable as printed."""
    span = ["--cut", str(inputs["cut"])] if inputs.get("cut") else (
        ["--range", str(inputs["range"])] if inputs.get("range") else ["--at", str(inputs.get("at"))])
    box = drawn.get("box")
    region = ["--region", f"{box[0]},{box[1]},{box[2] - box[0]},{box[3] - box[1]}"] if box else []
    mask = ["--mask", str(request["mask"])] if request.get("mask") else []
    rows: list[tuple[str, list[str]]] = []
    if not request.get("mask"):
        rows.append(("judge a mask over this span", base + span + region + ["--mask", "<mask media id>"]))
    if not request.get("take"):
        rows.append(("a take against the cut, same moments", base + span + region + mask
                     + ["--compare-media", "<take media id>"]))
    else:
        rows.append(("the take against the original media", base + span + region + mask
                     + ["--compare-media", str(request["take"]), "--against", "<source media id>"]))
    if box:
        at = f"{float(drawn.get('worst_t') or 0):.2f}"
        rows.append((f"the region at {at} s (where it moved most), one frame", base + ["--at", at] + region))
    rows.append(("the whole frame moving (motion sheet)", base + span))
    return rows
