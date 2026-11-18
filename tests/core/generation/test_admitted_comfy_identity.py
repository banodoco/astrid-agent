"""Admission, machine transport and settlement with a loopback ComfyUI double."""
from __future__ import annotations

import hashlib
import importlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.core.execution import generic_host
from astrid.core.execution.machine_run import MachineJob
from astrid.core.generation.local_job import run_job
from astrid.packs.runpod import machine
from astrid.packs.vibecomfy import local_job, production_engine
from astrid.sdk.workspace_client import WorkspaceClient
from tests.integrations.test_remote_machine_job_seam_cpu import FileTransport, LeaseOwner
from tests.packs.vibecomfy.test_local_server_run import comfy  # noqa: F401
from tests.sdk.test_execution_request_binding import _Generated


def _digest(value):
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _admit(comfy, tmp_path, *, kernel_client=False):
    from vibecomfy.workflow_bundle import emit_bundle

    loaded = production_engine.load_workflow_path(comfy.workflow, tmp_path / "author", comfy_url=comfy.url)
    workflow = production_engine._canonical_bundle_value(loaded.resolved).workflow
    folder = tmp_path / "bundle"
    folder.mkdir()
    emit_bundle(workflow, folder / "workflow.py", {"operation": "authored"})
    (folder / "source.json").write_bytes(comfy.workflow.read_bytes())
    members = {"python": folder / "workflow.py", "companion": folder / "workflow.vibe.json",
               "source": folder / "source.json"}
    descriptors, objects = {}, {}
    for name, path in members.items():
        data = path.read_bytes()
        digest = "sha256:" + hashlib.sha256(data).hexdigest()
        descriptors[name] = {"object_id": digest, "filename": path.name}
        objects[digest] = data
    client = object.__new__(WorkspaceClient)
    client._generated = _Generated()
    client.get_object = objects.__getitem__
    bindings = {"seed": 7}
    inputs = {**descriptors, "workflow_inputs": json.dumps(bindings)}
    if kernel_client:
        from astrid.sdk import invocation, remote

        client.list_capabilities = lambda **kwargs: (
            [{"capability_id": "vibecomfy.run", "definition_digest": "sha256:cap"}], None)
        capability = SimpleNamespace(id="vibecomfy.run", capability_type="executor",
                                     inputs=[SimpleNamespace(name=name, type="file") for name in descriptors])
        public = remote.RemoteAstridClient(client)
        for _ in range(2):
            result = invocation._kernel_invoke(capability, kind="executor", project=None, inputs=inputs,
                                               outputs={}, _client=public)
            assert result[5], result[4]
        assert len(client._generated.calls) == 2
        for admission in client._generated.calls:
            assert set(admission["input_object_ids"]) == set(objects)
    else:
        client.admit_task(capability_id="vibecomfy.run", capability_digest="sha256:cap",
                          input_object_ids=list(objects), idempotency_key="identity-test", spec={"inputs": inputs})
    spec = client._generated.calls[0]["spec"]
    expected = {"api_digest": _digest(workflow.compile("api", run_inputs=bindings)),
                "bound_inputs_digest": _digest(bindings)}
    assert spec["invocation_preflight"]["admitted_execution_identity"] == expected
    execution = generic_host._vibecomfy_admission_inputs(spec)
    assert json.loads(execution["admitted_execution_identity"]) == expected
    return members, execution, bindings, expected


def _ship_job(tmp_path, monkeypatch, inputs):
    transport, owner = FileTransport(), LeaseOwner()
    adapter = SimpleNamespace(placement=transport, lease_owner=owner)
    ops = SimpleNamespace(job_start=lambda command, *, job_id: {"id": job_id})
    lease = machine.MachineLease(SimpleNamespace(claim=owner.record), SimpleNamespace(ops=ops),
                                 SimpleNamespace(note=lambda *args, **kwargs: None), {}, False)
    monkeypatch.setattr(machine, "RUNS_ROOT", str(tmp_path / "pod"))
    selection = tmp_path / "selection.zip"
    selection.write_bytes(b"selection")
    monkeypatch.setattr(machine, "build_selection", lambda *args: (selection, {"packs": [], "entries": []}))
    monkeypatch.setattr("astrid.core.generation.settings.model_trust_unchanged", lambda: True)
    job = MachineJob(entry=None, mode=None, policy={}, inputs=inputs, invocation_id="task:attempt",
                     task_id="task", attempt_id="attempt", scratch=tmp_path,
                     progress_path=tmp_path / "progress.json", align="trust")
    remote = machine.RunPodMachine(adapter).start(lease, job)
    job_path = Path(remote.remote_root) / "job.json"
    assert job_path.read_bytes() == transport.uploads[str(job_path)]
    return job_path


@pytest.mark.parametrize(("placement", "scope_prefix"), [("local", True), ("runpod", True), ("local", False)])
def test_admitted_and_actually_submitted_identity_reach_durable_result(
    comfy, tmp_path, monkeypatch, placement, scope_prefix,
):
    action = importlib.import_module("astrid.packs.vibecomfy.actions.run.run")
    monkeypatch.setenv("VIBECOMFY_ON_DEMAND_SCHEMAS", "0")
    members, execution, bindings, admitted = _admit(comfy, tmp_path)
    out = tmp_path / "attempt" / "outputs"
    if not scope_prefix:
        # Stand in for an output node whose graph needs no attempt-specific filename.
        monkeypatch.setattr("astrid.packs.vibecomfy.local_server_engine.run_scoped_overrides",
                            lambda workflow, *_args, **_kwargs: workflow)
    endpoint = {"comfy_url": comfy.url, "comfy_output_dir": str(comfy.output)}
    if placement == "local":
        assert action.main([
            "run", "--python", str(members["python"]), "--companion", str(members["companion"]),
            "--source", str(members["source"]), "--out", str(out),
            "--task-identity", "task", "--attempt-identity", "attempt",
            "--workflow-inputs", json.dumps(bindings), "--comfy-url", comfy.url,
            "--comfy-output-dir", str(comfy.output),
            "--admitted-execution-identity", execution["admitted_execution_identity"],
        ]) == 0
        manifest = json.loads((out / "manifest.json").read_text())
    else:
        # Use the test endpoint in place of provisioning an engine.
        def owned(workflow, output, **kwargs):
            return action._run_and_settle(workflow, output, **endpoint, **kwargs)

        monkeypatch.setattr(local_job, "run_owned_action", owned)
        inputs = {name: str(path) for name, path in members.items()}
        path = _ship_job(tmp_path, monkeypatch, {**inputs, **execution, "workflow_inputs": json.dumps(bindings)})
        assert json.loads(path.read_text())["execution"]["admitted_execution_identity"] == execution["admitted_execution_identity"]
        manifest = run_job("workflow", path, out)

    envelope = json.loads((out / manifest["managed_generation_result_path"]).read_bytes())
    assert envelope["outcomes"]["execution"]["status"] == "succeeded"
    [prompt] = comfy.prompts
    assert prompt["1"]["inputs"]["seed"] == bindings["seed"]
    assert prompt["2"]["inputs"]["filename_prefix"].endswith("_astrid_attempt") == scope_prefix
    executed = {"api_digest": _digest(prompt), "bound_inputs_digest": _digest(bindings)}
    assert (admitted["api_digest"] != executed["api_digest"]) == scope_prefix
    assert envelope["evidence"]["producer"]["execution_identity"] == {
        "admitted": admitted, "executed": executed, "matches": not scope_prefix}


def test_live_kernel_admission_preflights_once_before_dispatch(comfy, tmp_path, monkeypatch):
    from astrid.packs.vibecomfy import invocation_preflight

    monkeypatch.setenv("VIBECOMFY_ON_DEMAND_SCHEMAS", "0")
    observed, counts_at_dispatch = [], []
    original = invocation_preflight.preflight_invocation
    dispatch = WorkspaceClient.dispatch_task_admission

    def record(*args, **kwargs):
        observed.append(kwargs["phase"])
        return original(*args, **kwargs)

    def record_dispatch(client, admission):
        counts_at_dispatch.append(len(observed))
        return dispatch(client, admission)

    monkeypatch.setattr(WorkspaceClient, "dispatch_task_admission", record_dispatch)
    monkeypatch.setattr(invocation_preflight, "preflight_invocation", record)
    # A second independent admission of the same bytes still needs its own observation.
    _admit(comfy, tmp_path, kernel_client=True)
    assert observed == ["submission", "submission"]
    assert counts_at_dispatch == [1, 2]


def test_hook_rebuild_keeps_admitted_identity_when_it_selects_another_workflow(tmp_path, monkeypatch):
    from astrid.core.generation import entries, preflight

    description = {"id": "hook", "mode": "t2i", "bundle": str(tmp_path / "entry")}
    rebuilt = tmp_path / "rebuilt"
    rebuilt.mkdir()
    (rebuilt / "workflow.py").write_text("# rebuilt")
    monkeypatch.setattr(entries, "resolve_entry_inputs", lambda *args, **kwargs: (description, {}))
    monkeypatch.setattr(entries, "find_entry", lambda name: description)
    monkeypatch.setattr(entries, "entry_hooks", lambda entry: {"build": "build"})
    monkeypatch.setattr(preflight, "require_generation_target_readiness", lambda *args: None)
    monkeypatch.setattr(entries, "build_entry", lambda *args, **kwargs: {
        "bundle": str(rebuilt), "workflow": object(), "bindings": {"seed": 28}})
    monkeypatch.setattr(entries, "requirements", lambda *args: {})
    monkeypatch.setattr(entries, "target_schema", lambda *args, **kwargs: None)
    admitted = {"api_digest": _digest("original"), "bound_inputs_digest": _digest({"seed": 7})}
    original = tmp_path / "controller.py"
    original.write_text("# controller")
    execution = {"python": str(original), "admitted_execution_identity": json.dumps(admitted)}
    calls = []

    def execute(workflow, out, **kwargs):
        calls.append((workflow, kwargs))
        return {"ok": True}

    monkeypatch.setattr(local_job, "run_owned_action", execute)
    assert local_job.run_prepared_job("hook", {}, execution, {}, tmp_path / "out",
                                      task="task", attempt="attempt", mode="t2i", align="trust") == {"ok": True}
    [(path, args)] = calls
    assert path == rebuilt / "workflow.py"
    assert json.loads(args["workflow_inputs"]) == {"seed": 28}
    assert args["admitted_execution_identity"] == admitted
