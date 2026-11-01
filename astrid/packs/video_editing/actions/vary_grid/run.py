#!/usr/bin/env python3
"""Vary Grid orchestrator: slice an existing grid, pick cells, edit into a new grid via OpenAI gpt-image-2."""


from __future__ import annotations

from astrid.core.pack.entrypoint import guard_canonical_entrypoint

guard_canonical_entrypoint('video_editing.vary_grid')
import argparse
import base64
import json
import math
import re
from pathlib import Path
from typing import Any, Sequence
from urllib.error import HTTPError, URLError

from astrid.core.cli_choices import add_choice_arg
from astrid.core.util.credentials_scope import CredentialsScope
from astrid.core.util.http import FAL_QUEUE_URL, default_client
from astrid.packs.video_editing.actions.logo_ideas.run import (
    DEFAULT_FIREWORKS_MODEL,
    FIREWORKS_CHAT_URL,
    parse_concepts,
)
from astrid.packs.video_editing.shared.result_receipts import write_action_receipt

FAL_EDIT_MODEL_ID = "openai/gpt-image-2/edit"
DEFAULT_COUNT = 9
DEFAULT_SIZE = "1024x1024"
DEFAULT_QUALITY = "high"
MAX_COUNT = 9

_client = default_client()


def _load_env_var(name: str, *, env_file: Path | None = None) -> str:
    scope = name.lower().removesuffix("_api_key").removesuffix("_key")
    return CredentialsScope.get_local(scope, env_file=env_file)



def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def parse_cells(spec: str, total: int) -> list[int]:
    spec = spec.strip()
    if not spec:
        raise argparse.ArgumentTypeError("--cells must not be empty")
    if spec.lower() == "all":
        return list(range(1, total + 1))
    picked: list[int] = []
    for token in spec.split(","):
        token = token.strip()
        if not token:
            continue
        if "-" in token:
            lo, hi = token.split("-", 1)
            a, b = int(lo), int(hi)
            if a < 1 or b < a or b > total:
                raise argparse.ArgumentTypeError(f"--cells range '{token}' out of bounds (1..{total})")
            picked.extend(range(a, b + 1))
        else:
            n = int(token)
            if n < 1 or n > total:
                raise argparse.ArgumentTypeError(f"--cells '{n}' out of bounds (1..{total})")
            picked.append(n)
    seen: set[int] = set()
    unique: list[int] = []
    for n in picked:
        if n not in seen:
            seen.add(n)
            unique.append(n)
    if not unique:
        raise argparse.ArgumentTypeError("--cells parsed to empty list")
    return unique


def detect_source_layout(grid_path: Path, override_rows: int | None, override_cols: int | None) -> tuple[int, int]:
    if override_rows and override_cols:
        return override_rows, override_cols
    sibling = grid_path.parent / "logo-manifest.json"
    if not (override_rows or override_cols) and sibling.is_file():
        try:
            data = json.loads(sibling.read_text())
            n = int(data.get("count") or len(data.get("candidates") or []))
            if n > 0:
                cols = max(1, math.ceil(math.sqrt(n)))
                rows = math.ceil(n / cols)
                return rows, cols
        except Exception:
            pass
    sibling = grid_path.parent / "vary-manifest.json"
    if not (override_rows or override_cols) and sibling.is_file():
        try:
            data = json.loads(sibling.read_text())
            n = int(data.get("count") or 9)
            if n > 0:
                cols = max(1, math.ceil(math.sqrt(n)))
                rows = math.ceil(n / cols)
                return rows, cols
        except Exception:
            pass
    return (override_rows or 3), (override_cols or 3)


def slice_grid(grid_path: Path, rows: int, cols: int, out_dir: Path) -> list[dict[str, Any]]:
    from PIL import Image

    out_dir.mkdir(parents=True, exist_ok=True)
    img = Image.open(grid_path).convert("RGB")
    W, H = img.size
    cw, ch = W // cols, H // rows
    cells: list[dict[str, Any]] = []
    for r in range(rows):
        for c in range(cols):
            index = r * cols + c + 1
            cell = img.crop((c * cw, r * ch, (c + 1) * cw, (r + 1) * ch))
            path = out_dir / f"cell-{index:03d}.png"
            cell.save(path)
            cells.append({"index": index, "row": r + 1, "col": c + 1, "path": str(path)})
    return cells


def write_refs(cells: list[dict[str, Any]], picks: list[int], refs_dir: Path) -> list[dict[str, Any]]:
    from PIL import Image

    refs_dir.mkdir(parents=True, exist_ok=True)
    refs: list[dict[str, Any]] = []
    by_index = {c["index"]: c for c in cells}
    for ref_index, src_index in enumerate(picks, start=1):
        cell = by_index[src_index]
        src_path = Path(cell["path"])
        dest = refs_dir / f"ref-{ref_index:03d}.png"
        Image.open(src_path).convert("RGB").save(dest)
        refs.append({
            "ref_index": ref_index,
            "source_cell_index": src_index,
            "source_row": cell["row"],
            "source_col": cell["col"],
            "path": str(dest),
        })
    return refs


def _system_prompt() -> str:
    return (
        "You are a creative director iterating on an image. The user will provide a brief and "
        "(implicitly) a reference image. Propose distinct, creatively varied modifications. "
        "Each variation should differ in posture, attire, gesture, or specific magical/visual "
        "effect — not minor color tweaks. Keep the reference's core identity (characters, palette, "
        "silhouette style) intact. Return only JSON."
    )


def _user_prompt(ideas: str, count: int, ref_count: int) -> str:
    return (
        f"Brief:\n{ideas}\n\n"
        f"There are {ref_count} reference image(s) attached describing the source archetype. "
        f"Produce exactly {count} distinct variations. Return JSON of the shape:\n"
        '{"concepts":[{"name":"short title","rationale":"1 sentence why",'
        '"prompt":"single self-contained edit instruction, ~25 words, '
        "describing how to modify the reference for this cell — posture, attire, hand gesture, "
        'magical effect, framing"}]}\n'
        "The 'prompt' field is fed verbatim to gpt-image-2 — make it concrete and visual. "
        "Do NOT restate the reference; describe only the modification."
    )


def call_kimi_variations(
    *,
    ideas: str,
    count: int,
    ref_count: int,
    model: str,
    api_key: str,
) -> dict[str, Any]:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": _system_prompt()},
            {"role": "user", "content": _user_prompt(ideas, count, ref_count)},
        ],
        "temperature": 0.9,
        "max_tokens": 4000,
        "response_format": {"type": "json_object"},
    }
    headers = {"authorization": f"Bearer {api_key}"}
    try:
        return _client.post_json(FIREWORKS_CHAT_URL, payload, headers=headers, timeout=180)
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        raise SystemExit(f"Fireworks chat call failed ({exc.code}): {detail}") from exc
    except URLError as exc:
        raise SystemExit(f"Fireworks chat call failed: {exc}") from exc


def _planned_concepts(ideas: str, count: int) -> list[dict[str, Any]]:
    return [
        {
            "candidate_id": f"var-{i:03d}",
            "index": i,
            "name": f"Planned variation {i}",
            "rationale": "Dry-run placeholder.",
            "prompt": f"[dry-run] variation {i} on: {ideas}",
        }
        for i in range(1, count + 1)
    ]


def renumber_concepts(concepts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for i, c in enumerate(concepts, start=1):
        item = dict(c)
        item["candidate_id"] = f"var-{i:03d}"
        item["index"] = i
        out.append(item)
    return out


def build_grid_prompt(ideas: str, concepts: list[dict[str, Any]], ref_count: int) -> str:
    n = len(concepts)
    cols = max(1, math.ceil(math.sqrt(n)))
    rows = math.ceil(n / cols)
    cells_text = "\n".join(
        f"Cell {i + 1} ({c.get('name') or c['candidate_id']}): {c['prompt']}"
        for i, c in enumerate(concepts)
    )
    ref_clause = (
        f"You are given {ref_count} reference image(s) showing the source archetype. "
        if ref_count > 0
        else ""
    )
    return (
        f"{ref_clause}Render a {rows}x{cols} contact-sheet grid containing {n} distinct variations "
        f"on that archetype. Keep the reference's core identity intact across every cell — same characters, "
        f"same palette, same silhouette style — and only modify what each cell asks for. "
        f"Each cell sits on a clean black background, equal size, thin gutters between cells, no labels.\n\n"
        f"Variation brief: {ideas}\n\n"
        f"Cells (in reading order, left-to-right, top-to-bottom):\n{cells_text}"
    )


def build_no_kimi_prompt(ideas: str, count: int, ref_count: int) -> str:
    cols = max(1, math.ceil(math.sqrt(count)))
    rows = math.ceil(count / cols)
    ref_clause = (
        f"You are given {ref_count} reference image(s) showing the source archetype. "
        if ref_count > 0
        else ""
    )
    return (
        f"{ref_clause}Render a {rows}x{cols} contact-sheet grid containing {count} distinct variations "
        f"on that archetype. Keep the reference's core identity intact across every cell — same "
        f"characters, same palette, same silhouette style. Each cell explores a different variation "
        f"per this brief: {ideas}. Each cell sits on a clean black background, equal size, thin "
        f"gutters between cells, no labels."
    )


def _data_uri_for(path: Path) -> str:
    raw = path.read_bytes()
    return "data:image/png;base64," + base64.b64encode(raw).decode("ascii")


def _fal_image_size(size: str) -> dict[str, int]:
    w, h = size.split("x", 1)
    return {"width": int(w), "height": int(h)}


def call_fal_edit(
    *,
    prompt: str,
    image_paths: list[Path],
    api_key: str,
    size: str,
    quality: str,
    output_format: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    image_urls = [_data_uri_for(p) for p in image_paths]
    payload = {
        "prompt": prompt,
        "image_urls": image_urls,
        "image_size": _fal_image_size(size),
        "quality": quality,
        "num_images": 1,
        "output_format": "jpeg" if output_format == "jpg" else output_format,
    }
    submission = submit_fal_job_edit(payload, api_key)
    result = _client.poll_until(
        submission["status_url"],
        submission["response_url"],
        headers={"authorization": f"Key {api_key}"},
        max_wait_sec=300,
    )
    return submission, result


def submit_fal_job_edit(payload: dict[str, Any], api_key: str) -> dict[str, Any]:
    url = f"{FAL_QUEUE_URL}/{FAL_EDIT_MODEL_ID}"
    headers = {"authorization": f"Key {api_key}"}
    try:
        return _client.post_json(url, payload, headers=headers, timeout=120)
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        raise SystemExit(f"fal submit failed ({exc.code}) for {FAL_EDIT_MODEL_ID}: {detail}") from exc
    except URLError as exc:
        raise SystemExit(f"fal submit failed for {FAL_EDIT_MODEL_ID}: {exc}") from exc


def save_fal_image(result: dict[str, Any], dest: Path) -> dict[str, Any]:
    images = result.get("images") or []
    if not images:
        raise SystemExit(f"fal result had no images: {str(result)[:300]}")
    first = images[0]
    url = first.get("url")
    if not url:
        raise SystemExit(f"fal image entry missing url: {first}")
    raw = _client.get_bytes(url, timeout=180)
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


def render_favicon_mockup(
    *,
    grid_path: Path,
    rows: int,
    cols: int,
    dest: Path,
    label_prefix: str = "Logo",
    favicon_px: int = 16,
    zoom: int = 9,
) -> dict[str, Any]:
    from PIL import Image, ImageDraw, ImageFont

    img = Image.open(grid_path).convert("RGB")
    W, H = img.size
    cw, ch = W // cols, H // rows
    cells = [
        img.crop((c * cw, r * ch, (c + 1) * cw, (r + 1) * ch))
        for r in range(rows)
        for c in range(cols)
    ]
    n = len(cells)

    try:
        font = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial.ttf", 12)
    except OSError:
        font = ImageFont.load_default()

    chip_pad_x, chip_pad_y, chip_inner_gap, chip_gap = 8, 6, 6, 8
    label_widths = [font.getbbox(f"{label_prefix} {i + 1}")[2] for i in range(n)]
    chip_h = max(favicon_px + 2 * chip_pad_y, 24)

    zoom_size = favicon_px * zoom
    zoom_pad = 24
    zoom_w = cols * zoom_size + (cols + 1) * zoom_pad
    zoom_h = rows * zoom_size + (rows + 1) * zoom_pad

    title_h = 24
    chip_row_h = title_h + chip_h + 16
    chips_total_w = sum(chip_pad_x + favicon_px + chip_inner_gap + lw + chip_pad_x for lw in label_widths) + chip_gap * (n - 1)
    canvas_w = max(zoom_w, chips_total_w + 32)
    canvas_h = chip_row_h + title_h + zoom_h + 16
    bg = (24, 24, 28)
    canvas = Image.new("RGB", (canvas_w, canvas_h), bg)
    draw = ImageDraw.Draw(canvas)
    fg = (220, 220, 220)

    draw.text((16, 4), f"Actual size ({favicon_px}x{favicon_px} favicon, on tab-sized chips)", fill=fg, font=font)
    x, y = 16, title_h
    for i, crop in enumerate(cells):
        lw = label_widths[i]
        cw_chip = chip_pad_x + favicon_px + chip_inner_gap + lw + chip_pad_x
        draw.rounded_rectangle((x, y, x + cw_chip, y + chip_h), radius=6, fill=(40, 42, 50), outline=(80, 84, 100))
        fav = crop.resize((favicon_px, favicon_px), Image.LANCZOS)
        canvas.paste(fav, (x + chip_pad_x, y + (chip_h - favicon_px) // 2))
        label = f"{label_prefix} {i + 1}"
        lh = font.getbbox(label)[3]
        draw.text((x + chip_pad_x + favicon_px + chip_inner_gap, y + (chip_h - lh) // 2 - 2), label, fill=fg, font=font)
        x += cw_chip + chip_gap

    zy = chip_row_h + 4
    draw.text((16, zy), f"Zoomed {zoom}x (nearest-neighbor)", fill=fg, font=font)
    base_y = zy + title_h
    for i, crop in enumerate(cells):
        r, c = divmod(i, cols)
        zoomed = crop.resize((favicon_px, favicon_px), Image.LANCZOS).resize((zoom_size, zoom_size), Image.NEAREST)
        px = zoom_pad + c * (zoom_size + zoom_pad)
        py = base_y + zoom_pad + r * (zoom_size + zoom_pad)
        draw.rectangle((px - 2, py - 2, px + zoom_size + 2, py + zoom_size + 2), outline=(80, 84, 100))
        canvas.paste(zoomed, (px, py))

    dest.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(dest)
    return {"path": str(dest), "rows": rows, "cols": cols, "favicon_px": favicon_px, "zoom": zoom}


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
    image.save(dest)
    return {"path": str(dest), "placeholder": True, "reason": "dry-run"}


def _validate_size(size: str) -> str:
    match = re.fullmatch(r"([1-9][0-9]*)x([1-9][0-9]*)", size.strip())
    if not match:
        raise argparse.ArgumentTypeError("--size must be WIDTHxHEIGHT (e.g. 1024x1024)")
    w, h = int(match.group(1)), int(match.group(2))
    if w % 16 != 0 or h % 16 != 0:
        raise argparse.ArgumentTypeError("--size width/height must be multiples of 16 (gpt-image-2)")
    return f"{w}x{h}"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Iteratively edit grids: slice -> pick cells -> vary into a new grid.")
    p.add_argument("--from", dest="from_path", type=Path, required=True, help="Source grid image.")
    p.add_argument("--cells", required=True, type=str, help="Cell selection: '4', '1,2', '1-3,5', or 'all'.")
    p.add_argument("--ideas", required=True, help="Variation brief.")
    p.add_argument("--out", type=Path, required=True, help="Output directory.")
    p.add_argument("--count", type=int, default=DEFAULT_COUNT, help=f"Number of variations (1..{MAX_COUNT}, default {DEFAULT_COUNT}).")
    p.add_argument("--source-rows", type=int, default=None, help="Override source grid rows (default auto-detect).")
    p.add_argument("--source-cols", type=int, default=None, help="Override source grid cols (default auto-detect).")
    p.add_argument("--size", default=DEFAULT_SIZE, type=_validate_size, help=f"gpt-image-2 size WxH (default {DEFAULT_SIZE}).")
    add_choice_arg(p, "--quality", values=("low", "medium", "high", "auto"), default=DEFAULT_QUALITY, help=f"gpt-image-2 quality (default {DEFAULT_QUALITY}).")
    add_choice_arg(p, "--output-format", values=("png", "jpeg", "webp"), default="png", help="Output image format.")
    p.add_argument("--model", default=DEFAULT_FIREWORKS_MODEL, help="Fireworks chat model id.")
    p.add_argument("--no-kimi", action="store_true", help="Skip the Kimi expansion; send the brief verbatim.")
    p.add_argument("--env-file", type=Path, help="Env file with FIREWORKS_API_KEY and FAL_KEY.")
    p.add_argument("--dry-run", action="store_true", help="Plan + write ref crops, skip API calls.")
    p.add_argument("--favicon", action="store_true", help="Write a favicon-scale mockup (favicons.png) alongside grid.")
    p.add_argument("--favicon-size", type=int, default=16, help="Favicon pixel size for the mockup (default 16).")
    p.add_argument("--favicon-zoom", type=int, default=9, help="Zoom factor for the nearest-neighbor preview (default 9).")
    return p


def _validate_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if args.count < 1 or args.count > MAX_COUNT:
        parser.error(f"--count must be in 1..{MAX_COUNT}")
    if not args.from_path.is_file():
        parser.error(f"--from path does not exist: {args.from_path}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _validate_args(parser, args)

    out_root = args.out.expanduser().resolve()
    out_root.mkdir(parents=True, exist_ok=True)
    cells_dir = out_root / "source_cells"
    refs_dir = out_root / "refs"

    rows, cols = detect_source_layout(args.from_path, args.source_rows, args.source_cols)
    total_cells = rows * cols
    picks = parse_cells(args.cells, total_cells)
    cells = slice_grid(args.from_path, rows, cols, cells_dir)
    refs = write_refs(cells, picks, refs_dir)

    plan = {
        "tool": "vary_grid",
        "version": 1,
        "mode": "dry-run" if args.dry_run else "run",
        "from": str(args.from_path.resolve()),
        "source_layout": {"rows": rows, "cols": cols, "total": total_cells},
        "cells_spec": args.cells,
        "picked_cells": picks,
        "ref_paths": [r["path"] for r in refs],
        "ideas": args.ideas,
        "count": args.count,
        "size": args.size,
        "quality": args.quality,
        "output_format": args.output_format,
        "fal_edit_model_id": FAL_EDIT_MODEL_ID,
        "fireworks_model": None if args.no_kimi else args.model,
        "out": str(out_root),
    }
    write_json(out_root / "vary-plan.json", plan)

    if args.no_kimi:
        concepts: list[dict[str, Any]] = []
        concepts_payload = {"mode": "no-kimi", "concepts": []}
        grid_prompt = build_no_kimi_prompt(args.ideas, args.count, len(refs))
    elif args.dry_run:
        concepts = _planned_concepts(args.ideas, args.count)
        concepts_payload = {"mode": "dry-run", "concepts": concepts, "raw_response": None}
        grid_prompt = build_grid_prompt(args.ideas, concepts, len(refs))
    else:
        fireworks_key = CredentialsScope.get_local("fireworks", env_file=args.env_file)
        response = call_kimi_variations(
            ideas=args.ideas,
            count=args.count,
            ref_count=len(refs),
            model=args.model,
            api_key=fireworks_key,
        )
        raw = parse_concepts(response, count=args.count)
        concepts = renumber_concepts(raw)
        concepts_payload = {
            "mode": "run",
            "model": args.model,
            "usage": response.get("usage"),
            "concepts": concepts,
        }
        grid_prompt = build_grid_prompt(args.ideas, concepts, len(refs))

    write_json(out_root / "concepts.json", concepts_payload)
    write_json(
        out_root / "prompts.json",
        {
            "fal_edit_model_id": FAL_EDIT_MODEL_ID,
            "size": args.size,
            "quality": args.quality,
            "ref_count": len(refs),
            "grid_prompt": grid_prompt,
            "concepts": [
                {"candidate_id": c["candidate_id"], "name": c.get("name"), "prompt": c["prompt"]}
                for c in concepts
            ] if concepts else None,
        },
    )

    grid_path = out_root / f"grid.{args.output_format}"
    if args.dry_run:
        generated = _placeholder_image(grid_path, "vary-grid (dry-run)")
    else:
        fal_key = CredentialsScope.get_local("fal", env_file=args.env_file)
        ref_paths = [Path(r["path"]) for r in refs]
        submission, result = call_fal_edit(
            prompt=grid_prompt,
            image_paths=ref_paths,
            api_key=fal_key,
            size=args.size,
            quality=args.quality,
            output_format=args.output_format,
        )
        generated = save_fal_image(result, grid_path)
        generated["request_id"] = submission.get("request_id")

    manifest = {
        "version": 1,
        "mode": plan["mode"],
        "from": plan["from"],
        "picked_cells": picks,
        "refs": refs,
        "ideas": args.ideas,
        "count": args.count,
        "fal_edit_model_id": FAL_EDIT_MODEL_ID,
        "fireworks_model": plan["fireworks_model"],
        "size": args.size,
        "quality": args.quality,
        "grid": {
            "path": generated.get("path"),
            "bytes": generated.get("bytes"),
            "placeholder": bool(generated.get("placeholder")),
            "prompt": grid_prompt,
        },
        "concepts": concepts,
    }
    if args.favicon and not args.dry_run:
        fav_cols = max(1, math.ceil(math.sqrt(args.count)))
        fav_rows = math.ceil(args.count / fav_cols)
        fav = render_favicon_mockup(
            grid_path=Path(generated["path"]),
            rows=fav_rows,
            cols=fav_cols,
            dest=out_root / "favicons.png",
            favicon_px=args.favicon_size,
            zoom=args.favicon_zoom,
        )
        manifest["favicons"] = fav
        print(f"wrote_favicons={fav['path']}")

    write_json(out_root / "vary-manifest.json", manifest)
    receipt_outputs = {
        "plan": "vary-plan.json",
        "concepts": "concepts.json",
        "prompts": "prompts.json",
        "manifest": "vary-manifest.json",
        "references": "refs",
    }
    if grid_path.is_file():
        receipt_outputs["grid"] = grid_path.relative_to(out_root).as_posix()
    if (out_root / "favicons.png").is_file():
        receipt_outputs["favicons"] = "favicons.png"
    write_action_receipt(
        out_root,
        kind="video_editing.vary_grid",
        inputs={"from_path": str(args.from_path), "cells": args.cells, "ideas": args.ideas},
        output_paths=receipt_outputs,
        primary_port="grid" if grid_path.is_file() else None,
    )

    print(f"wrote_vary_manifest={out_root / 'vary-manifest.json'}")
    print(f"wrote_grid={generated.get('path')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
