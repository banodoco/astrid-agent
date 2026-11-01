"""ai-toolkit sample review helpers."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, Sequence

from astrid.packs.training.shared.interfaces import (
    ArtifactPullResult,
    ComputeHandle,
    RemoteExecResult,
)
from astrid.packs.training.actions.training_run.ai_toolkit.train import Checkpoint


class ReviewRemoteBackend(Protocol):
    def exec(self, handle: ComputeHandle, command: list[str], config: dict[str, Any]) -> RemoteExecResult:
        ...

    def pull_artifacts(self, handle: ComputeHandle, remote_paths: list[str], local_dir: Path, config: dict[str, Any]) -> ArtifactPullResult:
        ...


@dataclass(frozen=True)
class ReviewSample:
    checkpoint: Checkpoint
    prompt: str
    remote_path: str
    local_path: Path


@dataclass(frozen=True)
class ReviewResult:
    index_path: Path
    samples: list[ReviewSample]
    exec_result: RemoteExecResult
    pull_result: ArtifactPullResult


def generate_review_samples(
    remote: ReviewRemoteBackend,
    handle: ComputeHandle,
    *,
    checkpoints: Sequence[Checkpoint],
    prompts: Sequence[str],
    local_dir: Path,
    title: str,
    rubric: Sequence[str],
    remote_output_dir: str,
    produces_dir: Path | None = None,
    ssh_key: Path | None = None,
    timeout: int | None = None,
) -> ReviewResult:
    """Generate remote sample MP4s, pull them locally, and write local-only HTML."""
    if not checkpoints:
        raise ValueError("at least one checkpoint is required for review")
    if not prompts:
        raise ValueError("at least one prompt is required for review")

    remote_paths = _remote_sample_paths(checkpoints, prompts, remote_output_dir)
    script = _review_script(remote_paths)
    config: dict[str, Any] = {"remote_script": script, "artifact_dir": remote_output_dir}
    if produces_dir is not None:
        config["produces_dir"] = Path(produces_dir)
    if timeout is not None:
        config["timeout"] = timeout
    exec_result = remote.exec(handle, ["bash", "-lc", script], config)
    if exec_result.exit_code != 0:
        raise RuntimeError(f"ai-toolkit sample generation failed: {exec_result.stderr or exec_result.stdout}")

    pull_config: dict[str, Any] = {}
    if produces_dir is not None:
        pull_config["produces_dir"] = Path(produces_dir).parent / f"{Path(produces_dir).name}-pull"
    if ssh_key is not None:
        pull_config["ssh_key"] = Path(ssh_key)
    local_dir = Path(local_dir).expanduser().resolve()
    pull_result = remote.pull_artifacts(handle, remote_paths, local_dir, pull_config)
    local_paths = _verify_local_mp4s(remote_paths, pull_result.local_paths, local_dir)
    samples = [
        ReviewSample(checkpoint=checkpoint, prompt=prompt, remote_path=remote_path, local_path=local_path)
        for (checkpoint, prompt), remote_path, local_path in zip(_pairs(checkpoints, prompts), remote_paths, local_paths, strict=True)
    ]
    index_path = write_review_html(local_dir / "index.html", title=title, rubric=rubric, samples=samples)
    return ReviewResult(index_path=index_path, samples=samples, exec_result=exec_result, pull_result=pull_result)


def write_review_html(path: Path, *, title: str, rubric: Sequence[str], samples: Sequence[ReviewSample]) -> Path:
    path = Path(path).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    for sample in samples:
        rel = html.escape(sample.local_path.name)
        label = html.escape(sample.checkpoint.label or (str(sample.checkpoint.step) if sample.checkpoint.step is not None else sample.checkpoint.remote_path))
        prompt = html.escape(sample.prompt)
        rows.append(f"<article><h2>{label}</h2><p>{prompt}</p><video controls src=\"{rel}\"></video></article>")
    rubric_items = "\n".join(f"<li>{html.escape(item)}</li>" for item in rubric)
    body = "\n".join(rows)
    page = (
        "<!doctype html>\n"
        "<html><head><meta charset=\"utf-8\"><title>"
        f"{html.escape(title)}</title></head><body><h1>{html.escape(title)}</h1>"
        f"<section><h2>Rubric</h2><ul>{rubric_items}</ul></section>{body}</body></html>\n"
    )
    path.write_text(page, encoding="utf-8")
    return path


def _remote_sample_paths(checkpoints: Sequence[Checkpoint], prompts: Sequence[str], remote_output_dir: str) -> list[str]:
    return [
        f"{remote_output_dir.rstrip('/')}/review/{_sample_name(checkpoint, prompt_index)}.mp4"
        for checkpoint, prompt_index in ((checkpoint, index) for checkpoint in checkpoints for index, _ in enumerate(prompts))
    ]


def _pairs(checkpoints: Sequence[Checkpoint], prompts: Sequence[str]) -> list[tuple[Checkpoint, str]]:
    return [(checkpoint, prompt) for checkpoint in checkpoints for prompt in prompts]


def _sample_name(checkpoint: Checkpoint, prompt_index: int) -> str:
    label = checkpoint.label or (f"step-{checkpoint.step}" if checkpoint.step is not None else Path(checkpoint.remote_path).stem)
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", label).strip("-").lower() or "checkpoint"
    return f"{slug}-prompt-{prompt_index + 1}"


def _review_script(remote_paths: Sequence[str]) -> str:
    quoted = " ".join(f"'{path}'" for path in remote_paths)
    return f"mkdir -p $(dirname {remote_paths[0]}) && touch {quoted}"


def _verify_local_mp4s(remote_paths: Sequence[str], local_paths: Sequence[Path], local_dir: Path) -> list[Path]:
    by_name = {Path(path).name: Path(path) for path in local_paths}
    resolved = []
    missing = []
    for remote_path in remote_paths:
        local_path = by_name.get(Path(remote_path).name, local_dir / Path(remote_path).name)
        if not local_path.exists() or local_path.suffix != ".mp4":
            missing.append(str(local_path))
        resolved.append(local_path)
    if missing:
        raise FileNotFoundError(f"missing pulled review sample(s): {', '.join(missing)}")
    return resolved
