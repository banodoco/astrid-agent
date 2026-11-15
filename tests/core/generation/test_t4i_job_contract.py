"""Plan 32 T4i: ``generate <entry> --placement local --job`` runs a named entry through the facade's contract.

(a) entry binds and defaults, (b) a contribution's build and after_outputs once each (its proof,
``test_job_runs_h3_build_and_after_outputs_once_each``, needs T6b-3's hooks and lands with that merge), (c) readiness
and target_schema on the machine that runs the job.
"""
from __future__ import annotations

import json

import pytest

from astrid.core.generation import cli, entries

RUN = "astrid.packs.vibecomfy.actions.run.run._run_and_settle"


def _job(tmp_path, entry, inputs, *, align="trust"):
    path = tmp_path / "job.json"
    path.write_text(json.dumps({"schema": "astrid.machine-job.v1", "entry": entry, "mode": None, "align": align,
                                "task_identity": "task-t4i", "attempt_identity": "attempt-t4i", "inputs": inputs}))
    return path


def _run_cli(tmp_path, entry, inputs, **kwargs):
    return cli._main([entry, "--placement", "local", "--job", str(_job(tmp_path, entry, inputs, **kwargs)),
                      "--out", str(tmp_path / "out"), "--json"])


@pytest.fixture
def user_entry(tmp_path, monkeypatch):
    """A user entry whose public name ``label`` is bound to the workflow input ``prefix``, with a default."""
    from vibecomfy.workflow_bundle import emit_bundle
    from vibecomfy.workflow import VibeOutput, VibeWorkflow, WorkflowSource
    monkeypatch.setenv("VIBECOMFY_ENTRIES_DIR", str(tmp_path / "entries"))
    monkeypatch.setenv("ASTRID_INTERNAL_INVOCATION", "1")
    folder = tmp_path / "entries" / "bound-image"
    folder.mkdir(parents=True)
    workflow = VibeWorkflow("bound-image", WorkflowSource("bound-image"))
    workflow.add_node("SaveImage", _id="1", uid="sink", filename_prefix="x")
    workflow.register_input("prefix", "1", "filename_prefix", "x", type="STRING")
    workflow.outputs = [VibeOutput("1", "SaveImage", name="image", artifact_kind="image", mime_type="image/png")]
    (folder / "source.json").write_text(json.dumps({"nodes": [
        {"id": 1, "type": "SaveImage", "inputs": [], "widgets_values": ["x"]}], "links": []}))
    emit_bundle(workflow, folder / "workflow.py", {"operation": "authored"})
    (folder / "entry.json").write_text(json.dumps({
        "id": "bound-image", "kind": "image", "modes": ["t2i"], "bundle": "workflow.py", "needs": [],
        "binds": {"label": "prefix"}, "defaults": {"label": "from-default"}}))
    return "bound-image"


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    """Stand in for the owned engine preparation a local job runs first (47e77a513); record its custody."""
    import contextlib
    from types import SimpleNamespace
    from astrid.core.generation import managed_preparation
    events = []

    class Preparation:
        root, recipe = str(tmp_path / "engine"), SimpleNamespace(engine="comfyui")

        def __init__(self, **kwargs):
            pass

        def prepare(self, workflow, **kwargs):
            events.append("prepare")
            return {"endpoint": "http://127.0.0.1:8188"}

        def close(self, *, execution_root, worker_drained):
            assert worker_drained
            events.append("close")

    monkeypatch.setattr(managed_preparation, "ManagedEnginePreparation", Preparation)
    monkeypatch.setattr(managed_preparation, "preparation_scope", lambda report: contextlib.nullcontext())
    return events


@pytest.fixture
def ready(monkeypatch):
    seen = []
    monkeypatch.setattr("astrid.core.generation.preflight.require_generation_target_readiness",
                        lambda entry, mode, client: seen.append((entry.id, mode, client)))
    return seen


def test_job_applies_entry_binds_and_defaults_like_the_facade(user_entry, ready, prepared, tmp_path, monkeypatch):
    runs = []
    monkeypatch.setattr(RUN, lambda workflow, out, **kwargs: runs.append(kwargs) or {"outputs": []})
    assert _run_cli(tmp_path, user_entry, {"label": "supplied"}) == 0
    assert json.loads(runs[0]["workflow_inputs"]) == {"prefix": "supplied"}  # bind: label -> prefix
    assert _run_cli(tmp_path, user_entry, {}) == 0
    assert json.loads(runs[1]["workflow_inputs"]) == {"prefix": "from-default"}  # the entry's default
    assert prepared == ["prepare", "close"] * 2  # each job prepares and releases the engine once


def test_job_runs_readiness_and_target_schema_on_the_machine(user_entry, ready, prepared, tmp_path, monkeypatch):
    runs, schema = [], []
    monkeypatch.setattr(RUN, lambda workflow, out, **kwargs: runs.append(kwargs) or {"outputs": []})
    real = entries.target_schema
    monkeypatch.setattr(entries, "target_schema", lambda *a, **k: schema.append((a[0]["id"], k.get("align"))) or real(*a, **k))
    assert _run_cli(tmp_path, user_entry, {}) == 0
    assert ready == [(user_entry, "t2i", None)] and schema == [(user_entry, "trust")]

    # A not-ready machine refuses before anything runs.
    from astrid.core.contracts.errors import AstridError

    def refuse(entry, mode, client):
        raise AstridError("engine not ready", code="COMFY_TARGET_NOT_READY", field="target")

    monkeypatch.setattr("astrid.core.generation.preflight.require_generation_target_readiness", refuse)
    # e724eb501: the job command re-raises a typed AstridError for the structured JSON renderer.
    with pytest.raises(AstridError, match="engine not ready") as caught:
        _run_cli(tmp_path, user_entry, {})
    assert caught.value.code == "COMFY_TARGET_NOT_READY"
    assert len(runs) == 1

    # An entry that needs review is refused by target_schema, before the run.
    monkeypatch.setattr("astrid.core.generation.preflight.require_generation_target_readiness", lambda *a: None)
    monkeypatch.setattr(entries, "target_schema", lambda *a, **k: (_ for _ in ()).throw(ValueError("entry requires review: x")))
    with pytest.raises(ValueError, match="requires review"):
        _run_cli(tmp_path, user_entry, {})
    assert len(runs) == 1
