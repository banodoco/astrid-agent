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
    assert [(p["name"], p["type"]) for p in entry["inputs"]] == [("request", "file"), ("asset_map", "file_map")]
    assert all(isinstance(need, dict) for need in entry["needs"])
    description = entries.describe_entry("h3", mode="i2v", _native=True)
    assert description["source"] == "workflow" and "action" not in description
    assert [p["name"] for p in description["inputs"]] == ["request", "asset_map"]


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
    engine.commands = NoCommands()
    outputs = engine.run(job, Local())  # prepare (build), align, submit, finish (after_outputs)
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
