"""FAL-owned sprite upscaling implementation."""

from __future__ import annotations

import argparse
import struct
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from astrid.core.contracts.errors import AstridError
from astrid.core.contracts.die import pack_die as _die
from astrid.core.util.credentials_scope import CredentialsScope

DEFAULT_FAL_UPSCALER = "fal-ai/clarity-upscaler"
FAL_KEY_NAMES = ("FAL_KEY", "FAL_API_KEY")

def _run(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True)


def _png_dimensions(path: Path) -> tuple[int, int]:
    with path.open("rb") as stream:
        header = stream.read(24)
    if len(header) != 24 or header[:8] != b"\x89PNG\r\n\x1a\n" or header[12:16] != b"IHDR":
        _die(f"Not a valid PNG image: {path}")
    return struct.unpack(">II", header[16:24])

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


