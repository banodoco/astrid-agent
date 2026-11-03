"""Argument parser construction and resolution for the hype orchestrator.

Extracted from ``run.py``.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from astrid.pack_api import has_url_scheme

from .config import (
    STEP_ORDER,
    load_config,
    normalize_config,
    normalize_extra_args,
    normalize_many,
    parse_asset_entry,
    usage_error,
)


def _resolve_theme_arg(value: object) -> Path:
    """Resolve only an explicitly supplied, materialized theme document."""
    text = str(value).strip()
    if not text or has_url_scheme(text):
        usage_error("astrid: theme must be an explicit runtime-materialized theme.json file")
    candidate = Path(text).expanduser().resolve()
    if not candidate.is_file() or candidate.name != "theme.json":
        usage_error("astrid: theme must be an explicit runtime-materialized theme.json file")
    return candidate


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run transcribe -> scenes -> shots -> triage -> scene_describe -> quote_scout -> "
            "pool_build -> pool_merge -> arrange -> cut -> refine -> render -> validate with cache-aware resume. "
            "When --video is omitted, source-video analysis steps are skipped."
        )
    )
    parser.add_argument("--video", help="Source video file.", default=argparse.SUPPRESS)
    parser.add_argument("--brief", help="Brief text file for arrangement composition.", default=argparse.SUPPRESS)
    parser.add_argument("--out", help="Per-source output directory.", default=argparse.SUPPRESS)
    parser.add_argument("--project", help="Project slug for a persistent project run.", default=argparse.SUPPRESS)
    parser.add_argument("--audio", help="Audio source for transcription. Defaults to --video.", default=argparse.SUPPRESS)
    parser.add_argument(
        "--target-duration",
        dest="target_duration",
        type=float,
        help="Target output duration in seconds; required when both --video and --audio are omitted.",
        default=argparse.SUPPRESS,
    )
    parser.add_argument("--asset", action="append", help="Additional source asset in KEY=PATH form.", default=argparse.SUPPRESS)
    parser.add_argument("--primary-asset", help="Primary asset key for cut.py.", dest="primary_asset", default=argparse.SUPPRESS)
    parser.add_argument("--source-slug", help="Source slug used in pool/arrangement metadata. Defaults to <out>.name.", dest="source_slug", default=argparse.SUPPRESS)
    parser.add_argument("--brief-slug", help="Brief slug used under <out>/briefs/. Defaults to Path(--brief).stem.", dest="brief_slug", default=argparse.SUPPRESS)
    parser.add_argument("--from", help="Force a step and all later steps to rerun.", dest="from_step", default=argparse.SUPPRESS)
    parser.add_argument("--skip", action="append", help="Skip a step entirely.", default=argparse.SUPPRESS)
    parser.add_argument("--render", action="store_true", help="Run render_remotion.py after cut.py.", default=argparse.SUPPRESS)
    parser.add_argument(
        "--theme",
        type=Path,
        help="Explicit runtime-materialized theme.json for Remotion render.",
        default=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--max-editor-passes",
        type=int,
        default=argparse.SUPPRESS,
        help="Maximum editor_review passes per brief. Hard-capped to 1 or 2.",
    )
    parser.add_argument("--config", help="Optional JSON config, or YAML when PyYAML is importable.", default=argparse.SUPPRESS)
    parser.add_argument("--python", help="Python executable for child scripts.", dest="python_exec", default=argparse.SUPPRESS)
    parser.add_argument("--verbose", action="store_true", help="Stream subprocess output while logging.", default=argparse.SUPPRESS)
    parser.add_argument(
        "--env-file",
        dest="env_file",
        help=(
            "Path to .env file with OPENAI_API_KEY / GEMINI_API_KEY / ANTHROPIC_API_KEY; "
            "forwarded to LLM-backed child steps."
        ),
        default=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--no-audit",
        action="store_true",
        help="Disable the run-local audit ledger under <out>/audit.",
        default=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--allow-generative-effects",
        dest="allow_generative_effects",
        action="store_true",
        help=(
            "Enable mixed-mode: allow the arrange step to include generative "
            "visual_source pool entries even when --video is set. Overrides the "
            "brief's allow_generative_visuals field for this run only. "
            "Phase 3 mixed-mode."
        ),
        default=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        help=(
            "Plan the run without invoking any executor: prep the brief, "
            "compute the step set + facts, build redacted commands, write "
            "<out>/hype.plan.json, and exit. Phase 3 mixed-mode."
        ),
        default=argparse.SUPPRESS,
    )
    return parser


def resolve_args(argv: list[str] | None = None) -> argparse.Namespace:
    parsed = build_parser().parse_args(argv)
    cli_values = vars(parsed)
    config_values = normalize_config(load_config(cli_values["config"])) if "config" in cli_values else {}
    merged = {**config_values, **cli_values}
    if not merged.get("out"):
        missing = []
        missing.append("--out")
        usage_error(f"astrid: missing required inputs: {', '.join(missing)}")

    if not merged.get("brief"):
        usage_error("astrid: missing required inputs: --brief")

    theme_explicit = "theme" in merged
    args = argparse.Namespace(**merged)
    args.theme_explicit = theme_explicit
    video_value = getattr(args, "video", None)
    if video_value is not None and has_url_scheme(str(video_value)):
        usage_error("astrid: --video must be a runtime-materialized file, not a URL")
    args.video = None if video_value is None else Path(video_value).expanduser().resolve()
    args.out = Path(args.out).expanduser().resolve()
    args.brief = Path(args.brief).expanduser().resolve()
    audio_value = getattr(args, "audio", args.video if args.video is not None else None)
    if audio_value is not None and has_url_scheme(str(audio_value)):
        usage_error("astrid: --audio must be a runtime-materialized file, not a URL")
    args.audio = None if audio_value is None else Path(audio_value).expanduser().resolve()
    args.target_duration = getattr(args, "target_duration", None)
    if args.video is None and args.audio is None:
        if args.target_duration is None:
            usage_error("astrid: --target-duration is required when both --video and --audio are omitted")
        if float(args.target_duration) <= 0:
            usage_error("astrid: --target-duration must be greater than 0")
    args.python_exec = str(getattr(args, "python_exec", sys.executable))
    args.render = bool(getattr(args, "render", False))
    args.no_audit = bool(getattr(args, "no_audit", False))
    args.allow_generative_effects = bool(getattr(args, "allow_generative_effects", False))
    args.dry_run = bool(getattr(args, "dry_run", False))
    theme_value = getattr(args, "theme", None)
    args.theme = _resolve_theme_arg(theme_value) if theme_value is not None else None
    args.verbose = bool(getattr(args, "verbose", False))
    raw_editor_passes = int(getattr(args, "max_editor_passes", 2))
    if not 1 <= raw_editor_passes <= 2:
        usage_error(
            f"astrid: --max-editor-passes must be 1 or 2 (got {raw_editor_passes}); "
            "vision budget is hard-capped."
        )
    args.max_editor_passes = raw_editor_passes
    env_file = getattr(args, "env_file", None)
    args.env_file = Path(env_file).expanduser().resolve() if env_file else None
    args.skip = normalize_many(getattr(args, "skip", None), key_name="skip")
    args.asset = normalize_many(getattr(args, "asset", None), key_name="asset")
    args.extra_args = normalize_extra_args(getattr(args, "extra_args", None))
    args.asset_pairs = [parse_asset_entry(item) for item in args.asset]

    args.source_slug = getattr(args, "source_slug", args.out.name)
    brief_slug = getattr(args, "brief_slug", None)
    if brief_slug is None:
        generic_brief_names = {"brief", "plan", "prompt"}
        brief_slug = args.out.name if args.brief.stem.lower() in generic_brief_names else args.brief.stem
    args.brief_slug = brief_slug
    args.brief_out = (args.out / "briefs" / args.brief_slug).resolve()
    args.brief_copy = args.brief_out / "brief.txt"
    for key in ("video", "brief", "audio"):
        path = getattr(args, key)
        if path is None:
            continue
        if not path.exists():
            usage_error(f"astrid: {key} input not found: {path}")
    allowed_skips = set(STEP_ORDER)
    unknown_skips = [name for name in args.skip if name not in allowed_skips]
    if unknown_skips:
        usage_error(f"astrid: unknown --skip step(s): {', '.join(unknown_skips)}")
    allowed_from_steps = set(STEP_ORDER)
    if getattr(args, "from_step", None) and args.from_step not in allowed_from_steps:
        usage_error(f"astrid: unknown --from step: {args.from_step}")
    if "cut" in args.skip:
        timeline_path = args.brief_out / "hype.timeline.json"
        if args.render and not timeline_path.exists():
            usage_error("astrid: cannot --skip cut while --render is set and hype.timeline.json is missing")
    primary = getattr(args, "primary_asset", None)
    if primary and primary != "main":
        extra_keys = {key for key, _ in args.asset_pairs}
        if primary not in extra_keys:
            usage_error(
                f"astrid: --primary-asset={primary!r} has no matching --asset entry. "
                "The primary video is registered as 'main', so --primary-asset must either be omitted, "
                f"set to 'main', or backed by an explicit --asset {primary}=<path>."
            )
    return args
