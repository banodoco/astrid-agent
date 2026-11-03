"""Publish a reachable video URL through banodoco-social."""

from __future__ import annotations

from astrid.pack_api import AstridError
from astrid.pack_api import guard_canonical_entrypoint

guard_canonical_entrypoint("youtube.upload")

import argparse
import json
from collections.abc import Iterable

from astrid.pack_api import run_pack_main
from .src.social_publish import PublishError, publish_youtube_video


def _tag_values(value: str | Iterable[str] | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return [str(item) for item in value]


def run(
    *,
    video_url: str,
    title: str,
    description: str,
    tag: str | Iterable[str] | None = None,
    tags: str | Iterable[str] | None = None,
    privacy_status: str = "private",
    playlist_id: str | None = None,
    made_for_kids: bool = False,
    webhook_url: str | None = None,
) -> dict:
    """Return the shared publisher's JSON result as action_result."""
    try:
        return publish_youtube_video(
            video_url=video_url,
            title=title,
            description=description,
            tags=[*_tag_values(tag), *_tag_values(tags)],
            privacy_status=privacy_status,
            playlist_id=playlist_id,
            made_for_kids=made_for_kids,
            webhook_url=webhook_url,
        )
    except PublishError as exc:
        raise AstridError(
            str(exc),
            recovery_command="verify the video URL is accessible, the webhook is configured correctly, and your Zapier integration is active, then retry",
        ) from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="publish-youtube",
        description="Publish a reachable http(s) video URL to YouTube via Zapier.",
    )
    parser.add_argument(
        "--video-url",
        "--video",
        dest="video_url",
        required=True,
        help="Reachable http(s) video URL for the rendered talk video.",
    )
    parser.add_argument("--title", required=True, help="YouTube video title.")
    parser.add_argument("--description", required=True, help="YouTube video description.")
    parser.add_argument("--tag", action="append", default=[], help="YouTube tag. May be repeated.")
    parser.add_argument("--tags", action="append", default=[], help="Comma-separated YouTube tags.")
    parser.add_argument("--privacy-status", default="private", help="YouTube privacy status: private, unlisted, or public.")
    parser.add_argument("--playlist-id", help="Optional YouTube playlist ID.")
    parser.add_argument("--made-for-kids", action="store_true", help="Mark the video as made for kids.")
    parser.add_argument("--webhook-url", help="Optional Zapier webhook override. Defaults to ZAPIER_YOUTUBE_URL.")
    return parser


def main(argv: list[str] | None = None) -> int:
    def _run() -> int:
        args = build_parser().parse_args(argv)
        result = run(
            video_url=args.video_url,
            title=args.title,
            description=args.description,
            tag=args.tag,
            tags=args.tags,
            privacy_status=args.privacy_status,
            playlist_id=args.playlist_id,
            made_for_kids=args.made_for_kids,
            webhook_url=args.webhook_url,
        )
        print(json.dumps(result, separators=(",", ":")))
        return 0

    return run_pack_main("youtube.upload", _run, argv=argv)


if __name__ == "__main__":
    raise SystemExit(main())
