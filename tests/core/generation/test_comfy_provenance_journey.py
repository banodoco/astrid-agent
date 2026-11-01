"""A04: local observations, native blockers, and durable terminal evidence."""
from __future__ import annotations

import hashlib
import importlib
import io
import json
import shutil
import socket
import subprocess
import sys
from types import SimpleNamespace

import pytest

from astrid.core.generation import engine_readiness as readiness
from astrid.core.generation.backends import comfy_target, registry, vibecomfy
from astrid.core.generation.backends.vibecomfy import CheckoutServerAdapter
from tests.test_producer_evidence_f06 import _managed_payload, _write_production


@pytest.fixture(autouse=True)
def no_external_work(monkeypatch):
    monkeypatch.setenv("ASTRID_INTERNAL_INVOCATION", "1")

    def forbidden(*args, **kwargs):
        pytest.fail("A04 must not spawn, connect, install, download, or save a human target")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    from astrid.core.generation import targets
    monkeypatch.setattr(targets, "save_target", forbidden)


def _workflow(version="==0.25.0", python=">=3.12"):
    from vibecomfy.contracts.runtime import RuntimeRequirements
    from vibecomfy.workflow import WorkflowRequirements

    return SimpleNamespace(requirements=WorkflowRequirements(
        runtime=RuntimeRequirements(comfy_version=version, python_version=python),
        models=[{"name": "model.safetensors", "subdir": "checkpoints", "sha256": "a" * 64},
                {"name": "vae.safetensors", "subdir": "vae"}],
        custom_node_refs=[{"name": "test-pack", "version": "1.0", "class_set": ["TestNode"]}],
    ))


def _opener(calls, *, version="0.26.0", stats=True):
    def open_request(request, *, timeout):
        assert request.get_method() == "GET"
        calls.append(request.full_url)
        if request.full_url.endswith("/system_stats"):
            if not stats:
                raise OSError("optional version endpoint unavailable")
            payload = {"system": {"comfyui_version": version, "python_version": "3.13.5"}}
        else:
            assert request.full_url.endswith("/object_info")
            payload = {"TestNode": {"cnr_id": "test-pack", "pack_version": "2.0"}}
        return io.BytesIO(json.dumps(payload).encode())
    return open_request


def test_known_drift_warns_unknown_is_explicit_and_receives_no_issue():
    facts = readiness.ComfyProvenance()
    facts.workflow(_workflow())
    facts.system_stats({"system": {"comfyui_version": "0.26.0", "python_version": "3.13.5"}})
    facts.object_info({"TestNode": {"pack_version": "2.0"}})
    evidence = facts.evidence()
    envelope = evidence["comfy_provenance"]["readiness"]
    assert envelope["status"] == "ready_with_warnings"
    assert {issue["details"]["name"] for issue in envelope["issues"]} == {"engine", "pack:test-pack"}
    assert all(issue["code"] == "version_drift" for issue in envelope["issues"])
    assert all(model["identity_status"] == "unknown" for model in evidence["models"])
    facts.system_stats({"system": {"comfyui_version": "unqualified-nightly"}})
    facts.object_info({"TestNode": {}})
    evidence = facts.evidence()
    assert evidence["comfy_provenance"]["readiness"]["issues"] == []
    assert evidence["versions"]["observed"]["pack"]["test-pack"] is None
    assert all(row["status"] == "unknown" for row in evidence["comfy_provenance"]["version_comparisons"])


@pytest.mark.parametrize("stats", [True, False])
def test_attached_observations_are_get_only_and_do_not_grant_hash_or_ownership(stats):
    calls = []
    client = comfy_target.ComfyTargetClient(
        comfy_target.ComfyTarget("http://127.0.0.1:18888", "attached"),
        opener=_opener(calls, stats=stats),
    )
    with readiness.ComfyProvenance().capture() as facts:
        facts.workflow(_workflow())
        client.object_info()
    assert readiness.current_comfy_provenance() is None
    assert calls == ["http://127.0.0.1:18888/object_info", "http://127.0.0.1:18888/system_stats"]
    evidence = facts.evidence()
    assert evidence["versions"]["observed"]["engine"] == ("0.26.0" if stats else None)
    assert all(row["observed_sha256"] is None for row in evidence["models"])
    assert evidence["build"]["source"] is None
    with pytest.raises(ValueError, match="unowned control"):
        client.request_json("/api/free", {"unload_models": True})
    assert len(calls) == 2


@pytest.mark.parametrize("observed", ["0.26.0", None, "nightly"])
def test_workflow_version_observation_does_not_block_execution(monkeypatch, observed):
    calls = []
    monkeypatch.setattr(vibecomfy, "_open_checkout_http", _opener(calls, version=observed))
    adapter = CheckoutServerAdapter("http://127.0.0.1:18888", expected_comfyui_version="==0.25.0")
    adapter._probe_system_stats()
    assert adapter._system_stats_verified
    assert adapter._version_readiness["status"] == ("ready_with_warnings" if observed == "0.26.0" else "ready")
    assert adapter.runtime_instance_id is None  # stats never attest a process incarnation


def test_unqualified_workflow_declaration_is_readiness_silent(monkeypatch):
    monkeypatch.setattr(vibecomfy, "_open_checkout_http", _opener([]))
    adapter = CheckoutServerAdapter("http://127.0.0.1:18888", expected_comfyui_version="nightly")
    adapter._probe_system_stats()
    assert adapter._version_readiness["issues"] == []
    assert adapter._expected_comfyui_version == "nightly"


@pytest.mark.parametrize("observed", ["0.25.0", None, "nightly"])
def test_installed_baseline_version_remains_strict(monkeypatch, observed):
    monkeypatch.setattr(vibecomfy, "_open_checkout_http", _opener([], version=observed))
    adapter = CheckoutServerAdapter("http://127.0.0.1:18888", expected_comfyui_version="==0.25.0")
    adapter._host_profile = {"runtime": {"comfyui_version": "==0.26.0"}}
    with pytest.raises(ValueError, match="installed baseline"):
        adapter._probe_system_stats()
    assert not adapter._system_stats_verified


def test_checkout_producer_allows_workflow_drift_to_reach_the_verified_adapter(tmp_path):
    """Workflow declarations reach execution without weakening profile checks."""
    from contextlib import nullcontext
    from vibecomfy.contracts.runtime import RuntimeRequirements
    from vibecomfy.workflow import VibeWorkflow, WorkflowSource
    from vibecomfy.workflow_bundle import load_bundle
    from astrid.packs.vibecomfy import production_engine

    workflow = VibeWorkflow("a04-drift", WorkflowSource("a04-drift"))
    workflow.requirements.runtime = RuntimeRequirements(comfy_version="==0.25.0")
    bundle = load_bundle(workflow)
    calls = []
    output = tmp_path / "verified-output.png"
    output.write_bytes(b"synthetic output")

    def run_compiled(*args, **kwargs):
        calls.append("executed")
        return [output]

    adapter = SimpleNamespace(run_compiled_workflow=run_compiled, _last_run_result=None,
                              release=lambda **kwargs: pytest.fail("unexpected release"))
    with readiness.ComfyProvenance().capture() as facts, pytest.warns(UserWarning) as warnings:
        result = production_engine._run_profile_result(
            bundle, "checkout_server", {"runtime": {"comfyui_version": "==0.26.0"}},
            model_id="a04", template_id="a04", task_identity="a04-task", destination=tmp_path,
            endpoint_checkout=lambda **kwargs: nullcontext(adapter),
        )
    assert calls == ["executed"]
    assert facts.recorded["engine"] == "==0.25.0"
    assert result.outputs == (output,)
    assert len(warnings) == 1
    assert "workflow='==0.25.0' profile='==0.26.0'" in str(warnings[0].message)
    assert result.readiness["status"] == "ready_with_warnings"
    issue = result.readiness["issues"][0]
    assert issue["code"] == "version_drift"
    assert issue["severity"] == "warning"
    assert issue["details"]["workflow"] == "==0.25.0"
    assert issue["details"]["profile"] == "==0.26.0"
    assert issue in facts.evidence()["comfy_provenance"]["readiness"]["issues"]
    assert facts.observed["engine"] is None  # A profile is not a live observation.


def test_checkout_producer_still_rejects_installed_baseline_mismatch(tmp_path, monkeypatch):
    from contextlib import nullcontext
    from vibecomfy.contracts.runtime import RuntimeRequirements
    from vibecomfy.workflow import VibeWorkflow, WorkflowSource
    from vibecomfy.workflow_bundle import load_bundle
    from astrid.packs.vibecomfy import production_engine

    workflow = VibeWorkflow("a04-baseline", WorkflowSource("a04-baseline"))
    workflow.requirements.runtime = RuntimeRequirements(comfy_version="==0.25.0")
    bundle = load_bundle(workflow)
    adapter = CheckoutServerAdapter("http://127.0.0.1:8188", expected_comfyui_version="==0.25.0")
    profile = {"runtime": {"comfyui_version": "==0.26.0"}}
    adapter._host_profile = profile
    calls = []
    monkeypatch.setattr(vibecomfy, "_open_checkout_http", _opener(calls, version="0.25.0"))
    monkeypatch.setattr(adapter, "run_compiled_workflow", lambda *args, **kwargs: adapter._probe_system_stats())
    releases = []
    monkeypatch.setattr(adapter, "release", lambda **kwargs: releases.append(kwargs))
    with readiness.ComfyProvenance().capture() as facts, pytest.warns(UserWarning):
        with pytest.raises(ValueError, match="installed baseline requires ComfyUI ==0.26.0"):
            production_engine._run_profile_result(
                bundle, "checkout_server", profile,
                model_id="a04", template_id="a04", task_identity="a04-task", destination=tmp_path,
                endpoint_checkout=lambda **kwargs: nullcontext(adapter),
            )
    assert calls == ["http://127.0.0.1:8188/system_stats"]
    assert not adapter._system_stats_verified
    assert releases == [{"reason": "failed"}]
    assert facts.observed["engine"] == "0.25.0"
    assert facts.evidence()["comfy_provenance"]["readiness"]["status"] == "ready_with_warnings"


@pytest.mark.parametrize("profile", ["local_server", "pip_embedded"])
def test_canonical_requirements_are_retained_before_profile_execution_fails(tmp_path, monkeypatch, profile):
    from astrid.packs.vibecomfy import local_server_engine, production_engine

    def fail(*args, **kwargs):
        raise RuntimeError("profile execution failed")

    monkeypatch.setattr(local_server_engine, "run_local_server", fail)
    monkeypatch.setattr(production_engine, "_bootstrap_embedded_comfy_client", fail)
    with readiness.ComfyProvenance().capture() as facts:
        with pytest.raises(RuntimeError, match="profile execution failed"):
            production_engine._run_profile_result(
                _workflow(), profile, None, model_id="a04", template_id="a04",
                task_identity="a04-task", destination=tmp_path,
            )
    assert facts.recorded["engine"] == "==0.25.0"
    assert facts.models[0]["recorded_sha256"] == "a" * 64


@pytest.mark.parametrize("revision,digest", [(None, None), ("unknown", "sha256:" + "b" * 64),
                                           ("2" * 40, "sha256:" + "b" * 64),
                                           ("1" * 40, "sha256:" + "c" * 64)])
def test_unknown_or_changed_installed_source_is_a_blocker(revision, digest):
    with pytest.raises(readiness.ReadinessError, match="source identity"):
        readiness.verify_installed_source("1" * 40, "sha256:" + "b" * 64, revision, digest)


def test_selected_source_precedes_unimported_ambient_copy_and_keeps_product_origins(tmp_path, monkeypatch):
    selected, ambient = tmp_path / "selected", tmp_path / "ambient"
    for root in (selected, ambient):
        (root / "vibecomfy").mkdir(parents=True)
        (root / "vibecomfy/__init__.py").write_text("raise RuntimeError('do not import')\n")
    for name in tuple(sys.modules):
        if name == "vibecomfy" or name.startswith("vibecomfy."):
            monkeypatch.delitem(sys.modules, name)
    monkeypatch.setattr(sys, "path", [*sys.path, str(ambient)])
    with pytest.raises(readiness.ReadinessError, match="does not resolve to the verified checkout"):
        readiness.bind_vibecomfy_checkout(selected)
    assert readiness.select_unimported_vibecomfy_checkout(selected) == selected
    assert readiness.bind_vibecomfy_checkout(selected) == selected
    assert importlib.util.find_spec("vibecomfy").origin == str(selected / "vibecomfy/__init__.py")
    assert "vibecomfy" not in sys.modules


@pytest.mark.parametrize("binder", [readiness.bind_vibecomfy_checkout, readiness.select_unimported_vibecomfy_checkout])
def test_loaded_conflicting_source_still_blocks(tmp_path, monkeypatch, binder):
    selected = tmp_path / "selected"
    (selected / "vibecomfy").mkdir(parents=True)
    (selected / "vibecomfy/__init__.py").write_text("")
    monkeypatch.setitem(sys.modules, "vibecomfy", SimpleNamespace(__file__=str(tmp_path / "other/__init__.py")))
    with pytest.raises(readiness.ReadinessError, match="conflicting VibeComfy origin"):
        binder(selected)


def test_installed_recipe_models_are_observations_not_invented_authored_hashes():
    facts = readiness.ComfyProvenance()
    facts.workflow(_workflow())
    facts.installed({"installed_provenance": {"recipe": {"sha256": "r" * 64}}, "launch": {
        "model_root": {"inventory": [
            {"name": "model.safetensors", "subdir": "checkpoints", "sha256": "sha256:" + "a" * 64},
            {"name": "vae.safetensors", "subdir": "vae", "sha256": "sha256:" + "b" * 64},
        ]}}})
    evidence = facts.evidence()
    assert [row["identity_status"] for row in evidence["models"]] == ["verified", "observed"]
    assert evidence["models"][1]["recorded_sha256"] is None
    assert evidence["build"]["recipe"] == {"sha256": "r" * 64}


def test_native_ui_and_static_python_provenance_never_execute_source(tmp_path):
    source = tmp_path / "workflow.py"
    source.write_text('READY_REQUIREMENTS = {"runtime": {"comfy_version": "==0.25.0"}, '
                      '"models": [{"name": "vae.safetensors", "subdir": "vae"}]}\n'
                      'raise RuntimeError("must never execute")\n')
    facts = readiness.ComfyProvenance()
    facts.source(source)
    assert facts.recorded["engine"] == "==0.25.0"
    assert facts.models[0]["recorded_sha256"] is None
    source = tmp_path / "source.json"
    source.write_text(json.dumps({"nodes": [{"id": 1, "type": "SaveImage", "properties": {
        "cnr_id": "comfy-core", "ver": "0.25.0"}}]}))
    facts = readiness.ComfyProvenance()
    facts.source(source)
    assert facts.recorded["engine"] == "0.25.0"


@pytest.mark.parametrize("missing", ["node", "model"])
def test_native_missing_capability_or_model_still_blocks_with_version_drift(missing):
    from astrid.core.generation import entries, preflight
    from vibecomfy.workflow import VibeWorkflow, WorkflowSource

    workflow = VibeWorkflow("a04-missing", WorkflowSource("a04-missing"))
    workflow.add_node("VAELoader", _id="1", vae_name="required.safetensors")
    entry = {"id": "a04-missing", "needs": [{"class_type": "VAELoader", "field": "vae_name",
                                            "value": "required.safetensors", "role": "vae"}]}
    info = {} if missing == "node" else {"VAELoader": {"input": {
        "required": {"vae_name": [["another.safetensors"]]}}, "output": ["VAE"]}}
    client = SimpleNamespace(object_info=lambda: info)
    with readiness.ComfyProvenance().capture() as facts:
        facts.workflow(_workflow())
        facts.system_stats({"system": {"comfyui_version": "0.26.0"}})
        with pytest.raises(preflight.GenerationReadinessError) as raised:
            entries.target_schema(entry, workflow, client)
    assert raised.value.readiness["status"] == "blocked"
    assert raised.value.readiness["issues"][0]["code"] == "missing_dependency"
    assert facts.evidence()["comfy_provenance"]["readiness"]["status"] == "ready_with_warnings"


@pytest.mark.parametrize("error_type,status", [(RuntimeError, "failed"), (KeyboardInterrupt, "cancelled")])
def test_producer_failure_spool_retains_observations_without_inventing_stop_proof(tmp_path, monkeypatch, error_type, status):
    workflow = tmp_path / "workflow.py"
    workflow.write_bytes(b'READY_REQUIREMENTS = {"runtime": {"comfy_version": "==0.25.0"}}\r\n')
    source = b'{"workflow_id":"a04","nodes":[]}\r\n'
    workflow.with_name("source.json").write_bytes(source)
    calls = []
    error = error_type("producer stopped")

    def execute(*args, **kwargs):
        client = comfy_target.ComfyTargetClient(
            comfy_target.ComfyTarget("http://127.0.0.1:18888", "attached"), opener=_opener(calls))
        client.object_info()
        raise error

    descriptor = SimpleNamespace(create=lambda **kwargs: SimpleNamespace(workflow=execute),
                                 placement=SimpleNamespace(resolve=lambda placement: "local"))
    monkeypatch.setattr(registry, "resolve_workflow_descriptor", lambda *args, **kwargs: descriptor)
    run = importlib.import_module("astrid.packs.vibecomfy.actions.run.run")
    spool = tmp_path / "spool"
    with pytest.raises(error_type) as raised:
        run._run_and_settle(workflow, spool, task_identity="task-1", attempt_identity="attempt-1", registry=object())
    assert raised.value is error
    envelope = json.loads((spool / "manifest.json").read_bytes())["managed_generation_result"]
    assert envelope["outcomes"]["execution"]["status"] == status
    assert envelope["evidence"]["producer"]["versions"]["observed"]["engine"] == "0.26.0"
    assert envelope["evidence"]["producer"]["versions"]["recorded"]["engine"] == "==0.25.0"
    assert envelope["warnings"]
    assert envelope["inputs"]["source"]["sha256"] == hashlib.sha256(source).hexdigest()
    assert len(calls) == 2
    if status == "cancelled":
        assert envelope["evidence"]["transport"]["cancel"]["stop_observed"] is None


@pytest.mark.parametrize("status,stop_observed", [
    ("succeeded", None), ("failed", None), ("cancelled", None),
    ("cancelled", False), ("cancelled", True),
], ids=["succeeded", "failed", "cancel-unknown-stop", "cancel-unobserved-stop", "cancel-observed-stop"])
@pytest.mark.parametrize("observed", ["0.26.0", None], ids=["known-drift", "unknown-version"])
def test_real_runtime_retains_version_evidence_after_terminal_cleanup_and_restart(tmp_path, monkeypatch, status, stop_observed, observed):
    from astrid.core._shared.result_manifest import harvest_staged_outputs
    from astrid.core.execution.generic_host import GenericPackHost, RuntimeProtocolClient
    from runtime_protocol.service import RuntimeService
    from runtime_protocol.store import RealmStore
    from tests.core.execution.test_producer_evidence_transport_f06 import _record

    realm = tmp_path / "realm"
    RealmStore.initialize(realm).close()
    service = RuntimeService(realm)
    try:
        capability, digest = "a04.fixture", "sha256:" + "2" * 64
        project = service.create_project({"slug": "a04", "name": "A04"}, idempotency_key="project")
        service.register_capability({"capability_id": capability, "definition_digest": digest})
        service.register_executor({"executor_id": "a04-worker", "capabilities": [capability]}, idempotency_key="worker")
        created = service.create_task({"capability_id": capability, "capability_digest": digest,
                                       "project": project["id"], "spec": {}, "idempotency_key": "task"})
        attempt = service.claim_next({"executor_id": "a04-worker", "capability_ids": [capability],
                                      "runtime_epoch": service.health()["runtime_epoch"]}, idempotency_key="claim")
        task_id, attempt_id = created["task"]["id"], attempt["attempt_id"]
        workflow = tmp_path / "workflow.py"
        workflow.write_bytes(b'READY_REQUIREMENTS = {"runtime": {"comfy_version": "==0.25.0"}}\r\n')
        original = b'{"workflow_id":"a04","nodes":[]}\r\n'
        workflow.with_name("source.json").write_bytes(original)
        calls = []

        def execute(*args, **kwargs):
            client = comfy_target.ComfyTargetClient(
                comfy_target.ComfyTarget("http://127.0.0.1:18888", "attached"),
                opener=_opener(calls, version=observed))
            client.object_info()
            if status != "succeeded":
                raise RuntimeError("producer failed") if status == "failed" else KeyboardInterrupt()
            producer = tmp_path / "producer"
            producer.mkdir()
            payload = _managed_payload(producer)
            payload["evidence"]["producer"].update(
                build=None,
                versions=None if observed else {"recorded": None, "observed": None},
            )
            # F06 verifies producer source identity against submitted bytes.
            # This fixture must attest those bytes, not the helper's defaults.
            source = payload["inputs"]["source"]
            (producer / source["path"]).write_bytes(original)
            source.update(sha256=hashlib.sha256(original).hexdigest(), bytes=len(original),
                          filename="source.json")
            from PIL import Image

            media = producer / "outputs/final.png"
            Image.new("RGB", (1, 1), (20, 50, 80)).save(media)
            payload["outputs"][0].update(path="outputs/final.png", media_type="image/png",
                                          bytes=media.stat().st_size,
                                          sha256=hashlib.sha256(media.read_bytes()).hexdigest())
            payload.update(task_id=task_id, attempt_id=attempt_id)
            return _write_production(producer, payload)

        descriptor = SimpleNamespace(create=lambda **kwargs: SimpleNamespace(workflow=execute),
                                     placement=SimpleNamespace(resolve=lambda placement: "local"))
        monkeypatch.setattr(registry, "resolve_workflow_descriptor", lambda *args, **kwargs: descriptor)
        run = importlib.import_module("astrid.packs.vibecomfy.actions.run.run")
        spool = tmp_path / "spool"
        if status == "succeeded":
            run._run_and_settle(workflow, spool, task_identity=task_id, attempt_identity=attempt_id, registry=object())
        else:
            with pytest.raises(RuntimeError if status == "failed" else KeyboardInterrupt):
                run._run_and_settle(workflow, spool, task_identity=task_id, attempt_identity=attempt_id, registry=object())
        assert len(calls) == 2
        assert readiness.current_comfy_provenance() is None

        class Generated:
            timeout = 30
            calls = []

            def health(self):
                return service.health()

            def get_task(self, task_id):
                task = service.task(task_id)["task"]
                return {**task, "version": int(task["attempt"]) + 1}

            def settle_attempt(self, identity, body, *, idempotency_key):
                self.calls.append(("settle", identity, body, idempotency_key))
                return service.settle_attempt(identity, body, idempotency_key=idempotency_key)

            def fail_attempt(self, identity, *, idempotency_key, **body):
                self.calls.append(("fail", identity, body, idempotency_key))
                return service.fail_attempt(identity, body, idempotency_key=idempotency_key)

            def cancel_task(self, identity, *, idempotency_key, **body):
                self.calls.append(("cancel", identity, body, idempotency_key))
                return service.cancel_task_canonical(identity, body, idempotency_key=idempotency_key)

        protocol = RuntimeProtocolClient.__new__(RuntimeProtocolClient)
        protocol.generated = Generated()
        protocol._attempt_runtime_epochs = {attempt_id: attempt["runtime_epoch"]}
        host = GenericPackHost.__new__(GenericPackHost)
        host.client = protocol
        descriptors = harvest_staged_outputs(spool)
        host._validate_producer_evidence(descriptors, output_root=spool, task_id=task_id, attempt_id=attempt_id)
        typed = host._typed_outputs(_record(), descriptors, spool)
        outputs = host._upload_outputs(typed, project_id=project["id"], run_id=created["run"]["id"],
                                       task_id=task_id, attempt_id=attempt_id, lease_id=attempt["lease_id"],
                                       fence=attempt["fence"], runtime_epoch=attempt["runtime_epoch"])
        manifest = next(row for row in outputs if row["name"] == "result_manifest")
        result = {"manifest_ref": {"object_id": manifest["digest"], "size": manifest["size"]}}
        fence = {"attempt_id": attempt_id, "fence": attempt["fence"]}
        if status == "succeeded":
            protocol.settle(task_id, attempt["lease_id"], result=result, outputs=outputs, effect=None, **fence)
        elif status == "failed":
            protocol.fail(task_id, attempt["lease_id"], "producer failed", result=result, outputs=outputs, **fence)
        else:
            protocol.cancel(task_id, lease_id=attempt["lease_id"], result=result, outputs=outputs,
                            stop_observed=stop_observed, **fence)
        mutation, identity, body, key = protocol.generated.calls[0]
        expected_status = {"succeeded": "completed", "failed": "failed", "cancelled": "cancelled"}[status]
        if status == "cancelled":
            assert body["stop_observed"] is (stop_observed is True)
            assert key.endswith(f"-stop-{int(stop_observed is True)}")
            if not stop_observed:
                expected_status = "cancel_requested"
                pending = service.task(task_id)["task"]
                assert pending["status"] == expected_status
                assert pending["attempt_id"] == attempt_id
                assert pending["waiting_reason"] == "provider_state_unknown"
                active = service.store.conn.execute("SELECT * FROM attempts WHERE id=?", (attempt_id,)).fetchone()
                assert not active["settled"]
                assert active["lease_id"] == attempt["lease_id"]
                assert active["fence"] == attempt["fence"]
        shutil.rmtree(spool)
        if (tmp_path / "producer").exists():
            shutil.rmtree(tmp_path / "producer")
        workflow.unlink()
        workflow.with_name("source.json").unlink()
        service.close()
        service = RuntimeService(realm)
        operation = {"settle": service.settle_attempt, "fail": service.fail_attempt,
                     "cancel": service.cancel_task_canonical}[mutation]
        operation(identity, body, idempotency_key=key)
        stored = service.task(task_id)["task"]
        assert stored["status"] == expected_status
        assert stored["result"]["manifest_ref"] == result["manifest_ref"]
        if status == "cancelled":
            assert stored["result"]["cancel"] == {"requested": True, "stop_observed": stop_observed is True}
            if not stop_observed:
                assert stored["attempt_id"] == attempt_id
                assert stored["waiting_reason"] == "provider_state_unknown"
        envelope = json.loads(service.cas.path_for(manifest["digest"].removeprefix("sha256:")).read_bytes())
        producer = envelope["evidence"]["producer"]
        assert producer["versions"]["recorded"]["engine"] == "==0.25.0"
        assert producer["versions"]["observed"]["engine"] == observed
        assert producer["comfy_provenance"]["readiness"]["status"] == (
            "ready_with_warnings" if observed else "ready")
        assert all(issue["message"] in envelope["warnings"]
                   for issue in producer["comfy_provenance"]["readiness"]["issues"])
        if observed is None:
            comparison = next(row for row in producer["comfy_provenance"]["version_comparisons"]
                              if row["name"] == "engine")
            assert comparison == {"name": "engine", "recorded": "==0.25.0",
                                  "observed": None, "status": "unknown"}
        ref = envelope["inputs"]["source"]
        assert service.cas.path_for(ref["sha256"]).read_bytes() == original
        assert ref["sha256"] == hashlib.sha256(original).hexdigest()
        assert len(service.managed_outputs(task_id)) == len(outputs)
        if status == "cancelled":
            assert envelope["evidence"]["transport"]["cancel"]["stop_observed"] is None
    finally:
        service.close()
