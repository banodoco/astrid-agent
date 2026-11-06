from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pytest

from astrid.core.execution.executor.runner import ExecutorRunRequest, _run_builtin_executor
from astrid.core.execution.executor.schema import ExecutorDefinition
from astrid.packs.training import pipeline
from astrid.packs.video_editing.actions.hype.steps import build_pool_steps as hype_steps


@pytest.mark.parametrize("theme", [False, True])
def test_training_commands_match_retained_hype_provider(tmp_path, theme):
    args = argparse.Namespace(out=tmp_path, python_exec=sys.executable, source_slug="source",
                              theme_explicit=theme, theme=tmp_path / "theme",
                              extra_args={"pool_build": ["--extra"], "pool_merge": ["--more"]})
    old = {step.name: step for step in hype_steps()}
    for step in pipeline.build_pool_steps():
        assert step.build_cmd(args) == old[step.name].build_cmd(args)
        assert (step.sentinels, step.always_run) == (old[step.name].sentinels, old[step.name].always_run)


def test_builtin_dispatch_runs_with_brief_without_touching_it(tmp_path, monkeypatch):
    brief = tmp_path / "brief.txt"
    brief.write_text("original")
    marker = tmp_path / "marker"
    monkeypatch.setattr(pipeline, "executor_argv", lambda *_: [
        sys.executable, "-c", "from pathlib import Path; import sys; Path(sys.argv[1]).write_text('ran')", str(marker)
    ])
    executor = ExecutorDefinition(
        id="training.pool_build", name="Pool build", kind="built_in", version="1",
        metadata={"command_builder": "astrid.packs.training.pipeline.build_pool_steps", "pipeline_step": "pool_build"},
    )
    result = _run_builtin_executor(executor, ExecutorRunRequest(executor_id=executor.id, out=tmp_path, brief=brief))
    assert result.returncode == 0
    assert marker.read_text() == "ran"
    assert brief.read_text() == "original"
