"""Canonical executor entrypoint for H3 candidate verification."""

from __future__ import annotations

from astrid.core.contracts.errors import AstridValueError
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from astrid.core._shared.result_manifest import build_manifest, write_manifest
from astrid.core.pack.entrypoint import guard_canonical_entrypoint, run_pack_main
from astrid.packs.h3_av.shared.input_bundle import resolve_preparation_assets

from astrid.packs.h3_av.actions.verify.verify import verify_candidate

from astrid.core.generation.engine_facts import comfyui_facts as _comfyui_facts
from astrid.packs.h3_av.shared import engine_facts as _h3_engine_facts

_h3_engine_facts.install(_comfyui_facts())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Verify an H3 audiovisual candidate.")
    parser.add_argument("--preparation", type=Path, required=True)
    parser.add_argument("--input-bundle", type=Path)
    parser.add_argument("--composition", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result_manifest_path = args.out.parent / "manifest.json"
    if args.out.is_symlink():
        raise AstridValueError("--out must not be a symbolic link")
    if args.out.name.casefold() == "manifest.json":
        raise AstridValueError("--out must not use the reserved result receipt filename manifest.json")
    # A reused receipt cannot certify this attempt, even if domain work fails.
    # Preserve stale bytes and require a fresh assigned output root.
    if result_manifest_path.exists() or result_manifest_path.is_symlink():
        raise FileExistsError(f"result receipt already exists: {result_manifest_path}; use a fresh output root")
    preparation = json.loads(args.preparation.read_text(encoding="utf-8"))
    if args.input_bundle is not None:
        preparation = resolve_preparation_assets(
            preparation, args.input_bundle, args.out.parent / "request-assets"
        )
    composition = json.loads(args.composition.read_text(encoding="utf-8"))
    report = verify_candidate(preparation=preparation, composition=composition, source=args.source, candidate=args.candidate)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    write_manifest(
        result_manifest_path,
        build_manifest(
            kind="h3_av.verify",
            inputs={
                "preparation": str(args.preparation),
                "input_bundle": str(args.input_bundle),
                "composition": str(args.composition),
                "candidate": str(args.candidate),
                "source": str(args.source) if args.source is not None else None,
            },
            outputs=[
                {"name": "verification", "path": args.out.name, "ordinal": 0},
            ],
            created=datetime.now(timezone.utc).isoformat(),
        ),
    )
    print(f"h3_av.verify: wrote {args.out}")
    return 0


if __name__ == "__main__":
    guard_canonical_entrypoint("h3_av.verify")
    raise SystemExit(run_pack_main("h3_av.verify", lambda: main()))
