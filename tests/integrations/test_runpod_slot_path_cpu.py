"""Every default generation slot through the RunPod machine, on CPU.

Real SDK, Runtime, host and lease owner; doubles sit only at HTTP I/O. On
RunPod the job runs on the machine (Plan 32 T5): the fake pod answers the
pod-side local run (``test_remote_machine_cpu.MachineProviderHTTP``). For the
attached and local cells, a stand-in ComfyUI validates /prompt against
object_info built from VibeComfy's packaged real node schemas, fills loader
menus from its install receipt, and returns the real output keys.
"""
from __future__ import annotations

import contextlib
import json
import os
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
from tests.integrations.test_remote_machine_cpu import MachineProviderHTTP
from tests.integrations.test_runpod_host_endpoint_r5 import tuple_sources  # noqa: F401  (fixture)

from tests.helpers.generation_defaults import default_entries
SLOT_DEFAULTS = default_entries()
pytestmark = pytest.mark.integration

SLOTS = sorted(SLOT_DEFAULTS)
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
    for identifier in SLOT_DEFAULTS.get(slot, _CAPABILITY_ENTRIES.get(slot, ())):
        for row in ready:
            if identifier in (row["id"], row.get("registry_id")):
                workflow = entries._load_entry_workflow(row)
                classes |= {node.class_type for node in workflow.runtime_nodes().values()}
    return classes


def _h3_built_classes(request_path, asset_map, tmp):
    """Node classes of the graph h3's build picks for this request (the stand-in ComfyUI must know them)."""
    from astrid.core.generation import entries
    built = entries.build_entry(entries.find_entry("h3"), {"request": str(request_path), "asset_map": asset_map},
                                Path(tmp) / "classes-job")
    return {node.class_type for node in built["workflow"].runtime_nodes().values()}


def _media(kind, tmp):
    """A small real media file, so delivery and thumbnails probe real bytes."""
    import subprocess
    import tempfile
    with tempfile.TemporaryDirectory() as scratch:
        path = Path(scratch) / ("out.wav" if kind == "audio" else "out.mp4")
        source = (["-f", "lavfi", "-i", "sine=frequency=440:duration=1"] if kind == "audio"
                  else ["-f", "lavfi", "-i", "color=c=blue:s=64x64:r=24:d=1", "-pix_fmt", "yuv420p"])
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *source, str(path)],
                       check=True, timeout=60)
        return path.read_bytes()


class SlotPod(MachineProviderHTTP):
    """The machine fake for the runpod cells; a validating stand-in ComfyUI for attached and local cells."""

    def __init__(self, claim_path, attempts, classes):
        super().__init__(claim_path, attempts)
        self.expect_entry = None
        from astrid.core.generation import entries
        self.listed = sorted({row["id"] for row in entries.ready_entries()}
                             | {row["id"] for row in entries.contributed_entries()}  # the pod's built-in packs (h3)
                             | {value for values in SLOT_DEFAULTS.values() for value in values}
                             | {value for values in _CAPABILITY_ENTRIES.values() for value in values})
        # Each engine's own readiness gate needs its required nodes in /object_info, besides the workflow's classes.
        from astrid.core.generation import engine_recipe
        required = {node for engine in entries.runpod_engines() for node in
                    engine_recipe.load_recipe(engine, "linux-x86_64-cu130").health.required_nodes}
        self.comfy = PodComfy(set(classes) | required, self._installed_files, extra=pin_object_info())
        self._outputs = {}

    def _installed_files(self):
        files = []
        for engine, receipt in self.receipts.items():
            if f"/root/astrid/engines/{engine}/.astrid-engine/install.json" in self.files:
                files += receipt_files(receipt)
        return files

    def output_for(self, job):
        mode = job.get("mode")
        kind = "image" if mode in {"t2i", "edit"} else "audio" if mode in {"tts", "music", "sfx"} else "video"
        if kind == "image":
            return "cpu.png", self.image, "image/png"
        if kind not in self._outputs:
            self._outputs[kind] = _media(kind, None)
        return (("cpu.wav", self._outputs[kind], "audio/wav") if kind == "audio"
                else ("cpu.mp4", self._outputs[kind], "video/mp4"))


@contextlib.contextmanager
def _attached_comfy(comfy, port=0):
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

    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()


@contextlib.contextmanager
def _managed_local_engine(provider, tmp_path, monkeypatch):
    """A registered managed ComfyUI install on this machine: a receipt under a recorded root, with the stand-in
    pod's ComfyUI listening where the recipe starts it. Only the install-health probe is a double."""
    import json as _json
    from astrid.core.generation import engine_recipe, engine_runner, entries
    from astrid.core.generation.engine_local_endpoint import DEFAULT_PLATFORM, record_engine_root
    # What the machine's installs list, read before the home directory moves to the test's.
    files = [p for engine in dict.fromkeys(("comfyui", *entries.runpod_engines()))
             for p in receipt_files(provider._receipt_for(engine))]
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    root = str(tmp_path / "managed" / "engines" / "comfyui")
    receipt = provider._install_receipt("comfyui", root)
    path = Path(root) / ".astrid-engine" / "install.json"
    path.parent.mkdir(parents=True)
    path.write_text(_json.dumps(receipt))
    record_engine_root("comfyui", root)
    provider.comfy.installed_files = lambda: files
    plan = engine_runner.plan_install(engine_recipe.load_recipe("comfyui", DEFAULT_PLATFORM), root=root, placement="local")
    monkeypatch.setattr(engine_runner, "engine_status",
                        lambda engine, **kwargs: engine_runner._parse_receipt(_json.loads(path.read_text()), plan))
    # The install holds every model a ready entry declares. The CPU harness keeps sparse placeholder files and answers
    # the byte-hash readers from the declarations, as the pod's stand-in does (real weights are Plan 34's).
    from astrid.core.generation import model_downloader, model_root
    from astrid.core.generation.asset_gaps import declared_model_specs
    sizes = {}
    declared = [(m["path"].split("/models/", 1)[-1], m["bytes"], m["sha256"].removeprefix("sha256:"))
                for engine in dict.fromkeys(("comfyui", *entries.runpod_engines()))
                for m in provider._receipt_for(engine)["models"]]
    for entry in entries.ready_entries():
        declared += [(m.destination, m.bytes, m.sha256) for m in
                     declared_model_specs(entry.get("model_assets", []), require_complete=False)]
    for destination, size, sha in declared:
        file = Path(root) / "models" / destination.removeprefix("models/")
        file.parent.mkdir(parents=True, exist_ok=True)
        with file.open("wb") as handle:
            handle.truncate(size)  # sparse: the declared size, no bytes
        sizes[str(file)] = (size, sha)
    real_digest, real_sha = model_downloader._digest, model_root._file_sha256
    monkeypatch.setattr(model_downloader, "_digest", lambda path, **kw: sizes.get(str(path)) or real_digest(path, **kw))
    monkeypatch.setattr(model_root, "_file_sha256", lambda path: "sha256:" + sizes[str(path)][1] if str(path) in sizes else real_sha(path))
    port = engine_recipe.load_recipe("comfyui", DEFAULT_PLATFORM).start.port
    with _attached_comfy(provider.comfy, port) as url:
        yield url


@contextlib.contextmanager
def _drive(slot, tmp_path, monkeypatch, real_runtime, tuple_sources, extra_classes=(), call=None,
           machine_identity=False, target_kind="runpod", h3_output=False):
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
    claim_path = target_selection.canonical_claim_path("cpu-fixture")
    monkeypatch.setenv("ASTRID_MACHINE_POLL_SECONDS", "0.05")
    provider = SlotPod(claim_path, tmp_path / "attempts", _slot_classes(slot) | set(extra_classes))
    provider.comfy.h3_output = h3_output
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
    # Preferences live in the Runtime's generation settings document; seed them through the same
    # connection the facade reads (describe/generate are handed this client).
    target_selection.save_runpod_preferences(preferences, client=client)
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
                quote = GenerationFacade().describe(placement="runpod", client=client)
                controls = dict(placement="runpod", client=client, project=project_id, consent=("paid",), max_usd=1.5,
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
                controls = dict(placement=url, client=client, project=project_id, wait=True)
            else:
                stack.enter_context(_managed_local_engine(provider, tmp_path, monkeypatch))
                controls = dict(placement="managed", client=client, project=project_id, wait=True)
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
def test_default_slot_through_the_runpod_machine(slot, tmp_path, monkeypatch, real_runtime, tuple_sources):
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
        _assert_ran_on_the_machine(provider)
        task = client.tasks.show(result.task_id)
        assert task.ok and task.data["state"] == "succeeded"
        if slot in {("audio", "sfx"), ("image", "edit")}:
            # Media inputs ship inside the job directory; nothing is uploaded to a pod ComfyUI.
            assert "managed_assets" in provider.generations[0]["execution"], provider.generations[0]["execution"]


H3_REQUEST = Path(__file__).resolve().parents[1] / "fixtures" / "h3_cpu" / "request.v2.json"


def test_h3_av_through_the_runpod_machine(tmp_path, monkeypatch, real_runtime, tuple_sources):
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
    h3_classes |= _h3_built_classes(H3_REQUEST, json.loads(assets.read_text()), tmp_path)
    def call(facade, controls, tmp):
        return facade.video(entry="h3", request=str(H3_REQUEST), asset_map=str(assets), **controls)
    with _drive(("video", "h3"), tmp_path, monkeypatch, real_runtime, tuple_sources,
                extra_classes=h3_classes, call=call, machine_identity=True) as (client, project_id, provider, result):
        assert result.ok, json.dumps(result.to_dict(), default=str)[:4000]
        delivered = deliver_generation(result, client=client, out=tmp_path / "delivered", project=project_id)
        assert delivered, "nothing was delivered"
        _assert_ran_on_the_machine(provider)
        assert client.tasks.show(result.task_id).data["state"] == "succeeded"


def _assert_ran_on_the_machine(provider):
    """Plan 32 T5: the job ran on the pod; the controller never reached a pod engine."""
    assert provider.generations, "no generation job ran on the machine"
    assert not any("/engines/" in str(event) or "/engine-proxy" in str(event) for event in provider.events)
    assert not provider.comfy.prompts and not provider.comfy.views


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


def test_local_managed_voice(tmp_path, monkeypatch, tuple_sources):
    """The managed local target (as in the installed Qwen child path) for the voice default's real template."""
    import http.server
    import subprocess
    import sys
    pod = SlotPod(tmp_path / "claim.json", tmp_path / "attempts", _slot_classes(("audio", "tts")))
    # The managed target's inventory is the stand-in pod's install receipt; the controller keeps no model files.
    pod.receipts["comfyui"] = pod._receipt_for("comfyui")
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
    # T1 characterization: flips in T4 (the managed local run path)
    assert result.returncode == 1 and "workflow-local model is not present locally: config.json" in result.stdout + result.stderr
    return


# One case per generation capability, each on every target kind. The runpod cells
# repeat the slot and h3 cases above through the same drive; the local-managed and
# attached cells are the questions T3/T4 answer. The i2v default is absent from
# SLOT_DEFAULTS and reaches RunPod only through its own engine lock.
CAPABILITIES = [("image", "t2i"), ("image", "edit"), ("audio", "tts"), ("audio", "music"),
                ("audio", "sfx"), ("video", "i2v"), ("video", "h3")]
# T1 characterization. Each red cell asserts today's refusal: (exception type or "result", key message, task that flips it).
# T3 Placement and attached routing, T4 local run path, T5 RunPod as a machine, T6 one generate, T6b h3_av.
_RED = {
    # The managed local target now exists (T4f); these three still refuse, for reasons it cannot fix on CPU.
    (("audio", "tts"), "local-managed"): ("CapabilityRuntimeError", "workflow-local model verification failed for config.json: sha256 mismatch", "Plan 34"),
    (("audio", "sfx"), "local-managed"): ("GenerationReadinessError", "target is missing checkpoint 'hunyuanvideo_foley_xl.pth'", "Plan 34"),
    (("video", "i2v"), "local-managed"): ("GenerationReadinessError", "No ready local video model supports mode 'i2v' on the selected target", "T3"),
    (("audio", "tts"), "attached"): ("GenerationReadinessError", "workflow-local model is not present locally: config.json", "T3"),
    (("audio", "sfx"), "attached"): ("GenerationReadinessError", "target is missing checkpoint 'hunyuanvideo_foley_xl.pth'", "T3"),
    (("video", "i2v"), "attached"): ("GenerationReadinessError", "No ready local video model supports mode 'i2v' on the selected target", "T3"),
    (("video", "i2v"), "runpod"): ("GenerationReadinessError", "No RunPod engine lock covers the models of 'video/ltx2_3_i2v'", "T5"),
}


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
    [pytest.param(c, t, id=f"{'/'.join(c)}-{t}",
                  # CPU cannot verify the 1.9GB pinned turbo LoRA hash h3's validate step re-hashes (Plan 34; as the TTS local cell).
                  marks=[pytest.mark.gpu] if (c, t) == (("video", "h3"), "local-managed") else [])
     for t in TARGET_KINDS for c in CAPABILITIES])
def test_capability_reaches_generation_on_each_target(capability, target_kind, tmp_path, monkeypatch,
                                                      real_runtime, tuple_sources):
    composed = set()
    if capability == ("video", "h3"):
        # The job directory (and the candidate composed in it) is removed when the run ends; hash it first.
        import hashlib, shutil
        real_rmtree = shutil.rmtree
        def hashing_rmtree(path, *args, **kwargs):
            if Path(path).name.startswith("astrid-job-"):
                composed.update(hashlib.sha256(f.read_bytes()).hexdigest()
                                for f in Path(path).rglob("*.composed.*") if "compose" in f.parts)
            return real_rmtree(path, *args, **kwargs)
        monkeypatch.setattr(shutil, "rmtree", hashing_rmtree)
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
        h3_classes |= _h3_built_classes(H3_REQUEST, json.loads(assets.read_text()), tmp_path)
        def call(facade, controls, tmp):
            return facade.video(entry="h3", request=str(H3_REQUEST), asset_map=str(assets), **controls)
        drive = dict(extra_classes=h3_classes, call=call, machine_identity=True, h3_output=True)
    else:
        drive = dict(call=_capability_call(capability))
    red = _RED.get((capability, target_kind))
    if red and red[0] != "result":
        # T1 characterization: flips in T<n>, the task named for this cell in _RED
        with pytest.raises(Exception) as caught:
            with _drive(capability, tmp_path, monkeypatch, real_runtime, tuple_sources, target_kind=target_kind, **drive):
                pass
        assert type(caught.value).__name__ == red[0] and red[1] in str(caught.value), repr(caught.value)[:2000]
        return
    with _drive(capability, tmp_path, monkeypatch, real_runtime, tuple_sources, target_kind=target_kind,
                **drive) as (client, project_id, provider, result):
        if red:
            # T1 characterization: flips in T<n>, the task named for this cell in _RED
            assert not result.ok and red[1] in json.dumps(result.to_dict(), default=str)
            return
        assert result.ok, json.dumps(result.to_dict(), default=str)[:4000]
        delivered = deliver_generation(result, client=client, out=tmp_path / "delivered", project=project_id)
        assert delivered, "nothing was delivered"
        if capability == ("video", "h3") and target_kind != "runpod":
            # The composed candidate (vibecomfy.run's after_outputs) is what is delivered, not the raw generation.
            import hashlib
            files = [f for f in (tmp_path / "delivered").rglob("*") if f.is_file() and f.suffix == ".mp4"]
            assert len(files) == 1, files
            delivered_sha = hashlib.sha256(files[0].read_bytes()).hexdigest()
            raw = {hashlib.sha256(data).hexdigest() for data, _mime in provider.comfy.outputs.values()}
            assert composed, "no composed candidate was observed in the job directory"
            assert delivered_sha in composed, (delivered_sha, composed)
            assert delivered_sha not in raw, "the raw generated output was delivered"
        if target_kind == "runpod":
            _assert_ran_on_the_machine(provider)
        else:
            assert provider.comfy.prompts and not provider.comfy.rejections
            assert provider.comfy.views, "no output was retrieved over /view"
        assert client.tasks.show(result.task_id).data["state"] == "succeeded"


def test_h3_facade_passes_its_after_outputs_job_and_the_hook_composes_the_candidate(tmp_path, monkeypatch, real_runtime, tuple_sources):
    """Facade level: generate --entry h3 builds, hands vibecomfy.run its after_outputs job, and that job's hook
    turns the generated video into the composed and verified candidate (not the raw output)."""
    import hashlib
    import importlib
    request = json.loads(H3_REQUEST.read_text())
    source = _video_source_av(tmp_path / "source.mp4")
    assets = {request["media"][0]["asset"]["id"]: str(source), "source": str(source)}
    assets_path = tmp_path / "assets.json"
    assets_path.write_text(json.dumps(assets))
    h3_classes = set(json.loads((Path(__file__).resolve().parents[1]
                                 / "fixtures/h3_cpu/object_info_h3_runpod_graph_closure.json").read_text()))
    h3_classes |= _h3_built_classes(H3_REQUEST, assets, tmp_path)
    captured = {}

    class Stop(Exception):
        pass

    def workflow(self, **kwargs):
        # Where vibecomfy.run would run: the job directory build wrote still exists during the call.
        captured.update(kwargs)
        spec = json.loads(kwargs["after_outputs_job"])
        assert Path(spec["job_dir"], "h3", "build-state.json").is_file()
        run = importlib.import_module("astrid.packs.vibecomfy.actions.run.run")
        generated = _video_source_av(tmp_path / "generated.mp4", seconds=4.5, size="1024x576")
        captured["generated"] = generated
        captured["composed"] = Path(run._after_outputs(spec)([generated])[0])
        captured["composed_bytes"] = captured["composed"].read_bytes()
        raise Stop

    monkeypatch.setattr(GenerationFacade, "workflow", workflow)

    def call(facade, controls, tmp):
        try:
            facade.video(entry="h3", request=str(H3_REQUEST), asset_map=str(assets_path), **controls)
        except Stop:
            return None
    with _drive(("video", "h3"), tmp_path, monkeypatch, real_runtime, tuple_sources, extra_classes=h3_classes,
                call=call, machine_identity=True, target_kind="attached", h3_output=True):
        pass
    spec = json.loads(captured["after_outputs_job"])
    assert spec["entry"] == "h3" and spec["files"]["asset_map"] == assets
    generated = captured["generated"]
    assert hashlib.sha256(captured["composed_bytes"]).hexdigest() != hashlib.sha256(generated.read_bytes()).hexdigest()
    assert captured["composed_bytes"]


def _video_source_av(path, seconds=2.4, size="1024x576"):
    import subprocess
    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", f"color=c=blue:s={size}:r=24",
               "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}"]
    if seconds == 2.4:
        command += ["-frames:v", "57"]
    subprocess.run([*command, "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path)],
                   check=True, timeout=120)
    return path
