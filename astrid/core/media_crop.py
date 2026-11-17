"""Crop a managed still image or video into a NEW managed media item.

``astrid media crop <media> --project P`` reads one project media item,
crops it (an explicit ``--box`` or detected letterbox bars), imports the
result through the same ``import_file`` path as ``astrid media import``, and
records provenance as a ``derived_from`` relation from the new media to the
source media. The source item is never modified.

Detection is deliberately simple and reported: a row (or column) is a bar
when at most ``_BAR_MAX_BRIGHT_FRACTION`` of its pixels have a max channel
value above the reported threshold. Bars are the contiguous bar rows/columns
at each edge. Nothing is written to the project during ``dry_run``.
"""

from __future__ import annotations

import hashlib
import io
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

_IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"})
_VIDEO_PREFIX = "video/"
_IMAGE_PREFIX = "image/"


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
    """Parse ``X,Y,W,H`` (source pixels)."""
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


def detect_bars(rgb: Any, *, threshold: int = DEFAULT_BAR_THRESHOLD) -> BarDetection:
    """Detect near-black letterbox/pillarbox bars on each edge of an RGB array.

    ``rgb`` is an ``(H, W, 3)`` uint8 array. Raises ``ValueError`` when the
    bars would leave no picture.
    """
    import numpy as np

    if not 0 <= threshold <= 255:
        raise ValueError("bar threshold must be 0..255")
    height, width = rgb.shape[:2]
    bright = np.asarray(rgb).max(axis=2) > threshold
    row_is_bar = bright.mean(axis=1) <= _BAR_MAX_BRIGHT_FRACTION
    col_is_bar = bright.mean(axis=0) <= _BAR_MAX_BRIGHT_FRACTION

    top = _leading_true(row_is_bar)
    bottom = _leading_true(row_is_bar[::-1])
    left = _leading_true(col_is_bar)
    right = _leading_true(col_is_bar[::-1])
    if top + bottom >= height or left + right >= width:
        raise ValueError("detected bars cover the whole frame; nothing would remain")
    box = Box(left, top, width - left - right, height - top - bottom)
    edges = {"top": top, "bottom": bottom, "left": left, "right": right}
    return BarDetection(box=box, threshold=threshold, edges=edges)


def _leading_true(flags: Any) -> int:
    count = 0
    for flag in flags:
        if not flag:
            break
        count += 1
    return count


def _load_rgb(data: bytes) -> Any:
    import numpy as np
    from PIL import Image

    with Image.open(io.BytesIO(data)) as image:
        return np.asarray(image.convert("RGB"), dtype=np.uint8).copy()


def crop_image_bytes(data: bytes, box: Box) -> bytes:
    """Crop still-image bytes and return lossless PNG bytes."""
    from PIL import Image

    with Image.open(io.BytesIO(data)) as image:
        box.check_within(*image.size)
        cropped = image.crop((box.x, box.y, box.x + box.w, box.y + box.h))
        out = io.BytesIO()
        cropped.save(out, format="PNG")
    return out.getvalue()


def image_size(data: bytes) -> tuple[int, int]:
    from PIL import Image

    with Image.open(io.BytesIO(data)) as image:
        return image.size


def ffmpeg_crop_args(
    src: Path, dst: Path, box: Box, *, fps_rational: tuple[int, int] | None
) -> list[str]:
    """Build the ffmpeg command: re-encode video at the same fps, copy audio."""
    args = [
        "ffmpeg", "-nostdin", "-v", "error", "-y",
        "-i", str(src),
        "-map", "0:v:0", "-map", "0:a?",
        "-vf", f"crop={box.w}:{box.h}:{box.x}:{box.y}",
    ]
    if fps_rational is not None:
        args += ["-r", f"{fps_rational[0]}/{fps_rational[1]}"]
    args += [
        "-c:v", "libx264", "-preset", "medium", "-crf", "16",
        "-pix_fmt", "yuv420p",
        "-c:a", "copy",
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
    if suffix in {".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi"}:
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
) -> DomainResult[Any]:
    """Crop one project media item into a new managed media item.

    Exactly one of ``box`` (``"X,Y,W,H"``) or ``remove_bars`` must be given.
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
            rgb = _load_rgb(data) if remove_bars else None
            plan = _plan(box, remove_bars, bar_threshold, width, height, rgb)
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
                kind=kind, even=even, payload=payload, suffix=".png",
                idempotency_key=idempotency_key, dry_run=dry_run,
            )
        return _crop_video(
            client, data=data, project=project, source=source, source_object_id=source_object_id,
            source_digest=source_digest, filename=filename, box=box, remove_bars=remove_bars,
            bar_threshold=bar_threshold, even=even, dry_run=dry_run,
            idempotency_key=idempotency_key,
        )
    except (ValueError, MediaProbeError) as exc:
        return _failure("validation_error", str(exc), idempotency_key or "")
    except RuntimeError as exc:
        return _failure("unavailable", str(exc), idempotency_key or "")


def _plan(box: str | None, remove_bars: bool, threshold: int, width: int, height: int, rgb: Any) -> dict[str, Any]:
    if remove_bars:
        if rgb is None:
            raise ValueError("bar detection needs pixels")
        detection = detect_bars(rgb, threshold=threshold)
        return {"box": detection.box, "bars": detection.as_dict()}
    parsed = parse_box(str(box))
    parsed.check_within(width, height)
    return {"box": parsed, "bars": None}


def _result(source: dict[str, Any], plan: dict[str, Any], crop: Box, kind: str, *, dry_run: bool, media: Any = None, provenance: Any = None) -> DomainResult[Any]:
    data: dict[str, Any] = {
        "dry_run": dry_run,
        "kind": kind,
        "source": {"object_id": source.get("object_id"), "digest": source.get("digest")},
        "box": crop.as_dict(),
        "output": {"width": crop.w, "height": crop.h},
        "detected": plan["bars"],
    }
    if media is not None:
        data["media"] = media
    if provenance is not None:
        data["provenance"] = provenance
    return DomainResult.success(data, idempotency_key="")


def _write_and_import(
    client: Any, *, project: str, source: dict[str, Any], source_object_id: str,
    source_digest: str, filename: str, plan: dict[str, Any], crop: Box, kind: str,
    even: bool, payload: bytes, suffix: str, idempotency_key: str | None, dry_run: bool,
) -> DomainResult[Any]:
    key = idempotency_key or _default_key(source_digest, crop, even)
    with tempfile.TemporaryDirectory(prefix="astrid-crop-") as tmp:
        out_path = Path(tmp) / f"{Path(filename).stem}-crop{suffix}"
        out_path.write_bytes(payload)
        imported = client.media.import_file(project=project, path=out_path, realm="managed_local", idempotency_key=key)
        if not imported.ok:
            return imported
        return _record_provenance(
            client, project=project, source=source, source_object_id=source_object_id,
            source_digest=source_digest, plan=plan, crop=crop, kind=kind, even=even,
            imported=imported, key=key,
        )


def _record_provenance(
    client: Any, *, project: str, source: dict[str, Any], source_object_id: str,
    source_digest: str, plan: dict[str, Any], crop: Box, kind: str, even: bool,
    imported: DomainResult[Any], key: str,
) -> DomainResult[Any]:
    media = dict(imported.data) if isinstance(imported.data, dict) else {"result": imported.data}
    new_object_id = media.get("object_id")
    if not isinstance(new_object_id, str):
        return _failure("protocol_error", "imported crop has no object_id", key, media=media)
    metadata = {
        "operation": "crop",
        "source_digest": source_digest,
        "source_object_id": source_object_id,
        "box": crop.as_dict(),
        "even": even,
        "bars": plan["bars"],
    }
    related = client.media.relate(
        project,
        from_object_id=new_object_id,
        to_object_id=source_object_id,
        kind="derived_from",
        metadata=metadata,
        idempotency_key=key,
    )
    if not related.ok:
        error = related.error
        return _failure(
            error.code if error is not None else "protocol_error",
            "crop was imported but its provenance could not be recorded",
            key,
            imported_object_id=new_object_id,
        )
    return _result(source, plan, crop, kind, dry_run=False, media=media, provenance=metadata)


def _default_key(source_digest: str, crop: Box, even: bool) -> str:
    raw = f"crop|{source_digest}|{crop.x},{crop.y},{crop.w},{crop.h}|even={even}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def _crop_video(
    client: Any, *, data: bytes, project: str, source: dict[str, Any], source_object_id: str,
    source_digest: str, filename: str, box: str | None, remove_bars: bool,
    bar_threshold: int, even: bool, dry_run: bool, idempotency_key: str | None,
) -> DomainResult[Any]:
    suffix = Path(filename).suffix.lower() or ".mp4"
    with tempfile.TemporaryDirectory(prefix="astrid-crop-") as tmp:
        src = Path(tmp) / f"source{suffix}"
        src.write_bytes(data)
        probe = ffprobe_metadata_strict(src)
        if probe.width is None or probe.height is None:
            raise ValueError("ffprobe found no video dimensions")
        width, height = probe.width, probe.height
        plan: dict[str, Any]
        if remove_bars:
            frame = Path(tmp) / "frame.png"
            duration = probe.duration_seconds or 0.0
            _run_ffmpeg([
                "ffmpeg", "-nostdin", "-v", "error", "-y",
                "-ss", f"{duration / 2:.3f}", "-i", str(src),
                "-frames:v", "1", str(frame),
            ])
            plan = _plan(None, True, bar_threshold, width, height, _load_rgb(frame.read_bytes()))
        else:
            plan = _plan(box, False, bar_threshold, width, height, None)
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
        out = Path(tmp) / f"{Path(filename).stem}-crop.mp4"
        _run_ffmpeg(ffmpeg_crop_args(src, out, crop, fps_rational=probe.fps_rational))
        if not out.exists() or out.stat().st_size == 0:
            raise RuntimeError("ffmpeg completed without producing a video")
        payload = out.read_bytes()
        return _write_and_import(
            client, project=project, source=source, source_object_id=source_object_id,
            source_digest=source_digest, filename=filename, plan=plan, crop=crop,
            kind="video", even=even, payload=payload, suffix=".mp4",
            idempotency_key=idempotency_key, dry_run=dry_run,
        )
