"""Bounded installed import probes; no Runtime server, renderer, or sockets."""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import venv
from pathlib import Path

import pytest

from astrid.core.source_identity import _source_digest, source_checkout_closure
from astrid.sdk.host_bootstrap import PackHostBootstrapError, _installed_execution_origins


@pytest.fixture
def installed_pair(tmp_path):
    source = Path(__file__).resolve().parents[2]
    with tempfile.TemporaryDirectory(dir=tmp_path) as temporary:
        private = Path(temporary)
        environment = private / "venv"
        venv.EnvBuilder(with_pip=False, symlinks=False).create(environment)
        python = environment / "bin/python"
        site = next((environment / "lib").glob("python*/site-packages"))
        ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
        shutil.copytree(source / "astrid", site / "astrid", ignore=ignore)
        shutil.copy2(source / "pyproject.toml", site / "pyproject.toml")
        for name in ("astrid_runtime_client", "runtime_protocol", "astrid_runtime"):
            spec = importlib.util.find_spec(name)
            assert spec is not None and spec.origin
            shutil.copytree(Path(spec.origin).parent, site / name, ignore=ignore)
        # Only dependency files are borrowed read-only; no shared .pth is run.
        dependencies = Path(sys.executable).parent.parent / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages"
        (site / "test-dependencies.pth").write_text(str(dependencies) + "\n")
        neutral = private / "neutral"
        neutral.mkdir()
        env = {"HOME": str(private), "PATH": os.defpath, "LANG": "C.UTF-8",
               "PYTHONDONTWRITEBYTECODE": "1"}
        yield site, str(python), neutral, env


def _host_import(pair, digest):
    site, python, neutral, env = pair
    return subprocess.run(
        [python, "-I", "-B", "-m", "astrid.core.execution.generic_host", "--help",
         "--source-checkout", str(site), "--source-closure-digest", digest],
        cwd=neutral, env=env, text=True, capture_output=True, timeout=30,
    )


def test_exact_installed_pair_passes_real_origin_and_child_checks(installed_pair):
    site, python, neutral, env = installed_pair
    origins = _installed_execution_origins(site, python)
    assert set(origins) == {"host", "child", "action", "client", "sdk",
                            "runtime_protocol", "runtime_protocol.platform", "astrid_runtime"}
    assert all(Path(origin).is_relative_to(site) for origin in origins.values())
    closure = source_checkout_closure(site)
    assert {"runtime_protocol", "astrid_runtime"} <= {row["component"] for row in closure["components"]}
    child = _host_import(installed_pair, closure["digest"])
    assert child.returncode == 0, child.stderr
    assert "astrid-generic-host" in child.stdout
    actual = subprocess.run(
        [python, "-I", "-B", "-c",
         "from astrid.core.rendering import remotion_runtime; print(remotion_runtime.platform.__file__)"],
        cwd=neutral, env=env, text=True, capture_output=True, timeout=30,
    )
    assert actual.returncode == 0, actual.stderr
    assert Path(actual.stdout.strip()) == site / "runtime_protocol/platform.py"


@pytest.mark.parametrize("package", ["runtime_protocol", "astrid_runtime"])
def test_foreign_runtime_symlink_refused_before_package_code(installed_pair, package):
    site, python, neutral, _ = installed_pair
    digest = source_checkout_closure(site)["digest"]
    foreign = neutral / "foreign-runtime"
    shutil.move(site / package, foreign)
    marker = neutral / "foreign-imported"
    with (foreign / "__init__.py").open("a") as stream:
        stream.write(f"\nfrom pathlib import Path\nPath({str(marker)!r}).touch()\n")
    (site / package).symlink_to(foreign, target_is_directory=True)
    with pytest.raises(PackHostBootstrapError, match="actual installed"):
        _installed_execution_origins(site, python)
    with pytest.raises(ValueError, match="symlink"):
        source_checkout_closure(site)
    child = _host_import(installed_pair, digest)
    assert child.returncode != 0
    assert "installed Runtime" in child.stderr
    assert not marker.exists()


@pytest.mark.parametrize("packages", [("runtime_protocol",), ("astrid_runtime",), ("runtime_protocol", "astrid_runtime")])
def test_missing_required_runtime_refuses_visibly(installed_pair, packages):
    site, python, _, _ = installed_pair
    digest = source_checkout_closure(site)["digest"]
    for package in packages:
        shutil.rmtree(site / package)
    with pytest.raises(PackHostBootstrapError, match="actual installed"):
        _installed_execution_origins(site, python)
    child = _host_import(installed_pair, digest)
    assert child.returncode != 0
    assert "installed Runtime origin differs or is missing" in child.stderr


@pytest.mark.parametrize("substitution", ["source", "native_source", "symlink", "platform_symlink"])
def test_runtime_substitution_after_real_probe_fenced_at_child_import(installed_pair, substitution):
    site, python, neutral, _ = installed_pair
    _installed_execution_origins(site, python)
    agent_digest = _source_digest(site / "astrid")
    digest = source_checkout_closure(site)["digest"]
    marker = neutral / "foreign-imported"
    platform = site / ("astrid_runtime/bootstrap.py" if substitution == "native_source" else "runtime_protocol/platform.py")
    with platform.open("a") as stream:
        stream.write(f"\nfrom pathlib import Path\nPath({str(marker)!r}).touch()\n")
    if substitution == "symlink":
        foreign = neutral / "foreign-runtime"
        shutil.move(site / "runtime_protocol", foreign)
        (site / "runtime_protocol").symlink_to(foreign, target_is_directory=True)
    elif substitution == "platform_symlink":
        foreign = neutral / "foreign-platform.py"
        shutil.move(platform, foreign)
        platform.symlink_to(foreign)
    assert _source_digest(site / "astrid") == agent_digest
    if substitution in {"source", "native_source"}:
        assert source_checkout_closure(site)["digest"] != digest
    child = _host_import(installed_pair, digest)
    assert child.returncode != 0
    assert ("source closure mismatch" if substitution in {"source", "native_source"} else "symlink") in child.stderr
    assert not marker.exists()


def test_unrelated_isolated_agent_import_does_not_require_runtime(installed_pair):
    site, python, neutral, env = installed_pair
    for package in ("runtime_protocol", "astrid_runtime"):
        shutil.rmtree(site / package)
    result = subprocess.run(
        [python, "-I", "-B", "-c",
         "import astrid; import astrid.sdk; "
         "from astrid.core.source_identity import verify_installed_host_startup; "
         "verify_installed_host_startup(); print('offline-ok')", "-m"],
        cwd=neutral, env=env, text=True, capture_output=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "offline-ok"
