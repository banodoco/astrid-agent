"""Run one Seedance 2.0 reference-media request through fal.ai."""

from __future__ import annotations

from astrid.core.pack.entrypoint import guard_canonical_entrypoint

guard_canonical_entrypoint("seedance_local.reference_video")

import argparse
import json
import mimetypes
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from astrid.core._shared.result_manifest import write_json_atomic, write_manifest
from astrid.core.contracts.errors import AstridError
from astrid.core.foundation.atomic_io import write_bytes_atomic
from astrid.core.foundation.hash import sha256_file
from astrid.core.pack.entrypoint import run_pack_main
from astrid.core.util.credentials_scope import CredentialsScope
from astrid.core.util.http import (
    HttpClient,
    default_client,
    fal_storage_upload,
    fal_submit_and_poll,
)


ENDPOINT = "bytedance/seedance-2.0/reference-to-video"
MAX_REFERENCE_BYTES = 50 * 1024 * 1024
MAX_IMAGE_BYTES = 30 * 1024 * 1024
ALLOWED_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}
ALLOWED_ASPECT_RATIOS = {"16:9", "9:16", "1:1", "4:3", "3:4", "21:9"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _probe_video(path: Path) -> dict[str, Any]:
    completed = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "format=duration:stream=width,height,codec_name",
            "-of",
            "json",
            str(path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode:
        raise ValueError(
            f"ffprobe could not read the reference video: {completed.stderr.strip()}"
        )
    payload = json.loads(completed.stdout)
    streams = payload.get("streams") or []
    if not streams:
        raise ValueError("reference file has no video stream")
    duration = float(payload.get("format", {}).get("duration", 0))
    return {
        "duration_seconds": duration,
        "width": int(streams[0].get("width", 0)),
        "height": int(streams[0].get("height", 0)),
        "codec": streams[0].get("codec_name"),
    }


def _stage_inputs(
    prompt_file: Path,
    video_ref: Path | None,
    image_refs: list[Path],
    out: Path,
) -> tuple[str, Path | None, list[Path], list[dict[str, Any]]]:
    prompt = prompt_file.read_text(encoding="utf-8").strip()
    if not prompt:
        raise ValueError("prompt file is empty")
    if video_ref is not None and "@Video1" not in prompt:
        raise ValueError("prompt must explicitly refer to the video as @Video1")
    for index in range(1, len(image_refs) + 1):
        if f"@Image{index}" not in prompt:
            raise ValueError(
                f"prompt must explicitly refer to image {index} as @Image{index}"
            )

    input_dir = out / "inputs"
    input_dir.mkdir(parents=True, exist_ok=True)
    staged_prompt = input_dir / "prompt.txt"
    staged_prompt.write_text(f"{prompt}\n", encoding="utf-8")
    ordered: list[dict[str, Any]] = []
    staged_images: list[Path] = []
    for index, image_ref in enumerate(image_refs, start=1):
        staged_image = input_dir / f"image{index}{image_ref.suffix.lower()}"
        shutil.copy2(image_ref, staged_image)
        staged_images.append(staged_image)
        ordered.append(
            {
                "ordinal": len(ordered) + 1,
                "role": "appearance_reference",
                "reference_label": f"@Image{index}",
                "path": staged_image.relative_to(out).as_posix(),
                "media_type": (
                    mimetypes.guess_type(staged_image.name)[0] or "image/png"
                ),
                "content_hash": f"sha256:{sha256_file(staged_image)}",
            }
        )
    staged_video: Path | None = None
    if video_ref is not None:
        staged_video = input_dir / f"video1{video_ref.suffix.lower()}"
        shutil.copy2(video_ref, staged_video)
        ordered.append(
            {
                "ordinal": len(ordered) + 1,
                "role": "motion_reference",
                "reference_label": "@Video1",
                "path": staged_video.relative_to(out).as_posix(),
                "media_type": (
                    mimetypes.guess_type(staged_video.name)[0] or "video/mp4"
                ),
                "content_hash": f"sha256:{sha256_file(staged_video)}",
            }
        )
    return prompt, staged_video, staged_images, ordered


def _download_video(
    client: HttpClient,
    url: str,
    destination: Path,
    timeout_seconds: int,
) -> None:
    if not url.startswith("https://"):
        raise ValueError("fal.ai returned a non-HTTPS output URL")
    destination.parent.mkdir(parents=True, exist_ok=True)
    write_bytes_atomic(destination, client.get_bytes(url, timeout=timeout_seconds))


def _video_url(result: dict[str, Any]) -> str:
    video = result.get("video")
    if isinstance(video, dict) and isinstance(video.get("url"), str):
        return video["url"]
    if isinstance(video, str):
        return video
    raise ValueError("fal.ai result did not contain video.url")


def _manifest(
    *,
    out: Path,
    started_at: str,
    status: str,
    prompt: str,
    ordered: list[dict[str, Any]],
    args: argparse.Namespace,
    probe: dict[str, Any] | None,
    outputs: list[dict[str, Any]],
    duration_ms: int,
    error: str | None = None,
) -> dict[str, Any]:
    inputs: dict[str, Any] = {
        "prompt": prompt,
        "prompt_capture": "exact",
        "mode": "reference_to_video",
        "model": "bytedance/seedance-2.0",
        "resolution": args.resolution,
        "duration": args.duration,
        "aspect_ratio": args.aspect_ratio,
        "generate_audio": args.generate_audio,
        "ordered_artifacts": ordered,
    }
    if args.seed is not None:
        inputs["seed"] = args.seed
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "kind": "fal.seedance_reference_video",
        "inputs": inputs,
        "outputs": outputs,
        "created": started_at,
        "warnings": [],
        "status": status,
        "provider_extension": {
            "endpoint": ENDPOINT,
            "reference_probe": probe,
            "duration_ms": duration_ms,
            "estimated_cost_usd": (
                round(
                    args.duration * (0.1814 if args.video_ref is not None else 0.3024),
                    4,
                )
                if args.resolution == "720p" and not args.dry_run
                else None
            ),
        },
    }
    if error:
        manifest["error"] = error
    return write_manifest(out / "manifest.json", manifest)


def execute(
    args: argparse.Namespace,
    *,
    client: HttpClient | None = None,
    api_key: str | None = None,
    uploader: Callable[[HttpClient, Path, str], str] = fal_storage_upload,
    submitter: Callable[..., dict[str, Any]] = fal_submit_and_poll,
    downloader: Callable[[HttpClient, str, Path, int], None] = _download_video,
) -> tuple[int, dict[str, Any]]:
    started_at = _now()
    started_clock = time.monotonic()
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)

    prompt_file = args.prompt_file.expanduser().resolve()
    video_ref = args.video_ref.expanduser().resolve() if args.video_ref else None
    image_refs = [
        value.expanduser().resolve()
        for value in (args.image_ref or [])
    ]
    if not prompt_file.is_file():
        raise ValueError(f"prompt file not found: {prompt_file}")
    if video_ref is None and not image_refs:
        raise ValueError("provide at least one --image-ref or --video-ref")
    if len(image_refs) > 9:
        raise ValueError("Seedance accepts at most 9 image references")
    for image_ref in image_refs:
        if not image_ref.is_file():
            raise ValueError(f"reference image not found: {image_ref}")
        if image_ref.suffix.lower() not in ALLOWED_IMAGE_SUFFIXES:
            raise ValueError("reference images must be JPEG, PNG, or WebP")
        if image_ref.stat().st_size >= MAX_IMAGE_BYTES:
            raise ValueError("each reference image must be smaller than 30 MB")

    probe: dict[str, Any] | None = None
    if video_ref is not None:
        if not video_ref.is_file():
            raise ValueError(f"reference video not found: {video_ref}")
        if video_ref.suffix.lower() not in {".mp4", ".mov"}:
            raise ValueError("reference video must be MP4 or MOV")
        if video_ref.stat().st_size >= MAX_REFERENCE_BYTES:
            raise ValueError("reference video must be smaller than 50 MB")
        probe = _probe_video(video_ref)
        if not 2 <= probe["duration_seconds"] <= 15.05:
            raise ValueError("reference video duration must be between 2 and 15 seconds")
        if not 480 <= probe["height"] <= 720:
            raise ValueError("reference video height must be between 480p and 720p")

    prompt, staged_video, staged_images, ordered = _stage_inputs(
        prompt_file,
        video_ref,
        image_refs,
        out,
    )
    request_record: dict[str, Any] = {
        "endpoint": ENDPOINT,
        "prompt": prompt,
        "image_references": [
            item["path"] for item in ordered if item["role"] == "appearance_reference"
        ],
        "video_reference": (
            next(
                (
                    item["path"]
                    for item in ordered
                    if item["role"] == "motion_reference"
                ),
                None,
            )
        ),
        "resolution": args.resolution,
        "duration": args.duration,
        "aspect_ratio": args.aspect_ratio,
        "generate_audio": args.generate_audio,
        "seed": args.seed,
        "dry_run": args.dry_run,
    }
    write_json_atomic(out / "request.json", request_record)

    if args.dry_run:
        manifest = _manifest(
            out=out,
            started_at=started_at,
            status="draft",
            prompt=prompt,
            ordered=ordered,
            args=args,
            probe=probe,
            outputs=[],
            duration_ms=round((time.monotonic() - started_clock) * 1000),
        )
        return 0, manifest

    if api_key is None:
        api_key = CredentialsScope.get("fal", env_file=args.env_file)
    if client is None:
        client = default_client()
    client.register_secret(api_key)

    payload: dict[str, Any] = {
        "prompt": prompt,
        "resolution": args.resolution,
        "duration": str(args.duration),
        "aspect_ratio": args.aspect_ratio,
        "generate_audio": args.generate_audio,
    }
    if staged_images:
        payload["image_urls"] = [
            uploader(client, staged_image, api_key)
            for staged_image in staged_images
        ]
    if staged_video is not None:
        payload["video_urls"] = [uploader(client, staged_video, api_key)]
    if args.seed is not None:
        payload["seed"] = args.seed

    try:
        result = submitter(
            client,
            ENDPOINT,
            payload,
            api_key,
            max_wait_sec=args.timeout_seconds,
        )
        if not isinstance(result, dict):
            raise ValueError("fal.ai returned a non-object result")
        output_name = (
            "seedance-2-reference-video.mp4"
            if staged_video is not None and not staged_images
            else "seedance-2-reference-media.mp4"
        )
        output_path = out / "outputs" / output_name
        downloader(client, _video_url(result), output_path, args.timeout_seconds)
        outputs = [{"path": output_path.relative_to(out).as_posix(), "type": "file", "media_type": "video/mp4"}]
        manifest = _manifest(
            out=out,
            started_at=started_at,
            status="completed",
            prompt=prompt,
            ordered=ordered,
            args=args,
            probe=probe,
            outputs=outputs,
            duration_ms=round((time.monotonic() - started_clock) * 1000),
        )
        write_json_atomic(
            out / "result.json",
            {
                "schema_version": 1,
                "status": "completed",
                "endpoint": ENDPOINT,
                "output": outputs[0],
                "duration_ms": manifest["provider_extension"]["duration_ms"],
            },
        )
        return 0, manifest
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"
        manifest = _manifest(
            out=out,
            started_at=started_at,
            status="failed",
            prompt=prompt,
            ordered=ordered,
            args=args,
            probe=probe,
            outputs=[],
            duration_ms=round((time.monotonic() - started_clock) * 1000),
            error=message,
        )
        write_json_atomic(
            out / "result.json",
            {
                "schema_version": 1,
                "status": "failed",
                "endpoint": ENDPOINT,
                "error": message,
            },
        )
        return 1, manifest


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--prompt-file", required=True, type=Path)
    parser.add_argument("--video-ref", type=Path)
    parser.add_argument(
        "--image-ref",
        action="append",
        type=Path,
        help="Ordered image reference; repeat up to nine times.",
    )
    parser.add_argument("--resolution", choices=("720p", "1080p"), default="720p")
    parser.add_argument("--duration", type=int, default=15)
    parser.add_argument("--aspect-ratio", default="16:9")
    parser.add_argument("--seed", type=int)
    parser.add_argument(
        "--generate-audio",
        action=argparse.BooleanOptionalAction,
        default=False,
    )
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--timeout-seconds", type=int, default=3600)
    parser.add_argument("--dry-run", action=argparse.BooleanOptionalAction, default=False)
    return parser


def _validate_args(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    if not 4 <= args.duration <= 15:
        parser.error("--duration must be between 4 and 15")
    if args.aspect_ratio not in ALLOWED_ASPECT_RATIOS:
        parser.error(
            "--aspect-ratio must be one of " + ", ".join(sorted(ALLOWED_ASPECT_RATIOS))
        )
    if not 60 <= args.timeout_seconds <= 7200:
        parser.error("--timeout-seconds must be between 60 and 7200")


def main(argv: list[str] | None = None) -> int:
    def _run() -> int:
        parser = _parser()
        args = parser.parse_args(argv)
        _validate_args(args, parser)
        returncode, manifest = execute(args)
        print(
            json.dumps(
                {
                    "status": manifest["status"],
                    "manifest": str(args.out / "manifest.json"),
                    "outputs": len(manifest["outputs"]),
                },
                sort_keys=True,
            )
        )
        if returncode:
            raise AstridError(
                manifest.get("error", "Seedance fal.ai request failed"),
                recovery_command=(
                    "inspect result.json and manifest.json before explicitly "
                    "submitting a new paid request"
                ),
                state_snapshot={
                    "status": manifest["status"],
                    "manifest": str(args.out / "manifest.json"),
                },
            )
        return 0

    return run_pack_main("seedance_local.reference_video", _run, argv=argv)


if __name__ == "__main__":
    raise SystemExit(main())
