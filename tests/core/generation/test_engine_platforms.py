import os
import time
from dataclasses import replace
from unittest.mock import patch

import pytest

from astrid.core.generation import engine_recipe, engine_runner
from astrid.core.generation.engine_contracts import DeadlineExceeded, RecipeValidationError, UnsafePathError, UnsupportedPlatform
from astrid.core.generation.engine_platforms import LinuxPlatformAdapter, platform_adapter

KEY = "linux-x86_64-cu130"
ROOT = "/TARGET_ONLY/engine"


@pytest.fixture
def recipe():
    return engine_recipe.load_recipe("vibecomfy-h3", KEY)


def test_keys():
    for suffix in ("cu124", "cu128", "cu130"):
        key = "linux-x86_64-" + suffix
        assert isinstance(platform_adapter(key), LinuxPlatformAdapter)
        assert platform_adapter(key).key == key
    for key in ("windows-x86_64-cu128", "windows-x86_64-cu130", "linux-x86_64-cu999"):
        with pytest.raises(UnsupportedPlatform):
            platform_adapter(key)


@pytest.mark.parametrize("root", ["/", "//host/a", "/a/", "/a//b", "/a/./b", "/a/../b", "a", "/a\\b", "/a\x00b", "/a:b"])
def test_unsafe_roots(root):
    with pytest.raises(UnsafePathError):
        platform_adapter(KEY).validate_root(root)


@pytest.mark.parametrize("child", ["", "/etc", "a//b", "a/../b", ".", "a/./b", "a\\b", "a\x00b", "a:"])
def test_unsafe_children(child):
    with pytest.raises(UnsafePathError):
        platform_adapter(KEY).join(ROOT, child)


def test_pure_target_paths_and_canonical_launch(recipe):
    adapter = platform_adapter(KEY)
    deadline = 123.5
    primary = next(e for e in recipe.environments if e.name == recipe.primary_environment)
    comfy = next(s for s in recipe.sources if s.name == "comfyui")
    values = {"install_root": ROOT, "python": ROOT + "/" + primary.relative_path + "/bin/python",
              "comfy_root": ROOT + "/" + comfy.relative_path, "models_root": ROOT + "/models",
              "models_config": ROOT + "/runtime/extra_model_paths.yaml", "host": "127.0.0.1",
              "port": str(recipe.start.port)}
    with patch("os.stat", side_effect=AssertionError("controller stat")), patch("os.open", side_effect=AssertionError("controller open")):
        assert adapter.venv_python(ROOT + "/runtime/venv") == ROOT + "/runtime/venv/bin/python"
        assert adapter.uv_executable(ROOT + "/runtime") == ROOT + "/runtime/bootstrap/bin/uv"
        assert adapter.environment(recipe, ROOT) == {"PYTHONDONTWRITEBYTECODE": "1", "UV_CACHE_DIR": ROOT + "/runtime/uv-cache"}
        assert adapter.environment_command_env(recipe, primary, ROOT, 0) == {"DEB_PYTHON_INSTALL_LAYOUT": "deb"}
        assert adapter.environment_command_env(recipe, primary, ROOT, 1) == {}
        request = adapter.start_request(recipe, ROOT, deadline=deadline)
        assert request.argv == tuple(v.format_map(values) for v in recipe.start.argv)
        assert request.cwd == recipe.start.cwd.format_map(values)
        assert request.env == {k: v.format_map(values) for k, v in recipe.start.env.items()}
        assert request.deadline == deadline
        runner = engine_runner._Runner.__new__(engine_runner._Runner)
        runner.adapter, runner.recipe, runner.deadline = adapter, recipe, deadline
        runner.plan = type("Plan", (), {"root": ROOT})()
        runner.step_id = None
        assert runner.launch_spec() == {"argv": list(request.argv), "cwd": request.cwd, "env": dict(request.env)}
        commands = adapter.environment_commands(recipe, primary, ROOT)
    assert commands == (("python3", "-m", "pip", "--isolated", "install", "--index-url", "https://pypi.org/simple", "--ignore-installed", "--prefix", ROOT + "/runtime/bootstrap", "uv==" + primary.uv_version),
                        (ROOT + "/runtime/bootstrap/bin/uv", "--no-config", "venv", "--allow-existing", "--python", primary.python_version, "--managed-python", ROOT + "/" + primary.relative_path))


def test_unknown_placeholder_rejected(recipe):
    modified = replace(recipe, start=replace(recipe.start, argv=("{unknown}",)))
    with pytest.raises(RecipeValidationError):
        platform_adapter(KEY).start_request(modified, ROOT, deadline=5)


def test_real_contention_deadline_and_release(tmp_path):
    adapter = platform_adapter(KEY)
    root = str(tmp_path / "new" / "root")
    with adapter.local_install_lock(root, deadline=time.monotonic() + 2):
        path = tmp_path / "new/root/.astrid-engine/install.lock"
        identity = path.stat().st_ino
        start = time.monotonic()
        with pytest.raises(DeadlineExceeded):
            with adapter.local_install_lock(root, deadline=start + 0.15):
                pytest.fail("admitted a second holder")
        assert 0.12 <= time.monotonic() - start < 1
        assert path.stat().st_ino == identity
    with adapter.local_install_lock(root, deadline=time.monotonic() + 2):
        assert path.stat().st_ino == identity


def test_symlinks_refused_without_cleanup(tmp_path):
    adapter = platform_adapter(KEY)
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(UnsafePathError):
        with adapter.local_install_lock(str(tmp_path / "link/root"), deadline=time.monotonic() + 1):
            pass
    assert list(outside.iterdir()) == []
    state = tmp_path / "root/.astrid-engine"
    state.mkdir(parents=True)
    foreign = tmp_path / "foreign"
    foreign.write_text("owned")
    (state / "install.lock").symlink_to(foreign)
    with pytest.raises(UnsafePathError):
        with adapter.local_install_lock(str(state.parent), deadline=time.monotonic() + 1):
            pass
    assert foreign.read_text() == "owned" and (state / "install.lock").is_symlink()


def test_replaced_inode_refused_without_removing_foreign(tmp_path, monkeypatch):
    from runtime_protocol import platform
    adapter = platform_adapter(KEY)
    root = tmp_path / "root"
    original = platform.lock_file
    def replace_before_lock(fd, **kwargs):
        path = root / ".astrid-engine/install.lock"
        path.rename(path.with_name("old.lock"))
        path.write_text("foreign")
        original(fd, **kwargs)
    monkeypatch.setattr(platform, "lock_file", replace_before_lock)
    with pytest.raises(UnsafePathError):
        with adapter.local_install_lock(str(root), deadline=time.monotonic() + 1):
            pytest.fail("admitted replaced lock")
    assert (root / ".astrid-engine/install.lock").read_text() == "foreign"
    assert (root / ".astrid-engine/old.lock").exists()
