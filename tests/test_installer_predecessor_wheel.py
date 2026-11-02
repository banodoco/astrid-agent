"""The closed predecessor policy is available in installed distributions."""

import json
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_predecessor_policy_survives_sdist_and_installed_wheel(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    for name in ("pyproject.toml", "MANIFEST.in", "astrid/__init__.py", "astrid/version.py",
                 "astrid/installer/__init__.py", "astrid/installer/legacy_identity.py",
                 "astrid/installer/predecessors.json"):
        destination = source / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / name, destination)

    def run(*args, cwd):
        result = subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=False)
        assert result.returncode == 0, result.stdout + result.stderr
        return result.stdout

    dist = tmp_path / "dist"
    run(sys.executable, "-m", "build", "--sdist", "--no-isolation", "--outdir", str(dist), cwd=source)
    archive = next(dist.glob("*.tar.gz"))
    with tarfile.open(archive) as sdist:
        assert any(name.endswith("/astrid/installer/predecessors.json") for name in sdist.getnames())
        sdist.extractall(tmp_path / "sdist", filter="data")
    unpacked = next((tmp_path / "sdist").iterdir())
    run(sys.executable, "-m", "build", "--wheel", "--no-isolation", "--outdir", str(dist), cwd=unpacked)
    wheel = next(dist.glob("*.whl"))
    installed = tmp_path / "installed"
    run(sys.executable, "-m", "pip", "install", "--no-index", "--no-deps", "--target", str(installed),
        str(wheel), cwd=tmp_path)
    script = """
import json, sys
from importlib import resources
sys.path.insert(0, sys.argv[1])
from astrid.installer import legacy_identity
rows = json.loads(resources.files('astrid.installer').joinpath('predecessors.json').read_text(encoding='utf-8'))
print(json.dumps({'rows': rows, 'policy': legacy_identity._policy(), 'module': legacy_identity.__file__}))
"""
    result = json.loads(run(sys.executable, "-I", "-c", script, str(installed), cwd=tmp_path))
    assert Path(result["module"]).is_relative_to(installed)
    assert result["rows"] == json.loads((ROOT / "astrid/installer/predecessors.json").read_text())
    assert result["policy"] == result["rows"][0]
