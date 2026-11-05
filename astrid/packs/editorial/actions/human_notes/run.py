#!/usr/bin/env python3
"""Translate human revision notes into editor_review.json."""

from __future__ import annotations

from astrid.pack_api import guard_canonical_entrypoint

guard_canonical_entrypoint('editorial.human_notes')

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from astrid.pack_api import build_manifest, write_manifest
from astrid.pack_api import executor_argv
from astrid.pack_api import require_runtime_materialized_file
from astrid.pack_api import load_arrangement, load_pool
from astrid.pack_api import ClaudeClient, build_claude_client
from astrid.packs.editorial.actions.arrange.run import pool_digest
from astrid.packs.editorial.actions.editor_review.run import (
    DEFAULT_MODEL,
    RESPONSE_SCHEMA,
    _validate_editor_notes,
    _validate_review_payload_shape,
    arrangement_summary,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Translate free-text human revision instructions into editor_review.json."
    )
    parser.add_argument("--instructions", type=Path, required=True, help="Plain-text human revision instructions.")
    parser.add_argument("--arrangement", type=Path, required=True, help="Existing arrangement.json.")
    parser.add_argument("--pool", type=Path, required=True, help="Existing pool.json.")
    parser.add_argument("--out", type=Path, required=True, help="Output directory for editor_review.json.")
    parser.add_argument("--iteration", type=int, default=1, help="Editor review iteration to write into the payload.")
    parser.add_argument("--env-file", type=Path, help="Optional environment file for LLM credentials.")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Claude model to use.")
    parser.add_argument("--apply", action="store_true", help="Reserved for chaining the revise pipeline.")
    parser.add_argument("--brief", type=Path, help="Brief text file required when --apply is set.")
    parser.add_argument("--brief-dir", type=Path, help="Brief output directory required when --apply is set.")
    parser.add_argument("--run-dir", type=Path, help="Source run directory required when --apply is set.")
    parser.add_argument("--video", type=str, help="Runtime-materialized primary source video for cut.py.")
    parser.add_argument("--asset", action="append", default=[], help="Additional source asset in KEY=PATH form.")
    parser.add_argument("--primary-asset", help="Primary asset key for cut.py.")
    parser.add_argument("--shots", type=Path, help="Optional shots.json path for cut.py.")
    parser.add_argument("--python-exec", default=sys.executable, help="Python interpreter for --apply subprocesses.")
    return parser


def build_system_prompt(
    *,
    arrangement: dict[str, Any],
    pool: dict[str, Any],
    instructions_text: str,
) -> str:
    return "\n\n".join(
        [
            (
                "You are translating human editorial revision instructions into structured "
                "editor_review JSON. Use only clip_uuid values copied exactly from the "
                "arrangement listing. Choose the correct action and action_detail shape for "
                "each note, set priority and brief_impact from the human intent, and omit "
                "notes that cannot be grounded in the arrangement or pool."
            ),
            "ARRANGEMENT LISTING:\n" + arrangement_summary(arrangement),
            "POOL DIGEST:\n" + pool_digest(pool),
            "HUMAN INSTRUCTIONS:\n" + instructions_text.strip(),
        ]
    )


def _pool_ids(pool: dict[str, Any]) -> set[str]:
    return {
        entry["id"]
        for entry in pool.get("entries", [])
        if isinstance(entry, dict) and isinstance(entry.get("id"), str)
    }


def _step_argv(executor_id: str, python_exec: str) -> list[str]:
    return executor_argv(executor_id, python_exec)


def _parse_asset_entry(parser: argparse.ArgumentParser, raw: str) -> tuple[str, Path]:
    if "=" not in raw:
        parser.error(f"invalid --asset value {raw!r}; expected KEY=PATH")
    key, path_text = raw.split("=", 1)
    key = key.strip()
    path_text = path_text.strip()
    if not key or not path_text:
        parser.error(f"invalid --asset value {raw!r}; expected KEY=PATH")
    if key == "main":
        parser.error("asset key 'main' is reserved; pass the primary video via --video")
    try:
        return key, require_runtime_materialized_file(path_text, label=f"asset {key!r}")
    except ValueError as exc:
        parser.error(str(exc))
        raise AssertionError("argparse.error did not exit")


def _asset_args(asset_pairs: list[tuple[str, Path]]) -> list[str]:
    args: list[str] = []
    for key, path in asset_pairs:
        args.extend(["--asset", f"{key}={path}"])
    return args


def _require_file(parser: argparse.ArgumentParser, path: Path | None, flag: str) -> Path:
    if path is None:
        parser.error(f"--apply requires {flag}")
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        parser.error(f"{flag} must be an existing file: {resolved}")
    return resolved


def _require_dir(parser: argparse.ArgumentParser, path: Path | None, flag: str) -> Path:
    if path is None:
        parser.error(f"--apply requires {flag}")
    resolved = path.expanduser().resolve()
    if not resolved.is_dir():
        parser.error(f"{flag} must be an existing directory: {resolved}")
    return resolved


def _validate_apply_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if not args.apply:
        return

    args.brief = _require_file(parser, args.brief, "--brief")
    args.brief_dir = _require_dir(parser, args.brief_dir, "--brief-dir")
    args.run_dir = _require_dir(parser, args.run_dir, "--run-dir")

    for name in ("hype.timeline.json", "hype.assets.json", "hype.metadata.json"):
        path = args.brief_dir / name
        if not path.is_file():
            parser.error(f"--brief-dir is missing {name}: {path}")
    for name in ("scenes.json", "transcript.json"):
        path = args.run_dir / name
        if not path.is_file():
            parser.error(f"--run-dir is missing {name}: {path}")

    if args.video is not None:
        try:
            args.video = require_runtime_materialized_file(args.video, label="--video")
        except ValueError as exc:
            parser.error(str(exc))
    args.asset_pairs = [_parse_asset_entry(parser, item) for item in args.asset]
    if args.video is None and not args.asset_pairs:
        parser.error("--apply requires either --video or at least one --asset KEY=PATH")

    if args.primary_asset:
        asset_keys = {key for key, _ in args.asset_pairs}
        if args.primary_asset != "main" and args.primary_asset not in asset_keys:
            parser.error(
                f"--primary-asset={args.primary_asset!r} has no matching source asset; "
                "use 'main' with --video or provide a matching --asset KEY=PATH"
            )
        if args.primary_asset == "main" and args.video is None:
            parser.error("--primary-asset='main' requires --video")

    if args.shots is None:
        default_shots = args.run_dir / "shots.json"
        args.shots = default_shots if default_shots.is_file() else None
    else:
        args.shots = _require_file(parser, args.shots, "--shots")


def _apply_pipeline(args: argparse.Namespace) -> None:
    editor_notes_path = args.out / "editor_review.json"
    arrangement_path = args.brief_dir / "arrangement.json"
    scenes_path = args.run_dir / "scenes.json"
    transcript_path = args.run_dir / "transcript.json"
    timeline_path = args.brief_dir / "hype.timeline.json"
    assets_path = args.brief_dir / "hype.assets.json"
    metadata_path = args.brief_dir / "hype.metadata.json"

    arrange_cmd = [
        *_step_argv("editorial.arrange", args.python_exec),
        "--revise",
        "--pool",
        str(args.pool),
        "--brief",
        str(args.brief),
        "--out",
        str(args.brief_dir),
        "--from-arrangement",
        str(args.arrangement),
        "--editor-notes",
        str(editor_notes_path),
    ]
    if args.env_file:
        arrange_cmd.extend(["--env-file", str(args.env_file)])
    if args.model:
        arrange_cmd.extend(["--model", args.model])

    cut_cmd = [
        *_step_argv("video_editing.cut", args.python_exec),
        "--scenes",
        str(scenes_path),
        "--transcript",
        str(transcript_path),
        "--pool",
        str(args.pool),
        "--arrangement",
        str(arrangement_path),
        "--brief",
        str(args.brief),
    ]
    if args.video is not None:
        cut_cmd.extend(["--video", str(args.video)])
    cut_cmd.extend(["--out", str(args.brief_dir)])
    if args.shots is not None:
        cut_cmd.extend(["--shots", str(args.shots)])
    cut_cmd.extend(_asset_args(args.asset_pairs))
    if args.primary_asset:
        cut_cmd.extend(["--primary-asset", args.primary_asset])

    refine_cmd = [
        *_step_argv("editorial.refine", args.python_exec),
        "--arrangement",
        str(arrangement_path),
        "--pool",
        str(args.pool),
        "--timeline",
        str(timeline_path),
        "--assets",
        str(assets_path),
        "--metadata",
        str(metadata_path),
        "--transcript",
        str(transcript_path),
        "--out",
        str(args.brief_dir),
    ]
    if args.primary_asset:
        refine_cmd.extend(["--primary-asset", args.primary_asset])
    if args.env_file:
        refine_cmd.extend(["--env-file", str(args.env_file)])

    for cmd in (arrange_cmd, cut_cmd, refine_cmd):
        subprocess.run(cmd, check=True)



def main(argv: Sequence[str] | None = None, *, client: ClaudeClient | None = None) -> Path:
    parser = build_parser()
    args = parser.parse_args(argv)
    _validate_apply_args(parser, args)

    pool = load_pool(args.pool)
    arrangement = load_arrangement(args.arrangement, _pool_ids(pool))
    instructions_text = args.instructions.read_text(encoding="utf-8")
    system_prompt = build_system_prompt(
        arrangement=arrangement,
        pool=pool,
        instructions_text=instructions_text,
    )
    messages = [
        {
            "role": "user",
            "content": "Return editor_review JSON conforming to the response schema. Iteration will be set by the tool.",
        }
    ]

    if client is None:
        client = build_claude_client(args.env_file)
    response = client.complete_json(
        model=args.model,
        system=system_prompt,
        messages=messages,
        response_schema=RESPONSE_SCHEMA,
        max_tokens=4000,
    )
    response["iteration"] = int(args.iteration)
    _validate_review_payload_shape(response, arrangement)
    _validate_editor_notes(response, arrangement)

    args.out.mkdir(parents=True, exist_ok=True)
    out_path = args.out / "editor_review.json"
    out_path.write_text(json.dumps(response, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(out_path)

    # --- universal result manifest -----------------------
    manifest_path = args.out / "manifest.json"
    manifest = build_manifest(
        kind="human_notes",
        inputs={
            "instructions": str(args.instructions),
            "arrangement": str(args.arrangement),
            "pool": str(args.pool),
        },
        outputs=[
            {"path": "editor_review.json", "type": "file"},
        ],
        created=datetime.now(timezone.utc).isoformat(),
    )
    write_manifest(manifest_path, manifest)
    # -------------------------------------------------------------------------

    if args.apply:
        _apply_pipeline(args)
    return out_path


if __name__ == "__main__":
    main()
