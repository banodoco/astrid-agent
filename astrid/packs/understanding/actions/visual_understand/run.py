#!/usr/bin/env python3
"""Query OpenAI vision models against one image or a numbered frame sheet."""


from __future__ import annotations

from astrid.pack_api import guard_canonical_entrypoint

guard_canonical_entrypoint('understanding.visual_understand')
import argparse
import base64
import hashlib
import json
import math
import mimetypes
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import jsonschema

if TYPE_CHECKING:
    from PIL import Image, ImageFont

from astrid.pack_api import build_manifest, write_manifest
from astrid.pack_api import add_choice_arg
from astrid.pack_api import AstridError
from astrid.pack_api import run_pack_main
from astrid.pack_api import CredentialsScope
from astrid.packs.understanding.shared._common import emit_dry_run_preview

API_URL = "https://api.openai.com/v1/responses"
MODEL_PRESETS = {
    "fast": "gpt-4o-mini",
    "best": "gpt-5.4",
}
DEFAULT_MODE = "fast"
DEFAULT_MAX_IMAGES = 20
DEFAULT_ORDERED_SETTINGS: dict[str, Any] = {
    "cost_ceiling": DEFAULT_MAX_IMAGES,
    "detail": "high",
    "max_output_tokens": 700,
    "timeout": 120,
}
_ORDERED_LOCAL_SETTINGS = frozenset({"cost_ceiling", "detail", "timeout"})
_ORDERED_RESERVED_SETTINGS = frozenset({"input", "model", "stream", "structured", "text"})


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _canonical_json_copy(value: Any, *, label: str) -> Any:
    try:
        return json.loads(_canonical_json(value))
    except (TypeError, ValueError) as exc:
        raise AstridError(
            f"{label} must contain only JSON-serializable values: {exc}",
            recovery_command=f"replace non-JSON values in {label} and retry",
        ) from exc


@dataclass(frozen=True)
class OrderedImageEvidence:
    """Provenance and validated answers from one ordered-image request."""

    prompt: str
    prompt_sha256: str
    image_paths: tuple[str, ...]
    image_hashes: tuple[str, ...]
    model: str
    settings: Mapping[str, Any]
    response_id: str | None
    returned_model: str | None
    usage: Mapping[str, Any] | None
    answers: Mapping[str, Any]
    cost_ceiling: int

    def to_dict(self) -> dict[str, Any]:
        """Return a deterministic, JSON-native representation of this evidence."""

        payload = {
            "answers": dict(self.answers),
            "cost_ceiling": self.cost_ceiling,
            "image_hashes": list(self.image_hashes),
            "image_paths": list(self.image_paths),
            "model": self.model,
            "prompt": self.prompt,
            "prompt_sha256": self.prompt_sha256,
            "response_id": self.response_id,
            "returned_model": self.returned_model,
            "settings": dict(self.settings),
            "usage": dict(self.usage) if self.usage is not None else None,
        }
        return _canonical_json_copy(payload, label="ordered image evidence")

    def to_json(self) -> str:
        """Serialize evidence as canonical JSON suitable for a run artifact."""

        return _canonical_json(self.to_dict())

    def to_json_bytes(self) -> bytes:
        """Serialize evidence to deterministic UTF-8 bytes."""

        return self.to_json().encode("utf-8")


def _pil():
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ModuleNotFoundError as exc:
        _die("visual_understand requires Pillow for image contact sheets; install the executor requirements first")
    return Image, ImageDraw, ImageFont


from astrid.pack_api import pack_die


def _die(message: str) -> None:
    pack_die(message, recovery_command="check the inputs and retry; see --help for usage")


def load_api_key(*, env_file: Path | None = None) -> str:
    return CredentialsScope.get_local("openai", env_file=env_file)


def _parse_timestamp(value: str) -> float:
    raw = value.strip()
    if not raw:
        _die("empty timestamp")
    if ":" not in raw:
        return float(raw)
    parts = [float(part) for part in raw.split(":")]
    if len(parts) == 2:
        minutes, seconds = parts
        return minutes * 60 + seconds
    if len(parts) == 3:
        hours, minutes, seconds = parts
        return hours * 3600 + minutes * 60 + seconds
    _die(f"invalid timestamp: {value}")
    return 0.0


def _format_time(seconds: float) -> str:
    whole = int(seconds)
    h, rem = divmod(whole, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def _parse_times(values: list[str]) -> list[float]:
    times: list[float] = []
    for value in values:
        for part in value.split(","):
            if part.strip():
                times.append(_parse_timestamp(part))
    return times


def _extract_video_frames(video: Path, times: list[float], out_dir: Path, force: bool) -> list[tuple[Path, str]]:
    if not video.is_file():
        _die(f"video not found: {video}")
    frames_dir = out_dir / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    frames: list[tuple[Path, str]] = []
    for index, seconds in enumerate(times, start=1):
        path = frames_dir / f"frame_{index:03d}_{int(seconds * 1000):09d}ms.jpg"
        if path.exists() and not force:
            frames.append((path, _format_time(seconds)))
            continue
        command = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            f"{seconds:.3f}",
            "-i",
            str(video),
            "-frames:v",
            "1",
            "-q:v",
            "2",
            str(path),
        ]
        subprocess.run(command, check=True)
        frames.append((path, _format_time(seconds)))
    return frames


def _load_font(size: int) -> ImageFont.ImageFont:
    _, _, ImageFont = _pil()
    for candidate in (
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/Library/Fonts/Arial Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ):
        path = Path(candidate)
        if path.exists():
            return ImageFont.truetype(str(path), size)
    return ImageFont.load_default()


def _fit_image(image: Image.Image, width: int, height: int) -> Image.Image:
    Image, _, _ = _pil()
    fitted = image.convert("RGB").copy()
    fitted.thumbnail((width, height), Image.Resampling.LANCZOS)
    canvas = Image.new("RGB", (width, height), (12, 12, 14))
    x = (width - fitted.width) // 2
    y = (height - fitted.height) // 2
    canvas.paste(fitted, (x, y))
    return canvas


def _build_contact_sheet(
    frames: list[tuple[Path, str]],
    *,
    out_path: Path,
    cols: int,
    tile_width: int,
    label_prefix: str,
) -> Path:
    Image, ImageDraw, _ = _pil()
    if not frames:
        _die("no images or frames provided")
    cols = max(1, min(cols, len(frames)))
    rows = math.ceil(len(frames) / cols)
    first = Image.open(frames[0][0])
    ratio = first.height / first.width
    tile_height = max(120, int(tile_width * ratio))
    label_height = max(42, int(tile_width * 0.09))
    sheet = Image.new("RGB", (cols * tile_width, rows * (tile_height + label_height)), (10, 10, 12))
    font = _load_font(max(18, int(tile_width * 0.052)))

    for index, (path, label) in enumerate(frames, start=1):
        col = (index - 1) % cols
        row = (index - 1) // cols
        x = col * tile_width
        y = row * (tile_height + label_height)
        with Image.open(path) as image:
            sheet.paste(_fit_image(image, tile_width, tile_height), (x, y))
        draw = ImageDraw.Draw(sheet)
        draw.rectangle((x, y, x + tile_width, y + label_height), fill=(0, 0, 0))
        text = f"{label_prefix} {index}"
        if label:
            text = f"{text}  {label}"
        draw.text((x + 14, y + 9), text, fill=(255, 255, 255), font=font)
        draw.rectangle((x, y, x + tile_width - 1, y + tile_height + label_height - 1), outline=(70, 70, 74), width=2)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out_path, quality=92)
    return out_path


def _parse_aspect(value: str) -> tuple[int, int, str]:
    raw = value.strip().lower()
    if ":" in raw:
        left, right = raw.split(":", 1)
    elif "/" in raw:
        left, right = raw.split("/", 1)
    else:
        _die(f"invalid crop aspect {value!r}; use WIDTH:HEIGHT, for example 9:16")
    width = int(left)
    height = int(right)
    if width <= 0 or height <= 0:
        _die(f"invalid crop aspect {value!r}")
    return width, height, f"{width}:{height}"


def _parse_csv(values: list[str] | None) -> list[str]:
    out: list[str] = []
    for value in values or []:
        out.extend(part.strip() for part in value.split(",") if part.strip())
    return out


def _default_crop_positions(aspect_width: int, aspect_height: int) -> list[str]:
    if aspect_width < aspect_height:
        return ["left", "center", "right"]
    if aspect_width > aspect_height:
        return ["top", "center", "bottom"]
    return ["center"]


def _crop_box(width: int, height: int, aspect_width: int, aspect_height: int, position: str) -> tuple[int, int, int, int]:
    target_ratio = aspect_width / aspect_height
    source_ratio = width / height
    if source_ratio > target_ratio:
        crop_h = height
        crop_w = int(round(height * target_ratio))
        if position in {"left", "top-left", "bottom-left"}:
            x = 0
        elif position in {"right", "top-right", "bottom-right"}:
            x = width - crop_w
        else:
            x = (width - crop_w) // 2
        y = 0
    else:
        crop_w = width
        crop_h = int(round(width / target_ratio))
        if position in {"top", "top-left", "top-right"}:
            y = 0
        elif position in {"bottom", "bottom-left", "bottom-right"}:
            y = height - crop_h
        else:
            y = (height - crop_h) // 2
        x = 0
    return x, y, x + crop_w, y + crop_h


def _build_crop_variants(
    source: tuple[Path, str],
    *,
    aspects: list[str],
    positions: list[str],
    out_dir: Path,
    force: bool,
) -> list[tuple[Path, str]]:
    Image, _, _ = _pil()
    image_path, source_label = source
    crop_dir = out_dir / "crops"
    crop_dir.mkdir(parents=True, exist_ok=True)
    variants: list[tuple[Path, str]] = []
    with Image.open(image_path) as opened:
        image = opened.convert("RGB")
        for aspect_value in aspects:
            aspect_width, aspect_height, aspect_label = _parse_aspect(aspect_value)
            selected_positions = positions or _default_crop_positions(aspect_width, aspect_height)
            for position in selected_positions:
                normalized_position = position.lower()
                box = _crop_box(image.width, image.height, aspect_width, aspect_height, normalized_position)
                out_path = crop_dir / f"{image_path.stem}_{aspect_label.replace(':', 'x')}_{normalized_position}.jpg"
                if force or not out_path.exists():
                    image.crop(box).save(out_path, quality=94)
                label = f"{aspect_label} {normalized_position}"
                if source_label:
                    label = f"{label} {source_label}"
                variants.append((out_path, label))
    return variants


def _encode_image(path: Path) -> tuple[str, str]:
    media_type = mimetypes.guess_type(path.name)[0] or "image/jpeg"
    data = base64.b64encode(path.read_bytes()).decode("ascii")
    return media_type, data


def _response_text(response: dict[str, Any]) -> str:
    if isinstance(response.get("output_text"), str):
        return response["output_text"]
    chunks: list[str] = []
    for item in response.get("output") or []:
        for content in item.get("content") or []:
            text = content.get("text")
            if isinstance(text, str):
                chunks.append(text)
    return "\n".join(chunks).strip()


def _send_responses_request(
    *,
    api_key: str,
    payload: Mapping[str, Any],
    timeout: int,
) -> dict[str, Any]:
    """POST one Responses API payload.

    This narrow transport seam is shared by the legacy single/contact-sheet
    path and the additive ordered-image path, and is intentionally easy to
    replace in hermetic tests.
    """

    request = Request(
        API_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            decoded = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail_text = exc.read().decode("utf-8", errors="replace")
        raise AstridError(
            f"OpenAI API error {exc.code}: {detail_text}",
            recovery_command="verify the API key and model name, then retry",
        ) from exc
    except URLError as exc:
        raise AstridError(
            f"Network error: {exc}",
            recovery_command="check network connectivity and retry",
        ) from exc
    if not isinstance(decoded, dict):
        raise AstridError(
            "OpenAI Responses API returned a non-object response",
            recovery_command="retry the request and inspect the provider response",
        )
    return decoded


def _call_responses_api(
    *,
    api_key: str,
    model: str,
    query: str,
    image_path: Path,
    detail: str,
    max_output_tokens: int,
    timeout: int,
    response_schema: dict[str, Any] | None = None,
) -> dict[str, Any]:
    media_type, data = _encode_image(image_path)
    payload: dict[str, Any] = {
        "model": model,
        "input": [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": query},
                    {
                        "type": "input_image",
                        "image_url": f"data:{media_type};base64,{data}",
                        "detail": detail,
                    },
                ],
            }
        ],
        "max_output_tokens": max_output_tokens,
    }
    if response_schema is not None:
        payload["text"] = {
            "format": {
                "type": "json_schema",
                "name": response_schema.get("name", "response"),
                "schema": response_schema["schema"] if "schema" in response_schema else response_schema,
                "strict": response_schema.get("strict", True),
            }
        }
    return _send_responses_request(api_key=api_key, payload=payload, timeout=timeout)


def _normalize_ordered_settings(settings: Mapping[str, Any] | None) -> dict[str, Any]:
    supplied = dict(settings or {})
    if any(not isinstance(key, str) for key in supplied):
        raise AstridError(
            "ordered-image setting names must be strings",
            recovery_command="use string keys for ordered-image settings and retry",
        )
    forbidden = sorted(_ORDERED_RESERVED_SETTINGS.intersection(supplied))
    if forbidden:
        raise AstridError(
            f"ordered-image settings may not override reserved request fields: {', '.join(forbidden)}",
            recovery_command="pass model and structured schema through their dedicated arguments",
        )

    normalized = {**DEFAULT_ORDERED_SETTINGS, **supplied}
    detail = normalized["detail"]
    if detail not in {"auto", "high", "low"}:
        raise AstridError(
            f"invalid ordered-image detail setting: {detail!r}",
            valid_options=("auto", "high", "low"),
            recovery_command="set settings['detail'] to auto, high, or low",
        )

    for key in ("cost_ceiling", "max_output_tokens", "timeout"):
        value = normalized[key]
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise AstridError(
                f"ordered-image setting {key!r} must be a positive integer",
                recovery_command=f"set settings['{key}'] to a positive integer",
            )
    if normalized["cost_ceiling"] > DEFAULT_MAX_IMAGES:
        raise AstridError(
            f"ordered-image cost_ceiling exceeds the hard maximum of {DEFAULT_MAX_IMAGES}",
            recovery_command=f"set settings['cost_ceiling'] to at most {DEFAULT_MAX_IMAGES}",
        )
    return _canonical_json_copy(normalized, label="ordered-image settings")


def _ordered_schema_format(
    structured: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    wrapper = _canonical_json_copy(dict(structured), label="structured schema")
    schema = wrapper.get("schema", wrapper)
    if not isinstance(schema, dict):
        raise AstridError(
            "structured schema must be a JSON object",
            recovery_command="pass a raw JSON schema or {name, schema, strict} wrapper",
        )
    name = wrapper.get("name", "response") if "schema" in wrapper else "response"
    strict = wrapper.get("strict", True) if "schema" in wrapper else True
    if not isinstance(name, str) or not name:
        raise AstridError(
            "structured schema name must be a non-empty string",
            recovery_command="provide a non-empty JSON-schema response name",
        )
    if not isinstance(strict, bool):
        raise AstridError(
            "structured schema strict must be a boolean",
            recovery_command="set structured['strict'] to true or false",
        )
    try:
        validator_class = jsonschema.validators.validator_for(schema)
        validator_class.check_schema(schema)
    except jsonschema.SchemaError as exc:
        raise AstridError(
            f"invalid structured JSON schema: {exc.message}",
            recovery_command="fix the structured JSON schema and retry",
        ) from exc
    return (
        {"name": name, "schema": schema, "strict": strict, "type": "json_schema"},
        schema,
    )


def _assert_exact_ordered_images(payload: Mapping[str, Any], expected_urls: Sequence[str]) -> None:
    inputs = payload.get("input")
    if not isinstance(inputs, list) or len(inputs) != 1 or not isinstance(inputs[0], dict):
        raise AstridError(
            "ordered-image request must contain exactly one user input",
            recovery_command="repair the ordered-image request builder before retrying",
        )
    content = inputs[0].get("content")
    if not isinstance(content, list):
        raise AstridError(
            "ordered-image request content is malformed",
            recovery_command="repair the ordered-image request builder before retrying",
        )
    image_blocks = [block for block in content if block.get("type") == "input_image"]
    actual_urls = tuple(block.get("image_url") for block in image_blocks)
    if len(image_blocks) != len(expected_urls) or actual_urls != tuple(expected_urls):
        raise AstridError(
            "ordered-image request did not preserve the exact image count and order",
            recovery_command="repair the ordered-image request builder before retrying",
        )


def _parse_ordered_answers(
    response: dict[str, Any],
    *,
    schema: dict[str, Any] | None,
) -> dict[str, Any]:
    response_text = _response_text(response)
    try:
        answers = json.loads(response_text)
    except json.JSONDecodeError as exc:
        raise AstridError(
            f"ordered-image response is not valid JSON: {exc.msg}",
            recovery_command="retry with a structured response schema",
        ) from exc
    if not isinstance(answers, dict):
        raise AstridError(
            "ordered-image response must be a JSON object",
            recovery_command="use a structured schema whose root type is object",
        )
    if schema is not None:
        validator_class = jsonschema.validators.validator_for(schema)
        try:
            validator_class(schema).validate(answers)
        except jsonschema.ValidationError as exc:
            raise AstridError(
                f"ordered-image response failed client-side schema validation: {exc.message}",
                recovery_command="reject this response before scoring and retry the evaluation",
            ) from exc
    return _canonical_json_copy(answers, label="ordered-image answers")


def understand_ordered(
    images: Sequence[Path],
    *,
    prompt: str,
    model: str,
    settings: Mapping[str, Any] | None = None,
    structured: Mapping[str, Any] | None = None,
) -> OrderedImageEvidence:
    """Send exact image bytes as separate, ordered inputs in one request."""

    if not isinstance(model, str) or not model or model != model.strip():
        raise AstridError(
            "ordered-image understanding requires an explicit pinned model id",
            recovery_command="pass the exact provider model id, for example gpt-5.6-sol",
        )
    if model.casefold() in MODEL_PRESETS:
        raise AstridError(
            f"ordered-image understanding rejects model alias {model!r}",
            recovery_command="replace the alias with an explicit pinned provider model id",
        )
    if not isinstance(prompt, str) or not prompt:
        raise AstridError(
            "ordered-image understanding requires a non-empty prompt",
            recovery_command="provide the evaluator prompt and retry",
        )

    resolved_settings = _normalize_ordered_settings(settings)
    cost_ceiling = resolved_settings["cost_ceiling"]
    image_paths = tuple(Path(path) for path in images)
    if not image_paths:
        raise AstridError(
            "ordered-image understanding requires at least one image",
            recovery_command="provide the ordered PNG bundle and retry",
        )
    if len(image_paths) > cost_ceiling:
        raise AstridError(
            f"ordered-image cost ceiling exceeded: {len(image_paths)} images > {cost_ceiling}",
            recovery_command="reduce the image bundle or explicitly raise its bounded cost_ceiling",
        )

    image_bytes: list[bytes] = []
    for path in image_paths:
        if not path.is_file():
            raise AstridError(
                f"ordered image not found: {path}",
                recovery_command="provide an existing image path and retry",
            )
        image_bytes.append(path.read_bytes())
    image_hashes = tuple(hashlib.sha256(data).hexdigest() for data in image_bytes)

    content: list[dict[str, Any]] = [{"type": "input_text", "text": prompt}]
    expected_urls: list[str] = []
    for path, data in zip(image_paths, image_bytes, strict=True):
        media_type = mimetypes.guess_type(path.name)[0] or "image/png"
        image_url = f"data:{media_type};base64,{base64.b64encode(data).decode('ascii')}"
        expected_urls.append(image_url)
        content.append(
            {
                "type": "input_image",
                "image_url": image_url,
                "detail": resolved_settings["detail"],
            }
        )

    payload: dict[str, Any] = {
        "input": [{"content": content, "role": "user"}],
        "model": model,
    }
    for key, value in resolved_settings.items():
        if key not in _ORDERED_LOCAL_SETTINGS:
            payload[key] = value

    schema: dict[str, Any] | None = None
    evidence_settings = dict(resolved_settings)
    if structured is not None:
        response_format, schema = _ordered_schema_format(structured)
        payload["text"] = {"format": response_format}
        evidence_settings["structured"] = response_format

    _assert_exact_ordered_images(payload, expected_urls)
    response = _send_responses_request(
        api_key=load_api_key(),
        payload=payload,
        timeout=resolved_settings["timeout"],
    )
    answers = _parse_ordered_answers(response, schema=schema)
    usage = response.get("usage")
    if usage is not None and not isinstance(usage, Mapping):
        raise AstridError(
            "OpenAI Responses API returned malformed usage provenance",
            recovery_command="retry the request and inspect the provider response",
        )
    response_id = response.get("id")
    returned_model = response.get("model")
    return OrderedImageEvidence(
        prompt=prompt,
        prompt_sha256=hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        image_paths=tuple(str(path) for path in image_paths),
        image_hashes=image_hashes,
        model=model,
        settings=_canonical_json_copy(evidence_settings, label="ordered-image settings"),
        response_id=response_id if isinstance(response_id, str) else None,
        returned_model=returned_model if isinstance(returned_model, str) else None,
        usage=(
            _canonical_json_copy(dict(usage), label="ordered-image usage")
            if usage is not None
            else None
        ),
        answers=answers,
        cost_ceiling=cost_ceiling,
    )


def _collect_inputs(args: argparse.Namespace) -> tuple[list[tuple[Path, str]], Path]:
    out_dir = args.out_dir.expanduser()
    frames: list[tuple[Path, str]] = []
    for path in args.image or []:
        image = path.expanduser()
        if not image.is_file():
            _die(f"image not found: {image}")
        frames.append((image, ""))
    if args.video:
        times = _parse_times(args.at or [])
        if not times:
            _die("provide --at timestamps when using --video. To find candidate timestamps first, run boundary_candidates.py with transcript/scenes/holding-screen refs.")
        frames.extend(_extract_video_frames(args.video.expanduser(), times, out_dir, args.force))
    if len(frames) > args.max_images:
        _die(f"too many images/frames: {len(frames)} > {args.max_images}")
    if not frames:
        _die("provide --image or --video with --at")
    crop_aspects = _parse_csv(args.crop_aspect)
    if crop_aspects:
        if len(frames) != 1:
            _die("--crop-aspect requires exactly one source image/frame")
        frames = _build_crop_variants(
            frames[0],
            aspects=crop_aspects,
            positions=_parse_csv(args.crop_position),
            out_dir=out_dir,
            force=args.force,
        )
        if len(frames) > args.max_images:
            _die(f"too many crop variants: {len(frames)} > {args.max_images}")
        sheet_path = args.contact_sheet or (out_dir / "crop-contact-sheet.jpg")
        return frames, _build_contact_sheet(
            frames,
            out_path=sheet_path,
            cols=args.cols,
            tile_width=args.tile_width,
            label_prefix=args.label_prefix,
        )
    if len(frames) == 1 and not args.contact_sheet:
        return frames, frames[0][0]
    sheet_path = args.contact_sheet or (out_dir / "contact-sheet.jpg")
    return frames, _build_contact_sheet(
        frames,
        out_path=sheet_path,
        cols=args.cols,
        tile_width=args.tile_width,
        label_prefix=args.label_prefix,
    )


def run(args: argparse.Namespace) -> int:
    if args.max_images < 1 or args.max_images > DEFAULT_MAX_IMAGES:
        _die(f"--max-images must be between 1 and {DEFAULT_MAX_IMAGES}")
    frames, image_for_query = _collect_inputs(args)
    primary_model = args.model or MODEL_PRESETS[args.mode]
    models = [primary_model, *args.compare_model]
    payload_preview = {
        "endpoint": API_URL,
        "models": models,
        "query": args.query,
        "image": str(image_for_query),
        "frames": [{"index": index, "path": str(path), "label": label} for index, (path, label) in enumerate(frames, start=1)],
        "detail": args.detail,
    }
    if args.dry_run:
        return emit_dry_run_preview(payload_preview, "understanding.visual_understand")

    api_key = load_api_key(env_file=args.env_file)
    response_schema: dict[str, Any] | None = None
    if args.response_schema:
        schema_path = args.response_schema.expanduser()
        if not schema_path.is_file():
            _die(f"--response-schema file not found: {schema_path}")
        response_schema = json.loads(schema_path.read_text(encoding="utf-8"))
    results: list[dict[str, Any]] = []
    for model in models:
        print(f"querying={model} image={image_for_query}", file=sys.stderr)
        started = time.time()
        try:
            response = _call_responses_api(
                api_key=api_key,
                model=model,
                query=args.query,
                image_path=image_for_query,
                detail=args.detail,
                max_output_tokens=args.max_output_tokens,
                timeout=args.timeout,
                response_schema=response_schema,
            )
            result = {
                "model": model,
                "status": "ok",
                "elapsed_sec": round(time.time() - started, 2),
                "answer": _response_text(response),
                "usage": response.get("usage"),
                "response_id": response.get("id"),
            }
        except Exception as exc:
            result = {
                "model": model,
                "status": "error",
                "elapsed_sec": round(time.time() - started, 2),
                "error": f"{type(exc).__name__}: {exc}",
            }
        results.append(result)

    output = {**payload_preview, "results": results}

    # --- universal result manifest -----------------------
    manifest_path = (args.out.parent / "manifest.json") if args.out else (args.out_dir / "manifest.json")
    output["schema_version"] = 1
    output["kind"] = "understanding.visual_understand"
    output["manifest_path"] = str(manifest_path)
    text = json.dumps(output, indent=2)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text + "\n", encoding="utf-8")
        print(f"wrote={args.out}", file=sys.stderr)

    # The receipt root is normally the host-assigned ``{out}`` directory.
    # Standalone custom paths remain writable, but files outside that spool
    # are deliberately not claimed as harvestable outputs.
    staging_root = args.out_dir.expanduser().resolve()
    receipt_path = manifest_path.expanduser().resolve()
    receipt_root = receipt_path.parent
    manifest_outputs: list[dict[str, Any]] = []
    declared_paths: set[Path] = set()

    def add_file_output(
        name: str,
        path: Path,
        *,
        artifact_type: str,
        role: str = "auxiliary",
        is_primary: bool = False,
    ) -> None:
        candidate = path.expanduser()
        if not candidate.is_file():
            return
        resolved = candidate.resolve()
        if resolved == receipt_path or resolved in declared_paths:
            return
        try:
            resolved.relative_to(staging_root)
            relative = resolved.relative_to(receipt_root).as_posix()
        except ValueError:
            return
        manifest_outputs.append(
            {
                "name": name,
                "output_port": name,
                "path": relative,
                "type": "file",
                "artifact_type": artifact_type,
                "ordinal": len(manifest_outputs),
                "role": role,
                "is_primary": is_primary,
            }
        )
        declared_paths.add(resolved)

    def add_directory_output(name: str, path: Path, *, artifact_type: str) -> None:
        candidate = path.expanduser()
        if not candidate.is_dir() or not any(child.is_file() for child in candidate.rglob("*")):
            return
        resolved = candidate.resolve()
        try:
            resolved.relative_to(staging_root)
            relative = resolved.relative_to(receipt_root).as_posix()
        except ValueError:
            return
        manifest_outputs.append(
            {
                "name": name,
                "output_port": name,
                "path": relative,
                "type": "directory",
                "artifact_type": artifact_type,
                "ordinal": len(manifest_outputs),
                "role": "auxiliary",
                "is_primary": False,
            }
        )

    if args.out:
        add_file_output(
            "result",
            args.out,
            artifact_type="understanding/visual",
            role="result",
            is_primary=True,
        )
    crop_aspects = _parse_csv(args.crop_aspect)
    if crop_aspects:
        add_file_output(
            "crop_contact_sheet",
            image_for_query,
            artifact_type="understanding/visual-crop-contact-sheet",
        )
        add_directory_output(
            "crops",
            args.out_dir / "crops",
            artifact_type="understanding/visual-crops",
        )
    elif len(frames) != 1 or args.contact_sheet:
        add_file_output(
            "contact_sheet",
            image_for_query,
            artifact_type="understanding/visual-contact-sheet",
        )
    if args.video:
        add_directory_output(
            "frames",
            args.out_dir / "frames",
            artifact_type="understanding/visual-frames",
        )

    image_paths = [path.expanduser() for path in (args.image or [])]
    manifest = build_manifest(
        kind="understanding.visual_understand",
        inputs={
            "query": args.query,
            "images": [str(p) for p in (args.image or [])],
            "image_hashes": [hashlib.sha256(path.read_bytes()).hexdigest() for path in image_paths],
            "video": str(args.video) if args.video else None,
            "mode": args.mode,
            "model": args.model or MODEL_PRESETS[args.mode],
            "compare_model": args.compare_model,
            "detail": args.detail,
            "crop_aspect": args.crop_aspect,
            "crop_position": args.crop_position,
            "max_images": args.max_images,
            "out_dir": str(args.out_dir),
        },
        outputs=manifest_outputs,
        created=datetime.now(timezone.utc).isoformat(),
        schema_version=1,
    )
    write_manifest(manifest_path, manifest)
    # -------------------------------------------------------------------------

    print(text)
    return 0 if all(result["status"] == "ok" for result in results) else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Ask an OpenAI vision model about one image or a numbered contact sheet.",
        epilog="Tip: if you do not know which video frames to query, run boundary_candidates.py first to package transcript, scenes, shots, quality zones, and holding-screen refs into candidate frame sets.",
    )
    add = parser.add_argument
    add("--query", required=True, help="Question/instruction for the model.")
    add("--image", type=Path, action="append", help="Image path; repeat for multiple images.")
    add("--video", type=Path, help="Video path to sample frames from.")
    add("--at", action="append", help="Frame timestamp(s), comma-separated or repeated. Supports seconds, MM:SS, HH:MM:SS.")
    add_choice_arg(parser, "--mode", values=sorted(MODEL_PRESETS), default=DEFAULT_MODE, help="Model preset: fast is cheapest/default, best uses the strongest detail model.")
    add("--model", help="Explicit model override; bypasses --mode for the primary query.")
    add("--compare-model", action="append", default=[], help="Additional model to query with the same image/contact sheet.")
    add_choice_arg(parser, "--detail", values=("low", "high", "auto"), default="low")
    add("--cols", type=int, default=4)
    add("--tile-width", type=int, default=480)
    add("--max-images", type=int, default=DEFAULT_MAX_IMAGES)
    add("--label-prefix", default="Frame")
    add("--contact-sheet", type=Path, help="Optional output path for the generated contact sheet.")
    add("--crop-aspect", action="append", help="Create crop variants from a single source image/frame, e.g. 9:16 or 1:1. Repeat or comma-separate.")
    add("--crop-position", action="append", help="Crop alignment(s), e.g. left,center,right or top,center,bottom. Repeat or comma-separate.")
    add("--out-dir", type=Path, default=Path("runs/visual-understanding"))
    add("--out", type=Path, help="Optional JSON result path.")
    add("--response-schema", type=Path,
        help="Optional path to a JSON schema file. When provided, the model is constrained to emit JSON matching this schema (OpenAI Responses API text.format=json_schema). File may be either the raw schema or {name, schema, strict?}.")
    add("--env-file", type=Path)
    add("--max-output-tokens", type=int, default=700)
    add("--timeout", type=int, default=120)
    add("--force", action="store_true")
    add("--dry-run", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    def _run() -> int:
        return run(build_parser().parse_args(argv))

    return run_pack_main(
        "understanding.visual_understand",
        _run,
        argv=argv,
        recovery_command="check the inputs and retry; see --help for usage",
    )


if __name__ == "__main__":
    raise SystemExit(main())
