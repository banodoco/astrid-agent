"""A prepared worker command owns its registry and discovery observations."""
from __future__ import annotations

import json
from contextlib import contextmanager
from types import SimpleNamespace


def _ready_entry(entry_id: str, bundle: str, *, choices: list[str] | None = None) -> dict:
    prompt = {"name": "prompt", "type": "string", "required": True}
    if choices is not None:
        prompt["choices"] = choices
    return {
        "id": entry_id,
        "kind": "image",
        "modes": ["t2i"],
        "bundle": bundle,
        "inputs": [prompt],
        # Declared inputs let the worker validate this synthetic ready entry
        # without loading a workflow from the synthetic bundle path.
        "contribution": "synthetic-worker-entry",
        "binds": {},
        "defaults": {},
        "requirements": {"models": []},
    }


def _prepared_job(path, entry: str, prompt: str, *, bindings: dict | None = None) -> None:
    path.write_text(json.dumps({
        "schema": "astrid.machine-job.v1",
        "align": "check",
        "entry": entry,
        "mode": "t2i",
        "inputs": {"prompt": prompt},
        "execution": {"python": "prepared/workflow.py"},
        "bindings": bindings or {"seed": 17},
    }))


def _stub_prepared_target(monkeypatch, tmp_path):
    from astrid.packs.vibecomfy.shared import bundle_inputs

    @contextmanager
    def staged(**_kwargs):
        yield tmp_path / "prepared-workflow.py", None

    calls = []
    monkeypatch.setattr(bundle_inputs, "staged_workflow_path", staged)
    from astrid.packs.vibecomfy import local_job
    monkeypatch.setattr(local_job, "run_owned_action",
                        lambda workflow, out, **kwargs: calls.append((workflow, out, kwargs)) or {"ok": True})
    return calls


def test_prepared_run_job_refreshes_named_ready_entry_each_command(monkeypatch, tmp_path):
    from astrid.core.generation import entries, progress
    from astrid.core.model_catalog.registry import ModelRegistry
    from astrid.core.generation.local_job import run_job

    observations = {"registry": 0, "users": 0, "actions": 0, "ready": 0, "complete": 0}

    class Registry:
        def get(self, _entry_id):
            raise KeyError(_entry_id)

    def load(cls):
        observations["registry"] += 1
        return Registry()

    def users():
        observations["users"] += 1
        return []

    def actions():
        observations["actions"] += 1
        return []

    def ready_rows(*, registry=None):
        observations["ready"] += 1
        prompt = "first" if observations["ready"] == 1 else "second"
        return [_ready_entry("worker-entry", str(tmp_path / "bundle.py"), choices=[prompt])]

    def complete(row, *, discovery):
        observations["complete"] += 1
        return row

    monkeypatch.setattr(ModelRegistry, "load_default", classmethod(load))
    monkeypatch.setattr(entries, "user_entries", users)
    monkeypatch.setattr(entries, "action_entries", actions)
    monkeypatch.setattr(entries, "_ready_entry_rows", ready_rows)
    monkeypatch.setattr(entries, "_complete_ready_entry", complete)
    monkeypatch.setattr(entries, "_discover_ready_workflows", lambda: object())
    monkeypatch.setattr(progress, "write_progress", lambda *_args, **_kwargs: None)
    calls = _stub_prepared_target(monkeypatch, tmp_path)

    for index, prompt in enumerate(("first", "second")):
        job_path = tmp_path / f"job-{index}.json"
        _prepared_job(job_path, "worker-entry", prompt, bindings={"seed": 17 + index})
        assert run_job("worker-entry", job_path, tmp_path / f"out-{index}") == {"ok": True}

    assert observations == {"registry": 2, "users": 2, "actions": 2, "ready": 2, "complete": 2}
    assert [call[2]["entry"] for call in calls] == ["worker-entry", "worker-entry"]
    assert [json.loads(call[2]["workflow_inputs"]) for call in calls] == [{"seed": 17}, {"seed": 18}]


def test_prepared_catalog_fallback_keeps_controller_bindings(monkeypatch, tmp_path):
    from astrid.core.generation import entries, progress
    from astrid.core.model_catalog.registry import ModelRegistry
    from astrid.core.generation.local_job import run_job

    target = SimpleNamespace(template="ready-template")
    spec = SimpleNamespace(supports=["prompt"], requires=[], backends={"local": target})
    catalog = SimpleNamespace(id="catalog-entry", modality="image", modes={"t2i": spec})
    loads = []

    class Registry:
        def get(self, entry_id):
            if entry_id == "catalog-entry":
                return catalog
            raise KeyError(entry_id)

    def load(cls):
        loads.append(object())
        return Registry()

    raw = {"users": 0, "actions": 0, "ready": 0}
    monkeypatch.setattr(ModelRegistry, "load_default", classmethod(load))
    monkeypatch.setattr(entries, "user_entries", lambda: raw.__setitem__("users", raw["users"] + 1) or [])
    monkeypatch.setattr(entries, "action_entries", lambda: raw.__setitem__("actions", raw["actions"] + 1) or [])
    monkeypatch.setattr(entries, "_catalog_inputs", lambda *_args: [
        {"name": "prompt", "type": "string", "required": True},
    ])

    def ready_rows(*, registry=None):
        raw["ready"] += 1
        return [_ready_entry("ready-template", str(tmp_path / "ready.py"))]

    monkeypatch.setattr(entries, "_ready_entry_rows", ready_rows)
    monkeypatch.setattr(entries, "_complete_ready_entry", lambda row, **_kwargs: row)
    monkeypatch.setattr(entries, "_discover_ready_workflows", lambda: object())
    monkeypatch.setattr(progress, "write_progress", lambda *_args, **_kwargs: None)
    calls = _stub_prepared_target(monkeypatch, tmp_path)

    job_path = tmp_path / "catalog-job.json"
    native = {"seed": 89, "native_control": 0.25}
    _prepared_job(job_path, "catalog-entry", "catalog prompt", bindings=native)
    assert run_job("catalog-entry", job_path, tmp_path / "out") == {"ok": True}

    assert len(loads) == 1
    assert raw == {"users": 1, "actions": 1, "ready": 1}
    assert calls[0][2]["entry"] == "ready-template"
    assert json.loads(calls[0][2]["workflow_inputs"]) == native
