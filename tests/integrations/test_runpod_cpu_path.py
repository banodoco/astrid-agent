"""CPU RunPod journey: real ownership/composition, doubles only at HTTP I/O.

The provider's pod agent reports synthetic install/inference evidence; the pod
runs the job as a remote machine (Plan 32 T5, the fake in
``test_remote_machine_cpu``). No installer, provider, GPU, pod process or
external socket is used by this test.
"""
from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
import requests

from astrid.core.execution.generic_host import GenericPackHost, RuntimeProtocolClient
from astrid.core.generation.delivery import deliver_generation
from astrid.packs.runpod import target_selection
from astrid.sdk import AstridClient
from astrid.sdk.generation import GenerationFacade
from astrid.sdk.remote import RemoteAstridClient
from astrid.sdk.workspace_client import WorkspaceClient
from tests.integrations.test_remote_machine_cpu import MachineProviderHTTP, tuple_pins
from tests.integrations.test_runpod_host_endpoint_r5 import tuple_sources  # noqa: F401  (fixture)

pytestmark = pytest.mark.integration


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _same_root_lifecycle(root, realm_root, realm_id):
    """Attach this journey's existing stopped realm; never create a workspace."""
    from astrid_runtime.runtime_boundary import LocalRuntimeBoundary

    data = root / "launcher"
    data.mkdir()
    source = Path(__file__).resolve().parents[2]
    import astrid_runtime
    runtime = Path(astrid_runtime.__file__).resolve().parents[1]
    manifest = data / "source-manifest.json"
    manifest.write_text(json.dumps({"profile": "astrid", "source_checkout": str(source),
        "runtime_checkout": str(runtime), "runtime_environment": sys.prefix}))
    lock = json.loads((source / "runtime.lock.json").read_text())
    runtime_sha = subprocess.check_output(["git", "-C", str(runtime), "rev-parse", "HEAD"], text=True).strip()
    assert runtime_sha == lock["source_commit"]
    digest = _sha(manifest.read_bytes())
    environment = {key: os.environ[key] for key in ("HOME", "PATH", "TMPDIR", "LANG") if key in os.environ}
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    receipts = []
    def cli(*args):
        # Bootstrap only the explicitly selected Runtime source. Agent selection
        # belongs to production CLI and the manifest, never this launcher's path.
        argv = [sys.executable, "-I", "-B", "-c",
                "import runpy,sys; sys.path.insert(0,sys.argv.pop(1)); "
                "runpy.run_module('astrid_runtime',run_name='__main__')", str(runtime), *args,
                "--profile", "astrid", "--data-root", str(data), "--source-manifest", str(manifest),
                "--hosted-origin", "off", "--json"]
        result = subprocess.run(argv, env=environment, capture_output=True, text=True, timeout=90)
        payload = json.loads(result.stdout)
        receipts.append({"argv": argv, "exit": result.returncode, "result": payload,
                         "runtime_sha": runtime_sha, "lock_runtime_sha": lock["source_commit"]})
        (root / "runtime-lifecycle.json").write_text(json.dumps(receipts, indent=2))
        assert result.returncode == 0, {"result": payload, "stderr": result.stderr}
        assert _sha(manifest.read_bytes()) == digest
        return payload
    hosts = []
    def snapshot():
        from astrid.sdk import host_bootstrap
        # Start the real selected owner's resident host at this launcher root.
        with pytest.MonkeyPatch.context() as selected:
            selected.setenv("ASTRID_RUNTIME_DATA_ROOT", str(data))
            selected.setenv("ASTRID_RUNTIME_SOURCE_MANIFEST", str(manifest))
            selected.setenv("ASTRID_PACK_HOST_PYTHON", sys.executable)
            assert AstridClient.open_from_launcher().projects.list().ok
        support = data / "runtime"
        host = json.loads((support / "generic-host.json").read_text())
        assert host["source_checkout"] == str(source)
        assert host_bootstrap._registered_host_status(host) == "ours"
        hosts.append(host)
        discovery = json.loads((support / "discovery.json").read_text())
        marker = json.loads((support / "instance.lock").read_text())
        assert host["runtime_instance_id"] == marker["runtime_instance_id"]
        assert marker["realm_id"] == discovery["active_realm"] == realm_id
        assert marker["realm_root"] == discovery["realm_root"] == str(realm_root)
        for name in ("pid", "process_birth_id", "runtime_instance_id"):
            assert marker[name] == discovery[name]
        assert LocalRuntimeBoundary.process_birth_identity(marker["pid"]) == marker["process_birth_id"]
        profile_path = support / "source-profiles" / "astrid.json"
        profile = json.loads(profile_path.read_text())
        for name, value in json.loads(manifest.read_text()).items():
            assert profile[name] == value
        health = LocalRuntimeBoundary._http_health_payload(discovery["endpoint"])
        assert health["status"] == "ok" and health["runtime_instance_id"] == marker["runtime_instance_id"]
        receipts.append({"snapshot": {"data_root": str(data), "manifest_sha256": digest,
            "profile_sha256": _sha(profile_path.read_bytes()), "owner": marker, "discovery": discovery,
            "host": host, "host_kernel_birth_id": LocalRuntimeBoundary.process_birth_identity(host["pid"])}})
        (root / "runtime-lifecycle.json").write_text(json.dumps(receipts, indent=2))
        return marker
    cli("workspace", "attach", "--realm-root", str(realm_root), "--realm-id", realm_id,
        "--process-control", "sandbox")
    running = False
    try:
        cli("up")
        running = True
        prior = snapshot()
        assert cli("down")["status"] == "stopped"
        running = False
        assert LocalRuntimeBoundary.process_birth_identity(prior["pid"]) != prior["process_birth_id"]
        assert not (data / "runtime" / "discovery.json").exists()
        from astrid.sdk import host_bootstrap
        assert host_bootstrap._host_birth_identity(hosts[-1]["pid"]) != hosts[-1]["process_birth_id"]
        assert not (data / "runtime" / "generic-host.json").exists()
        receipts.append({"after_down": {"host_pid": hosts[-1]["pid"],
            "host_after_birth": host_bootstrap._host_birth_identity(hosts[-1]["pid"]),
            "runtime_after_birth": LocalRuntimeBoundary.process_birth_identity(prior["pid"]),
            "registration_exists": False, "discovery_exists": False}})
        (root / "runtime-lifecycle.json").write_text(json.dumps(receipts, indent=2))
        cli("up")
        running = True
        current = snapshot()
        assert current["runtime_instance_id"] != prior["runtime_instance_id"]
        assert (current["pid"], current["process_birth_id"]) != (prior["pid"], prior["process_birth_id"])
    finally:
        if running:
            final_owner = json.loads((data / "runtime" / "instance.lock").read_text())
            cli("down")
            from astrid.sdk import host_bootstrap
            assert host_bootstrap._host_birth_identity(hosts[-1]["pid"]) != hosts[-1]["process_birth_id"]
            assert LocalRuntimeBoundary.process_birth_identity(final_owner["pid"]) != final_owner["process_birth_id"]
            assert not (data / "runtime" / "generic-host.json").exists()
            receipts.append({"final_down": {"host": hosts[-1], "host_after_birth":
                host_bootstrap._host_birth_identity(hosts[-1]["pid"]), "runtime_after_birth":
                LocalRuntimeBoundary.process_birth_identity(final_owner["pid"])}})
            (root / "runtime-lifecycle.json").write_text(json.dumps(receipts, indent=2))
    return {"source_checkout": str(source), "runtime_checkout": str(runtime),
            "runtime_sha": runtime_sha, "lock_runtime_sha": lock["source_commit"],
            "manifest_sha256": digest}


def test_cpu_runpod_public_catalog_and_dry_run(tmp_path, monkeypatch, capsys):
    from astrid import console

    connect = socket.socket.connect
    def local_only(sock, address):
        assert not isinstance(address, tuple) or address[0] in {"127.0.0.1", "localhost", "::1"}, address
        return connect(sock, address)
    monkeypatch.setattr(socket.socket, "connect", local_only)
    assert console.main(["generate", "list", "--json"]) == 0
    inventory = json.loads(capsys.readouterr().out)
    selected = next(row for row in inventory if row["id"] == "z-image" and row["backend"] == "local"
                    and row["mode"] == "t2i")
    assert selected["template"] == "image/z_image_turbo"
    (tmp_path / "generate-list.json").write_text(json.dumps(inventory, indent=2))
    output = tmp_path / "dry.png"
    # cause 889312e1f; ruling: machines 11:45 Sat
    # A dry run plans without renting; paid consent and limits belong to machines claim.
    argv = ["generate", "image", "A red cube on a white tabletop.",
        "--model", selected["id"], "--placement", "runpod", "--engine", "comfyui",
        "--width", "1024", "--height", "1024", "--steps", "8", "--seed", "0",
        "--out", str(output), "--dry-run", "--json"]
    assert console.main(argv) == 0
    refused = json.loads(capsys.readouterr().out)
    assert "z_image_turbo" in json.dumps(refused) or "z-image" in json.dumps(refused)
    assert not output.exists()
    (tmp_path / "generate-dry-run.json").write_text(json.dumps(refused, indent=2))
    for paid_option in (
        ["--consent", "paid"], ["--max-usd", "10"],
        ["--deadline", (datetime.now(timezone.utc) + timedelta(minutes=90)).isoformat()],
        ["--preparation-seconds", "1800"], ["--execution-seconds", "900"],
        ["--idle-seconds", "1800"],
    ):
        assert console.main([*argv, *paid_option]) == 2, paid_option
        rejection = json.loads(capsys.readouterr().out)
        assert rejection["issues"][0]["code"] == "input_rejected", rejection
        assert "astrid machines claim" in rejection["issues"][0]["message"], rejection
        assert not output.exists()


def test_cpu_runpod_http_journey(tmp_path, monkeypatch, real_runtime, tuple_sources):
    from vibecomfy.workflow import VibeWorkflow, WorkflowSource, VibeOutput
    from vibecomfy.workflow_bundle import emit_bundle

    monkeypatch.setenv("ASTRID_RUNTIME_DATA_ROOT", str(tmp_path / "machine"))
    monkeypatch.setattr('astrid.packs.runpod.managed_sources.ensure_managed_sources', lambda: Path(tuple_sources))
    monkeypatch.setattr('astrid.packs.runpod.managed_sources._pins', lambda: tuple_pins(tuple_sources))
    monkeypatch.setenv("VIBECOMFY_ENTRIES_DIR", str(tmp_path / "entries"))
    preferences = {"provider_account_ref": "cpu-fixture", "gpu_types": ["CPU fixture GPU"],
        "image": "runpod/base:1.0.3-dev-fix-pytorch-version-verification-cuda1300-ubuntu2404"
                 "@sha256:24795acee5856a3e6d383e879ccdd53edd25d24ea38d0edab5a1d3718ea385e6",
        "volume": {"id": None, "name": None, "size_gb": 0, "mount_path": "/workspace"},
        "idle_timeout_seconds": 1800,
        "credential_ref": "env:RUNPOD_RUNPOD_FAKE_CREDENTIAL", "budget_cap_usd": 2.0}
    target_selection.save_runpod_preferences(preferences)
    claim_path = target_selection.canonical_claim_path("cpu-fixture")
    monkeypatch.setenv("ASTRID_MACHINE_POLL_SECONDS", "0.05")
    provider = MachineProviderHTTP(claim_path, tmp_path / "attempts")

    # These are the only substitutions: provider HTTP, plus a fail-closed
    # socket guard. Runtime, host, admission, adapter, lease and transport run.
    send = httpx.Client.send
    send_requests = requests.Session.send
    def provider_response(request):
        try:
            return provider.handle(request)
        except Exception as error:
            (tmp_path / "provider-http-failure.json").write_text(json.dumps({
                "type": type(error).__name__, "message": str(error)[:1000]}))
            raise
    def http_send(client, request, **kwargs):
        if request.url.host in {"127.0.0.1", "localhost", "::1"}:
            return send(client, request, **kwargs)
        response = provider_response(request)
        if not kwargs.get("stream"):
            response.read()
        return response
    def requests_send(session, request, **kwargs):
        if httpx.URL(request.url).host in {"127.0.0.1", "localhost", "::1"}:
            return send_requests(session, request, **kwargs)
        response = provider_response(httpx.Request(request.method, request.url, headers=request.headers, content=request.body))
        result = requests.Response()
        result.status_code, result._content = response.status_code, response.read()
        return result
    connect = socket.socket.connect
    def local_connect(sock, address):
        assert not isinstance(address, tuple) or address[0] in {"127.0.0.1", "localhost", "::1"}
        return connect(sock, address)
    monkeypatch.setattr(httpx.Client, "send", http_send)
    monkeypatch.setattr(requests.Session, "send", requests_send)
    monkeypatch.setattr(socket.socket, "connect", local_connect)

    folder = tmp_path / "entries" / "cpu-image"
    folder.mkdir(parents=True)
    workflow = VibeWorkflow("cpu-image", WorkflowSource("cpu-image"))
    workflow.add_node("SaveImage", _id="1", uid="sink", filename_prefix="cpu")
    workflow.register_input("filename_prefix", "1", "filename_prefix", "cpu", type="STRING")
    workflow.outputs = [VibeOutput("1", "SaveImage", name="image", artifact_kind="image", mime_type="image/png")]
    (folder / "source.json").write_text(json.dumps({"nodes": [
        {"id": 1, "type": "SaveImage", "inputs": [], "widgets_values": ["cpu"]}], "links": []}))
    emit_bundle(workflow, folder / "workflow.py", {"operation": "authored"})
    (folder / "entry.json").write_text(json.dumps({"id": "cpu-image", "kind": "image", "modes": ["t2i"],
        "bundle": "workflow.py", "needs": [], "binds": {}, "defaults": {}}))

    daemon = real_runtime()
    realm_id = daemon.service.realm["id"]
    owner = WorkspaceClient(daemon.endpoint, daemon.token)
    client = AstridClient(remote=RemoteAstridClient(owner))
    project = client.projects.create(name="CPU RunPod", slug="cpu-runpod", idempotency_key="project")
    assert project.ok, project.error
    project_id = project.data["project_id"]
    owner.select_project(project_id, idempotency_key="select")
    packs = Path(__file__).resolve().parents[2] / "astrid" / "packs"
    host = GenericPackHost(pack_roots=[packs / "vibecomfy", packs / "runpod"],
        client=RuntimeProtocolClient(daemon.endpoint, daemon.worker_token), attempt_base=tmp_path / "attempts",
        credential_source={"RUNPOD_RUNPOD_FAKE_CREDENTIAL": "synthetic-only"})
    host.discover()
    host.preflight()
    for name in ("runpod.setup", "runpod.ls", "runpod.status", "runpod.release", "vibecomfy.run"):
        assert host.capabilities[name].ready, host.capabilities[name].preflight
    host.register()
    worker = threading.Thread(target=host.run, kwargs={"poll_seconds": 0.02}, daemon=True)
    worker.start()
    def setup(raw):
        return client.invoke_result("runpod.setup", kind="action", project=None,
            inputs={"placement_policy": json.dumps({"kind": "runpod", "engine": "comfyui", "preferences": raw})},
            wait=True, timeout_seconds=90, poll_seconds=0.02, read_managed_outputs=False)
    try:
        assert setup(preferences).ok
        retained = host._target_bindings.provider
        changed = {**preferences, "budget_cap_usd": 1.5}
        mismatch = client.invoke_result("runpod.status", kind="action", project=None,
            inputs={"placement_policy": json.dumps({"kind": "runpod", "engine": "comfyui", "preferences": changed})},
            wait=True, timeout_seconds=90, poll_seconds=0.02, read_managed_outputs=False)
        assert not mismatch.ok and "runpod.setup" in str(mismatch.to_dict())
        assert setup(changed).ok
        assert host._target_bindings.provider is not retained and retained.lease_owner.closed
        assert not provider.events, "setup observed provider stock"
        quote = GenerationFacade().describe(placement="runpod", client=client)
        assert quote["offer"]["datacenter_id"] == "DC-unpinned"
        assert target_selection.read_runpod_preferences().datacenter_id is None
        result = GenerationFacade().image(entry="cpu-image", placement="runpod", client=client,
            project=project_id, consent=("paid",), max_usd=1.5,
            deadline=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            offer_digest=quote["offer_digest"], preparation_seconds=120, execution_seconds=60,
            idle_seconds=1800, wait=True)
        assert result.ok, result.to_dict()
        delivered = deliver_generation(result, client=client, out=tmp_path / "delivered", project=project_id)
        image_path = Path(delivered["image_paths"][0])
        assert image_path.read_bytes() == provider.image
        task = client.tasks.show(result.task_id)
        assert task.ok and task.data["state"] == "succeeded"
        run_id = task.data["run_id"]
        assert run_id
        generations, cursor = owner.list_generations(project_id)
        assert cursor is None and len(generations) == 1
        generation = generations[0]
        assert generation["source_task_id"] == result.task_id and generation["status"] == "completed"
        variants, cursor = owner.list_variants(generation["generation_id"])
        assert cursor is None and len(variants) == 1
        assert variants[0]["object_id"] == "sha256:" + _sha(image_path.read_bytes())
        assert client.runs.show(run_id).ok
        record = json.loads(claim_path.read_text())
        assert record["lease"]["state"] == "idle" and record["paid_limits"]["idle_seconds"] == 1800
        snapshot = claim_path.read_bytes()
        refusal = setup({**changed, "budget_cap_usd": 1.25})
        assert not refusal.ok and claim_path.read_bytes() == snapshot and len(provider.pods) == 1
        release = GenerationFacade().release(engine="comfyui", placement="runpod", client=client, wait=True)
        assert release.ok, release.to_dict()
        record = json.loads(claim_path.read_text())
        assert record["state"] == "cleanup_complete" and record["cleanup"]["absence"]["exact_get_status"] == 404
        assert not provider.pods and len(provider.kinds(" generate cpu-image ")) == 1
        assert not any("/engines/" in str(event) for event in provider.events)
        released_owner = host._target_bindings.provider
        observations = len(provider.events)
        assert setup({**changed, "budget_cap_usd": 1.25}).ok
        assert host._target_bindings.provider is not released_owner and released_owner.lease_owner.closed
        assert len(provider.events) == observations, "setup after verified release observed the provider"
        evidence = {"task_id": result.task_id, "run_id": run_id, "generation_id": generation["generation_id"],
                    "output_object": variants[0]["object_id"], "delivered_file": str(image_path),
                    "sha256": _sha(image_path.read_bytes()), "synthetic_inference": True,
                    "release": record["cleanup"], "receipt": result.to_dict()}
        (tmp_path / "cpu-receipt.json").write_text(json.dumps(evidence, default=str, indent=2))
    finally:
        (tmp_path / "provider-http-events.json").write_text(json.dumps(provider.events, indent=2))
        host._shutdown.set()
        worker.join(timeout=35)
        assert not worker.is_alive()
        host.shutdown()
        daemon.stop()
    evidence["selected_source"] = _same_root_lifecycle(tmp_path, tmp_path / "realm", realm_id)
    evidence["selected_source"]["agent_sha"] = subprocess.check_output(
        ["git", "-C", str(Path(__file__).resolve().parents[2]), "rev-parse", "HEAD"], text=True).strip()
    (tmp_path / "cpu-receipt.json").write_text(json.dumps(evidence, default=str, indent=2))
