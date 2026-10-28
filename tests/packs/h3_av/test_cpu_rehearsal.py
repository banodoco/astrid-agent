"""R1: CPU rehearsal of local placement. No GPU and no network.

ComfyUI is the only fake: the fixture HTTP server in ``tests/helpers/h3_cpu_runtime.py``. These tests
pin what it must accept: the generate graph the real compiler produces for the H3 workflow, as a real
local run would POST it to ``/prompt``, and a valid ``/history`` answer that ``/view`` can serve.
"""

from __future__ import annotations

import hashlib
import json
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from astrid.packs.h3_av.actions.compile.compile import compile_preparation
from astrid.packs.h3_av.actions.prepare.prepare import prepare_request
from astrid.packs.h3_av.shared.request import normalize_request

# Integration: the H3 workflow is compiled by the pinned VibeComfy checkout. ffmpeg writes the clip.
pytestmark = [pytest.mark.integration, pytest.mark.ffmpeg]

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "h3_cpu"


def _request() -> object:
    return normalize_request({
        "version": 1, "operation": "continue",
        "source": {"asset": "source", "range": [0, 1]}, "output": {"duration": 2},
        "content": {"prompt": "continue"},
        "changes": {"video": [{"during": [1, 2], "area": {"full_frame": True}, "action": "generate"}],
                    "audio": [{"during": [1, 2], "action": "generate"}]},
        "references": [], "overrides": {},
    })


def _generate_graph(tmp_path: Path, source: Path) -> tuple[dict, dict]:
    """The API-format prompt for the compiled H3 workflow, as the local run submits it."""
    from astrid.packs.vibecomfy.shared.python_execution_consent import confirmed_python_execution_scope
    from vibecomfy import workflow_bundle

    compilation = compile_preparation(
        prepare_request(_request(), asset_map={"source": str(source)}), out_dir=tmp_path / "compiled",
    )
    with confirmed_python_execution_scope("confirmed"):
        bundle = workflow_bundle.load_bundle(Path(compilation["workflow"]["workflow.py"]["path"]))
        graph = bundle.workflow.compile("api")
    loaders = [n for n in graph.values() if n["class_type"] == "VHS_LoadVideoFFmpeg"]
    assert len(loaders) == 1
    # The run binds the managed source basename into the loader before submission.
    loaders[0]["inputs"]["video"] = compilation["workflow_inputs"]["source_video"]
    return graph, compilation


class _FakeComfy:
    def __init__(self, root: Path, *, source: Path, basename: str) -> None:
        from tests.helpers import h3_cpu_runtime as helper

        self.input_dir, self.output_dir = root / "input", root / "output"
        self.input_dir.mkdir()
        self.output_dir.mkdir()
        (self.input_dir / basename).write_bytes(source.read_bytes())
        self.evidence = root / "evidence.json"
        self.expected = root / "expected.json"
        self.expected.write_text(json.dumps({
            "basename": basename, "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "size": source.stat().st_size, "generated_seconds": 1,
        }), encoding="utf-8")
        config = root / "config.json"
        config.write_text(json.dumps({"input_directory": str(self.input_dir),
                                      "output_directory": str(self.output_dir)}), encoding="utf-8")
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = probe.getsockname()[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.process = subprocess.Popen(
            [sys.executable, "-c", helper._SERVER_CODE, str(self.port),
             str(FIXTURES / "object_info_h3_runpod.json"), str(config), str(self.expected), str(self.evidence)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 15
        while True:
            try:
                self.get("/object_info")
                return
            except OSError:
                if time.monotonic() > deadline or self.process.poll() is not None:
                    self.close()
                    raise RuntimeError("fake ComfyUI did not start")
                time.sleep(0.05)

    def get(self, path: str) -> bytes:
        with urllib.request.urlopen(self.url + path, timeout=10) as response:
            return response.read()

    def post(self, graph: dict) -> tuple[int, dict]:
        request = urllib.request.Request(
            self.url + "/prompt", data=json.dumps({"prompt": graph}).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def close(self) -> None:
        self.process.terminate()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()


@pytest.fixture
def fake_comfy(tmp_path):
    source = tmp_path / "source.mp4"
    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "color=c=blue:s=64x64:r=24:d=1", "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(source),
    ], check=True, capture_output=True, timeout=60)
    graph, compilation = _generate_graph(tmp_path, source)
    root = tmp_path / "comfy"
    root.mkdir()
    fake = _FakeComfy(root, source=source, basename=compilation["workflow_inputs"]["source_video"])
    try:
        yield fake, graph
    finally:
        fake.close()


def test_fake_comfy_accepts_the_real_h3_generate_graph_and_returns_a_valid_history(fake_comfy):
    fake, graph = fake_comfy
    # The real graph has one saved sink and one preview sink; the preview must not make it ambiguous.
    saved = [k for k, n in graph.items() if n["inputs"].get("save_output") is True]
    previews = [k for k, n in graph.items() if n["class_type"] == "VHS_VideoCombine"]
    assert len(saved) == 1 and previews and saved[0] not in previews

    status, body = fake.post(graph)
    assert status == 200, body
    assert body["node_errors"] == {}
    prompt_id = body["prompt_id"]

    history = json.loads(fake.get(f"/history/{prompt_id}"))[prompt_id]
    assert history["status"]["status_str"] == "success" and history["status"]["completed"] is True
    (item,) = history["outputs"][saved[0]]["gifs"]
    assert item["type"] == "output" and item["subfolder"] == ""
    clip = fake.get(f"/view?filename={item['filename']}&subfolder=&type=output")
    assert clip == (fake.output_dir / item["filename"]).read_bytes() and clip
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type", "-of", "csv=p=0",
         str(fake.output_dir / item["filename"])], capture_output=True, text=True, check=True, timeout=30)
    assert sorted(probe.stdout.split()) == ["audio", "video"]
    evidence = json.loads(fake.evidence.read_text())
    assert evidence["output_node"] == saved[0] and evidence["output_filename"] == item["filename"]
    assert evidence["node_id"] == "99" and evidence["video"] == graph["99"]["inputs"]["video"]


def test_fake_comfy_accepts_a_source_free_generate_graph(fake_comfy):
    fake, graph = fake_comfy
    loader = next(k for k, n in graph.items() if n["class_type"] == "VHS_LoadVideoFFmpeg")
    source_free = {k: n for k, n in graph.items() if k != loader}
    source_free = {k: {**n, "inputs": {i: v for i, v in n["inputs"].items()
                                       if not (isinstance(v, list) and v[0] == loader)}}
                   for k, n in source_free.items()}
    status, body = fake.post(source_free)
    assert status == 200, body


@pytest.mark.parametrize("fault", ["dangling_link", "two_saved_sinks", "wrong_source", "untyped_node"])
def test_fake_comfy_still_rejects_graphs_the_real_run_must_not_send(fake_comfy, fault):
    fake, graph = fake_comfy
    graph = json.loads(json.dumps(graph))
    if fault == "dangling_link":
        next(n for n in graph.values() if n["class_type"] == "VAEDecode")["inputs"]["samples"] = ["no-such-node", 0]
    elif fault == "two_saved_sinks":
        next(n for n in graph.values() if n["class_type"] == "VHS_VideoCombine")["inputs"]["save_output"] = True
    elif fault == "wrong_source":
        graph["99"]["inputs"]["video"] = "other.mp4"
    else:
        graph["99"] = {"inputs": {}}
    status, body = fake.post(graph)
    assert status == 400 and "rejected prompt" in body["error"], body
    assert not list(fake.output_dir.iterdir())
