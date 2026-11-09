"""CPU RunPod-as-a-machine journeys: real Runtime, host, lease owner and HTTPS transport.

Plan 32 T5. Only provider HTTP is doubled: RunPod GraphQL/REST and the pod agent.
The pod-side generation command is answered by the fake (``T4 STUB`` in
``astrid.packs.runpod.machine.generation_argv``): it writes the job's output tree,
``result.json``, a real ``wrapper-manifest.json`` with its sha256 line, and
``progress.json`` once per poll. No ``/engines/`` request is ever made.

Journeys: a cold run then a warm second run; a custody change that refuses
reuse; cancel mid-poll; a bad mount mapping that fails before install.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import re
import shlex
import socket
import subprocess
import threading
import time
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
from tests.integrations.test_runpod_host_endpoint_r5 import tuple_sources  # noqa: F401  (fixture)

pytestmark = pytest.mark.integration

POD = "cpupod"
VOLUME = {"id": "volcpu1", "name": "cpu-models", "size_gb": 100, "mount_path": "/workspace"}
DC = "DC-unpinned"
#: Pod polls before the fake generation job exits (each poll writes one phase).
PHASES = ({"phase": "align"}, {"phase": "submit"}, {"phase": "executing", "current": 1, "total": 1},
          {"phase": "fetch"})


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def tuple_pins(source_root):
    """The (repository, commit) pins of the shared source tuple; no installed release manifest."""
    return {directory: ("https://example.invalid/" + Path(directory).name, subprocess.check_output(
        ["git", "-C", str(Path(source_root) / directory), "rev-parse", "HEAD"], text=True).strip())
        for directory in remote_placement.SOURCE_DIRS}


class MachineProviderHTTP:
    """RunPod GraphQL/REST and pod-agent contracts for the machine route."""

    def __init__(self, claim_path, attempts, *, volume=None):
        self.claim_path, self.attempts, self.volume = claim_path, attempts, volume
        self.events, self.commands, self.pods, self.files = [], [], [], {}
        self.agent_jobs, self.detached = {}, {}
        self.deploys = 0
        self.pod_id = POD  # the current (latest) pod; each deploy gets a new id
        self.killed = []
        self.mount_id, self.mount_path = 7, (volume or {}).get("mount_path")
        self.expect_entry = "cpu-image"  # None: any entry, or a shipped workflow
        self.generations = []  # job.json of every generation job the pod ran
        self.seen_controller_progress = []
        self.job_hold = threading.Event()
        self.job_hold.set()
        image = io.BytesIO()
        Image.new("RGB", (16, 16), (12, 34, 56)).save(image, format="PNG")
        self.image = image.getvalue()
        self.receipts = {}
        self.receipt = self._receipt_for("comfyui")

    # -- evidence ------------------------------------------------------------

    def _receipt_for(self, engine):
        """The pod's install receipt for a RunPod engine, from that engine's own recipe and lock."""
        if engine not in self.receipts:
            self.receipts[engine] = self._install_receipt(engine)
        return self.receipts[engine]

    def _install_receipt(self, engine):
        from dataclasses import asdict
        from astrid.core.generation.engine_contracts import (
            DownloadResult, HealthEvidence, InstallReceipt, StepReceipt,
        )
        recipe = engine_recipe.load_recipe(engine, "linux-x86_64-cu130")
        root = f"/root/astrid/engines/{engine}"
        plan = engine_runner.plan_install(recipe, root=root, placement="local")
        lock = engine_recipe.load_model_lock(recipe.models_lock)
        stamp = datetime.now(timezone.utc).isoformat()
        receipt = InstallReceipt(
            "astrid.engine-install.v1", f"cpu-http-install-{engine}", plan.engine, plan.platform, plan.placement,
            root, "installed", False, plan.recipe_sha256, plan.models_lock_sha256,
            {env.name: (env.requirements.packaged_lock.sha256 if env.requirements.packaged_lock
                        else _sha(env.name.encode())) for env in recipe.environments},
            {source.name: source.pin.revision for source in recipe.sources},
            tuple(DownloadResult(model.name, root + "/models/" + model.destination, "downloaded",
                                 model.bytes, model.sha256, True, 0) for model in lock.models),
            tuple(StepReceipt(step.id, step.kind, "executed", "passed", {}) for step in plan.steps),
            HealthEvidence("healthy", stamp, _sha(b"{}"), True), stamp, stamp, None,
        )
        engine_runner._parse_receipt(asdict(receipt), plan)
        return asdict(receipt)

    def kinds(self, *names):
        return [c for c in self.commands if any(name in c for name in names)]

    # -- provider ------------------------------------------------------------

    def graphql(self, body):
        query = body["query"]
        variables = body.get("variables", {})
        self.events.append(("graphql", query.split("{")[0].strip(), variables))
        if "GpuCatalogue" in query:
            return {"gpuTypes": [{"id": "gpu-exact", "displayName": "CPU fixture GPU"}]}
        if "DataCenterStock" in query:
            assert variables["availability"]["allowedCudaVersions"] == list(DEFAULT_ALLOWED_CUDA_VERSIONS)
            return {"dataCenters": [{"id": DC, "storageSupport": self.volume is not None,
                    "gpuAvailability": [{"gpuTypeId": "gpu-exact", "available": True, "stockStatus": "High"}]}]}
        if "DataCenterPrices" in query:
            return {"gpuTypes": [{"id": "gpu-exact", "dc0": {"stockStatus": "High", "uninterruptablePrice": 1.0}}]}
        if "gpuTypes(input:" in query:
            requested = re.search(r'id: "([^"]+)"', query)[1]
            assert requested in {"gpu-exact", "CPU fixture GPU"}
            return {"gpuTypes": [{"id": requested, "displayName": "CPU fixture GPU"}]}
        if "query GpuTypes" in query:
            assert 'id: "gpu-exact"' in query
            return {"gpuTypes": [{"id": "gpu-exact", "displayName": "CPU fixture GPU"}]}
        if "podFindAndDeployOnDemand" in query:
            record = json.loads(self.claim_path.read_text())
            assert record["state"] == "allocation_pending" and record["lease"]["state"] == "preparing"
            assert json.loads(re.search(r"allowedCudaVersions: (\[[^\]]+\])", query)[1]) == list(
                DEFAULT_ALLOWED_CUDA_VERSIONS)
            if self.volume is None:
                assert "networkVolumeId" not in query
            else:
                assert f'networkVolumeId: "{self.volume["id"]}"' in query
            assert 'gpuTypeId: "gpu-exact"' in query and record["request_name"] in query
            assert not self.pods, "a second pod was deployed while one exists"
            self.deploys += 1
            self.pod_id = POD if self.deploys == 1 else f"{POD}{self.deploys}"
            self.pods.append({"id": self.pod_id, "name": record["request_name"],
                "desiredStatus": "RUNNING", "actualStatus": "RUNNING", "costPerHr": 1.0, "gpuCount": 1,
                "imageName": record["worker_image"],
                "networkVolumeId": self.volume["id"] if self.volume else None,
                **({"volumeMountPath": self.volume["mount_path"]} if self.volume else {}),
                "dataCenterId": DC, "machineType": "CPU fixture GPU",
                "machine": {"gpuTypeId": "gpu-exact", "gpuDisplayName": "CPU fixture GPU", "dataCenterId": DC}})
            return {"podFindAndDeployOnDemand": self.pods[0]}
        if "podMounts" in query:
            # Typed mounts, as runpod_lifecycle.discovery.observe_pod_mounts reads them (f2948817).
            assert f'podId: "{self.pod_id}"' in query
            mounts = [] if self.volume is None else [{"volumeId": self.volume["id"], "volumeType": "NETWORK_VOLUME",
                                                      "mountPath": self.mount_path, "readOnly": False}]
            return {"pod": {"id": self.pod_id, "volumeMounts": mounts}}
        if "networkVolumes" in query:
            return {"myself": {"networkVolumes": self._volumes()}}
        if "myself" in query:
            return {"myself": {"pods": self.pods}}
        if "pod(" in query:
            return {"pod": self.pods[0] if self.pods else None}
        raise AssertionError("unexpected provider GraphQL operation")

    def _volumes(self):
        assert self.volume is not None, "ephemeral route queried volumes"
        return [{"id": self.volume["id"], "name": self.volume["name"], "size": self.volume["size_gb"],
                 "dataCenterId": DC}]

    # -- pod agent commands --------------------------------------------------

    def command(self, command):
        self.commands.append(command)
        if command.startswith("test -f "):
            return (0 if shlex.split(command)[2] in self.files else 1), ""
        if "base64 -d" in command and '"$d/command.sh"' in command:
            directory = shlex.split(command.splitlines()[1])[0].removeprefix("d=")
            detached = base64.b64decode(re.search(r"printf %s (\S+) \| base64 -d", command)[1]).decode()
            self.commands.append(detached)
            self.files.update({directory + "/pid": b"123", directory + "/stderr": b""})
            if "pod_bootstrap.sh" in detached:
                self.files[self._marker(detached)] = b"ok"
                self._exit(directory, 0, b"")
            elif re.search(r"engines install (\S+) --platform linux-x86_64-cu130", detached):
                engine = re.search(r"engines install (\S+) --platform", detached)[1]
                if self.volume is not None:
                    assert f"--model-cache-root /workspace/astrid/engines/{engine}/models" in detached
                self.files[f"/root/astrid/engines/{engine}/.astrid-engine/install.json"] = json.dumps(
                    self._receipt_for(engine)).encode()
                self._exit(directory, 0, b"")
            else:
                self._start_generation(directory, detached)
            return 0, ""
        if ': > "$d/cancel"' in command:
            # detached_cancel: signal the recorded group; a finished job keeps its exit.
            directory = shlex.split(command.splitlines()[0])[0].removeprefix("d=")
            job = self.detached[directory]
            if directory + "/exit" not in self.files:
                job["state"] = "cancelled"
                self.files[directory + "/status"] = b"cancelled"
            self.events.append(("detached_cancel", directory))
            return 0, ""
        if "engines status" in command:
            engine = re.search(r"engines status (\S+)", command)[1]
            installed = f"/root/astrid/engines/{engine}/.astrid-engine/install.json" in self.files
            return 0, json.dumps(self._receipt_for(engine) if installed else None)
        if command.startswith("python3 -c ") and "ASTRID_STORAGE_DIAGNOSTIC" in command:
            return self._storage_probe(command)
        if "generate list --json" in command:
            # The runtime check: ensure_runtime picks its own port; nothing pins one.
            assert "ensure_runtime" in command
            assert "17340" not in command and "--port" not in command and "8188" not in command
            assert any(path.endswith("/.astrid-engine/install.json") for path in self.files), "checked before install"
            listed = [self.expect_entry] if self.expect_entry else list(self.listed)
            return 0, json.dumps([{"id": entry, "backend": "local", "ready": True} for entry in listed])
        if command.startswith(("cat ", "tail ")):
            return 0, self.files.get(shlex.split(command)[-1], b"").decode()
        if command.startswith("sha256sum "):
            path = shlex.split(command)[1]
            return 0, _sha(self.files[path]) + "  " + path
        assert command.startswith("mkdir -p "), command
        return 0, ""

    def _marker(self, detached):
        work = re.search(r"cd (\S+);", detached)[1].strip("'")
        digest = work.rsplit("upload-", 1)[1]
        return f"{remote_placement.BOOTSTRAP_ROOT}/{digest}/ok"

    def _exit(self, directory, code, stdout):
        self.files.update({directory + "/status": b"exited", directory + "/exit": str(code).encode(),
                           directory + "/stdout": stdout})

    def _storage_probe(self, command):
        request = json.loads(base64.b64decode(shlex.split(command)[-1]))
        mapping = request.pop("provider_mapping")
        assert mapping["schema"] == "runpod.provider-mounts.v1" and mapping["pod_id"] == self.pod_id
        if not [e for e in self.events if e[0] == "storage_probe"]:
            # The first probe of a pod precedes every install write (a warm reuse re-probes later).
            assert not any(path.endswith("/.astrid-engine/install.json") for path in self.files), "probe after install"
        self.events.append(("storage_probe", request["mount"]))
        return 0, json.dumps({**request, "cache_device": 64768, "cache_mount_id": self.mount_id,
                              "free_bytes": 10**12, "total_bytes": 2 * 10**12, "local_free_bytes": 10**12})

    def _start_generation(self, directory, detached):
        # T4 STUB (pod side): `astrid generate <entry> --placement local` is answered here.
        argv = shlex.split(next(line for line in detached.splitlines() if " generate " in line).split(" > ")[0])
        assert argv[1] == "generate" and argv[3:5] == ["--placement", "local"], argv
        assert self.expect_entry is None or argv[2] == self.expect_entry, argv
        assert "17340" not in detached and "--port" not in detached
        root = argv[argv.index("--job") + 1].rsplit("/", 1)[0]
        job = json.loads(self.files[root + "/job.json"])
        assert argv[2] == (job["entry"] or "workflow") and set(job["inputs"]) >= {"python", "companion", "source"}
        for name in ("python", "companion", "source", "managed_assets", "source_video"):
            if name in job["inputs"]:
                # Input files ship inside the job directory, digest-checked after upload.
                assert job["inputs"][name].startswith(root + "/in/") and job["inputs"][name] in self.files
        self.generations.append(job)
        self.detached[directory] = {"state": "running", "root": root, "polls": 0, "job": job}
        self.files[directory + "/status"] = b"running"

    def _advance(self, directory):
        """One detached poll of the generation job: write its next phase, exit after the last."""
        job = self.detached[directory]
        if job["state"] != "running":
            return
        if not self.job_hold.is_set():
            return  # held running (cancel journey)
        root, index = job["root"], job["polls"]
        job["polls"] += 1
        self.files[root + "/progress.json"] = json.dumps(PHASES[min(index, len(PHASES) - 1)]).encode()
        if index == 2:
            # A phase written at an earlier poll is already in the controller's attempt file.
            for path in Path(self.attempts).rglob(".astrid-progress.json"):
                self.seen_controller_progress.append(json.loads(path.read_text()))
        if index < len(PHASES) - 1:
            return
        out = root + "/out"
        # The producer's own result manifest, written by the same helpers vibecomfy.run uses.
        import tempfile
        from astrid.pack_api import build_manifest, write_manifest
        name, data, media_type = self.output_for(job["job"])
        with tempfile.TemporaryDirectory() as scratch:
            (Path(scratch) / "artifacts").mkdir()
            (Path(scratch) / "artifacts" / name).write_bytes(data)
            write_manifest(Path(scratch) / "manifest.json", build_manifest(
                kind="vibecomfy.run", inputs={}, created=datetime.now(timezone.utc).isoformat(),
                outputs=[{"path": f"artifacts/{name}", "name": "vibecomfy_run", "ordinal": 0, "role": "result",
                          "is_primary": True, "media_type": media_type}],
                schema_version=2, warnings=[], engine_run_id=None, engine_prompt_id=None))
            manifest_bytes = (Path(scratch) / "manifest.json").read_bytes()
        tree = {f"{out}/artifacts/{name}": data, f"{out}/manifest.json": manifest_bytes}
        self.files.update(tree)
        result = {"status": "succeeded", "manifest": f"{out}/manifest.json"}
        self.files[root + "/result.json"] = json.dumps(result).encode()
        files = {path[len(out) + 1:]: {"sha256": _sha(data), "size": len(data)} for path, data in tree.items()}
        body = json.dumps({"schema_version": 1, "files": files}, sort_keys=True, indent=2) + "\n"
        self.files[f"{out}/{remote_placement.MANIFEST_NAME}"] = body.encode()
        stdout = (f"{remote_placement.RESULT_MARK}{json.dumps(result)}\n"
                  f"{remote_placement.MANIFEST_MARK}{_sha(body.encode())}\n").encode()
        job["state"] = "exited"
        self._exit(directory, 0, stdout)

    listed = ()

    def kill_pod(self):
        """The provider terminates the pod between jobs (spot reclaim, user action)."""
        self.killed.append(self.pod_id)
        self.pods.clear()
        self.files.clear()  # its container disk goes with it

    def output_for(self, job):
        """The pod's generated file for a job (an image unless a subclass knows the modality)."""
        return "cpu.png", self.image, "image/png"

    # -- HTTP ----------------------------------------------------------------

    def handle(self, request):
        host, path, method = request.url.host, request.url.path, request.method
        request.read()
        body = (json.loads(request.content)
                if request.content and request.headers.get("content-type", "").split(";", 1)[0] == "application/json"
                else {})
        self.events.append((method, host, path))

        def reply(data, status=200):
            encoded = json.dumps(data).encode()
            return httpx.Response(status, stream=httpx.ByteStream(encoded), request=request,
                                  headers={"Content-Type": "application/json", "Content-Length": str(len(encoded))})
        if host in {"api.runpod.io", "graphql.runpod.io"}:
            return reply({"data": self.graphql(body)})
        if host == "rest.runpod.io":
            if path.startswith("/v1/networkvolumes"):
                return reply(self._volumes())
            assert path.startswith("/v1/pods"), path
            if method == "DELETE":
                if path.endswith("/" + self.pod_id) and self.pod_id in self.killed:
                    # The provider already terminated this pod: an exact delete finds nothing.
                    self.events.append(("pod-delete-gone", self.pod_id))
                    return reply({}, 404)
                assert path.endswith("/" + self.pod_id) and self.pods
                record = json.loads(self.claim_path.read_text())
                assert record["release_authorized"] is True
                job = record["lease"].get("job")
                assert job is None or job["state"] in {"exited", "cancelled"}, "pod deleted with a live job"
                self.pods.clear()
                self.events.append(("pod-delete", self.pod_id))
                return reply({}, 204)
            assert method == "GET"
            if path == "/v1/pods":
                return reply(self.pods)
            if not self.pods:
                self.events.append(("pod-404", self.pod_id))
            return reply(self.pods[0] if self.pods else {}, 200 if self.pods else 404)
        if host == "astrid-egress-preflight-8000.proxy.runpod.net":
            return reply({}, 404)
        assert host == self.pod_id + "-8000.proxy.runpod.net", host
        assert "/engines/" not in path and path != "/engine-proxy", "the controller reached a pod engine"
        if path == "/health":
            return reply({"ok": True, "protocol": 1})
        if path == "/jobs" and method == "POST":
            code, stdout = self.command(body["command"])
            self.agent_jobs[body["id"]] = (code, stdout.encode())
            return reply({"id": body["id"]})
        if path.startswith("/jobs/"):
            code, stdout = self.agent_jobs[path.rsplit("/", 1)[1]]
            return reply({"status": "exited", "exit_code": code, "stdout": base64.b64encode(stdout).decode(),
                          "stderr": "", "stdout_offset": len(stdout), "stderr_offset": 0,
                          "stdout_eof": True, "stderr_eof": True})
        if path == "/uploads":
            upload_id = _sha(body["path"].encode())[:32]
            self.agent_jobs[upload_id] = body
            self.files[body["path"]] = b""
            return reply({"id": upload_id, "offset": 0, "complete": False})
        if path.startswith("/uploads/"):
            meta = self.agent_jobs[path.rsplit("/", 1)[1]]
            if method == "PUT":
                self.files[meta["path"]] += request.content
            data = self.files[meta["path"]]
            return reply({"offset": len(data), "complete": len(data) == meta["size"] and _sha(data) == meta["sha256"]})
        if path == "/download-chunk":
            target = body["path"]
            directory, _, name = target.rpartition("/")
            if name == "exit" and directory in self.detached:
                self._advance(directory)  # detached_poll reads status, then exit
            if target not in self.files:
                return reply({}, 400)
            data = self.files[target]
            return reply({"data": base64.b64encode(data[body["offset"]:]).decode(), "offset": body["offset"],
                          "total": len(data), "sha256": _sha(data[body["offset"]:]), "etag": _sha(data)})
        raise AssertionError((method, host, path))


@pytest.fixture
def journey(tmp_path, monkeypatch, real_runtime, tuple_sources):
    """Runtime, host and the provider fake; yields a small driver for one account."""
    from vibecomfy.workflow import VibeOutput, VibeWorkflow, WorkflowSource
    from vibecomfy.workflow_bundle import emit_bundle

    monkeypatch.setenv("ASTRID_RUNTIME_DATA_ROOT", str(tmp_path / "machine"))
    monkeypatch.setenv("ASTRID_MACHINE_POLL_SECONDS", "0.05")
    monkeypatch.setattr("astrid.packs.runpod.managed_sources.ensure_managed_sources", lambda: Path(tuple_sources))
    monkeypatch.setattr("astrid.packs.runpod.managed_sources._pins", lambda: tuple_pins(tuple_sources))
    monkeypatch.setenv("VIBECOMFY_ENTRIES_DIR", str(tmp_path / "entries"))
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

    state = {}

    def start(volume=None):
        preferences = {"provider_account_ref": "cpu-fixture", "gpu_types": ["CPU fixture GPU"],
            "image": "runpod/base:1.0.3-dev-fix-pytorch-version-verification-cuda1300-ubuntu2404"
                     "@sha256:24795acee5856a3e6d383e879ccdd53edd25d24ea38d0edab5a1d3718ea385e6",
            "volume": ({"id": None, "name": None, "size_gb": 0, "mount_path": "/workspace"} if volume is None
                       else dict(volume)),
            "idle_timeout_seconds": 1800,
            "credential_ref": "env:RUNPOD_RUNPOD_FAKE_CREDENTIAL", "budget_cap_usd": 2.0}
        target_selection.save_runpod_preferences(preferences)
        claim_path = target_selection.canonical_claim_path("cpu-fixture")
        provider = MachineProviderHTTP(claim_path, tmp_path / "attempts", volume=volume)
        send, send_requests = httpx.Client.send, requests.Session.send

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
            response = provider_response(httpx.Request(request.method, request.url, headers=request.headers,
                                                       content=request.body))
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

        daemon = real_runtime()
        owner = WorkspaceClient(daemon.endpoint, daemon.token)
        client = AstridClient(remote=RemoteAstridClient(owner))
        project = client.projects.create(name="CPU machine", slug="cpu-machine", idempotency_key="project")
        assert project.ok, project.error
        project_id = project.data["project_id"]
        owner.select_project(project_id, idempotency_key="select")
        packs = Path(__file__).resolve().parents[2] / "astrid" / "packs"
        host = GenericPackHost(pack_roots=[packs / "vibecomfy", packs / "runpod"],
            client=RuntimeProtocolClient(daemon.endpoint, daemon.worker_token), attempt_base=tmp_path / "attempts",
            credential_source={"RUNPOD_RUNPOD_FAKE_CREDENTIAL": "synthetic-only"})
        host.discover()
        host.preflight()
        host.register()
        worker = threading.Thread(target=host.run, kwargs={"poll_seconds": 0.02}, daemon=True)
        worker.start()
        state.update(provider=provider, daemon=daemon, host=host, worker=worker, client=client, owner=owner,
                     project=project_id, claim_path=claim_path, preferences=preferences)
        setup = client.invoke_result("runpod.setup", kind="action", project=None,
            inputs={"placement_policy": json.dumps({"kind": "runpod", "engine": "comfyui",
                                                    "preferences": preferences})},
            wait=True, timeout_seconds=90, poll_seconds=0.02, read_managed_outputs=False)
        assert setup.ok, setup.to_dict()
        return state

    def image(*, wait=True, invocation=None):
        client = state["client"]
        quote = GenerationFacade().describe(placement="runpod", client=client)
        return GenerationFacade().image(entry="cpu-image", placement="runpod", client=client,
            project=state["project"], consent=("paid",), max_usd=1.5,
            deadline=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            offer_digest=quote["offer_digest"], preparation_seconds=120, execution_seconds=60,
            idle_seconds=1800, wait=wait)

    def record():
        return json.loads(state["claim_path"].read_text())

    def release():
        result = GenerationFacade().release(engine="comfyui", placement="runpod", client=state["client"], wait=True)
        assert result.ok, result.to_dict()
        return record()

    driver = type("Journey", (), {})()
    driver.start, driver.image, driver.record, driver.release, driver.state = start, image, record, release, state
    driver.tmp = tmp_path
    try:
        yield driver
    finally:
        provider = state.get("provider")
        if provider is not None:
            (tmp_path / "provider-http-events.json").write_text(json.dumps(provider.events, indent=2, default=str))
            assert not any("/engines/" in str(event) or "/engine-proxy" in str(event) for event in provider.events)
        if "host" in state:
            state["host"]._shutdown.set()
            state["worker"].join(timeout=35)
            assert not state["worker"].is_alive()
            state["host"].shutdown()
            state["daemon"].stop()


def _released(provider, record):
    assert record["state"] == "cleanup_complete"
    assert record["cleanup"]["absence"]["exact_get_status"] == 404
    assert not provider.pods


def _machines_json(capsys):
    from astrid.core.machines_cli import main as machines

    capsys.readouterr()
    assert machines(["--json"]) == 0
    return json.loads(capsys.readouterr().out)


def test_cold_run_then_warm_second_run_reuses_the_machine(journey):
    j = journey
    s = j.start()
    provider = s["provider"]
    first = j.image()
    assert first.ok, first.to_dict()
    delivered = deliver_generation(first, client=s["client"], out=j.tmp / "first", project=s["project"])
    assert Path(delivered["image_paths"][0]).read_bytes() == provider.image
    assert provider.deploys == 1
    assert len(provider.kinds("pod_bootstrap.sh")) == 1
    assert len(provider.kinds("engines install comfyui")) == 1
    assert len(provider.kinds("generate list --json")) == 1
    record = j.record()
    lease = record["lease"]
    assert lease["state"] == "idle" and lease["job"]["state"] == "exited"
    preparation = lease["preparation"]
    assert preparation["state"] == "prepared"
    assert preparation["storage"] == {"mode": "ephemeral"}
    assert preparation["runtime_check"]["entry"] == "cpu-image" and preparation["runtime_check"]["ready"]
    custody = preparation["custody"]
    assert custody["gpu_type"] == "CPU fixture GPU" and custody["account"] == "cpu-fixture"
    # T5c: the attempt records which machine ran it, and the task listing shows it.
    shown = s["client"].tasks.show(first.task_id)
    machine = shown.data["result"]["machine"]
    assert (machine["placement"], machine["machine"], machine["id"]) == ("runpod", "runpod.runpod", POD)
    assert machine["lease"] == record["operation_id"] and machine["device"] == "CPU fixture GPU"
    from astrid.core.cli.list_narrow import machine_label, render_list_table
    assert machine_label(shown.data) == f"runpod:{POD} (CPU fixture GPU)"
    assert f"runpod:{POD}" in render_list_table([shown.data], total=1, noun="tasks")
    assert custody["digest"].startswith("sha256:")
    # Progress: each poll copied one phase into the controller's attempt file before the job ended.
    assert provider.seen_controller_progress and provider.seen_controller_progress[0]["phase"] in {"align", "submit"}

    jobs_before = list(provider.commands)
    second = j.image()
    assert second.ok, second.to_dict()
    assert provider.deploys == 1, "the warm machine was not reused"
    new = provider.commands[len(jobs_before):]
    assert not [c for c in new if "pod_bootstrap.sh" in c or "engines install" in c]
    assert len([c for c in new if "generate list --json" in c]) == 1
    assert len([c for c in new if " generate cpu-image " in c]) == 1
    assert j.record()["lease"]["preparation"]["custody"] == custody
    _released(provider, j.release())
    order = [e[0] for e in provider.events if e[0] in {"pod-delete", "pod-404"}]
    assert order[:2] == ["pod-delete", "pod-404"]


def test_a_custody_change_refuses_warm_reuse(journey):
    j = journey
    s = j.start(volume=VOLUME)
    provider = s["provider"]
    first = j.image()
    assert first.ok, first.to_dict()
    preparation = j.record()["lease"]["preparation"]
    assert preparation["storage"]["mode"] == "volume" and preparation["storage"]["mount_id"] == 7
    assert preparation["custody"]["volume"] == {"id": VOLUME["id"], "mount_path": "/workspace",
                                                "cache_device": 64768, "mount_id": 7}
    assert [e for e in provider.events if e[0] == "storage_probe"] == [("storage_probe", "/workspace")]
    # The volume was remounted under the same pod: the pinned mount id differs.
    provider.mount_id = 8
    generations = len(provider.kinds(" generate cpu-image "))
    with pytest.raises(Exception, match=r"custody differs: volume\.mount_id"):
        j.image()
    assert len(provider.kinds(" generate cpu-image ")) == generations, "a job ran on a refused machine"
    _released(provider, j.record())
    assert provider.deploys == 1


def test_cancel_mid_poll_stops_the_job_then_releases(journey):
    j = journey
    s = j.start()
    provider, client = s["provider"], s["client"]
    started = j.image(wait=False)
    task_id = started.kernel_task_id
    assert task_id, started.to_dict()
    deadline = time.monotonic() + 120
    while not provider.detached or next(iter(provider.detached.values()))["polls"] < 2:
        assert time.monotonic() < deadline, "the generation job never polled twice"
        time.sleep(0.05)
    provider.job_hold.clear()  # keep it running until the cancel lands
    assert client.tasks.cancel(task_id).ok
    while j.record()["state"] != "cleanup_complete":
        assert time.monotonic() < deadline, "the cancelled machine was not released"
        time.sleep(0.05)
    record = j.record()
    assert record["lease"]["job"]["state"] == "cancelled"
    _released(provider, record)
    order = [e[0] for e in provider.events if e[0] in {"detached_cancel", "pod-delete", "pod-404"}]
    assert order[:3] == ["detached_cancel", "pod-delete", "pod-404"]
    assert not any(path.endswith("/out/wrapper-manifest.json") for path in provider.files)


def test_a_bad_mount_mapping_fails_before_install_and_releases(journey):
    j = journey
    s = j.start(volume=VOLUME)
    provider = s["provider"]
    provider.mount_path = "/mnt/elsewhere"  # the provider maps the volume somewhere else
    with pytest.raises(Exception, match="mount mapping refused before install"):
        j.image()
    assert not provider.kinds("engines install"), "install ran on an unpinned volume"
    assert not [e for e in provider.events if e[0] == "storage_probe"]
    _released(provider, j.record())


def test_a_killed_pod_ends_its_lease_and_the_next_default_job_claims_fresh(journey, capsys):
    """T5d: liveness before reuse; provider_terminated; a pinned job is refused; astrid machines lists both."""
    j = journey
    s = j.start()
    provider = s["provider"]
    first = j.image()
    assert first.ok, first.to_dict()
    assert j.record()["lease"]["state"] == "idle" and provider.deploys == 1
    assert [row["id"] for row in _machines_json(capsys) if row["live"]] == [POD]
    provider.kill_pod()
    second = j.image()
    assert second.ok, second.to_dict()
    assert provider.deploys == 2, "the dead machine was not replaced"
    assert len(provider.kinds("pod_bootstrap.sh")) == 2, "the fresh machine skipped bootstrap"
    machine = s["client"].tasks.show(second.task_id).data["result"]["machine"]
    assert machine["id"] == f"{POD}2" and machine["warm"] is False
    rows = {row["id"]: row for row in _machines_json(capsys)}
    assert rows[f"{POD}2"]["live"] and rows[f"{POD}2"]["state"] == "idle"
    assert rows[POD]["live"] is False and rows[POD]["ended"] == "provider_terminated"
    # A job pinned to the dead machine is refused with the reason and the live leases.
    with pytest.raises(Exception, match=rf"no live runpod lease for machine '{POD}' \(ended: provider_terminated\); live leases: {POD}2"):
        GenerationFacade().image(entry="cpu-image", placement={"kind": "runpod", "machine": POD},
            client=s["client"], project=s["project"], consent=("paid",), max_usd=1.5,
            deadline=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            offer_digest=GenerationFacade().describe(placement="runpod", client=s["client"])["offer_digest"],
            preparation_seconds=120, execution_seconds=60, idle_seconds=1800, wait=True)
    # Pinned to the live one, it runs warm there.
    pinned = GenerationFacade().image(entry="cpu-image", placement={"kind": "runpod", "machine": f"{POD}2"},
        client=s["client"], project=s["project"], consent=("paid",), max_usd=1.5,
        deadline=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        offer_digest=GenerationFacade().describe(placement="runpod", client=s["client"])["offer_digest"],
        preparation_seconds=120, execution_seconds=60, idle_seconds=1800, wait=True)
    assert pinned.ok and provider.deploys == 2
    _released(provider, j.release())
    rows = {row["id"]: row for row in _machines_json(capsys)}
    assert rows[f"{POD}2"]["ended"] == "explicit" and rows[POD]["ended"] == "provider_terminated"
