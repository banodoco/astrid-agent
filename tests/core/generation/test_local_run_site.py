"""Direct local runs use owned managed preparation and submit through ComfyUI's HTTP endpoint."""
from __future__ import annotations

import importlib
import os
from pathlib import Path
from pathlib import Path

from astrid.packs.vibecomfy import production_engine
from tests.packs.vibecomfy.test_local_server_run import comfy  # noqa: F401  (the repo's stand-in ComfyUI, CPU only)


def _install_owned_preparation_fixture(monkeypatch, tmp_path, target, *, root=None):
    """Use real install exclusion/requirements/alignment with a CPU HTTP target."""
    from astrid.core.generation import managed_preparation
    from astrid.core.generation.engine_contracts import IntegrityError
    from astrid.core.generation.engine_platforms import platform_adapter
    from astrid.core.generation.engine_recipe import load_recipe, recipe_digest

    root = Path(root).resolve() if root is not None else (tmp_path / "managed-engine").resolve()
    root.mkdir(parents=True, exist_ok=True)
    adapter = platform_adapter("linux-x86_64-cu130")
    observations = {"root": root, "events": [], "lock_states": [], "engine_calls": [],
                    "workflow_callback": None, "report": {"endpoint": target.url},
                    "models_root": root / "runtime" / "ComfyUI" / "models", "manifest_override": None, "engine": None}

    def lock_held():
        from runtime_protocol import platform as native

        path = root / ".astrid-engine" / "install.lock"
        fd = os.open(path, os.O_RDWR)
        try:
            try:
                native.lock_file(fd, exclusive=True, blocking=False)
            except BlockingIOError:
                return True
            native.unlock_file(fd)
            return False
        finally:
            os.close(fd)

    class FakePreparation:
        def __init__(self, *, task, attempt, mode, deadline, cancelled, scratch, **_kwargs):
            self.task, self.attempt, self.mode = task, attempt, mode
            self.deadline, self.cancelled, self.scratch = deadline, cancelled, scratch
            self.root = str(root)
            self.recipe = load_recipe("comfyui", "linux-x86_64-cu130")
            self.lock = None
            self.on_phase = _kwargs.get("on_phase")

        def prepare(self, workflow, *, execution_identity, inputs, canonical_workflow, input_digests):
            self.lock = adapter.local_install_lock(self.root, deadline=self.deadline)
            self.lock.__enter__()
            if observations["workflow_callback"] is not None:
                observations["workflow_callback"](canonical_workflow)
            from astrid.packs.vibecomfy.actions.run import run as run_action

            engine = observations["engine"] or run_action._engine_for({"engine": "comfyui"})
            observations["engine_calls"].append("workflow_requirements")
            observations["lock_states"].append(lock_held())
            manifest = engine.workflow_requirements([canonical_workflow])
            if observations["manifest_override"] is not None:
                manifest = {**manifest, **observations["manifest_override"]}
            manifest = {**manifest, "models_root": str(observations["models_root"])}
            observations["events"].append("workflow_requirements")
            observations["engine_calls"].append("align")
            observations["lock_states"].append(lock_held())
            if self.on_phase is not None:
                self.on_phase("align")
            alignment = engine.align(manifest, self.mode, workflow=workflow)
            observations["events"].append("align")
            if alignment.get("status") == "missing":
                raise IntegrityError("managed engine is missing declared models: "
                                     + ", ".join(alignment["missing"]))
            from astrid.core.generation.managed_preparation import SCHEMA, digest

            # This fixture models the managed preparation's owned start
            # boundary after successful alignment.
            if self.on_phase is not None:
                self.on_phase("start_engine")

            observations["report"].update({
                "schema": SCHEMA, "task_id": self.task, "attempt_id": self.attempt,
                "execution_identity": execution_identity,
                "inputs": dict(input_digests),
                "manifest": manifest, "manifest_sha256": digest(manifest),
                "installation": {"root": self.root, "receipt_sha256": "0" * 64,
                    "recipe_sha256": recipe_digest(self.recipe), "helper_sha256": "0" * 64},
                "output_dir": str(target.output), "placement": "managed", "mode": self.mode,
                "alignment": alignment,
                "health": {"healthy": True, "object_info_sha256": "0" * 64,
                           "required_nodes_present": True},
            })
            return observations["report"]

        def close(self, **_kwargs):
            if self.lock is not None:
                lock, self.lock = self.lock, None
                lock.__exit__(None, None, None)

    monkeypatch.setattr(managed_preparation, "ManagedEnginePreparation", FakePreparation)
    observations["lock_held"] = lock_held
    from astrid.core.generation.backends.comfy_target import ComfyTargetClient

    real_submit = ComfyTargetClient.submit_prompt

    def observe_submit(client, payload):
        observations["events"].append("prompt_submit")
        observations["lock_states"].append(lock_held())
        return real_submit(client, payload)

    monkeypatch.setattr(ComfyTargetClient, "submit_prompt", observe_submit)
    return observations


def test_local_run_uses_owned_preparation_and_submits_under_lock(comfy, tmp_path, monkeypatch):  # noqa: F811
    action = importlib.import_module("astrid.packs.vibecomfy.actions.run.run")

    preparation = _install_owned_preparation_fixture(monkeypatch, tmp_path, comfy)
    profiles = []
    real = production_engine.run_workflow_result_path

    def record(*args, **kwargs):
        profiles.append(kwargs.get("profile_id"))
        return real(*args, **kwargs)

    monkeypatch.setattr(production_engine, "run_workflow_result_path", record)

    out = tmp_path / "attempt" / "outputs"
    manifest = action._run_and_settle(comfy.workflow, out, task_identity="task-t4c", attempt_identity="attempt-t4c")

    assert preparation["engine_calls"] == ["workflow_requirements", "align"]
    assert preparation["events"] == ["workflow_requirements", "align", "prompt_submit"]
    assert preparation["lock_states"] == [True, True, True]
    assert profiles == ["local_server"]  # the engine run is always local_server
    assert len(comfy.prompts) == 1
    assert (out / manifest["managed_generation_result_path"]).is_file()


def test_owned_local_run_reports_preparation_execution_and_finalization_progress(comfy, tmp_path, monkeypatch):  # noqa: F811
    action = importlib.import_module("astrid.packs.vibecomfy.actions.run.run")
    _install_owned_preparation_fixture(monkeypatch, tmp_path, comfy)
    progress = tmp_path / "progress.json"
    monkeypatch.setenv("ASTRID_PROGRESS_PATH", str(progress))
    from astrid.core.generation import progress as progress_module

    writes = []
    real_write = progress_module.write_progress

    def observe(phase, *args, **kwargs):
        result = real_write(phase, *args, **kwargs)
        writes.append((phase, json.loads(progress.read_text())))
        return result

    import json
    monkeypatch.setattr(progress_module, "write_progress", observe)
    action._run_and_settle(comfy.workflow, tmp_path / "attempt" / "outputs",
                           task_identity="task-progress", attempt_identity="attempt-progress")

    assert [phase for phase, _row in writes] == ["align", "start_engine", "run", "finalize"]
    assert [row["percent"] for _phase, row in writes] == [15, 30, 50, 90]


def test_recursive_execution_failure_is_materialized_once(comfy, tmp_path, monkeypatch):  # noqa: F811
    import pytest

    action = importlib.import_module("astrid.packs.vibecomfy.actions.run.run")
    _install_owned_preparation_fixture(monkeypatch, tmp_path, comfy)
    original = RuntimeError("owned execution failed")

    def fail_execution(*_args, **_kwargs):
        raise original

    monkeypatch.setattr(production_engine, "run_workflow_result_path", fail_execution)
    real_materialize = action._materialize_managed_generation_result
    materialized = []

    def count_materialization(*args, **kwargs):
        materialized.append(1)
        return real_materialize(*args, **kwargs)

    monkeypatch.setattr(action, "_materialize_managed_generation_result", count_materialization)
    with pytest.raises(RuntimeError, match="owned execution failed") as error:
        action._run_and_settle(comfy.workflow, tmp_path / "attempt" / "outputs",
                               task_identity="task-recursive", attempt_identity="attempt-recursive")

    assert error.value is original
    assert len(materialized) == 1


def test_local_run_checks_the_workflows_declared_models_before_queueing(comfy, tmp_path, monkeypatch):  # noqa: F811
    """The owned preparation aligns real workflow requirements and refuses an absent model before /prompt."""
    import pytest
    from astrid.core.generation.engine_contracts import IntegrityError

    action = importlib.import_module("astrid.packs.vibecomfy.actions.run.run")
    preparation = _install_owned_preparation_fixture(monkeypatch, tmp_path, comfy)

    def declare_missing_model(workflow):
        workflow.metadata["model_assets"] = [{
            "name": "missing.safetensors", "subdir": "checkpoints", "url": "https://example.invalid/missing",
            "sha256": "0" * 64, "size_bytes": 5,
        }]

    preparation["workflow_callback"] = declare_missing_model
    output = tmp_path / "out"
    with pytest.raises(IntegrityError, match="managed engine is missing declared models") as error:
        action._run_and_settle(comfy.workflow, output, task_identity="t", attempt_identity="a")
    receipt = __import__("json").loads((output / "managed-generation-result.json").read_text())
    assert receipt["task_id"] == "t" and receipt["attempt_id"] == "a"
    assert receipt["outcomes"]["execution"] == {"status": "failed", "code": "IntegrityError"}
    assert "managed engine is missing declared models" in str(error.value)
    assert comfy.prompts == []


def test_local_run_refuses_an_unpinned_custom_node_in_the_canonical_graph(comfy, tmp_path, monkeypatch):  # noqa: F811
    import pytest
    action = importlib.import_module("astrid.packs.vibecomfy.actions.run.run")
    from astrid.core.generation import entries
    preparation = _install_owned_preparation_fixture(monkeypatch, tmp_path, comfy)
    engine = entries.engine_table()["comfyui"]

    def declare_unpinned(workflow):
        workflow.metadata["custom_node_packs"] = {
            "pack": {"url": "https://example.invalid/pack"}
        }

    # Preparation evaluates the frozen canonical graph through workflow_requirements.
    # Give that graph an unpinned custom-node declaration and preserve the engine's refusal.
    preparation["engine"] = engine
    preparation["workflow_callback"] = declare_unpinned
    with pytest.raises(ValueError, match="comfyui extra must") as error:
        action._run_and_settle(comfy.workflow, tmp_path / "out", task_identity="t", attempt_identity="a")
    assert error.value.unpinned is True
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


def test_local_run_refuses_an_unreadable_workflow_before_preparation(comfy, tmp_path, monkeypatch):  # noqa: F811
    """The owned path freezes a canonical workflow before target preparation or HTTP submission."""
    import pytest

    action = importlib.import_module("astrid.packs.vibecomfy.actions.run.run")
    _install_owned_preparation_fixture(monkeypatch, tmp_path, comfy)
    comfy.workflow.write_text("{malformed JSON")
    with pytest.raises(production_engine.ProductionEngineError, match="workflow could not be read"):
        action._run_and_settle(comfy.workflow, tmp_path / "attempt" / "outputs", task_identity="t", attempt_identity="a")
    assert comfy.prompts == []


def test_local_run_does_not_swallow_an_engine_error_from_requirements(comfy, tmp_path, monkeypatch):  # noqa: F811
    """An EngineError from canonical workflow requirement inspection propagates unchanged."""
    import pytest
    from astrid.core.generation import entries
    from astrid.core.generation.engine_contracts import EngineError

    action = importlib.import_module("astrid.packs.vibecomfy.actions.run.run")
    preparation = _install_owned_preparation_fixture(monkeypatch, tmp_path, comfy)
    engine = entries.engine_table()["comfyui"]

    def refused(workflows):
        raise EngineError("engine refused the declared requirements")

    preparation["engine"] = engine
    monkeypatch.setattr(engine, "workflow_requirements", refused)
    with pytest.raises(EngineError, match="engine refused"):
        action._run_and_settle(comfy.workflow, tmp_path / "attempt" / "outputs", task_identity="t", attempt_identity="a")
    assert comfy.prompts == []
