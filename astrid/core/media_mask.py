"""Create binary edit masks and preview them in the working frame."""

from __future__ import annotations

import io
import json
import math
import re
import subprocess
import tempfile
from fractions import Fraction
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageOps, PngImagePlugin

from astrid.core.media_crop import _failure, _kind_for
from astrid.core.media_framing import fill_filter, probe_streams
from astrid.sdk.contracts import DomainResult
from astrid.sdk.exceptions import ServiceError

DEFAULT_SIZE = (1024, 576)
DEFAULT_SAMPLES = 6
RECIPE_KEY = "astrid.mask.recipe"


def _numbers(value: str, lengths: tuple[int, ...], label: str) -> list[float]:
    try:
        parts = [float(part) for part in value.split(",")]
    except ValueError:
        parts = []
    if len(parts) not in lengths or not all(math.isfinite(part) for part in parts):
        raise ValueError(f"{label} requires finite comma-separated numbers")
    return parts


def _ellipse_points(shape: list[float], grow: int) -> list[tuple[float, float]]:
    cx, cy, rx, ry, degrees = shape
    angle = math.radians(degrees)
    cosine, sine = math.cos(angle), math.sin(angle)
    points = []
    for degree in range(360):
        t = math.radians(degree)
        x, y = (rx + grow) * math.cos(t), (ry + grow) * math.sin(t)
        points.append((cx + x * cosine - y * sine, cy + x * sine + y * cosine))
    return points


def build_mask(
    name: str, ellipses: list[str], boxes: list[str], grow: int,
    size: tuple[int, int], invert: bool,
) -> tuple[Image.Image, dict]:
    if not name.strip():
        raise ValueError("give a non-empty --name when creating a mask")
    if not ellipses and not boxes:
        raise ValueError("give at least one --ellipse or --box")
    if grow < 0:
        raise ValueError("--grow must be non-negative")
    width, height = size
    image = Image.new("L", size)
    draw = ImageDraw.Draw(image)
    recipe = {"name": name, "size": list(size), "grow": grow, "invert": invert,
              "ellipses": [], "boxes": []}
    for raw in ellipses:
        shape = _numbers(raw, (4, 5), "--ellipse CX,CY,RX,RY[,ROT_DEG]")
        if len(shape) == 4:
            shape.append(0.0)
        cx, cy, rx, ry, degrees = shape
        if rx <= 0 or ry <= 0:
            raise ValueError("ellipse radii must be positive")
        angle = math.radians(degrees)
        dx = math.hypot(rx * math.cos(angle), ry * math.sin(angle))
        dy = math.hypot(rx * math.sin(angle), ry * math.cos(angle))
        if cx - dx < 0 or cy - dy < 0 or cx + dx > width or cy + dy > height:
            raise ValueError("ellipse is outside the working frame")
        # Beyond the frame diagonal, the grown ellipse already covers the canvas.
        draw.polygon(_ellipse_points(shape, min(grow, max(size) * 2)), fill=255)
        recipe["ellipses"].append(shape)
    for raw in boxes:
        try:
            x, y, w, h = (int(part) for part in raw.split(","))
        except ValueError as exc:
            raise ValueError("--box requires integer X,Y,W,H") from exc
        if w <= 0 or h <= 0:
            raise ValueError("box width and height must be positive")
        if x < 0 or y < 0 or x + w > width or y + h > height:
            raise ValueError("box is outside the working frame")
        draw.rectangle((max(0, x - grow), max(0, y - grow),
                        min(width - 1, x + w + grow - 1), min(height - 1, y + h + grow - 1)), fill=255)
        recipe["boxes"].append([x, y, w, h])
    return (ImageOps.invert(image) if invert else image), recipe


def _size(value: str | None) -> tuple[int, int] | None:
    if value is None:
        return None
    match = re.fullmatch(r"([0-9]{1,5})[xX]([0-9]{1,5})", value)
    if match:
        width, height = map(int, match.groups())
        if 0 < width <= 8192 and 0 < height <= 8192:
            return width, height
    raise ValueError("--size must be WIDTHxHEIGHT with dimensions between 1 and 8192")


def _load_mask(payload: bytes) -> tuple[Image.Image, dict | None]:
    with Image.open(io.BytesIO(payload)) as image:
        if (image.format != "PNG" or image.mode not in {"1", "L"}
                or getattr(image, "n_frames", 1) != 1):
            raise ValueError("--mask requires a single-channel PNG")
        mask = image.convert("L")
        if any(mask.histogram()[1:255]):
            raise ValueError("--mask pixels must be strictly binary (0 or 255)")
        recipe = image.info.get(RECIPE_KEY)
        return mask, json.loads(recipe) if recipe else None


def _preview_times(
    info: dict, stream: dict, kind: str, span: str | None, samples: int,
) -> list[float]:
    if kind == "image":
        if span is not None:
            raise ValueError("--range requires video")
        return [0.0]
    try:
        duration = float(stream.get("duration") or info["format"]["duration"])
    except (KeyError, ValueError, TypeError) as exc:
        raise ValueError("preview video duration is unavailable") from exc
    start, end = _numbers(span, (2,), "--range A,B") if span is not None else (0.0, duration)
    if not math.isfinite(duration) or not 0 <= start < end <= duration:
        raise ValueError("--range must satisfy 0 <= A < B within the video duration")
    try:
        frame_period = float(1 / Fraction(stream["avg_frame_rate"]))
    except (KeyError, ValueError, ZeroDivisionError):
        frame_period = 1 / 24
    last = max(0.0, duration - frame_period)
    return [min(last, start + (end - start) * (i + 0.5) / samples) for i in range(samples)]


def _fitted_frame(path: Path, seconds: float, size: tuple[int, int], *, square_default: bool) -> Image.Image:
    result = subprocess.run(
        ["ffmpeg", "-v", "error", "-nostdin", "-filter_threads", "1",
         "-ss", f"{seconds:.9f}", "-i", str(path), "-map", "0:v:0",
         "-vf", fill_filter(*size, square_default=square_default), "-frames:v", "1",
         "-c:v", "png", "-threads", "1", "-f", "image2pipe", "pipe:1"],
        check=True, capture_output=True, timeout=30,
    )
    if not result.stdout:
        raise ValueError(f"no video frame available at {seconds:.6f} seconds")
    with Image.open(io.BytesIO(result.stdout)) as frame:
        return frame.convert("RGB")


def _preview(path: Path, kind: str, mask: Image.Image, span: str | None, samples: int) -> dict:
    info = probe_streams(path)
    stream = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), None)
    if stream is None:
        raise ValueError("--preview-on requires an image or video")
    times = _preview_times(info, stream, kind, span, samples)
    square_default = stream.get("sample_aspect_ratio") in (None, "N/A", "0:1")
    edge = ImageChops.subtract(mask, mask.filter(ImageFilter.MinFilter(3)))
    edge_color = Image.new("RGB", mask.size, (0, 255, 255))
    tint = mask.point(lambda value: value // 5)
    masked_pixels = mask.histogram()[255]
    width, height = mask.size
    columns = min(3, len(times))
    sheet_size = (columns * width, math.ceil(len(times) / columns) * height)
    if math.prod(sheet_size) > 64_000_000:
        raise ValueError("preview sheet is too large; reduce --size or --samples")
    sheet = Image.new("RGB", sheet_size)
    reports = []
    previous = None
    for index, seconds in enumerate(times):
        frame = _fitted_frame(path, seconds, mask.size, square_default=square_default)
        changed_fraction = None
        if previous is not None and masked_pixels:
            delta = ImageChops.difference(frame, previous)
            red, green, blue = delta.split()
            changed = ImageChops.lighter(ImageChops.lighter(red, green), blue).point(
                lambda value: 255 if value >= 16 else 0
            )
            changed_fraction = ImageChops.multiply(mask, changed).histogram()[255] / masked_pixels
        reports.append({"seconds": seconds, "changed_fraction": changed_fraction})
        previous = frame
        tile = Image.composite(edge_color, frame, tint)
        tile.paste(edge_color, mask=edge)
        sheet.paste(tile, (index % columns * width, index // columns * height))
    return {"sheet": sheet, "samples": reports}


def mask_media(
    client: Any, *, project: str, name: str | None, ellipses: list[str], boxes: list[str],
    grow: int | None, size: str | None, invert: bool, mask_ref: str | None,
    preview_on: str | None, span: str | None, samples: int | None,
) -> DomainResult[Any]:
    try:
        requested_size = _size(size)
        if preview_on is None and (span is not None or samples is not None):
            raise ValueError("--range and --samples require --preview-on")
        count = DEFAULT_SAMPLES if samples is None else samples
        if not 1 <= count <= 24:
            raise ValueError("--samples must be between 1 and 24")
        if mask_ref is not None:
            if name is not None or ellipses or boxes or grow is not None or invert:
                raise ValueError("--mask cannot be combined with mask creation options")
            shown = client.media.show(project, mask_ref)
            if not shown.ok:
                return shown
            object_id = shown.data["object_id"]
            image, recipe = _load_mask(client.media.read_bytes(object_id))
            if requested_size is not None and image.size != requested_size:
                raise ValueError("--size must match the stored mask dimensions")
        else:
            image, recipe = build_mask(name or "", ellipses, boxes, grow or 0,
                                       requested_size or DEFAULT_SIZE, invert)
        with tempfile.TemporaryDirectory(prefix="astrid-mask-") as temporary:
            work = Path(temporary)
            preview = None
            if preview_on is not None:
                source = client.media.show(project, preview_on)
                if not source.ok:
                    return source
                filename = source.data.get("filename") or ""
                kind = _kind_for(source.data.get("media_type") or "", filename)
                if kind is None:
                    raise ValueError("--preview-on requires an image or video")
                path = work / ("source" + Path(filename).suffix)
                path.write_bytes(client.media.read_bytes(source.data["object_id"]))
                preview = _preview(path, kind, image, span, count)
            receipt = None
            key = ""
            if mask_ref is None:
                metadata = PngImagePlugin.PngInfo()
                metadata.add_itxt(RECIPE_KEY, json.dumps(recipe, sort_keys=True, separators=(",", ":")))
                path = work / "mask.png"
                image.save(path, pnginfo=metadata)
                imported = client.media.import_file(project=project, path=path, realm="managed_local")
                if not imported.ok:
                    return imported
                object_id = imported.data["object_id"]
                receipt, key = imported.receipt, imported.idempotency_key
            data = {"object_id": object_id, "recipe": recipe}
            if preview is not None:
                sheet_path = Path(tempfile.mkdtemp(prefix="astrid-mask-preview-")) / "contact.png"
                preview["sheet"].save(sheet_path)
                data.update(preview_path=str(sheet_path), samples=preview["samples"])
        return DomainResult.success(data, receipt=receipt, idempotency_key=key)
    except (ValueError, Image.UnidentifiedImageError, Image.DecompressionBombError) as exc:
        return _failure("validation_error", str(exc))
    except subprocess.CalledProcessError as exc:
        return _failure("unavailable", f"could not decode preview frame: {exc.stderr.decode(errors='replace').strip()}")
    except (OSError, subprocess.TimeoutExpired, ServiceError) as exc:
        return _failure("unavailable", str(exc))
