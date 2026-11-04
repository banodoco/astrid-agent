"""Video and audio managed assets under local_server placement."""

from __future__ import annotations

import dataclasses
import hashlib
import importlib
import json
import threading
import zipfile
from urllib.parse import parse_qs, urlsplit
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from astrid.core.generation.backends.vibecomfy_local_server import upload_image, upload_media
from astrid.packs.vibecomfy.asset_manifest import build_asset_manifest

VIDEO = b"\x00\x00\x00\x18ftypmp42 fake video bytes"
AUDIO = b"RIFF$\x00\x00\x00WAVEfmt fake audio bytes"


class _Upload:
    """Records /upload/image bodies and answers like ComfyUI."""

    def __init__(self) -> None:
        self.bodies: list[bytes] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args) -> None:
                pass

            def do_POST(self) -> None:
                raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                outer.bodies.append(raw)
                name = raw.split(b'filename="', 1)[1].split(b'"', 1)[0].decode()
                data = json.dumps({"name": name, "subfolder": "astrid/task-1", "type": "input"}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


@pytest.fixture
def upload():
    server = _Upload()
    try:
        yield server
    finally:
        server.close()


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_video_is_staged_with_hash_check_and_same_namespace(upload, tmp_path):
    source = tmp_path / "clip.mp4"
    source.write_bytes(VIDEO)
    reference = upload_media(upload.url, source, task_id="task-1", kind="video", expected_sha256=_sha(VIDEO))
    assert reference == "astrid/task-1/clip.mp4"
    body = upload.bodies[0]
    assert b'name="subfolder"' in body and b"astrid/task-1" in body
    assert b'name="type"' in body and b"input" in body
    assert b'name="image"; filename="clip.mp4"' in body
    assert b"Content-Type: video/mp4" in body
    assert VIDEO in body


def test_audio_is_staged_with_hash_check_and_same_namespace(upload, tmp_path):
    source = tmp_path / "voice.wav"
    source.write_bytes(AUDIO)
    reference = upload_media(upload.url, source, task_id="task-1", kind="audio", expected_sha256=_sha(AUDIO))
    assert reference == "astrid/task-1/voice.wav"
    body = upload.bodies[0]
    assert b'filename="voice.wav"' in body and AUDIO in body
    assert b"Content-Type: audio/" in body


@pytest.mark.parametrize("kind,name,data", [("video", "clip.mp4", VIDEO), ("audio", "voice.wav", AUDIO)])
def test_hash_mismatch_or_missing_hash_is_rejected_before_any_request(upload, tmp_path, kind, name, data):
    source = tmp_path / name
    source.write_bytes(data + b"tampered")
    with pytest.raises(ValueError, match="failed hash verification"):
        upload_media(upload.url, source, task_id="task-1", kind=kind, expected_sha256=_sha(data))
    with pytest.raises(ValueError, match="failed hash verification"):
        upload_media(upload.url, source, task_id="task-1", kind=kind)
    assert upload.bodies == []


def test_sent_bytes_are_the_hashed_bytes(upload, tmp_path):
    source = tmp_path / "clip.mp4"
    source.write_bytes(VIDEO)
    upload_media(upload.url, source, task_id="task-1", kind="video", expected_sha256=_sha(VIDEO))
    assert hashlib.sha256(upload.bodies[0].split(b"\r\n\r\n", 3)[3].rsplit(b"\r\n--", 1)[0]).hexdigest() == _sha(VIDEO)


@pytest.mark.parametrize("kind,name", [("video", "voice.wav"), ("audio", "clip.mp4"), ("video", "frame.png"), ("audio", "x.bin")])
def test_media_type_must_match_the_declared_kind(upload, tmp_path, kind, name):
    source = tmp_path / name
    source.write_bytes(b"x")
    with pytest.raises(ValueError, match=f"managed {kind} asset must have a {kind}/"):
        upload_media(upload.url, source, task_id="task-1", kind=kind, expected_sha256=_sha(b"x"))
    assert upload.bodies == []


def test_symlink_and_unknown_kind_are_rejected(upload, tmp_path):
    real = tmp_path / "real.mp4"
    real.write_bytes(VIDEO)
    link = tmp_path / "link.mp4"
    link.symlink_to(real)
    with pytest.raises(ValueError, match="not a regular file"):
        upload_media(upload.url, link, task_id="task-1", kind="video", expected_sha256=_sha(VIDEO))
    with pytest.raises(ValueError, match="unsupported"):
        upload_media(upload.url, real, task_id="task-1", kind="mask")
    assert upload.bodies == []


def test_image_path_is_unchanged(upload, tmp_path):
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(VIDEO)
    with pytest.raises(ValueError, match="local_server managed assets must be images"):
        upload_image(upload.url, clip, task_id="task-1")
    png = tmp_path / "portrait.png"
    png.write_bytes(b"png bytes")
    assert upload_image(upload.url, png, task_id="task-1") == "astrid/task-1/portrait.png"


def _archive(path: Path, assets: dict[str, tuple[Path, bytes]]) -> Path:
    bindings = {}
    for binding, (source, data) in assets.items():
        source.write_bytes(data)
        bindings[binding] = source
    manifest = build_asset_manifest(bindings)
    by_binding = {record["binding"]: record["member"] for record in manifest["assets"]}
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        for binding, (_, data) in assets.items():
            archive.writestr(by_binding[binding], data)
    return path


def test_local_staging_records_verified_digests_for_the_engine(tmp_path):
    from astrid.packs.vibecomfy import local_server_engine

    action = importlib.import_module("astrid.packs.vibecomfy.actions.run.run")
    archive = _archive(tmp_path / "assets.zip", {
        "source_video": (tmp_path / "clip.mp4", VIDEO),
        "source_audio": (tmp_path / "voice.wav", AUDIO),
    })
    output_root = tmp_path / "attempt" / "outputs"
    bindings = action._stage_managed_assets(str(archive), readiness_profile=None, output_root=output_root)
    for name, data in (("source_video", VIDEO), ("source_audio", AUDIO)):
        staged = output_root / "engine-input" / bindings[name]
        assert staged.read_bytes() == data
        assert local_server_engine._staged_digest(output_root, bindings[name]) == _sha(data)
    assert local_server_engine._staged_digest(output_root, "unknown.mp4") is None


def test_staging_digest_ledger_is_not_written_for_checkout_server_profiles(tmp_path):
    action = importlib.import_module("astrid.packs.vibecomfy.actions.run.run")
    archive = _archive(tmp_path / "assets.zip", {"source_video": (tmp_path / "clip.mp4", VIDEO)})
    session = tmp_path / "session"
    session.mkdir()
    (session / "config.json").write_text(json.dumps({"input_directory": str(tmp_path / "comfy-input")}))
    profile = {"vibecomfy_session": {"session_dir": str(session)}}
    action._stage_managed_assets(str(archive), readiness_profile=profile, output_root=tmp_path / "out")
    assert not (tmp_path / "comfy-input" / ".managed-asset-digests.json").exists()


def test_staged_digest_ignores_symlink_and_corrupt_ledger(tmp_path):
    from astrid.packs.vibecomfy import local_server_engine

    inputs = tmp_path / "engine-input"
    inputs.mkdir()
    ledger = inputs / ".managed-asset-digests.json"
    ledger.write_text("{not json")
    assert local_server_engine._staged_digest(tmp_path, "clip.mp4") is None
    ledger.unlink()
    target = tmp_path / "elsewhere.json"
    target.write_text(json.dumps({"clip.mp4": _sha(VIDEO)}))
    ledger.symlink_to(target)
    assert local_server_engine._staged_digest(tmp_path, "clip.mp4") is None


# --- fake ComfyUI continuation graph with a source video, local placement ---

def _continuation_workflow():
    return {
        "last_node_id": 3, "last_link_id": 2, "version": 0.4,
        "nodes": [
            {"id": 99, "type": "VHS_LoadVideoFFmpeg", "pos": [0, 0], "size": [200, 100],
             "flags": {}, "order": 0, "mode": 0,
             "inputs": [{"name": "video", "type": "COMBO", "widget": {"name": "video"}, "link": None}],
             "outputs": [{"name": "IMAGE", "type": "IMAGE", "links": [1], "slot_index": 0}],
             "properties": {"Node name for S&R": "VHS_LoadVideoFFmpeg"}, "widgets_values": ["original.mp4"]},
            {"id": 2, "type": "ContinueFrames", "pos": [250, 0], "size": [200, 100],
             "flags": {}, "order": 1, "mode": 0,
             "inputs": [{"name": "frames", "type": "IMAGE", "link": 1}],
             "outputs": [{"name": "IMAGE", "type": "IMAGE", "links": [2], "slot_index": 0}],
             "properties": {"Node name for S&R": "ContinueFrames"}, "widgets_values": []},
            {"id": 3, "type": "SaveImage", "pos": [500, 0], "size": [200, 100],
             "flags": {}, "order": 2, "mode": 0,
             "inputs": [{"name": "images", "type": "IMAGE", "link": 2}],
             "outputs": [], "properties": {"Node name for S&R": "SaveImage"},
             "widgets_values": ["continuation"]},
        ],
        "links": [[1, 99, 0, 2, 0, "IMAGE"], [2, 2, 0, 3, 0, "IMAGE"]],
    }


@pytest.fixture
def comfy_video(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTRID_INTERNAL_INVOCATION", "1")
    monkeypatch.setenv("VIBECOMFY_HEADLESS", "1")
    monkeypatch.chdir(tmp_path)
    schema = {
        "VHS_LoadVideoFFmpeg": {"input": {"required": {"video": [["original.mp4"], {"video_upload": True}]}},
                                "output": ["IMAGE"], "output_name": ["IMAGE"], "output_node": False},
        "ContinueFrames": {"input": {"required": {"frames": ["IMAGE"]}},
                           "output": ["IMAGE"], "output_name": ["IMAGE"], "output_node": False},
        "SaveImage": {"input": {"required": {"images": ["IMAGE"], "filename_prefix": ["STRING", {"default": "astrid"}]}},
                      "output": [], "output_node": True},
    }
    (tmp_path / "node_index.json").write_text(json.dumps([
        {"class_type": "VHS_LoadVideoFFmpeg", "pack": "vhs", "inputs": {"video": {"type": "STRING", "required": True}},
         "outputs": [{"type": "IMAGE", "name": "IMAGE"}]},
        {"class_type": "ContinueFrames", "pack": "test", "inputs": {"frames": {"type": "IMAGE", "required": True}},
         "outputs": [{"type": "IMAGE", "name": "IMAGE"}]},
        {"class_type": "SaveImage", "pack": "core", "inputs": {
            "images": {"type": "IMAGE", "required": True},
            "filename_prefix": {"type": "STRING", "required": True}}, "outputs": []},
    ]))
    user_output = tmp_path / "user-output"
    user_output.mkdir()
    state = SimpleNamespace(schema=schema, calls=[], prompts=[], uploads=[], output=user_output, histories={})

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def send(self, payload):
            data = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            state.calls.append(("GET", self.path))
            if self.path == "/object_info":
                self.send(state.schema)
            elif self.path == "/system_stats":
                self.send({"system": {"comfyui_version": "0.26.0"}, "devices": []})
            elif self.path.startswith("/history/"):
                self.send({self.path.rsplit("/", 1)[1]: state.histories[self.path.rsplit("/", 1)[1]]})
            elif self.path == "/queue":
                self.send({"queue_running": [], "queue_pending": []})
            elif self.path.startswith("/view?"):
                name = parse_qs(urlsplit(self.path).query)["filename"][0]
                data = (user_output / name).read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            else:
                self.send({})

        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            state.calls.append(("POST", self.path))
            if self.path == "/upload/image":
                state.uploads.append(raw)
                name = raw.split(b'filename="', 1)[1].split(b'"', 1)[0].decode()
                self.send({"name": name, "subfolder": "astrid/task-1", "type": "input"})
                return
            body = json.loads(raw or b"{}")
            if self.path == "/prompt":
                state.prompts.append(body["prompt"])
                prompt_id = f"ours-{len(state.prompts)}"
                filename = f"astrid_{len(state.prompts)}.png"
                Image.new("RGB", (16, 16), (len(state.prompts), 10, 20)).save(user_output / filename)
                state.histories[prompt_id] = {
                    "outputs": {"3": {"images": [{"filename": filename, "subfolder": "", "type": "output"}]}},
                    "status": {"status_str": "success", "completed": True, "messages": []},
                }
                self.send({"prompt_id": prompt_id, "number": len(state.prompts), "node_errors": {}})
            else:
                self.send({})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state.url = f"http://127.0.0.1:{server.server_port}"
    state.workflow = tmp_path / "continuation.json"
    state.workflow.write_text(json.dumps(_continuation_workflow()))
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def _declare_source_video(monkeypatch):
    """Declare the continuation graph's public ``source_video`` input.

    The H3 continuation template declares it with ``media_semantics="video"``;
    UI JSON has no such declaration, so derive the same public contract here.
    """
    from vibecomfy.security.provenance import Provenance
    from vibecomfy.workflow_bundle import emit_bundle_with_candidate, load_bundle

    from astrid.packs.vibecomfy import production_engine

    real = production_engine.load_workflow_path

    def load(*args, **kwargs):
        loaded = real(*args, **kwargs)
        bundle = production_engine._canonical_bundle_value(loaded.resolved)
        workflow = bundle.workflow.clone()
        node = next(n for n in workflow.nodes.values() if n.class_type == "VHS_LoadVideoFFmpeg")
        workflow.register_input("source_video", node.id, "video", "", type="COMBO", media_semantics="video")
        root = Path(args[1]) / "declared-workflow"
        root.mkdir(parents=True, exist_ok=True)
        derived = emit_bundle_with_candidate(
            workflow, root / "workflow.py", Provenance.USER_CONFIRMED, bundle.ui_sidecar,
            parent_revision=bundle.revision_id, operation="authored",
            parent_evidence={"revision_id": bundle.revision_id, "workflow_identity": bundle.workflow_identity},
            source_format="ready_template",
        )
        return dataclasses.replace(
            loaded, resolved=load_bundle(derived.python_path, trust=Provenance.USER_CONFIRMED),
        )

    monkeypatch.setattr(production_engine, "load_workflow_path", load)


@pytest.mark.integration
def test_continuation_graph_with_source_video_runs_under_local_placement(comfy_video, tmp_path, monkeypatch):
    _declare_source_video(monkeypatch)
    action = importlib.import_module("astrid.packs.vibecomfy.actions.run.run")
    archive = _archive(tmp_path / "assets.zip", {"source_video": (tmp_path / "clip.mp4", VIDEO)})
    result = action._run_and_settle(
        comfy_video.workflow, tmp_path / "attempt" / "outputs", task_identity="task-1",
        comfy_url=comfy_video.url, comfy_output_dir=str(comfy_video.output), managed_assets=str(archive),
    )
    assert result["managed_generation_result_path"]
    uploads = [body for body in comfy_video.uploads]
    assert len(uploads) == 1 and VIDEO in uploads[0] and b"Content-Type: video/mp4" in uploads[0]
    reference = comfy_video.prompts[0]["99"]["inputs"]["video"]
    staged_name = reference.rsplit("/", 1)[1]
    assert reference == f"astrid/task-1/{staged_name}"
    assert staged_name.endswith("-clip.mp4")


@pytest.mark.integration
def test_continuation_graph_rejects_video_whose_staged_bytes_changed(comfy_video, tmp_path, monkeypatch):
    _declare_source_video(monkeypatch)
    from astrid.packs.vibecomfy import production_engine

    action = importlib.import_module("astrid.packs.vibecomfy.actions.run.run")
    archive = _archive(tmp_path / "assets.zip", {"source_video": (tmp_path / "clip.mp4", VIDEO)})
    output_root = tmp_path / "attempt" / "outputs"
    bindings = action._stage_managed_assets(str(archive), readiness_profile=None, output_root=output_root)
    (output_root / "engine-input" / bindings["source_video"]).write_bytes(b"swapped after verification")
    with pytest.raises(Exception, match="failed hash verification"):
        production_engine.run_workflow_result_path(
            comfy_video.workflow, output_root, task_identity="task-1",
            comfy_url=comfy_video.url, comfy_output_dir=str(comfy_video.output), profile_id="local_server",
            workflow_input_bindings=bindings,
        )
    assert not comfy_video.prompts and not comfy_video.uploads


@pytest.mark.integration
def test_only_media_bound_file_pickers_are_exempt_from_the_enum_check():
    from vibecomfy.schema import InputSpec, NodeSchema

    from astrid.packs.vibecomfy.local_server_engine import _UploadedMediaSchema

    class Provider:
        marker = "delegated"

        def get_schema(self, class_type):
            return {
                "VHS_LoadVideoFFmpeg": NodeSchema("VHS_LoadVideoFFmpeg", None, {
                    "video": InputSpec("COMBO", choices=["old.mp4"]),
                    "format": InputSpec("COMBO", choices=["AnimateDiff"])}, []),
                "Other": NodeSchema("Other", None, {"video": InputSpec("COMBO", choices=["old.mp4"])}, []),
            }.get(class_type)

    view = _UploadedMediaSchema(Provider(), {("VHS_LoadVideoFFmpeg", "video")})
    schema = view.get_schema("VHS_LoadVideoFFmpeg")
    assert schema.inputs["video"].choices is None and schema.inputs["video"].unresolved_choices
    assert schema.inputs["format"].choices == ["AnimateDiff"]
    assert view.get_schema("Other").inputs["video"].choices == ["old.mp4"]
    assert view.get_schema("Missing") is None
    assert view.marker == "delegated"
