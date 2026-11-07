from __future__ import annotations

import argparse
import sys
from types import SimpleNamespace

import pytest

from astrid.core.execution.pipeline_step import run_step


def _step():
    return SimpleNamespace(name="fixture", per_brief=False, sentinels=(), invoke=None)


def test_real_command_executes_and_records_log(tmp_path):
    marker = tmp_path / "marker"
    args = argparse.Namespace(out=tmp_path, verbose=False)
    command = [sys.executable, "-c", "from pathlib import Path; import sys; Path(sys.argv[1]).write_text('done'); print('ran')", str(marker)]
    assert run_step(_step(), command, args) == 0
    assert marker.read_text() == "done"
    assert "ran" in (tmp_path / "logs/fixture.log").read_text()


@pytest.mark.parametrize("cancelled, expected", [(False, 124), (True, 143)])
def test_command_timeout_and_cancellation(tmp_path, cancelled, expected):
    args = argparse.Namespace(out=tmp_path, verbose=False, command_timeout=0.05,
                              cancelled=lambda: cancelled)
    assert run_step(_step(), [sys.executable, "-c", "import time; time.sleep(10)"], args) == expected
    assert ("cancelled" if cancelled else "timed out") in (tmp_path / "logs/fixture.log").read_text()


def test_audit_records_redacted_command_and_output(tmp_path):
    class Audit:
        def __init__(self):
            self.nodes = []
        def register_asset(self, **kwargs):
            return kwargs["kind"]
        def register_node(self, **kwargs):
            self.nodes.append(kwargs)

    audit = Audit()
    args = argparse.Namespace(out=tmp_path, verbose=False, audit=audit)
    command = [sys.executable, "-c", "pass", "--token", "secret"]
    assert run_step(_step(), command, args) == 0
    assert audit.nodes[0]["metadata"]["command"][-2:] == ["--token", "<redacted>"]
    assert args.audit_parent_ids == ["log"]


def test_real_callback_runs_in_worker(tmp_path):
    step = _step()
    step.invoke = print
    args = argparse.Namespace(out=tmp_path, verbose=False)
    assert run_step(step, [], args) == 0
    assert "Namespace(" in (tmp_path / "logs/fixture.log").read_text()
    assert not (tmp_path / "logs/fixture.log.request").exists()
