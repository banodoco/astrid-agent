"""Canonical executor entrypoint for H3 candidate composition."""

from __future__ import annotations

from astrid.core.contracts.errors import AstridValueError
import argparse
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

from astrid.core._shared.result_manifest import build_manifest, write_manifest
from astrid.core.pack.entrypoint import guard_canonical_entrypoint, run_pack_main
from astrid.packs.h3_av.shared.input_bundle import resolve_preparation_assets

from astrid.packs.h3_av.shared.compose import compose_candidate


def _video_media_type(path: Path) -> str:
    """Return the stable MIME identity used again by final publication."""
    with path.open("rb") as handle:
        header = handle.read(16)
    if header.startswith(b"\x1aE\xdf\xa3"):
        return "video/x-matroska"
    if len(header) >= 12 and header[4:8] == b"ftyp":
        return "video/quicktime" if header[8:12] == b"qt  " else "video/mp4"
    raise AstridValueError("composed candidate must be an MP4/QuickTime or Matroska video container")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Compose an H3 audiovisual candidate.")
    parser.add_argument("--preparation", type=Path, required=True)
    parser.add_argument("--input-bundle", type=Path)
    parser.add_argument("--generated", type=Path, required=True)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--preservation-evidence", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result_manifest_path = args.out / "manifest.json"
    # A reused receipt cannot certify this attempt, even if domain work fails.
    # Preserve stale bytes and require a fresh assigned output root.
    if result_manifest_path.exists() or result_manifest_path.is_symlink():
        raise FileExistsError(f"result receipt already exists: {result_manifest_path}; use a fresh output root")
    preparation = json.loads(args.preparation.read_text(encoding="utf-8"))
    if args.input_bundle is not None:
        preparation = resolve_preparation_assets(
            preparation, args.input_bundle, args.out / "request-assets"
        )
    evidence = None
    if args.preservation_evidence:
        evidence = json.loads(args.preservation_evidence.read_text(encoding="utf-8"))
    composition = compose_candidate(
        preparation=preparation,
        generated=args.generated,
        source=args.source,
        out_dir=args.out,
        preservation_evidence=evidence,
    )
    # Fixed declared output name, without pretending every container is MKV.
    # Decoders detect the container from bytes; preserve the original bytes.
    candidate = args.out / "candidate.media"
    original = Path(composition["candidate"]["path"])
    if original.resolve() != candidate.resolve():
        shutil.copyfile(original, candidate)
    composition["candidate"]["path"] = str(candidate.resolve())
    # Runtime keys stored objects by content digest and requires their MIME
    # metadata to remain stable. Declare the actual container here so the
    # finalizer can republish the same bytes without reclassifying the object.
    media_type = _video_media_type(candidate)
    (args.out / "composition-manifest.json").write_text(
        json.dumps(composition, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    write_manifest(
        result_manifest_path,
        build_manifest(
            kind="h3_av.compose",
            inputs={
                "preparation": str(args.preparation),
                "input_bundle": str(args.input_bundle),
                "generated": str(args.generated),
                "source": str(args.source) if args.source is not None else None,
                "preservation_evidence": str(args.preservation_evidence) if args.preservation_evidence is not None else None,
            },
            outputs=[
                {"name": "candidate", "path": "candidate.media", "ordinal": 0, "media_type": media_type},
                {"name": "composition", "path": "composition-manifest.json", "ordinal": 1},
            ],
            created=datetime.now(timezone.utc).isoformat(),
        ),
    )
    print(f"h3_av.compose: wrote {args.out / 'composition-manifest.json'}")
    return 0


if __name__ == "__main__":
    guard_canonical_entrypoint("h3_av.compose")
    raise SystemExit(run_pack_main("h3_av.compose", lambda: main()))
