"""Regression proofs for H3 plain-input admission and execution preflight."""
import json
from pathlib import Path

import pytest
from astrid.core.generation import entries


def _plain_judge_hooks():
    entry = entries.find_entry("h3")
    hooks = entries.entry_hooks(entry)
    return entry, hooks, hooks["build"].__globals__


def _judge_image_inputs(tmp_path):
    from PIL import Image

    image = tmp_path / "look.png"
    Image.new("RGB", (1024, 576), "blue").save(image)
    return {"prompt": "a kite", "image_ref": str(image), "frames": 5}


def _hide_judge_media_tools(monkeypatch, namespace):
    original = namespace["shutil"].which
    monkeypatch.setattr(namespace["shutil"], "which",
                        lambda name, *a, **k: None if name in {"ffmpeg", "ffprobe"}
                        else original(name, *a, **k))


def test_h3_plain_cpu_dry_run_needs_no_media_binaries(tmp_path, monkeypatch):
    entry, hooks, namespace = _plain_judge_hooks()
    _hide_judge_media_tools(monkeypatch, namespace)
    job = entries.entry_job(entry, _judge_image_inputs(tmp_path), tmp_path / "job", hooks=hooks)
    result = entries.engine_table()["comfyui"].dry_run(job)
    assert result["plan"]["request_digest"]
    assert result["bindings"]


@pytest.mark.parametrize("attached", [False, True])
def test_h3_runtime_media_check_precedes_compile_align_and_submit(tmp_path, monkeypatch, attached):
    entry, hooks, namespace = _plain_judge_hooks()
    _hide_judge_media_tools(monkeypatch, namespace)
    stages = namespace["_shared"]("stages")
    monkeypatch.setattr(stages, "compile_stages", lambda *a, **k: pytest.fail("compiled before tool check"))
    engine = entries.engine_table()["comfyui"]
    monkeypatch.setattr(engine, "align", lambda *a, **k: pytest.fail("aligned before tool check"))
    monkeypatch.setattr(engine, "probe", lambda *a, **k: pytest.fail("probed before tool check"))

    class Local:
        def ensure_running(self):
            pytest.fail("started engine before tool check")

        def submit(self, *_args):
            pytest.fail("submitted before tool check")

    job = entries.entry_job(entry, _judge_image_inputs(tmp_path), tmp_path / "job", hooks=hooks)
    if attached:
        job["endpoint"] = "http://127.0.0.1:8188"
    with pytest.raises(ValueError, match="ffmpeg.*ffprobe"):
        engine.run(job, Local())


def test_h3_build_entry_runtime_check_precedes_compilation(tmp_path, monkeypatch):
    entry, _hooks, namespace = _plain_judge_hooks()
    _hide_judge_media_tools(monkeypatch, namespace)
    inputs = _judge_image_inputs(tmp_path)
    stages = namespace["_shared"]("stages")
    monkeypatch.setattr(stages, "compile_stages", lambda *a, **k: pytest.fail("compiled before tool check"))
    with pytest.raises(ValueError, match="ffmpeg.*ffprobe"):
        entries.build_entry(entry, inputs, tmp_path / "run", check_runtime=True)


def test_hookless_attached_run_needs_no_h3_media_binaries(tmp_path, monkeypatch):
    _entry, _hooks, namespace = _plain_judge_hooks()
    _hide_judge_media_tools(monkeypatch, namespace)
    engine = entries.engine_table()["comfyui"]
    monkeypatch.setattr(engine, "probe", lambda endpoint: {"ready": True})
    calls = []

    class Local:
        def ensure_running(self):
            pytest.fail("attached server should be reused")

        def submit(self, url, workflow):
            calls.append((url, workflow))
            return ["result.mp4"]

    job = {"hooks": {}, "endpoint": "http://127.0.0.1:8188", "workflow": object()}
    assert engine.run(job, Local()) == ["result.mp4"]
    assert calls == [(job["endpoint"], job["workflow"])]
