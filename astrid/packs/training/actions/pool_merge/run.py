#!/usr/bin/env python3
"""Merge generative effect catalog entries into pool.json."""

# extends prior plan Step 10

from __future__ import annotations

from astrid.pack_api import guard_canonical_entrypoint

guard_canonical_entrypoint('training.pool_merge')
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from astrid.core import timeline
from astrid.pack_api import build_manifest, write_manifest
from astrid.pack_api import register_outputs
from astrid.core.element import catalog as effects_catalog
from astrid.pack_api import utc_now_seconds


def _stable_pool_id(effect_id: str) -> str:
    return f"pool_g_{effect_id.replace('-', '_')}"


def _skeleton() -> dict[str, Any]:
    return {
        "version": timeline.POOL_VERSION,
        "generated_at": utc_now_seconds(),
        "entries": [],
    }


def _entry_for_effect(effect_id: str) -> dict[str, Any]:
    return {
        "id": _stable_pool_id(effect_id),
        "kind": "generative",
        "category": "visual",
        "effect_id": effect_id,
        "param_schema": effects_catalog.read_effect_schema(effect_id),
        "defaults": effects_catalog.read_effect_defaults(effect_id),
        "meta": effects_catalog.read_effect_meta(effect_id),
        "duration": None,
        "scores": {},
        "excluded": False,
    }


def merge_pool(pool: dict[str, Any], *, theme: str | Path | None = None) -> dict[str, Any]:
    del theme
    effect_ids = effects_catalog.list_effect_ids()
    effect_id_set = set(effect_ids)
    upserts = {_stable_pool_id(effect_id): _entry_for_effect(effect_id) for effect_id in effect_ids}
    entries: list[dict[str, Any]] = []
    replaced: set[str] = set()
    kept_stale_theme_entries = False
    for entry in pool.get("entries", []):
        if not isinstance(entry, dict):
            continue
        entry_id = entry.get("id")
        if isinstance(entry_id, str) and entry_id.startswith("pool_g_"):
            if entry.get("effect_id") not in effect_id_set:
                # Follow-up: no-theme runs intentionally leave stale theme pool entries
                # in place because the previous active theme is not encoded in pool ids.
                if theme is not None:
                    continue
                entries.append(entry)
                kept_stale_theme_entries = True
                continue
            entries.append(upserts[entry_id])
            replaced.add(entry_id)
            continue
        entries.append(entry)
    for entry_id in sorted(set(upserts) - replaced):
        entries.append(upserts[entry_id])
    merged = dict(pool)
    merged["version"] = timeline.POOL_VERSION
    merged.setdefault("generated_at", utc_now_seconds())
    merged["entries"] = entries
    if not kept_stale_theme_entries:
        timeline.validate_pool(merged)
    return merged


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Merge workspace and optional theme generative effects into pool.json.")
    parser.add_argument("--pool", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--theme", help="Theme id, theme directory, or path to theme.json.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    pool = timeline.load_pool(args.pool) if args.pool.exists() else _skeleton()
    merged = merge_pool(pool, theme=args.theme)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")
    register_outputs(
        stage="pool_merge",
        outputs=[("pool", args.out, "Merged pool")],
        metadata={"entries": len(merged.get("entries", [])), "theme": args.theme},
    )
    print(args.out)

    # --- universal result manifest -----------------------
    out_file = args.out.resolve()
    manifest_path = out_file.parent / "manifest.json"
    manifest = build_manifest(
        kind="pool_merge",
        inputs={
            "pool": str(args.pool.resolve()) if args.pool.exists() else None,
            "theme": args.theme,
        },
        outputs=[
            {"path": out_file.name, "type": "file"},
        ],
        created=datetime.now(timezone.utc).isoformat(),
        schema_version=1,
        warnings=[],
    )
    write_manifest(manifest_path, manifest)
    # -------------------------------------------------------------------------

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
