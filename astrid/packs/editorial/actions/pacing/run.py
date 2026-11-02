#!/usr/bin/env python3
"""Render a rhythm sheet from one admitted immutable timeline bundle."""

from __future__ import annotations

from astrid.pack_api import guard_canonical_entrypoint, run_pack_main

guard_canonical_entrypoint("editorial.pacing")

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from astrid.pack_api import build_manifest, write_manifest
from astrid.pack_api import AstridError
from astrid.packs.editorial.actions.pacing import model, reader, sheet


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="editorial.pacing", description="Render a timeline rhythm sheet.")
    parser.add_argument("--project", required=True, help="Owning project identity supplied by the invocation.")
    parser.add_argument("--timeline-ref", required=True, help="Timeline slug, UUID, or ULID bound by the invocation.")
    parser.add_argument("--window", default=None, help="Optional [start, end] seconds, or 'start,end'.")
    parser.add_argument("--bundle", type=Path, help="Host-admitted immutable authoring bundle JSON.")
    parser.add_argument("--out", type=Path, required=True, help="Output directory.")
    return parser


def _parse_window(text: str | None) -> list[float] | None:
    if text is None or not str(text).strip():
        return None
    raw = str(text).strip()
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        value = raw.split(",")
    if not isinstance(value, list) or len(value) != 2:
        raise AstridError(
            f"window must be [start, end] seconds, got {text!r}",
            recovery_command="pass --window '[0, 5]' or --window '0,5'",
        )
    try:
        return [float(value[0]), float(value[1])]
    except (TypeError, ValueError) as exc:
        raise AstridError(
            f"window values must be numbers, got {text!r}",
            recovery_command="pass --window '[0, 5]' or --window '0,5'",
        ) from exc


def _canvas(bundle: Mapping[str, Any]) -> dict[str, Any]:
    config = bundle.get("parent", {}).get("config", {}) if isinstance(bundle.get("parent"), Mapping) else {}
    visual = (config.get("theme_overrides") or {}).get("visual") or {}
    canvas = visual.get("canvas") or {}
    return {
        "width": int(canvas.get("width") or 1920),
        "height": int(canvas.get("height") or 1080),
        "fps": canvas.get("fps") or 30,
    }


def _beats_value(bundle: Mapping[str, Any]) -> Any:
    parent = bundle.get("parent")
    config = parent.get("config") if isinstance(parent, Mapping) else None
    app = config.get("app") if isinstance(config, Mapping) else None
    return app.get("beats") if isinstance(app, Mapping) else None


def main(argv: list[str] | None = None) -> int:
    def _run() -> int:
        args = build_parser().parse_args(argv)
        out_dir = args.out.expanduser().resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
        window = _parse_window(args.window)
        try:
            bundle, identity = reader.read_bundle(
                args.project,
                args.timeline_ref,
                admitted_bundle=args.bundle,
            )
            beats = model.load_beats(_beats_value(bundle))
            timeline = {
                "ref": args.timeline_ref,
                "project": args.project,
                **identity,
                "canvas": _canvas(bundle),
            }
            rhythm = model.build_rhythm(bundle, timeline=timeline, window=window, beats=beats)
        except (model.PacingError, reader.PacingReadError) as exc:
            raise AstridError(
                str(exc),
                recovery_command="supply the Runtime-admitted bundle and check --window",
            ) from exc

        sheet.render_png(rhythm, out_dir / "pacing.png")
        (out_dir / "pacing.md").write_text(sheet.render_markdown(rhythm), encoding="utf-8")
        (out_dir / "pacing.json").write_text(json.dumps(rhythm, indent=2, sort_keys=True) + "\n", encoding="utf-8")

        manifest = build_manifest(
            kind="editorial_pacing",
            inputs={
                "project": args.project,
                "timeline_ref": args.timeline_ref,
                "window": window,
                "head_revision_id": identity["head_revision_id"],
                "beats_linked": beats is not None,
                "read_authority": "runtime-admitted-bundle",
            },
            outputs=[
                {"name": "pacing_png", "path": "pacing.png", "type": "file", "artifact_type": "image", "role": "result", "is_primary": True},
                {"name": "pacing_md", "path": "pacing.md", "type": "file", "role": "result"},
                {"name": "pacing_json", "path": "pacing.json", "type": "file", "role": "auxiliary"},
            ],
            created=datetime.now(timezone.utc).isoformat(),
        )
        write_manifest(out_dir / "manifest.json", manifest)
        return 0

    return run_pack_main("editorial.pacing", _run, argv=argv)


if __name__ == "__main__":
    raise SystemExit(main())
