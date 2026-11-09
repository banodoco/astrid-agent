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
# stay until T5; align downloads only through VibeComfy fetch (asset_alignment is not listed).
ALLOWED = {
    "astrid/core/generation/model_downloader.py",
    "astrid/core/generation/engine_runner.py",
    "astrid/core/generation/engine_execution.py",
    "astrid/core/generation/engine_contracts.py",
    "astrid/packs/runpod/h3_release_remote.py",
    "astrid/packs/vibecomfy/production_engine.py",  # only a refused-key name, not a download
}
PATTERN = re.compile(r"download_model\b|_download_model\b|snapshot_download|hf_hub_download|download_models\b")


def test_no_code_path_outside_align_downloads_a_model():
    hits = sorted(str(p.relative_to(ROOT)) for p in (ROOT / "astrid").rglob("*.py")
                  if PATTERN.search(p.read_text(encoding="utf-8")) and str(p.relative_to(ROOT)) not in ALLOWED)
    assert hits == []


def test_build_and_finalize_run_once_at_the_run_site(tmp_path):
    """Plan 32 T6: the engine's run site calls build before submit and finalize after it, once each."""
    events = []

    def build(inputs, files, out):
        events.append(("build", dict(inputs), dict(files), out))
        return {"workflow": {"graph": "built"}, "bindings": {}, "plan": "two steps"}

    def finalize(job_dir, inputs, files, outputs):
        events.append(("finalize", job_dir, dict(inputs), dict(files), outputs))
        return {**outputs, "finalized": True}

    class Local(FakeLocal):
        def submit(self, url, workflow):
            events.append(("submit", workflow))
            return super().submit(url, workflow)

    job = {"manifest": _manifest(tmp_path), "mode": "check", "workflow": {"graph": "raw"},
           "inputs": {"prompt": "x"}, "files": {"ref": "/a/ref.png"}, "out": tmp_path / "out",
           "job_dir": tmp_path / "job", "hooks": {"build": build, "finalize": finalize}}
    local = Local(attached=True)
    result = mod.ComfyUIEngine(FakeCommands()).run(job, local)

    assert [e[0] for e in events] == ["build", "submit", "finalize"]
    assert events[0][1:] == ({"prompt": "x"}, {"ref": "/a/ref.png"}, tmp_path / "job")  # build and finalize share job_dir
    assert local.submitted == [("http://127.0.0.1:8188", {"graph": "built"})]
    assert result == {"prompt_id": "p1", "finalized": True}

    # The dry run calls build only, and prints its plan.
    events.clear()
    dry = mod.ComfyUIEngine(FakeCommands()).dry_run(job)
    assert [e[0] for e in events] == ["build"] and dry["plan"] == "two steps"


def test_finalize_failure_fails_the_job_and_no_hooks_means_no_change(tmp_path):
    def boom(job_dir, inputs, files, outputs):
        raise RuntimeError("finalize failed")

    job = {"manifest": _manifest(tmp_path), "mode": "check", "workflow": {"graph": "raw"},
           "hooks": {"finalize": boom}}
    with pytest.raises(RuntimeError, match="finalize failed"):
        mod.ComfyUIEngine(FakeCommands()).run(job, FakeLocal(attached=True))
    plain = {**job, "hooks": {}}
    local = FakeLocal(attached=True)
    assert mod.ComfyUIEngine(FakeCommands()).run(plain, local) == {"prompt_id": "p1"}
    assert local.submitted[0][1] == {"graph": "raw"}


def test_core_names_neither_build_nor_finalize():
    core = (ROOT / "astrid/core").rglob("*.py")
    named = [str(p.relative_to(ROOT)) for p in core
             if re.search(r"\bhooks?\[['\"](build|finalize)['\"]\]|\.get\(['\"](build|finalize)['\"]", p.read_text())
             and p.name != "workflows.py"]
    assert named == []


def test_vibecomfy_argv_parses_with_the_real_cli():
    """The argv is not assumed: each step must parse with VibeComfy's own parser."""
    cli = pytest.importorskip("vibecomfy.cli")
    parser = cli.build_parser()
    commands = mod.VibeComfyCommands("wf.json", "custom_nodes.lock")
    parsed = {step: parser.parse_args(commands.argv(step)) for step in ("doctor", "prepare", "nodes_restore", "fetch")}
    assert parsed["prepare"].deps == "sync" and parsed["prepare"].no_models and parsed["prepare"].no_packs
    assert parsed["fetch"].workflow == "wf.json" and not parsed["fetch"].force
    assert parsed["nodes_restore"].lockfile == "custom_nodes.lock"


def test_build_and_finalize_share_one_job_dir(tmp_path):
    seen = []
    hooks = {"build": lambda inputs, files, directory: seen.append(("build", directory)) or {"workflow": {"g": 1}},
             "finalize": lambda directory, inputs, files, outputs: seen.append(("finalize", directory)) or outputs}
    job = {"hooks": hooks, "out": tmp_path, "manifest": {"models": [], "extra": {"nodes": []}}, "mode": "check"}
    mod.ComfyUIEngine(FakeCommands()).run(job, FakeLocal(True))
    assert seen == [("build", tmp_path), ("finalize", tmp_path)]
