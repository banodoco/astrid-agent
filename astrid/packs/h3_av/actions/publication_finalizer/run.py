"""Copy one verified H3 candidate into the Runtime publication spool."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

from astrid.core._shared.result_manifest import build_manifest, write_manifest
from astrid.core.contracts.errors import AstridValueError
from astrid.core.pack.entrypoint import guard_canonical_entrypoint, run_pack_main

from astrid.core.generation.engine_facts import comfyui_facts as _comfyui_facts
from astrid.packs.h3_av.shared import engine_facts as _h3_engine_facts

_h3_engine_facts.install(_comfyui_facts())


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _video_media_type(path: Path) -> str:
    with path.open("rb") as handle:
        header = handle.read(16)
    if header.startswith(b"\x1aE\xdf\xa3"):
        return "video/x-matroska"
    if len(header) >= 12 and header[4:8] == b"ftyp":
        return "video/quicktime" if header[8:12] == b"qt  " else "video/mp4"
    raise AstridValueError("verified candidate must be an MP4/QuickTime or Matroska video container")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--verification", type=Path, required=True)
    parser.add_argument("--expected-request-digest", required=True)
    parser.add_argument("--out", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    source = args.candidate.expanduser()
    if source.is_symlink() or not source.is_file():
        raise AstridValueError("candidate must be a regular managed file")
    source = source.resolve(strict=True)
    media_type = _video_media_type(source)
    verification_path = args.verification.expanduser()
    if verification_path.is_symlink() or not verification_path.is_file():
        raise AstridValueError("verification must be a regular managed file")
    verification_path = verification_path.resolve(strict=True)
    verification = json.loads(verification_path.read_text(encoding="utf-8"))
    if (
        not isinstance(verification, dict)
        or verification.get("schema_version") != 1
        or verification.get("kind") != "h3_av_verification"
        or verification.get("status") != "verified"
    ):
        raise AstridValueError("candidate publication requires a verified H3 verification report")
    expected_request_digest = args.expected_request_digest
    if not isinstance(expected_request_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_request_digest):
        raise AstridValueError("final publication intent has no valid request digest")
    request_digest = verification.get("request_digest")
    if request_digest != expected_request_digest:
        raise AstridValueError("verification request digest does not match final publication intent")
    source_digest = _sha256(source)
    if verification.get("candidate_sha256") != source_digest:
        raise AstridValueError("candidate digest does not match the verification report")
    lifecycle = verification.get("lifecycle")
    if not isinstance(lifecycle, dict) or any(lifecycle.get(key) is not True for key in ("prepared", "composed", "verified")):
        raise AstridValueError("verification report does not attest the prepared/composed/verified lifecycle")

    if args.out.is_symlink():
        raise AstridValueError("publication output directory must not be a symbolic link")
    out = args.out.expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)
    manifest_path = out / "manifest.json"
    if manifest_path.exists() or manifest_path.is_symlink():
        raise FileExistsError(f"publication result receipt already exists: {manifest_path}")
    candidate = out / "verified-candidate.media"
    if candidate.exists() or candidate.is_symlink():
        raise FileExistsError(f"publication output already exists: {candidate}")
    if source == candidate:
        raise AstridValueError("publication input must be distinct from the output spool")

    shutil.copyfile(source, candidate)
    digest = _sha256(candidate)
    if digest != source_digest:
        candidate.unlink(missing_ok=True)
        raise AstridValueError("candidate changed during publication finalization")

    digest_identity = f"sha256:{digest}"
    write_manifest(
        manifest_path,
        build_manifest(
            kind="h3_av.publication_finalizer",
            inputs={
                "candidate_sha256": digest_identity,
                "request_digest": request_digest,
                "verification_sha256": _sha256(verification_path),
            },
            outputs=[{
                "name": "verified_candidate",
                "path": candidate.name,
                "ordinal": 0,
                "content_hash": digest_identity,
                "bytes": candidate.stat().st_size,
                "media_type": media_type,
                "output_port": "verified_candidate",
                "group_key": "main",
                "variant_key": "final-composition",
                "selector": {"group_key": "main", "variant_key": "final-composition"},
                "role": "result",
                "is_primary": True,
            }],
            created=datetime.now(timezone.utc).isoformat(),
        ),
    )
    return 0


if __name__ == "__main__":
    guard_canonical_entrypoint("h3_av.publication_finalizer")
    raise SystemExit(run_pack_main("h3_av.publication_finalizer", lambda: main()))
