#!/usr/bin/env python3
"""Animate Image: restyle the video's first frame in the style of a reference image, then drive wan-animate."""


from __future__ import annotations

from astrid.core.contracts.errors import AstridError
from astrid.core.pack.entrypoint import guard_canonical_entrypoint

guard_canonical_entrypoint('video_editing.animate_image')
import argparse
import base64
import json
import math
import mimetypes
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Sequence
from urllib.error import HTTPError, URLError

from astrid.core.cli_choices import (
    AstridArgumentError,
    RecoverableArgumentParser,
    add_choice_arg,
)
from astrid.core.util.http import FAL_QUEUE_URL, default_client
from astrid.packs.video_editing.shared.result_receipts import write_action_receipt
from astrid.core.util.credentials_scope import CredentialsScope

FAL_EDIT_MODEL_ID = "openai/gpt-image-2/edit"
FAL_ANIMATE_MODEL_ID = "fal-ai/wan/v2.2-14b/animate/move"

DEFAULT_QUALITY = "high"
DEFAULT_OUTPUT_FORMAT = "png"
DEFAULT_RESOLUTION = "720p"
DEFAULT_STEPS = 20
DEFAULT_GUIDANCE = 1.0
DEFAULT_SHIFT = 5.0
DEFAULT_VIDEO_QUALITY = "high"

VALID_RESOLUTIONS = ("480p", "580p", "720p")
VALID_VIDEO_QUALITIES = ("low", "medium", "high", "maximum")

GPT_IMAGE_2_MIN_PIXELS = 655_360
GPT_IMAGE_2_MAX_PIXELS = 8_294_400
GPT_IMAGE_2_MAX_EDGE = 3840
GPT_IMAGE_2_MAX_RATIO = 3.0
GPT_IMAGE_2_MIN_EDGE = 256

BASE_PROMPT = (
    "Replace the person in image 1 with the exact character shown in image 2. "
    "The output must depict the character from image 2 — same face, same eyes "
    "(including any unusual or stylized features like dreaded or distorted eyes), "
    "same hairstyle, same skin tone, same clothing and accessories, same overall "
    "identity. Do not blend the two faces; fully replace. "
    "Restyle the entire scene — background, environment, props, lighting, colour "
    "palette, materials, and rendering technique — to match the world and art "
    "style of image 2. The whole frame should feel like it lives inside image 2's "
    "universe. "
    "Preserve image 1's camera angle, framing, subject pose, gesture, body "
    "position, and overall composition exactly; reinterpret the rest. "
    "Critical: the character must face the same direction as the subject in "
    "image 1 — match head orientation, gaze direction, body facing, and shoulder "
    "angle precisely. If the subject in image 1 faces left, the character faces "
    "left; if forward, forward; if three-quarter, the same three-quarter angle. "
    "Do not mirror, flip, or rotate the facing direction."
)


def _load_env_var(name: str, env_file: Path | None) -> str:
    scope = name.lower().removesuffix("_api_key").removesuffix("_key")
    return CredentialsScope.get_local(scope, env_file=env_file)


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _data_uri_for(path: Path, *, override_mime: str | None = None) -> str:
    raw = path.read_bytes()
    mime = override_mime or mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"


def _probe_video_dimensions(video: Path) -> tuple[int, int]:
    if not shutil.which("ffprobe"):
        raise AstridError(
            "ffprobe not found on PATH (install ffmpeg).",
            recovery_command="install ffmpeg and retry animate_image",
        )
    proc = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height",
            "-of", "json",
            str(video),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    streams = json.loads(proc.stdout).get("streams") or []
    if not streams:
        raise AstridError(
            f"ffprobe found no video stream in {video}",
            recovery_command="pass a valid --ref-video file",
            state_snapshot={"ref_video": video},
        )
    return int(streams[0]["width"]), int(streams[0]["height"])


def _extract_first_frame(video: Path, dest: Path) -> None:
    if not shutil.which("ffmpeg"):
        raise AstridError(
            "ffmpeg not found on PATH.",
            recovery_command="install ffmpeg and retry animate_image",
        )
    dest.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", str(video),
            "-frames:v", "1",
            "-q:v", "2",
            str(dest),
        ],
        check=True,
    )
    if not dest.is_file() or dest.stat().st_size == 0:
        raise AstridError(
            f"first-frame extraction produced no output: {dest}",
            recovery_command="pass a valid --ref-video file",
            state_snapshot={"ref_video": video, "dest": dest},
        )


def _snap_to_gpt_image_2_size(width: int, height: int) -> tuple[int, int]:
    """Snap (w,h) to the nearest gpt-image-2-legal size (multiples of 16, edge/pixel/ratio bounds)."""

    def _round16(x: float) -> int:
        return max(16, int(round(x / 16.0)) * 16)

    aspect = width / height if height else 1.0
    aspect = max(1.0 / GPT_IMAGE_2_MAX_RATIO, min(GPT_IMAGE_2_MAX_RATIO, aspect))

    w, h = _round16(width), _round16(height)
    w = max(GPT_IMAGE_2_MIN_EDGE, min(GPT_IMAGE_2_MAX_EDGE, w))
    h = max(GPT_IMAGE_2_MIN_EDGE, min(GPT_IMAGE_2_MAX_EDGE, h))

    px = w * h
    if px < GPT_IMAGE_2_MIN_PIXELS:
        scale = math.sqrt(GPT_IMAGE_2_MIN_PIXELS / px)
        w, h = _round16(w * scale), _round16(h * scale)
    px = w * h
    if px > GPT_IMAGE_2_MAX_PIXELS:
        scale = math.sqrt(GPT_IMAGE_2_MAX_PIXELS / px)
        w, h = _round16(w * scale), _round16(h * scale)

    w = max(GPT_IMAGE_2_MIN_EDGE, min(GPT_IMAGE_2_MAX_EDGE, w))
    h = max(GPT_IMAGE_2_MIN_EDGE, min(GPT_IMAGE_2_MAX_EDGE, h))
    return w, h


def _submit_fal(model_id: str, payload: dict[str, Any], api_key: str) -> dict[str, Any]:
    url = f"{FAL_QUEUE_URL}/{model_id}"
    headers = {"authorization": f"Key {api_key}"}
    try:
        return default_client().post_json(url, payload, headers=headers, timeout=180)
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        raise AstridError(
            f"fal submit failed ({exc.code}) for {model_id}: {detail}",
            recovery_command="check FAL_KEY and retry animate_image",
            state_snapshot={"model_id": model_id},
        ) from exc
    except URLError as exc:
        raise AstridError(
            f"fal submit failed for {model_id}: {exc}",
            recovery_command="check network connectivity and retry animate_image",
            state_snapshot={"model_id": model_id},
        ) from exc
    except SystemExit as exc:
        raise AstridError(
            f"fal submit failed for {model_id}: {exc}",
            recovery_command="check FAL_KEY and retry animate_image",
            state_snapshot={"model_id": model_id},
        ) from exc


def _poll_fal_result(
    submission: dict[str, Any],
    api_key: str,
    *,
    max_wait_sec: int,
) -> dict[str, Any]:
    status_url = submission.get("status_url")
    response_url = submission.get("response_url")
    if not status_url or not response_url:
        raise AstridError(
            f"fal submission missing status_url/response_url: {json.dumps(submission)}",
            recovery_command="retry animate_image",
            state_snapshot={"submission": submission},
        )
    headers = {"authorization": f"Key {api_key}"}
    try:
        result = default_client().poll_until(
            status_url,
            response_url,
            headers=headers,
            max_wait_sec=max_wait_sec,
        )
    except SystemExit as exc:
        raise AstridError(
            f"fal job failed: {exc}",
            recovery_command="retry animate_image or inspect fal job status",
            state_snapshot={"status_url": status_url, "response_url": response_url},
        ) from exc
    if "request_id" not in result and "request_id" in submission:
        result["request_id"] = submission["request_id"]
    return result


def _save_first_image(result: dict[str, Any], dest: Path) -> dict[str, Any]:
    images = result.get("images") or []
    if not images:
        raise AstridError(
            f"fal result had no images: {str(result)[:300]}",
            recovery_command="retry animate_image",
        )
    first = images[0]
    url = first.get("url")
    if not url:
        raise AstridError(
            f"fal image entry missing url: {first}",
            recovery_command="retry animate_image",
        )
    try:
        raw = default_client().get_bytes(url, timeout=180)
    except SystemExit as exc:
        raise AstridError(
            f"failed to download fal image: {exc}",
            recovery_command="retry animate_image",
            state_snapshot={"url": url},
        ) from exc
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(raw)
    return {
        "path": str(dest),
        "source_url": url,
        "width": first.get("width"),
        "height": first.get("height"),
        "content_type": first.get("content_type"),
        "bytes": len(raw),
    }


def _save_video(result: dict[str, Any], dest: Path) -> dict[str, Any]:
    video = result.get("video") or {}
    url = video.get("url") if isinstance(video, dict) else None
    if not url:
        raise AstridError(
            f"fal result had no video url: {str(result)[:300]}",
            recovery_command="retry animate_image",
        )
    try:
        raw = default_client().get_bytes(url, timeout=600)
    except SystemExit as exc:
        raise AstridError(
            f"failed to download fal video: {exc}",
            recovery_command="retry animate_image",
            state_snapshot={"url": url},
        ) from exc
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(raw)
    return {
        "path": str(dest),
        "source_url": url,
        "content_type": video.get("content_type"),
        "file_name": video.get("file_name"),
        "bytes": len(raw),
    }


def _placeholder_image(dest: Path, label: str, size: tuple[int, int]) -> dict[str, Any]:
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        dest.write_bytes(b"")
        return {"path": str(dest), "placeholder": True, "reason": "Pillow unavailable"}
    image = Image.new("RGB", size, (24, 24, 28))
    draw = ImageDraw.Draw(image)
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial Bold.ttf", 28)
    except OSError:
        font = ImageFont.load_default()
    draw.text((24, 24), "[dry-run]", fill=(220, 220, 220), font=font)
    draw.text((24, 72), label[:60], fill=(180, 180, 180), font=font)
    image.save(dest)
    return {"path": str(dest), "placeholder": True, "reason": "dry-run", "width": size[0], "height": size[1]}


def call_gpt_image_edit(
    *,
    prompt: str,
    image_paths: list[Path],
    api_key: str,
    width: int,
    height: int,
    quality: str,
    output_format: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    payload = {
        "prompt": prompt,
        "image_urls": [_data_uri_for(p) for p in image_paths],
        "image_size": {"width": width, "height": height},
        "quality": quality,
        "num_images": 1,
        "output_format": "jpeg" if output_format == "jpg" else output_format,
    }
    submission = _submit_fal(FAL_EDIT_MODEL_ID, payload, api_key)
    result = _poll_fal_result(submission, api_key, max_wait_sec=600)
    return submission, result


def call_wan_animate_move(
    *,
    image_path: Path,
    video_path: Path,
    api_key: str,
    resolution: str,
    num_inference_steps: int,
    guidance_scale: float,
    shift: float,
    video_quality: str,
    use_turbo: bool,
    seed: int | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    payload: dict[str, Any] = {
        "image_url": _data_uri_for(image_path),
        "video_url": _data_uri_for(video_path),
        "resolution": resolution,
        "num_inference_steps": num_inference_steps,
        "guidance_scale": guidance_scale,
        "shift": shift,
        "video_quality": video_quality,
        "use_turbo": use_turbo,
    }
    if seed is not None:
        payload["seed"] = seed
    submission = _submit_fal(FAL_ANIMATE_MODEL_ID, payload, api_key)
    result = _poll_fal_result(submission, api_key, max_wait_sec=1800)
    return submission, result


def build_parser() -> argparse.ArgumentParser:
    p = RecoverableArgumentParser(
        description=(
            "Restyle the first frame of a video to match a style reference image "
            "via openai/gpt-image-2/edit on fal, then animate that styled frame "
            "with fal-ai/wan/v2.2-14b/animate/move using the original video as the driver."
        ),
    )
    p.add_argument("--style-image", dest="style_image", type=Path, required=True, help="Style reference image (the look to adopt).")
    p.add_argument("--ref-video", dest="ref_video", type=Path, required=True, help="Driver video; first frame is the composition target.")
    p.add_argument("--out", type=Path, required=True, help="Output directory.")
    p.add_argument("--prompt", default=None, help="Extra direction appended to the built-in style-transfer prompt. Use --replace-prompt to override the whole thing.")
    p.add_argument("--replace-prompt", default=None, help="Replace the entire gpt-image-2 prompt (skips the built-in identity/composition/scene rules).")
    add_choice_arg(p, "--quality", values=("low", "medium", "high", "auto"), default=DEFAULT_QUALITY, help=f"gpt-image-2 quality (default {DEFAULT_QUALITY}).")
    add_choice_arg(p, "--output-format", values=("png", "jpeg", "webp"), default=DEFAULT_OUTPUT_FORMAT, help="Generated image format.")
    add_choice_arg(p, "--resolution", values=VALID_RESOLUTIONS, default=DEFAULT_RESOLUTION, help=f"Wan Animate output resolution (default {DEFAULT_RESOLUTION}).")
    p.add_argument("--num-inference-steps", type=int, default=DEFAULT_STEPS, help=f"Wan Animate steps (default {DEFAULT_STEPS}).")
    p.add_argument("--guidance-scale", type=float, default=DEFAULT_GUIDANCE, help=f"Wan Animate guidance scale (default {DEFAULT_GUIDANCE}).")
    p.add_argument("--shift", type=float, default=DEFAULT_SHIFT, help=f"Wan Animate shift, 1.0..10.0 (default {DEFAULT_SHIFT}).")
    add_choice_arg(p, "--video-quality", values=VALID_VIDEO_QUALITIES, default=DEFAULT_VIDEO_QUALITY, help=f"Wan Animate write quality (default {DEFAULT_VIDEO_QUALITY}).")
    p.add_argument("--use-turbo", action="store_true", help="Enable Wan Animate turbo path.")
    p.add_argument("--seed", type=int, default=None, help="Wan Animate seed.")
    p.add_argument("--env-file", type=Path, help="Env file with FAL_KEY.")
    p.add_argument("--dry-run", action="store_true", help="Plan only; skip API calls.")
    p.add_argument("--skip-generate", action="store_true", help="Skip stage 1; use --use-image as the character.")
    p.add_argument("--use-image", type=Path, help="When --skip-generate is set, use this image directly.")
    p.add_argument("--skip-animate", action="store_true", help="Run stage 1 only; do not call wan-animate.")
    return p


def _validate_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if not args.style_image.is_file():
        raise AstridError(
            f"--style-image path does not exist: {args.style_image}",
            recovery_command="pass an existing image path to --style-image",
            state_snapshot={"style_image": args.style_image},
        )
    if not args.ref_video.is_file():
        raise AstridError(
            f"--ref-video path does not exist: {args.ref_video}",
            recovery_command="pass an existing video path to --ref-video",
            state_snapshot={"ref_video": args.ref_video},
        )
    if args.skip_generate and not args.use_image:
        raise AstridError(
            "--skip-generate requires --use-image PATH",
            recovery_command="add --use-image <path> or remove --skip-generate",
        )
    if args.use_image and not args.use_image.is_file():
        raise AstridError(
            f"--use-image path does not exist: {args.use_image}",
            recovery_command="pass an existing image path to --use-image",
            state_snapshot={"use_image": args.use_image},
        )
    if args.shift < 1.0 or args.shift > 10.0:
        raise AstridError(
            "--shift must be in 1.0..10.0",
            recovery_command="set --shift to a value from 1.0 to 10.0",
            valid_options=["1.0..10.0"],
        )
    if args.num_inference_steps < 1:
        raise AstridError(
            "--num-inference-steps must be >= 1",
            recovery_command="set --num-inference-steps to a positive integer",
        )


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except AstridArgumentError as exc:
        raise AstridError(
            str(exc),
            valid_options=exc.valid_options,
            recovery_command=(
                "Correct the typed inputs and re-invoke video_editing.animate_image "
                "through the public action API; see docs/SKILL.md"
            ),
        ) from exc
    except SystemExit as exc:
        if int(exc.code or 0) == 0:
            return 0
        raise AstridError(
            "animate_image: invalid arguments",
            recovery_command=(
                "Correct the typed inputs and re-invoke video_editing.animate_image "
                "through the public action API; see docs/SKILL.md"
            ),
        ) from exc
    _validate_args(parser, args)

    out_root = args.out.expanduser().resolve()
    out_root.mkdir(parents=True, exist_ok=True)

    if args.replace_prompt:
        effective_prompt = args.replace_prompt
    elif args.prompt:
        effective_prompt = f"{BASE_PROMPT}\n\nAdditional direction: {args.prompt}"
    else:
        effective_prompt = BASE_PROMPT

    first_frame_path = out_root / "first_frame.png"
    _extract_first_frame(args.ref_video, first_frame_path)
    src_w, src_h = _probe_video_dimensions(args.ref_video)
    target_w, target_h = _snap_to_gpt_image_2_size(src_w, src_h)

    image_ext = "jpg" if args.output_format == "jpeg" else args.output_format
    image_path = out_root / f"generated.{image_ext}"
    animation_path = out_root / "animation.mp4"

    plan = {
        "tool": "animate_image",
        "version": 2,
        "mode": "dry-run" if args.dry_run else "run",
        "style_image": str(args.style_image.resolve()),
        "ref_video": str(args.ref_video.resolve()),
        "first_frame": str(first_frame_path),
        "video_dimensions": {"width": src_w, "height": src_h},
        "gpt_image_2_size": {"width": target_w, "height": target_h},
        "prompt": effective_prompt,
        "user_prompt_extra": args.prompt,
        "replaced_prompt": bool(args.replace_prompt),
        "quality": args.quality,
        "output_format": args.output_format,
        "fal_edit_model_id": FAL_EDIT_MODEL_ID,
        "fal_animate_model_id": FAL_ANIMATE_MODEL_ID,
        "wan_animate": {
            "resolution": args.resolution,
            "num_inference_steps": args.num_inference_steps,
            "guidance_scale": args.guidance_scale,
            "shift": args.shift,
            "video_quality": args.video_quality,
            "use_turbo": args.use_turbo,
            "seed": args.seed,
        },
        "skip_generate": args.skip_generate,
        "use_image": str(args.use_image.resolve()) if args.use_image else None,
        "out": str(out_root),
    }
    write_json(out_root / "plan.json", plan)
    print(f"wrote_first_frame={first_frame_path} ({src_w}x{src_h})")
    print(f"gpt_image_2_size={target_w}x{target_h}")

    fal_key: str | None = None
    if not args.dry_run:
        fal_key = _load_env_var("FAL_KEY", args.env_file)

    if args.skip_generate:
        src = args.use_image.expanduser().resolve()
        image_path = out_root / f"generated{src.suffix or '.png'}"
        image_path.write_bytes(src.read_bytes())
        generated = {
            "path": str(image_path),
            "source_url": None,
            "bytes": image_path.stat().st_size,
            "skipped_generate": True,
            "from": str(src),
        }
    elif args.dry_run:
        generated = _placeholder_image(image_path, "animate_image: stage 1 (gpt-image-2)", (target_w, target_h))
    else:
        assert fal_key is not None
        submission, result = call_gpt_image_edit(
            prompt=effective_prompt,
            image_paths=[first_frame_path, args.style_image],
            api_key=fal_key,
            width=target_w,
            height=target_h,
            quality=args.quality,
            output_format=args.output_format,
        )
        generated = _save_first_image(result, image_path)
        generated["request_id"] = submission.get("request_id")
        print(f"wrote_generated_image={generated['path']}")

    if args.dry_run:
        animation = {
            "path": None,
            "placeholder": True,
            "reason": "dry-run",
        }
    elif args.skip_animate:
        animation = {
            "path": None,
            "skipped": True,
            "reason": "--skip-animate",
        }
        print("skipped_animation=--skip-animate")
    else:
        assert fal_key is not None
        submission, result = call_wan_animate_move(
            image_path=Path(generated["path"]),
            video_path=args.ref_video,
            api_key=fal_key,
            resolution=args.resolution,
            num_inference_steps=args.num_inference_steps,
            guidance_scale=args.guidance_scale,
            shift=args.shift,
            video_quality=args.video_quality,
            use_turbo=args.use_turbo,
            seed=args.seed,
        )
        animation = _save_video(result, animation_path)
        animation["request_id"] = submission.get("request_id")
        animation["seed"] = result.get("seed")
        animation["prompt"] = result.get("prompt")
        print(f"wrote_animation={animation['path']}")

    manifest = {
        "version": 2,
        "mode": plan["mode"],
        "style_image": plan["style_image"],
        "ref_video": plan["ref_video"],
        "first_frame": plan["first_frame"],
        "video_dimensions": plan["video_dimensions"],
        "prompt": effective_prompt,
        "stage1": {
            "fal_model_id": FAL_EDIT_MODEL_ID,
            "image_size": plan["gpt_image_2_size"],
            "quality": args.quality,
            "output_format": args.output_format,
            "skipped": args.skip_generate,
            "image_inputs": [plan["first_frame"], plan["style_image"]],
            "image": generated,
        },
        "stage2": {
            "fal_model_id": FAL_ANIMATE_MODEL_ID,
            **plan["wan_animate"],
            "animation": animation,
        },
    }
    animation_is_concrete = bool(animation.get("path")) and Path(str(animation["path"])).is_file()
    receipt_outputs = {
        "plan": "plan.json",
        "first_frame": "first_frame.png",
        "generated_image": image_path.resolve().relative_to(out_root).as_posix(),
    }
    if animation_is_concrete:
        receipt_outputs["animation"] = Path(str(animation["path"])).resolve().relative_to(out_root).as_posix()
    write_action_receipt(
        out_root,
        kind="video_editing.animate_image",
        inputs={
            "style_image": str(args.style_image),
            "ref_video": str(args.ref_video),
            "use_image": str(args.use_image) if args.use_image else None,
        },
        output_paths=receipt_outputs,
        primary_port="animation" if animation_is_concrete else "generated_image",
        extra=manifest,
    )
    print(f"wrote_manifest={out_root / 'manifest.json'}")
    return 0


if __name__ == "__main__":
    from astrid.core.pack.entrypoint import run_pack_main

    raise SystemExit(
        run_pack_main(
            "video_editing.animate_image",
            lambda: main(),
            argv=list(os.sys.argv[1:]),
            recovery_command=(
                "Correct the typed inputs and re-invoke video_editing.animate_image "
                "through the public action API; see docs/SKILL.md"
            ),
        )
    )
