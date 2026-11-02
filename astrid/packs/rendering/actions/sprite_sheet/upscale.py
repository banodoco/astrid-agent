"""FAL/OpenAI image edit and upscale helpers for sprite sheet processing."""

from __future__ import annotations

import argparse
import base64
import json
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from astrid.pack_api import AstridError
from astrid.pack_api import CredentialsScope
from astrid.packs.generation.actions.generate_image_openai.run import (
    DEFAULT_MODEL,
    _die,
)

from .png_io import (
    _png_dimensions,
    _read_rgba_png,
    scrub_fully_transparent_rgb,
)
from .sheet import _key_color_name

EDIT_API_URL = "https://api.openai.com/v1/images/edits"
DEFAULT_KEY_COLOR = "#ff00ff"
DEFAULT_FAL_UPSCALER = "fal-ai/clarity-upscaler"
FAL_KEY_NAMES = ("FAL_KEY", "FAL_API_KEY")

# Re-exported from generate_image_openai for convenience
# (also available via the generate_image_openai module directly)


def _run(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True)


def load_fal_key(env_file: Path | None = None) -> str:
    """Resolve the FAL key through the canonical resolver only (m4 Step 31).

    Frozen precedence: explicit option, shared Astrid env file, process
    environment, then one explicitly named fallback env file. Broad
    cwd/repository/workspace/home env-file scavenging is absent, so the key
    is never discovered from an implicit file.
    """
    try:
        return CredentialsScope.get_local("fal", env_file=env_file)
    except AstridError as exc:
        raise SystemExit(f"FAL key not found. {exc.cause}") from exc


def _download_url(url: str, output_path: Path, *, force: bool, timeout: int) -> None:
    if output_path.exists() and not force:
        _die(f"Output exists: {output_path} (use --force to overwrite)")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    request = Request(url, headers={"User-Agent": "Astrid/1.0"})
    try:
        with urlopen(request, timeout=timeout) as response:
            output_path.write_bytes(response.read())
    except (HTTPError, URLError, TimeoutError) as exc:
        _die(f"Failed to download FAL output: {exc}")


def _fal_image_url(result: dict[str, Any]) -> str:
    image = result.get("image")
    if isinstance(image, dict) and isinstance(image.get("url"), str):
        return image["url"]
    images = result.get("images")
    if isinstance(images, list) and images:
        first = images[0]
        if isinstance(first, dict) and isinstance(first.get("url"), str):
            return first["url"]
        if isinstance(first, str):
            return first
    _die("FAL upscaler result did not include an image URL")
    return ""


def _ai_upscale_prompt(args: argparse.Namespace) -> str:
    if args.ai_upscale_prompt:
        return args.ai_upscale_prompt
    return (
        f"High-quality AI upscaling for a transparent game sprite frame. Preserve the exact pose, silhouette, animation timing, "
        f"character identity, and clean 2D sprite style. Subject: {args.subject}. Animation: {args.animation}."
    )


def _merge_upscaled_rgb_with_source_alpha(
    source_frame: Path, upscaled_rgb: Path, output_path: Path, *, factor: float, force: bool
) -> None:
    width, height, _ = _read_rgba_png(source_frame)
    target_width = max(1, int(round(width * factor)))
    target_height = max(1, int(round(height * factor)))
    if output_path.exists() and not force:
        _die(f"Output exists: {output_path} (use --force to overwrite)")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    _run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y" if force else "-n",
            "-i",
            str(upscaled_rgb),
            "-i",
            str(source_frame),
            "-filter_complex",
            (
                f"[0:v]scale={target_width}:{target_height}:flags=lanczos,format=rgb24[rgb];"
                f"[1:v]alphaextract,scale={target_width}:{target_height}:flags=lanczos[alpha];"
                "[rgb][alpha]alphamerge,format=rgba[out]"
            ),
            "-map",
            "[out]",
            "-frames:v",
            "1",
            str(output_path),
        ]
    )
    scrub_fully_transparent_rgb(output_path)


def _scale_upscaled_image(
    source_frame: Path, upscaled_image: Path, output_path: Path, *, factor: float, force: bool
) -> None:
    width, height = _png_dimensions(source_frame)
    target_width = max(1, int(round(width * factor)))
    target_height = max(1, int(round(height * factor)))
    if output_path.exists() and not force:
        _die(f"Output exists: {output_path} (use --force to overwrite)")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    _run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y" if force else "-n",
            "-i",
            str(upscaled_image),
            "-vf",
            f"scale={target_width}:{target_height}:flags=lanczos,format=rgba",
            "-frames:v",
            "1",
            str(output_path),
        ]
    )


def ai_upscale_frames_with_fal(
    frames: list[str],
    out_dir: Path,
    *,
    args: argparse.Namespace,
    force: bool,
) -> tuple[list[str], list[dict[str, Any]]]:
    try:
        import fal_client
    except ImportError:
        _die(
            "fal-client is required for --ai-upscale-provider fal. Install requirements or run: pip install fal-client"
        )

    fal_key = load_fal_key(args.fal_env_file or args.env_file)
    client = fal_client.SyncClient(key=fal_key, default_timeout=float(args.fal_timeout))
    out_dir.mkdir(parents=True, exist_ok=True)
    raw_dir = out_dir / "fal_raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    prompt = _ai_upscale_prompt(args)
    outputs: list[str] = []
    reports: list[dict[str, Any]] = []
    for index, frame in enumerate(frames, start=1):
        source_frame = Path(frame)
        raw_output = raw_dir / f"frame_{index:03d}.png"
        final_output = out_dir / f"frame_{index:03d}.png"
        print(
            f"FAL upscaling frame {index}/{len(frames)} with {args.ai_upscale_model}",
            file=sys.stderr,
        )
        image_url = client.upload_file(source_frame)
        payload = {
            "image_url": image_url,
            "prompt": prompt,
            "upscale_factor": args.ai_upscale_factor,
            "negative_prompt": args.ai_upscale_negative_prompt,
            "creativity": args.ai_upscale_creativity,
            "resemblance": args.ai_upscale_resemblance,
            "guidance_scale": args.ai_upscale_guidance_scale,
            "num_inference_steps": args.ai_upscale_steps,
            "enable_safety_checker": True,
        }
        result = client.subscribe(
            args.ai_upscale_model,
            arguments=payload,
            with_logs=args.fal_logs,
            client_timeout=float(args.fal_timeout),
        )
        output_url = _fal_image_url(result)
        _download_url(output_url, raw_output, force=force, timeout=args.fal_timeout)
        if args.transparent:
            _merge_upscaled_rgb_with_source_alpha(
                source_frame,
                raw_output,
                final_output,
                factor=args.ai_upscale_factor,
                force=force,
            )
        else:
            _scale_upscaled_image(
                source_frame, raw_output, final_output, factor=args.ai_upscale_factor, force=force
            )
        outputs.append(str(final_output))
        reports.append(
            {
                "frame": index,
                "source": str(source_frame),
                "raw_output": str(raw_output),
                "output": str(final_output),
                "model": args.ai_upscale_model,
                "upscale_factor": args.ai_upscale_factor,
                "seed": result.get("seed") if isinstance(result, dict) else None,
            }
        )
    return outputs, reports


def _sprite_prompt(
    args: argparse.Namespace, layout: dict[str, Any], *, has_reference_image: bool = False
) -> str:
    frame_count = int(layout["frame_count"])
    capacity = int(layout["capacity"])
    extra = ""
    if capacity > frame_count:
        extra = f" Use only the first {frame_count} cells for animation frames; leave the final {capacity - frame_count} unused cell(s) blank white."
    safe_margin = int(layout.get("safe_margin") or 0)
    style = (
        args.style.strip()
        if args.style
        else "clean high-quality 2D game animation, crisp silhouette, consistent character design"
    )
    if args.transparent:
        background = (
            f"perfectly flat solid {_key_color_name(args.key_color)} chroma-key background. "
            "The background must be one uniform exact color with no shadows, gradients, texture, floor, glow, or lighting variation. "
            f"Do not use {_key_color_name(args.key_color)} anywhere in the character, props, outline, highlights, shadows, or effects."
        )
    else:
        background = args.background.strip() if args.background else "plain white background"
    lines = [
        "Create one complete animation sprite sheet.",
        f"Animation: {args.animation.strip()}",
        f"Subject: {args.subject.strip()}",
        f"Style: {style}",
        f"Canvas: {layout['sheet_width']}x{layout['sheet_height']} pixels.",
        f"Grid: {layout['cols']} columns by {layout['rows']} rows, {capacity} cells total.",
        f"Animation frames: exactly {frame_count} sequential frames.{extra}",
        f"Each frame cell is exactly {layout['frame_width']}x{layout['frame_height']} pixels.",
        f"Safe area: keep the entire character, all limbs, cape, backpack, motion arcs, and effects at least {safe_margin} pixels inside each cell boundary.",
        "Never let any body part cross, touch, or continue through a cell boundary. No partial heads, feet, hands, capes, or limbs at any cell edge.",
        "Frame order is left-to-right across each row, then top-to-bottom.",
        "Each cell must contain one sequential pose from the animation.",
        "Keep the subject centered inside each cell with consistent scale, camera, lighting, and proportions.",
        "Unless the animation explicitly requires locomotion or repositioning, keep the subject's base, feet, body anchor, or contact point pixel-registered in the same place across every frame. The torso/core/base must not bob, drift, float, shrink, grow, or shift up and down; only the parts named by the animation should move.",
        "Make the motion read like a usable sprite animation: frame-to-frame changes should be coherent increments of the requested action, with consistent spacing and no accidental camera movement. Use small controlled movements for subtle actions, and only use larger pose changes when the animation description clearly calls for them.",
        f"Background for every cell: {background}.",
        "Do not include text, labels, numbers, watermarks, UI, borders, grid lines, gutters, or frame separators in the final artwork.",
        "Do not merge frames together. Do not create a collage. Do not vary art style between frames.",
        "The provided guide image is only a layout template showing cell placement and safe areas; remove all guide lines from the final sprite sheet.",
    ]
    if has_reference_image:
        lines.insert(
            3,
            (
                "Use the provided reference image as the source of truth for the character identity, silhouette, "
                "palette, rendering style, markings, proportions, and distinctive details. Animate that exact "
                "character; do not redesign it, replace it, or invent a different character."
            ),
        )
        lines.append(
            "If multiple images are provided, the character reference controls identity and the layout guide controls only grid placement."
        )
    return "\n".join(lines)


def _multipart_field(name: str, value: str, boundary: str) -> bytes:
    return (
        f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'
    ).encode("utf-8")


def _image_content_type(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".jpg", ".jpeg"}:
        return "image/jpeg"
    if suffix == ".webp":
        return "image/webp"
    return "image/png"


def _multipart_file(name: str, path: Path, boundary: str, content_type: str | None = None) -> bytes:
    content_type = content_type or _image_content_type(path)
    header = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{name}"; filename="{path.name}"\r\n'
        f"Content-Type: {content_type}\r\n\r\n"
    ).encode("utf-8")
    return header + path.read_bytes() + b"\r\n"


def _call_image_edit_api(
    payload: dict[str, Any], image_paths: Path | list[Path], api_key: str, timeout: int
) -> dict[str, Any]:
    paths = [image_paths] if isinstance(image_paths, Path) else list(image_paths)
    if not paths:
        _die("At least one image is required for the image edit API")
    boundary = f"astrid-{uuid.uuid4().hex}"
    parts: list[bytes] = []
    for key, value in payload.items():
        if value is not None:
            parts.append(_multipart_field(key, str(value), boundary))
    for image_path in paths:
        parts.append(_multipart_file("image[]", image_path, boundary))
    parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    body = b"".join(parts)
    request = Request(
        EDIT_API_URL,
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        _die(f"OpenAI API error {exc.code}: {detail}")
    except URLError as exc:
        _die(f"Network error: {exc}")
    return {}


def _request_payload_for_image_model(
    args: argparse.Namespace, prompt: str, size: str
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": args.model,
        "prompt": prompt,
        "size": size,
        "quality": args.quality,
        "n": 1,
    }
    if args.model != DEFAULT_MODEL:
        payload["output_format"] = "png"
        payload["background"] = "opaque"
    return payload


def _write_first_image(response: dict[str, Any], out_path: Path, force: bool) -> None:
    data = response.get("data") or []
    if not data or not data[0].get("b64_json"):
        _die("OpenAI response did not include b64_json image data")
    if out_path.exists() and not force:
        _die(f"Output exists: {out_path} (use --force to overwrite)")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(base64.b64decode(data[0]["b64_json"]))
    print(f"Wrote {out_path}")


def upscale_frames(
    frames: list[str], out_dir: Path, *, factor: float, filter_name: str, force: bool
) -> list[str]:
    if factor <= 0:
        _die("--upscale-factor must be > 0")
    out_dir.mkdir(parents=True, exist_ok=True)
    outputs: list[str] = []
    for index, frame in enumerate(frames, start=1):
        out = out_dir / f"frame_{index:03d}.png"
        if out.exists() and not force:
            _die(f"Output exists: {out} (use --force to overwrite)")
        if abs(factor - 1.0) < 0.0001:
            out.write_bytes(Path(frame).read_bytes())
        else:
            _run(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y" if force else "-n",
                    "-i",
                    str(frame),
                    "-vf",
                    f"scale=round(iw*{factor})*1:round(ih*{factor})*1:flags={filter_name},format=rgba",
                    "-frames:v",
                    "1",
                    str(out),
                ]
            )
            scrub_fully_transparent_rgb(out)
        outputs.append(str(out))
    return outputs
