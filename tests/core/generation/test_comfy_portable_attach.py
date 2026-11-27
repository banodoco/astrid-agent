"""Proof of attaching a finished Portable install without launching it."""

from pathlib import Path

import pytest

from astrid.core.generation import comfy_portable, settings, target_launch, targets


@pytest.fixture
def pin():
    return {
        "schema": "astrid.comfy-portable-pin.v1",
        "platform": "win32",
        "note": "Synthetic release for unit tests only.",
        "supported_gpu_patterns": [r"RTX (20|30|40|50)\d\d"],
        "release": {
            "tag": "test-release",
            "url": "https://example.invalid/portable.zip",
            "archive_name": "portable.zip",
            "archive_format": "zip",
            "archive_bytes": 100,
            "archive_sha256": "a" * 64,
            "extracted_bytes": 200,
            "estimated_minutes": 1,
            "inner_directory": "ComfyUI_windows_portable",
            "required_files": ["python_embeded/python.exe", "ComfyUI/main.py"],
            "start_script": "run_nvidia_gpu.bat",
            "launch_args": ["--listen", "127.0.0.1"],
        },
    }


@pytest.fixture
def installed(tmp_path, pin):
    root = tmp_path / pin["release"]["inner_directory"]
    comfy_dir = root / "ComfyUI"
    python = root / "python_embeded" / "python.exe"
    for path, contents in ((comfy_dir / "main.py", "print('comfy')\n"),
                           (python, "fake Python executable\n"),
                           (root / pin["release"]["start_script"], "@echo off\n")):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")
    python.chmod(0o755)
    return {
        "status": "installed",
        "resumed_from": None,
        "root": str(root),
        "comfy_dir": str(comfy_dir),
        "python": str(python),
        "release": pin["release"]["tag"],
        "kept_previous": None,
    }


def _old_choice(tmp_path, *, prefer=None):
    previous = targets.TargetRecord(
        layout="desktop", endpoint="http://127.0.0.1:8190",
        root=str(tmp_path / "previous"), prefer=prefer,
    )
    targets.save_target(previous)
    placement = settings.set_placement("http://127.0.0.1:8190")["placement"]
    return previous.as_dict(), placement


@pytest.mark.parametrize("earlier", ["none", "desktop", "preferred-desktop"])
def test_attach_retains_choice_and_exact_launch_facts(tmp_path, installed, pin, earlier):
    if earlier == "none":
        previous_target, previous_placement = None, {}
        assert targets.read_target() is None
        assert settings.placement_default() == {}
    else:
        previous_target, previous_placement = _old_choice(
            tmp_path, prefer="attached" if earlier == "preferred-desktop" else None,
        )
    port = 8191
    endpoint = f"http://127.0.0.1:{port}"
    app = str(Path(installed["comfy_dir"]) / "main.py")
    script = str(Path(installed["root"]) / pin["release"]["start_script"])
    argv = [installed["python"], "-s", app, *pin["release"]["launch_args"],
            "--port", str(port)]
    expected = targets.TargetRecord(
        engine="comfyui", location="local", owner="human", layout="portable",
        endpoint=endpoint, root=installed["comfy_dir"], python=installed["python"],
        facts={
            "installed_by": "astrid", "release": installed["release"],
            "start_script": script, "launch_app": app, "launch_argv": argv,
        },
        verified_at=None,
        prefer="attached" if earlier == "preferred-desktop" else None,
    )
    placement = {"kind": "attached", "endpoint": endpoint}

    attached = comfy_portable.attach_installation(installed, pin=pin, port=port)

    assert attached == {
        "target": expected.as_dict(),
        "placement": placement,
        "previous_target": previous_target,
        "previous_placement": previous_placement,
        "undo": "astrid generate settings unset placement",
        "start": {
            "who": "person", "script": script,
            "text": f"Start ComfyUI by opening {script}. Then ask me to check again.",
        },
    }
    record = targets.read_target()
    assert record == expected
    assert settings.placement_default() == placement
    plan = target_launch.prepare_target_launch(record)
    assert plan.argv == tuple(argv)
    assert plan.endpoint == endpoint
    assert plan.root == installed["comfy_dir"]
    assert plan.python == installed["python"]

    config = targets.config_path()
    before = config.read_bytes(), config.stat().st_mtime_ns
    version = settings.read().version
    again = comfy_portable.attach_installation(installed, pin=pin, port=port)
    assert again == {**attached, "previous_target": expected.as_dict(),
                     "previous_placement": placement}
    assert targets.read_target() == expected
    assert settings.placement_default() == placement
    assert (config.read_bytes(), config.stat().st_mtime_ns) == before
    assert settings.read().version == version


@pytest.mark.parametrize("relative", ["python_embeded/python.exe", "ComfyUI/main.py"])
@pytest.mark.parametrize("broken", ["missing", "empty"])
@pytest.mark.parametrize("earlier", [False, True])
def test_attach_refuses_uninstalled_without_writes(
    tmp_path, installed, pin, monkeypatch, relative, broken, earlier,
):
    previous_target, previous_placement = _old_choice(tmp_path) if earlier else (None, {})
    path = Path(installed["root"]) / relative
    if broken == "missing":
        path.unlink()
    else:
        path.write_bytes(b"")

    def unexpected_write(*args, **kwargs):
        pytest.fail("An unfinished install must not write either setting.")

    monkeypatch.setattr(targets, "save_target", unexpected_write)
    monkeypatch.setattr(settings, "set_placement", unexpected_write)
    with pytest.raises(comfy_portable.PortableError) as caught:
        comfy_portable.attach_installation(installed, pin=pin)
    assert caught.value.code == "not_installed"
    actual = targets.read_target()
    assert (actual.as_dict() if actual else None) == previous_target
    assert settings.placement_default() == previous_placement
