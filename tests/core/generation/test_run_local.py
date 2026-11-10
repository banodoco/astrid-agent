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


@pytest.mark.parametrize("attached", [False, True])
def test_real_commands_align_the_jobs_workflow_without_changing_manifest(tmp_path, monkeypatch, attached):
    """The production command builder receives the executable path, not an absent manifest field."""
    import subprocess

    calls = []
    monkeypatch.setattr(subprocess, "run", lambda argv, **kwargs: calls.append(argv))
    manifest = {"engine": "comfyui", "base": None, "models": [], "extra": {"nodes": []}}
    workflow = tmp_path / "workflow.py"
    job = {"manifest": manifest, "workflow": workflow, "mode": "align"}
    engine = mod.ComfyUIEngine()  # real VibeComfyCommands; only the subprocess boundary is doubled
    if attached:
        job["endpoint"] = "http://127.0.0.1:8188"
        monkeypatch.setattr(engine, "probe", lambda endpoint: {"ready": True})
    engine.run(job, FakeLocal(attached))
    argv = [call[3:] for call in calls]
    assert argv[:2] == [
        ["doctor", str(workflow), "--non-interactive"],
        ["prepare", str(workflow), "--deps", "sync", "--no-models", "--no-packs", "--non-interactive"],
    ]
    assert "workflow" not in manifest
    assert job["manifest"] is manifest


def test_direct_align_keeps_supporting_a_manifest_workflow(tmp_path, monkeypatch):
    import subprocess

    calls = []
    monkeypatch.setattr(subprocess, "run", lambda argv, **kwargs: calls.append(argv))
    workflow = str(tmp_path / "workflow.py")
    mod.ComfyUIEngine().align({"workflow": workflow, "models": []}, "align")
    assert calls[0][3:5] == ["doctor", workflow]
    assert calls[1][3:5] == ["prepare", workflow]


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


def _pod_job_align(root, rows, mode="align"):
    """What the pod-side run does for one job: align the job's manifest against the pod disk."""
    commands = FakeCommands()
    manifest = {"models_root": str(root), "extra": {"nodes": []}, "models": rows}
    mod.ComfyUIEngine(commands).align(manifest, mode)
    return [c for c in commands.calls if isinstance(c, tuple)]


def test_pod_claim_fetches_only_the_rows_the_job_manifest_needs(tmp_path):
    rows = [{"name": "need.safetensors", "subdir": "vae", "size": 9}]  # the job's manifest, not the 32-row lock
    assert _pod_job_align(tmp_path, rows) == [("fetch", ["need.safetensors"])]


def _task(align):
    return {"generation_intent": {"metadata": {"model": "e", "mode": "t2i", **({"align": align} if align else {})}}}


def test_explicit_align_check_on_a_runpod_job_does_not_fetch(tmp_path):
    """--align check reaches the pod as check, not the pod default align, so a missing row is reported, never fetched."""
    from astrid.core.execution.generic_host import _machine_align_mode
    from astrid.core.generation import cli
    assert cli._parser().parse_args(["image", "a cat", "--align", "check"]).align == "check"  # explicit survives
    assert cli._parser().parse_args(["image", "a cat"]).align is None  # unasked stays distinguishable
    assert [_machine_align_mode(_task(m)) for m in ("check", "trust", "align", None)] == ["check", "trust", "align", "align"]
    rows = [{"name": "need.safetensors", "subdir": "vae", "size": 9}]
    assert _pod_job_align(tmp_path, rows, _machine_align_mode(_task("check"))) == []
    assert _pod_job_align(tmp_path, rows, _machine_align_mode(_task(None))) == [("fetch", ["need.safetensors"])]


def test_warm_pod_job_fetches_nothing(tmp_path):
    rows = [{"name": "need.safetensors", "subdir": "vae", "size": 9}]
    (tmp_path / "vae").mkdir()
    (tmp_path / "vae/need.safetensors").write_bytes(b"x" * 9)  # left by the first job on the warm pod
    assert _pod_job_align(tmp_path, rows) == []


def test_local_job_align_fetches_only_its_missing_row(tmp_path):
    commands = FakeCommands()  # _manifest: one row present, one missing
    mod.ComfyUIEngine(commands).align(_manifest(tmp_path), "align")
    assert [c for c in commands.calls if isinstance(c, tuple)] == [("fetch", ["need.safetensors"])]


# Files that may still download a model. Engine install is model-free (T5h): engine_runner,
# engine_execution and engine_contracts hold no download. align downloads only through VibeComfy
# fetch (asset_alignment is not listed). model_downloader stays only as the H3 release pod's
# verified downloader, which h3_release_remote imports.
ALLOWED = {
    "astrid/core/generation/model_downloader.py",
    "astrid/packs/runpod/h3_release_remote.py",
    "astrid/packs/vibecomfy/production_engine.py",  # only a refused-key name, not a download
}
PATTERN = re.compile(r"download_model\b|_download_model\b|snapshot_download|hf_hub_download|download_models\b")


def test_no_code_path_outside_align_downloads_a_model():
    hits = sorted(str(p.relative_to(ROOT)) for p in (ROOT / "astrid").rglob("*.py")
                  if PATTERN.search(p.read_text(encoding="utf-8")) and str(p.relative_to(ROOT)) not in ALLOWED)
    assert hits == []


def test_build_and_after_outputs_run_once_at_the_run_site(tmp_path):
    """Plan 32 T6: the engine's run site calls build before submit and after_outputs after it, once each."""
    events = []

    def build(inputs, files, out):
        events.append(("build", dict(inputs), dict(files), out))
        return {"workflow": {"graph": "built"}, "bindings": {}, "plan": "two steps"}

    def after_outputs(job_dir, inputs, files, outputs):
        events.append(("after_outputs", job_dir, dict(inputs), dict(files), outputs))
        return {**outputs, "after_outputs_ran": True}

    class Local(FakeLocal):
        def submit(self, url, workflow):
            events.append(("submit", workflow))
            return super().submit(url, workflow)

    job = {"manifest": _manifest(tmp_path), "mode": "check", "workflow": {"graph": "raw"},
           "inputs": {"prompt": "x"}, "files": {"ref": "/a/ref.png"}, "out": tmp_path / "out",
           "job_dir": tmp_path / "job", "hooks": {"build": build, "after_outputs": after_outputs}}
    local = Local(attached=True)
    result = mod.ComfyUIEngine(FakeCommands()).run(job, local)

    assert [e[0] for e in events] == ["build", "submit", "after_outputs"]
    assert events[0][1:] == ({"prompt": "x"}, {"ref": "/a/ref.png"}, tmp_path / "job")  # build and after_outputs share job_dir
    assert local.submitted == [("http://127.0.0.1:8188", {"graph": "built"})]
    assert result == {"prompt_id": "p1", "after_outputs_ran": True}

    # The dry run calls build only, and prints its plan.
    events.clear()
    dry = mod.ComfyUIEngine(FakeCommands()).dry_run(job)
    assert [e[0] for e in events] == ["build"] and dry["plan"] == "two steps"


def test_after_outputs_failure_fails_the_job_and_no_hooks_means_no_change(tmp_path):
    def boom(job_dir, inputs, files, outputs):
        raise RuntimeError("after_outputs failed")

    job = {"manifest": _manifest(tmp_path), "mode": "check", "workflow": {"graph": "raw"},
           "hooks": {"after_outputs": boom}}
    with pytest.raises(RuntimeError, match="after_outputs failed"):
        mod.ComfyUIEngine(FakeCommands()).run(job, FakeLocal(attached=True))
    plain = {**job, "hooks": {}}
    local = FakeLocal(attached=True)
    assert mod.ComfyUIEngine(FakeCommands()).run(plain, local) == {"prompt_id": "p1"}
    assert local.submitted[0][1] == {"graph": "raw"}


def test_core_names_neither_build_nor_after_outputs():
    core = (ROOT / "astrid/core").rglob("*.py")
    named = [str(p.relative_to(ROOT)) for p in core
             if re.search(r"\bhooks?\[['\"](build|after_outputs)['\"]\]|\.get\(['\"](build|after_outputs)['\"]", p.read_text())
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


def test_build_and_after_outputs_share_one_job_dir(tmp_path):
    seen = []
    hooks = {"build": lambda inputs, files, directory: seen.append(("build", directory)) or {"workflow": {"g": 1}},
             "after_outputs": lambda directory, inputs, files, outputs: seen.append(("after_outputs", directory)) or outputs}
    job = {"hooks": hooks, "out": tmp_path, "manifest": {"models": [], "extra": {"nodes": []}}, "mode": "check"}
    mod.ComfyUIEngine(FakeCommands()).run(job, FakeLocal(True))
    assert seen == [("build", tmp_path), ("after_outputs", tmp_path)]


def test_catalog_requirements_use_the_resolved_workflow_not_the_first_mode(tmp_path, monkeypatch):
    from astrid.core.generation import entries
    from astrid.packs.vibecomfy.actions.run import run as action

    monkeypatch.setattr(entries, "find_entry", lambda identifier: None)
    seen = []
    class Engine:
        def requirements(self, entry, inputs):
            seen.append((entry, inputs))
            return {"engine": "comfyui", "base": None, "models": [], "extra": {"nodes": []}}
    monkeypatch.setattr(entries, "engine_table", lambda: {"comfyui": Engine()})
    resolved = tmp_path / "selected-second-mode" / "workflow.py"
    manifest = action._entry_manifest("multi-mode-catalog", {"prompt": "selected"}, workflow_path=resolved)
    assert seen == [({"engine": "comfyui", "bundle": str(resolved)}, {"prompt": "selected"})]
    assert manifest["engine"] == "comfyui"
    with pytest.raises(ValueError, match="resolved workflow path"):
        action._entry_manifest("multi-mode-catalog", {})


def test_requirements_of_built_entries_read_selected_graph_without_rebuilding(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from astrid.core.generation import entries
    from astrid.packs.vibecomfy.actions.run import run as action

    def unexpected_build(*args, **kwargs):
        raise AssertionError("requirements reran the build hook")
    monkeypatch.setattr(entries, "find_entry", lambda identifier: {
        "id": identifier, "hooks": {"build": unexpected_build},
        "requirements": {"engine": "comfyui", "base": None, "models": ["stale"], "extra": {"nodes": []}}})
    workflows = {str(tmp_path / f"{name}.py"): SimpleNamespace(metadata={
        "model_assets": [{"name": f"{name}.bin", "subdir": "checkpoints", "sha256": "a" * 64, "size_bytes": 1}]})
        for name in ("first", "second")}
    monkeypatch.setattr(entries, "_load_entry_workflow", lambda entry: workflows[entry["bundle"]])
    engine = entries.engine_table()["comfyui"]
    for path, selected in workflows.items():
        dry = engine.requirements({"workflow": selected}, {})
        live = action._entry_manifest("input-dependent-hook", {}, workflow_path=Path(path))
        assert live == dry
        assert live["models"][0]["name"] == Path(path).with_suffix(".bin").name


def test_prepared_random_seed_is_fresh_recorded_and_replayable(monkeypatch):
    from types import SimpleNamespace
    from astrid.core.generation.entries import prepare_execution_bindings

    fresh = iter((123, 456))
    monkeypatch.setattr("secrets.randbelow", lambda limit: next(fresh))
    workflow = SimpleNamespace(inputs={"seed": object()})
    first = prepare_execution_bindings(workflow, {"prompt": "exact prompt"})
    second = prepare_execution_bindings(workflow, {"prompt": "exact prompt"})
    assert first == {"prompt": "exact prompt", "seed": 123}
    assert second == {"prompt": "exact prompt", "seed": 456}
    assert prepare_execution_bindings(workflow, first) == first
    assert prepare_execution_bindings(workflow, {"seed": 0}) == {"seed": 0}


def test_run_scoped_overrides_preserve_built_graph_and_only_namespace_declared_outputs():
    import copy
    from types import SimpleNamespace
    from astrid.packs.vibecomfy.local_server_engine import run_scoped_overrides

    class Workflow:
        def __init__(self):
            self.nodes = {"save": SimpleNamespace(inputs={"filename_prefix": "chosen", "images": ["decode", 0]}, widgets={}),
                          "other": SimpleNamespace(inputs={"filename_prefix": "untouched", "seed": 7}, widgets={})}
            self.outputs = [SimpleNamespace(node_id="save")]
            self.metadata = {"model_assets": [{"name": "chosen.bin"}]}
        def clone(self):
            return copy.deepcopy(self)
    built = Workflow()
    scoped = run_scoped_overrides(built, "attempt-123")
    assert built.nodes["save"].inputs["filename_prefix"] == "chosen"
    assert scoped.nodes["save"].inputs == {"filename_prefix": "chosen_astrid_attempt-123", "images": ["decode", 0]}
    assert scoped.nodes["other"].inputs == built.nodes["other"].inputs
    assert scoped.metadata == built.metadata


def test_run_scoped_prefix_survives_public_input_compilation():
    from vibecomfy.workflow import VibeNode, VibeOutput, VibeWorkflow, WorkflowSource
    from astrid.packs.vibecomfy.local_server_engine import run_scoped_overrides

    built = VibeWorkflow(id="scope", source=WorkflowSource(id="scope"),
                         nodes={"1": VibeNode(id="1", class_type="SaveImage",
                                              inputs={"filename_prefix": "chosen"})},
                         outputs=[VibeOutput(node_id="1", output_type="SaveImage")])
    built.register_input("filename_prefix", "1", "filename_prefix", "chosen", type="STRING")
    scoped = run_scoped_overrides(built, "attempt-123")
    assert scoped.compile("api")["1"]["inputs"]["filename_prefix"] == "chosen_astrid_attempt-123"
    assert built.compile("api")["1"]["inputs"]["filename_prefix"] == "chosen"
