"""Generated Runtime launchers stay independent of the selected Agent package."""

from __future__ import annotations

import ast
import io
import json
import os
import runpy
import subprocess
import sys
import tarfile
import venv
from pathlib import Path
from types import ModuleType

import pytest

from astrid.installer import launcher
from astrid.installer.core import current, write_json
from astrid.installer.service import LABEL, command

RELEASE0_AGENT = "3204bb00135bec8ee2d2d3fb21eed5abfceca649"
RELEASE0_MANIFEST = Path(__file__).parent / "fixtures" / "release0" / "served" / "install-manifest.json"
LEGACY_VERSION = "3204bb00-6d53a190-legacy01"


def _legacy_installation(tmp_path: Path, python: Path) -> tuple[Path, Path, Path]:
    root, data = tmp_path / "program", tmp_path / "workspace"
    version_root = root / "versions" / LEGACY_VERSION
    version_root.mkdir(parents=True, exist_ok=True)
    profile = version_root / "profile.json"
    profile.write_text("{}\n", encoding="utf-8")
    (version_root / "manifest.json").write_bytes(RELEASE0_MANIFEST.read_bytes())
    marker = tmp_path / "runtime-call.json"
    # Authentic release 0 wrote this four-field shape. It had no identity
    # member and its Version class had no release/managed_by fields.
    write_json(version_root / "version.json", {
        "root": str(version_root),
        "python": str(python),
        "profile": str(profile),
        "env": {"M1_5A_RUNTIME_MARKER": str(marker)},
    })
    write_json(root / "install.json", {
        "owner": LABEL, "managed_by": "pinokio", "data_root": str(data),
    })
    write_json(root / "service-env.json", {})
    (root / "current").write_text(version_root.name + "\n", encoding="utf-8")
    return root, data, marker


def _agent_imports(source: str) -> list[str]:
    names = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            names.append(node.module)
    return sorted(name for name in names if name == "astrid" or name.startswith("astrid."))


def test_generated_launch_preludes_have_no_installed_agent_imports(tmp_path: Path) -> None:
    root, data, _marker = _legacy_installation(tmp_path, Path(sys.executable))
    selected = current(root)
    assert selected is not None
    service_source = command(selected, data, env_file=root / "service-env.json")[-1]
    foreground_source = launcher.publish_runtime_runner(root).read_text(encoding="utf-8")

    assert _agent_imports(service_source) == []
    assert _agent_imports(foreground_source) == []
    assert 'import_module("astrid' not in foreground_source
    for source in (service_source, foreground_source):
        assert "from astrid." not in source
        assert "import astrid." not in source
    assert "from astrid_runtime.cli import main" in service_source
    assert "from astrid_runtime.cli import main" in foreground_source


def _release0_environment(environment: Path) -> tuple[Path, Path]:
    venv.EnvBuilder(with_pip=False, system_site_packages=True).create(environment)
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    site_packages = (
        environment / "Lib" / "site-packages" if os.name == "nt"
        else next((environment / "lib").glob("python*/site-packages"))
    )
    archive = subprocess.run(
        ["git", "archive", "--format=tar", RELEASE0_AGENT, "astrid"],
        check=True, stdout=subprocess.PIPE,
    ).stdout
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as source:
        source.extractall(site_packages, filter="data")
    # The selected Runtime supplies its protocol package to isolated Python.
    import shutil
    import runtime_protocol

    shutil.copytree(Path(runtime_protocol.__file__).parent, site_packages / "runtime_protocol")
    runtime = site_packages / "astrid_runtime"
    runtime.mkdir(exist_ok=True)
    (runtime / "__init__.py").write_text("", encoding="utf-8")
    (runtime / "cli.py").write_text(
        "import json,os\n"
        "from pathlib import Path\n"
        "def main(argv):\n"
        "    Path(os.environ['M1_5A_RUNTIME_MARKER']).write_text(json.dumps({\n"
        "        'argv': argv, 'pid': os.getpid(),\n"
        "        'release': os.environ.get('ASTRID_INSTALL_RELEASE'),\n"
        "        'managed_by': os.environ.get('ASTRID_INSTALL_MANAGED_BY'),\n"
        "    }))\n"
        "    return 0\n",
        encoding="utf-8",
    )
    return python, site_packages


@pytest.mark.posix_native
def test_service_and_foreground_exec_with_authentic_release0_package(
    tmp_path: Path,
) -> None:
    python, site_packages = _release0_environment(
        tmp_path / "program" / "versions" / LEGACY_VERSION / "py"
    )
    assert (site_packages / "astrid" / "installer" / "core.py").is_file()
    release0_core = (site_packages / "astrid" / "installer" / "core.py").read_text(encoding="utf-8")
    version_class = next(
        node for node in ast.parse(release0_core).body
        if isinstance(node, ast.ClassDef) and node.name == "Version"
    )
    assert [
        node.target.id for node in version_class.body
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
    ] == ["root", "python", "profile", "env"]

    root, data, marker = _legacy_installation(tmp_path, python)
    selected = current(root)
    assert selected is not None and selected.release == 0 and selected.managed_by == "pinokio"
    environment = dict(os.environ)
    environment.update({
        "HOME": str(tmp_path / "home"),
        "XDG_DATA_HOME": str(tmp_path / "xdg-data"),
    })

    service = subprocess.run(
        command(selected, data, env_file=root / "service-env.json"),
        check=False, capture_output=True, text=True, env=environment, timeout=30,
    )
    assert service.returncode == 0, service.stderr
    service_call = json.loads(marker.read_text(encoding="utf-8"))
    assert service_call["argv"] == [
        "run", "--profile", "astrid", "--data-root", str(data),
        "--source-manifest", str(selected.profile),
    ]
    assert service_call["release"] == "0"
    assert service_call["managed_by"] == "pinokio"

    launcher.install(root)
    foreground = subprocess.run(
        [str(root / "run"), "--hosted-origin", "off"],
        check=False, capture_output=True, text=True, env=environment, timeout=30,
    )
    assert foreground.returncode == 0, foreground.stderr
    foreground_call = json.loads(marker.read_text(encoding="utf-8"))
    assert foreground_call["argv"][-2:] == ["--hosted-origin", "off"]
    assert foreground_call["release"] == "0"
    assert foreground_call["managed_by"] == "pinokio"


def test_generated_windows_foreground_runs_release0_shape_in_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, _data, _marker = _legacy_installation(tmp_path, Path(sys.executable))
    runner = launcher.publish_runtime_runner(root)
    calls: list[tuple[list[str], int]] = []
    runtime = ModuleType("astrid_runtime.cli")
    runtime.main = lambda argv: calls.append((list(argv), os.getpid())) or 0  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "astrid_runtime.cli", runtime)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setattr(os, "environ", dict(os.environ))
    monkeypatch.setattr("runtime_protocol.platform.private_directory", lambda path: path.mkdir(parents=True, exist_ok=True))
    monkeypatch.setattr("runtime_protocol.platform.lock_file", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("runtime_protocol.platform.unlock_file", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(os, "chdir", lambda _path: None)
    monkeypatch.setattr(sys, "argv", [str(runner), "--hosted-origin", "off"])

    with pytest.raises(SystemExit) as stopped:
        runpy.run_path(str(runner))
    assert stopped.value.code == 0
    assert calls == [([
        "run", "--profile", "astrid", "--data-root", str(tmp_path / "workspace"),
        "--source-manifest", str(root / "versions" / LEGACY_VERSION / "profile.json"),
        "--hosted-origin", "off",
    ], os.getpid())]
