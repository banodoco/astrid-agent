"""Plan 32 T4g: the local run path is complete (align modes, the --job form, ASTRID_PROGRESS_PATH)."""
from __future__ import annotations

import importlib
import json
from types import SimpleNamespace

import pytest

from astrid.core.generation import cli
from tests.packs.vibecomfy.test_local_server_run import comfy  # noqa: F401  (CPU stand-in ComfyUI)


def _endpoint(comfy, monkeypatch):  # noqa: F811
    endpoint = SimpleNamespace(endpoint=SimpleNamespace(url=comfy.url), comfy_output_dir=str(comfy.output),
                               model_root_binding=None)
    monkeypatch.setattr("astrid.core.generation.engine_local_endpoint.local_engine_endpoint",
                        lambda *args, **kwargs: endpoint)


@pytest.mark.parametrize("mode", ["align", "check", "trust"])
def test_cli_accepts_all_three_align_modes(mode):
    parser = cli._parser()
    parsed = parser.parse_args(["image", "a cat", "--align", mode])
    assert parsed.align == mode
    from astrid.sdk.generation import _align_mode
    assert _align_mode(mode) == mode


def test_job_align_reaches_engine_align(comfy, tmp_path, monkeypatch):  # noqa: F811
    _endpoint(comfy, monkeypatch)
    action = importlib.import_module("astrid.packs.vibecomfy.actions.run.run")
    seen = []
    engine_cls = type(action._comfyui_engine())

    class Spy(engine_cls):
        def align(self, manifest, mode):
            seen.append((mode, manifest))
            return super().align(manifest, mode)

    monkeypatch.setattr(action, "_comfyui_engine", lambda: Spy())
    manifest = {"models": [], "extra": {"nodes": []}, "engine": "comfyui", "base": None}
    for mode in ("trust", "check"):
        action._run_and_settle(comfy.workflow, tmp_path / mode / "outputs", task_identity="t", attempt_identity="a",
                               align=mode, manifest=manifest)
    assert [row[0] for row in seen] == ["trust", "check"]
    assert seen[0][1] is manifest


def test_job_file_form_runs_end_to_end_on_the_cpu_standin(comfy, tmp_path, monkeypatch, capsys):  # noqa: F811
    _endpoint(comfy, monkeypatch)
    job = tmp_path / "job.json"
    job.write_text(json.dumps({"schema": "astrid.machine-job.v1", "entry": None, "mode": None, "align": "trust",
                               "task_identity": "task-t4g", "attempt_identity": "attempt-t4g",
                               "inputs": {"source": str(comfy.workflow)}}))
    out = tmp_path / "attempt" / "out"
    code = cli._main(["workflow", "--placement", "local", "--job", str(job), "--out", str(out), "--json"])
    assert code == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["managed_generation_result_path"]
    assert (out / printed["managed_generation_result_path"]).is_file()
    assert len(comfy.prompts) == 1


def test_job_file_rejects_a_foreign_document(tmp_path):
    from astrid.core.generation.local_job import JobFileError, read_job
    bad = tmp_path / "job.json"
    bad.write_text(json.dumps({"schema": "nope"}))
    with pytest.raises(JobFileError):
        read_job(bad)


def test_local_run_writes_astrid_progress_path(comfy, tmp_path, monkeypatch, capsys):  # noqa: F811
    _endpoint(comfy, monkeypatch)
    progress = tmp_path / "progress.json"
    monkeypatch.setenv("ASTRID_PROGRESS_PATH", str(progress))
    phases = []
    from astrid.core.generation import progress as module
    real = module.write_progress

    def spy(phase, *args, **kwargs):
        wrote = real(phase, *args, **kwargs)
        phases.append((phase, json.loads(progress.read_text())))
        return wrote

    monkeypatch.setattr(module, "write_progress", spy)
    for target in ("astrid.packs.vibecomfy.actions.run.run.write_progress",
                   "astrid.core.generation.cli.write_progress"):
        try:
            monkeypatch.setattr(target, spy)
        except AttributeError:
            pass
    job = tmp_path / "job.json"
    job.write_text(json.dumps({"schema": "astrid.machine-job.v1", "entry": None, "mode": None, "align": "check",
                               "inputs": {"source": str(comfy.workflow)}}))
    assert cli._main(["workflow", "--placement", "local", "--job", str(job),
                      "--out", str(tmp_path / "attempt" / "out"), "--json"]) == 0
    final = json.loads(progress.read_text())
    assert final == {"phase": "done", "percent": 100}
    names = [name for name, _ in phases]
    assert {"align", "start_engine", "run", "finalize"} <= set(names)
    assert not any(set(row) - {"phase", "percent", "current", "total"} for _, row in phases)
