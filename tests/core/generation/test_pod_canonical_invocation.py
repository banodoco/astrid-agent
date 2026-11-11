"""A fresh installed console job uses exact in-process runner authority."""
import json
from pathlib import Path
import subprocess
import sys

import pytest

from astrid.packs.runpod.machine import generation_argv
from tests.core.generation.test_pod_job_cli import installed as installed  # noqa: F401


@pytest.mark.installed_smoke
@pytest.mark.parametrize("outcome", ["success", "execution-failure", "cancelled"])
def test_installed_job_has_scoped_authority_and_restores_it(installed, tmp_path, outcome):
    from vibecomfy.workflow import VibeWorkflow, WorkflowSource
    from vibecomfy.workflow_bundle import emit_bundle

    bundle = tmp_path / "bundle"
    bundle.mkdir()
    workflow = VibeWorkflow("cpu-proof", WorkflowSource("cpu-proof"))
    workflow.add_node("SaveImage", _id="1", uid="sink", filename_prefix="proof")
    emit_bundle(workflow, bundle / "workflow.py", {"operation": "authored"})
    (bundle / "source.json").write_text(json.dumps({"nodes": [
        {"id": 1, "type": "SaveImage", "inputs": [], "widgets_values": ["proof"]}], "links": []}))
    (tmp_path / "job.json").write_text(json.dumps({
        "schema": "astrid.machine-job.v1", "entry": "qwen-image-2.1-turbo", "mode": "t2i", "align": "check",
        "task_identity": "task", "attempt_identity": "attempt", "inputs": {"prompt": "cat"}, "bindings": {},
        "execution": {"python": str(bundle / "workflow.py"), "companion": str(bundle / "workflow.vibe.json"),
                      "source": str(bundle / "source.json")}}))
    argv = generation_argv("qwen-image-2.1-turbo", str(tmp_path), mode="t2i")
    script = r'''
import builtins, contextlib, io, json, os, runpy, sys
from astrid.core.pack.entrypoint import canonical_runtime_entrypoint, guard_canonical_entrypoint
from astrid.core.generation import managed_preparation
assert "ASTRID_INTERNAL_INVOCATION" not in os.environ
events=[]
class Preparation:
    def __init__(self, **kwargs):
        assert kwargs["mode"]=="check"
    def prepare(self, *args, **kwargs):
        guard_canonical_entrypoint("vibecomfy.run")
        events.append("prepare")
        return {"endpoint":"http://127.0.0.1:8188"}
    def close(self, **kwargs):
        assert kwargs["worker_drained"]
        events.append("close")
managed_preparation.ManagedEnginePreparation=Preparation
managed_preparation.preparation_scope=lambda report: contextlib.nullcontext()
original_import=builtins.__import__
def execute(*args, **kwargs):
    guard_canonical_entrypoint("vibecomfy.run")
    assert kwargs["align"]=="check"
    events.append("execute")
    if OUTCOME=="execution-failure":
        raise ValueError("fixture execution failed")
    if OUTCOME=="cancelled":
        raise SystemExit(143)
    return {"outputs":[]}
def import_action(name, globals=None, locals=None, fromlist=(), level=0):
    value=original_import(name, globals, locals, fromlist, level)
    if name=="astrid.packs.vibecomfy.actions.run" and "run" in fromlist:
        # The real module's guard executes before this engine-only spy is installed.
        value.run._run_and_settle=execute
    return value
builtins.__import__=import_action
sys.argv=[CONSOLE,*ARGV]
try:
    runpy.run_path(CONSOLE,run_name="__main__")
except SystemExit as exc:
    status=exc.code
assert status=={"success":0,"execution-failure":2,"cancelled":143}[OUTCOME],status
assert events==["prepare","execute","close"],events
assert "ASTRID_INTERNAL_INVOCATION" not in os.environ
for wrong in (None,"other.capability"):
    scope=contextlib.nullcontext() if wrong is None else canonical_runtime_entrypoint(wrong)
    with scope,contextlib.redirect_stderr(io.StringIO()):
        try:
            guard_canonical_entrypoint("vibecomfy.run")
        except SystemExit as exc:
            assert exc.code==2
        else:
            raise AssertionError("canonical authority escaped its exact call scope")
print("SCOPED_AUTHORITY_RESTORED")
'''
    script = ("CONSOLE=" + repr(str(installed.target / "bin/astrid")) + "\nARGV=" + repr(argv[1:])
              + "\nOUTCOME=" + repr(outcome) + "\n" + script)
    env = dict(installed.env)
    env.pop("ASTRID_INTERNAL_INVOCATION", None)
    checked = subprocess.run([sys.executable, "-c", script], cwd=installed.cwd, env=env,
                             capture_output=True, text=True, timeout=60)
    assert checked.returncode == 0, checked.stderr
    assert "SCOPED_AUTHORITY_RESTORED" in checked.stdout
    assert "not meant to be invoked directly" not in checked.stderr


@pytest.mark.installed_smoke
def test_installed_direct_action_still_refuses_without_runner(installed):
    env = dict(installed.env)
    env.pop("ASTRID_INTERNAL_INVOCATION", None)
    denied = subprocess.run([sys.executable, "-c", "import astrid.packs.vibecomfy.actions.run.run"],
                            cwd=installed.cwd, env=env, capture_output=True, text=True, timeout=30)
    assert denied.returncode == 2
    assert "not meant to be invoked directly" in denied.stderr
    assert "vibecomfy.run" in denied.stderr
