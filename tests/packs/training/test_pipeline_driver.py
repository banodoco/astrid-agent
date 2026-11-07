from __future__ import annotations

import sys

from astrid.core.execution.executor.runner import ExecutorRunRequest, _run_builtin_executor
from astrid.core.execution.executor.schema import ExecutorDefinition
from astrid.packs.training import pipeline


# Cross-pack command parity belongs to the external video pack test suite.


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
