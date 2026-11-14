"""A person's own saved ComfyUI UI workflow converts against their attached server.

The fake ComfyUI serves a custom node class that only its ``/object_info`` declares.
Requests are recorded so the read-only ``describe`` and dry-run paths can be shown to
send nothing but GETs.
"""
from __future__ import annotations

import copy
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from astrid.core.generation import cli

CUSTOM_SEED = ["INT", {"default": 0, "min": 0, "max": 2**32, "control_after_generate": True}]

# Stock ComfyUI shape: the control option sits on the INT widget, and the saved UI
# keeps its value ("fixed") as the widget value that follows the seed.
OBJECT_INFO_STOCK = {
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

def _ui_workflow(sampler_widgets: list) -> dict:
    """A saved ComfyUI UI workflow; the sampler widgets are the seed shape under test."""
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
             "widgets_values": list(sampler_widgets)},
            {"id": 3, "type": "SaveImage", "pos": [700, 0], "size": [300, 100], "flags": {},
             "order": 2, "mode": 0,
             "inputs": [{"name": "images", "type": "IMAGE", "link": 2}],
             "outputs": [], "properties": {"Node name for S&R": "SaveImage"},
             "widgets_values": ["astrid"]},
        ],
        "links": [[1, 1, 0, 2, 0, "MODEL"], [2, 2, 0, 3, 0, "IMAGE"]],
    }


# The two seed shapes a saved UI workflow comes in: the stock frontend keeps the
# control_after_generate value ("fixed") after seed; older exports have only seed.
SEED_SHAPES = {"control-widget": [7, "fixed", 4, 1.0], "seed-only": [7, 4, 1.0]}


class _Fake:
    def __init__(self, object_info: dict, workflow: dict):
        self.object_info = object_info
        self.workflow = workflow
        self.requests: list[tuple[str, str]] = []
        self.prompts: list[dict] = []
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
                fake.requests.append(("GET", self.path))
                path = urlsplit(self.path).path
                if path == "/system_stats":
                    self._send({"system": {"comfyui_version": "0.3.32", "python_version": "3.13.0",
                                           "embedded_python": False, "argv": ["main.py", "--cpu"]},
                                "devices": [{"name": "cpu", "type": "cpu", "vram_total": 0, "vram_free": 0}]})
                elif path == "/object_info":
                    self._send(fake.object_info)
                elif path == "/models":
                    self._send(["checkpoints"])
                elif path == "/models/checkpoints":
                    self._send(["tiny-cpu-test.safetensors"])
                elif path == "/queue":
                    self._send({"queue_running": [], "queue_pending": []})
                else:
                    self._send({"error": "not found in fake", "path": path}, 404)

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
                fake.requests.append(("POST", self.path))
                if self.path == "/prompt":
                    fake.prompts.append(body)
                    self._send({"prompt_id": f"fake-{len(fake.prompts)}", "number": 1, "node_errors": {}})
                else:
                    self._send({"error": "not found in fake"}, 404)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    def methods(self) -> set[str]:
        return {method for method, _ in self.requests}

    def paths(self) -> set[str]:
        return {path for _, path in self.requests}


@pytest.fixture(params=sorted(SEED_SHAPES))
def fake_comfy(request):
    fake = _Fake(OBJECT_INFO_STOCK, _ui_workflow(SEED_SHAPES[request.param])).start()
    yield fake
    fake.stop()


@pytest.fixture
def saved_workflow(tmp_path, fake_comfy) -> Path:
    path = tmp_path / "my_portrait_custom.json"
    path.write_text(json.dumps(fake_comfy.workflow), encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def _no_real_runtime(monkeypatch):
    monkeypatch.setattr(cli, "_open_client", lambda: pytest.fail("the test must not open the Runtime"))


def test_describe_with_attached_placement_lists_custom_node_inputs(fake_comfy, saved_workflow, capsys):
    assert cli.main(["describe", str(saved_workflow), "--placement", fake_comfy.url, "--json"]) == 0
    description = json.loads(capsys.readouterr().out)

    assert "/object_info" in fake_comfy.paths()
    names = {item["name"] for item in description["inputs"]}
    assert "seed" in names, names  # only the server's schema declares the custom node's seed input
    assert fake_comfy.methods() == {"GET"}


def test_workflow_dry_run_admits_saved_ui_workflow(fake_comfy, saved_workflow, capsys):
    code = cli.main(["workflow", str(saved_workflow), "--placement", fake_comfy.url,
                     "--dry-run", "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert code == 0, payload
    assert payload.get("status", "ready") == "ready", payload
    assert "/object_info" in fake_comfy.paths()
    assert fake_comfy.methods() == {"GET"}


def test_queued_prompt_keeps_saved_seed_and_custom_node(fake_comfy, saved_workflow, tmp_path):
    from astrid.packs.vibecomfy import production_engine

    # A real run queues one prompt. The fake has no /history, so the run may end
    # with an error after the POST; the queued body is what this test checks.
    try:
        production_engine.run_workflow_path(saved_workflow, tmp_path / "out", task_identity="task-p4-4",
                                            comfy_url=fake_comfy.url)
    except Exception:  # noqa: BLE001 - only the request that reached the fake matters here
        pass

    assert len(fake_comfy.prompts) == 1, fake_comfy.requests
    prompt = fake_comfy.prompts[0]["prompt"]
    custom = [node for node in prompt.values() if node["class_type"] == "AstridCustomSampler"]
    assert len(custom) == 1, prompt
    assert custom[0]["inputs"]["seed"] == 7
    assert custom[0]["inputs"]["steps"] == 4
    assert [path for method, path in fake_comfy.requests if method == "POST"][:1] == ["/prompt"]
    assert {method for method, _ in fake_comfy.requests} <= {"GET", "POST"}
