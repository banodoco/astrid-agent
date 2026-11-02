#!/usr/bin/env python3
"""Claude transcript scouting for brief-agnostic quote candidates."""


from __future__ import annotations

from astrid.pack_api import AstridValueError
from astrid.pack_api import guard_canonical_entrypoint, run_pack_main

guard_canonical_entrypoint('editorial.quote_scout')
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from astrid.pack_api import build_manifest, write_manifest
from astrid.pack_api import register_outputs
from astrid.pack_api import ClaudeClient, build_claude_client
from astrid.pack_api import utc_now_seconds

QUOTE_CANDIDATES_VERSION = 1
RESPONSE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "candidates": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "segment_ids": {"type": "array", "items": {"type": "integer", "minimum": 0}},
                    "text": {"type": "string"},
                    "speaker": {"type": ["string", "null"]},
                    "theme": {"type": "string"},
                    "power": {"type": "integer", "minimum": 1, "maximum": 5},
                    "quote_kind": {"type": "string"},
                },
                "required": ["segment_ids", "text", "speaker", "theme", "power", "quote_kind"],
            },
        }
    },
    "required": ["candidates"],
}
SYSTEM_PROMPT = (
    "You are scouting reusable quote candidates from a source transcript. "
    "Return only segment_ids, text, speaker, theme, power, and quote_kind. "
    "Never return timestamps, seconds, or source ranges."
)


def validate_quote_candidates(payload: Any) -> None:
    if not isinstance(payload, dict):
        raise AstridValueError("quote_candidates payload must be an object")
    if payload.get("version") != QUOTE_CANDIDATES_VERSION:
        raise AstridValueError(f"quote_candidates.version must be {QUOTE_CANDIDATES_VERSION}")
    generated_at = payload.get("generated_at")
    if not isinstance(generated_at, str) or not generated_at.endswith("Z"):
        raise AstridValueError("quote_candidates.generated_at must be a UTC timestamp ending in 'Z'")
    candidates = payload.get("candidates")
    if not isinstance(candidates, list):
        raise AstridValueError("quote_candidates.candidates must be a list")
    for index, candidate in enumerate(candidates):
        path = f"quote_candidates.candidates[{index}]"
        if not isinstance(candidate, dict):
            raise AstridValueError(f"{path} must be an object")
        if set(candidate) != {"segment_ids", "text", "speaker", "theme", "power", "quote_kind"}:
            raise AstridValueError(f"{path} has unexpected keys")
        segment_ids = candidate.get("segment_ids")
        if not isinstance(segment_ids, list) or not segment_ids or not all(isinstance(segment_id, int) and segment_id >= 0 for segment_id in segment_ids):
            raise AstridValueError(f"{path}.segment_ids must be a non-empty list of 0-based integers")
        for field in ("text", "theme", "quote_kind"):
            if not isinstance(candidate.get(field), str) or not candidate[field]:
                raise AstridValueError(f"{path}.{field} must be a non-empty string")
        speaker = candidate.get("speaker")
        if speaker is not None and not isinstance(speaker, str):
            raise AstridValueError(f"{path}.speaker must be a string or null")
        power = candidate.get("power")
        if not isinstance(power, int) or power < 1 or power > 5:
            raise AstridValueError(f"{path}.power must be an integer from 1 to 5")


def _transcript_digest(transcript: dict[str, Any] | list[dict[str, Any]]) -> str:
    segments = transcript.get("segments") if isinstance(transcript, dict) else transcript
    if not isinstance(segments, list):
        raise AstridValueError("transcript must be a list or an object with segments")
    lines = [
        f"{index}: speaker={segment.get('speaker')!r} text={str(segment.get('text', '')).strip()}"
        for index, segment in enumerate(segments)
        if isinstance(segment, dict)
    ]
    return "\n".join(lines)


def build_quote_candidates(
    transcript: dict[str, Any] | list[dict[str, Any]],
    *,
    client: ClaudeClient,
    model: str = "claude-sonnet-4-6",
) -> dict[str, Any]:
    transcript_text = _transcript_digest(transcript)
    # TODO: split very long transcripts with overlap windows if future sources exceed ADOS-scale context.
    response = client.complete_json(
        model=model,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": transcript_text}],
        response_schema=RESPONSE_SCHEMA,
        max_tokens=4000,
    )
    candidates = response.get("candidates")
    if not isinstance(candidates, list):
        raise AstridValueError("Claude quote scout response is missing candidates")
    payload = {"version": QUOTE_CANDIDATES_VERSION, "generated_at": utc_now_seconds(), "candidates": candidates}
    validate_quote_candidates(payload)
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Scout reusable quote candidates from a transcript.")
    parser.add_argument("--transcript", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--env-file", dest="env_file", type=Path)
    parser.add_argument("--model", default="claude-sonnet-4-6")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    def _run() -> int:
        parser = build_parser()
        args = parser.parse_args(argv)
        transcript = json.loads(args.transcript.read_text(encoding="utf-8"))
        payload = build_quote_candidates(transcript, client=build_claude_client(args.env_file), model=args.model)
        out_dir = args.out.resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / "quote_candidates.json"
        out_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        register_outputs(
            stage="quote_scout",
            outputs=[("quote_candidates", out_path, "Quote candidates")],
            metadata={"model": args.model, "candidates": len(payload.get("candidates", []))},
        )
        print(out_path)

        # --- universal result manifest -----------------------
        manifest_outputs: list[dict[str, Any]] = [
            {"path": str(out_path), "type": "file"},
        ]
        manifest_path = out_dir / "manifest.json"
        manifest = build_manifest(
            kind="quotes",
            inputs={
                "transcript": str(args.transcript.resolve()),
                "model": args.model,
            },
            outputs=manifest_outputs,
            created=datetime.now(timezone.utc).isoformat(),
        )
        write_manifest(manifest_path, manifest)
        # -------------------------------------------------------------------------

        return 0

    return run_pack_main("editorial.quote_scout", _run, argv=list(argv) if argv is not None else None)


if __name__ == "__main__":
    raise SystemExit(main())
