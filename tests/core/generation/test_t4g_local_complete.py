"""Plan 32 T4g: the local run path is complete (align modes, the --job form, ASTRID_PROGRESS_PATH)."""
from __future__ import annotations

import importlib
import json
from types import SimpleNamespace

import pytest

from astrid.core.generation import cli
from tests.packs.vibecomfy.test_local_server_run import comfy  # noqa: F401  (CPU stand-in ComfyUI)


def _endpoint(comfy, monkeypatch):  # noqa: F811
    endpoint = SimpleNamespace(endpoint=SimpleNamespace(url=comfy.url), comfy_output_dir=str(comfy.output),
                               model_root_binding=None)
    monkeypatch.setattr("astrid.core.generation.engine_local_endpoint.local_engine_endpoint",
                        lambda *args, **kwargs: endpoint)


@pytest.mark.parametrize("mode", ["align", "check", "trust"])
def test_cli_accepts_all_three_align_modes(mode):
    parser = cli._parser()
    parsed = parser.parse_args(["image", "a cat", "--align", mode])
    assert parsed.align == mode
    from astrid.sdk.generation import _align_mode
    assert _align_mode(mode) == mode


def test_job_align_reaches_engine_align(comfy, tmp_path, monkeypatch):  # noqa: F811
    _endpoint(comfy, monkeypatch)
    action = importlib.import_module("astrid.packs.vibecomfy.actions.run.run")
    seen = []
    engine_cls = type(action._comfyui_engine())

    class Spy(engine_cls):
        def align(self, manifest, mode):
            seen.append((mode, manifest))
            return super().align(manifest, mode)

    monkeypatch.setattr(action, "_comfyui_engine", lambda: Spy())
    manifest = {"models": [], "extra": {"nodes": []}, "engine": "comfyui", "base": None}
    for mode in ("trust", "check"):
        action._run_and_settle(comfy.workflow, tmp_path / mode / "outputs", task_identity="t", attempt_identity="a",
                               align=mode, manifest=manifest)
    assert [row[0] for row in seen] == ["trust", "check"]
    assert seen[0][1] == {**manifest, "models_root": seen[0][1]["models_root"]}  # the install's models dir is added
    assert seen[0][1]["models_root"].endswith("/models")


def test_job_file_form_runs_end_to_end_on_the_cpu_standin(comfy, tmp_path, monkeypatch, capsys):  # noqa: F811
    _endpoint(comfy, monkeypatch)
    job = tmp_path / "job.json"
    job.write_text(json.dumps({"schema": "astrid.machine-job.v1", "entry": None, "mode": None, "align": "trust",
                               "task_identity": "task-t4g", "attempt_identity": "attempt-t4g",
                               "inputs": {"source": str(comfy.workflow)}}))
    out = tmp_path / "attempt" / "out"
    code = cli._main(["workflow", "--placement", "local", "--job", str(job), "--out", str(out), "--json"])
    assert code == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["managed_generation_result_path"]
    assert (out / printed["managed_generation_result_path"]).is_file()
    assert len(comfy.prompts) == 1


def test_job_file_rejects_a_foreign_document(tmp_path):
    from astrid.core.generation.local_job import JobFileError, read_job
    bad = tmp_path / "job.json"
    bad.write_text(json.dumps({"schema": "nope"}))
    with pytest.raises(JobFileError):
        read_job(bad)


def test_local_run_writes_astrid_progress_path(comfy, tmp_path, monkeypatch, capsys):  # noqa: F811
    _endpoint(comfy, monkeypatch)
    progress = tmp_path / "progress.json"
    monkeypatch.setenv("ASTRID_PROGRESS_PATH", str(progress))
    phases = []
    from astrid.core.generation import progress as module
    real = module.write_progress

    def spy(phase, *args, **kwargs):
        wrote = real(phase, *args, **kwargs)
        phases.append((phase, json.loads(progress.read_text())))
        return wrote

    monkeypatch.setattr(module, "write_progress", spy)
    for target in ("astrid.packs.vibecomfy.actions.run.run.write_progress",
                   "astrid.core.generation.cli.write_progress"):
        try:
            monkeypatch.setattr(target, spy)
        except AttributeError:
            pass
    job = tmp_path / "job.json"
    job.write_text(json.dumps({"schema": "astrid.machine-job.v1", "entry": None, "mode": None, "align": "check",
                               "inputs": {"source": str(comfy.workflow)}}))
    assert cli._main(["workflow", "--placement", "local", "--job", str(job),
                      "--out", str(tmp_path / "attempt" / "out"), "--json"]) == 0
    final = json.loads(progress.read_text())
    assert final == {"phase": "done", "percent": 100}
    names = [name for name, _ in phases]
    assert {"align", "start_engine", "run", "finalize"} <= set(names)
    assert not any(set(row) - {"phase", "percent", "current", "total"} for _, row in phases)


# -- warm reuse: the local run starts or reuses the engine's long-lived server ---------------------------


class _Supervisor:
    """A stand-in for the owned-server probe/start: one real child process, started once, never stopped."""

    def __init__(self):
        import subprocess
        import sys
        self._spawn = lambda: subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
        self.proc = None
        self.starts = 0

    def runner(self, **_kwargs):
        sup = self

        class Runner:
            step_id = None

            def start(self, readonly=False):
                if sup.proc is not None and sup.proc.poll() is None:  # probe: owned
                    return "skipped", {}
                sup.proc = sup._spawn()
                sup.starts += 1
                return "executed", {}

        return Runner()


@pytest.fixture
def installed_root(tmp_path):
    from astrid.core.generation.engine_local_endpoint import RECEIPT_RELATIVE_PATH
    receipt = tmp_path / "engine" / RECEIPT_RELATIVE_PATH
    receipt.parent.mkdir(parents=True)
    receipt.write_text("{}")
    return str(tmp_path / "engine")


def test_two_consecutive_local_jobs_hit_the_same_server_process(installed_root):
    from astrid.core.generation.engine_local_server import ensure_local_engine_server
    sup = _Supervisor()
    try:
        assert ensure_local_engine_server(root=installed_root, runner_factory=sup.runner) == "started"
        pid = sup.proc.pid
        assert ensure_local_engine_server(root=installed_root, runner_factory=sup.runner) == "reused"
        assert (sup.proc.pid, sup.starts) == (pid, 1)  # same pid, no restart
    finally:
        sup.proc and sup.proc.kill()


def test_a_stopped_server_is_started_by_a_job_and_reused_by_the_next(installed_root):
    from astrid.core.generation.engine_local_server import ensure_local_engine_server
    sup = _Supervisor()
    try:
        ensure_local_engine_server(root=installed_root, runner_factory=sup.runner)
        first = sup.proc.pid
        sup.proc.kill()
        sup.proc.wait()  # the server is stopped
        assert ensure_local_engine_server(root=installed_root, runner_factory=sup.runner) == "started"
        second = sup.proc.pid
        assert second != first and sup.starts == 2
        assert ensure_local_engine_server(root=installed_root, runner_factory=sup.runner) == "reused"
        assert sup.proc.pid == second and sup.starts == 2
    finally:
        sup.proc and sup.proc.kill()


def test_the_local_run_ensures_the_server_before_it_reads_the_endpoint(comfy, tmp_path, monkeypatch):  # noqa: F811
    _endpoint(comfy, monkeypatch)
    order = []
    monkeypatch.setattr("astrid.core.generation.engine_local_server.ensure_local_engine_server",
                        lambda *a, **k: order.append("ensure"))
    action = importlib.import_module("astrid.packs.vibecomfy.actions.run.run")
    action._run_and_settle(comfy.workflow, tmp_path / "attempt" / "outputs", task_identity="t", attempt_identity="a")
    assert order == ["ensure"] and len(comfy.prompts) == 1


# -- review fixes ----------------------------------------------------------------------------------------


def test_job_file_inputs_reach_the_run_as_file_arguments(tmp_path, monkeypatch):
    """A reference video/asset shipped in the job is passed on, not silently dropped."""
    from astrid.core.generation.local_job import run_job
    action = importlib.import_module("astrid.packs.vibecomfy.actions.run.run")
    seen = {}
    monkeypatch.setattr(action, "_run_and_settle", lambda workflow, out, **kw: seen.update(kw) or {})
    video, assets, workflow = tmp_path / "in" / "source_video.mp4", tmp_path / "in" / "managed_assets.zip", tmp_path / "wf.json"
    video.parent.mkdir()
    for path in (video, assets, workflow):
        path.write_bytes(b"x")
    job = tmp_path / "job.json"
    job.write_text(json.dumps({"schema": "astrid.machine-job.v1", "entry": None, "mode": None, "align": "check",
                               "inputs": {"source": str(workflow), "source_video": str(video),
                                          "managed_assets": str(assets), "prompt": "hi"}}))
    run_job("workflow", job, tmp_path / "out")
    assert seen["source_video"] == str(video) and seen["managed_assets"] == str(assets)
    assert json.loads(seen["workflow_inputs"]) == {"prompt": "hi"}


def test_the_plain_local_run_passes_align_and_the_requirements_manifest_to_align(comfy, tmp_path, monkeypatch):  # noqa: F811
    """host admission -> ASTRID_ALIGN / ASTRID_GENERATION_ENTRY -> run.main -> engine.align(manifest, mode)."""
    _endpoint(comfy, monkeypatch)
    action = importlib.import_module("astrid.packs.vibecomfy.actions.run.run")
    from astrid.core.generation import entries
    manifest = {"engine": "comfyui", "base": None, "models": [], "extra": {"nodes": []}, "marker": "from-requirements"}
    monkeypatch.setattr(entries, "find_entry", lambda entry_id: {"id": entry_id})
    monkeypatch.setattr(entries, "requirements", lambda entry, inputs=None: manifest)
    seen = []
    engine_cls = type(action._comfyui_engine())

    class Spy(engine_cls):
        def align(self, manifest, mode):
            seen.append((mode, manifest))
            return super().align(manifest, mode)

    monkeypatch.setattr(action, "_comfyui_engine", lambda: Spy())
    monkeypatch.setenv("ASTRID_ALIGN", "trust")
    monkeypatch.setenv("ASTRID_GENERATION_ENTRY", "some-entry")
    out = tmp_path / "attempt" / "outputs"
    assert action.main(["run", str(comfy.workflow), "--out", str(out), "--task-identity", "t"]) == 0
    assert [(mode, {k: v for k, v in m.items() if k != "models_root"}) for mode, m in seen] == [("trust", manifest)]
    assert seen[0][1]["models_root"].endswith("/models")


def test_the_host_admits_the_requested_align_mode_and_entry():
    from astrid.core.execution import generic_host
    for mode, expected in (("align", "align"), ("trust", "trust"), ("check", None)):
        task = {"generation_intent": {"metadata": {"model": "e1", "mode": "m", **({"align": mode} if mode != "check" else {})}}}
        admitted = generic_host._align_admission(task)
        assert admitted.get("align") == expected and admitted["entry"] == "e1"


def test_nothing_installed_is_a_clear_value(tmp_path):
    from astrid.core.generation.engine_local_server import ensure_local_engine_server
    assert ensure_local_engine_server(root=str(tmp_path / "none")) == "not_installed"


def test_the_real_runner_start_probe_reuses_then_restarts_a_cpu_standin_server(installed_root):
    """The real _Runner.start decides; only the target transport (service/helper/launch) is a CPU stand-in.

    The real service bridge needs the engine supervisor, whose own tests (test_engine_custody) already fail
    in this environment on the base commit, so the transport is the part replaced here.
    """
    import subprocess
    import sys
    from astrid.core.generation.engine_local_server import ensure_local_engine_server
    from astrid.core.generation.engine_runner import _Runner

    state = {"proc": None, "starts": 0, "ops": []}

    def alive():
        return state["proc"] is not None and state["proc"].poll() is None

    def factory(**_kwargs):
        runner = _Runner.__new__(_Runner)  # the real start(); no recipe/install needed for the probe logic
        runner.step_id, runner.service_owner = None, None
        runner.launch_spec = lambda: {"argv": ["server"], "cwd": "/", "env": {}}

        def helper(operation, *args, **kw):
            state["ops"].append(operation)
            return {"valid": True} if operation == "models-config-check" else {"written": True}

        def service(operation, *, launch=None, deadline=None):
            state["ops"].append(operation)
            if operation == "start":
                state["proc"] = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
                state["starts"] += 1
            return {"state": "owned" if alive() else "absent"}

        runner.helper, runner.service = helper, service
        return runner

    try:
        assert ensure_local_engine_server(root=installed_root, runner_factory=factory) == "started"
        pid = state["proc"].pid
        assert ensure_local_engine_server(root=installed_root, runner_factory=factory) == "reused"
        assert (state["proc"].pid, state["starts"]) == (pid, 1)
        state["proc"].kill()
        state["proc"].wait()
        assert ensure_local_engine_server(root=installed_root, runner_factory=factory) == "started"
        assert state["starts"] == 2 and "stop" not in state["ops"]  # a job never stops the server
    finally:
        state["proc"] and state["proc"].kill()


def test_a_plain_align_run_fetches_only_the_manifests_missing_rows(comfy, tmp_path, monkeypatch):  # noqa: F811
    """align uses the install's own models dir, so only rows missing there are fetched."""
    _endpoint(comfy, monkeypatch)
    action = importlib.import_module("astrid.packs.vibecomfy.actions.run.run")
    from astrid.core.generation import entries
    engine_root = tmp_path / "engine-root"
    present = engine_root / "models" / "checkpoints" / "have.safetensors"
    present.parent.mkdir(parents=True)
    present.write_bytes(b"12345")
    have = {"name": "have.safetensors", "subdir": "checkpoints", "url": "u1", "sha256": None, "size": 5}
    lack = {"name": "lack.safetensors", "subdir": "loras", "url": "u2", "sha256": None, "size": 9}
    manifest = {"engine": "comfyui", "base": None, "models": [have, lack], "extra": {"nodes": []}}
    monkeypatch.setattr("astrid.core.generation.engine_local_endpoint.default_engine_root", lambda engine: str(engine_root))
    monkeypatch.setattr(entries, "find_entry", lambda entry_id: {"id": entry_id})
    monkeypatch.setattr(entries, "requirements", lambda entry, inputs=None: manifest)
    calls = []

    class Commands:
        def doctor(self): calls.append(("doctor",))
        def prepare_deps_sync(self): calls.append(("prepare",))
        def nodes_restore(self, nodes): calls.append(("nodes", nodes))
        def fetch(self, rows): calls.append(("fetch", [row["name"] for row in rows]))

    engine_cls = type(action._comfyui_engine())
    monkeypatch.setattr(action, "_comfyui_engine", lambda: engine_cls(commands=Commands()))
    monkeypatch.setenv("ASTRID_ALIGN", "align")
    monkeypatch.setenv("ASTRID_GENERATION_ENTRY", "some-entry")
    out = tmp_path / "attempt" / "outputs"
    assert action.main(["run", str(comfy.workflow), "--out", str(out), "--task-identity", "t"]) == 0
    assert ("fetch", ["lack.safetensors"]) in calls and ("fetch", ["have.safetensors"]) not in calls
    assert [call for call in calls if call[0] == "fetch"] == [("fetch", ["lack.safetensors"])]


def test_the_job_path_applies_the_entrys_public_input_contract(tmp_path, monkeypatch):
    """Aliases and defaults of the entry's public inputs, not the bundle's own names, reach the run."""
    from astrid.core.generation import entries
    from astrid.core.generation.local_job import JobFileError, run_job
    action = importlib.import_module("astrid.packs.vibecomfy.actions.run.run")
    public = {"id": "named", "kind": "image", "source": "workflow", "modes": ["t2i"], "mode": "t2i",
              "inputs": [{"name": "prompt", "type": "string", "required": True},
                         {"name": "seed", "type": "integer", "required": False, "default": 42},
                         {"name": "paint_strength", "type": "number", "required": True, "aliases": ["texture"]}]}
    bundle = tmp_path / "bundle" / "workflow.py"
    bundle.parent.mkdir()
    bundle.write_text("")
    monkeypatch.setattr(entries, "describe_entry", lambda *a, **k: public)
    monkeypatch.setattr(entries, "find_entry", lambda entry_id: {"id": entry_id, "bundle": str(bundle)})
    seen = {}
    monkeypatch.setattr(action, "_run_and_settle", lambda workflow, out, **kw: seen.update(kw, workflow=workflow) or {})

    def write(inputs):
        job = tmp_path / "job.json"
        job.write_text(json.dumps({"schema": "astrid.machine-job.v1", "entry": "named", "mode": None,
                                   "align": "check", "inputs": inputs}))
        return job

    run_job("named", write({"prompt": "x", "texture": 0.5}), tmp_path / "out")
    assert json.loads(seen["workflow_inputs"]) == {"prompt": "x", "paint_strength": 0.5}
    assert seen["entry"] == "named" and seen["workflow"] == bundle
    with pytest.raises(JobFileError, match="not valid for entry"):
        run_job("named", write({"prompt": "x", "unknown_field": 1}), tmp_path / "out")
