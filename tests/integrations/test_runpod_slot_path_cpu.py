"""Every default generation slot through the real RunPod host bind, on CPU.

Real SDK, Runtime, host, lease owner and engine bridge (the journey's
harness); doubles sit only at HTTP I/O. The stand-in pod validates /prompt
against object_info built from VibeComfy's packaged real node schemas, fills
loader menus from its install receipt, and returns the real output keys.
"""
from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import shlex
import re
import socket
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
import requests

from astrid.core.execution.generic_host import GenericPackHost, RuntimeProtocolClient
from astrid.core.generation.delivery import deliver_generation
from astrid.packs.runpod import target_selection
from astrid.sdk import AstridClient, generation
from astrid.sdk.generation import GenerationFacade
from astrid.sdk.remote import RemoteAstridClient
from astrid.sdk.workspace_client import WorkspaceClient
from tests.integrations.runpod_slot_pod import PodComfy, pin_object_info, receipt_files
from tests.integrations.test_runpod_cpu_path import ProviderHTTP
from tests.integrations.test_runpod_host_endpoint_r5 import tuple_sources  # noqa: F401  (fixture)

pytestmark = pytest.mark.integration

SLOTS = sorted(generation._DEFAULT_ENTRIES)
PROMPTS = {("image", "t2i"): {"prompt": "A red cube on a white tabletop."},
           ("image", "edit"): {"prompt": "Make the cube blue."},
           ("audio", "tts"): {"text": "Hello from the pod."},
           ("audio", "music"): {"prompt": "A short upbeat chiptune."},
           ("audio", "sfx"): {"prompt": "Footsteps on gravel."},
           ("video", "i2v"): {"prompt": "A boat drifting on a calm lake."}}
# The i2v default has no facade slot mapping; its workflow is the entry the engine-selection test resolves.
_CAPABILITY_ENTRIES = {("video", "i2v"): ("ltx-2.3",)}
TARGET_KINDS = ("local-managed", "attached", "runpod")


def _slot_classes(slot):
    """Classes of every default workflow of a slot, from the real ready templates."""
    from astrid.core.generation import entries
    ready = entries.ready_entries()
    classes = set()
    for identifier in generation._DEFAULT_ENTRIES.get(slot, _CAPABILITY_ENTRIES.get(slot, ())):
        for row in ready:
            if identifier in (row["id"], row.get("registry_id")):
                workflow = entries._load_entry_workflow(row)
                classes |= {node.class_type for node in workflow.runtime_nodes().values()}
    return classes


class SlotPod(ProviderHTTP):
    """The journey's provider/pod contract with a validating ComfyUI behind the engine proxy."""

    def __init__(self, source_root, claim_path, classes):
        super().__init__(source_root, claim_path)
        self.receipts = {"comfyui": self.receipt}
        # Each engine's own readiness gate needs its required nodes in /object_info, besides the workflow's classes.
        from astrid.core.generation import engine_recipe, entries
        required = {node for engine in entries.runpod_engines() for node in
                    engine_recipe.load_recipe(engine, "linux-x86_64-cu130").health.required_nodes}
        self.comfy = PodComfy(set(classes) | required, self._installed_files, extra=pin_object_info())

    def _receipt_for(self, engine):
        """The pod's install receipt for any RunPod engine, synthesized from that engine's own recipe and lock."""
        if engine not in self.receipts:
            from dataclasses import asdict
            from astrid.core.generation import engine_recipe, engine_runner
            from astrid.core.generation.engine_contracts import (
                DownloadResult, HealthEvidence, InstallReceipt, StepReceipt)
            recipe = engine_recipe.load_recipe(engine, "linux-x86_64-cu130")
            root = f"/root/astrid/engines/{engine}"
            plan = engine_runner.plan_install(recipe, root=root, placement="local")
            lock = engine_recipe.load_model_lock(recipe.models_lock)
            stamp = datetime.now(timezone.utc).isoformat()
            receipt = InstallReceipt(
                "astrid.engine-install.v1", f"cpu-http-install-{engine}", plan.engine, plan.platform, plan.placement,
                root, "installed", False, plan.recipe_sha256, plan.models_lock_sha256,
                {env.name: (env.requirements.packaged_lock.sha256 if env.requirements.packaged_lock
                            else hashlib.sha256(env.name.encode()).hexdigest()) for env in recipe.environments},
                {source.name: source.pin.revision for source in recipe.sources},
                tuple(DownloadResult(model.name, root + "/models/" + model.destination, "downloaded",
                                     model.bytes, model.sha256, True, 0) for model in lock.models),
                tuple(StepReceipt(step.id, step.kind, "executed", "passed", {}) for step in plan.steps),
                HealthEvidence("healthy", stamp, hashlib.sha256(json.dumps(self.schema).encode()).hexdigest(), True),
                stamp, stamp, None)
            engine_runner._parse_receipt(asdict(receipt), plan)
            self.receipts[engine] = asdict(receipt)
        return self.receipts[engine]

    def _installed_files(self):
        files = []
        for engine, receipt in self.receipts.items():
            if f"/root/astrid/engines/{engine}/.astrid-engine/install.json" in self.files:
                files += receipt_files(receipt)
        return files

    def graphql(self, body):
        query = body["query"]
        match = re.search(r"gpuTypes\(input: \{id: (\"[^\"]*\")\}\)", query)
        if match:
            gpu = json.loads(match[1])
            return {"gpuTypes": [{"id": gpu, "displayName": "CPU fixture GPU"}]}
        data = super().graphql(body)
        for pod in self.pods:
            # The agent's rate gate compares gpuCount x costPerHr; the journey's pod omits the count.
            pod.setdefault("gpuCount", 1)
            pod["machine"].setdefault("gpuCount", 1)
        return data

    def command(self, command):
        match = re.search(r"engines (status|install) (\S+) --platform", command)
        if match and match[2] != "comfyui":
            engine, operation = match[2], match[1]
            marker = f"/root/astrid/engines/{engine}/.astrid-engine/install.json"
            if operation == "status":
                return 0, json.dumps(self._receipt_for(engine) if marker in self.files else None)
        if "base64 -d" in command and '"$d/command.sh"' in command:
            detached = base64.b64decode(re.search(r"printf %s (\S+) \| base64 -d", command)[1]).decode()
            match = re.search(r"engines install (\S+) --platform", detached)
            if match and match[1] != "comfyui":
                self.commands.append(command)
                self.commands.append(detached)
                directory = shlex.split(command.splitlines()[1])[0].removeprefix("d=")
                self.files[f"/root/astrid/engines/{match[1]}/.astrid-engine/install.json"] = json.dumps(
                    self._receipt_for(match[1])).encode()
                self.files.update({directory + "/status": b"exited", directory + "/exit": b"0",
                                   directory + "/pid": b"123", directory + "/stdout": b"", directory + "/stderr": b""})
                return 0, ""
        if ': > "$d/cancel"' in command:  # the job-cancel script the controller runs when it abandons a job
            self.commands.append(command)
            return 0, ""
        return super().command(command)

    def handle(self, request):
        path = request.url.path
        if request.url.host == self.pod_id + "-8000.proxy.runpod.net" and "/engines/" in path:
            operation = path.split("/", 3)[-1]
            request.read()
            def reply(data, status=200):
                encoded = json.dumps(data).encode()
                return httpx.Response(status, stream=httpx.ByteStream(encoded), request=request,
                    headers={"Content-Type": "application/json", "Content-Length": str(len(encoded))})
            if operation == "object_info":
                return reply(self.comfy.object_info())
            if operation == "prompt":
                status, payload = self.comfy.submit(json.loads(request.content))
                return reply(payload, status)
            if operation.startswith("history"):
                history = getattr(self.comfy, "history", {})
                tail = operation.partition("/")[2]
                return reply({tail: history[tail]} if tail in history else {} if tail else history)
            if operation.startswith("view"):
                name = request.url.params.get("filename")
                self.comfy.views.append(name)
                data, mime = self.comfy.outputs[name]
                return httpx.Response(200, stream=httpx.ByteStream(data), request=request,
                    headers={"Content-Type": mime, "Content-Length": str(len(data))})
            if operation == "upload/image":
                body = request.content
                subfolder = re.search(rb'name="subfolder"\r\n\r\n([^\r]*)\r\n', body)
                name = re.search(rb'name="image"; filename="([^"]+)"', body)
                saved = name[1].decode()
                self.comfy.uploads[saved] = body
                self.comfy.input_files.add(f"{subfolder[1].decode()}/{saved}" if subfolder and subfolder[1] else saved)
                return reply({"name": saved, "subfolder": subfolder[1].decode() if subfolder else "", "type": "input"})
        return super().handle(request)


@contextlib.contextmanager
def _attached_comfy(comfy):
    """A human-attached ComfyUI on loopback: the stand-in pod's ComfyUI behind a real socket."""
    import http.server
    from urllib.parse import parse_qs, urlsplit

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, status, data, mime="application/json"):
            body = data if isinstance(data, bytes) else json.dumps(data).encode()
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            url = urlsplit(self.path)
            if url.path == "/object_info":
                return self._send(200, comfy.object_info())
            if url.path == "/system_stats":
                return self._send(200, {"system": {"os": "linux"}})
            if url.path == "/queue":
                return self._send(200, {"queue_running": [], "queue_pending": []})
            if url.path.startswith("/history"):
                tail = url.path.partition("/history/")[2]
                history = getattr(comfy, "history", {})
                return self._send(200, {tail: history[tail]} if tail in history else {} if tail else history)
            if url.path == "/view":
                name = parse_qs(url.query)["filename"][0]
                comfy.views.append(name)
                data, mime = comfy.outputs[name]
                return self._send(200, data, mime)
            self._send(404, {})

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            if self.path == "/prompt":
                status, payload = comfy.submit(json.loads(body))
                return self._send(status, payload)
            if self.path in ("/queue", "/interrupt", "/free"):
                return self._send(200, {})
            if self.path == "/upload/image":
                subfolder = re.search(rb'name="subfolder"\r\n\r\n([^\r]*)\r\n', body)
                name = re.search(rb'name="image"; filename="([^"]+)"', body)
                saved = name[1].decode()
                comfy.uploads[saved] = body
                comfy.input_files.add(f"{subfolder[1].decode()}/{saved}" if subfolder and subfolder[1] else saved)
                return self._send(200, {"name": saved, "subfolder": subfolder[1].decode() if subfolder else "",
                                        "type": "input"})
            self._send(404, {})

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()


@contextlib.contextmanager
def _drive(slot, tmp_path, monkeypatch, real_runtime, tuple_sources, extra_classes=(), call=None,
           machine_identity=False, target_kind="runpod"):
    monkeypatch.setenv("ASTRID_RUNTIME_DATA_ROOT", str(tmp_path / "machine"))
    monkeypatch.setattr('astrid.packs.runpod.managed_sources.ensure_managed_sources', lambda: Path(tuple_sources))
    import subprocess
    def pins():
        # The installed release manifest does not exist on a source checkout; pin each bootstrap
        # source to the HEAD of the shared clean source tuple the journey already ships.
        head = lambda name: subprocess.check_output(
            ["git", "-C", str(Path(tuple_sources) / "src" / name), "rev-parse", "HEAD"], text=True).strip()
        return {f"src/{name}": (f"https://example.invalid/{name}", head(name))
                for name in ("astrid-agent", "astrid-runtime", "runpod-lifecycle", "VibeComfy")}
    monkeypatch.setattr('astrid.packs.runpod.managed_sources._pins', pins)
    preferences = {"provider_account_ref": "cpu-fixture", "gpu_types": ["CPU fixture GPU"],
        "image": "runpod/base:1.0.3-dev-fix-pytorch-version-verification-cuda1300-ubuntu2404"
                 "@sha256:24795acee5856a3e6d383e879ccdd53edd25d24ea38d0edab5a1d3718ea385e6",
        "volume": {"id": None, "name": None, "size_gb": 0, "mount_path": "/workspace"},
        "idle_timeout_seconds": 1800,
        "credential_ref": "env:RUNPOD_RUNPOD_FAKE_CREDENTIAL", "budget_cap_usd": 2.0}
    target_selection.save_runpod_preferences(preferences)
    claim_path = target_selection.canonical_claim_path("cpu-fixture")
    provider = SlotPod(tuple_sources, claim_path, _slot_classes(slot) | set(extra_classes))
    send, send_requests, connect = httpx.Client.send, requests.Session.send, socket.socket.connect
    local = {"127.0.0.1", "localhost", "::1"}
    def respond(request):
        try:
            return provider.handle(request)
        except Exception as error:
            (tmp_path / "provider-http-failure.json").write_text(json.dumps({
                "type": type(error).__name__, "message": str(error)[:1000]}))
            raise
    def http_send(client, request, **kwargs):
        if request.url.host in local:
            return send(client, request, **kwargs)
        response = respond(request)
        if not kwargs.get("stream"):
            response.read()
        return response
    def requests_send(session, request, **kwargs):
        if httpx.URL(request.url).host in local:
            return send_requests(session, request, **kwargs)
        response = respond(httpx.Request(request.method, request.url, headers=request.headers, content=request.body))
        result = requests.Response()
        result.status_code, result._content = response.status_code, response.read()
        return result
    def local_connect(sock, address):
        assert not isinstance(address, tuple) or address[0] in local
        return connect(sock, address)
    monkeypatch.setattr(httpx.Client, "send", http_send)
    monkeypatch.setattr(requests.Session, "send", requests_send)
    monkeypatch.setattr(socket.socket, "connect", local_connect)

    from astrid.core.execution._child_bridge import ChildBridgeError, HostChildBridge
    dispatch = HostChildBridge.dispatch
    def observe_dispatch(bridge, request):
        try:
            return dispatch(bridge, request)
        except Exception as exc:
            provider.bridge_rejections.append(str(exc) if isinstance(exc, ChildBridgeError) else type(exc).__name__)
            raise
    provider.bridge_rejections = []
    monkeypatch.setattr(HostChildBridge, "dispatch", observe_dispatch)
    daemon = real_runtime()
    worker_token = daemon.worker_token
    if machine_identity:
        # H3's delegated child needs the ordinary host's credential-verified machine identity
        # (test-owned Runtime state, as in the retained-endpoint H3 journey).
        import hashlib
        from runtime_protocol.daemon import WORKER_SCOPES
        worker_token, _ = daemon.credentials.provision('astrid-pack-host', list(WORKER_SCOPES), rotate=True,
            metadata={'execution_binding': {'actual': {'kind': 'machine', 'id': 'slot-path-test-machine'},
                'executor_incarnation': 'slot-path-test', 'verification': {'method': 'credential_claim',
                    'verified': True, 'evidence_digest': 'sha256:' + hashlib.sha256(b'test-machine').hexdigest()}}})
    owner = WorkspaceClient(daemon.endpoint, daemon.token)
    client = AstridClient(remote=RemoteAstridClient(owner))
    project = client.projects.create(name="CPU RunPod", slug="cpu-runpod", idempotency_key="project")
    assert project.ok, project.error
    project_id = project.data["project_id"]
    owner.select_project(project_id, idempotency_key="select")
    invoke = client.invoke_result
    def bounded(capability, **kwargs):
        kwargs.setdefault("timeout_seconds", 600)
        return invoke(capability, **kwargs)
    client.invoke_result = bounded
    packs = Path(__file__).resolve().parents[2] / "astrid" / "packs"
    host = GenericPackHost(pack_roots=[packs / "vibecomfy", packs / "h3_av", packs / "runpod"],
        client=RuntimeProtocolClient(daemon.endpoint, worker_token), max_concurrency=4, attempt_base=tmp_path / "attempts",
        credential_source={"RUNPOD_RUNPOD_FAKE_CREDENTIAL": "synthetic-only"})
    host.discover()
    host.preflight()
    host.register()
    worker = threading.Thread(target=host.run, kwargs={"poll_seconds": 0.02}, daemon=True)
    worker.start()
    try:
        with contextlib.ExitStack() as stack:
            if target_kind == "runpod":
                setup = client.invoke_result("runpod.setup", kind="action", project=None,
                    inputs={"placement_policy": json.dumps({"kind": "runpod", "engine": "comfyui", "preferences": preferences})},
                    wait=True, timeout_seconds=90, poll_seconds=0.02, read_managed_outputs=False)
                assert setup.ok
                quote = GenerationFacade().describe(target="runpod", client=client)
                controls = dict(target="runpod", client=client, project=project_id, consent=("paid",), max_usd=1.5,
                    deadline=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
                    offer_digest=quote["offer_digest"], preparation_seconds=120, execution_seconds=60,
                    idle_seconds=1800, wait=True)
            elif target_kind == "attached":
                # A human's ComfyUI already holds the models its engines' install receipts list (as a
                # provisioned pod does after its install); the RunPod setup never ran against it.
                from astrid.core.generation import entries
                files = [path for engine in dict.fromkeys(("comfyui", *entries.runpod_engines()))
                         for path in receipt_files(provider._receipt_for(engine))]
                provider.comfy.installed_files = lambda: files
                url = stack.enter_context(_attached_comfy(provider.comfy))
                controls = dict(target=url, client=client, project=project_id, wait=True)
            else:
                controls = dict(target="managed", client=client, project=project_id, wait=True)
            result = call(GenerationFacade(), controls, tmp_path)
            yield client, project_id, provider, result
    finally:
        (tmp_path / "provider-http-events.json").write_text(json.dumps(
            {"events": provider.events, "commands": provider.commands,
             "rejections": provider.comfy.rejections, "bridge_rejections": provider.bridge_rejections}, indent=1, default=str))
        host._shutdown.set()
        worker.join(timeout=35)
        host.shutdown()
        daemon.stop()


# The sound-effects case needs real Foley weights (hunyuanvideo_foley_xl.pth,
# vae_128d_48k.pth); the CPU harness keeps none, so it is gpu-marked (Plan 34). Plan 33's U3
# removed the refusal: the case now reaches the pod and fails only on those missing weights.
_WEIGHTS = "needs real model weights, which the CPU harness keeps none of (Plan 34)"
_SLOT_PARAMS = [
    pytest.param(slot, marks=pytest.mark.gpu) if slot == ("audio", "sfx") else slot
    for slot in SLOTS
]


@pytest.mark.parametrize("slot", _SLOT_PARAMS, ids=lambda slot: "/".join(slot))
def test_default_slot_through_the_runpod_bind(slot, tmp_path, monkeypatch, real_runtime, tuple_sources):
    modality, mode = slot
    def call(facade, controls, tmp):
        inputs = dict(PROMPTS[slot])
        if modality == "image" and mode == "edit":
            source = tmp / "source.png"
            from PIL import Image
            Image.new("RGB", (64, 64), (200, 30, 30)).save(source)
            inputs["image"] = str(source)
        if slot == ("audio", "sfx"):
            import subprocess
            video = tmp / "source.mp4"
            subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                            "color=c=blue:s=256x144:r=24:d=1", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(video)],
                           check=True, timeout=60)
            inputs["video_ref"] = str(video)
        method = facade.image if modality == "image" else facade.audio
        return method(mode=mode, **controls, **inputs)
    with _drive(slot, tmp_path, monkeypatch, real_runtime, tuple_sources, call=call) as (client, project_id, provider, result):
        assert result.ok, json.dumps(result.to_dict(), default=str)[:4000]
        delivered = deliver_generation(result, client=client, out=tmp_path / "delivered", project=project_id)
        assert delivered, "nothing was delivered"
        assert provider.comfy.prompts and not provider.comfy.rejections
        assert provider.comfy.views, "no output was retrieved over /view"
        task = client.tasks.show(result.task_id)
        assert task.ok and task.data["state"] == "succeeded"
        if slot == ("audio", "sfx"):
            assert provider.comfy.uploads, "the source video was never uploaded to the pod"


H3_REQUEST = Path(__file__).resolve().parents[1] / "fixtures" / "h3_cpu" / "request.v2.json"


def test_h3_av_through_the_runpod_bind(tmp_path, monkeypatch, real_runtime, tuple_sources):
    """The Matrix continuation request (57 frames, 24 fps, 1024x576 source) through the host bind."""
    import subprocess
    source = tmp_path / "source.mp4"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    "color=c=blue:s=1024x576:r=24", "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
                    "-frames:v", "57", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
                    str(source)], check=True, timeout=120)
    request = json.loads(H3_REQUEST.read_text())
    assets = tmp_path / "assets.json"
    assets.write_text(json.dumps({request["media"][0]["asset"]["id"]: str(source), "source": str(source)}))
    h3_classes = set(json.loads((Path(__file__).resolve().parents[1]
                                 / "fixtures/h3_cpu/object_info_h3_runpod_graph_closure.json").read_text()))
    def call(facade, controls, tmp):
        return facade.video(entry="h3", request=str(H3_REQUEST), asset_map=str(assets), **controls)
    with _drive(("video", "h3"), tmp_path, monkeypatch, real_runtime, tuple_sources,
                extra_classes=h3_classes, call=call, machine_identity=True) as (client, project_id, provider, result):
        assert result.ok, json.dumps(result.to_dict(), default=str)[:4000]
        delivered = deliver_generation(result, client=client, out=tmp_path / "delivered", project=project_id)
        assert delivered, "nothing was delivered"
        assert provider.comfy.prompts and not provider.comfy.rejections
        assert provider.comfy.views, "no output was retrieved over /view"
        assert client.tasks.show(result.task_id).data["state"] == "succeeded"


VOICE_CHILD = '''
import json, sys
from pathlib import Path
url, work, template = sys.argv[1:]
work = Path(work)
workflow = work / "voice.json"
workflow.write_text(json.dumps({"template_id": template, "bindings": {}}))
from astrid.packs.vibecomfy.actions.run import run as action
raise SystemExit(action.main(["run", str(workflow), "--comfy-url", url, "--out", str(work / "outputs"),
    "--task-identity", "voice-task", "--attempt-identity", "voice-attempt",
    "--workflow-inputs", json.dumps({"text": "Hello from the managed target."})]))
'''


# needs real model weights; VibeComfy verifies sha256; CPU harness keeps none; Plan 34
@pytest.mark.gpu
def test_local_managed_voice(tmp_path, monkeypatch, tuple_sources):
    """The managed local target (as in the installed Qwen child path) for the voice default's real template."""
    import http.server
    import subprocess
    import sys
    pod = SlotPod(tuple_sources, tmp_path / "claim.json", _slot_classes(("audio", "tts")))
    # The managed target's inventory is the stand-in pod's install receipt; the controller keeps no model files.
    pod.receipts["comfyui"] = pod._receipt_for("vibecomfy-h3")
    pod.files["/root/astrid/engines/comfyui/.astrid-engine/install.json"] = b"{}"
    comfy = pod.comfy
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def _send(self, status, data, mime="application/json"):
            body = data if isinstance(data, bytes) else json.dumps(data).encode()
            self.send_response(status); self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
        def do_GET(self):
            path = self.path
            if path.startswith("/object_info"): return self._send(200, comfy.object_info())
            if path.startswith("/system_stats"): return self._send(200, {"system": {"os": "linux"}})
            if path.startswith("/queue"): return self._send(200, {"queue_running": [], "queue_pending": []})
            if path.startswith("/history"):
                tail = path.partition("/history/")[2]
                history = getattr(comfy, "history", {})
                return self._send(200, {tail: history[tail]} if tail in history else {} if tail else history)
            if path.startswith("/view"):
                from urllib.parse import parse_qs, urlsplit
                name = parse_qs(urlsplit(path).query)["filename"][0]
                comfy.views.append(name)
                data, mime = comfy.outputs[name]
                return self._send(200, data, mime)
            self._send(404, {})
        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            if self.path == "/prompt":
                status, payload = comfy.submit(json.loads(body))
                return self._send(status, payload)
            self._send(404, {})
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        work = tmp_path / "work"
        work.mkdir()
        result = subprocess.run([sys.executable, "-c", VOICE_CHILD, f"http://127.0.0.1:{server.server_port}",
                                 str(work), "audio/qwen3_tts_custom_voice"],
                                capture_output=True, text=True, timeout=300, env={**os.environ, "VIBECOMFY_HEADLESS": "1"})
    finally:
        server.shutdown()
    assert result.returncode == 0, result.stdout + result.stderr
    assert comfy.prompts and not comfy.rejections
    assert comfy.views, "no output was retrieved over /view"
    manifest = json.loads((work / "outputs" / "manifest.json").read_text())
    assert manifest["managed_generation_result"]["outcomes"]["execution"]["status"] == "succeeded"


# One case per generation capability, each on every target kind. The runpod cells
# repeat the slot and h3 cases above through the same drive; the local-managed and
# attached cells are the questions T3/T4 answer. The i2v default is absent from
# _DEFAULT_ENTRIES and reaches RunPod only through its own engine lock.
CAPABILITIES = [("image", "t2i"), ("image", "edit"), ("audio", "tts"), ("audio", "music"),
                ("audio", "sfx"), ("video", "i2v"), ("video", "h3")]
# A cell is gpu-marked only where the CPU harness cannot run it by nature: a local ComfyUI needs a GPU
# runtime, and sfx, tts and i2v need model weights the harness does not keep. Cells that fail for a
# product reason (attached H3 routing, the RunPod LTX lock, the H3 child timing out) carry no mark.
_GPU_CELLS = {(("audio", "sfx"), "attached"), (("audio", "tts"), "attached"), (("video", "i2v"), "attached"),
              (("audio", "sfx"), "runpod")}
_CELL_MARKS = {(c, "local-managed"): [pytest.mark.gpu] for c in CAPABILITIES}
_CELL_MARKS.update({cell: [pytest.mark.gpu] for cell in _GPU_CELLS})


def _video_source(path, size, duration=1):
    import subprocess
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    f"color=c=blue:s={size}:r=24:d={duration}", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)],
                   check=True, timeout=60)
    return path


def _capability_call(capability):
    modality, mode = capability
    def call(facade, controls, tmp):
        inputs = dict(PROMPTS[capability])
        if capability == ("image", "edit"):
            from PIL import Image
            source = tmp / "source.png"
            Image.new("RGB", (64, 64), (200, 30, 30)).save(source)
            inputs["image"] = str(source)
        if capability == ("video", "i2v"):
            from PIL import Image
            source = tmp / "start.png"
            Image.new("RGB", (64, 64), (30, 30, 200)).save(source)
            inputs["image_ref"] = str(source)
        if capability == ("audio", "sfx"):
            inputs["video_ref"] = str(_video_source(tmp / "source.mp4", "256x144"))
        if modality == "image":
            return facade.image(mode=mode, **controls, **inputs)
        if modality == "audio":
            return facade.audio(mode=mode, **controls, **inputs)
        return facade.video(**controls, **inputs)
    return call


@pytest.mark.parametrize(
    "capability,target_kind",
    [pytest.param(c, t, id=f"{'/'.join(c)}-{t}", marks=_CELL_MARKS.get((c, t), []))
     for t in TARGET_KINDS for c in CAPABILITIES])
def test_capability_reaches_generation_on_each_target(capability, target_kind, tmp_path, monkeypatch,
                                                      real_runtime, tuple_sources):
    if capability == ("video", "h3"):
        import subprocess
        source = tmp_path / "source.mp4"
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                        "color=c=blue:s=1024x576:r=24", "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
                        "-frames:v", "57", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
                        str(source)], check=True, timeout=120)
        request = json.loads(H3_REQUEST.read_text())
        assets = tmp_path / "assets.json"
        assets.write_text(json.dumps({request["media"][0]["asset"]["id"]: str(source), "source": str(source)}))
        h3_classes = set(json.loads((Path(__file__).resolve().parents[1]
                                     / "fixtures/h3_cpu/object_info_h3_runpod_graph_closure.json").read_text()))
        def call(facade, controls, tmp):
            return facade.video(entry="h3", request=str(H3_REQUEST), asset_map=str(assets), **controls)
        drive = dict(extra_classes=h3_classes, call=call, machine_identity=True)
    else:
        drive = dict(call=_capability_call(capability))
    with _drive(capability, tmp_path, monkeypatch, real_runtime, tuple_sources, target_kind=target_kind,
                **drive) as (client, project_id, provider, result):
        assert result.ok, json.dumps(result.to_dict(), default=str)[:4000]
        delivered = deliver_generation(result, client=client, out=tmp_path / "delivered", project=project_id)
        assert delivered, "nothing was delivered"
        assert provider.comfy.prompts and not provider.comfy.rejections
        assert provider.comfy.views, "no output was retrieved over /view"
        assert client.tasks.show(result.task_id).data["state"] == "succeeded"
