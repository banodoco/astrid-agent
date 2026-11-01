#!/usr/bin/env python3
"""Score one video clip with Foley audio via fal.ai hunyuan-video-foley."""


from __future__ import annotations

from astrid.core.contracts.errors import AstridError
from astrid.core.pack.entrypoint import guard_canonical_entrypoint

guard_canonical_entrypoint('fal.fal_foley')
import argparse
import base64
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from astrid.core._shared.result_manifest import build_manifest, write_manifest
from astrid.core.util.credentials_scope import CredentialsScope
from astrid.core.util.http import (
    FAL_QUEUE_URL,
    HttpClient,
    default_client,
    fal_submit_and_poll,
)

FAL_MODEL_ID = "fal-ai/hunyuan-video-foley"


def _data_uri_for_video(path: Path) -> str:
    suffix = path.suffix.lower().lstrip(".")
    mime = {
        "mp4": "video/mp4",
        "mov": "video/quicktime",
        "webm": "video/webm",
        "m4v": "video/x-m4v",
        "gif": "image/gif",
    }.get(suffix, "video/mp4")
    return f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode("ascii")


def _submit(client: HttpClient, payload: dict[str, Any], api_key: str) -> dict[str, Any]:
    """Submit a Foley job via HttpClient."""
    url = f"{FAL_QUEUE_URL}/{FAL_MODEL_ID}"
    headers = {"authorization": f"Key {api_key}"}
    return client.post_json(url, payload, headers=headers, timeout=180)


def _save_audio(
    client: HttpClient, result: dict[str, Any], dest: Path
) -> dict[str, Any]:
    # Hunyuan-Foley returns { "audio": { "url": ..., "content_type": ..., "file_name": ... } }
    # or possibly a "video" key with embedded audio. Probe both.
    audio = result.get("audio") or result.get("output_audio")
    if not audio and isinstance(result.get("video"), dict):
        audio = result["video"]
    if not audio or not audio.get("url"):
        raise SystemExit(f"fal foley result had no audio url: {str(result)[:400]}")
    url = audio["url"]
    raw = client.get_bytes(url, timeout=300)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(raw)
    return {
        "path": str(dest),
        "source_url": url,
        "content_type": audio.get("content_type"),
        "file_name": audio.get("file_name"),
        "bytes": len(raw),
    }


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Score one video clip with fal hunyuan-video-foley."
    )
    p.add_argument("--clip", type=Path, required=True, help="Input video clip.")
    p.add_argument(
        "--prompt", required=True, help="Text description of the Foley to generate."
    )
    p.add_argument("--out", type=Path, required=True, help="Output audio file path.")
    p.add_argument("--env-file", type=Path, help="Env file holding FAL_KEY.")
    p.add_argument(
        "--max-wait-sec", type=int, default=600, help="Max seconds to wait for the fal job."
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print planned request, skip the API call.",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    clip = args.clip.expanduser().resolve()
    if not clip.is_file():
        raise AstridError(f"clip not found: {clip}", recovery_command="verify the clip path exists and is a valid video file")
    out = args.out.expanduser().resolve()

    payload_preview = {
        "model_id": FAL_MODEL_ID,
        "clip": str(clip),
        "prompt": args.prompt,
        "out": str(out),
    }

    if args.dry_run:
        print(json.dumps(payload_preview, indent=2))
        return 0

    api_key = CredentialsScope.get_local("fal", env_file=args.env_file)
    payload = {
        "video_url": _data_uri_for_video(clip),
        "text_prompt": args.prompt,
    }

    client = default_client()
    client.register_secret(api_key)

    # Use the shared fal_submit_and_poll helper instead of custom submit + poll.
    result = fal_submit_and_poll(
        client,
        FAL_MODEL_ID,
        payload,
        api_key,
        max_wait_sec=args.max_wait_sec,
    )
    saved = _save_audio(client, result, out)

    sidecar = out.with_suffix(out.suffix + ".fal.json")
    sidecar.write_text(
        json.dumps(
            {
                **payload_preview,
                "request_id": result.get("request_id"),
                "saved": saved,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    manifest = build_manifest(
        kind="fal.fal_foley",
        inputs={"clip": str(clip), "prompt": args.prompt},
        outputs=[
            {
                "name": "audio",
                "path": out.name,
                "type": "file",
                "artifact_type": "audio",
                "media_type": saved.get("content_type") or "audio/wav",
                "ordinal": 0,
                "role": "result",
                "is_primary": True,
            },
            {
                "name": "audio_provenance",
                "path": sidecar.name,
                "type": "file",
                "artifact_type": "application/json",
                "media_type": "application/json",
                "ordinal": 1,
                "role": "auxiliary",
                "is_primary": False,
            },
        ],
        created=datetime.now(timezone.utc).isoformat(),
        warnings=[],
        provider_extension={
            "model_id": FAL_MODEL_ID,
            "request_id": result.get("request_id"),
            "source_url": saved.get("source_url"),
            "source_file_name": saved.get("file_name"),
        },
    )
    write_manifest(out.parent / "manifest.json", manifest)

    print(f"wrote_audio={saved['path']}")
    print(f"wrote_manifest={out.parent / 'manifest.json'}")
    print(f"wrote_sidecar={sidecar}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
