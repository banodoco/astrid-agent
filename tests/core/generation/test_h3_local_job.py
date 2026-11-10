"""Proof node ``test_job_runs_h3_build_and_after_outputs_once_each`` (Plan 32 T6b-3, T4i's h3 half).

The pod-side job file for entry ``h3`` goes through ``local_job.run_job``: build runs once (CPU), the run
action's ``after_outputs`` seam runs the hook once on the generated output, and what is sealed is the
composed and verified candidate, not the raw output. Only the ComfyUI submit is stood in for.
"""
import hashlib
import importlib
import json
import subprocess
from pathlib import Path

import pytest

from astrid.core.generation import entries
from astrid.core.generation.local_job import run_job

REQUEST = Path(__file__).resolve().parents[2] / "fixtures" / "h3_cpu" / "request.v2.json"


def _video(path, seconds, frames=None):
    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "color=c=blue:s=1024x576:r=24",
               "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}"]
    if frames:
        command += ["-frames:v", str(frames)]
    subprocess.run([*command, "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path)],
                   check=True, timeout=120)
    return path


@pytest.mark.parametrize("prepared", [False, True], ids=["public-job", "controller-prepared-job"])
def test_job_runs_h3_build_and_after_outputs_once_each(tmp_path, monkeypatch, prepared):
    action = importlib.import_module("astrid.packs.vibecomfy.actions.run.run")
    source = _video(tmp_path / "source.mp4", 3, frames=57)
    request = json.loads(REQUEST.read_text())
    asset_map = {request["media"][0]["asset"]["id"]: str(source), "source": str(source)}
    job = tmp_path / "job.json"
    payload = {"schema": "astrid.machine-job.v1", "entry": "h3", "mode": "v2v", "align": "check",
               "inputs": {"request": str(REQUEST), "asset_map": asset_map}}
    if prepared:
        payload.update(execution={"python": str(entries.bundle_path(entries.find_entry("h3")))}, bindings={"seed": 987})
    job.write_text(json.dumps(payload))

    builds, hooks = [], []
    real_build = entries.build_entry
    monkeypatch.setattr(entries, "build_entry", lambda *a, **k: builds.append(1) or real_build(*a, **k))
    monkeypatch.setattr("astrid.core.generation.preflight.require_generation_target_readiness", lambda *a: None)
    monkeypatch.setattr(entries, "target_schema", lambda *a, **k: None)
    sealed = {}
    generated = _video(tmp_path / "generated.mp4", 4.5)

    def settle(workflow, out, **kwargs):
        """The run action's seam: the hook sees the generated output before it is sealed."""
        sealed["job_dir_during_run"] = json.loads(kwargs["after_outputs_job"])["job_dir"]
        assert Path(workflow).is_relative_to(sealed["job_dir_during_run"])
        assert kwargs["manifest"]["engine"] == "comfyui"
        if prepared:
            assert json.loads(kwargs["workflow_inputs"])["seed"] == 987
            from vibecomfy.workflow_bundle import load_bundle
            from vibecomfy.security.provenance import Provenance
            built = load_bundle(workflow, trust=Provenance.USER_CONFIRMED).workflow
            assert built.inputs["seed"].value == 987
        assert Path(sealed["job_dir_during_run"], "h3", "build-state.json").is_file()  # build ran first
        after_outputs = action._after_outputs(kwargs["after_outputs_job"])
        hooks.append(1)
        sealed["raw"] = hashlib.sha256(generated.read_bytes()).hexdigest()
        composed = Path(after_outputs([generated])[0])
        sealed["composed"] = hashlib.sha256(composed.read_bytes()).hexdigest()
        return {}

    monkeypatch.setattr(action, "_run_and_settle", settle)
    run_job("h3", job, tmp_path / "out")
    assert builds == [1] and hooks == [1]
    assert sealed["composed"] != sealed["raw"]
    assert not Path(sealed["job_dir_during_run"]).exists()  # removed when the job ends
