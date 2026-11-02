#!/usr/bin/env python3
# The canonical-entrypoint guard intentionally runs before imports.
# ruff: noqa: E402

from __future__ import annotations

from astrid.pack_api import guard_canonical_entrypoint

guard_canonical_entrypoint("typed_timeline.map")

import argparse
import json
import sys
from pathlib import Path

from astrid.pack_api import build_manifest, write_manifest
from astrid.pack_api import run_pack_main
from astrid.packs.typed_timeline.common import (
    confined_output_path,
    ensure_tone_wav,
    load_admitted_rows,
    portable_input_ref,
    resolve_mapping_path,
)


def main(argv=None) -> int:
    def _run() -> int:
        parser = argparse.ArgumentParser()
        parser.add_argument("--source", required=True)
        parser.add_argument("--mapping", required=True)
        parser.add_argument("--out", required=True, type=Path)
        parser.add_argument("--run-id", dest="run_id", default=None)
        parser.add_argument("--project", default=None)
        parser.add_argument("--json-path", dest="json_path", default=None)
        parser.add_argument("--json-rows", dest="json_rows", default=None)
        args = parser.parse_args(argv)

        out_dir = Path(args.out)
        out_dir.mkdir(parents=True, exist_ok=True)

        # Project identity is supplied by kernel admission (environment) or an
        # explicit internal invocation.  This pack never guesses an owner.
        from astrid.pack_api import resolve_projects_root
        from astrid.pack_api import selected_project

        projects_root = resolve_projects_root(None)
        project_id, _ = selected_project(args.project)

        try:
            rows = load_admitted_rows(
                source=args.source,
                json_path=args.json_path,
                json_rows=args.json_rows,
                project=project_id,
                projects_root=projects_root,
                run_id=args.run_id,
            )
            mapping_path = resolve_mapping_path(
                args.mapping, project=project_id, projects_root=projects_root
            )
        except Exception as exc:  # noqa: BLE001 - CLI boundary reports rejected input
            print(f"typed timeline input rejected: {exc}", file=sys.stderr)
            return 1

        from astrid.packs.typed_timeline.mapper import TypedDataTimelineMapper

        mapper = TypedDataTimelineMapper(rows, mapping_path)
        timeline = mapper.to_timeline()
        assets = mapper.to_assets()

        timeline_path = confined_output_path(out_dir, "timeline.json")
        assets_path = confined_output_path(out_dir, "assets.json")

        # Materialize only project-run staging outputs.  Mapping-controlled
        # asset paths cannot escape ``out_dir``.
        total_sec = mapper.total_duration_sec
        assets_file = (
            assets.get("assets", {}).get("audio", {}).get("file")
            if isinstance(assets.get("assets"), dict)
            else None
        )
        try:
            tone_path = confined_output_path(out_dir, assets_file or "tone.wav")
            ensure_tone_wav(tone_path, total_sec)
        except Exception as exc:  # noqa: BLE001 - CLI boundary reports output failures
            print(f"typed timeline output rejected: {exc}", file=sys.stderr)
            return 1

        timeline_path.write_text(
            json.dumps(timeline, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        assets_path.write_text(
            json.dumps(assets, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

        # write manifest
        manifest_path = out_dir / "manifest.json"
        from astrid.pack_api import utc_now_iso

        manifest = build_manifest(
            kind="typed_timeline.map",
            inputs={
                "source": args.source,
                "mapping": portable_input_ref(args.mapping, projects_root=projects_root),
                "run_id": args.run_id,
                "project": project_id,
            },
            outputs=[
                {"path": timeline_path.relative_to(out_dir.resolve()).as_posix(), "type": "file"},
                {"path": assets_path.relative_to(out_dir.resolve()).as_posix(), "type": "file"},
            ],
            created=utc_now_iso(),
        )
        # include tone.wav if exists
        if tone_path.exists():
            manifest["outputs"].append(
                {
                    "path": tone_path.relative_to(out_dir.resolve()).as_posix(),
                    "type": "file",
                }
            )

        write_manifest(manifest_path, manifest)
        return 0

    return run_pack_main("typed_timeline.map", _run, argv=argv)


if __name__ == "__main__":
    raise SystemExit(main())
