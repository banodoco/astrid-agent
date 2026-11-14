"""An attached ComfyUI that refuses Astrid's scoped cleanup says what failed.

The fake ComfyUI refuses ``POST /queue`` with HTTP 404, the shape of an older server
that lacks the scoped delete. The attached run only ever sends scoped requests, so the
request log must hold nothing but ``/prompt`` and ``{"delete": [id]}``. The refusal
message must name the endpoint and status and say the person's other jobs were untouched.
The managed-path message must keep its exact text.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from astrid.core.execution.generic_host import _ManagedLocalServerTaskAdapter, _ManagedTaskAdapter
from astrid.core.execution.managed_tool_session import (
    CapabilityDescriptor,
    ManagedToolSession,
    SessionBinding,
    SessionCapacityError,
)
from astrid.packs.vibecomfy import local_server_engine, production_engine

MANAGED_RELEASE_TEXT = "managed session release was not verified"
REFUSAL_PHRASE = "other queued jobs were not touched"

CUSTOM_SEED = ["INT", {"default": 0, "min": 0, "max": 2**32, "control_after_generate": True}]
OBJECT_INFO = {
    "CheckpointLoaderSimple": {
        "input": {"required": {"ckpt_name": [["tiny-cpu-test.safetensors"]]}},
        "output": ["MODEL", "CLIP", "VAE"], "output_name": ["MODEL", "CLIP", "VAE"],
        "output_node": False, "category": "loaders", "name": "CheckpointLoaderSimple"},
    "AstridCustomSampler": {
        "input": {"required": {
            "model": ["MODEL"],
            "seed": list(CUSTOM_SEED),
            "steps": ["INT", {"default": 20, "min": 1, "max": 10000}],
            "cfg": ["FLOAT", {"default": 8.0}],
        }},
        "output": ["LATENT"], "output_name": ["LATENT"], "output_node": False,
        "category": "astrid/custom", "name": "AstridCustomSampler"},
    "SaveImage": {
        "input": {"required": {"images": ["IMAGE"],
                               "filename_prefix": ["STRING", {"default": "ComfyUI"}]}},
        "output": [], "output_name": [], "output_node": True,
        "category": "image", "name": "SaveImage"},
}


def _ui_workflow() -> dict:
    return {
        "last_node_id": 3, "last_link_id": 2, "version": 0.4,
        "nodes": [
            {"id": 1, "type": "CheckpointLoaderSimple", "pos": [0, 0], "size": [300, 100], "flags": {},
             "order": 0, "mode": 0, "inputs": [],
             "outputs": [{"name": "MODEL", "type": "MODEL", "links": [1], "slot_index": 0},
                         {"name": "CLIP", "type": "CLIP", "links": None, "slot_index": 1},
                         {"name": "VAE", "type": "VAE", "links": None, "slot_index": 2}],
             "properties": {"Node name for S&R": "CheckpointLoaderSimple"},
             "widgets_values": ["tiny-cpu-test.safetensors"]},
            {"id": 2, "type": "AstridCustomSampler", "pos": [350, 0], "size": [300, 160], "flags": {},
             "order": 1, "mode": 0,
             "inputs": [{"name": "model", "type": "MODEL", "link": 1}],
             "outputs": [{"name": "LATENT", "type": "LATENT", "links": [2], "slot_index": 0}],
             "properties": {"Node name for S&R": "AstridCustomSampler"},
             "widgets_values": [7, "fixed", 4, 1.0]},
            {"id": 3, "type": "SaveImage", "pos": [700, 0], "size": [300, 100], "flags": {},
             "order": 2, "mode": 0,
             "inputs": [{"name": "images", "type": "IMAGE", "link": 2}],
             "outputs": [], "properties": {"Node name for S&R": "SaveImage"},
             "widgets_values": ["astrid"]},
        ],
        "links": [[1, 1, 0, 2, 0, "MODEL"], [2, 2, 0, 3, 0, "IMAGE"]],
    }


class _RefusingComfy:
    """Stock-looking ComfyUI whose prompts never complete and whose POST /queue is missing."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, str, object]] = []
        self.prompts = 0
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                return

            def _send(self, payload, status=200):
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                fake.requests.append(("GET", self.path, None))
                path = urlsplit(self.path).path
                if path == "/system_stats":
                    self._send({"system": {"comfyui_version": "0.3.32", "python_version": "3.13.0",
                                           "embedded_python": False, "argv": ["main.py", "--cpu"]},
                                "devices": [{"name": "cpu", "type": "cpu", "vram_total": 0, "vram_free": 0}]})
                elif path == "/object_info":
                    self._send(OBJECT_INFO)
                elif path == "/models":
                    self._send(["checkpoints"])
                elif path == "/models/checkpoints":
                    self._send(["tiny-cpu-test.safetensors"])
                elif path == "/queue":
                    self._send({"queue_running": [], "queue_pending": []})
                elif path.startswith("/history"):
                    self._send({})  # the prompt never completes
                else:
                    self._send({"error": "not found in fake", "path": path}, 404)

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                body = json.loads(raw) if raw else None
                fake.requests.append(("POST", self.path, body))
                if self.path == "/prompt":
                    fake.prompts += 1
                    self._send({"prompt_id": f"fake-{fake.prompts}", "number": 1, "node_errors": {}})
                else:
                    # Covers POST /queue: the older server has no scoped delete.
                    self._send({"error": "not found in fake", "path": self.path}, 404)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self) -> "_RefusingComfy":
        self.thread.start()
        return self

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def posts(self) -> list[tuple[str, object]]:
        return [(path, body) for method, path, body in self.requests if method == "POST"]


@pytest.fixture
def attached(tmp_path, monkeypatch):
    """An accepted-but-never-completed attached prompt, ready for cleanup."""
    monkeypatch.setattr(local_server_engine, "_LOCAL_PROMPT_TIMEOUT_SEC", 0.05)
    comfy = _RefusingComfy().start()
    workflow = tmp_path / "my_portrait_custom.json"
    workflow.write_text(json.dumps(_ui_workflow()), encoding="utf-8")
    user_output = tmp_path / "user-output"
    user_output.mkdir()
    out = tmp_path / "attempt" / "outputs"
    out.mkdir(parents=True)
    try:
        with pytest.raises(Exception):
            production_engine.run_workflow_result_path(
                workflow, out, task_identity="task-cleanup-refusal", comfy_url=comfy.url,
                comfy_output_dir=str(user_output), profile_id="local_server",
            )
        yield comfy, out
    finally:
        comfy.stop()


def _close_attached_session(comfy: _RefusingComfy, out: Path) -> BaseException:
    adapter = _ManagedLocalServerTaskAdapter(threading.Event(), url=comfy.url, destination=out)
    session = ManagedToolSession()
    binding = SessionBinding(session_id="s-cleanup", runtime_instance_id="r-cleanup",
                             process_birth_id="p-cleanup", endpoint=comfy.url,
                             source_digest="src", config_digest="cfg")
    session.open(capability=CapabilityDescriptor(capability_id="vibecomfy.run"),
                 binding=binding, adapter=adapter)
    with pytest.raises(SessionCapacityError) as info:
        session.close(reason="probe_cleanup_refusal")
    return info.value


def _chain(exc: BaseException):
    seen, depth = exc, 0
    while seen is not None and depth < 12:
        yield seen
        seen = seen.__cause__ or seen.__context__
        depth += 1


def test_attached_cleanup_sends_only_scoped_requests(attached):
    comfy, out = attached
    _close_attached_session(comfy, out)

    posts = comfy.posts()
    assert posts, comfy.requests
    assert posts[0][0] == "/prompt"
    deletes = [body for path, body in posts if path == "/queue"]
    assert deletes and all(body == {"delete": ["fake-1"]} for body in deletes), posts
    assert {path for path, _ in posts} <= {"/prompt", "/queue"}, posts
    assert not any(path in {"/interrupt", "/api/free"} for path, _ in posts), posts
    assert not any(isinstance(body, dict) and body.get("clear") for _, body in posts), posts


def test_attached_refusal_names_endpoint_status_and_untouched_jobs(attached):
    comfy, out = attached
    error = _close_attached_session(comfy, out)

    messages = [str(item) for item in _chain(error)]
    named = [text for text in messages if "/queue" in text and "404" in text and REFUSAL_PHRASE in text]
    assert named, messages


def test_managed_release_message_is_unchanged(tmp_path):
    # Managed path: a non-attached adapter whose release is not verified keeps the exact text.
    session = ManagedToolSession()
    adapter = _ManagedTaskAdapter(threading.Event(), process_census=lambda: True)
    binding = SessionBinding(session_id="s-managed", runtime_instance_id="r-managed",
                             process_birth_id="p-managed", endpoint="managed",
                             source_digest="src", config_digest="cfg")
    session.open(capability=CapabilityDescriptor(capability_id="managed.test"),
                 binding=binding, adapter=adapter)
    with pytest.raises(SessionCapacityError) as info:
        session.close(reason="probe_managed")
    assert str(info.value) == MANAGED_RELEASE_TEXT
    assert REFUSAL_PHRASE not in str(info.value)

    source = (Path(__file__).resolve().parents[3]
              / "astrid" / "core" / "execution" / "managed_tool_session.py").read_text(encoding="utf-8")
    assert f'raise RuntimeError("{MANAGED_RELEASE_TEXT}")' in source
    assert f'raise SessionCapacityError("{MANAGED_RELEASE_TEXT}") from exc' in source
