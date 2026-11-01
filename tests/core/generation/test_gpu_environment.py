from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from astrid.core.generation import gpu_environment as gpu

DEVICE = {"available": True, "devices": ["NVIDIA GeForce RTX 4090"], "reason": ""}


@pytest.mark.parametrize("pack", ["wan2gp", "vibecomfy"])
@pytest.mark.parametrize("platform", ["win32", "darwin", "linux"])
def test_plan_is_platform_specific_and_does_not_install(tmp_path, monkeypatch, pack, platform):
    monkeypatch.setattr(gpu.subprocess, "run", lambda *_a, **_k: pytest.fail("plan spawned a process"))
    plan = gpu.environment_plan(
        pack, tmp_path / "source", vibecomfy_checkout=tmp_path / "vibecomfy",
        platform=platform, environ={"LOCALAPPDATA": str(tmp_path / "user")},
    )
    assert not (tmp_path / "user").exists()
    assert plan["platform"] == platform
    if platform != "win32":
        assert plan["managed"] is False
        assert plan["commands"] == []
        return
    assert plan["managed"] is True
    assert Path(plan["python"]).parts[-2:] == ("Scripts", "python.exe")
    # GPU environments survive removal of the versioned Astrid program root.
    assert str(tmp_path / "user" / "Astrid GPU" / pack) in plan["root"]
    assert plan["qualified"] is False
    assert "--managed-python" in plan["commands"][0]
    compile_command = plan["commands"][1]
    assert "--generate-hashes" in compile_command
    assert "--exclude-newer" in compile_command
    assert str(tmp_path / "source" / "requirements.txt") in compile_command
    sync_command = plan["commands"][2]
    assert "--require-hashes" in sync_command
    assert plan["lock"] in sync_command
    for command in (compile_command, sync_command):
        assert command[command.index("--index-url") + 1] == "https://pypi.org/simple"
        assert command[command.index("--only-binary") + 1] == ":all:"
    if pack == "vibecomfy":
        assert len(plan["sources"]) == 4
        assert str(tmp_path / "vibecomfy" / "pyproject.toml") in compile_command
        for node in plan["description"]["custom_nodes"]:
            assert str(tmp_path / "source" / "custom_nodes" / node["name"] / "requirements.txt") in compile_command
        assert "--no-deps" in plan["commands"][3]


def test_environment_source_pins_match_existing_admission():
    from astrid.packs.wan2gp.src.driver import WAN2GP_PIN_SHA

    assert gpu._description("wan2gp")[1]["source"]["revision"] == WAN2GP_PIN_SHA
    for pack in ("wan2gp", "vibecomfy"):
        directory, description = gpu._description(pack)
        requirements = (directory / "requirements.windows-nvidia.txt").read_text()
        for name, version in description["torch"].items():
            filename = f"{name}-{version.replace('+', '%2B')}-cp312-cp312-win_amd64.whl"
            assert f"{name} @ https://download.pytorch.org/whl/{description['cuda_index']}/{filename}" in requirements
            assert version.endswith("+" + description["cuda_index"])


def test_comfy_plan_requires_admitted_vibecomfy_source(tmp_path):
    with pytest.raises(gpu.GPUEnvironmentError, match="VibeComfy checkout"):
        gpu.environment_plan("vibecomfy", tmp_path, platform="win32")


@pytest.mark.parametrize("capability", ["wan2gp.generate_video", "vibecomfy.run", "vibecomfy.generate_image", "vibecomfy.video_enhance", "comfy_wrap.run"])
@pytest.mark.parametrize("opt_in", ["", "true", "0", "1"])
def test_unqualified_label_is_a_warning_and_the_retired_opt_in_changes_nothing(capability, opt_in):
    status = gpu.windows_gpu_status(capability, platform="win32", environ={gpu.ALLOW_UNQUALIFIED_ENV: opt_in}, detection=DEVICE)
    assert status["ok"] is True
    assert status["qualified"] is False
    assert status["status"] == "not qualified yet"
    assert status["reason"].endswith("is not qualified yet.")
    label = "Wan2GP" if capability == "wan2gp.generate_video" else "ComfyUI"
    assert status["warning"] == (
        f"{label} on Windows with this NVIDIA GPU is untested. It will probably work, "
        "but please tell us how it went (GPU model and driver from `nvidia-smi`)."
    )
    assert "opted_in" not in status and "opt_in_env" not in status


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_other_platforms_do_not_probe_or_change_readiness(platform, monkeypatch):
    monkeypatch.setattr(gpu, "detect_nvidia", lambda: pytest.fail("non-Windows probe"))
    for capability in ("wan2gp.generate_video", "vibecomfy.run", "comfy_wrap.run"):
        assert gpu.windows_gpu_status(capability, platform=platform) is None


@pytest.mark.parametrize("capability", ["wan2gp.validate_settings", "vibecomfy.validate", "vibecomfy.import", "vibecomfy.inspect", "vibecomfy.edit", "generation.generate_video", "rendering.render"])
def test_offline_or_other_capabilities_do_not_need_a_gpu(capability, monkeypatch):
    monkeypatch.setattr(gpu, "detect_nvidia", lambda: pytest.fail("offline GPU probe"))
    assert gpu.windows_gpu_status(capability, platform="win32") is None


def test_qualification_table_is_specific_to_pack_os_and_gpu(monkeypatch):
    monkeypatch.setattr(gpu, "QUALIFIED_COMBINATIONS", frozenset({("wan2gp", "win32", DEVICE["devices"][0])}))
    status = gpu.windows_gpu_status("wan2gp.generate_video", platform="win32", environ={}, detection=DEVICE)
    assert status["qualified"] is True and status["ok"] is True
    assert status["reason"] == ""
    assert gpu.windows_gpu_status("vibecomfy.run", platform="win32", environ={}, detection=DEVICE)["ok"] is True
    other = {**DEVICE, "devices": ["NVIDIA GeForce RTX 3090"]}
    assert gpu.windows_gpu_status("wan2gp.generate_video", platform="win32", environ={}, detection=other)["ok"] is True


@pytest.mark.parametrize("capability", ["vibecomfy.run", "vibecomfy.generate_image", "comfy_wrap.run"])
def test_vibecomfy_readiness_does_not_require_detected_nvidia(capability):
    status = gpu.windows_gpu_status(
        capability, platform="win32", environ={},
        detection={"available": False, "devices": [], "reason": "No NVIDIA GPU was detected."},
    )
    assert status["ok"] is True
    assert status["qualified"] is False
    assert status["reason"] == "No NVIDIA GPU was detected."
    assert status["warning"] == "VibeComfy uses your own ComfyUI, so Astrid does not need to detect an NVIDIA GPU."


def test_detection_resolves_executable_and_parses_devices(monkeypatch):
    monkeypatch.setattr(gpu.shutil, "which", lambda name: r"C:\NVIDIA Tools\nvidia-smi.exe" if name == "nvidia-smi" else None)
    seen = []

    def run(argv, **kwargs):
        seen.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout="NVIDIA GeForce RTX 4090\r\nNVIDIA RTX A6000\r\n")

    monkeypatch.setattr(gpu.subprocess, "run", run)
    detected = gpu.detect_nvidia()
    assert detected["available"] is True
    assert detected["devices"] == ["NVIDIA GeForce RTX 4090", "NVIDIA RTX A6000"]
    assert seen[0][0] == [r"C:\NVIDIA Tools\nvidia-smi.exe", "--query-gpu=name", "--format=csv,noheader"]
    assert seen[0][1]["timeout"] == 5


@pytest.mark.parametrize("failure", ["missing", "timeout", "oserror", "exit", "empty"])
def test_detection_failures_are_plain_and_cannot_be_overridden(monkeypatch, failure):
    monkeypatch.setattr(gpu.shutil, "which", lambda _: None if failure == "missing" else "nvidia-smi.exe")

    def run(*args, **kwargs):
        if failure == "timeout":
            raise subprocess.TimeoutExpired("nvidia-smi", 5)
        if failure == "oserror":
            raise OSError("driver unavailable")
        return SimpleNamespace(returncode=1 if failure == "exit" else 0, stdout="")

    monkeypatch.setattr(gpu.subprocess, "run", run)
    detected = gpu.detect_nvidia()
    assert detected["available"] is False
    assert detected["devices"] == []
    assert "NVIDIA GPU" in detected["reason"]
    status = gpu.windows_gpu_status("vibecomfy.run", platform="win32", environ={gpu.ALLOW_UNQUALIFIED_ENV: "1"}, detection=detected)
    assert status["ok"] is True
    assert status["reason"] == detected["reason"]


def _provision_fixture(tmp_path, monkeypatch, pack="wan2gp"):
    from astrid.core.pack import source_setup

    plan = gpu.environment_plan(pack, tmp_path / "checkout", destination=tmp_path / "venv",
                                vibecomfy_checkout=tmp_path / "vibe", platform="win32")
    for source in plan["sources"]:
        path = Path(source["path"])
        path.mkdir(parents=True, exist_ok=True)
        (path / "requirements.txt").write_text("# pinned upstream fixture\n")
    if pack == "vibecomfy":
        vibe = tmp_path / "vibe"
        (vibe / "pyproject.toml").write_text('[build-system]\nrequires=["setuptools==80.9.0"]\n')
        (vibe / "custom_nodes.lock").write_text("\n".join(
            f'[nodepacks.{node["name"]}]\ncommit="{node["revision"]}"\n' for node in plan["description"]["custom_nodes"]
        ))
    heads = {source["path"]: source["revision"] for source in plan["sources"]}
    monkeypatch.setattr(source_setup, "_run_git", lambda _repo, *args: heads[args[1]] if "rev-parse" in args else "")
    monkeypatch.setattr(gpu.sys, "platform", "win32")
    monkeypatch.setattr(gpu, "detect_nvidia", lambda: dict(DEVICE))
    return plan


@pytest.mark.parametrize("pack", ["wan2gp", "vibecomfy"])
def test_provision_executes_plan_and_records_lock_without_qualifying(tmp_path, monkeypatch, pack):
    plan = _provision_fixture(tmp_path, monkeypatch, pack)
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        if "--version" in argv:
            return SimpleNamespace(stdout=f"uv {gpu.UV_VERSION} (test)")
        if "venv" in argv:
            Path(plan["root"]).mkdir()
        if "compile" in argv:
            Path(plan["lock"]).write_text("torch==test --hash=sha256:" + "a" * 64)
        assert "PIP_INDEX_URL" not in kwargs["env"]
        assert "UV_INDEX_URL" not in kwargs["env"]
        assert "PYTHONPATH" not in kwargs["env"]
        return SimpleNamespace(returncode=0)

    monkeypatch.setenv("PIP_INDEX_URL", "https://ambient.invalid")
    monkeypatch.setenv("UV_INDEX_URL", "https://ambient.invalid")
    monkeypatch.setenv("PYTHONPATH", "ambient")
    monkeypatch.setattr(gpu.subprocess, "run", run)
    receipt = gpu.provision_environment(plan)
    assert calls[1:] == plan["commands"]
    assert len(receipt["lock_sha256"]) == 64
    assert receipt["qualified"] is False
    assert receipt["status"] == "not qualified yet"
    assert json.loads((Path(plan["root"]) / "astrid-gpu-environment.json").read_text()) == receipt


@pytest.mark.parametrize("failure", ["revision", "dirty", "missing-requirements", "node-lock", "no-gpu"])
def test_provision_rejects_bad_sources_and_hardware_before_install(tmp_path, monkeypatch, failure):
    from astrid.core.pack import source_setup

    plan = _provision_fixture(tmp_path, monkeypatch, "vibecomfy")
    if failure == "revision":
        monkeypatch.setattr(source_setup, "_run_git", lambda *_a, **_k: "wrong-revision")
    elif failure == "dirty":
        original = source_setup._run_git
        monkeypatch.setattr(source_setup, "_run_git", lambda *a, **k: " M requirements.txt" if "status" in a else original(*a, **k))
    elif failure == "missing-requirements":
        Path(plan["requirements"][-2]).unlink()
    elif failure == "node-lock":
        (tmp_path / "vibe" / "custom_nodes.lock").write_text("[nodepacks]\n")
    else:
        monkeypatch.setattr(gpu, "detect_nvidia", lambda: {"available": False, "devices": [], "reason": "No NVIDIA GPU was detected."})
    monkeypatch.setattr(gpu.subprocess, "run", lambda *_a, **_k: pytest.fail("installed despite failed preflight"))
    with pytest.raises(gpu.GPUEnvironmentError):
        gpu.provision_environment(plan)
    assert not Path(plan["root"]).exists()


def test_failed_install_does_not_emit_success_receipt(tmp_path, monkeypatch):
    plan = _provision_fixture(tmp_path, monkeypatch)

    def run(argv, **kwargs):
        if "--version" in argv:
            return SimpleNamespace(stdout=f"uv {gpu.UV_VERSION}")
        Path(plan["root"]).mkdir()
        raise subprocess.CalledProcessError(1, argv)

    monkeypatch.setattr(gpu.subprocess, "run", run)
    with pytest.raises(gpu.GPUEnvironmentError, match="not admitted"):
        gpu.provision_environment(plan)
    assert not (Path(plan["root"]) / "astrid-gpu-environment.json").exists()


def test_existing_environment_is_not_overwritten(tmp_path, monkeypatch):
    plan = _provision_fixture(tmp_path, monkeypatch)
    Path(plan["root"]).mkdir()
    monkeypatch.setattr(gpu.subprocess, "run", lambda *_a, **_k: pytest.fail("modified existing environment"))
    with pytest.raises(gpu.GPUEnvironmentError, match="already exists"):
        gpu.provision_environment(plan)


@pytest.mark.parametrize("failure", ["cpu-wheel", "wrong-cuda", "no-cuda", "healthy"])
def test_post_install_probe_checks_exact_cuda_build_and_usable_device(monkeypatch, capsys, failure):
    versions = gpu._description("wan2gp")[1]["torch"]
    modules = {name: SimpleNamespace(__version__=version) for name, version in versions.items()}
    if failure == "cpu-wheel":
        modules["torchvision"].__version__ = versions["torchvision"].split("+")[0]
    modules["torch"].version = SimpleNamespace(cuda="12.1" if failure == "wrong-cuda" else "12.8")
    modules["torch"].cuda = SimpleNamespace(
        is_available=lambda: failure != "no-cuda", device_count=lambda: 1,
        get_device_name=lambda _: DEVICE["devices"][0],
    )
    monkeypatch.setattr(gpu.sys, "argv", ["probe", json.dumps(versions)])
    with patch.dict("sys.modules", modules):
        if failure == "healthy":
            exec(gpu._TORCH_PROBE, {})
            report = json.loads(capsys.readouterr().out)
            assert report["qualified"] is False
            assert report["devices"] == DEVICE["devices"]
        else:
            message = {"cpu-wheel": "CUDA wheel versions differ", "wrong-cuda": "CUDA runtime version differs", "no-cuda": "No NVIDIA GPU"}[failure]
            with pytest.raises(SystemExit, match=message):
                exec(gpu._TORCH_PROBE, {})


def test_solver_version_must_match_before_environment_creation(tmp_path, monkeypatch):
    plan = _provision_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(gpu.subprocess, "run", lambda *_a, **_k: SimpleNamespace(stdout="uv 0.0.1"))
    with pytest.raises(gpu.GPUEnvironmentError, match="requires uv"):
        gpu.provision_environment(plan)
    assert not Path(plan["root"]).exists()
