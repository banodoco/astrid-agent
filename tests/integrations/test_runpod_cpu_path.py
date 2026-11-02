"""CPU RunPod journey: real ownership/composition, doubles only at HTTP I/O.

The provider's pod agent reports synthetic install/inference evidence. No
installer, provider, GPU, pod process or external socket is used by this test.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import shlex
import socket
import subprocess
import sys
import threading
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
import requests
from PIL import Image

from astrid.core.execution.generic_host import GenericPackHost, RuntimeProtocolClient
from astrid.core.generation import engine_recipe, engine_runner
from astrid.core.generation.delivery import deliver_generation
from astrid.packs.runpod import remote_placement, target_selection
from astrid.packs.runpod.cuda_versions import DEFAULT_ALLOWED_CUDA_VERSIONS
from astrid.sdk import AstridClient
from astrid.sdk.generation import GenerationFacade
from astrid.sdk.remote import RemoteAstridClient
from astrid.sdk.workspace_client import WorkspaceClient
from tests.integrations.test_runpod_host_endpoint_r5 import tuple_sources

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
    digest = _sha(manifest.read_bytes())
    environment = {key: os.environ[key] for key in ("HOME", "PATH", "TMPDIR", "LANG") if key in os.environ}
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    receipts = []
    def cli(*args):
        argv = [sys.executable, "-I", "-B", "-m", "astrid_runtime", *args,
                "--profile", "astrid", "--data-root", str(data), "--source-manifest", str(manifest),
                "--hosted-origin", "off", "--json"]
        result = subprocess.run(argv, env=environment, capture_output=True, text=True, timeout=90)
        payload = json.loads(result.stdout)
        receipts.append({"argv": argv, "exit": result.returncode, "result": payload})
        (root / "runtime-lifecycle.json").write_text(json.dumps(receipts, indent=2))
        assert result.returncode == 0, {"result": payload, "stderr": result.stderr}
        assert _sha(manifest.read_bytes()) == digest
        return payload
    def snapshot():
        support = data / "runtime"
        discovery = json.loads((support / "discovery.json").read_text())
        marker = json.loads((support / "instance.lock").read_text())
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
            "profile_sha256": _sha(profile_path.read_bytes()), "owner": marker, "discovery": discovery}})
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
        cli("up")
        running = True
        current = snapshot()
        assert current["runtime_instance_id"] != prior["runtime_instance_id"]
        assert (current["pid"], current["process_birth_id"]) != (prior["pid"], prior["process_birth_id"])
    finally:
        if running:
            cli("down")


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
    assert console.main(["generate", "image", "A red cube on a white tabletop.",
        "--model", selected["id"], "--target", "runpod", "--engine", "comfyui",
        "--width", "1024", "--height", "1024", "--steps", "8", "--seed", "0",
        "--out", str(output), "--consent", "paid", "--max-usd", "10",
        "--deadline", (datetime.now(timezone.utc) + timedelta(minutes=90)).isoformat(),
        "--preparation-seconds", "1800", "--execution-seconds", "900", "--idle-seconds", "1800",
        "--dry-run", "--json"]) == 0
    dry = json.loads(capsys.readouterr().out)
    assert dry["target"]["placement"] == "runpod" and dry["entry"]["model"] == selected["id"]
    assert not output.exists()
    (tmp_path / "generate-dry-run.json").write_text(json.dumps(dry, indent=2))


class ProviderHTTP:
    """RunPod GraphQL/REST and pod-agent HTTP contracts, with no host doubles."""

    pod_id = "cpupod"

    def __init__(self, source_root, claim_path):
        self.claim_path = claim_path
        self.events, self.commands, self.pods, self.jobs, self.files = [], [], [], {}, {}
        self.marker = f"{remote_placement.BOOTSTRAP_ROOT}/{remote_placement.source_digest(source_root)}/ok"
        self.schema = {
            "SaveImage": {"input": {"required": {"filename_prefix": ["STRING", {"default": "cpu"}]}},
                          "output": [], "output_node": True},
            **{name: {"input": {"required": {}}, "output": [], "output_node": False}
               for name in ("UNETLoader", "VAELoader")},
        }
        image = io.BytesIO()
        Image.new("RGB", (16, 16), (12, 34, 56)).save(image, format="PNG")
        self.image = image.getvalue()
        self.prompts = []
        self.receipt = self._install_receipt()

    def _install_receipt(self):
        from astrid.core.generation.engine_contracts import (
            DownloadResult, HealthEvidence, InstallReceipt, StepReceipt,
        )
        recipe = engine_recipe.load_recipe("comfyui", "linux-x86_64-cu130")
        root = "/workspace/astrid/engines/comfyui"
        plan = engine_runner.plan_install(recipe, root=root, placement="local")
        lock = engine_recipe.load_model_lock(recipe.models_lock)
        stamp = datetime.now(timezone.utc).isoformat()
        # Remote fixture evidence is synthesized from the selected recipe.
        # Production parses and compares both live status and downloaded receipt.
        receipt = InstallReceipt(
            "astrid.engine-install.v1", "cpu-http-install", plan.engine, plan.platform, plan.placement,
            root, "installed", False, plan.recipe_sha256, plan.models_lock_sha256,
            {env.name: (env.requirements.packaged_lock.sha256 if env.requirements.packaged_lock
                        else _sha(env.name.encode())) for env in recipe.environments},
            {source.name: source.pin.revision for source in recipe.sources},
            tuple(DownloadResult(model.name, root + "/models/" + model.destination, "downloaded",
                                 model.bytes, model.sha256, True, 0) for model in lock.models),
            tuple(StepReceipt(step.id, step.kind, "executed", "passed", {}) for step in plan.steps),
            HealthEvidence("healthy", stamp, _sha(json.dumps(self.schema).encode()), True), stamp, stamp, None,
        )
        engine_runner._parse_receipt(asdict(receipt), plan)
        return asdict(receipt)

    def graphql(self, body):
        query = body["query"]
        variables = body.get("variables", {})
        self.events.append(("graphql", query.split("{")[0], variables))
        if "GpuCatalogue" in query:
            return {"gpuTypes": [{"id": "gpu-exact", "displayName": "CPU fixture GPU"}]}
        if "DataCenterStock" in query:
            assert variables["availability"]["allowedCudaVersions"] == list(DEFAULT_ALLOWED_CUDA_VERSIONS)
            return {"dataCenters": [{"id": "DC-unpinned", "storageSupport": False,
                    "gpuAvailability": [{"gpuTypeId": "gpu-exact", "available": True, "stockStatus": "High"}]}]}
        if "DataCenterPrices" in query:
            assert variables["p0"]["dataCenterId"] == "DC-unpinned"
            return {"gpuTypes": [{"id": "gpu-exact", "dc0": {"stockStatus": "High", "uninterruptablePrice": 1.0}}]}
        if "query GpuTypes" in query:
            # The actual SDK validates the accepted exact ID before create.
            assert 'id: "gpu-exact"' in query
            return {"gpuTypes": [{"id": "gpu-exact", "displayName": "CPU fixture GPU"}]}
        if "podFindAndDeployOnDemand" in query:
            record = json.loads(self.claim_path.read_text())
            assert record["state"] == "allocation_pending"
            assert record["lease"]["state"] == "preparing"
            allowed = re.search(r"allowedCudaVersions: (\[[^\]]+\])", query)[1]
            assert json.loads(allowed) == list(DEFAULT_ALLOWED_CUDA_VERSIONS)
            assert "DC-unpinned" in query and "networkVolumeId" not in query
            assert record["worker_image"] in query and record["request_name"] in query
            assert 'gpuTypeId: "gpu-exact"' in query
            self.pods.append({"id": self.pod_id, "name": record["request_name"],
                "desiredStatus": "RUNNING", "actualStatus": "RUNNING", "costPerHr": 1.0,
                "imageName": record["worker_image"], "networkVolumeId": None,
                "dataCenterId": "DC-unpinned", "machineType": "CPU fixture GPU",
                "machine": {"gpuTypeId": "gpu-exact", "dataCenterId": "DC-unpinned"}})
            return {"podFindAndDeployOnDemand": self.pods[0]}
        if "myself" in query:
            return {"myself": {"pods": self.pods}}
        if "pod(" in query:
            return {"pod": self.pods[0] if self.pods else None}
        raise AssertionError("unexpected provider GraphQL operation")

    def command(self, command):
        self.commands.append(command)
        if command.startswith("test -f "):
            return (0 if shlex.split(command)[2] in self.files else 1), ""
        if "base64 -d" in command and '"$d/command.sh"' in command:
            directory = shlex.split(command.splitlines()[1])[0].removeprefix("d=")
            encoded = re.search(r"printf %s (\S+) \| base64 -d", command)[1]
            detached = base64.b64decode(encoded).decode()
            self.commands.append(detached)
            assert "pod_bootstrap.sh" in detached or "engines install comfyui --platform linux-x86_64-cu130" in detached
            if "pod_bootstrap.sh" in detached:
                self.files[self.marker] = b"ok"
            else:
                self.files["/workspace/astrid/engines/comfyui/.astrid-engine/install.json"] = json.dumps(self.receipt).encode()
            self.files.update({directory + "/status": b"exited", directory + "/exit": b"0",
                               directory + "/pid": b"123", directory + "/stdout": b"", directory + "/stderr": b""})
            return 0, ""
        if "engines status" in command:
            installed = "/workspace/astrid/engines/comfyui/.astrid-engine/install.json" in self.files
            return 0, json.dumps(self.receipt if installed else None)
        if command.startswith(("cat ", "tail ")):
            return 0, self.files[shlex.split(command)[-1]].decode()
        if command.startswith("sha256sum "):
            path = shlex.split(command)[1]
            return 0, _sha(self.files[path]) + "  " + path
        assert command.startswith("mkdir -p "), command
        return 0, ""

    def handle(self, request):
        host, path, method = request.url.host, request.url.path, request.method
        request.read()
        body = json.loads(request.content) if request.content and request.content[:1] == b"{" else {}
        self.events.append((method, host, path))
        def reply(data, status=200):
            encoded = json.dumps(data).encode()
            return httpx.Response(status, stream=httpx.ByteStream(encoded), request=request,
                headers={"Content-Type": "application/json", "Content-Length": str(len(encoded))})
        if host in {"api.runpod.io", "graphql.runpod.io"}:
            return reply({"data": self.graphql(body)})
        if host == "rest.runpod.io":
            assert path.startswith("/v1/pods"), "ephemeral route queried volumes"
            if method == "DELETE":
                assert path.endswith("/" + self.pod_id) and self.pods
                assert json.loads(self.claim_path.read_text())["release_authorized"] is True
                self.pods.clear()
                return reply({}, 204)
            assert method == "GET"
            if path == "/v1/pods":
                return reply(self.pods)
            return reply(self.pods[0] if self.pods else {}, 200 if self.pods else 404)
        if host == "astrid-egress-preflight-8000.proxy.runpod.net":
            assert method == "GET" and path == "/health"
            return reply({}, 404)
        assert host == self.pod_id + "-8000.proxy.runpod.net", host
        if path == "/health":
            return reply({"ok": True, "protocol": 1})
        if path == "/jobs" and method == "POST":
            code, stdout = self.command(body["command"])
            self.jobs[body["id"]] = (code, stdout.encode())
            return reply({"id": body["id"]})
        if path.startswith("/jobs/"):
            code, stdout = self.jobs[path.rsplit("/", 1)[1]]
            return reply({"status": "exited", "exit_code": code, "stdout": base64.b64encode(stdout).decode(),
                          "stderr": "", "stdout_offset": len(stdout), "stderr_offset": 0,
                          "stdout_eof": True, "stderr_eof": True})
        if path == "/uploads":
            upload_id = _sha(body["path"].encode())[:32]
            self.jobs[upload_id] = body
            self.files[body["path"]] = b""
            return reply({"id": upload_id, "offset": 0, "complete": False})
        if path.startswith("/uploads/"):
            meta = self.jobs[path.rsplit("/", 1)[1]]
            if method == "PUT":
                self.files[meta["path"]] += request.content
            data = self.files[meta["path"]]
            return reply({"offset": len(data), "complete": len(data) == meta["size"] and _sha(data) == meta["sha256"]})
        if path == "/download-chunk":
            if body["path"] not in self.files:
                return reply({}, 400)
            data = self.files[body["path"]]
            return reply({"data": base64.b64encode(data[body["offset"]:]).decode(), "offset": body["offset"],
                          "total": len(data), "sha256": _sha(data[body["offset"]:]), "etag": _sha(data)})
        if path == "/engine-proxy":
            binding = json.loads(self.claim_path.read_text())["lease"]["endpoint_binding"]
            assert binding["state"] == "binding" and binding["pod_id"] == self.pod_id
            assert "loopback_url" not in binding
            return reply({"ok": True})
        if "/engines/" in path:
            binding = json.loads(self.claim_path.read_text())["lease"]["endpoint_binding"]
            assert binding["state"] in {"bound", "ready"}
            assert binding["loopback_url"].startswith("http://127.0.0.1:")
            operation = path.split("/", 3)[-1]
            if operation == "object_info":
                return reply(self.schema)
            if operation == "system_stats":
                return reply({"system": {"os": "linux"}})
            if operation == "prompt":
                self.prompts.append(body)
                return reply({"prompt_id": "cpu-image", "number": 1, "node_errors": {}})
            if operation.startswith("history/"):
                sink = next(key for key, node in self.prompts[0]["prompt"].items() if node["class_type"] == "SaveImage")
                return reply({"cpu-image": {"outputs": {sink: {"images": [
                    {"filename": "cpu.png", "subfolder": "", "type": "output"}]}},
                    "status": {"status_str": "success", "completed": True, "messages": []}}})
            if operation == "view":
                return httpx.Response(200, stream=httpx.ByteStream(self.image), request=request,
                    headers={"Content-Type": "image/png", "Content-Length": str(len(self.image))})
            if operation == "queue":
                return reply({"queue_running": [], "queue_pending": []})
        raise AssertionError((method, host, path))


def test_cpu_runpod_http_journey(tmp_path, monkeypatch, real_runtime, tuple_sources):
    from vibecomfy.workflow import VibeWorkflow, WorkflowSource, VibeOutput
    from vibecomfy.workflow_bundle import emit_bundle

    monkeypatch.setenv("ASTRID_RUNTIME_DATA_ROOT", str(tmp_path / "machine"))
    monkeypatch.setenv("ASTRID_RUNPOD_SOURCE_ROOT", str(tuple_sources))
    monkeypatch.setenv("VIBECOMFY_ENTRIES_DIR", str(tmp_path / "entries"))
    preferences = {"provider_account_ref": "cpu-fixture", "gpu_types": ["CPU fixture GPU"],
        "image": "runpod/base:1.0.3-dev-fix-pytorch-version-verification-cuda1300-ubuntu2404"
                 "@sha256:24795acee5856a3e6d383e879ccdd53edd25d24ea38d0edab5a1d3718ea385e6",
        "volume": {"id": None, "name": None, "size_gb": 0, "mount_path": "/workspace"},
        "idle_timeout_seconds": 1800,
        "credential_ref": "env:FAKE_RUNPOD_CREDENTIAL", "budget_cap_usd": 2.0}
    target_selection.save_runpod_preferences(preferences)
    claim_path = target_selection.canonical_claim_path("cpu-fixture")
    provider = ProviderHTTP(tuple_sources, claim_path)

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
        response = provider_response(httpx.Request(request.method, request.url, content=request.body))
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
        credential_source={"FAKE_RUNPOD_CREDENTIAL": "synthetic-only"})
    host.discover()
    host.preflight()
    for name in ("runpod.setup", "runpod.ls", "runpod.status", "runpod.release", "vibecomfy.run"):
        assert host.capabilities[name].ready, host.capabilities[name].preflight
    host.register()
    worker = threading.Thread(target=host.run, kwargs={"poll_seconds": 0.02}, daemon=True)
    worker.start()
    def setup(raw):
        return client.invoke_result("runpod.setup", kind="action", project=None,
            inputs={"target_policy": json.dumps({"kind": "runpod", "engine": "comfyui", "preferences": raw})},
            wait=True, timeout_seconds=90, poll_seconds=0.02, read_managed_outputs=False)
    try:
        assert setup(preferences).ok
        retained = host._target_bindings.provider
        changed = {**preferences, "budget_cap_usd": 1.5}
        mismatch = client.invoke_result("runpod.status", kind="action", project=None,
            inputs={"target_policy": json.dumps({"kind": "runpod", "engine": "comfyui", "preferences": changed})},
            wait=True, timeout_seconds=90, poll_seconds=0.02, read_managed_outputs=False)
        assert not mismatch.ok and "runpod.setup" in str(mismatch.to_dict())
        assert setup(changed).ok
        assert host._target_bindings.provider is not retained and retained.lease_owner.closed
        assert not provider.events, "setup observed provider stock"
        quote = GenerationFacade().describe(target="runpod", client=client)
        assert quote["offer"]["datacenter_id"] == "DC-unpinned"
        assert target_selection.read_runpod_preferences().datacenter_id is None
        result = GenerationFacade().image(entry="cpu-image", target="runpod", client=client,
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
        assert generation.metadata["source_task_id"] == result.task_id and generation.status == "completed"
        variants, cursor = owner.list_variants(generation.generation_id)
        assert cursor is None and len(variants) == 1
        assert variants[0].object_id == "sha256:" + _sha(image_path.read_bytes())
        assert client.runs.show(run_id).ok
        record = json.loads(claim_path.read_text())
        assert record["lease"]["state"] == "idle" and record["paid_limits"]["idle_seconds"] == 1800
        snapshot = claim_path.read_bytes()
        refusal = setup({**changed, "budget_cap_usd": 1.25})
        assert not refusal.ok and claim_path.read_bytes() == snapshot and len(provider.pods) == 1
        release = GenerationFacade().release(engine="comfyui", target="runpod", client=client, wait=True)
        assert release.ok, release.to_dict()
        record = json.loads(claim_path.read_text())
        assert record["state"] == "cleanup_complete" and record["cleanup"]["absence"]["exact_get_status"] == 404
        assert not provider.pods and len(provider.prompts) == 1
        released_owner = host._target_bindings.provider
        observations = len(provider.events)
        assert setup({**changed, "budget_cap_usd": 1.25}).ok
        assert host._target_bindings.provider is not released_owner and released_owner.lease_owner.closed
        assert len(provider.events) == observations, "setup after verified release observed the provider"
        evidence = {"task_id": result.task_id, "run_id": run_id, "generation_id": generation.generation_id,
                    "output_object": variants[0].object_id, "delivered_file": str(image_path),
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
    _same_root_lifecycle(tmp_path, tmp_path / "realm", realm_id)
