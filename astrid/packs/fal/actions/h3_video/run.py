"""Generate MiniMax H3 text or reference video through fal.ai."""

from __future__ import annotations

from astrid.core.pack.entrypoint import guard_canonical_entrypoint

guard_canonical_entrypoint("fal.h3_video")

import argparse
import json
import mimetypes
import shutil
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


ENDPOINTS = {
    "text-to-video": "minimax/h3/text-to-video",
    "reference-to-video": "minimax/h3/reference-to-video",
}
ALLOWED_ASPECT_RATIOS = {"adaptive", "21:9", "16:9", "4:3", "1:1", "3:4", "9:16"}
TEXT_ASPECT_RATIOS = ALLOWED_ASPECT_RATIOS - {"adaptive"}
ALLOWED_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}
ALLOWED_VIDEO_SUFFIXES = {".mp4", ".mov", ".webm", ".m4v"}
ALLOWED_AUDIO_SUFFIXES = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"}
MAX_PROMPT_CHARS = 2000
MAX_IMAGE_REFS = 9
MAX_VIDEO_REFS = 3
MAX_AUDIO_REFS = 3
MAX_TOTAL_REFS = 12
MAX_IMAGE_BYTES = 30 * 1024 * 1024
MAX_VIDEO_BYTES = 50 * 1024 * 1024
MAX_AUDIO_BYTES = 15 * 1024 * 1024


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stage_file(source: Path, destination: Path) -> dict[str, Any]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    return {
        "path": destination,
        "media_type": mimetypes.guess_type(destination.name)[0] or "application/octet-stream",
        "content_hash": f"sha256:{sha256_file(destination)}",
    }


def _read_and_stage_prompt(prompt_file: Path, out: Path) -> str:
    if not prompt_file.is_file():
        raise ValueError(f"prompt file not found: {prompt_file}")
    prompt = prompt_file.read_text(encoding="utf-8").strip()
    if not prompt:
        raise ValueError("prompt file is empty")
    staged = out / "inputs" / "prompt.txt"
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_text(f"{prompt}\n", encoding="utf-8")
    return prompt


def _validate_file(path: Path, *, suffixes: set[str], max_bytes: int, label: str) -> None:
    if not path.is_file():
        raise ValueError(f"{label} not found: {path}")
    if path.suffix.lower() not in suffixes:
        allowed = ", ".join(sorted(suffixes))
        raise ValueError(f"{label} must use one of: {allowed}")
    if path.stat().st_size >= max_bytes:
        raise ValueError(f"{label} exceeds the endpoint size limit")


def _validate_request(args: argparse.Namespace, prompt: str) -> None:
    if len(prompt) > MAX_PROMPT_CHARS:
        raise ValueError(
            f"MiniMax H3 accepts at most {MAX_PROMPT_CHARS} prompt characters; "
            f"the supplied exact prompt has {len(prompt)}"
        )
    if not 5 <= args.duration <= 15:
        raise ValueError("MiniMax H3 duration must be between 5 and 15 seconds")
    allowed_ratios = TEXT_ASPECT_RATIOS if args.mode == "text-to-video" else ALLOWED_ASPECT_RATIOS
    if args.aspect_ratio not in allowed_ratios:
        raise ValueError(
            f"aspect ratio {args.aspect_ratio!r} is invalid for {args.mode}; "
            f"allowed: {', '.join(sorted(allowed_ratios))}"
        )
    if not 60 <= args.timeout_seconds <= 7200:
        raise ValueError("timeout_seconds must be between 60 and 7200")


def _stage_references(
    args: argparse.Namespace,
    out: Path,
) -> tuple[list[Path], list[Path], list[Path], list[dict[str, Any]]]:
    images = [value.expanduser().resolve() for value in (args.image_ref or [])]
    videos = [value.expanduser().resolve() for value in (args.video_ref or [])]
    audios = [value.expanduser().resolve() for value in (args.audio_ref or [])]

    if args.mode == "text-to-video" and (images or videos or audios):
        raise ValueError("text-to-video does not accept reference files")
    if args.mode == "reference-to-video" and not (images or videos):
        raise ValueError("reference-to-video requires at least one image or video reference")
    if len(images) > MAX_IMAGE_REFS:
        raise ValueError(f"MiniMax H3 accepts at most {MAX_IMAGE_REFS} image references")
    if len(videos) > MAX_VIDEO_REFS:
        raise ValueError(f"MiniMax H3 accepts at most {MAX_VIDEO_REFS} video references")
    if len(audios) > MAX_AUDIO_REFS:
        raise ValueError(f"MiniMax H3 accepts at most {MAX_AUDIO_REFS} audio references")
    if len(images) + len(videos) + len(audios) > MAX_TOTAL_REFS:
        raise ValueError(f"MiniMax H3 accepts at most {MAX_TOTAL_REFS} total references")

    staged_images: list[Path] = []
    staged_videos: list[Path] = []
    staged_audios: list[Path] = []
    ordered: list[dict[str, Any]] = []
    groups = (
        (images, staged_images, ALLOWED_IMAGE_SUFFIXES, MAX_IMAGE_BYTES, "Image", "image"),
        (videos, staged_videos, ALLOWED_VIDEO_SUFFIXES, MAX_VIDEO_BYTES, "Video", "video"),
        (audios, staged_audios, ALLOWED_AUDIO_SUFFIXES, MAX_AUDIO_BYTES, "Audio", "audio"),
    )
    for sources, staged_group, suffixes, max_bytes, label, role in groups:
        for index, source in enumerate(sources, start=1):
            _validate_file(source, suffixes=suffixes, max_bytes=max_bytes, label=f"{label} {index}")
            destination = out / "inputs" / f"{role}{index}{source.suffix.lower()}"
            metadata = _stage_file(source, destination)
            staged_group.append(destination)
            ordered.append(
                {
                    "ordinal": len(ordered) + 1,
                    "role": f"{role}_reference",
                    "reference_label": f"{label} {index}",
                    "path": destination.relative_to(out).as_posix(),
                    "media_type": metadata["media_type"],
                    "content_hash": metadata["content_hash"],
                }
            )
    return staged_images, staged_videos, staged_audios, ordered


def _video_url(result: dict[str, Any]) -> str:
    video = result.get("video")
    if isinstance(video, dict) and isinstance(video.get("url"), str):
        return video["url"]
    if isinstance(video, str):
        return video
    raise ValueError("fal.ai result did not contain video.url")


def _download_video(client: HttpClient, url: str, destination: Path, timeout_seconds: int) -> None:
    if not url.startswith("https://"):
        raise ValueError("fal.ai returned a non-HTTPS output URL")
    destination.parent.mkdir(parents=True, exist_ok=True)
    write_bytes_atomic(destination, client.get_bytes(url, timeout=timeout_seconds))


def _write_run_manifest(
    *,
    out: Path,
    args: argparse.Namespace,
    prompt: str,
    ordered: list[dict[str, Any]],
    status: str,
    started_at: str,
    duration_ms: int,
    outputs: list[dict[str, Any]],
    error: str | None = None,
    request_id: str | None = None,
) -> dict[str, Any]:
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "kind": "fal.h3_video",
        "inputs": {
            "model": "minimax-h3",
            "mode": args.mode,
            "prompt": prompt,
            "prompt_capture": "exact",
            "prompt_characters": len(prompt),
            "duration": args.duration,
            "resolution": "2K",
            "aspect_ratio": args.aspect_ratio,
            "ordered_artifacts": ordered,
        },
        "outputs": outputs,
        "created": started_at,
        "status": status,
        "warnings": [],
        "provider_extension": {
            "endpoint": ENDPOINTS[args.mode],
            "duration_ms": duration_ms,
            "request_id": request_id,
            "estimated_cost_usd": round(args.duration * 0.26, 2) if status == "completed" else None,
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
    out = args.out.expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)
    prompt = ""
    ordered: list[dict[str, Any]] = []

    try:
        prompt_file = args.prompt_file.expanduser().resolve()
        prompt = _read_and_stage_prompt(prompt_file, out)
        _validate_request(args, prompt)
        staged_images, staged_videos, staged_audios, ordered = _stage_references(args, out)

        request_record = {
            "endpoint": ENDPOINTS[args.mode],
            "prompt": prompt,
            "prompt_capture": "exact",
            "duration": args.duration,
            "resolution": "2K",
            "aspect_ratio": args.aspect_ratio,
            "references": [item["path"] for item in ordered],
            "dry_run": args.dry_run,
        }
        write_json_atomic(out / "request.json", request_record)

        if args.dry_run:
            return 0, _write_run_manifest(
                out=out,
                args=args,
                prompt=prompt,
                ordered=ordered,
                status="draft",
                started_at=started_at,
                duration_ms=round((time.monotonic() - started_clock) * 1000),
                outputs=[],
            )

        if api_key is None:
            api_key = CredentialsScope.get_local("fal", env_file=args.env_file)
        if client is None:
            client = default_client()
        client.register_secret(api_key)

        payload: dict[str, Any] = {
            "prompt": prompt,
            "duration": args.duration,
            "resolution": "2K",
            "aspect_ratio": args.aspect_ratio,
        }
        if staged_images:
            payload["reference_image_urls"] = [uploader(client, path, api_key) for path in staged_images]
        if staged_videos:
            payload["reference_video_urls"] = [uploader(client, path, api_key) for path in staged_videos]
        if staged_audios:
            payload["reference_audio_urls"] = [uploader(client, path, api_key) for path in staged_audios]

        result = submitter(
            client,
            ENDPOINTS[args.mode],
            payload,
            api_key,
            max_wait_sec=args.timeout_seconds,
        )
        if not isinstance(result, dict):
            raise ValueError("fal.ai returned a non-object result")
        output_path = out / "outputs" / f"minimax-h3-{args.mode}.mp4"
        downloader(client, _video_url(result), output_path, args.timeout_seconds)
        outputs = [
            {
                "name": "generated_videos",
                "path": output_path.relative_to(out).as_posix(),
                "type": "file",
                "artifact_type": "video/clip",
                "media_type": "video/mp4",
                "ordinal": 0,
                "role": "result",
                "is_primary": True,
            }
        ]
        manifest = _write_run_manifest(
            out=out,
            args=args,
            prompt=prompt,
            ordered=ordered,
            status="completed",
            started_at=started_at,
            duration_ms=round((time.monotonic() - started_clock) * 1000),
            outputs=outputs,
            request_id=result.get("request_id"),
        )
        write_json_atomic(
            out / "result.json",
            {
                "schema_version": 1,
                "status": "completed",
                "endpoint": ENDPOINTS[args.mode],
                "output": outputs[0],
                "duration_ms": manifest["provider_extension"]["duration_ms"],
            },
        )
        return 0, manifest
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"
        manifest = _write_run_manifest(
            out=out,
            args=args,
            prompt=prompt,
            ordered=ordered,
            status="failed",
            started_at=started_at,
            duration_ms=round((time.monotonic() - started_clock) * 1000),
            outputs=[],
            error=message,
        )
        write_json_atomic(
            out / "result.json",
            {
                "schema_version": 1,
                "status": "failed",
                "endpoint": ENDPOINTS[args.mode],
                "error": message,
            },
        )
        return 1, manifest


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--mode", required=True, choices=tuple(ENDPOINTS))
    parser.add_argument("--prompt-file", required=True, type=Path)
    parser.add_argument("--image-ref", action="append", type=Path)
    parser.add_argument("--video-ref", action="append", type=Path)
    parser.add_argument("--audio-ref", action="append", type=Path)
    parser.add_argument("--duration", type=int, default=15)
    parser.add_argument("--aspect-ratio", default="16:9")
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--timeout-seconds", type=int, default=3600)
    parser.add_argument("--dry-run", action=argparse.BooleanOptionalAction, default=False)
    return parser


def main(argv: list[str] | None = None) -> int:
    def _run() -> int:
        args = _parser().parse_args(argv)
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
                manifest.get("error", "MiniMax H3 fal.ai request failed"),
                recovery_command=(
                    "inspect result.json and manifest.json; do not alter an exact "
                    "prompt or submit a paid approximation without explicit intent"
                ),
                state_snapshot={
                    "status": manifest["status"],
                    "manifest": str(args.out / "manifest.json"),
                },
            )
        return 0

    return run_pack_main("fal.h3_video", _run, argv=argv)


if __name__ == "__main__":
    raise SystemExit(main())
