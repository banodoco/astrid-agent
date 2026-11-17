"""Crop a managed still image or video into a NEW managed media item.

``astrid media crop <media> --project P`` reads one project media item,
crops it (an explicit ``--box`` or detected letterbox bars), imports the
result through the same ``import_file`` path as ``astrid media import``, and
records provenance as a ``derived_from`` relation from the new media to the
source media. The source item is never modified.

Bar detection (``--remove-bars``) is deliberately simple and reported. An edge
row (or column) is a bar only when both hold:

- at most ``_BAR_MAX_BRIGHT_FRACTION`` of its pixels have a max channel value
  above the threshold (it is dark), and
- its values are near-uniform: standard deviation at most ``_BAR_MAX_STD``
  (a dark but textured edge is therefore kept).

Each edge may remove at most ``_BAR_MAX_EDGE_FRACTION`` of its dimension. A
larger band is refused with the edges that were measured, so the caller can
pass an explicit ``--box``. Video samples several frames across the clip and
keeps the smallest bar per edge, so one dark frame cannot widen the crop.
Nothing is written to the project during ``dry_run``.
"""

from __future__ import annotations

import hashlib
import io
import json
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from astrid.core.media import MediaProbeError, ffprobe_metadata_strict
from astrid.sdk.contracts import DomainResult, ErrorObject
from astrid.sdk.exceptions import ServiceError

DEFAULT_BAR_THRESHOLD = 16
"""Max channel value (0-255) still counted as near-black."""

_BAR_MAX_BRIGHT_FRACTION = 0.005
"""A bar row/column may contain at most 0.5% pixels above the threshold."""

_BAR_MIN_STD = 1.5
_BAR_STD_PER_THRESHOLD = 0.2
"""A bar row/column is near-uniform: its value standard deviation is at most
``max(_BAR_MIN_STD, _BAR_STD_PER_THRESHOLD * threshold)``. The limit scales with
the threshold, so faint film grain in a black band still counts as a bar."""

_BAR_EXTEND_ROWS = 16
"""After the uniform core, keep extending while rows stay dark, up to this many
rows. This covers JPEG block ringing that is dark but not uniform."""

_BAR_MAX_EDGE_FRACTION = 0.25
"""One edge may remove at most this fraction of its dimension."""

_VIDEO_SAMPLE_FRACTIONS = (0.1, 0.3, 0.5, 0.7, 0.9)
"""Fractions of the clip duration sampled for bar detection."""

_EDGES = ("top", "bottom", "left", "right")
_IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"})
_VIDEO_SUFFIXES = frozenset({".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi"})
_VIDEO_PREFIX = "video/"
_IMAGE_PREFIX = "image/"

_PNG_MODES = frozenset({"1", "L", "LA", "P", "RGB", "RGBA", "I", "I;16"})
"""Modes PNG can store; anything else is converted explicitly."""

_AUDIO_COPY_CODECS = frozenset({"aac", "mp3", "ac3", "eac3", "alac"})
"""Audio codecs that can be stream-copied into an MP4 container."""

_DERIVATION_FIELDS = ("operation", "source_object_id", "source_digest", "box", "even")
"""Metadata fields that identify a derivation (bar and audio detail excluded)."""

_MAX_RELATION_PAGES = 100


@dataclass(frozen=True)
class Box:
    x: int
    y: int
    w: int
    h: int

    def as_dict(self) -> dict[str, int]:
        return {"x": self.x, "y": self.y, "w": self.w, "h": self.h}

    def even(self) -> "Box":
        """Round width and height down to even values (stays inside the source)."""
        return Box(self.x, self.y, self.w - self.w % 2, self.h - self.h % 2)

    def check_within(self, width: int, height: int) -> None:
        if self.x < 0 or self.y < 0 or self.w <= 0 or self.h <= 0:
            raise ValueError("box must have x,y >= 0 and positive w,h")
        if self.x + self.w > width or self.y + self.h > height:
            raise ValueError(
                f"box {self.x},{self.y},{self.w},{self.h} exceeds the "
                f"{width}x{height} source"
            )


def parse_box(text: str) -> Box:
    """Parse ``X,Y,W,H`` (source pixels, after EXIF orientation for images)."""
    parts = [part.strip() for part in str(text).split(",")]
    if len(parts) != 4:
        raise ValueError("box must be X,Y,W,H in source pixels")
    try:
        x, y, w, h = (int(part) for part in parts)
    except ValueError as exc:
        raise ValueError("box values must be integers") from exc
    return Box(x, y, w, h)


@dataclass(frozen=True)
class BarDetection:
    box: Box
    threshold: int
    edges: dict[str, int]  # pixels removed per edge: top, bottom, left, right

    def as_dict(self) -> dict[str, Any]:
        return {
            "box": self.box.as_dict(),
            "threshold": self.threshold,
            "edges": dict(self.edges),
        }


def _check_threshold(threshold: int) -> None:
    if not 0 <= threshold <= 255:
        raise ValueError("bar threshold must be 0..255")


def _edge_measure(rgb: Any, threshold: int) -> dict[str, int]:
    """Raw leading bar size per edge, before the per-edge cap is applied.

    A bar starts with a dark, near-uniform core (leading rows that are dark and
    within the spread limit). From the core it extends while rows stay dark,
    up to ``_BAR_EXTEND_ROWS`` rows, so JPEG ringing at the picture edge is kept.
    """
    import numpy as np

    pixels = np.asarray(rgb, dtype=np.uint8)
    bright = pixels.max(axis=2) > threshold
    values = pixels.astype(np.float32)
    limit = max(_BAR_MIN_STD, _BAR_STD_PER_THRESHOLD * threshold)
    row_dark = bright.mean(axis=1) <= _BAR_MAX_BRIGHT_FRACTION
    row_uniform = values.std(axis=(1, 2)) <= limit
    col_dark = bright.mean(axis=0) <= _BAR_MAX_BRIGHT_FRACTION
    col_uniform = values.std(axis=(0, 2)) <= limit
    return {
        "top": _leading_bar(row_dark, row_uniform),
        "bottom": _leading_bar(row_dark[::-1], row_uniform[::-1]),
        "left": _leading_bar(col_dark, col_uniform),
        "right": _leading_bar(col_dark[::-1], col_uniform[::-1]),
    }


def _leading_bar(dark: Any, uniform: Any) -> int:
    """Length of the bar from one edge: a uniform dark core, then dark extension."""
    core = _leading_true(dark & uniform)
    if core == 0:
        return 0
    extent = core
    while extent < len(dark) and extent - core < _BAR_EXTEND_ROWS and dark[extent]:
        extent += 1
    return extent


def _box_from_edges(edges: dict[str, int], width: int, height: int, threshold: int) -> BarDetection:
    found = ", ".join(f"{name}={edges[name]}" for name in _EDGES)
    percent = int(_BAR_MAX_EDGE_FRACTION * 100)
    for name in _EDGES:
        dimension = height if name in ("top", "bottom") else width
        if edges[name] > int(dimension * _BAR_MAX_EDGE_FRACTION):
            raise ValueError(
                f"detected bars ({found} px in a {width}x{height} frame) exceed "
                f"{percent}% of the frame on {name}; refusing to crop automatically. "
                "Inspect with --dry-run and pass an explicit --box X,Y,W,H instead"
            )
    if edges["top"] + edges["bottom"] >= height or edges["left"] + edges["right"] >= width:
        raise ValueError("detected bars cover the whole frame; nothing would remain")
    box = Box(
        edges["left"],
        edges["top"],
        width - edges["left"] - edges["right"],
        height - edges["top"] - edges["bottom"],
    )
    return BarDetection(box=box, threshold=threshold, edges=dict(edges))


def detect_bars(rgb: Any, *, threshold: int = DEFAULT_BAR_THRESHOLD) -> BarDetection:
    """Detect near-black, near-uniform letterbox/pillarbox bars on each edge.

    ``rgb`` is an ``(H, W, 3)`` uint8 array. Raises ``ValueError`` when a band
    exceeds ``_BAR_MAX_EDGE_FRACTION`` or the bars would leave no picture.
    """
    _check_threshold(threshold)
    height, width = rgb.shape[:2]
    return _box_from_edges(_edge_measure(rgb, threshold), width, height, threshold)


def _leading_true(flags: Any) -> int:
    count = 0
    for flag in flags:
        if not flag:
            break
        count += 1
    return count


def _open_still(data: bytes) -> tuple[Any, bytes | None]:
    """Open still-image bytes as an oriented, PNG-writable image.

    EXIF orientation is applied, so boxes address the picture a viewer sees.
    The ICC profile is returned for re-embedding. Modes PNG cannot store
    (CMYK, YCbCr, LAB, ...) are converted explicitly. Animated files are refused.
    """
    from PIL import Image, ImageOps

    try:
        with Image.open(io.BytesIO(data)) as raw:
            frames = getattr(raw, "n_frames", 1)
            # MPO (multi-picture JPEG, e.g. phone depth/stereo) is a still: frame 0 is the photo.
            if frames > 1 and raw.format != "MPO":
                raise ValueError(
                    f"animated or multi-page image ({frames} frames) is not supported; "
                    "export one still frame and crop that instead"
                )
            icc = raw.info.get("icc_profile")
            image = ImageOps.exif_transpose(raw)
            image.load()
    except (OSError, SyntaxError, Image.DecompressionBombError) as exc:
        raise ValueError(f"not a readable still image: {exc}") from exc
    if image.mode not in _PNG_MODES:
        # The profile describes the source space (e.g. CMYK); it no longer applies.
        image = image.convert("RGBA" if "A" in image.getbands() else "RGB")
        icc = None
    return image, icc


def _load_rgb(data: bytes) -> Any:
    import numpy as np

    image, _ = _open_still(data)
    return np.asarray(image.convert("RGB"), dtype=np.uint8).copy()


def crop_image_bytes(data: bytes, box: Box) -> bytes:
    """Crop still-image bytes and return lossless PNG bytes.

    The ICC profile is kept. EXIF is not carried over, so the orientation tag
    is gone and the pixels are already upright.
    """
    image, icc = _open_still(data)
    box.check_within(*image.size)
    cropped = image.crop((box.x, box.y, box.x + box.w, box.y + box.h))
    out = io.BytesIO()
    # Pass the profile explicitly: a None here stops Pillow reusing the source's info["icc_profile"].
    cropped.save(out, format="PNG", icc_profile=icc)
    return out.getvalue()


def image_size(data: bytes) -> tuple[int, int]:
    image, _ = _open_still(data)
    return image.size


def _audio_plan(codec: str | None) -> dict[str, Any]:
    if codec is None:
        return {"action": "none"}
    if codec in _AUDIO_COPY_CODECS:
        return {"action": "copy", "codec": codec}
    return {"action": "reencode", "codec": codec, "output_codec": "aac"}


def ffmpeg_crop_args(
    src: Path, dst: Path, box: Box, *, fps_rational: tuple[int, int] | None, audio_action: str = "copy"
) -> list[str]:
    """Build the ffmpeg command: re-encode video at the same fps, keep audio.

    ``exact=1`` makes the crop start at the requested x/y. Without it ffmpeg
    rounds odd offsets down to the chroma grid and the first row is wrong.
    """
    args = [
        "ffmpeg", "-nostdin", "-v", "error", "-y",
        "-i", str(src),
        "-map", "0:v:0", "-map", "0:a?",
        "-vf", f"crop={box.w}:{box.h}:{box.x}:{box.y}:exact=1",
    ]
    if fps_rational is not None:
        args += ["-r", f"{fps_rational[0]}/{fps_rational[1]}"]
    if audio_action == "reencode":
        audio = ["-c:a", "aac", "-b:a", "192k"]
    else:
        audio = ["-c:a", "copy"]
    args += [
        "-c:v", "libx264", "-preset", "medium", "-crf", "16",
        "-pix_fmt", "yuv420p",
        *audio,
        "-movflags", "+faststart",
        str(dst),
    ]
    return args


def _run_ffmpeg(args: list[str]) -> None:
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("ffmpeg is required to crop video and was not found")
    proc = subprocess.run(args, capture_output=True, text=True, timeout=3600)
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip()[-400:]
        raise RuntimeError(f"ffmpeg failed with exit {proc.returncode}: {detail}")


def _kind_for(media_type: str, filename: str) -> str | None:
    media_type = (media_type or "").lower()
    if media_type.startswith(_IMAGE_PREFIX):
        return "image"
    if media_type.startswith(_VIDEO_PREFIX):
        return "video"
    suffix = Path(filename or "").suffix.lower()
    if suffix in _IMAGE_SUFFIXES:
        return "image"
    if suffix in _VIDEO_SUFFIXES:
        return "video"
    return None


def _failure(code: str, message: str, key: str = "", **details: Any) -> DomainResult[Any]:
    return DomainResult.failure(ErrorObject(code, message, dict(details)), idempotency_key=key)


def crop_media(
    client: Any,
    *,
    project: str,
    ref: str,
    box: str | None = None,
    remove_bars: bool = False,
    bar_threshold: int = DEFAULT_BAR_THRESHOLD,
    even: bool = False,
    dry_run: bool = False,
    idempotency_key: str | None = None,
    allow_no_bars: bool = False,
) -> DomainResult[Any]:
    """Crop one project media item into a new managed media item.

    Exactly one of ``box`` (``"X,Y,W,H"``) or ``remove_bars`` must be given.
    With ``remove_bars`` and no bars found, nothing is imported unless
    ``allow_no_bars`` is set (a dry run always just reports the result).
    """
    if (box is None) == (not remove_bars):
        return _failure("validation_error", "give exactly one of --box or --remove-bars", idempotency_key or "", field="box")
    if project is None:
        return _failure("validation_error", "media crop requires a project", idempotency_key or "", field="project")

    shown = client.media.show(project, ref)
    if not shown.ok or not isinstance(shown.data, dict):
        return shown
    source = shown.data
    source_object_id = source.get("object_id")
    source_digest = source.get("digest")
    filename = str(source.get("filename") or source.get("stored_filename") or ref)
    if not isinstance(source_object_id, str) or not isinstance(source_digest, str):
        return _failure("protocol_error", "media row lacks object_id or digest", idempotency_key or "")

    kind = _kind_for(str(source.get("media_type") or ""), filename)
    if kind is None:
        return _failure("unsupported_operation", "crop supports still images and video only", idempotency_key or "", media_type=source.get("media_type"))

    try:
        data = client.media.read_bytes(source_object_id)
    except (ServiceError, OSError) as exc:
        return _failure("unavailable", f"media bytes are unavailable: {exc}", idempotency_key or "")

    try:
        if kind == "image":
            width, height = image_size(data)
            detection = detect_bars(_load_rgb(data), threshold=bar_threshold) if remove_bars else None
            plan = _plan(box, detection, width, height)
            _refuse_if_no_bars(plan, remove_bars=remove_bars, dry_run=dry_run, allow_no_bars=allow_no_bars)
            crop_dims = plan["box"].even() if even else plan["box"]
            crop_dims.check_within(width, height)
            if crop_dims.w <= 0 or crop_dims.h <= 0:
                raise ValueError("box is empty after even rounding")
            if dry_run:
                return _result(source, plan, crop_dims, kind, dry_run=True)
            payload = crop_image_bytes(data, crop_dims)
            return _write_and_import(
                client, project=project, source=source, source_object_id=source_object_id,
                source_digest=source_digest, filename=filename, plan=plan, crop=crop_dims,
                kind=kind, even=even, payload=payload, suffix=".png", idempotency_key=idempotency_key,
            )
        return _crop_video(
            client, data=data, project=project, source=source, source_object_id=source_object_id,
            source_digest=source_digest, filename=filename, box=box, remove_bars=remove_bars,
            bar_threshold=bar_threshold, even=even, dry_run=dry_run,
            idempotency_key=idempotency_key, allow_no_bars=allow_no_bars,
        )
    except (ValueError, MediaProbeError) as exc:
        return _failure("validation_error", str(exc), idempotency_key or "")
    except RuntimeError as exc:
        return _failure("unavailable", str(exc), idempotency_key or "")


def _plan(box: str | None, detection: BarDetection | None, width: int, height: int) -> dict[str, Any]:
    if detection is not None:
        return {"box": detection.box, "bars": detection.as_dict(), "audio": None}
    parsed = parse_box(str(box))
    parsed.check_within(width, height)
    return {"box": parsed, "bars": None, "audio": None}


def _bars_found(plan: dict[str, Any]) -> bool:
    bars = plan.get("bars")
    return bars is not None and any(bars["edges"].values())


def _refuse_if_no_bars(plan: dict[str, Any], *, remove_bars: bool, dry_run: bool, allow_no_bars: bool) -> None:
    """Import nothing when --remove-bars found no bars, unless explicitly allowed."""
    if not remove_bars or dry_run or allow_no_bars or _bars_found(plan):
        return
    raise ValueError(
        "--remove-bars found no bars, so there is nothing to remove; nothing was imported. "
        "Pass --allow-no-bars to import an uncropped copy, or --box X,Y,W,H to crop explicitly"
    )


def _result(
    source: dict[str, Any], plan: dict[str, Any], crop: Box, kind: str, *, dry_run: bool,
    media: Any = None, provenance: Any = None, provenance_recorded: str | None = None,
) -> DomainResult[Any]:
    data: dict[str, Any] = {
        "dry_run": dry_run,
        "kind": kind,
        "source": {"object_id": source.get("object_id"), "digest": source.get("digest")},
        "box": crop.as_dict(),
        "output": {"width": crop.w, "height": crop.h},
        "detected": plan["bars"],
    }
    if plan["bars"] is not None:
        data["bars_found"] = _bars_found(plan)
    if plan.get("audio") is not None:
        data["audio"] = plan["audio"]
    if media is not None:
        data["media"] = media
    if provenance is not None:
        data["provenance"] = provenance
    if provenance_recorded is not None:
        data["provenance_recorded"] = provenance_recorded
    return DomainResult.success(data, idempotency_key="")


def _provenance_metadata(
    source_digest: str, source_object_id: str, crop: Box, even: bool, plan: dict[str, Any]
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "operation": "crop",
        "source_digest": source_digest,
        "source_object_id": source_object_id,
        "box": crop.as_dict(),
        "even": even,
        "bars": plan["bars"],
    }
    if plan.get("audio") is not None:
        metadata["audio"] = plan["audio"]
    return metadata


def _default_key(project: str, metadata: dict[str, Any]) -> str:
    """Idempotency key over the project and the FULL relate metadata.

    Two runs that differ in any recorded detail (bars, audio, box, even) get
    different keys, so a replay can never return a result for other metadata.
    """
    raw = json.dumps({"project": project, "metadata": metadata}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def _write_and_import(
    client: Any, *, project: str, source: dict[str, Any], source_object_id: str,
    source_digest: str, filename: str, plan: dict[str, Any], crop: Box, kind: str,
    even: bool, payload: bytes, suffix: str, idempotency_key: str | None,
) -> DomainResult[Any]:
    metadata = _provenance_metadata(source_digest, source_object_id, crop, even, plan)
    key = idempotency_key or _default_key(project, metadata)
    with tempfile.TemporaryDirectory(prefix="astrid-crop-") as tmp:
        out_path = Path(tmp) / f"{Path(filename).stem}-crop{suffix}"
        out_path.write_bytes(payload)
        imported = client.media.import_file(project=project, path=out_path, realm="managed_local", idempotency_key=key)
        if not imported.ok:
            return imported
        return _record_provenance(
            client, project=project, source=source, source_object_id=source_object_id,
            metadata=metadata, plan=plan, crop=crop, kind=kind, imported=imported, key=key,
        )


def _record_provenance(
    client: Any, *, project: str, source: dict[str, Any], source_object_id: str,
    metadata: dict[str, Any], plan: dict[str, Any], crop: Box, kind: str,
    imported: DomainResult[Any], key: str,
) -> DomainResult[Any]:
    media = dict(imported.data) if isinstance(imported.data, dict) else {"result": imported.data}
    new_object_id = media.get("object_id")
    if not isinstance(new_object_id, str):
        return _failure("protocol_error", "imported crop has no object_id", key, media=media)
    related = client.media.relate(
        project,
        from_object_id=new_object_id,
        to_object_id=source_object_id,
        kind="derived_from",
        metadata=metadata,
        idempotency_key=key,
    )
    if related.ok:
        return _result(source, plan, crop, kind, dry_run=False, media=media, provenance=metadata, provenance_recorded="new")
    if _identical_relation_exists(client, project, new_object_id, source_object_id, metadata):
        return _result(source, plan, crop, kind, dry_run=False, media=media, provenance=metadata, provenance_recorded="existing")
    error = related.error
    code = error.code if error is not None else "protocol_error"
    reason = error.message if error is not None else "no error detail"
    return _failure(
        code,
        (
            f"crop was imported as {new_object_id} but its provenance could not be recorded, "
            "so the item was imported without provenance. Re-run the same command with "
            f"--idempotency-key {key} to record it. Reason: {reason}"
        ),
        key,
        imported_object_id=new_object_id,
        idempotency_key=key,
    )


def _same_derivation(existing: dict[str, Any], metadata: dict[str, Any]) -> bool:
    """Same crop of the same source: bar and audio detail are not compared.

    ``even`` only matters when the box is not already even: rounding an
    even box down changes nothing, so an even-only difference is identical.
    """
    if not all(existing.get(name) == metadata.get(name) for name in _DERIVATION_FIELDS if name != "even"):
        return False
    if existing.get("even") == metadata.get("even"):
        return True
    box = metadata.get("box") or {}
    return box.get("w", 1) % 2 == 0 and box.get("h", 1) % 2 == 0


def _identical_relation_exists(
    client: Any, project: str, from_object_id: str, to_object_id: str, metadata: dict[str, Any]
) -> bool:
    """True when the project already holds this same derivation as derived_from."""
    cursor: str | None = None
    for _ in range(_MAX_RELATION_PAGES):
        page = client.media.list_relations(project, cursor=cursor, limit=200)
        if not page.ok or not isinstance(page.data, dict):
            return False
        for row in page.data.get("items") or []:
            if (
                row.get("from_object_id") == from_object_id
                and row.get("to_object_id") == to_object_id
                and row.get("kind") == "derived_from"
            ):
                if _same_derivation(row.get("metadata") or {}, metadata):
                    return True
        cursor = page.data.get("next_cursor")
        if not cursor:
            return False
    return False


def _detect_video_bars(
    src: Path, duration: float, width: int, height: int, threshold: int, workdir: Path,
) -> BarDetection:
    """Sample frames across the clip and keep the smallest bar on each edge."""
    _check_threshold(threshold)
    stamps = [duration * fraction for fraction in _VIDEO_SAMPLE_FRACTIONS] if duration > 0 else [0.0]
    smallest: dict[str, int] | None = None
    for index, stamp in enumerate(stamps):
        frame = workdir / f"frame-{index}.png"
        _run_ffmpeg([
            "ffmpeg", "-nostdin", "-v", "error", "-y",
            "-ss", f"{stamp:.3f}", "-i", str(src),
            "-frames:v", "1", str(frame),
        ])
        edges = _edge_measure(_load_rgb(frame.read_bytes()), threshold)
        if smallest is None:
            smallest = edges
        else:
            smallest = {name: min(smallest[name], edges[name]) for name in _EDGES}
    if smallest is None:
        raise ValueError("no frames could be sampled for bar detection")
    return _box_from_edges(smallest, width, height, threshold)


def _crop_video(
    client: Any, *, data: bytes, project: str, source: dict[str, Any], source_object_id: str,
    source_digest: str, filename: str, box: str | None, remove_bars: bool,
    bar_threshold: int, even: bool, dry_run: bool, idempotency_key: str | None,
    allow_no_bars: bool = False,
) -> DomainResult[Any]:
    suffix = Path(filename).suffix.lower() or ".mp4"
    with tempfile.TemporaryDirectory(prefix="astrid-crop-") as tmp:
        workdir = Path(tmp)
        src = workdir / f"source{suffix}"
        src.write_bytes(data)
        probe = ffprobe_metadata_strict(src)
        if probe.width is None or probe.height is None:
            raise ValueError("ffprobe found no video dimensions")
        width, height = probe.width, probe.height
        detection = (
            _detect_video_bars(src, probe.duration_seconds or 0.0, width, height, bar_threshold, workdir)
            if remove_bars else None
        )
        plan = _plan(box, detection, width, height)
        plan["audio"] = _audio_plan(probe.audio_codec)
        _refuse_if_no_bars(plan, remove_bars=remove_bars, dry_run=dry_run, allow_no_bars=allow_no_bars)
        crop = plan["box"].even() if even else plan["box"]
        crop.check_within(width, height)
        if crop.w <= 0 or crop.h <= 0:
            raise ValueError("box is empty after even rounding")
        if not even and (crop.w % 2 or crop.h % 2):
            raise ValueError(
                f"video crop {crop.w}x{crop.h} has odd dimensions; pass --even"
            )
        if dry_run:
            return _result(source, plan, crop, "video", dry_run=True)
        out = workdir / f"{Path(filename).stem}-crop.mp4"
        _run_ffmpeg(ffmpeg_crop_args(
            src, out, crop, fps_rational=probe.fps_rational, audio_action=plan["audio"]["action"],
        ))
        if not out.exists() or out.stat().st_size == 0:
            raise RuntimeError("ffmpeg completed without producing a video")
        payload = out.read_bytes()
        return _write_and_import(
            client, project=project, source=source, source_object_id=source_object_id,
            source_digest=source_digest, filename=filename, plan=plan, crop=crop,
            kind="video", even=even, payload=payload, suffix=".mp4",
            idempotency_key=idempotency_key,
        )
