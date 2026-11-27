"""94-F: all seven rungs through the real public SDK, CLI and E classifier."""
import copy
import json
import subprocess
from dataclasses import replace

import pytest

from astrid.core.generation import cli, gpu_environment as gpu, preflight, target_launch, targets
from astrid.sdk.generation import GenerationFacade
from tests.core.generation.environment_states import snapshot
from tests.core.generation.test_desktop_rungs import installation_for
from tests.core.generation.test_generate_targets_desktop import scenario, URL, OTHER
from tests.core.generation.test_gpu_compute_probe import report, OTHER as SECOND_GPU, failed


OPS = ["matmul_fp32", "matmul_fp16"]


def _compute(facade=None, **kwargs):
    return (facade or GenerationFacade()).targets(
        through="computes", compute_device="cuda:1", required_operations=OPS, **kwargs)


@pytest.fixture
def selected(monkeypatch, tmp_path, scenario):
    state, _ = scenario
    state["installations"] = []
    monkeypatch.setattr(preflight, "_platform", lambda: "linux")
    root = tmp_path / "selected"
    root.mkdir()
    python = root / "venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_bytes(b"selected python fixture")
    python.chmod(0o755)
    app = root / "main.py"
    app.write_bytes(b"selected app")
    argv = (str(python), str(app), "--port", "8000")
    targets.save_target(targets.TargetRecord(
        layout="git", endpoint=URL, root=str(root), python=str(python),
        facts={"launch_app": str(app), "launch_argv": list(argv)}))
    process = target_launch.ProcessObservation(
        123, "boot:1", argv, str(root.resolve()), str(python.resolve()))
    state = {"listener": process, "changes": {}, "runs": [], "device": 1,
             "change_after_probe": False}

    class Observer:
        def listeners(self, endpoint, *, deadline):
            assert endpoint == URL
            return (state["listener"],)

        def process(self, pid, *, deadline):
            assert pid == 123
            return state["listener"]

        def _read(self, path, limit):
            assert str(path) == "/proc/123/environ"
            return b"CUDA_VISIBLE_DEVICES=2,3\0CUDA_DEVICE_ORDER=PCI_BUS_ID\0"

    monkeypatch.setattr(target_launch, "ProcLaunchObserver", Observer)
    monkeypatch.setattr(preflight, "_comfy_get_json", lambda url, path:
                        {"system": {"comfyui_version": "0.3"},
                         "devices": [{"type": "cuda", "index": state["device"]}]}
                        if path == "/system_stats" else {"ExampleNode": {}})
    real_probe = gpu.probe_compute

    def probe(executable, *, device, backend, required_operations, environ):
        assert executable == str(python) and device == "cuda:1" and backend == "cuda"
        assert list(required_operations) == OPS
        assert environ["CUDA_VISIBLE_DEVICES"] == "2,3"
        assert environ["CUDA_DEVICE_ORDER"] == "PCI_BUS_ID"

        def run(*args, **kwargs):
            state["runs"].append((args, kwargs))
            raw = report(required=OPS, **{
                "interpreter": str(python), "device": device, "devices": [SECOND_GPU],
                "cuda_visible_devices": environ["CUDA_VISIBLE_DEVICES"],
                "cuda_device_order": environ["CUDA_DEVICE_ORDER"], **state["changes"]})
            if state["change_after_probe"]:
                state["listener"] = replace(process, birth_id="boot:2")
            return subprocess.CompletedProcess([], 0, json.dumps(raw), "")

        return real_probe(executable, device=device, backend=backend,
                          required_operations=required_operations, environ=environ, run=run)

    monkeypatch.setattr(gpu, "probe_compute", probe)
    return state


def test_pass_selected_numeric_probe_and_cli(selected, tmp_path, generation_settings_store, capsys):
    facade = GenerationFacade()
    before = snapshot(tmp_path), copy.deepcopy(generation_settings_store.documents)
    assert facade.targets()["ladder"]["through"] == "compatible"
    assert selected["runs"] == []
    payload = _compute(facade)
    assert payload["status"] == "ready" and payload["next_action"] is None
    assert payload["ladder"]["through"] == "computes"
    rungs = payload["ladder"]["rungs"]
    assert len(rungs) == 7
    assert [r["state"] for r in rungs] == ["observed"] * 5 + ["unchecked"] * 2
    assert rungs[4]["observation"]["ok"] is True
    assert cli.main(["targets", "--through", "computes", "--compute-device", "cuda:1",
                     "--required-operation", OPS[0], "--required-operation", OPS[1], "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == payload
    assert len(selected["runs"]) == 2
    assert (snapshot(tmp_path), generation_settings_store.documents) == before


def test_actual_e_failure_stops_at_compute(selected):
    selected["changes"] = {"ok": False, "operations": {}, "error": failed(message="out of memory")}
    payload = _compute()
    assert payload["status"] == "blocked"
    assert payload["ladder"]["stopped_at"] == "computes"
    rungs = payload["ladder"]["rungs"]
    assert rungs[4]["state"] == "failed" and rungs[4]["reason"] == "out_of_memory"
    assert all(r["state"] == "unchecked" for r in rungs[5:])
    action = payload["next_action"]
    assert action["alternative"] is None
    assert action["then_argv"][:3] == ["astrid", "generate", "targets"]
    assert "--compute-device" in action["then_argv"]


@pytest.mark.parametrize("condition", ["wrong_listener", "wrong_device", "changed_after_probe", "windows"])
def test_unverified_binding_never_positive(selected, monkeypatch, condition):
    if condition == "wrong_listener":
        selected["listener"] = replace(selected["listener"], executable="/wrong/python")
    elif condition == "wrong_device":
        selected["device"] = 0
    elif condition == "changed_after_probe":
        selected["change_after_probe"] = True
    else:
        monkeypatch.setattr(preflight, "_platform", lambda: "win32")
    payload = _compute()
    rung = payload["ladder"]["rungs"][4]
    assert payload["status"] == "blocked"
    assert rung["state"] == "unchecked"
    assert rung["reason"] == ("compute_binding_changed" if condition == "changed_after_probe"
                              else "interpreter_binding_unverified")
    assert len(selected["runs"]) == (1 if condition == "changed_after_probe" else 0)


@pytest.mark.parametrize("installation", ["closed", "denied", "ambiguous", "wrong_responder", "wrong_install"])
def test_earlier_stop_preserves_p93_action(scenario, monkeypatch, installation):
    state, _ = scenario
    state["installations"] = [installation_for(installation)]
    monkeypatch.setattr(gpu, "probe_compute", lambda *a, **kw: pytest.fail("earlier rung stopped"))
    facade = GenerationFacade()
    old = facade.targets(placement=URL)
    payload = _compute(facade, placement=URL)
    assert payload["ladder"]["stopped_at"] == old["ladder"]["stopped_at"]
    assert payload["next_action"] == old["next_action"]
    assert payload["ladder"]["rungs"][4]["state"] == "unchecked"


def test_unbound_proposal_keeps_attach_consent(scenario, monkeypatch):
    monkeypatch.setattr(gpu, "probe_compute", lambda *a, **kw: pytest.fail("not selected"))
    facade = GenerationFacade()
    old = facade.targets()
    payload = _compute(facade)
    assert payload["next_action"] == old["next_action"]
    assert payload["next_action"]["requires_consent"] == "attach_existing"
    assert payload["ladder"]["rungs"][4]["reason"] == "selection_required"


@pytest.mark.parametrize("os_name,backend,reason", [
    ("linux", "cuda", "interpreter_required"), ("darwin", "mps", "backend_owned_elsewhere")])
def test_unknown_origin_or_mps_remains_compatible(scenario, monkeypatch, os_name, backend, reason):
    state, _ = scenario
    state["installations"] = []
    monkeypatch.setattr(preflight, "_platform", lambda: os_name)
    monkeypatch.setattr(preflight, "_comfy_get_json", lambda url, path:
                        {"system": {"comfyui_version": "0.3"}, "devices": []}
                        if path == "/system_stats" else {"ExampleNode": {}})
    monkeypatch.setattr(gpu, "probe_compute", lambda *a, **kw: pytest.fail("unknown origin"))
    facade = GenerationFacade()
    assert facade.targets(placement=OTHER)["status"] == "ready"
    payload = _compute(facade, placement=OTHER, compute_backend=backend)
    assert payload["ladder"]["rungs"][4]["reason"] == reason
    assert payload["ladder"]["rungs"][4]["state"] == "unchecked"
    assert payload["next_action"]["alternative"] is None
