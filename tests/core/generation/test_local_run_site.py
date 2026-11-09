"""Plan 32 T4c: the local production run site goes through ComfyUIEngine.run, not an embedded or checkout profile."""
from __future__ import annotations

import importlib
from pathlib import Path
from types import SimpleNamespace

from astrid.packs.vibecomfy import production_engine
from tests.packs.vibecomfy.test_local_server_run import comfy  # noqa: F401  (the repo's stand-in ComfyUI, CPU only)


def test_local_run_uses_the_engine_run(comfy, tmp_path, monkeypatch):  # noqa: F811
    action = importlib.import_module("astrid.packs.vibecomfy.actions.run.run")
    # The installed local engine is the stand-in server: no comfy_url is given to the run.
    endpoint = SimpleNamespace(endpoint=SimpleNamespace(url=comfy.url), comfy_output_dir=str(comfy.output),
                               model_root_binding=None)
    monkeypatch.setattr("astrid.core.generation.engine_local_endpoint.local_engine_endpoint",
                        lambda *args, **kwargs: endpoint)
    engine_runs = []
    from astrid.core.generation import entries
    real_table = entries.engine_table()
    selected = []

    class Spy(type(real_table["comfyui"])):
        def run(self, job, local):
            engine_runs.append(job)
            return super().run(job, local)

    monkeypatch.setattr(entries, "engine_table", lambda: selected.append("table") or {"comfyui": Spy()})
    profiles = []
    real = production_engine.run_workflow_result_path

    def record(*args, **kwargs):
        profiles.append(kwargs.get("profile_id"))
        return real(*args, **kwargs)

    monkeypatch.setattr(production_engine, "run_workflow_result_path", record)

    out = tmp_path / "attempt" / "outputs"
    manifest = action._run_and_settle(comfy.workflow, out, task_identity="task-t4c", attempt_identity="attempt-t4c")

    assert len(engine_runs) == 1 and engine_runs[0]["workflow"] == comfy.workflow
    assert profiles == ["local_server"]  # the engine run is always local_server
    assert len(comfy.prompts) == 1
    assert (out / manifest["managed_generation_result_path"]).is_file()
    assert selected == ["table"] and engine_runs[0]["manifest"]["engine"] == "comfyui"  # selected from manifest.engine


def test_local_run_checks_the_workflows_declared_models_before_queueing(comfy, tmp_path, monkeypatch):  # noqa: F811
    """The run site passes the engine's real requirements(): a declared model that is absent refuses before any /prompt."""
    import pytest
    from astrid.core.contracts.errors import AstridValueError
    from astrid.core.generation import entries

    action = importlib.import_module("astrid.packs.vibecomfy.actions.run.run")
    row = next(r for r in entries.ready_entries() if r.get("bundle") and r.get("model_assets"))
    root = tmp_path / "engine"
    (root / "models").mkdir(parents=True)
    monkeypatch.setattr("astrid.core.generation.engine_local_endpoint.default_engine_root", lambda engine: str(root))
    with pytest.raises(AstridValueError, match="missing declared models"):
        action._run_and_settle(Path(row["bundle"]), tmp_path / "out", task_identity="t", attempt_identity="a")
    assert comfy.prompts == []


def test_local_run_refuses_an_unpinned_custom_node_with_a_clear_message(comfy, tmp_path, monkeypatch):  # noqa: F811
    import pytest
    from astrid.core.contracts.errors import AstridValueError

    action = importlib.import_module("astrid.packs.vibecomfy.actions.run.run")
    from astrid.core.generation import entries
    engine = entries.engine_table()["comfyui"]
    unpinned = SimpleNamespace(metadata={"custom_node_packs": {"pack": {"url": "https://example.invalid/pack"}}})
    monkeypatch.setitem(engine.requirements.__globals__, "_workflows", lambda entry, inputs=None: [unpinned])  # no commit
    monkeypatch.setattr(entries, "engine_table", lambda: {"comfyui": engine})
    with pytest.raises(AstridValueError, match="workflow declares an unpinned custom node"):
        action._run_and_settle(comfy.workflow, tmp_path / "out", task_identity="t", attempt_identity="a")
    assert comfy.prompts == []


def test_attached_run_probes_before_submit_and_never_aligns(comfy, tmp_path, monkeypatch):  # noqa: F811
    action = importlib.import_module("astrid.packs.vibecomfy.actions.run.run")
    from astrid.core.generation import entries
    events = []

    class Spy(type(entries.engine_table()["comfyui"])):
        def probe(self, endpoint, timeout=5.0):
            events.append("probe")
            return super().probe(endpoint, timeout)

        def align(self, manifest, mode):
            events.append("align")
            return super().align(manifest, mode)

    monkeypatch.setattr(entries, "engine_table", lambda: {"comfyui": Spy()})
    real = production_engine.run_workflow_result_path
    monkeypatch.setattr(production_engine, "run_workflow_result_path",
                        lambda *a, **k: events.append("submit") or real(*a, **k))
    out = tmp_path / "attempt" / "outputs"
    action._run_and_settle(comfy.workflow, out, task_identity="t", attempt_identity="a",
                           comfy_url=comfy.url, comfy_output_dir=str(comfy.output))
    assert events == ["probe", "submit"]


def test_probe_succeeds_on_the_stand_in_and_fails_cleanly_on_a_closed_port(comfy):  # noqa: F811
    import socket
    from astrid.core.generation import entries
    engine = entries.engine_table()["comfyui"]
    assert engine.probe(comfy.url)["ready"] is True
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    result = engine.probe(f"http://127.0.0.1:{port}", timeout=1)
    assert result["ready"] is False and result["reason"]
