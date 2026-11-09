"""Training-owned pipeline steps used by the built-in executor."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from astrid.pack_api import executor_argv

STEP_ORDER = ("pool_build", "pool_merge")


@dataclass(frozen=True)
class Step:
    name: str
    sentinels: tuple[str, ...]
    build_cmd: Callable
    per_brief: bool = False
    always_run: bool = False
    invoke: Callable | None = None


def _pool_build(args):
    return [
        *executor_argv("training.pool_build", args.python_exec),
        "--triage", str(args.out / "scene_triage.json"),
        "--scene-descriptions", str(args.out / "scene_descriptions.json"),
        "--quote-candidates", str(args.out / "quote_candidates.json"),
        "--transcript", str(args.out / "transcript.json"),
        "--scenes", str(args.out / "scenes.json"),
        "--source-slug", args.source_slug,
        "--out", str(args.out),
        *args.extra_args.get("pool_build", []),
    ]


def _pool_merge(args):
    return [
        *executor_argv("training.pool_merge", args.python_exec),
        "--pool", str(args.out / "pool.json"),
        "--out", str(args.out / "pool.json"),
        *(["--theme", str(args.theme)] if getattr(args, "theme_explicit", False) and getattr(args, "theme", None) else []),
        *args.extra_args.get("pool_merge", []),
    ]


def build_pool_steps():
    return [Step("pool_build", ("pool.json",), _pool_build),
            Step("pool_merge", (), _pool_merge, always_run=True)]


def prepare_brief_artifacts(args):
    """Training steps do not consume or modify the brief."""
