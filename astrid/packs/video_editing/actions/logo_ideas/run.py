#!/usr/bin/env python3
"""Logo Ideas orchestrator: Kimi K2 (Fireworks) drafts prompts, fal renders them."""


from __future__ import annotations

from astrid.pack_api import guard_canonical_entrypoint

guard_canonical_entrypoint('video_editing.logo_ideas')
import argparse
import json
import math
import re
from pathlib import Path
from typing import Any, Sequence

from astrid.pack_api import add_choice_arg
from astrid.pack_api import CredentialsScope
from astrid.pack_api import HttpClient, default_client, fal_submit_and_poll
from astrid.packs.video_editing.shared.result_receipts import write_action_receipt

FIREWORKS_CHAT_URL = "https://api.fireworks.ai/inference/v1/chat/completions"

DEFAULT_COUNT = 9
DEFAULT_FIREWORKS_MODEL = "accounts/fireworks/models/kimi-k2p5"
DEFAULT_PROVIDER = "gpt-image"
DEFAULT_IMAGE_SIZE = "square_hd"
DEFAULT_OUTPUT_FORMAT = "png"

GRID_PROVIDERS = {"gpt-image"}

PROVIDER_MODEL_IDS = {
    "z-image": "fal-ai/z-image/turbo",
    "gpt-image": "openai/gpt-image-2",
}

FAL_PRESETS = {
    "square_hd",
    "square",
    "portrait_4_3",
    "portrait_16_9",
    "landscape_4_3",
    "landscape_16_9",
}


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def parse_image_size(value: str) -> str | dict[str, int]:
    if value in FAL_PRESETS:
        return value
    match = re.fullmatch(r"([1-9][0-9]*)x([1-9][0-9]*)", value.strip())
    if not match:
        raise argparse.ArgumentTypeError(
            "image-size must be a fal preset (square_hd, portrait_16_9, ...) or WIDTHxHEIGHT"
        )
    return {"width": int(match.group(1)), "height": int(match.group(2))}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate a grid of logo ideas via Kimi K2 + fal.")
    parser.add_argument("--ideas", required=True, help="Brief describing the logo: brand, vibe, motifs, constraints.")
    parser.add_argument("--out", type=Path, required=True, help="Output directory for concepts, prompts, images, and the grid.")
    parser.add_argument("--count", type=int, default=DEFAULT_COUNT, help=f"Number of logos to generate (default {DEFAULT_COUNT}).")
    add_choice_arg(
        parser,
        "--provider",
        default=DEFAULT_PROVIDER,
        values=sorted(PROVIDER_MODEL_IDS),
        help=f"Image provider (default {DEFAULT_PROVIDER}).",
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_FIREWORKS_MODEL,
        help=f"Fireworks chat model id (default {DEFAULT_FIREWORKS_MODEL}).",
    )
    parser.add_argument(
        "--image-size",
        default=DEFAULT_IMAGE_SIZE,
        type=parse_image_size,
        help=f"fal image size: preset or WIDTHxHEIGHT (default {DEFAULT_IMAGE_SIZE}).",
    )
    add_choice_arg(
        parser,
        "--output-format",
        default=DEFAULT_OUTPUT_FORMAT,
        values=("png", "jpeg", "jpg", "webp"),
        help=f"Image format saved locally (default {DEFAULT_OUTPUT_FORMAT}).",
    )
    parser.add_argument("--env-file", type=Path, help="Env file holding FIREWORKS_API_KEY and FAL_KEY.")
    parser.add_argument("--dry-run", action="store_true", help="Plan and write artifacts; skip both Fireworks and fal calls.")
    return parser


def _validate_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if args.count < 1:
        parser.error("--count must be >= 1")
    if args.count > 64:
        parser.error("--count must be <= 64 (sanity bound)")


def build_layout(out_dir: Path) -> dict[str, Path]:
    root = out_dir.expanduser().resolve()
    layout = {
        "root": root,
        "images": root / "images",
    }
    for path in layout.values():
        path.mkdir(parents=True, exist_ok=True)
    return layout


def _system_prompt() -> str:
    return (
        "You are a senior brand designer. Given a logo brief, propose distinct, "
        "creatively varied logo concepts. Each concept should differ in style, "
        "composition, or motif — not minor color variants. Return only JSON."
    )


def _user_prompt(ideas: str, count: int) -> str:
    return (
        f"Brief:\n{ideas}\n\n"
        f"Produce exactly {count} logo concepts. Return JSON of the shape:\n"
        '{"concepts":[{"name":"short title","rationale":"1 sentence why",'
        '"prompt":"single self-contained image-gen prompt, ~40 words, '
        "describing style, layout, palette, typography hints, no negative prompts, "
        'no text like \\"logo:\\"\"}]}\n'
        "The 'prompt' field is fed verbatim to a text-to-image model — make it "
        "concrete and visual. Avoid trademarked references unless the brief asks."
    )


def call_fireworks_concepts(
    *,
    ideas: str,
    count: int,
    model: str,
    api_key: str,
    client: HttpClient,
) -> dict[str, Any]:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": _system_prompt()},
            {"role": "user", "content": _user_prompt(ideas, count)},
        ],
        "temperature": 0.9,
        "max_tokens": 4000,
        "response_format": {"type": "json_object"},
    }
    headers = {"authorization": f"Bearer {api_key}"}
    return client.post_json(FIREWORKS_CHAT_URL, payload, headers=headers, timeout=180)


def parse_concepts(response: dict[str, Any], *, count: int) -> list[dict[str, Any]]:
    choices = response.get("choices") or []
    if not choices:
        raise SystemExit("Fireworks response had no choices")
    content = (choices[0].get("message") or {}).get("content") or ""
    text = content.strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if not match:
            raise SystemExit(f"Could not parse JSON from Fireworks response: {text[:300]}")
        data = json.loads(match.group(0))
    raw_concepts = data.get("concepts") if isinstance(data, dict) else None
    if not isinstance(raw_concepts, list) or not raw_concepts:
        raise SystemExit(f"Fireworks response missing 'concepts' list: {text[:300]}")
    concepts: list[dict[str, Any]] = []
    for index, item in enumerate(raw_concepts[:count], start=1):
        if not isinstance(item, dict):
            continue
        prompt = str(item.get("prompt") or "").strip()
        if not prompt:
            continue
        concepts.append(
            {
                "candidate_id": f"logo-{index:03d}",
                "index": index,
                "name": str(item.get("name") or f"Concept {index}").strip(),
                "rationale": str(item.get("rationale") or "").strip(),
                "prompt": prompt,
            }
        )
    if not concepts:
        raise SystemExit("Fireworks response yielded no usable concepts")
    return concepts


def _planned_concepts(ideas: str, count: int) -> list[dict[str, Any]]:
    return [
        {
            "candidate_id": f"logo-{index:03d}",
            "index": index,
            "name": f"Planned concept {index}",
            "rationale": "Dry-run placeholder; no model call was made.",
            "prompt": f"[dry-run] minimalist logo for: {ideas}",
        }
        for index in range(1, count + 1)
    ]


def _fal_payload(provider: str, prompt: str, image_size: str | dict[str, int], output_format: str) -> dict[str, Any]:
    fmt = "jpeg" if output_format == "jpg" else output_format
    if provider == "z-image":
        return {
            "prompt": prompt,
            "image_size": image_size,
            "num_images": 1,
            "output_format": fmt,
        }
    if provider == "gpt-image":
        return {
            "prompt": prompt,
            "image_size": image_size,
            "num_images": 1,
            "output_format": fmt,
            "quality": "high",
        }
    raise SystemExit(f"unknown provider {provider!r}")


def _ext_for_format(output_format: str) -> str:
    return "jpg" if output_format in ("jpeg", "jpg") else output_format


def _save_first_image(result: dict[str, Any], dest: Path, client: HttpClient) -> dict[str, Any]:
    images = result.get("images") or []
    if not images:
        raise SystemExit(f"fal result had no images: {result}")
    first = images[0]
    url = first.get("url")
    if not url:
        raise SystemExit(f"fal image entry missing url: {first}")
    data = client.get_bytes(url, timeout=180)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    return {
        "path": str(dest),
        "source_url": url,
        "width": first.get("width"),
        "height": first.get("height"),
        "content_type": first.get("content_type"),
        "bytes": len(data),
    }


def _placeholder_image(dest: Path, label: str) -> dict[str, Any]:
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        dest.write_bytes(b"")
        return {"path": str(dest), "placeholder": True, "reason": "Pillow unavailable"}
    image = Image.new("RGB", (768, 768), (24, 24, 28))
    draw = ImageDraw.Draw(image)
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial Bold.ttf", 28)
    except OSError:
        font = ImageFont.load_default()
    draw.text((24, 24), "[dry-run]", fill=(220, 220, 220), font=font)
    draw.text((24, 72), label[:48], fill=(180, 180, 180), font=font)
    image.save(dest, quality=90)
    return {"path": str(dest), "placeholder": True, "reason": "dry-run"}


def render_concepts(
    *,
    concepts: list[dict[str, Any]],
    layout: dict[str, Path],
    provider: str,
    image_size: str | dict[str, int],
    output_format: str,
    fal_key: str | None,
    client: HttpClient,
    dry_run: bool,
) -> list[dict[str, Any]]:
    ext = _ext_for_format(output_format)
    results: list[dict[str, Any]] = []
    model_id = PROVIDER_MODEL_IDS[provider]
    for concept in concepts:
        candidate_id = concept["candidate_id"]
        dest = layout["images"] / f"{candidate_id}.{ext}"
        if dry_run or not fal_key:
            generated = _placeholder_image(dest, concept["name"])
        else:
            payload = _fal_payload(provider, concept["prompt"], image_size, output_format)
            result = fal_submit_and_poll(client, model_id, payload, fal_key)
            generated = _save_first_image(result, dest, client)
            generated["request_id"] = result.get("request_id")
        results.append({**concept, "generated": generated})
    return results


def build_grid_prompt(ideas: str, concepts: list[dict[str, Any]]) -> str:
    n = len(concepts)
    cols = max(1, math.ceil(math.sqrt(n)))
    rows = math.ceil(n / cols)
    cells = "\n".join(
        f"Cell {i + 1} ({c.get('name') or c['candidate_id']}): {c['prompt']}"
        for i, c in enumerate(concepts)
    )
    return (
        f"A {rows}x{cols} contact-sheet grid containing {n} distinct logo concepts on a "
        f"clean neutral dark background. Each cell shows ONE complete, separate logo at "
        f"equal size, with thin gutters between cells. No labels, captions, or watermark text.\n\n"
        f"Brief: {ideas}\n\n"
        f"Cells (in reading order, left-to-right, top-to-bottom):\n{cells}"
    )


def render_grid_image(
    *,
    ideas: str,
    concepts: list[dict[str, Any]],
    layout: dict[str, Path],
    provider: str,
    image_size: str | dict[str, int],
    output_format: str,
    fal_key: str | None,
    client: HttpClient,
    dry_run: bool,
) -> tuple[list[dict[str, Any]], dict[str, Any], str]:
    ext = _ext_for_format(output_format)
    grid_path = layout["root"] / f"grid.{ext}"
    grid_prompt = build_grid_prompt(ideas, concepts)
    model_id = PROVIDER_MODEL_IDS[provider]

    if dry_run or not fal_key:
        generated = _placeholder_image(grid_path, "grid (dry-run)")
    else:
        payload = _fal_payload(provider, grid_prompt, image_size, output_format)
        result = fal_submit_and_poll(client, model_id, payload, fal_key)
        generated = _save_first_image(result, grid_path, client)
        generated["request_id"] = result.get("request_id")

    generated["grid_prompt"] = grid_prompt
    results = [{**c, "generated": dict(generated)} for c in concepts]
    return results, generated, grid_prompt


def write_grid(results: list[dict[str, Any]], out_path: Path) -> dict[str, Any]:
    paths: list[tuple[Path, str]] = []
    for item in results:
        gen = item.get("generated") or {}
        path = Path(str(gen.get("path") or ""))
        if path.is_file() and path.stat().st_size > 0:
            paths.append((path, str(item.get("name") or item.get("candidate_id"))))
    if not paths:
        return {"path": None, "image_count": 0, "reason": "no images available"}
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return {"path": None, "image_count": len(paths), "reason": "Pillow unavailable"}
    cols = max(1, math.ceil(math.sqrt(len(paths))))
    rows = math.ceil(len(paths) / cols)
    tile = 384
    label_h = 28
    grid = Image.new("RGB", (cols * tile, rows * (tile + label_h)), (16, 16, 18))
    draw = ImageDraw.Draw(grid)
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial.ttf", 16)
    except OSError:
        font = ImageFont.load_default()
    for index, (path, label) in enumerate(paths):
        col = index % cols
        row = index // cols
        x = col * tile
        y = row * (tile + label_h)
        try:
            with Image.open(path) as opened:
                tile_image = opened.convert("RGB")
                tile_image.thumbnail((tile, tile))
                offset_x = x + (tile - tile_image.width) // 2
                offset_y = y + (tile - tile_image.height) // 2
                grid.paste(tile_image, (offset_x, offset_y))
        except Exception:
            draw.rectangle((x, y, x + tile, y + tile), outline=(60, 60, 64), width=2)
        draw.text((x + 8, y + tile + 4), label[:40], fill=(220, 220, 220), font=font)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    grid.save(out_path, quality=90)
    return {"path": str(out_path), "image_count": len(paths), "cols": cols, "rows": rows}


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _validate_args(parser, args)

    layout = build_layout(args.out)
    image_size_payload = args.image_size if isinstance(args.image_size, str) else dict(args.image_size)

    plan = {
        "tool": "logo_ideas",
        "version": 1,
        "mode": "dry-run" if args.dry_run else "run",
        "ideas": args.ideas,
        "count": args.count,
        "provider": args.provider,
        "fal_model_id": PROVIDER_MODEL_IDS[args.provider],
        "fireworks_model": args.model,
        "image_size": image_size_payload,
        "output_format": args.output_format,
        "out": str(layout["root"]),
    }
    write_json(layout["root"] / "logo-plan.json", plan)

    client = default_client()

    if args.dry_run:
        concepts = _planned_concepts(args.ideas, args.count)
        concepts_payload = {"mode": "dry-run", "raw_response": None, "concepts": concepts}
    else:
        fireworks_key = CredentialsScope.get_local("fireworks", env_file=args.env_file)
        client.register_secret(fireworks_key)
        response = call_fireworks_concepts(
            ideas=args.ideas,
            count=args.count,
            model=args.model,
            api_key=fireworks_key,
            client=client,
        )
        concepts = parse_concepts(response, count=args.count)
        concepts_payload = {
            "mode": "run",
            "model": args.model,
            "usage": response.get("usage"),
            "concepts": concepts,
        }
    write_json(layout["root"] / "concepts.json", concepts_payload)
    write_json(
        layout["root"] / "prompts.json",
        {
            "provider": args.provider,
            "fal_model_id": PROVIDER_MODEL_IDS[args.provider],
            "image_size": image_size_payload,
            "output_format": args.output_format,
            "prompts": [
                {"candidate_id": c["candidate_id"], "name": c["name"], "prompt": c["prompt"]}
                for c in concepts
            ],
        },
    )

    fal_key = None if args.dry_run else CredentialsScope.get_local("fal", env_file=args.env_file)
    if fal_key:
        client.register_secret(fal_key)
    grid_mode = args.provider in GRID_PROVIDERS
    if grid_mode:
        results, grid_generated, grid_prompt = render_grid_image(
            ideas=args.ideas,
            concepts=concepts,
            layout=layout,
            provider=args.provider,
            image_size=image_size_payload,
            output_format=args.output_format,
            fal_key=fal_key,
            client=client,
            dry_run=args.dry_run,
        )
        grid = {
            "path": grid_generated.get("path"),
            "image_count": 1,
            "mode": "single-call",
            "prompt": grid_prompt,
            "placeholder": bool(grid_generated.get("placeholder")),
        }
    else:
        results = render_concepts(
            concepts=concepts,
            layout=layout,
            provider=args.provider,
            image_size=image_size_payload,
            output_format=args.output_format,
            fal_key=fal_key,
            client=client,
            dry_run=args.dry_run,
        )
        grid = write_grid(results, layout["root"] / "grid.jpg")
        grid["mode"] = "composite"

    manifest = {
        "version": 1,
        "mode": plan["mode"],
        "ideas": args.ideas,
        "count": args.count,
        "provider": args.provider,
        "fireworks_model": args.model,
        "image_size": image_size_payload,
        "grid": grid,
        "candidates": results,
    }
    write_json(layout["root"] / "logo-manifest.json", manifest)
    grid_path = Path(str(grid.get("path") or ""))
    receipt_outputs = {
        "plan": "logo-plan.json",
        "concepts": "concepts.json",
        "prompts": "prompts.json",
        "manifest": "logo-manifest.json",
        "images": "images",
    }
    if grid_path.is_file():
        receipt_outputs["grid"] = grid_path.resolve().relative_to(layout["root"].resolve()).as_posix()
    write_action_receipt(
        layout["root"],
        kind="video_editing.logo_ideas",
        inputs={"ideas": args.ideas, "provider": args.provider, "count": args.count},
        output_paths=receipt_outputs,
        primary_port="grid" if grid_path.is_file() else None,
    )
    print(f"wrote_logo_manifest={layout['root'] / 'logo-manifest.json'}")
    if grid.get("path"):
        print(f"wrote_grid={grid['path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
