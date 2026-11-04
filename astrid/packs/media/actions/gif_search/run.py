#!/usr/bin/env python3
"""Search GIPHY for GIF/sticker assets and optionally download one result."""

from __future__ import annotations

from astrid.pack_api import AstridError
from astrid.pack_api import guard_canonical_entrypoint, run_pack_main

guard_canonical_entrypoint("media.gif_search")

import argparse
import html
import json
import mimetypes
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from astrid.pack_api import build_manifest, write_manifest
from astrid.pack_api import StaticChoices
from astrid.pack_api import CredentialsScope

GIPHY_SEARCH_BASE = "https://api.giphy.com/v1/{kind}/search"
DEFAULT_LIMIT = 12
MAX_LIMIT = 50
DEFAULT_RATING = "pg-13"
DEFAULT_LANG = "en"
SUPPORTED_RATINGS = {"g", "pg", "pg-13", "r"}
SUPPORTED_KINDS = {"gif": "gifs", "sticker": "stickers"}
PREFERRED_DOWNLOAD_RENDITIONS = (
    ("original", "mp4"),
    ("downsized", "mp4"),
    ("fixed_width", "mp4"),
    ("original", "url"),
    ("downsized", "url"),
    ("fixed_width", "url"),
)

Urlopen = Callable[..., Any]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="media.gif_search",
        description="Search GIPHY for GIFs or stickers.",
    )
    parser.add_argument("--query", required=True, help="GIF/sticker search query.")
    parser.add_argument("--out", type=Path, required=True, help="Output directory.")
    parser.add_argument("--provider", default="giphy", help="Provider. Only giphy is implemented.")
    parser.add_argument(
        "--media-kind",
        choices=StaticChoices(sorted(SUPPORTED_KINDS), catalog="gif_search.media_kind"),
        default="gif",
        help="Search GIFs or stickers.",
    )
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="Result count, max 50.")
    parser.add_argument("--offset", type=int, default=0, help="Provider pagination offset.")
    parser.add_argument("--rating", default=DEFAULT_RATING, help="GIPHY rating filter.")
    parser.add_argument("--lang", default=DEFAULT_LANG, help="GIPHY language code.")
    parser.add_argument("--env-file", type=Path, help="Optional .env file containing GIPHY_API_KEY.")
    parser.add_argument("--download-index", type=int, help="Zero-based result index to download.")
    parser.add_argument("--download-id", help="Provider result id to download.")
    parser.add_argument("--timeout", type=float, default=30.0, help="HTTP timeout in seconds.")
    return parser


def _validate_args(args: argparse.Namespace) -> None:
    if not args.query.strip():
        raise AstridError("query must not be empty", recovery_command="provide a non-empty --query")
    if args.provider != "giphy":
        raise AstridError(
            f"unsupported GIF provider: {args.provider}",
            recovery_command="use --provider giphy",
        )
    if args.limit < 1 or args.limit > MAX_LIMIT:
        raise AstridError(
            f"limit must be between 1 and {MAX_LIMIT}, got {args.limit}",
            recovery_command=f"use --limit between 1 and {MAX_LIMIT}",
        )
    if args.offset < 0:
        raise AstridError("offset must be >= 0", recovery_command="use --offset 0 or greater")
    if args.rating not in SUPPORTED_RATINGS:
        valid = ", ".join(sorted(SUPPORTED_RATINGS))
        raise AstridError(
            f"unsupported GIPHY rating: {args.rating}",
            recovery_command=f"use one of: {valid}",
        )
    if args.download_index is not None and args.download_id is not None:
        raise AstridError(
            "use only one of --download-index or --download-id",
            recovery_command="choose a result by index or id, not both",
        )
    if args.download_index is not None and args.download_index < 0:
        raise AstridError(
            "download index must be >= 0",
            recovery_command="use a zero-based result index from results.json",
        )


def _load_json_url(url: str, *, timeout: float, urlopen: Urlopen) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "Astrid/media.gif_search"},
        method="GET",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise AstridError(
            f"GIPHY request failed with HTTP {exc.code}",
            recovery_command="check GIPHY_API_KEY, query parameters, and provider quota, then rerun",
            state_snapshot={"response": body[:2000]},
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise AstridError(
            f"GIPHY request failed: {exc}",
            recovery_command="check network connectivity and rerun media.gif_search",
        ) from exc

    try:
        decoded = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise AstridError(
            "GIPHY returned non-JSON response",
            recovery_command="inspect provider response and rerun",
            state_snapshot={"response": payload[:2000]},
        ) from exc
    if not isinstance(decoded, dict):
        raise AstridError("GIPHY returned unexpected response shape")
    return decoded


def _giphy_search_url(args: argparse.Namespace, api_key: str) -> str:
    provider_kind = SUPPORTED_KINDS[args.media_kind]
    params = {
        "api_key": api_key,
        "q": args.query,
        "limit": args.limit,
        "offset": args.offset,
        "rating": args.rating,
        "lang": args.lang,
        "bundle": "messaging_non_clips",
    }
    return f"{GIPHY_SEARCH_BASE.format(kind=provider_kind)}?{urllib.parse.urlencode(params)}"


def _coerce_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _normalize_image(image: dict[str, Any]) -> dict[str, Any]:
    return {
        "url": image.get("url"),
        "mp4": image.get("mp4"),
        "webp": image.get("webp"),
        "width": _coerce_int(image.get("width")),
        "height": _coerce_int(image.get("height")),
        "size": _coerce_int(image.get("size")),
    }


def _normalize_item(item: dict[str, Any], *, index: int, provider: str, media_kind: str) -> dict[str, Any]:
    images = item.get("images") if isinstance(item.get("images"), dict) else {}
    normalized_images = {
        name: _normalize_image(value)
        for name, value in images.items()
        if isinstance(value, dict)
    }
    preview_url = (
        normalized_images.get("fixed_width", {}).get("url")
        or normalized_images.get("downsized", {}).get("url")
        or normalized_images.get("original", {}).get("url")
    )
    return {
        "index": index,
        "provider": provider,
        "media_kind": media_kind,
        "id": item.get("id"),
        "title": item.get("title") or item.get("slug") or "",
        "url": item.get("url"),
        "embed_url": item.get("embed_url"),
        "username": item.get("username") or "",
        "rating": item.get("rating"),
        "source": item.get("source"),
        "preview_url": preview_url,
        "images": normalized_images,
    }


def normalize_giphy_response(response: dict[str, Any], *, query: str, media_kind: str) -> dict[str, Any]:
    raw_items = response.get("data")
    if not isinstance(raw_items, list):
        raise AstridError(
            "GIPHY response did not include a data list",
            recovery_command="inspect provider response and rerun",
            state_snapshot={"response_keys": sorted(response)},
        )
    return {
        "provider": "giphy",
        "media_kind": media_kind,
        "query": query,
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "pagination": response.get("pagination") if isinstance(response.get("pagination"), dict) else {},
        "meta": response.get("meta") if isinstance(response.get("meta"), dict) else {},
        "results": [
            _normalize_item(item, index=index, provider="giphy", media_kind=media_kind)
            for index, item in enumerate(raw_items)
            if isinstance(item, dict)
        ],
        "attribution": "Powered by GIPHY",
    }


def _select_result(results: list[dict[str, Any]], *, index: int | None, result_id: str | None) -> dict[str, Any] | None:
    if index is not None:
        if index >= len(results):
            return None
        return results[index]
    if result_id is not None:
        for result in results:
            if result.get("id") == result_id:
                return result
    return None


def _pick_download_url(result: dict[str, Any]) -> tuple[str, str, str] | None:
    images = result.get("images")
    if not isinstance(images, dict):
        return None
    for rendition, field in PREFERRED_DOWNLOAD_RENDITIONS:
        image = images.get(rendition)
        if not isinstance(image, dict):
            continue
        value = image.get(field)
        if not value:
            continue
        suffix = ".mp4" if field == "mp4" else ".gif"
        return str(value), suffix, f"{rendition}.{field}"
    return None


def _download_file(url: str, dest: Path, *, timeout: float, urlopen: Urlopen) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "Astrid/media.gif_search"}, method="GET")
    try:
        with urlopen(request, timeout=timeout) as response:
            content_type = response.headers.get("content-type", "")
            data = response.read()
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as exc:
        raise AstridError(
            f"download failed: {exc}",
            recovery_command="choose another result or rerun after checking network access",
        ) from exc
    guessed = mimetypes.guess_extension(content_type.split(";", 1)[0].strip()) if content_type else None
    if guessed and dest.suffix != guessed and dest.suffix == ".bin":
        dest = dest.with_suffix(guessed)
    dest.write_bytes(data)


def _write_preview(path: Path, results_payload: dict[str, Any]) -> None:
    cards = []
    for result in results_payload["results"]:
        title = html.escape(result.get("title") or result.get("id") or "Untitled")
        preview_url = html.escape(result.get("preview_url") or "")
        result_id = html.escape(result.get("id") or "")
        provider_url = html.escape(result.get("url") or "")
        if preview_url:
            media = f'<img src="{preview_url}" alt="{title}" loading="lazy">'
        else:
            media = '<div class="missing">No preview</div>'
        cards.append(
            f"""
            <article>
              <a href="{provider_url}" target="_blank" rel="noreferrer">{media}</a>
              <h2>{title}</h2>
              <p><code>{result["index"]}</code> <code>{result_id}</code></p>
            </article>
            """
        )
    markup = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Astrid GIF Search Preview</title>
  <style>
    body {{ margin: 0; font: 14px/1.4 system-ui, sans-serif; background: #101114; color: #f3f3f1; }}
    header {{ padding: 18px 24px; border-bottom: 1px solid #30323a; }}
    main {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); gap: 14px; padding: 18px; }}
    article {{ background: #1c1e24; border: 1px solid #30323a; border-radius: 6px; overflow: hidden; }}
    img {{ display: block; width: 100%; aspect-ratio: 1 / 1; object-fit: cover; background: #050608; }}
    h2 {{ font-size: 14px; margin: 10px 10px 4px; }}
    p {{ margin: 0 10px 12px; color: #b8bac4; overflow-wrap: anywhere; }}
    code {{ font-size: 12px; }}
    .missing {{ display: grid; place-items: center; aspect-ratio: 1 / 1; color: #b8bac4; }}
  </style>
</head>
<body>
  <header>
    <strong>{html.escape(results_payload["query"])}</strong>
    <span> · {len(results_payload["results"])} results · Powered by GIPHY</span>
  </header>
  <main>
    {''.join(cards)}
  </main>
</body>
</html>
"""
    path.write_text(markup, encoding="utf-8")


def main(argv: list[str] | None = None, *, urlopen: Urlopen = urllib.request.urlopen) -> int:
    def _main() -> int:
        args = build_parser().parse_args(argv)
        _validate_args(args)

        out_dir = args.out.expanduser().resolve()
        env_file = args.env_file.expanduser().resolve() if args.env_file else None
        out_dir.mkdir(parents=True, exist_ok=True)

        api_key = CredentialsScope.get_local("giphy", env_file=env_file)
        response = _load_json_url(_giphy_search_url(args, api_key), timeout=args.timeout, urlopen=urlopen)
        payload = normalize_giphy_response(response, query=args.query, media_kind=args.media_kind)

        results_path = out_dir / "results.json"
        preview_path = out_dir / "preview.html"
        results_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        _write_preview(preview_path, payload)

        outputs = [
            {"path": results_path.name, "type": "metadata"},
            {"path": preview_path.name, "type": "preview"},
        ]
        download_selector = None
        if args.download_index is not None or args.download_id is not None:
            selected = _select_result(payload["results"], index=args.download_index, result_id=args.download_id)
            if selected is None:
                raise AstridError(
                    "download selector did not match any returned result",
                    recovery_command="inspect results.json and rerun with a valid --download-index or --download-id",
                )
            picked = _pick_download_url(selected)
            if picked is None:
                raise AstridError(
                    "selected result does not include a downloadable GIF or MP4 rendition",
                    recovery_command="choose another result from results.json",
                    state_snapshot={"selected": selected},
                )
            download_url, suffix, rendition = picked
            selected_path = out_dir / f"selected-{selected['index']}-{selected['id']}{suffix}"
            _download_file(download_url, selected_path, timeout=args.timeout, urlopen=urlopen)
            download_selector = {
                "index": selected["index"],
                "id": selected["id"],
                "rendition": rendition,
                "path": selected_path.name,
            }
            outputs.append({"path": selected_path.name, "type": "file", "source": rendition})

        manifest = build_manifest(
            kind="gif_search",
            inputs={
                "query": args.query,
                "provider": args.provider,
                "media_kind": args.media_kind,
                "limit": args.limit,
                "offset": args.offset,
                "rating": args.rating,
                "lang": args.lang,
                "env_file": str(env_file) if env_file else None,
                "download_index": args.download_index,
                "download_id": args.download_id,
            },
            outputs=outputs,
            created=datetime.now(timezone.utc).isoformat(),
            recipe={
                "provider": "giphy",
                "endpoint": SUPPORTED_KINDS[args.media_kind],
                "download": download_selector,
            },
            metrics={
                "result_count": len(payload["results"]),
                "total_count": payload.get("pagination", {}).get("total_count"),
            },
        )
        write_manifest(out_dir / "manifest.json", manifest)
        print(results_path)
        return 0

    return run_pack_main("media.gif_search", _main, argv=argv)


if __name__ == "__main__":
    raise SystemExit(main())
