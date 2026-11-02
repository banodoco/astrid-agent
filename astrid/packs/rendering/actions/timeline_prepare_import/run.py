"""Read-only timeline import preflight and commit-body preparation."""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from astrid.pack_api import guard_canonical_entrypoint
from astrid.packs.rendering.actions.timeline_transfer_common import (
    MAX_MANIFEST_INLINE_BYTES,
    decode_manifest,
    output_root,
    read_json_file,
    run_cli,
    write_artifact,
    write_receipt,
)
from astrid.sdk.timeline_transfer import check_import_manifest, prepare_import

guard_canonical_entrypoint("rendering.timeline_prepare_import")


def build_prepare_import_artifacts(
    *, project: str, out_dir: str | Path, manifest_json: str | None = None,
    manifest_file: str | None = None, payload_file: str | None = None,
    name: str | None = None, check_only: bool = False,
) -> dict[str, Any]:
    if not isinstance(project, str) or not project.strip():
        raise ValueError("project must be a resolved target project id")
    root = output_root(str(out_dir))
    manifest = decode_manifest(manifest_json, manifest_file)
    differences = check_import_manifest(manifest) if check_only else None
    inputs = {"project": project, "check_only": bool(check_only)}
    if name:
        inputs["name"] = name
    if manifest_file:
        inputs["manifest_file"] = Path(manifest_file).name
    else:
        inputs["manifest_bytes"] = len((manifest_json or "").encode("utf-8"))

    output_entries: list[dict[str, Any]] = []
    if check_only:
        summary = {
            "check_only": True,
            "project": project,
            "name": name or str(manifest.get("name") or "Imported timeline"),
            "missing_effects": differences["missing_effects"],
            "different_effects": differences["different_effects"],
        }
    else:
        if not payload_file:
            raise ValueError("payload is required unless check_only=true")
        payload = read_json_file(payload_file, "payload")
        if not isinstance(payload, dict) or "timeline" not in payload or "transcripts" not in payload:
            raise ValueError("payload JSON must be an object containing timeline and transcripts")
        prepared = prepare_import(
            payload["timeline"], manifest, payload["transcripts"], project, name=name,
        )
        prepared_path = root / "prepared.json"
        write_artifact(prepared_path, prepared)
        output_entries.append({
            "name": "prepared", "path": prepared_path.name, "role": "result",
            "is_primary": True, "type": "file",
        })
        publication = prepared["publication"]
        dependencies = publication.get("dependency_manifest") or {}
        media = dependencies.get("media") or []
        summary = {
            "check_only": False,
            "project": project,
            "name": prepared["name"],
            "timeline_id": prepared["timeline_id"],
            "shot_count": len(publication.get("shot_revisions") or []),
            "internal_timeline_count": len(publication.get("internal_timeline_revisions") or []),
            "media_count": len(media),
            "transcript_count": len(prepared.get("transcripts") or []),
            "missing_effects": prepared["differences"]["missing_effects"],
            "different_effects": prepared["differences"]["different_effects"],
        }

    result_path = root / "result.json"
    write_artifact(result_path, summary)
    output_entries.append({
        "name": "result", "path": result_path.name,
        "role": "auxiliary" if output_entries else "result",
        "is_primary": not output_entries, "type": "file",
    })
    write_receipt(root, kind="rendering.timeline_prepare_import", inputs=inputs, outputs=output_entries)
    return summary


def main(argv: list[str] | None = None) -> int:
    def _run() -> int:
        parser = argparse.ArgumentParser(description="Validate or prepare a timeline import.")
        parser.add_argument("--project", required=True)
        parser.add_argument("--name")
        parser.add_argument("--manifest")
        parser.add_argument("--manifest-file")
        parser.add_argument("--payload")
        parser.add_argument("--check-only", action="store_true")
        parser.add_argument("--out", required=True)
        args = parser.parse_args(argv)
        if args.manifest and len(args.manifest.encode("utf-8")) > MAX_MANIFEST_INLINE_BYTES:
            raise ValueError(f"manifest exceeds {MAX_MANIFEST_INLINE_BYTES} encoded bytes; pass manifest_file")
        build_prepare_import_artifacts(
            project=args.project, out_dir=args.out, manifest_json=args.manifest,
            manifest_file=args.manifest_file, payload_file=args.payload,
            name=args.name, check_only=args.check_only,
        )
        return 0

    return run_cli("rendering.timeline_prepare_import", _run, argv)


if __name__ == "__main__":
    raise SystemExit(main())
