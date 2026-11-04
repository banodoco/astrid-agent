"""thumbnail_maker orchestrator — plan v2 emission + direct step execution."""


from __future__ import annotations

from astrid.core.contracts.errors import AstridError
from astrid.core.generation.availability import require_action_available
from astrid.core.pack.entrypoint import guard_canonical_entrypoint

guard_canonical_entrypoint('video_editing.thumbnail_maker')
import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Sequence

from astrid.core.cli_choices import add_choice_arg
from astrid.core.foundation.hash import sha256_file
from astrid.core.foundation.project_paths import project_dir
from astrid.core.media import require_runtime_materialized_file
from astrid.core.project.kernel_admission import admit_orchestrator_project_run
from astrid.core.project.runtime import reject_project_with_out
from astrid.packs.video_editing.actions.thumbnail_maker.plan_template import (
    build_plan_v2,
    emit_plan_json,
)

# ---------------------------------------------------------------------------
# Constants (kept from original)
# ---------------------------------------------------------------------------

DEFAULT_SIZE = "1536x864"
DEFAULT_COUNT = 1
DEFAULT_MODEL = "gpt-image-2"
DEFAULT_QUALITY = "medium"
DEFAULT_OUTPUT_FORMAT = "png"
DEFAULT_VISUAL_MODE = "fast"
DEFAULT_REFERENCE_MODE = "auto"
DEFAULT_MAX_CANDIDATES = 20

OUTPUT_DIRS = {
    "evidence": "evidence",
    "references": "references",
    "prompts": "prompts",
    "generated": "generated",
    "review": "review",
}

PERSON_TERMS = {
    "face", "headshot", "host", "interview", "man", "person",
    "portrait", "presenter", "speaker", "talking", "woman",
}
SCENE_TERMS = {
    "background", "crowd", "environment", "location", "room",
    "scene", "stage", "studio", "venue",
}
TEXT_TERMS = {
    "caption", "headline", "quote", "subtitle", "text", "title", "words",
}
EMOTION_TERMS = {
    "angry", "dramatic", "emotional", "excited", "funny",
    "intense", "laugh", "shocked", "surprised",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _json_default(value: object) -> str:
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=_json_default) + "\n",
        encoding="utf-8",
    )


def parse_size(value: str) -> tuple[int, int]:
    match = re.fullmatch(r"([1-9][0-9]*)x([1-9][0-9]*)", value.strip())
    if not match:
        raise argparse.ArgumentTypeError("size must be WIDTHxHEIGHT, for example 1536x864")
    return int(match.group(1)), int(match.group(2))


def normalized_size(value: str) -> str:
    width, height = parse_size(value)
    return f"{width}x{height}"


def build_output_layout(out_dir: Path) -> dict[str, Path]:
    root = out_dir.expanduser().resolve()
    layout = {"root": root}
    layout.update({key: root / name for key, name in OUTPUT_DIRS.items()})
    return layout


def ensure_output_layout(layout: dict[str, Path]) -> None:
    for path in layout.values():
        path.mkdir(parents=True, exist_ok=True)


def _query_tokens(query: str) -> list[str]:
    return sorted(set(re.findall(r"[a-z0-9]+", query.lower())))


def plan_evidence_needs(query: str) -> dict[str, Any]:
    tokens = _query_tokens(query)
    token_set = set(tokens)
    needs: list[dict[str, Any]] = []
    if token_set & PERSON_TERMS:
        needs.append({
            "id": "speaker_or_person_framing",
            "reason": "Query appears to need a readable person or speaker-oriented frame.",
            "source": "video_frames",
            "selection_hint": "Prefer clear upper-body or face-visible composition when present.",
        })
    if token_set & SCENE_TERMS:
        needs.append({
            "id": "scene_context",
            "reason": "Query references the surrounding scene or location.",
            "source": "scene_frames",
            "selection_hint": "Prefer frames that show the environment clearly.",
        })
    if token_set & TEXT_TERMS:
        needs.append({
            "id": "title_or_quote_context",
            "reason": "Query references text, a title, caption, or quoted idea.",
            "source": "query_text",
            "selection_hint": "Preserve room for readable thumbnail text.",
        })
    if token_set & EMOTION_TERMS:
        needs.append({
            "id": "expressive_moment",
            "reason": "Query asks for an emotional or high-energy thumbnail.",
            "source": "video_frames",
            "selection_hint": "Prefer visually expressive frames.",
        })
    if not needs:
        needs.append({
            "id": "representative_visual_context",
            "reason": "No specialized evidence need was detected, so representative video frames are sufficient.",
            "source": "scene_frames",
            "selection_hint": "Prefer sharp, legible, non-transitional frames.",
        })
    return {
        "query": query,
        "tokens": tokens,
        "needs": needs,
        "planner": {"name": "deterministic_keyword_planner", "version": 1},
    }


def resolve_video_for_analysis(video: str, *, dry_run: bool) -> dict[str, Any]:
    original = str(video)
    try:
        resolved = require_runtime_materialized_file(original, label="--video")
    except Exception as exc:
        if not dry_run:
            raise
        return {
            "original": original,
            "resolved": original,
            "resolved_ok": False,
            "resolution_error": str(exc),
        }
    return {
        "original": original,
        "resolved": str(Path(resolved)),
        "resolved_ok": True,
        "resolution_error": None,
    }


# ---------------------------------------------------------------------------
# Orchestrator (plan v2 + direct step execution)
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create source-relevant thumbnail candidates for a video."
    )
    subparsers = parser.add_subparsers(dest="command")

    resolve_cmd = subparsers.add_parser("resolve-video", help="Resolve source video metadata.")
    resolve_cmd.add_argument("--video", required=True)
    resolve_cmd.add_argument("--out", type=Path, required=True)

    plan_cmd = subparsers.add_parser("plan-evidence", help="Write evidence planning metadata.")
    plan_cmd.add_argument("--query", required=True)
    plan_cmd.add_argument("--out", type=Path, required=True)

    discover_cmd = subparsers.add_parser(
        "discover-video-evidence",
        help="Write deterministic candidate evidence metadata.",
    )
    discover_cmd.add_argument("--query", required=True)
    discover_cmd.add_argument("--out", type=Path, required=True)
    discover_cmd.add_argument("--video")
    discover_cmd.add_argument("--previous-manifest", type=Path, required=True)

    reference_cmd = subparsers.add_parser(
        "build-reference-pack",
        help="Build a deterministic reference-pack manifest.",
    )
    reference_cmd.add_argument("--query", required=True)
    reference_cmd.add_argument("--out", type=Path, required=True)
    reference_cmd.add_argument("--previous-manifest", type=Path, required=True)

    generate_cmd = subparsers.add_parser(
        "generate-thumbnails",
        help="Write a placeholder generated-thumbnail manifest.",
    )
    generate_cmd.add_argument("--query", required=True)
    generate_cmd.add_argument("--out", type=Path, required=True)
    generate_cmd.add_argument("--previous-manifest", type=Path, required=True)
    generate_cmd.add_argument("--count", type=int, default=DEFAULT_COUNT)
    generate_cmd.add_argument("--size", default=DEFAULT_SIZE, type=normalized_size)

    parser.add_argument("--video", help="Runtime-materialized source video file.", default=argparse.SUPPRESS)
    parser.add_argument("--query", default="auto", help="Thumbnail direction or search query.")
    parser.add_argument("--out", type=Path, help="Output directory.", default=argparse.SUPPRESS)
    parser.add_argument("--size", default=DEFAULT_SIZE, type=normalized_size)
    parser.add_argument("--count", type=int, default=DEFAULT_COUNT)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    add_choice_arg(parser, "--quality", values=("low", "medium", "high", "auto"), default=DEFAULT_QUALITY)
    add_choice_arg(parser, "--output-format", values=("png", "jpeg", "jpg", "webp"), default=DEFAULT_OUTPUT_FORMAT)
    add_choice_arg(parser, "--visual-mode", values=("fast", "best"), default=DEFAULT_VISUAL_MODE)
    add_choice_arg(parser, "--reference-mode", values=("auto", "always", "never"), default=DEFAULT_REFERENCE_MODE)
    parser.add_argument("--max-candidates", type=int, default=DEFAULT_MAX_CANDIDATES)
    parser.add_argument("--previous-manifest", type=Path)
    parser.add_argument("--feedback")
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--project", help="Project slug.", default=argparse.SUPPRESS)
    parser.add_argument("--python-exec", help="Python executable.", default=sys.executable)
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def resolve_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parsed = build_parser().parse_args(argv)
    cli_values = vars(parsed)
    merged = dict(cli_values)

    args = argparse.Namespace(**merged)
    args.python_exec = str(getattr(args, "python_exec", sys.executable))
    args.verbose = bool(getattr(args, "verbose", False))
    args.dry_run = bool(getattr(args, "dry_run", False))

    out = getattr(args, "out", None)
    if out is not None:
        args.out = Path(out).expanduser().resolve()

    video = getattr(args, "video", None)
    if video is not None:
        args.video = str(require_runtime_materialized_file(video, label="--video"))

    return args


def _exec_resolve_video(args: argparse.Namespace) -> int:
    out: Path = args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = resolve_video_for_analysis(str(args.video), dry_run=True)
    payload["query"] = None
    write_json(out, payload)
    print(f"thumbnail_maker: wrote={out}")
    return 0


def _exec_plan_evidence(args: argparse.Namespace) -> int:
    out: Path = args.out
    payload = plan_evidence_needs(args.query)
    write_json(out, payload)
    print(f"thumbnail_maker: wrote={out}")
    return 0


def _exec_discover_video_evidence(args: argparse.Namespace) -> int:
    raise NotImplementedError(
        "thumbnail_maker.discover_video_evidence: not implemented; see SPRINT_1 milestone D"
    )


def _exec_build_reference_pack(args: argparse.Namespace) -> int:
    raise NotImplementedError(
        "thumbnail_maker.build_reference_pack: not implemented; see SPRINT_1 milestone D"
    )


def _exec_generate_thumbnails(args: argparse.Namespace) -> int:
    del args
    require_action_available("video_editing.thumbnail_maker")
    raise AssertionError("unreachable: unavailable thumbnail action was accepted")

def run_orchestrator(args: argparse.Namespace) -> int:
    args.out.mkdir(parents=True, exist_ok=True)

    project_slug = getattr(args, "project", None)
    if project_slug is not None:
        proj_root = project_dir(project_slug)
        plan_path = proj_root / "plan.json"
    else:
        plan_path = args.out / "plan.json"

    plan = build_plan_v2(
        python_exec=args.python_exec,
        run_root=args.out,
        source=getattr(args, "video", None),
    )
    emit_plan_json(plan, plan_path)

    plan_hash = "sha256:" + sha256_file(plan_path)

    if args.dry_run:
        print(f"thumbnail_maker: plan emitted to {plan_path} (plan_hash={plan_hash})")
        return 0

    return _execute_steps_directly(args)


def _child_env() -> dict[str, str]:
    """Child env: pass the current env plus the internal-invocation marker."""

    env = os.environ.copy()
    env["ASTRID_INTERNAL_INVOCATION"] = "1"
    return env


def _run_step_subprocess(cmd: list[str], *, label: str) -> None:
    """Run one local step as a guarded subprocess, failing on non-zero exit."""

    proc = subprocess.run(cmd, capture_output=True, text=True, env=_child_env())
    if proc.stdout.strip():
        print(proc.stdout.strip())
    if proc.returncode != 0:
        raise AstridError(
            f"[thumbnail_maker] {label} failed (exit {proc.returncode})",
            recovery_command=f"check the step command and rerun: {' '.join(cmd)}",
            state_snapshot={
                "stdout": proc.stdout,
                "stderr": proc.stderr,
                "command": " ".join(cmd),
            },
        )


def _execute_steps_directly(args: argparse.Namespace) -> int:
    """Run the five local steps sequentially as guarded subprocesses.

    Each step re-enters this module's ``main()`` step-executor fast path
    (``ASTRID_INTERNAL_INVOCATION=1``), mirroring the plan-template step
    commands with concrete output paths under ``--out``.
    """
    run_module = "astrid.packs.video_editing.actions.thumbnail_maker.run"
    steps: list[tuple[str, list[str]]] = []

    video = getattr(args, "video", None)
    if video is not None:
        steps.append((
            "resolve-video",
            [
                args.python_exec, "-X", "utf8", "-m", run_module, "resolve-video",
                "--video", str(video),
                "--out", str(args.out / "video-resolution.json"),
            ],
        ))
    else:
        print("thumbnail_maker: skipping resolve-video (no --video provided)")

    query = getattr(args, "query", "auto")
    steps.append((
        "plan-evidence",
        [
            args.python_exec, "-X", "utf8", "-m", run_module, "plan-evidence",
            "--query", str(query),
            "--out", str(args.out / "evidence" / "evidence-plan.json"),
        ],
    ))

    steps.append((
        "discover-video-evidence",
        [
            args.python_exec, "-X", "utf8", "-m", run_module, "discover-video-evidence",
            "--query", str(query),
            "--out", str(args.out / "evidence" / "candidates.json"),
            "--previous-manifest", str(args.out / "evidence" / "evidence-plan.json"),
        ],
    ))

    steps.append((
        "build-reference-pack",
        [
            args.python_exec, "-X", "utf8", "-m", run_module, "build-reference-pack",
            "--query", str(query),
            "--out", str(args.out / "evidence" / "reference-pack.json"),
            "--previous-manifest", str(args.out / "evidence" / "candidates.json"),
        ],
    ))

    steps.append((
        "generate-thumbnails",
        [
            args.python_exec, "-X", "utf8", "-m", run_module, "generate-thumbnails",
            "--query", str(query),
            "--out", str(args.out / "thumbnail-manifest.json"),
            "--previous-manifest", str(args.out / "evidence" / "reference-pack.json"),
            "--count", str(getattr(args, "count", 1)),
            "--size", str(getattr(args, "size", DEFAULT_SIZE)),
        ],
    ))

    for label, cmd in steps:
        print(f"thumbnail_maker: running step={label}")
        _run_step_subprocess(cmd, label=label)
    return 0


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _run_step_subcommand(
    *,
    effective_argv: list[str],
    args: argparse.Namespace,
    runner: Callable[[argparse.Namespace], int],
) -> int:
    """Run one local-adapter step directly (no task gate)."""

    del effective_argv
    return runner(args)


def main(argv: Sequence[str] | None = None) -> int:
    effective_argv = list(sys.argv[1:] if argv is None else argv)
    try:
        require_action_available("video_editing.thumbnail_maker")
    except AstridError as exc:
        from astrid.core.contracts.errors import render_astrid_error

        return render_astrid_error(exc)
    step_commands = {
        "resolve-video",
        "plan-evidence",
        "discover-video-evidence",
        "build-reference-pack",
        "generate-thumbnails",
    }

    if effective_argv and effective_argv[0] in step_commands:
        args = build_parser().parse_args(effective_argv)
        if args.command == "resolve-video":
            return _run_step_subcommand(
                effective_argv=effective_argv,
                args=args,
                runner=_exec_resolve_video,
            )
        if args.command == "plan-evidence":
            return _run_step_subcommand(
                effective_argv=effective_argv,
                args=args,
                runner=_exec_plan_evidence,
            )
        if args.command == "discover-video-evidence":
            return _run_step_subcommand(
                effective_argv=effective_argv,
                args=args,
                runner=_exec_discover_video_evidence,
            )
        if args.command == "build-reference-pack":
            return _run_step_subcommand(
                effective_argv=effective_argv,
                args=args,
                runner=_exec_build_reference_pack,
            )
        if args.command == "generate-thumbnails":
            return _run_step_subcommand(
                effective_argv=effective_argv,
                args=args,
                runner=_exec_generate_thumbnails,
            )
        raise AstridError(
            f"thumbnail_maker: unknown subcommand: {args.command}",
            valid_options=sorted(step_commands),
            recovery_command="choose one of the valid subcommands listed above",
        )
    # Runtime admission; the local path below is only an ephemeral pack workspace.
    try:
        parser = argparse.ArgumentParser(add_help=False)
        parser.add_argument("--project")
        parser.add_argument("--out")
        parsed, _unknown = parser.parse_known_args(effective_argv)

        kernel_ctx = None
        if parsed.project:
            reject_project_with_out(parsed.project, parsed.out)
            from astrid.sdk.client import AstridClient

            with AstridClient.open_from_launcher() as client:
                kernel_ctx = admit_orchestrator_project_run(
                    project=parsed.project,
                    tool_id="video_editing.thumbnail_maker",
                    argv=["thumbnail_maker", *effective_argv],
                    client=client,
                )
            effective_argv = [*effective_argv, "--out", str(kernel_ctx.run_root)]

        args = resolve_args(effective_argv)
        if kernel_ctx is not None:
            args.project = kernel_ctx.project_slug

        returncode = run_orchestrator(args)
        return returncode
    except SystemExit as exc:
        if isinstance(exc.code, int):
            return exc.code
        return 1
    except Exception:
        raise
