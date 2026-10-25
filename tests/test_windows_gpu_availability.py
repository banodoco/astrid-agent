from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from astrid.core.execution.generic_host import GenericPackHost, HostError
from astrid.core.generation import gpu_environment as gpu
from tests.test_generic_host import FakeRuntime, _write_manifest


@pytest.fixture
def windows_gpu(monkeypatch):
    original = gpu.windows_gpu_status
    monkeypatch.setattr(gpu, "windows_gpu_status", lambda capability_id, **kwargs: original(capability_id, platform="win32", **kwargs))
    detected = {"available": True, "devices": ["NVIDIA GeForce RTX 4090"], "reason": ""}
    calls = []
    monkeypatch.setattr(gpu, "detect_nvidia", lambda: calls.append(1) or dict(detected))
    monkeypatch.delenv(gpu.ALLOW_UNQUALIFIED_ENV, raising=False)
    return detected, calls


@pytest.mark.parametrize("opted_in", [False, True])
def test_registered_availability_warns_without_blocking_unqualified_gpu(tmp_path, monkeypatch, windows_gpu, opted_in):
    for capability_id in ("wan2gp.generate_video", "vibecomfy.run", "vibecomfy.validate"):
        _write_manifest(tmp_path / capability_id, capability_id=capability_id)
    runtime = FakeRuntime()
    host = GenericPackHost(pack_roots=[tmp_path], client=runtime)
    if opted_in:
        monkeypatch.setenv(gpu.ALLOW_UNQUALIFIED_ENV, "1")
    host.register()
    registrations = {item["capability_id"]: item for item in runtime.registrations[-1][1]["capabilities"]}
    for capability_id in ("wan2gp.generate_video", "vibecomfy.run"):
        row = registrations[capability_id]
        assert row["status"] == "ready"
        assert row["unavailable_reason"].endswith("please tell us how it went (GPU model and driver from `nvidia-smi`).")
        assert host.capabilities[capability_id].manifest()["preflight"]["gpu"]["qualified"] is False
    assert registrations["vibecomfy.validate"]["status"] == "ready"
    assert len(windows_gpu[1]) == 1


def test_missing_gpu_warns_for_vibecomfy_but_still_blocks_wan2gp(tmp_path, monkeypatch, windows_gpu):
    windows_gpu[0].update(available=False, devices=[], reason="No NVIDIA GPU was detected.")
    monkeypatch.setenv(gpu.ALLOW_UNQUALIFIED_ENV, "1")
    _write_manifest(tmp_path / "comfy", capability_id="vibecomfy.run")
    _write_manifest(tmp_path / "wan", capability_id="wan2gp.generate_video")
    runtime = FakeRuntime()
    host = GenericPackHost(pack_roots=[tmp_path], client=runtime)
    host.register()
    rows = {item["capability_id"]: item for item in runtime.registrations[-1][1]["capabilities"]}
    assert rows["vibecomfy.run"]["status"] == "ready"
    assert rows["vibecomfy.run"]["unavailable_reason"] == "VibeComfy uses your own ComfyUI, so Astrid does not need to detect an NVIDIA GPU."
    assert rows["wan2gp.generate_video"]["status"] == "unavailable"
    assert rows["wan2gp.generate_video"]["unavailable_reason"] == "No NVIDIA GPU was detected."


def test_unqualified_label_survives_other_readiness_failures(tmp_path, monkeypatch, windows_gpu):
    path = _write_manifest(tmp_path / "comfy", capability_id="comfy_wrap.run")
    manifest = json.loads(path.read_text())
    manifest["metadata"]["required_packages"] = ["astrid_missing_gpu_dependency"]
    path.write_text(json.dumps(manifest))
    monkeypatch.setenv(gpu.ALLOW_UNQUALIFIED_ENV, "1")
    runtime = FakeRuntime()
    host = GenericPackHost(pack_roots=[tmp_path], client=runtime)
    host.register()
    row = runtime.registrations[-1][1]["capabilities"][0]
    assert row["status"] == "unavailable"
    assert row["unavailable_reason"].startswith("ComfyUI on Windows with this NVIDIA GPU is untested.")
    assert "astrid_missing_gpu_dependency" in row["unavailable_reason"]


@pytest.mark.parametrize("opted_in", [False, True])
def test_execution_runs_and_labels_settlement_without_opt_in(tmp_path, monkeypatch, windows_gpu, opted_in):
    packs = tmp_path / "packs"
    _write_manifest(packs / "comfy", capability_id="comfy_wrap.run")
    runtime = FakeRuntime()
    host = GenericPackHost(pack_roots=[packs], client=runtime, attempt_base=tmp_path / "attempts")
    host.discover()
    if opted_in:
        monkeypatch.setenv(gpu.ALLOW_UNQUALIFIED_ENV, "1")

    def run(record, inputs, output_root, attempt, **kwargs):
        # Exercise the host's real harvest/publication/settlement path without
        # launching a GPU process or a network service.
        (output_root / "answer.txt").write_text("fixture")
        return SimpleNamespace(process_id=1234, returncode=0,
                               payload={"gpu_qualification": {"qualified": True}})

    monkeypatch.setattr(host, "_run_command_definition", run)
    task = {"id": "gpu-test", "capability": "comfy_wrap.run", "project_id": "project",
            "attempt_id": "attempt", "fence": 1, "spec": {"spec": {"inputs": {}}}}
    runtime.tasks["gpu-test"] = task
    host.run_task(task, lease_token="lease")
    result = runtime.settlements[-1][2]["result"]
    assert result["gpu_qualification"]["qualified"] is False
    assert result["gpu_qualification"]["status"] == "not qualified yet"
    assert result["gpu_qualification"]["warning"].startswith("ComfyUI on Windows with this NVIDIA GPU is untested.")
    # ASTRID_ALLOW_UNQUALIFIED_GPU changes nothing; the dead opt-in is no longer reported.
    assert "opted_in" not in result["gpu_qualification"]


def test_vibecomfy_remains_ready_when_nvidia_detection_is_lost(tmp_path, monkeypatch, windows_gpu):
    _write_manifest(tmp_path / "comfy", capability_id="comfy_wrap.run")
    monkeypatch.setenv(gpu.ALLOW_UNQUALIFIED_ENV, "1")
    runtime = FakeRuntime()
    host = GenericPackHost(pack_roots=[tmp_path], client=runtime)
    host.register()
    assert host.capabilities["comfy_wrap.run"].ready
    windows_gpu[0].update(available=False, devices=[], reason="No NVIDIA GPU was detected.")
    host.preflight("comfy_wrap.run")
    assert host.capabilities["comfy_wrap.run"].ready
    assert host.capabilities["comfy_wrap.run"].preflight["gpu"]["warning"].startswith("VibeComfy uses your own ComfyUI")


def test_failed_opt_in_run_retains_unqualified_label(tmp_path, monkeypatch, windows_gpu):
    _write_manifest(tmp_path / "comfy", capability_id="comfy_wrap.run")
    monkeypatch.setenv(gpu.ALLOW_UNQUALIFIED_ENV, "1")
    runtime = FakeRuntime()
    host = GenericPackHost(pack_roots=[tmp_path], client=runtime)
    host.discover()

    def fail(*args, **kwargs):
        raise RuntimeError("GPU execution failed")

    monkeypatch.setattr(host, "_run_command_definition", fail)
    task = {"id": "failed", "capability": "comfy_wrap.run", "project_id": "project", "attempt_id": "failed-attempt", "fence": 1}
    runtime.tasks["failed"] = task
    with pytest.raises(RuntimeError, match="GPU execution failed"):
        host.run_task(task, lease_token="lease")
    assert runtime.failures[-1][2].endswith("ComfyUI on Windows with this NVIDIA GPU is untested. It will probably work, but please tell us how it went (GPU model and driver from `nvidia-smi`).")
