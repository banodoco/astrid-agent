from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest

from astrid.packs.vibecomfy import invocation_preflight as preflight
from astrid.sdk.remote import _vibecomfy_invocation_preflight


def _fake_decode(argv: list[str], *, label: str) -> bytes:
    if argv[0] == "ffprobe":
        return b'{"streams":[{"r_frame_rate":"24/1","avg_frame_rate":"24/1"}]}'
    if "rawvideo" in argv:
        return b"\0" * (47 * 4)
    if "f32le" in argv:
        return b"\0" * (62650 * 4 * 2)
    raise AssertionError((label, argv))


def test_h3_source_av_measurement_uses_loaded_frame_count(monkeypatch, tmp_path):
    source = tmp_path / "prefix.mov"
    source.write_bytes(b"source")
    monkeypatch.setattr(preflight, "_run_checked", _fake_decode)

    measured = preflight.measure_h3_source_av(source)

    assert measured.video_frames == 47
    assert measured.expected_audio_samples == 62667
    assert measured.audio_samples == 62650
    assert measured.fractional_mismatch < 0.005
    assert measured.source_frame_rate == "24/1"


def test_h3_source_av_rejects_short_audio_before_gpu(monkeypatch, tmp_path):
    source = tmp_path / "bad.mp4"
    source.write_bytes(b"source")

    def decode(argv: list[str], *, label: str) -> bytes:
        if argv[0] == "ffprobe":
            return b'{"streams":[{"r_frame_rate":"24/1","avg_frame_rate":"24/1"}]}'
        if "rawvideo" in argv:
            return b"\0" * (47 * 4)
        return b"\0" * (60930 * 4 * 2)

    monkeypatch.setattr(preflight, "_run_checked", decode)

    with pytest.raises(
        preflight.InvocationPreflightError,
        match=r"H3_SOURCE_AV_MISMATCH.*2\.772%.*0\.500%",
    ):
        preflight.measure_h3_source_av(source)


@pytest.mark.parametrize(
    ("source_rate", "average_rate"),
    [("24000/1001", "24000/1001"), ("30/1", "24/1")],
)
def test_h3_source_av_rejects_non_cfr_24_input(
    monkeypatch, tmp_path, source_rate: str, average_rate: str
):
    source = tmp_path / "bad-rate.mp4"
    source.write_bytes(b"source")

    def probe(argv: list[str], *, label: str) -> bytes:
        assert argv[0] == "ffprobe"
        return (
            '{"streams":[{"r_frame_rate":"%s","avg_frame_rate":"%s"}]}'
            % (source_rate, average_rate)
        ).encode()

    monkeypatch.setattr(preflight, "_run_checked", probe)
    with pytest.raises(preflight.InvocationPreflightError, match="H3_SOURCE_AV_RATE"):
        preflight.measure_h3_source_av(source)


def test_remote_admission_uses_bundled_manifest_for_media_and_task_inputs(monkeypatch, tmp_path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source-bytes")
    workflow_members = {
        "workflow.py": b"workflow = 'fixture'",
        "workflow.vibe.json": b"{}",
        "source.json": b"{}",
    }
    from astrid.packs.h3_av.actions.compile.compile import _write_asset_bundle

    managed_path = tmp_path / "managed-assets.zip"
    _write_asset_bundle(
        managed_path,
        {"source_video": source},
        workflow_inputs={"seed": 17, "prompt": "say the exact line"},
    )
    objects = dict(workflow_members)
    objects["managed-assets.zip"] = managed_path.read_bytes()

    class Workflow:
        inputs = {
            "source_video": SimpleNamespace(node_id="7", field="video", media_semantics="video"),
            "seed": SimpleNamespace(node_id="8", field="value", media_semantics=None),
            "prompt": SimpleNamespace(node_id="2", field="prompt", media_semantics=None),
        }
        outputs = [SimpleNamespace(node_id="3")]
        edges = [
            SimpleNamespace(from_node="2", to_node="3"),
            SimpleNamespace(from_node="7", to_node="3"),
        ]

        def compile(self, _kind, *, run_inputs):
            return {
                "2": {"class_type": "Prompt", "inputs": {"prompt": run_inputs["prompt"]}},
                "3": {"class_type": "Output", "inputs": {}},
                "7": {"class_type": "LoadVideo", "inputs": {"video": run_inputs["source_video"]}},
            }

    loaded = SimpleNamespace(
        resolved=SimpleNamespace(
            workflow=Workflow(),
            workflow_identity="fixture",
            revision_id="rev-1",
            semantic_digest="sha256:" + "1" * 64,
        ),
        workflow_content_digest="sha256:" + "2" * 64,
    )
    monkeypatch.setattr(
        "astrid.packs.vibecomfy.production_engine.load_workflow_path",
        lambda *_args, **_kwargs: loaded,
    )

    def descriptor(data: bytes, filename: str) -> dict[str, str]:
        digest = "sha256:" + hashlib.sha256(data).hexdigest()
        return {"object_id": digest, "digest": digest, "filename": filename}

    class Client:
        def get_object(self, digest: str) -> bytes:
            filename = {
                "workflow.py": "workflow.py",
                "workflow.vibe.json": "workflow.vibe.json",
                "source.json": "source.json",
                "managed-assets.zip": "managed-assets.zip",
            }[next(name for name, payload in objects.items() if "sha256:" + hashlib.sha256(payload).hexdigest() == digest)]
            return objects[filename]

    inputs = {
        "python": descriptor(objects["workflow.py"], "workflow.py"),
        "companion": descriptor(objects["workflow.vibe.json"], "workflow.vibe.json"),
        "source": descriptor(objects["source.json"], "source.json"),
        "managed_assets": descriptor(objects["managed-assets.zip"], "managed-assets.zip"),
        "workflow_inputs": json.dumps({"seed": 17, "prompt": "say the exact line"}),
    }
    receipt = _vibecomfy_invocation_preflight(Client(), {"inputs": inputs}, strict=True)
    assert receipt["resolved_workflow_inputs"]["source_video"].endswith("source.mp4")
    assert receipt["resolved_workflow_inputs"]["seed"] == 17
    assert receipt["managed_asset_manifest"]["kind"] == "vibecomfy_managed_assets"


def _fake_loaded_workflow(*, prompt: str = "hello", prompt_reachable: bool = True):
    class Workflow:
        inputs = {"source_video": SimpleNamespace(node_id="7", field="video")}
        outputs = [SimpleNamespace(node_id="3")]
        edges = [
            *(  # prompt node is part of the output path only when connected
                [SimpleNamespace(from_node="2", to_node="3")]
                if prompt_reachable
                else []
            ),
            *(  # source node is part of the output path
                [SimpleNamespace(from_node="7", to_node="3")]
                if prompt_reachable
                else []
            ),
        ]

        def compile(self, _kind, *, run_inputs):
            del run_inputs
            return {
                "2": {"class_type": "Prompt", "inputs": {"prompt": prompt}},
                "3": {"class_type": "Output", "inputs": {}},
                "7": {"class_type": "LoadVideo", "inputs": {"video": "clip.mp4"}},
            }

    return SimpleNamespace(
        resolved=SimpleNamespace(
            workflow=Workflow(),
            workflow_identity="fixture",
            revision_id="rev-1",
            semantic_digest="sha256:" + "1" * 64,
        ),
        workflow_content_digest="sha256:" + "2" * 64,
    )


def test_remote_admission_preflights_against_the_run_comfyui_schema(monkeypatch, tmp_path):
    members = {
        "python": b"workflow = 'fixture'",
        "companion": b"{}",
        "source": b"{}",
    }
    by_digest = {
        "sha256:" + hashlib.sha256(data).hexdigest(): data
        for data in members.values()
    }
    filenames = {"python": "workflow.py", "companion": "workflow.vibe.json", "source": "source.json"}
    descriptors = {
        name: {
            "object_id": "sha256:" + hashlib.sha256(data).hexdigest(),
            "digest": "sha256:" + hashlib.sha256(data).hexdigest(),
            "filename": filenames[name],
        }
        for name, data in members.items()
    }

    class Client:
        def get_object(self, digest: str) -> bytes:
            return by_digest[digest]

    class Workflow:
        inputs = {}
        outputs = [SimpleNamespace(node_id="1")]
        edges = []

        def compile(self, _kind, *, run_inputs):
            assert run_inputs == {}
            return {"1": {"class_type": "SaveImage", "inputs": {}}}

    loaded = SimpleNamespace(
        resolved=SimpleNamespace(
            workflow=Workflow(), workflow_identity="fixture", revision_id="rev-1",
            semantic_digest="sha256:" + "1" * 64,
        ),
        workflow_content_digest="sha256:" + "2" * 64,
    )
    seen = {}

    def load(path, scratch, **kwargs):
        seen["comfy_url"] = kwargs.get("comfy_url")
        assert path.read_bytes() == members["python"]
        return loaded

    monkeypatch.setattr("astrid.packs.vibecomfy.production_engine.load_workflow_path", load)
    comfy_url = "http://127.0.0.1:8188"
    receipt = _vibecomfy_invocation_preflight(
        Client(),
        {"inputs": {**descriptors, "comfy_url": comfy_url}},
        strict=True,
    )

    assert seen["comfy_url"] == comfy_url
    assert receipt["phase"] == "submission"


def test_invocation_preflight_rejects_unresolved_prompt_and_wrong_source_binding(
    monkeypatch, tmp_path
):
    workflow = tmp_path / "workflow.py"
    workflow.write_text("workflow = 'fixture'", encoding="utf-8")
    monkeypatch.setattr(
        "astrid.packs.vibecomfy.production_engine.load_workflow_path",
        lambda *_args, **_kwargs: _fake_loaded_workflow(prompt="{{PROMPT}}"),
    )

    with pytest.raises(preflight.InvocationPreflightError, match="PROMPT_UNRESOLVED"):
        preflight.preflight_invocation(
            workflow,
            run_inputs={"source_video": "clip.mp4"},
            expected_source_node="7",
        )


def test_invocation_preflight_rejects_blank_clips_text_prompt(monkeypatch, tmp_path):
    workflow = tmp_path / "workflow.py"
    workflow.write_text("workflow = 'fixture'", encoding="utf-8")
    loaded = _fake_loaded_workflow(prompt="hello")
    original_compile = loaded.resolved.workflow.compile

    def compile_with_clip_text(_kind, *, run_inputs):
        api = original_compile(_kind, run_inputs=run_inputs)
        api["2"] = {"class_type": "CLIPTextEncode", "inputs": {"text": ""}}
        return api

    loaded.resolved.workflow.compile = compile_with_clip_text
    monkeypatch.setattr(
        "astrid.packs.vibecomfy.production_engine.load_workflow_path",
        lambda *_args, **_kwargs: loaded,
    )
    with pytest.raises(preflight.InvocationPreflightError, match="PROMPT_BINDING"):
        preflight.preflight_invocation(
            workflow,
            run_inputs={"source_video": "clip.mp4"},
            expected_prompt="hello",
        )


def test_invocation_preflight_rejects_placeholder_in_non_prompt_node_text(
    monkeypatch, tmp_path
):
    workflow = tmp_path / "workflow.py"
    workflow.write_text("workflow = 'fixture'", encoding="utf-8")
    loaded = _fake_loaded_workflow(prompt="a finished prompt")
    original_compile = loaded.resolved.workflow.compile

    def compile_with_node_text(_kind, *, run_inputs):
        api = original_compile(_kind, run_inputs=run_inputs)
        api["3"]["inputs"]["text"] = "caption={{CAPTION}}"
        return api

    loaded.resolved.workflow.compile = compile_with_node_text
    monkeypatch.setattr(
        "astrid.packs.vibecomfy.production_engine.load_workflow_path",
        lambda *_args, **_kwargs: loaded,
    )
    with pytest.raises(preflight.InvocationPreflightError, match="PLACEHOLDER_UNRESOLVED"):
        preflight.preflight_invocation(
            workflow,
            run_inputs={"source_video": "clip.mp4"},
        )

    with pytest.raises(preflight.InvocationPreflightError, match="SOURCE_BINDING"):
        preflight.preflight_invocation(
            workflow,
            run_inputs={"source_video": "clip.mp4"},
            expected_source_node="99",
        )


def test_invocation_preflight_rejects_unsafe_and_disconnected_prompt_bindings(
    monkeypatch, tmp_path
):
    workflow = tmp_path / "workflow.py"
    workflow.write_text("workflow = 'fixture'", encoding="utf-8")
    monkeypatch.setattr(
        "astrid.packs.vibecomfy.production_engine.load_workflow_path",
        lambda *_args, **_kwargs: _fake_loaded_workflow(prompt="hello", prompt_reachable=False),
    )

    with pytest.raises(preflight.InvocationPreflightError, match="safe non-empty basenames"):
        preflight.preflight_invocation(workflow, run_inputs={"source_video": "../clip.mp4"})

    with pytest.raises(preflight.InvocationPreflightError, match="PROMPT_DISCONNECTED"):
        preflight.preflight_invocation(
            workflow, run_inputs={"source_video": "clip.mp4"}
        )
