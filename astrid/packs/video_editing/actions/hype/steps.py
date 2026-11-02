"""Pack-owned Hype step definitions and command builders for v3 callbacks.

Preserve the legacy provider while its declaration consumers are migrated.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from astrid.pack_api import executor_argv

from .config import STEP_ORDER  # noqa: F401 — exported callback order

PER_SOURCE_SENTINELS = (
    "transcript.json",
    "scenes.json",
    "quality_zones.json",
    "shots.json",
    "scene_triage.json",
    "scene_descriptions.json",
    "quote_candidates.json",
    "pool.json",
)
PER_BRIEF_SENTINELS = (
    "arrangement.json",
    "hype.timeline.json",
    "hype.assets.json",
    "hype.metadata.json",
    "refine.json",
    "hype.mp4",
    "hype.mp4.provenance.json",
    "editor_review.json",
    "validation.json",
)

@dataclass(frozen=True)
class Step:
    name: str
    sentinels: tuple[str, ...]
    build_cmd: Callable[[argparse.Namespace], list[str]]
    per_brief: bool = False
    always_run: bool = False
    invoke: Callable[[argparse.Namespace], Path] | None = None

def step_argv(action_id: str, python_exec: str) -> list[str]:
    """Argv tokens for invoking a qualified public child action."""
    return executor_argv(action_id, python_exec)


def add_extra_args(args: argparse.Namespace, step_name: str, cmd: list[str]) -> list[str]:
    return cmd + args.extra_args.get(step_name, [])


def asset_args(asset_pairs: list[tuple[str, Path | str]]) -> list[str]:
    args: list[str] = []
    for key, path in asset_pairs:
        args.extend(["--asset", f"{key}={path}"])
    return args

def probe_audio_duration(path: Path | str) -> float:
    from astrid.pack_api import ffprobe_duration_seconds

    return ffprobe_duration_seconds(path)


def _arrange_target_duration(args: argparse.Namespace) -> float | None:
    if args.video is not None:
        return None
    if args.audio is not None:
        return probe_audio_duration(args.audio)
    return float(args.target_duration)

def build_pool_cut_cmd(args: argparse.Namespace) -> list[str]:
    cmd = [
        *step_argv("video_editing.cut", args.python_exec),
        "--pool",
        str(args.out / "pool.json"),
        "--arrangement",
        str(args.brief_out / "arrangement.json"),
        "--brief",
        str(args.brief_copy),
        "--out",
        str(args.brief_out),
    ]
    if (args.out / "scenes.json").exists():
        cmd.extend(["--scenes", str(args.out / "scenes.json")])
    if (args.out / "transcript.json").exists():
        cmd.extend(["--transcript", str(args.out / "transcript.json")])
    if args.video is not None:
        cmd.extend(["--video", str(args.video)])
    if args.audio is not None:
        cmd.extend(["--audio", str(args.audio)])
    if "shots" not in args.skip and (args.out / "shots.json").exists():
        cmd.extend(["--shots", str(args.out / "shots.json")])
    cmd.extend(asset_args(args.asset_pairs))
    if getattr(args, "primary_asset", None):
        cmd.extend(["--primary-asset", args.primary_asset])
    # extends prior plan Step 14
    if getattr(args, "theme_explicit", False) and getattr(args, "theme", None):
        cmd.extend(["--theme", str(args.theme)])
    return cmd


def build_hype_render_cmd(args: argparse.Namespace) -> list[str]:
    """Build the direct render-facade invocation for Hype renders.

    The ``executors`` gateway family was retired with the task-mode runtime;
    children now run as guarded module subprocesses with
    ``ASTRID_INTERNAL_INVOCATION=1`` (see the orchestrator retarget
    convention).
    """

    cmd = [
        *step_argv("rendering.render", args.python_exec),
        "--timeline",
        str(args.brief_out / "hype.timeline.json"),
        "--assets",
        str(args.brief_out / "hype.assets.json"),
        "--out",
        str(args.brief_out / "hype.mp4"),
    ]
    return add_extra_args(args, "render", cmd)


def _verdict_build_cmd(args: argparse.Namespace) -> list[str]:
    raise NotImplementedError(
        "hype.verdict: stub step requires a real verdict implementation"
    )

def build_pool_steps() -> list[Step]:
    return [
        Step(
            "transcribe",
            ("transcript.json",),
            lambda args: add_extra_args(
                args,
                "transcribe",
                [
                    *step_argv("editorial.transcribe", args.python_exec),
                    "--audio",
                    str(args.audio),
                    "--out",
                    str(args.out),
                    *(["--env-file", str(args.env_file)] if args.env_file else []),
                ],
            ),
        ),
        Step(
            "scenes",
            ("scenes.json",),
            lambda args: add_extra_args(
                args,
                "scenes",
                [*step_argv("editorial.scenes", args.python_exec), "--video", str(args.video), "--out", str(args.out / "scenes.json")],
            ),
        ),
        Step(
            "quality_zones",
            ("quality_zones.json",),
            lambda args: add_extra_args(
                args,
                "quality_zones",
                [
                    *step_argv("editorial.quality_zones", args.python_exec),
                    str(args.video),
                    "--out",
                    str(args.out / "quality_zones.json"),
                ],
            ),
        ),
        Step(
            "shots",
            ("shots.json",),
            lambda args: add_extra_args(
                args,
                "shots",
                [*step_argv("editorial.shots", args.python_exec), "--video", str(args.video), "--scenes", str(args.out / "scenes.json"), "--out", str(args.out)],
            ),
        ),
        Step(
            "triage",
            ("scene_triage.json",),
            lambda args: add_extra_args(
                args,
                "triage",
                [
                    *step_argv("editorial.triage", args.python_exec),
                    "--scenes",
                    str(args.out / "scenes.json"),
                    "--shots",
                    str(args.out / "shots.json"),
                    "--shots-dir",
                    str(args.out),
                    "--out",
                    str(args.out),
                    *(["--env-file", str(args.env_file)] if args.env_file else []),
                ],
            ),
        ),
        Step(
            "scene_describe",
            ("scene_descriptions.json",),
            lambda args: add_extra_args(
                args,
                "scene_describe",
                [
                    *step_argv("understanding.scene_describe", args.python_exec),
                    "--scenes",
                    str(args.out / "scenes.json"),
                    "--triage",
                    str(args.out / "scene_triage.json"),
                    "--video",
                    str(args.video),
                    "--out",
                    str(args.out),
                    *(["--env-file", str(args.env_file)] if args.env_file else []),
                ],
            ),
        ),
        Step(
            "quote_scout",
            ("quote_candidates.json",),
            lambda args: add_extra_args(
                args,
                "quote_scout",
                [
                    *step_argv("editorial.quote_scout", args.python_exec),
                    "--transcript",
                    str(args.out / "transcript.json"),
                    "--out",
                    str(args.out),
                    *(["--env-file", str(args.env_file)] if args.env_file else []),
                ],
            ),
        ),
        Step(
            "pool_build",
            ("pool.json",),
            lambda args: add_extra_args(
                args,
                "pool_build",
                [
                    *step_argv("training.pool_build", args.python_exec),
                    "--triage",
                    str(args.out / "scene_triage.json"),
                    "--scene-descriptions",
                    str(args.out / "scene_descriptions.json"),
                    "--quote-candidates",
                    str(args.out / "quote_candidates.json"),
                    "--transcript",
                    str(args.out / "transcript.json"),
                    "--scenes",
                    str(args.out / "scenes.json"),
                    "--source-slug",
                    args.source_slug,
                    "--out",
                    str(args.out),
                ],
            ),
        ),
        Step(
            "pool_merge",
            (),
            lambda args: add_extra_args(
                args,
                "pool_merge",
                [
                    *step_argv("training.pool_merge", args.python_exec),
                    "--pool",
                    str(args.out / "pool.json"),
                    "--out",
                    str(args.out / "pool.json"),
                    # extends prior plan Step 14
                    *(["--theme", str(args.theme)] if getattr(args, "theme_explicit", False) and getattr(args, "theme", None) else []),
                ],
            ),
            always_run=True,
        ),
        Step(
            "arrange",
            ("arrangement.json",),
            lambda args: add_extra_args(
                args,
                "arrange",
                [
                    *step_argv("editorial.arrange", args.python_exec),
                    "--pool",
                    str(args.out / "pool.json"),
                    "--brief",
                    str(args.brief_copy),
                    "--out",
                    str(args.brief_out),
                    "--source-slug",
                    args.source_slug,
                    "--brief-slug",
                    args.brief_slug,
                    # extends prior plan Step 14
                    *(["--theme", str(args.theme)] if getattr(args, "theme_explicit", False) and getattr(args, "theme", None) else []),
                    *(
                        ["--target-duration", f"{target_duration:.6f}"]
                        if (target_duration := _arrange_target_duration(args)) is not None
                        else []
                    ),
                    *(["--allow-generative-effects"] if (args.video is None or getattr(args, "allow_generative_effects", False) or getattr(args, "brief_allow_generative_visuals", False)) else []),
                    *(["--no-audio"] if args.video is None and args.audio is None else []),
                    *(["--env-file", str(args.env_file)] if args.env_file else []),
                ],
            ),
            per_brief=True,
        ),
        Step("cut", ("hype.timeline.json", "hype.assets.json", "hype.metadata.json"), lambda args: add_extra_args(args, "cut", build_pool_cut_cmd(args)), per_brief=True),
        # refine mutates the cut sentinels, so should_rerun also compares their mtimes against refine.json.
        Step(
            "refine",
            ("refine.json",),
            lambda args: add_extra_args(
                args,
                "refine",
                [
                    *step_argv("editorial.refine", args.python_exec),
                    "--arrangement",
                    str(args.brief_out / "arrangement.json"),
                    "--pool",
                    str(args.out / "pool.json"),
                    "--timeline",
                    str(args.brief_out / "hype.timeline.json"),
                    "--assets",
                    str(args.brief_out / "hype.assets.json"),
                    "--metadata",
                    str(args.brief_out / "hype.metadata.json"),
                    "--transcript",
                    str(args.out / "transcript.json"),
                    "--out",
                    str(args.brief_out),
                    *(["--primary-asset", args.primary_asset] if getattr(args, "primary_asset", None) else []),
                    *(["--env-file", str(args.env_file)] if args.env_file else []),
                ],
            ),
            per_brief=True,
        ),
        Step(
            "render",
            ("hype.mp4", "hype.mp4.provenance.json"),
            build_hype_render_cmd,
            per_brief=True,
        ),
        Step(
            "editor_review",
            ("editor_review.json",),
            lambda args: add_extra_args(
                args,
                "editor_review",
                [
                    *step_argv("editorial.editor_review", args.python_exec),
                    "--brief-dir",
                    str(args.brief_out),
                    "--run-dir",
                    str(args.out),
                    "--out",
                    str(args.brief_out),
                    "--iteration",
                    str(getattr(args, "editor_iteration", 1)),
                    *(["--env-file", str(args.env_file)] if args.env_file else []),
                ],
            ),
            per_brief=True,
        ),
        Step(
            "validate",
            ("validation.json",),
            lambda args: add_extra_args(
                args,
                "validate",
                [
                    *step_argv("editorial.validate", args.python_exec),
                    "--video",
                    str(args.brief_out / "hype.mp4"),
                    "--timeline",
                    str(args.brief_out / "hype.timeline.json"),
                    "--metadata",
                    str(args.brief_out / "hype.metadata.json"),
                    "--out",
                    str(args.brief_out / "validation.json"),
                    *(["--env-file", str(args.env_file)] if args.env_file else []),
                ],
            ),
            per_brief=True,
        ),
        Step(
            "verdict",
            ("verdict.json",),
            _verdict_build_cmd,
            per_brief=True,
        ),
    ]


def _initial_facts(args: argparse.Namespace) -> set[str]:
    """Compute the set of pipeline facts available before any step runs.

    Facts are matched against each executor's `pipeline_requirements`; a step
    runs when its requirements are a subset of the running facts, where
    each step that runs adds its `graph.provides` to the set.
    """
    facts: set[str] = {"brief", "theme"}
    if args.video is not None:
        facts.update({"source_video", "video", "source_media"})
    if args.audio is not None:
        facts.update({"source_audio", "audio", "source_media"})
    if getattr(args, "target_duration", None) is not None:
        facts.add("target_duration")
    # Precedence: explicit CLI --allow-generative-effects wins,
    # else the brief's allow_generative_visuals frontmatter, else False.
    if getattr(args, "allow_generative_effects", False) or getattr(
        args, "brief_allow_generative_visuals", False
    ):
        facts.add("generative_visuals_enabled")
    return facts

def select_steps(args: argparse.Namespace) -> list[Step]:
    """Select pipeline steps via manifest-declared requirements.

    Walks STEP_ORDER (used as the topological hint) and includes each step
    whose executor's `pipeline_requirements` are satisfied by the running
    facts set. Each step that runs contributes its `graph.provides` for
    downstream steps. Replaces the old mode-fork logic; equivalent for
    source-video, audio-only, and pure-generative briefs.
    """
    from astrid.pack_api import load_default_registry

    registry = load_default_registry()
    executors_by_step = {
        executor.metadata.get("pipeline_step"): executor
        for executor in registry.list()
        if executor.metadata.get("pipeline_step")
    }
    facts = _initial_facts(args)
    all_steps = {step.name: step for step in build_pool_steps()}
    selected: list[Step] = []
    for name in STEP_ORDER:
        step = all_steps.get(name)
        if step is None:
            continue
        executor = executors_by_step.get(name)
        if executor is None:
            selected.append(step)
            continue
        requirements = set(executor.pipeline_requirements)
        if not requirements.issubset(facts):
            continue
        selected.append(step)
        facts.update(executor.graph.provides or ())
    return selected

def _write_dry_run_plan(args: argparse.Namespace) -> int:
    """Write hype.plan.json with the computed step set + redacted commands."""
    from .runner import _redact_command  # late import to avoid circular dependency
    facts = sorted(_initial_facts(args))
    selected = select_steps(args)
    skipped_explicit = set(getattr(args, "skip", ()) or ())
    final_steps = [step for step in selected if step.name not in skipped_explicit]
    selected_step_payload: list[dict[str, Any]] = []
    for step in final_steps:
        try:
            cmd = step.build_cmd(args)
        except Exception as exc:  # pragma: no cover - dry-run never raises mid-loop
            cmd = [f"<unbuildable: {exc}>"]
        selected_step_payload.append(
            {
                "name": step.name,
                "per_brief": step.per_brief,
                "sentinels": list(step.sentinels),
                "argv_redacted": _redact_command(cmd),
            }
        )
    skipped_payload = [
        {"name": step.name, "reason": "skipped via --skip"}
        for step in selected
        if step.name in skipped_explicit
    ]
    all_known = set(STEP_ORDER)
    excluded_by_capability = sorted(all_known - {s.name for s in selected})
    payload = {
        "tool": "hype",
        "phase": "dry-run",
        "version": 1,
        "runtime_facts": facts,
        "capability_intent": {
            "video": args.video is not None,
            "audio": args.audio is not None,
            "allow_generative_effects": args.allow_generative_effects,
            "brief_allow_generative_visuals": getattr(
                args, "brief_allow_generative_visuals", False
            ),
            "target_duration": getattr(args, "target_duration", None),
        },
        "selected_steps": selected_step_payload,
        "skipped_steps": skipped_payload,
        "excluded_by_capability": excluded_by_capability,
    }
    plan_path = args.out / "hype.plan.json"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"hype.plan.json written: {plan_path}")
    print(f"  selected steps ({len(selected_step_payload)}): {[s['name'] for s in selected_step_payload]}")
    if skipped_payload:
        print(f"  skipped via --skip: {[s['name'] for s in skipped_payload]}")
    if excluded_by_capability:
        print(f"  excluded by capability/facts: {excluded_by_capability}")
    return 0
