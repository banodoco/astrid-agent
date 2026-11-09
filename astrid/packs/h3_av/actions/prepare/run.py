"""Canonical executor entrypoint for H3 request preparation."""

from __future__ import annotations

from astrid.core.contracts.errors import AstridValueError
import argparse
from datetime import datetime, timezone
from pathlib import Path

from astrid.core._shared.result_manifest import build_manifest, write_manifest
from astrid.core.pack.entrypoint import guard_canonical_entrypoint, run_pack_main

from astrid.packs.h3_av.actions.prepare.prepare import prepare_request, write_preparation
from astrid.packs.h3_av.shared.request import load_request
from astrid.packs.h3_av.shared.input_bundle import bundle_digest, materialize_input_bundle

from astrid.core.generation.engine_facts import comfyui_facts as _comfyui_facts
from astrid.packs.h3_av.shared import engine_facts as _h3_engine_facts

_h3_engine_facts.install(_comfyui_facts())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Prepare an H3 audiovisual request.")
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--input-bundle", type=Path, required=True)
    parser.add_argument("--fps", type=float, default=24.0)
    parser.add_argument("--width", type=int, default=1024)
    parser.add_argument("--height", type=int, default=576)
    parser.add_argument("--sample-rate", type=int, default=48000)
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
    request = load_request(args.request)
    asset_map, identities = materialize_input_bundle(
        request, args.input_bundle, args.out.parent / "request-assets"
    )
    manifest = prepare_request(
        request,
        asset_map=asset_map,
        fps=args.fps,
        width=args.width,
        height=args.height,
        sample_rate=args.sample_rate,
    )
    manifest["assets"] = identities
    manifest["input_bundle_sha256"] = bundle_digest(args.input_bundle)
    write_preparation(args.out, manifest)
    write_manifest(
        result_manifest_path,
        build_manifest(
            kind="h3_av.prepare",
            inputs={
                "request": str(args.request),
                "input_bundle": str(args.input_bundle),
                "fps": args.fps,
                "width": args.width,
                "height": args.height,
                "sample_rate": args.sample_rate,
            },
            outputs=[
                {"name": "preparation", "path": args.out.name, "ordinal": 0},
            ],
            created=datetime.now(timezone.utc).isoformat(),
        ),
    )
    print(f"h3_av.prepare: wrote {args.out}")
    return 0


if __name__ == "__main__":
    guard_canonical_entrypoint("h3_av.prepare")
    raise SystemExit(run_pack_main("h3_av.prepare", lambda: main()))
