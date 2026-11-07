"""Real Runtime/host/attached-runner composition with fake provider and engine I/O."""
from __future__ import annotations

import http.client
import json
import os
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit
from types import SimpleNamespace

import pytest

from astrid.core.execution.generic_host import GenericPackHost, RuntimeProtocolClient
from astrid.core.generation.delivery import deliver_generation
from astrid.packs.runpod import target_selection
from astrid.sdk import AstridClient
from astrid.sdk.generation import GenerationFacade
from astrid.sdk.remote import RemoteAstridClient
from astrid.sdk.workspace_client import WorkspaceClient
from tests.packs.runpod.test_generation_preparation import retained, small_lease, lease_selection_case, sources
from tests.packs.vibecomfy.test_local_server_run import comfy


pytestmark = pytest.mark.integration
_TUPLE_SOURCES = pytest.StashKey[Path]()


@pytest.fixture(scope="session")
def tuple_sources(tmp_path_factory, request):
    """One clean shared-object source tuple for the real remote bootstrap gate."""
    import shutil
    # Imported fixture aliases share one session-owned, immutable source tuple.
    # Each consumer still runs the real source pin/cleanliness/archive gates.
    if _TUPLE_SOURCES in request.session.stash:
        yield request.session.stash[_TUPLE_SOURCES]
        return
    root = tmp_path_factory.mktemp("retained-bootstrap")
    selected = {
        "runpod-python": Path(os.environ["ASTRID_TEST_RUNPOD_CHECKOUT"]),
        "astrid-runtime": Path(os.environ["ASTRID_RUNTIME_CHECKOUT"]),
        "runpod-lifecycle": Path(os.environ["ASTRID_TEST_LIFECYCLE_CHECKOUT"]),
        "astrid-agent": Path(__file__).resolve().parents[2],
        "VibeComfy": Path(os.environ["ASTRID_VIBECOMFY_CHECKOUT"]),
    }
    try:
        for name, source in selected.items():
            destination = root / "src" / name
            subprocess.run(["git", "clone", "--quiet", "--shared", str(source), str(destination)], check=True)
            assert subprocess.check_output(["git", "-C", str(destination), "rev-parse", "HEAD"]) == subprocess.check_output(
                ["git", "-C", str(source), "rev-parse", "HEAD"])
        request.session.stash[_TUPLE_SOURCES] = root
        yield root
    finally:
        shutil.rmtree(root)


@pytest.fixture
def retained_host(retained, tmp_path, monkeypatch, real_runtime, tuple_sources):
    """Real SDK/Runtime/host/owner/bridge; doubles only at paid OS/inference I/O.

    Serving is explicit so admission-only tests can prove the client returns
    before any host acquires a lease. No SDK method or host seam is replaced.
    """
    import hashlib
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from PIL import Image
    from runpod_lifecycle import https
    from astrid.packs.runpod import remote_placement
    from vibecomfy.workflow import VibeWorkflow, WorkflowSource, VibeOutput
    from vibecomfy.workflow_bundle import emit_bundle

    c = retained
    c.host.close()
    monkeypatch.setattr(remote_placement, "subprocess", subprocess)
    monkeypatch.setattr(target_selection, "resolve_runtime_data_root", lambda: tmp_path / "machine")
    monkeypatch.setattr(target_selection, "read_runpod_preferences", lambda: c.prefs)
    monkeypatch.setenv("ASTRID_RUNPOD_SOURCE_ROOT", str(tuple_sources))
    monkeypatch.setenv("VIBECOMFY_ENTRIES_DIR", str(tmp_path / "entries"))
    c.pod.marker = f"{remote_placement.BOOTSTRAP_ROOT}/{remote_placement.source_digest(tuple_sources)}/ok"
    c.context.deadline = time.monotonic() + 600
    schema = {name: {"input": {"required": {"filename_prefix": ["STRING", {"default": "proof"}]}},
                     "output": [], "output_node": True} for name in ("SaveImage", "VHS_VideoCombine")}
    schema.update({name: {"input": {"required": {}}, "output": [], "output_node": False}
                   for name in ("UNETLoader", "VAELoader")})
    paths = {}
    for kind, sink in (("image", "SaveImage"), ("video", "VHS_VideoCombine")):
        folder = tmp_path / "entries" / ("retained-" + kind)
        folder.mkdir(parents=True)
        workflow = VibeWorkflow("retained-" + kind, WorkflowSource("retained-" + kind))
        workflow.add_node(sink, _id="1", uid="sink", filename_prefix="proof")
        workflow.register_input("filename_prefix", "1", "filename_prefix", "proof", type="STRING")
        workflow.outputs = [VibeOutput("1", sink, name=kind, artifact_kind=kind,
            mime_type="image/png" if kind == "image" else "video/mp4")]
        (folder / "source.json").write_text(json.dumps({"nodes": [
            {"id": 1, "type": sink, "inputs": [], "widgets_values": ["proof"]}], "links": []}))
        emit_bundle(workflow, folder / "workflow.py", {"operation": "authored"})
        (folder / "entry.json").write_text(json.dumps({"id": "retained-" + kind, "kind": kind,
            "modes": ["t2i" if kind == "image" else "t2v"], "bundle": "workflow.py",
            "needs": [], "binds": {}, "defaults": {}}))
        paths[kind] = folder / "workflow.py"
    output = tmp_path / "inference"
    output.mkdir()
    Image.new("RGB", (16, 16), (10, 20, 30)).save(output / "proof.png")
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=blue:s=32x32:r=8",
                    "-t", "1", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(output / "proof.mp4")], check=True)
    state = SimpleNamespace(prompts=[], events=[], before_relay=None, histories={}, schema=schema)

    class Inference(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def send(self, value):
            data = json.dumps(value).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        def do_GET(self):
            if self.path == "/object_info": self.send(schema)
            elif self.path.startswith("/history/"):
                key = self.path.rsplit("/", 1)[1]
                self.send({key: state.histories[key]})
            elif self.path.startswith("/view?"):
                from urllib.parse import parse_qs
                data = (output / parse_qs(urlsplit(self.path).query)["filename"][0]).read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "video/mp4" if "proof.mp4" in self.path else "image/png")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            elif self.path == "/queue": self.send({"queue_running": [], "queue_pending": []})
            else: self.send({})
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            if self.path == "/queue":
                assert set(body) == {"delete"} and body["delete"]
                self.send({})
                return
            if self.path == "/interrupt":
                assert set(body) == {"prompt_id"}
                self.send({})
                return
            assert self.path == "/prompt", self.path
            state.prompts.append(body["prompt"])
            video = any(row["class_type"] == "VHS_VideoCombine" for row in body["prompt"].values())
            sink = next(key for key, row in body["prompt"].items()
                        if row["class_type"] in {"SaveImage", "VHS_VideoCombine"})
            key = "proof-" + str(len(state.prompts))
            state.histories[key] = {"outputs": {sink: {"gifs" if video else "images": [
                {"filename": "proof.mp4" if video else "proof.png", "subfolder": "", "type": "output"}]}},
                "status": {"status_str": "success", "completed": True, "messages": []}}
            self.send({"prompt_id": key, "number": len(state.prompts), "node_errors": {}})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Inference)
    inference = threading.Thread(target=server.serve_forever, daemon=True)
    inference.start()
    def enable(self, endpoint, port):
        state.events.append(("enable", endpoint, port))
    def relay(self, handler, endpoint):
        binding = c.read()["lease"]["endpoint_binding"]
        assert binding["state"] in {"bound", "ready"}
        assert binding["loopback_url"].startswith("http://127.0.0.1:")
        state.events.append(("request", handler.command, handler.path, binding["loopback_url"]))
        if state.before_relay is not None:
            state.before_relay(handler)
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=15)
        size = int(handler.headers.get("Content-Length", "0"))
        body = handler.rfile.read(size) if size else None
        connection.request(handler.command, handler.path, body=body,
                           headers={"Content-Type": handler.headers.get("Content-Type", "application/json")})
        response = connection.getresponse()
        data = response.read()
        handler.send_response(response.status)
        handler.send_header("Content-Type", response.getheader("Content-Type", "application/json"))
        handler.send_header("Content-Length", str(len(data)))
        handler.end_headers()
        handler.wfile.write(data)
        connection.close()
    monkeypatch.setattr(https.HttpsPodTransport, "enable_engine_proxy", enable, raising=False)
    monkeypatch.setattr(https.HttpsPodTransport, "relay_engine", relay, raising=False)
    daemon = real_runtime()
    owner = WorkspaceClient(daemon.endpoint, daemon.token)
    client = AstridClient(remote=RemoteAstridClient(owner))
    project = client.projects.create(name="Host regression", slug="host-regression", idempotency_key="project")
    assert project.ok, project.error
    project_id = project.data["project_id"]
    owner.select_project(project_id, idempotency_key="select-project")
    packs = Path(__file__).resolve().parents[2] / "astrid" / "packs"
    host = GenericPackHost(pack_roots=[packs / name for name in ("vibecomfy", "runpod", "h3_av")],
        client=RuntimeProtocolClient(daemon.endpoint, daemon.worker_token),
        credential_source={"FAKE_RUNPOD_CREDENTIAL": "fake-only"}, attempt_base=tmp_path / "attempts")
    host.discover()
    host.preflight()
    host.register()
    # Obtain the actual provider quote without serving generation yet. The
    # ordinary status-task path is exercised separately below and by journeys.
    selection = {"kind": "runpod", "engine": "comfyui", "preferences": c.prefs.as_mapping()}
    bindings = host._host_target_bindings("runpod")
    quote = bindings.operate(selection, "status", include_quote=True)
    paid = {"consent": ("paid",), "max_usd": 2.0,
            "deadline": (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(),
            "offer_digest": quote["offer_digest"], "preparation_seconds": 60,
            "execution_seconds": 60, "idle_seconds": 1800}
    worker = None
    def start():
        nonlocal worker
        assert worker is None
        worker = threading.Thread(target=host.run, kwargs={"poll_seconds": 0.02}, daemon=True)
        worker.start()
    def generate(kind="image", *, raw=False, **kwargs):
        args = {"client": client, "project": project_id, "target": "runpod", "wait": False, **paid, **kwargs}
        if raw:
            return GenerationFacade().workflow(workflow=paths[kind], timeout_seconds=90, poll_seconds=0.02, **args)
        return getattr(GenerationFacade(), kind)(entry="retained-" + kind, **args)
    def show(task_id):
        result = client.tasks.show(task_id)
        assert result.ok, result.error
        return result.data
    def follow(task_id):
        task = show(task_id)
        from astrid.sdk.invocation import observe_task_invocation
        return observe_task_invocation(client, capability_id=task.get("capability_id", task.get("capability")),
            capability_type="executor", native_kind="command", task_id=task_id, run_id=task.get("run_id"),
            wait=True, timeout_seconds=90, poll_seconds=0.02)
    def release():
        return GenerationFacade().release(engine="comfyui", target="runpod", client=client, wait=True)
    try:
        yield SimpleNamespace(c=c, client=client, host=host, bindings=bindings, paid=paid, selection=selection,
            project=project_id, paths=paths, output=output, state=state, tmp=tmp_path,
            start=start, generate=generate, show=show, follow=follow, release=release)
    finally:
        host._shutdown.set()
        if worker is not None:
            worker.join(timeout=35)
            assert not worker.is_alive(), "retained host did not drain"
        host.shutdown()
        daemon.stop()
        server.shutdown()
        server.server_close()
        inference.join()


@pytest.fixture(params=["image", "video"])
def host_route(retained_host, request):
    retained_host.kind = request.param
    return retained_host


def test_runtime_claim_binds_one_endpoint_and_delivers(retained, comfy, tmp_path, monkeypatch, real_runtime, tuple_sources):
    from runpod_lifecycle import https
    from astrid.packs.runpod import remote_placement

    c = retained
    c.host.close()
    # The fixture's fake provider and remote OS remain the only external edge.
    # Qualify the installation's actual source checks with test-owned shared
    # checkouts of this exact Agent, Runtime, Lifecycle, SDK and engine tuple.
    c.placement.source_root = tuple_sources
    monkeypatch.setattr(remote_placement, "subprocess", subprocess)
    c.pod.marker = (f"{remote_placement.BOOTSTRAP_ROOT}/"
                    f"{remote_placement.source_digest(c.placement.source_root)}/ok")
    c.context.deadline = time.monotonic() + 180
    monkeypatch.setattr(target_selection, "resolve_runtime_data_root", lambda: tmp_path / "machine")
    monkeypatch.setattr(target_selection, "read_runpod_preferences", lambda: c.prefs)
    monkeypatch.setenv("ASTRID_RUNPOD_SOURCE_ROOT", str(c.placement.source_root))
    # This synthetic node has no model-loader role; the route must not bypass
    # the ordinary ambiguity gate for a fictitious loader.
    del comfy.schema["SeedImage"]["input"]["required"]["ckpt_name"]
    index_path = tmp_path / "node_index.json"
    index = json.loads(index_path.read_text())
    del next(row for row in index if row["class_type"] == "SeedImage")["inputs"]["ckpt_name"]
    index_path.write_text(json.dumps(index))
    workflow = json.loads(comfy.workflow.read_text())
    workflow["nodes"][0]["widgets_values"] = [42]
    comfy.workflow.write_text(json.dumps(workflow))
    for name in ("UNETLoader", "VAELoader"):
        comfy.schema[name] = {"input": {"required": {}}, "output": [], "output_node": False}
    bridge_events = []

    def enable(self, endpoint, port):
        bridge_events.append(("enable", endpoint, port))

    def relay(self, handler, endpoint):
        binding = c.read()["lease"]["endpoint_binding"]
        assert binding["state"] in {"bound", "ready"}
        bridge_events.append(("request", handler.command, handler.path, binding["loopback_url"]))
        target = urlsplit(comfy.url)
        connection = http.client.HTTPConnection(target.hostname, target.port, timeout=15)
        size = int(handler.headers.get("Content-Length", "0"))
        body = handler.rfile.read(size) if size else None
        headers = {"Content-Type": handler.headers.get("Content-Type", "application/json")}
        connection.request(handler.command, handler.path, body=body, headers=headers)
        response = connection.getresponse()
        data = response.read()
        handler.send_response(response.status)
        handler.send_header("Content-Type", response.getheader("Content-Type", "application/json"))
        handler.send_header("Content-Length", str(len(data)))
        handler.end_headers()
        handler.wfile.write(data)
        connection.close()

    monkeypatch.setattr(https.HttpsPodTransport, "enable_engine_proxy", enable, raising=False)
    monkeypatch.setattr(https.HttpsPodTransport, "relay_engine", relay, raising=False)
    daemon = real_runtime()
    host = None
    worker_thread = None
    errors = []
    try:
        owner = WorkspaceClient(daemon.endpoint, daemon.token)
        client = AstridClient(remote=RemoteAstridClient(owner))
        invoke = client.invoke_result
        def bounded_invoke(capability, **kwargs):
            kwargs.setdefault("timeout_seconds", 90)
            return invoke(capability, **kwargs)
        client.invoke_result = bounded_invoke
        project = client.projects.create(name="Retained endpoint", slug="retained-endpoint", idempotency_key="project")
        assert project.ok, project.error
        project_id = project.data["project_id"]
        owner.select_project(project_id, idempotency_key="select-project")
        pack_root = Path(__file__).resolve().parents[2] / "astrid" / "packs"
        host = GenericPackHost(
            pack_roots=[pack_root / "vibecomfy", pack_root / "runpod"],
            client=RuntimeProtocolClient(daemon.endpoint, daemon.worker_token),
            credential_source={"FAKE_RUNPOD_CREDENTIAL": "fake-only"},
            attempt_base=tmp_path / "attempts",
        )
        host.discover()
        host.preflight()
        for capability in ("vibecomfy.run", "runpod.status", "runpod.release"):
            assert host.capabilities[capability].ready, host.capabilities[capability].preflight
        host.register()

        def serve():
            try:
                host.run(poll_seconds=0.02, max_tasks=4)
            except BaseException as exc:
                errors.append(exc)

        worker_thread = threading.Thread(target=serve, daemon=True)
        worker_thread.start()
        quote = GenerationFacade().describe(target="runpod", client=client)
        retained_owner = host._target_bindings.provider.lease_owner
        deadline = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
        result = GenerationFacade().workflow(workflow=comfy.workflow, target="runpod", client=client,
            project=project_id, consent=("paid",), max_usd=1.0, deadline=deadline,
            offer_digest=quote["offer_digest"], preparation_seconds=60, execution_seconds=60,
            idle_seconds=1800, wait=True, timeout_seconds=90, recovery_path=tmp_path / "recovery.json")
        delivered = deliver_generation(result, client=client, out=tmp_path / "delivered", project=project_id)
        assert len(delivered["image_paths"]) == 1 and comfy.prompts and c.creates == 1
        assert Path(delivered["image_paths"][0]).read_bytes() == (comfy.output / "astrid_1.png").read_bytes()
        resumed = GenerationFacade().workflow(workflow=comfy.workflow, target="runpod", client=client,
            project=project_id, consent=("paid",), max_usd=1.0, deadline=deadline,
            offer_digest=quote["offer_digest"], preparation_seconds=60, execution_seconds=60,
            idle_seconds=1800, wait=True, timeout_seconds=90, recovery_path=tmp_path / "recovery.json",
            resume=True)
        assert resumed.task_id == result.task_id and len(comfy.prompts) == 1 and c.creates == 1
        assert [event[0] for event in bridge_events].count("enable") == 1
        assert len({event[3] for event in bridge_events if event[0] == "request"}) == 1
        binding = c.read()["lease"]["endpoint_binding"]
        assert binding["state"] == "ready" and binding["invocation_id"]
        observed = client.tasks.show(result.task_id)
        assert observed.ok
        settled = observed.data["result"]
        assert settled.get("payload", settled)["target_binding"] == binding
        status = GenerationFacade().describe(target="runpod", client=client)
        assert status['pod_id'] == 'pod-1' and status['lease']['state'] == 'idle'
        assert host._target_bindings.provider.lease_owner is retained_owner
        assert c.read()['paid_limits']['idle_seconds'] == 1800
        released = GenerationFacade().release(engine="comfyui", target="runpod", client=client, wait=True)
        assert released.ok and c.read()["state"] == "cleanup_complete"
        assert host._target_bindings.provider.lease_owner is retained_owner and c.deletes == ['pod-1']
        assert c.read()['cleanup']['absence']['exact_get_status'] == 404
        worker_thread.join(timeout=30)
        assert not worker_thread.is_alive() and not errors
    finally:
        if host is not None:
            host.shutdown()
        if worker_thread is not None:
            worker_thread.join(timeout=5)
        daemon.stop()
