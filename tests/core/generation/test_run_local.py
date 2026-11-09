"""Plan 32 T4: one local run path. align/check/trust on CPU fakes; no model is downloaded outside align."""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
ENGINE_FILE = ROOT / "astrid/packs/vibecomfy/extensions/generation.engine/comfyui_engine.py"
spec = importlib.util.spec_from_file_location("comfyui_engine_t4", ENGINE_FILE)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


class FakeCommands:
    def __init__(self):
        self.calls = []

    def doctor(self): self.calls.append("doctor")
    def prepare_deps_sync(self): self.calls.append("prepare --deps sync")
    def nodes_restore(self, nodes): self.calls.append("nodes restore")
    def fetch(self, rows): self.calls.append(("fetch", [r["name"] for r in rows]))


class FakeLocal:
    def __init__(self, attached):
        self.attached, self.started, self.submitted = attached, 0, []

    def ensure_running(self):
        if not self.attached:
            self.started += 1
        return "http://127.0.0.1:8188"

    def submit(self, url, workflow):
        self.submitted.append((url, workflow))
        return {"prompt_id": "p1"}


def _manifest(tmp_path):
    (tmp_path / "checkpoints").mkdir()
    (tmp_path / "checkpoints/have.safetensors").write_bytes(b"abcd")
    return {"models_root": str(tmp_path), "extra": {"nodes": []}, "models": [
        {"name": "have.safetensors", "subdir": "checkpoints", "size": 4},
        {"name": "need.safetensors", "subdir": "vae", "size": 9}]}


def test_align_fetches_only_missing_rows(tmp_path):
    commands = FakeCommands()
    result = mod.ComfyUIEngine(commands).align(_manifest(tmp_path), "align")
    assert commands.calls == ["doctor", "prepare --deps sync", "nodes restore", ("fetch", ["need.safetensors"])]
    assert result["fetched"] == ["vae/need.safetensors"] and result["verified"] is True


def test_align_with_nothing_missing_does_not_fetch(tmp_path):
    manifest = _manifest(tmp_path)
    manifest["models"] = manifest["models"][:1]
    commands = FakeCommands()
    mod.ComfyUIEngine(commands).align(manifest, "align")
    assert not any(isinstance(c, tuple) for c in commands.calls)


def test_check_reports_and_touches_nothing(tmp_path):
    commands = FakeCommands()
    result = mod.ComfyUIEngine(commands).align(_manifest(tmp_path), "check")
    assert commands.calls == [] and result["status"] == "missing" and result["missing"] == ["vae/need.safetensors"]


def test_trust_is_marked_unverified(tmp_path):
    commands = FakeCommands()
    result = mod.ComfyUIEngine(commands).align(_manifest(tmp_path), "trust")
    assert commands.calls == [] and result["verified"] is False and result["status"] == "unverified"


def test_unknown_mode_refused(tmp_path):
    with pytest.raises(ValueError):
        mod.ComfyUIEngine(FakeCommands()).align(_manifest(tmp_path), "install")


@pytest.mark.parametrize("attached", [False, True])
def test_run_aligns_then_submits_to_localhost(tmp_path, attached):
    commands, local = FakeCommands(), FakeLocal(attached)
    out = mod.ComfyUIEngine(commands).run({"manifest": _manifest(tmp_path), "workflow": {"1": {}}}, local)
    assert out == {"prompt_id": "p1"} and local.submitted == [("http://127.0.0.1:8188", {"1": {}})]
    assert local.started == (0 if attached else 1) and ("fetch", ["need.safetensors"]) in commands.calls


# Files that may still download a model. model_downloader and the engine_runner model steps
# stay until T5; asset_alignment is the Agent-side align until it calls VibeComfy fetch.
ALLOWED = {
    "astrid/core/generation/model_downloader.py",
    "astrid/core/generation/engine_runner.py",
    "astrid/core/generation/engine_execution.py",
    "astrid/core/generation/engine_contracts.py",
    "astrid/core/generation/asset_alignment.py",
    "astrid/packs/runpod/h3_release_remote.py",
    "astrid/packs/vibecomfy/production_engine.py",  # only a refused-key name, not a download
}
PATTERN = re.compile(r"download_model\b|_download_model\b|snapshot_download|hf_hub_download|download_models\b")


def test_no_code_path_outside_align_downloads_a_model():
    hits = sorted(str(p.relative_to(ROOT)) for p in (ROOT / "astrid").rglob("*.py")
                  if PATTERN.search(p.read_text(encoding="utf-8")) and str(p.relative_to(ROOT)) not in ALLOWED)
    assert hits == []
