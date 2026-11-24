"""Shared CPU-only ComfyUI target fixtures for generation tests."""
from __future__ import annotations

import copy
import hashlib
import io
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from PIL import Image


def _add_workflow_classes(schema, workflow):
    from vibecomfy.porting.object_info import get_class

    for node in workflow.runtime_nodes().values():
        definition = get_class(node.class_type)
        assert definition is not None
        schema.setdefault(node.class_type, {
            "input": copy.deepcopy(definition["inputs"]),
            "input_order": copy.deepcopy(definition["input_order"]),
            "output": [output["type"] for output in definition["outputs"]],
            "output_name": [output["name"] for output in definition["outputs"]],
            "output_is_list": [output["is_list"] for output in definition["outputs"]],
            "output_node": node.class_type == "SaveImage",
        })


def _target_schema(*entry_ids):
    """Expose the shipped default image graphs with exact installed role names.

    The default image entry owns its failure (no fallback to another model), so
    the target must serve the preferred default's assets as well as z-image.
    """
    from astrid.core.model_catalog.registry import ModelRegistry
    from vibecomfy.registry.ready import workflow_from_ready

    schema = {}
    for entry_id in entry_ids or ("z-image", "qwen-image-2.1-turbo"):
        spec = ModelRegistry.load_default().get(entry_id).modes["t2i"].backends["local"]
        workflow = workflow_from_ready(spec.template)
        _add_workflow_classes(schema, workflow)
        for role in spec.model_roles.values():
            choices = schema[role.node_class]["input"]["required"].get(role.input)
            names = list(choices[0]) if choices and isinstance(choices[0], list) else []
            schema[role.node_class]["input"]["required"][role.input] = [list(dict.fromkeys([*names, role.names[0]]))]
    return schema


@pytest.fixture
def ready_workflow_schema():
    """Expose the actual ready graph's classes and selected loader values."""
    from vibecomfy.entries import draft_entry

    def build(workflow):
        schema = {}
        _add_workflow_classes(schema, workflow)
        for need in draft_entry(workflow, "ready-schema")["needs"]:
            class_type, field, value = need.get("class_type"), need.get("field"), need.get("value")
            if class_type not in schema or not isinstance(value, str):
                continue
            required = schema[class_type]["input"]["required"]
            if field not in required:
                continue
            spec = required[field]
            choices = list(spec[0]) if spec and isinstance(spec[0], list) else []
            required[field] = [list(dict.fromkeys([*choices, value])), *spec[1:]]
        return schema

    return build


@pytest.fixture
def loopback_comfy(tmp_path):
    state = SimpleNamespace(schema=_target_schema(), calls=[], prompts=[], histories={})
    image = io.BytesIO()
    Image.new("RGB", (16, 16), (24, 48, 96)).save(image, format="PNG")
    state.image = image.getvalue()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def send(self, data, media_type="application/json"):
            if media_type == "application/json":
                data = json.dumps(data).encode()
            self.send_response(200)
            self.send_header("Content-Type", media_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            state.calls.append(("GET", self.path, None))
            if self.path == "/object_info":
                self.send(state.schema)
            elif self.path == "/system_stats":
                self.send({"system": {"comfyui_version": "0.36.0"}, "devices": []})
            elif self.path.startswith("/history/"):
                prompt_id = self.path.rsplit("/", 1)[1]
                self.send({prompt_id: state.histories[prompt_id]})
            elif self.path == "/queue":
                self.send({"queue_running": [], "queue_pending": []})
            elif self.path.startswith("/view?"):
                descriptor = parse_qs(urlsplit(self.path).query)
                assert descriptor["filename"] == [state.filename]
                assert descriptor["type"] == ["output"]
                self.send(state.image, "image/png")
            else:
                self.send_error(404)

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            state.calls.append(("POST", self.path, body))
            # Repeated deletion of this fixture's accepted prompt is task
            # containment, not ownership of the attached server lifecycle.
            if self.path == "/queue" and any(
                body == {"delete": [prompt_id]} for prompt_id in state.histories
            ):
                self.send({})
                return
            if self.path != "/prompt":
                self.send_error(400, "attached fixture rejects lifecycle or unowned controls")
                return
            state.prompts.append(body["prompt"])
            prompt_id = f"g24-{len(state.prompts)}"
            save_id, save = next((key, node) for key, node in body["prompt"].items()
                                 if node["class_type"] == "SaveImage")
            state.filename = save["inputs"]["filename_prefix"].rsplit("/", 1)[-1] + "_00001_.png"
            state.histories[prompt_id] = {
                "outputs": {save_id: {"images": [{"filename": state.filename, "subfolder": "", "type": "output"}]}},
                "status": {"completed": True, "status_str": "success", "messages": []},
            }
            self.send({"prompt_id": prompt_id, "number": len(state.prompts), "node_errors": {}})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state.url = f"http://127.0.0.1:{server.server_port}"
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()
        (tmp_path / "fake-comfy-http.json").write_text(json.dumps({
            "url": state.url, "calls": state.calls, "prompts": state.prompts,
            "object_info": state.schema,
            "output_sha256": hashlib.sha256(state.image).hexdigest(),
            "output_bytes": len(state.image),
        }, indent=2))
