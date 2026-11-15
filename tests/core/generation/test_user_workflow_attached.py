"""A person's own saved ComfyUI UI workflow converts against their attached server.

The fake ComfyUI serves a custom node class that only its ``/object_info`` declares.
Requests are recorded so the read-only ``describe`` and dry-run paths can be shown to
send nothing but GETs.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from astrid.core.generation import cli

CUSTOM_SEED = ["INT", {"default": 0, "min": 0, "max": 2**32, "control_after_generate": True}]

# Only the stock ComfyUI shape is tested (as in ComfyUI's nodes.py KSampler): the control is an
# option on ``seed`` and the frontend saves its value right after the seed widget. The converted
# node holding a ``control_after_generate`` input is not something a stock server produces.
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


def _ui_workflow(control: str = "fixed") -> dict:
    """A saved ComfyUI UI workflow; the stock frontend stores seed 7 followed by its control value."""
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
             "widgets_values": [7, control, 4, 1.0]},
            {"id": 3, "type": "SaveImage", "pos": [700, 0], "size": [300, 100], "flags": {},
             "order": 2, "mode": 0,
             "inputs": [{"name": "images", "type": "IMAGE", "link": 2}],
             "outputs": [], "properties": {"Node name for S&R": "SaveImage"},
             "widgets_values": ["astrid"]},
        ],
        "links": [[1, 1, 0, 2, 0, "MODEL"], [2, 2, 0, 3, 0, "IMAGE"]],
    }


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


def _serve(tmp_path: Path, control: str = "fixed"):
    fake = _Fake(OBJECT_INFO_STOCK, _ui_workflow(control)).start()
    path = tmp_path / "my_portrait_custom.json"
    path.write_text(json.dumps(fake.workflow), encoding="utf-8")
    return fake, path


@pytest.fixture
def fake_comfy(tmp_path):
    fake, path = _serve(tmp_path)
    fake.saved = path
    yield fake
    fake.stop()


@pytest.fixture
def saved_workflow(fake_comfy) -> Path:
    return fake_comfy.saved


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


def _queue(fake_comfy, workflow_path: Path, out: Path, bindings=None) -> None:
    from astrid.packs.vibecomfy import production_engine

    # A real run queues one prompt. The fake has no /history, so the run may end
    # with an error after the POST; the queued body is what these tests check.
    try:
        production_engine.run_workflow_path(workflow_path, out, task_identity="task-p4-4",
                                            comfy_url=fake_comfy.url, workflow_input_bindings=bindings)
    except Exception:  # noqa: BLE001 - only the request that reached the fake matters here
        pass


def _custom_inputs(prompt: dict) -> dict:
    custom = [node for node in prompt.values() if node["class_type"] == "AstridCustomSampler"]
    assert len(custom) == 1, prompt
    return custom[0]["inputs"]


@pytest.mark.parametrize("control", ["fixed", "randomize"])
def test_queued_prompt_keeps_saved_seed_and_custom_node(control, tmp_path):
    # P4.4 rule: a fixed saved seed runs as saved; randomize keeps Astrid's fresh seed.
    fake, path = _serve(tmp_path, control)
    try:
        runs = 2 if control == "randomize" else 1
        for run in range(runs):
            _queue(fake, path, tmp_path / f"out{run}")
        assert len(fake.prompts) == runs, fake.requests
        assert {m for m, _ in fake.requests} <= {"GET", "POST"}
        assert [p for m, p in fake.requests if m == "POST"][:1] == ["/prompt"]
        inputs = _custom_inputs(fake.prompts[0]["prompt"])
        assert inputs["steps"] == 4 and inputs["cfg"] == 1.0
        seeds = [_custom_inputs(prompt["prompt"])["seed"] for prompt in fake.prompts]
        if control == "fixed":
            assert seeds == [7]
        else:
            assert all(type(seed) is int and 0 <= seed < 2**32 for seed in seeds), seeds
            assert seeds[0] != seeds[1], seeds
    finally:
        fake.stop()


def test_explicit_seed_input_wins_over_saved_fixed_seed(tmp_path):
    fake, path = _serve(tmp_path, "fixed")
    try:
        _queue(fake, path, tmp_path / "out", bindings={"seed": 99})
        assert _custom_inputs(fake.prompts[0]["prompt"])["seed"] == 99
    finally:
        fake.stop()


# The saved seed through the paths a real run takes: the plan the facade prepares (entries.prepare),
# the explicit public input, and `--as-entry --dry-run` (P4.9.4).

def _prepared_seed(fake, path, values=None) -> int:
    from types import SimpleNamespace

    from astrid.core.generation import entries
    _, plan, _ = entries.prepare(path, target_client=SimpleNamespace(target=SimpleNamespace(url=fake.url)),
                                 mode="workflow", binds=None, values=values or {})
    return plan["bindings"]["seed"]


@pytest.mark.parametrize("control", ["fixed", "randomize", "increment", "decrement"])
def test_prepared_plan_keeps_a_fixed_saved_seed_only(control, tmp_path):
    fake, path = _serve(tmp_path, control)
    try:
        seeds = [_prepared_seed(fake, path) for _ in range(2)]
        if control == "fixed":
            assert seeds == [7, 7]
        else:
            assert all(type(seed) is int and 0 <= seed < 2**32 for seed in seeds), seeds
            assert seeds[0] != seeds[1], seeds
    finally:
        fake.stop()


def test_prepared_plan_uses_the_explicit_seed_over_a_fixed_saved_one(tmp_path):
    fake, path = _serve(tmp_path, "fixed")
    try:
        assert _prepared_seed(fake, path, {"seed": 99}) == 99
    finally:
        fake.stop()


def test_workflow_accepts_the_seed_input_describe_lists(fake_comfy, saved_workflow, tmp_path, capsys):
    inputs = tmp_path / "inputs.json"
    inputs.write_text(json.dumps({"seed": 7}), encoding="utf-8")
    code = cli.main(["workflow", str(saved_workflow), "--placement", fake_comfy.url,
                     "--inputs-json", str(inputs), "--dry-run", "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert code == 0, payload
    assert payload.get("status", "ready") == "ready", payload


def test_as_entry_dry_run_lists_the_inputs_describe_lists(fake_comfy, saved_workflow, capsys):
    assert cli.main(["describe", str(saved_workflow), "--placement", fake_comfy.url, "--json"]) == 0
    described = [item["name"] for item in json.loads(capsys.readouterr().out)["inputs"]]
    code = cli.main(["workflow", str(saved_workflow), "--placement", fake_comfy.url,
                     "--as-entry", "my-portrait", "--dry-run", "--json"])
    previewed = [item["name"] for item in json.loads(capsys.readouterr().out)["inputs"]]

    assert code == 0
    assert "seed" in previewed, previewed
    assert previewed == described
    assert fake_comfy.methods() == {"GET"}


def test_seed_control_is_found_by_the_servers_widget_order():
    from astrid.packs.vibecomfy.production_engine import saved_ui_seed_controls
    object_info = {"Sampler": {"input": {"required": {
        "model": ["MODEL"], "mode": [["a", "b"]], "seed": CUSTOM_SEED, "steps": ["INT", {}]}}}}
    raw = {"nodes": [{"id": 5, "type": "Sampler", "widgets_values": ["b", 11, "increment", 4]},
                     {"id": 6, "type": "Unknown", "widgets_values": [1, "fixed"]}]}

    assert saved_ui_seed_controls(raw, object_info) == {
        "5": {"field": "seed", "value": 11, "control": "increment"}}


def test_a_workflow_not_read_as_a_saved_file_keeps_its_seed_fresh(fake_comfy, saved_workflow, tmp_path):
    # Catalog and entry runs do not read saved controls: a fixed saved seed is not theirs to keep.
    from astrid.core.generation import entries
    from astrid.packs.vibecomfy.production_engine import load_workflow_path
    workflow = load_workflow_path(saved_workflow, tmp_path / "s", comfy_url=fake_comfy.url).resolved.workflow
    _, plan, _ = entries.apply_workflow_inputs(workflow, mode="workflow", values={})

    assert plan["bindings"]["seed"] != 7 and 0 <= plan["bindings"]["seed"] < 2**32


def test_a_canonical_workflow_loaded_in_the_same_scratch_keeps_a_fresh_seed(fake_comfy, saved_workflow, tmp_path):
    # Saved controls belong to the load that read them, not to the scratch folder they were read in.
    from astrid.core.generation.entries import prepare_execution_bindings
    from astrid.packs.vibecomfy.production_engine import load_workflow_path
    scratch = tmp_path / "shared"
    saved = load_workflow_path(saved_workflow, scratch, comfy_url=fake_comfy.url)
    assert prepare_execution_bindings(
        saved.resolved.workflow, saved_seed_controls=saved.saved_seed_controls)["seed"] == 7

    canonical = next(scratch.glob("canonical-ui-*/workflow.py"))
    again = load_workflow_path(canonical, scratch)
    assert again.saved_seed_controls is None
    seed = prepare_execution_bindings(again.resolved.workflow, saved_seed_controls=again.saved_seed_controls)["seed"]
    assert seed != 7 and 0 <= seed < 2**32
