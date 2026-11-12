from __future__ import annotations

import importlib
import json
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

from astrid.core._shared import run_logs
from astrid.core.generation.engine_custody import engine_log_path
from astrid.packs.vibecomfy import local_job


def _owned_action(monkeypatch, tmp_path, *, fail_declare=False):
    source = tmp_path / "workflow.py"
    source.write_text("{}")
    source.with_suffix(".vibe.json").write_text("{}")
    source.with_name("source.json").write_text("{}")
    loaded = SimpleNamespace(resolved=object())
    import astrid.packs.vibecomfy.production_engine as production
    monkeypatch.setattr(production, "load_workflow_path", lambda *_args: loaded)
    monkeypatch.setattr(production, "_canonical_bundle_value", lambda _value: SimpleNamespace(workflow=object()))
    monkeypatch.setattr(production, "loaded_workflow_execution_identity", lambda _loaded: "execution")

    class Preparation:
        root = str(tmp_path / "engine-root")
        recipe = SimpleNamespace(engine="comfyui")

        def __init__(self, **_kwargs):
            log = Path(engine_log_path(self.root, self.recipe.engine))
            log.parent.mkdir(parents=True)
            log.write_bytes(b"x" * 40)

        def prepare(self, *_args, **_kwargs):
            return {"endpoint": "http://local"}

        def close(self, **_kwargs):
            pass

    import astrid.core.generation.managed_preparation as managed
    monkeypatch.setattr(managed, "ManagedEnginePreparation", Preparation)
    monkeypatch.setattr(managed, "preparation_scope", lambda _report: nullcontext())
    action = importlib.import_module("astrid.packs.vibecomfy.actions.run.run")
    monkeypatch.setattr(action, "_run_and_settle", lambda *_args, **_kwargs: {"ok": True})
    if fail_declare:
        monkeypatch.setattr(run_logs, "declare", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk")))
    result = local_job._run_owned_action(source, tmp_path / "out", task_identity="task", attempt_identity="attempt",
                                         align="check")
    return result, tmp_path / "out", Path(engine_log_path(Preparation.root, Preparation.recipe.engine))


def test_owned_job_declares_engine_offset_before_preparation(monkeypatch, tmp_path):
    _result, out, log = _owned_action(monkeypatch, tmp_path)
    entry = json.loads((out / "logs.json").read_text())["logs"][0]
    assert entry["name"] == "engine"
    assert entry["role"] == "engine"
    assert entry["path"] == str(log)
    assert entry["start"] == 40


def test_owned_job_ignores_declaration_failure(monkeypatch, tmp_path):
    result, _out, _log = _owned_action(monkeypatch, tmp_path, fail_declare=True)
    assert result == {"ok": True}


def _with_comfy(monkeypatch, tmp_path):
    from tests.packs.vibecomfy.test_local_server_run import comfy as comfy_fixture

    fixture = comfy_fixture.__wrapped__(tmp_path, monkeypatch)
    return fixture, next(fixture)


def _prepared_server_run(monkeypatch, tmp_path, *, fail_declare=False):
    fixture, comfy = _with_comfy(monkeypatch, tmp_path)
    prepared = {"placement": "managed", "alignment": {"mode": "check"},
                "manifest": {"models": []}, "execution_identity": "execution"}
    monkeypatch.setattr("astrid.core.generation.managed_preparation.read_preparation", lambda **_kwargs: prepared)
    runtime = importlib.import_module("vibecomfy.runtime.run")
    monkeypatch.setattr(runtime, "run_sync", lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("forced run failure")))
    if fail_declare:
        monkeypatch.setattr(run_logs, "declare_child", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk")))
    from astrid.packs.vibecomfy import production_engine

    destination = tmp_path / "attempt" / "outputs"
    try:
        try:
            production_engine.run_workflow_result_path(
                comfy.workflow, destination, task_identity="task", attempt_identity="attempt",
                comfy_url=comfy.url, profile_id="local_server",
            )
        except ValueError as error:
            assert str(error) == "forced run failure"
        else:
            raise AssertionError("run_sync failure was not propagated")
        return destination
    finally:
        fixture.close()


def test_prepared_server_declares_child_before_run_failure(monkeypatch, tmp_path):
    destination = _prepared_server_run(monkeypatch, tmp_path)
    data = json.loads((destination / "logs.json").read_text())
    run_dir = next((destination / ".vibecomfy-runtime" / "out" / "runs").iterdir())
    assert data["children"] == [str((run_dir / "logs.json").relative_to(destination))]


def test_server_run_failure_is_preserved_when_child_declaration_fails(monkeypatch, tmp_path):
    _prepared_server_run(monkeypatch, tmp_path, fail_declare=True)
