"""Exercise the released CLI in an isolated, newly installed environment."""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import venv
from pathlib import Path

import pytest

from tests.helpers.runtime import locate_runtime

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.installed_smoke
def test_fresh_wheel_cli(tmp_path: Path) -> None:
    if importlib.util.find_spec("build") is None or importlib.util.find_spec("wheel") is None:
        pytest.skip("wheel build toolchain is absent")
    runtime = locate_runtime()
    assert runtime is not None, "installed smoke needs the CI Runtime checkout (ASTRID_RUNTIME_CHECKOUT)"
    vibe = Path(os.environ.get("ASTRID_VIBECOMFY_CHECKOUT", ROOT.parent / "vibecomfy"))
    assert (vibe / "pyproject.toml").is_file(), "installed smoke needs the VibeComfy checkout (ASTRID_VIBECOMFY_CHECKOUT)"

    def space() -> None:
        assert shutil.disk_usage(tmp_path).free >= 2 * 1024**3, "less than 2 GiB free"

    def run(args, *, cwd=tmp_path, env=None):
        result = subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True, timeout=600)
        return result

    space()
    source = tmp_path / "agent"
    source.mkdir()
    for name in ("pyproject.toml", "LICENSE", "NOTICE"):
        shutil.copy2(ROOT / name, source / name)
    for name in ("astrid", "astrid_runtime_client"):
        shutil.copytree(ROOT / name, source / name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    runtime_source = tmp_path / "runtime"
    runtime_source.mkdir()
    shutil.copy2(runtime / "pyproject.toml", runtime_source / "pyproject.toml")
    for name in ("runtime_protocol", "astrid_runtime"):
        shutil.copytree(runtime / name, runtime_source / name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    dist = tmp_path / "dist"
    for project in (source, runtime_source):
        built = run([sys.executable, "-m", "build", "--wheel", "--no-isolation", "--outdir", str(dist), str(project)])
        assert built.returncode == 0, built.stdout + built.stderr
    space()
    environment = tmp_path / "venv"
    venv.create(environment, with_pip=True)
    scripts = environment / ("Scripts" if os.name == "nt" else "bin")
    python = scripts / ("python.exe" if os.name == "nt" else "python")
    clean_env = {key: os.environ[key] for key in ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "SSL_CERT_FILE", "SSL_CERT_DIR") if key in os.environ}
    clean_env.update(HOME=str(tmp_path / "home"), USERPROFILE=str(tmp_path / "home"),
                     XDG_CONFIG_HOME=str(tmp_path / "config"), XDG_DATA_HOME=str(tmp_path / "data"),
                     XDG_STATE_HOME=str(tmp_path / "state"), XDG_CACHE_HOME=str(tmp_path / "cache"),
                     PIP_DISABLE_PIP_VERSION_CHECK="1", PIP_NO_CACHE_DIR="1")
    wheels = sorted(dist.glob("*.whl"))
    assert len(wheels) == 2
    installed = run([str(python), "-m", "pip", "install", *(str(p) for p in wheels)], env=clean_env)
    assert installed.returncode == 0, installed.stdout + installed.stderr
    # Install the real VibeComfy library without its engine, as the installer does.
    vibe_source = tmp_path / "vibecomfy"
    shutil.copytree(vibe, vibe_source, ignore=shutil.ignore_patterns(
        ".git", "__pycache__", "*.pyc", ".venv", "node_modules", "build", "dist"))
    installed_vibe = run([str(python), "-m", "pip", "install", "--no-deps", str(vibe_source)], env=clean_env)
    assert installed_vibe.returncode == 0, installed_vibe.stdout + installed_vibe.stderr
    space()
    probe = run([str(python), "-I", "-c", "import astrid,vibecomfy,sys; from pathlib import Path; "
                 "assert Path(vibecomfy.__file__).is_relative_to(Path(sys.prefix)); "
                 "assert Path(astrid.__file__).is_relative_to(Path(sys.prefix)); "
                 "assert all(not Path(p or '.').resolve().is_relative_to(Path(root)) for p in sys.path for root in sys.argv[1:]); "
                 "from astrid.sdk.host_bootstrap import _execution_profile, _capability_matrix_path; "
                 "from astrid.core.rendering._local_schema import verified_schema_path; "
                 "root=Path(astrid.__file__).parent.parent; "
                 "assert _execution_profile() == 'installed'; "
                 "assert _capability_matrix_path(root).is_file(); "
                 "schema=root/'astrid/core/timeline/schema/timeline-config.schema.json'; "
                 "assert verified_schema_path(root,str(schema)) == schema", str(ROOT), str(source), str(runtime_source), str(vibe_source)], env=clean_env)
    assert probe.returncode == 0, probe.stdout + probe.stderr

    def cli(name, *args):
        executable = scripts / (name + (".exe" if os.name == "nt" else ""))
        result = run([str(executable), *args], env=clean_env)
        assert "Traceback" not in result.stdout + result.stderr, result.stdout + result.stderr
        return result

    for name in ("astrid-tools", "astrid-credential", "astrid-with-credential", "astrid-upgrade"):
        result = cli(name, "--help")
        assert result.returncode == 0, result.stdout + result.stderr
    for args in (("--version",), ("generate", "targets", "--json"),
                 ("generate", "list", "--json"), ("generate", "describe", "--entry", "z-image", "--json")):
        result = cli("astrid", *args)
        assert result.returncode == 0, result.stdout + result.stderr
        if "--json" in args:
            assert isinstance(json.loads(result.stdout), (dict, list))
    result = cli("astrid", "generate", "image", "a red boat", "--entry", "z-image", "--mode", "t2i", "--target", "managed", "--dry-run", "--json")
    assert result.returncode == 2, result.stdout + result.stderr
    envelope = json.loads(result.stdout)
    assert envelope["status"] == "blocked"
    assert envelope["issues"][0]["code"] == "engine_not_found"
    assert envelope["issues"][0]["details"]["engine_code"] == "COMFY_TARGET_NOT_READY"
    assert "ComfyUI" in envelope["issues"][0]["message"]
    assert "vibecomfy" not in envelope["issues"][0]["message"].lower()
