"""event_talks orchestrator — plan v2 emission + direct step execution."""


from __future__ import annotations

from astrid.pack_api import AstridError
from astrid.pack_api import guard_canonical_entrypoint

guard_canonical_entrypoint('video_editing.event_talks')
import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Sequence

from astrid.pack_api import sha256_file
from astrid.pack_api import project_dir
from astrid.pack_api import ffprobe_duration_seconds
from astrid.pack_api import admit_orchestrator_project_run
from astrid.pack_api import reject_project_with_out
from astrid.packs.video_editing.actions.event_talks.plan_template import (
    build_plan_v2,
    emit_plan_json,
)

# ---------------------------------------------------------------------------
# Constants — preserved from the legacy orchestrator
# ---------------------------------------------------------------------------

ADOS_SUNDAY_SPEAKERS: list[dict[str, str]] = json.loads(
    (Path(__file__).with_name("data") / "ados_sunday_speakers.json").read_text(encoding="utf-8")
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fold(text: str) -> str:
    """Lower-case + collapse whitespace for case-insensitive matching."""
    return re.sub(r"\s+", " ", text.lower()).strip()


def _slugify(text: str) -> str:
    """Turn a human-readable string into a URL-ish slug."""
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _fmt_time(seconds: float) -> str:
    """Format a float second count as ``HH:MM:SS.mmm``."""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}"


# ---------------------------------------------------------------------------
# Orchestrator-level parser (plan v2, no subcommand)
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """Build the orchestrator-level argument parser.

    When invoked *without* a subcommand, this emits a plan v2 and
    optionally executes the local steps directly.  When invoked *with*
    a subcommand (e.g. ``ados-sunday-template``) it acts as a step
    executor called by the orchestrator.
    """
    parser = argparse.ArgumentParser(
        description="Build and render individual event talk videos from long recordings.",
    )

    # Orchestrator flags
    parser.add_argument(
        "--source",
        help="Source video file path.",
        default=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--transcript",
        help="Pre-computed transcript JSON path (for search-transcript step).",
        default=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--out",
        type=Path,
        help="Output directory for the run.",
        default=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--project",
        help="Project slug for a persistent project run.",
        default=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--python-exec",
        help="Python executable for child commands.",
        default=sys.executable,
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Stream subprocess output while logging.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Emit plan.json without executing steps.",
    )

    # Step-level subcommands (used when the local adapter invokes a step)
    subparsers = parser.add_subparsers(dest="command")

    # ados-sunday-template
    tmpl = subparsers.add_parser(
        "ados-sunday-template",
        help="Write the ADOS Paris Sunday speaker template.",
    )
    tmpl.add_argument("--out", type=Path, required=True)

    # search-transcript
    search = subparsers.add_parser(
        "search-transcript",
        help="Search a Whisper JSON transcript for speaker/title phrases.",
    )
    search.add_argument("--transcript", type=Path, required=True)
    search.add_argument("--out", type=Path, required=True)
    search.add_argument("--phrases", nargs="*", default=[])

    # find-holding-screens
    holding = subparsers.add_parser(
        "find-holding-screens",
        help="Sample video frames and OCR likely wait/holding/title-card screens.",
    )
    holding.add_argument("--video", type=Path, required=True)
    holding.add_argument("--out", type=Path, required=True)

    # render
    render = subparsers.add_parser(
        "render",
        help="Render each manifest talk with ADOS intro, lower-third, and outro.",
    )
    render.add_argument("--manifest", type=Path, required=True)
    render.add_argument("--out-dir", type=Path, required=True)
    render.add_argument("--dry-run", action="store_true")

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

    source = getattr(args, "source", None)
    if source is not None:
        args.source = Path(source).expanduser().resolve()
        if not args.source.exists():
            raise AstridError(
                f"source not found: {args.source}",
                recovery_command="verify the source path and rerun with --source <path>",
            )

    transcript = getattr(args, "transcript", None)
    if transcript is not None:
        args.transcript = Path(transcript).expanduser().resolve()

    return args


# ---------------------------------------------------------------------------
# Step executors — invoked by the local adapter for each step
# ---------------------------------------------------------------------------


def _exec_ados_sunday_template(args: argparse.Namespace) -> int:
    """Write the static ADOS Sunday speaker template JSON."""
    out: Path = args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "event": "ADOS Paris 2026",
        "day": "Sunday",
        "talks": [
            {
                "slug": _slugify(f"{entry['speaker']} {entry['title']}"),
                **entry,
                "source": "",
                "start": None,
                "end": None,
            }
            for entry in ADOS_SUNDAY_SPEAKERS
        ],
    }
    out.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"event_talks: wrote={out}")
    return 0


def _exec_search_transcript(args: argparse.Namespace) -> int:
    """Search a Whisper JSON transcript for speaker/title phrases."""
    transcript: Path = args.transcript
    out: Path = args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    data = json.loads(transcript.read_text(encoding="utf-8"))
    segments = data.get("segments") or []

    phrases: list[str] = args.phrases or []
    if not phrases:
        phrases = [e["speaker"] for e in ADOS_SUNDAY_SPEAKERS] + [
            e["title"] for e in ADOS_SUNDAY_SPEAKERS
        ]

    compiled = [
        (phrase, re.compile(re.escape(_fold(phrase)), re.IGNORECASE))
        for phrase in phrases
    ]

    found = 0
    lines: list[str] = []
    for segment in segments:
        text = str(segment.get("text") or "")
        folded = _fold(text)
        matches = [phrase for phrase, pattern in compiled if pattern.search(folded)]
        if matches:
            found += 1
            start = float(segment.get("start") or 0.0)
            end = float(segment.get("end") or start)
            lines.append(
                f"{_fmt_time(start)}-{_fmt_time(end)} | "
                f"{', '.join(matches)} | {text.strip()}"
            )
    lines.append(f"matches={found}")
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"event_talks: wrote={out} matches={found}")
    return 0


def _exec_find_holding_screens(args: argparse.Namespace) -> int:
    """Sample video frames and OCR for holding/title-card screens.

    This is a lightweight port — writes a placeholder manifest when
    ffmpeg/tesseract are unavailable, and does the real work when they
    are.
    """
    import shutil
    import subprocess as sp

    video: Path = args.video
    out: Path = args.out
    out.parent.mkdir(parents=True, exist_ok=True)

    # When ffmpeg/tesseract aren't available (CI, test envs), emit a
    # minimal placeholder so the pipeline can proceed.
    if shutil.which("ffmpeg") is None or shutil.which("tesseract") is None:
        payload = {
            "video": str(video),
            "sample_sec": 10.0,
            "hits": [],
            "intervals": [],
            "note": "placeholder — ffmpeg/tesseract unavailable",
        }
        out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"event_talks: placeholder wrote={out}")
        return 0

    # Real implementation
    work_dir = out.parent / f"{out.stem}.frames"
    work_dir.mkdir(parents=True, exist_ok=True)

    duration = ffprobe_duration_seconds(video)
    phrases = ["LUNCH BREAK", "WE'LL BE BACK", "THANK YOU", "BREAK"]
    folded_phrases = [_fold(p) for p in phrases]
    hits: list[dict[str, Any]] = []
    t = 0.0
    sample_sec = 10.0
    while t <= duration:
        frame = work_dir / f"frame_{int(round(t)):06d}.jpg"
        if not frame.is_file():
            sp.run(
                [
                    "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-ss", f"{t:.3f}", "-i", str(video),
                    "-frames:v", "1", str(frame),
                ],
                check=True,
            )
        text = sp.run(
            ["tesseract", str(frame), "stdout", "--psm", "6"],
            check=False, capture_output=True, text=True,
        ).stdout.strip()
        folded = _fold(text)
        matched = [p for p, fp in zip(phrases, folded_phrases) if fp in folded]
        if matched:
            hits.append({
                "time": round(t, 3),
                "timecode": _fmt_time(t),
                "matched": matched,
                "text": text,
                "frame": str(frame),
            })
        t += sample_sec

    intervals = _coalesce_hit_intervals(hits, sample_sec)
    payload = {
        "video": str(video),
        "sample_sec": sample_sec,
        "phrases": phrases,
        "hits": hits,
        "intervals": intervals,
    }
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"event_talks: wrote={out} hits={len(hits)} intervals={len(intervals)}")
    return 0


def _exec_render_manifest(args: argparse.Namespace) -> int:
    raise NotImplementedError(
        "event_talks.render_manifest: not implemented; see SPRINT_1 milestone D"
    )


def _coalesce_hit_intervals(
    hits: list[dict[str, Any]], threshold: float
) -> list[dict[str, Any]]:
    """Merge adjacent hit timestamps into contiguous intervals."""
    if not hits:
        return []
    sorted_hits = sorted(hits, key=lambda h: h["time"])
    intervals: list[dict[str, Any]] = []
    cur_start = sorted_hits[0]["time"]
    cur_end = cur_start
    cur_matched: set[str] = set(sorted_hits[0]["matched"])

    for hit in sorted_hits[1:]:
        if hit["time"] - cur_end <= threshold * 1.5:
            cur_end = hit["time"]
            cur_matched.update(hit["matched"])
        else:
            intervals.append({
                "start": cur_start,
                "end": cur_end,
                "start_timecode": _fmt_time(cur_start),
                "end_timecode": _fmt_time(cur_end),
                "matched": sorted(cur_matched),
            })
            cur_start = hit["time"]
            cur_end = hit["time"]
            cur_matched = set(hit["matched"])

    intervals.append({
        "start": cur_start,
        "end": cur_end,
        "start_timecode": _fmt_time(cur_start),
        "end_timecode": _fmt_time(cur_end),
        "matched": sorted(cur_matched),
    })
    return intervals


# ---------------------------------------------------------------------------
# Orchestrator run (plan v2 emission + direct step execution)
# ---------------------------------------------------------------------------


def run_orchestrator(args: argparse.Namespace) -> int:
    """Emit plan v2 and execute steps directly (kernel is authority, no run.json)."""
    args.out.mkdir(parents=True, exist_ok=True)

    # 1. Emit plan v2
    project_slug = getattr(args, "project", None)
    if project_slug is not None:
        proj_root = project_dir(project_slug)
        plan_path = proj_root / "plan.json"
    else:
        plan_path = args.out / "plan.json"

    plan = build_plan_v2(
        python_exec=args.python_exec,
        run_root=args.out,
        source=getattr(args, "source", None),
    )
    emit_plan_json(plan, plan_path)

    # 2. Compute plan hash
    plan_hash = "sha256:" + sha256_file(plan_path)

    if args.dry_run:
        print(f"event_talks: plan emitted to {plan_path} (plan_hash={plan_hash})")
        return 0

    # 3. Execute the local steps directly (file-only pipeline, no second ledger)
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
            f"[event_talks] {label} failed (exit {proc.returncode})",
            recovery_command=f"check the step command and rerun: {' '.join(cmd)}",
            state_snapshot={
                "stdout": proc.stdout,
                "stderr": proc.stderr,
                "command": " ".join(cmd),
            },
        )


def _execute_steps_directly(args: argparse.Namespace) -> int:
    """Run the four local steps sequentially as guarded subprocesses.

    Each step re-enters this module's ``main()`` step-executor fast path
    (``ASTRID_INTERNAL_INVOCATION=1``), mirroring the plan-template step
    commands with concrete output paths under ``--out``.
    """
    run_module = "astrid.packs.video_editing.actions.event_talks.run"
    steps: list[tuple[str, list[str]]] = []

    steps.append((
        "ados-sunday-template",
        [
            args.python_exec, "-X", "utf8", "-m", run_module, "ados-sunday-template",
            "--out", str(args.out / "ados-sunday-template.json"),
        ],
    ))

    transcript = getattr(args, "transcript", None)
    if transcript is not None and isinstance(transcript, Path) and transcript.is_file():
        steps.append((
            "search-transcript",
            [
                args.python_exec, "-X", "utf8", "-m", run_module, "search-transcript",
                "--transcript", str(transcript),
                "--out", str(args.out / "search-results.txt"),
            ],
        ))
    else:
        print("event_talks: skipping search-transcript (no --transcript provided)")

    source = getattr(args, "source", None)
    if source is not None and isinstance(source, Path) and source.is_file():
        steps.append((
            "find-holding-screens",
            [
                args.python_exec, "-X", "utf8", "-m", run_module, "find-holding-screens",
                "--video", str(source),
                "--out", str(args.out / "holding-screens.json"),
            ],
        ))
    else:
        print("event_talks: skipping find-holding-screens (no --source video provided)")

    steps.append((
        "render",
        [
            args.python_exec, "-X", "utf8", "-m", run_module, "render",
            "--manifest", str(args.out / "ados-sunday-template.json"),
            "--out-dir", str(args.out / "render"),
        ],
    ))

    for label, cmd in steps:
        print(f"event_talks: running step={label}")
        _run_step_subprocess(cmd, label=label)
    return 0


# ---------------------------------------------------------------------------
# Main entry point
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
    """Main entry point for the event_talks orchestrator.

    Two modes:

    1. **Step executor** — when a subcommand is present (e.g.
       ``ados-sunday-template``), execute that step directly.  This
       is the path taken by the orchestrator's step loop.

    2. **Orchestrator** — when no subcommand is given, emit a plan v2
       and execute the local steps directly.
    """
    effective_argv = list(sys.argv[1:] if argv is None else argv)

    # Fast-path: detect a step-execution subcommand before any
    # project/gate setup, so the local adapter can invoke steps
    # without session/project context.
    step_commands = {
        "ados-sunday-template",
        "search-transcript",
        "find-holding-screens",
        "render",
    }
    if effective_argv and effective_argv[0] in step_commands:
        args = build_parser().parse_args(effective_argv)
        cmd = args.command
        if cmd == "ados-sunday-template":
            return _run_step_subcommand(
                effective_argv=effective_argv,
                args=args,
                runner=_exec_ados_sunday_template,
            )
        if cmd == "search-transcript":
            return _run_step_subcommand(
                effective_argv=effective_argv,
                args=args,
                runner=_exec_search_transcript,
            )
        if cmd == "find-holding-screens":
            return _run_step_subcommand(
                effective_argv=effective_argv,
                args=args,
                runner=_exec_find_holding_screens,
            )
        if cmd == "render":
            return _run_step_subcommand(
                effective_argv=effective_argv,
                args=args,
                runner=_exec_render_manifest,
            )
        # Should not reach here — subparser dispatch guarantees `command`
        raise AstridError(
            f"unknown subcommand: {cmd}",
            recovery_command="use one of: ados-sunday-template, search-transcript, find-holding-screens, render",
        )

    # Orchestrator path — runtime admission with an ephemeral pack workspace.
    try:
        pre_parser = argparse.ArgumentParser(add_help=False)
        pre_parser.add_argument("--project")
        pre_parser.add_argument("--out")
        parsed, _unknown = pre_parser.parse_known_args(effective_argv)

        kernel_ctx = None
        if parsed.project:
            reject_project_with_out(parsed.project, parsed.out)
            from astrid.sdk.client import AstridClient

            with AstridClient.open_from_launcher() as client:
                kernel_ctx = admit_orchestrator_project_run(
                    project=parsed.project,
                    tool_id="video_editing.event_talks",
                    argv=["event_talks", *effective_argv],
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

if __name__ == "__main__":
    raise SystemExit(main())
