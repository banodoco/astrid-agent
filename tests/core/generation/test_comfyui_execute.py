"""Plan 32 T4b: ComfyUIEngine.execute delegates to the one local run path and writes into job.out."""
from __future__ import annotations

import importlib.util
from pathlib import Path

from tests.packs.vibecomfy.test_local_server_run import comfy  # noqa: F401  (the repo's stand-in ComfyUI)

ROOT = Path(__file__).resolve().parents[3]
ENGINE_FILE = ROOT / "astrid/packs/vibecomfy/extensions/generation.engine/comfyui_engine.py"
spec = importlib.util.spec_from_file_location("comfyui_engine_t4b", ENGINE_FILE)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def test_execute_writes_outputs_to_job_out(comfy, tmp_path):  # noqa: F811
    out = tmp_path / "job" / "outputs"
    job = {"workflow": comfy.workflow, "out": out, "task_identity": "task-t4b",
           "attempt_identity": "attempt-t4b", "comfy_url": comfy.url, "comfy_output_dir": str(comfy.output)}
    result = mod.ComfyUIEngine().execute(job)
    files = [out / row["path"] for row in result["outputs"]]
    assert files and all(f.is_file() and f.is_relative_to(out) for f in files)
    assert (out / result["managed_generation_result_path"]).is_file()
    assert len(comfy.prompts) == 1
