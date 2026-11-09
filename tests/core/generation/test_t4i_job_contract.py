"""Plan 32 T4i: ``generate <entry> --placement local --job`` runs a named entry through the facade's contract.

(a) entry binds and defaults, (b) a contribution's build and after_outputs once each, (c) readiness
and target_schema on the machine that runs the job.
"""
from __future__ import annotations

import functools
import json
import subprocess
from pathlib import Path

import pytest

from astrid.core.generation import cli, entries
from tests.core.generation.test_h3_build_after_outputs import REQUEST, _video

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
def ready(monkeypatch):
    seen = []
    monkeypatch.setattr("astrid.core.generation.preflight.require_generation_target_readiness",
                        lambda entry, mode, client: seen.append((entry.id, mode, client)))
    return seen


def test_job_applies_entry_binds_and_defaults_like_the_facade(user_entry, ready, tmp_path, monkeypatch):
    runs = []
    monkeypatch.setattr(RUN, lambda workflow, out, **kwargs: runs.append(kwargs) or {"outputs": []})
    assert _run_cli(tmp_path, user_entry, {"label": "supplied"}) == 0
    assert json.loads(runs[0]["workflow_inputs"]) == {"prefix": "supplied"}  # bind: label -> prefix
    assert _run_cli(tmp_path, user_entry, {}) == 0
    assert json.loads(runs[1]["workflow_inputs"]) == {"prefix": "from-default"}  # the entry's default


def test_job_runs_readiness_and_target_schema_on_the_machine(user_entry, ready, tmp_path, monkeypatch):
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
    with pytest.raises(ValueError, match="engine not ready"):  # --json errors surface as the readiness error
        _run_cli(tmp_path, user_entry, {})
    assert len(runs) == 1

    # An entry that needs review is refused by target_schema, before the run.
    monkeypatch.setattr("astrid.core.generation.preflight.require_generation_target_readiness", lambda *a: None)
    monkeypatch.setattr(entries, "target_schema", lambda *a, **k: (_ for _ in ()).throw(ValueError("entry requires review: x")))
    with pytest.raises(ValueError, match="requires review"):
        _run_cli(tmp_path, user_entry, {})
    assert len(runs) == 1


def test_job_runs_h3_build_and_after_outputs_once_each(ready, tmp_path, monkeypatch):
    """T6b-3's real h3 hooks, counted. The engine run is stubbed below the seam: the producer's file goes
    through the real ``_materialize_managed_generation_result`` with the job's real ``after_outputs_job``."""
    import hashlib
    import importlib
    import astrid.packs.vibecomfy.extensions  # noqa: F401
    from astrid.core.generation import producer_result
    from tests.test_producer_evidence_f06 import _managed_payload, _write_production
    run_module = importlib.import_module("astrid.packs.vibecomfy.actions.run.run")
    calls, composed = [], []
    real_hooks = entries.entry_hooks

    def counted(fn, name):
        @functools.wraps(fn)  # the engine reads the hook's signature to decide whether to hand it the engine
        def wrapper(*args, **kwargs):
            calls.append(name)
            result = fn(*args, **kwargs)
            if name == "after_outputs":  # job_dir is deleted after the job: hash the candidate now
                composed.append(hashlib.sha256(Path(result[0]).read_bytes()).hexdigest())
            return result
        return wrapper

    monkeypatch.setattr(entries, "entry_hooks",
                        lambda entry, registry=None: {n: counted(f, n) for n, f in real_hooks(entry, registry).items()})
    source = _video(tmp_path / "source.mp4", frames=57)
    generated = _video(tmp_path / "generated.mp4", seconds=4.5)
    request = json.loads(REQUEST.read_text())
    asset_map = {request["media"][0]["asset"]["id"]: str(source), "source": str(source)}
    runs = []

    def fake_run(workflow, out, **kwargs):
        runs.append((workflow, kwargs))
        root = tmp_path / "producer"
        payload = _managed_payload(root)
        payload.update(task_id="task-t4i", attempt_id="attempt-t4i")
        data = generated.read_bytes()
        (root / "outputs" / "final.mp4").write_bytes(data)
        payload["outputs"][0].update(bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
        production = _write_production(root, payload)
        settings = payload["inputs"]["effective_settings"]
        producer_result.bind_executed_settings(production, settings, task_id="task-t4i", attempt_id="attempt-t4i",
                                               staging_root=root)
        return run_module._materialize_managed_generation_result(
            Path(out), production, task_identity="task-t4i", attempt_identity="attempt-t4i",
            workflow_path=Path(workflow), after_outputs=run_module._after_outputs(kwargs["after_outputs_job"]))

    monkeypatch.setattr(RUN, fake_run)
    assert _run_cli(tmp_path, "h3", {"request": str(REQUEST), "asset_map": asset_map}) == 0
    assert calls == ["build", "after_outputs"]
    assert len(runs) == 1 and Path(runs[0][0]).name == "workflow.py"
    # The align manifest is the built graph's requirements, not the asset archive's manifest.
    align_manifest = runs[0][1]["manifest"]
    assert "models" in align_manifest and "engine" in align_manifest
    built_folder = Path(runs[0][0]).parent
    assert align_manifest == entries.requirements({"id": "h3", "kind": "video", "bundle": str(built_folder)},
                                                  {"request": str(REQUEST), "asset_map": asset_map})
    first = json.loads((tmp_path / "out" / "managed-generation-result.json").read_text())["outputs"][0] \
        if (tmp_path / "out" / "managed-generation-result.json").is_file() else None
    delivered = next((tmp_path / "out" / "outputs").glob("*.mp4"))
    sealed = hashlib.sha256(delivered.read_bytes()).hexdigest()
    assert sealed == composed[0] != hashlib.sha256(generated.read_bytes()).hexdigest()  # the candidate, not the raw output
    if first is not None:
        assert first["sha256"] == composed[0]
