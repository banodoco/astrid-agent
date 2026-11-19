"""Proof node ``h3_contribution_build_and_after_outputs_end_to_end`` (Plan 32 T6b-3).

``find_entry("h3")`` is the ``vibecomfy.workflows`` contribution, not an action entry. The
production engine methods (``ComfyUIEngine.prepare`` / ``run`` / ``finish``) run its real
``build`` and ``after_outputs`` on CPU: build prepares and compiles once and saves its state in
the job directory; after_outputs composes and verifies from that state without preparing or
compiling again.
"""
import json
import subprocess
from pathlib import Path

import pytest

from astrid.core.generation import entries
from astrid.core.generation import engine_facts  # noqa: F401  (the facts the engine hands the hooks)

REQUEST = Path(__file__).resolve().parents[2] / "fixtures" / "h3_cpu" / "request.v2.json"


def _video(path, *, seconds=3, size="1024x576", frames=None):
    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", f"color=c=blue:s={size}:r=24",
               "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}"]
    if frames:
        command += ["-frames:v", str(frames)]
    subprocess.run([*command, "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path)],
                   check=True, timeout=120)
    return path


def test_h3_is_the_contribution_not_an_action_entry():
    entry = entries.find_entry("h3")
    assert entry["source"] == "user-entry" and entry["contribution"] == "h3_av.h3"
    assert "action" not in entry
    assert entry["hooks_declared"] == ["build", "after_outputs"]
    assert [(p["name"], p["type"]) for p in entry["inputs"]] == [
        ("request", "file"), ("asset_map", "file_map"), ("prompt", "string"), ("image_ref", "file"),
        ("video_ref", "file"), ("frames", "integer"), ("seed", "integer"), ("steps", "integer")]
    assert all(isinstance(need, dict) for need in entry["needs"])
    description = entries.describe_entry("h3", mode="i2v", _native=True)
    assert description["source"] == "workflow" and "action" not in description
    assert [p["name"] for p in description["inputs"]] == [
        "request", "asset_map", "prompt", "image_ref", "video_ref", "frames", "seed", "steps"]


def test_h3_contribution_build_and_after_outputs_end_to_end(tmp_path):
    engine = entries.engine_table()["comfyui"]
    entry = entries.find_entry("h3")
    source = _video(tmp_path / "source.mp4", frames=57)
    request = json.loads(REQUEST.read_text())
    asset_map = {request["media"][0]["asset"]["id"]: str(source), "source": str(source)}
    inputs = {"request": str(REQUEST), "asset_map": asset_map}
    job_dir = tmp_path / "job"

    built = entries.build_entry(entry, inputs, job_dir)
    assert built is not None and {"workflow", "bindings", "plan"} <= set(built)
    assert built["plan"]["request_digest"] and built["bindings"]
    state = job_dir / "h3" / "build-state.json"
    assert state.is_file()

    hooks = entries.entry_hooks(entry)
    assert set(hooks) == {"build", "after_outputs"}
    generated = _video(tmp_path / "generated.mp4", seconds=4.5)
    calls = []

    class Local:
        def ensure_running(self):
            return "http://127.0.0.1:8188"

        def submit(self, url, workflow):
            calls.append(("submit", workflow))
            return [str(generated)]

    class NoCommands:
        def __getattr__(self, name):
            return lambda *a, **k: None

    import astrid.packs.vibecomfy.extensions  # noqa: F401
    job = {**entries.entry_job(entry, inputs, job_dir, hooks=hooks),
           "manifest": {"models": [], "extra": {"nodes": []}}}
    original_commands = engine.commands
    engine.commands = NoCommands()
    try:
        outputs = engine.run(job, Local())  # prepare (build), align, submit, finish (after_outputs)
    finally:
        engine.commands = original_commands
    assert [c[0] for c in calls] == ["submit"] and calls[0][1] is not None
    assert outputs and Path(outputs[0]).is_file() and Path(outputs[0]) != generated

    # after_outputs alone must not run prepare or compile again: it reads build's saved state.
    import importlib
    stages = importlib.import_module("_h3_av_pack.shared.stages")
    original = stages.compile_stages
    stages.compile_stages = lambda *a, **k: pytest.fail("after_outputs re-ran prepare/compile")
    try:
        again = engine.finish(job, [str(generated)])
    finally:
        stages.compile_stages = original
    assert again and Path(again[0]).is_file()


def test_the_run_actions_after_outputs_spec_composes_the_candidate(tmp_path):
    """vibecomfy.run's ``--after-outputs-job`` (what the facade passes) resolves h3's hook and composes."""
    import importlib
    entry = entries.find_entry("h3")
    source = _video(tmp_path / "source.mp4", frames=57)
    request = json.loads(REQUEST.read_text())
    inputs = {"request": str(REQUEST), "asset_map": {request["media"][0]["asset"]["id"]: str(source), "source": str(source)}}
    job_dir = tmp_path / "job"
    entries.build_entry(entry, inputs, job_dir)
    job = entries.entry_job(entry, inputs, job_dir, hooks={})
    spec = json.dumps({"entry": "h3", "job_dir": str(job_dir), "inputs": job["inputs"], "files": job["files"]})
    run = importlib.import_module("astrid.packs.vibecomfy.actions.run.run")
    after_outputs = run._after_outputs(spec)
    generated = _video(tmp_path / "generated.mp4", seconds=4.5)
    produced = after_outputs([generated])
    assert Path(produced[0]).is_file() and Path(produced[0]) != generated
    assert run._after_outputs("{}") is None


def test_h3_builds_the_same_request_from_the_kinds_plain_inputs(tmp_path):
    """A prompt with one media file is the request document h3 would be handed: no second path."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "h3_hooks_plain", "astrid/packs/h3_av/extensions/vibecomfy.workflows/hooks.py")
    hooks = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hooks)

    class Facts:
        @staticmethod
        def measure_source_av(path):
            return {"video_frames": 60}

    request_module = hooks._shared("request")
    document, assets = hooks._plain_request({"prompt": "a kite", "frames": 72, "seed": 7}, {"image_ref": "/x/look.png"}, Facts)
    assert assets == {"image_ref": "/x/look.png"}
    value = request_module.normalize_request(document).value
    assert value["operation"] == "generate" and value["output"]["duration"] == 3.0 and value["overrides"] == {"seed": 7}
    assert value["references"] == [{"asset": "image_ref", "purpose": "appearance"}]

    document, assets = hooks._plain_request({"prompt": "as water"}, {"video_ref": "/x/clip.mp4"}, Facts)
    assert assets == {"video_ref": "/x/clip.mp4"}
    value = request_module.normalize_request(document).value
    assert value["operation"] == "edit" and value["source"] == {"asset": "video_ref", "range": [0.0, 2.5]}
    assert value["output"]["duration"] == 2.5

    for bad in ({}, {"prompt": "x"}, {"prompt": "x", "image_ref": "a.png", "video_ref": "b.mp4"}, {"image_ref": "a.png"}):
        with pytest.raises(ValueError, match="request.*or a prompt with exactly one of"):
            hooks._plain_request(bad, {}, Facts)
