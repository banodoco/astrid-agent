"""A failed managed preparation step keeps its whole output as declared run logs."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from astrid.core._shared import run_logs
from astrid.core.generation import managed_preparation
from astrid.core.generation.engine_contracts import IntegrityError
from astrid.core.generation.engine_local_endpoint import DEFAULT_PLATFORM
from astrid.core.generation.engine_recipe import load_recipe

STDOUT = "doctor report line\n" * 3
STDERR = "Traceback (most recent call last):\nValueError: workflow node 7 has no input 'seed'\n"


def _preparation(tmp_path, logs_root):
    prep = object.__new__(managed_preparation.ManagedEnginePreparation)
    prep.task, prep.attempt, prep.logs_root = "task-1", "attempt-1", logs_root
    prep.root, prep.deadline = str(tmp_path / "engine"), 0.0
    prep.recipe = load_recipe("comfyui", DEFAULT_PLATFORM)
    prep.adapter = SimpleNamespace(venv_python=lambda path: path + "/bin/python",
                                   join=lambda *parts: "/".join(parts))
    prep.check = lambda: None
    result = SimpleNamespace(exit_code=1, timed_out=False, stdout=STDOUT, stderr=STDERR)
    prep.context = SimpleNamespace(run=lambda request: result)
    return prep


def test_failed_step_output_is_declared_and_named_in_the_error(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    with pytest.raises(IntegrityError) as caught:
        _preparation(tmp_path, out)._native("print()", step="doctor")
    message = str(caught.value)
    assert "preparation.doctor.stdout" in message and "preparation.doctor.stderr" in message
    declared = {row["name"]: row for row in json.loads((out / "logs.json").read_text())["logs"]}
    assert set(declared) == {"preparation.doctor.stdout", "preparation.doctor.stderr"}
    assert declared["preparation.doctor.stderr"]["role"] == "stderr"
    assert open(declared["preparation.doctor.stdout"]["path"]).read() == STDOUT
    assert open(declared["preparation.doctor.stderr"]["path"]).read() == STDERR
    # The collector brings both back whole.
    files = {row["name"]: row for row in run_logs.collect(out)}
    assert (out / "logs" / files["preparation.doctor.stderr"]["file"]).read_text() == STDERR


def test_no_logs_root_keeps_the_previous_failure(tmp_path):
    with pytest.raises(IntegrityError, match="inspect the host log for this attempt"):
        _preparation(tmp_path, None)._native("print()", step="doctor")


def test_an_unwritable_logs_root_never_changes_the_failure(tmp_path):
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("")
    with pytest.raises(IntegrityError, match=r"step=doctor.*inspect the host log for this attempt"):
        _preparation(tmp_path, blocker)._native("print()", step="doctor")


def test_a_successful_step_also_keeps_its_output(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    prep = _preparation(tmp_path, out)
    ok = SimpleNamespace(exit_code=0, timed_out=False, stdout="ComfyUI-QwenTTS: refreshed d8122a8 (declared patches verified: 1)\n", stderr="")
    prep.context = SimpleNamespace(run=lambda request: ok)
    prep._native("print()", step="nodes_restore")
    declared = {row["name"]: row for row in json.loads((out / "logs.json").read_text())["logs"]}
    assert "declared patches verified: 1" in open(declared["preparation.nodes_restore.stdout"]["path"]).read()
