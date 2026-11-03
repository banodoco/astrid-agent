"""The optional RunPod dependency must not abort ordinary collection."""

import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


def test_missing_runpod_dependency_routes_collection_and_fails_required_lanes(tmp_path):
    # Simulate an absent distribution even when this test runs in the dev venv.
    (tmp_path / "sitecustomize.py").write_text(
        """import importlib.abc
import importlib.util
import sys
original = importlib.util.find_spec
def find_spec(name, *args, **kwargs):
    if name == 'runpod_lifecycle' or name.startswith('runpod_lifecycle.'):
        return None
    return original(name, *args, **kwargs)
importlib.util.find_spec = find_spec
class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'runpod_lifecycle' or fullname.startswith('runpod_lifecycle.'):
            raise ModuleNotFoundError('simulated missing dependency', name=fullname)
sys.meta_path.insert(0, Block())
""",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env.pop("CI", None)
    env["PYTHONPATH"] = os.pathsep.join((str(tmp_path), env.get("PYTHONPATH", "")))
    command = [sys.executable, "-m", "pytest", "--collect-only", "-q", "--tb=short",
               "tests/packs/runpod", "tests/test_installer_schema.py::test_d72_runtime_and_composition_pins_have_no_obsolete_externals"]

    ordinary = subprocess.run(command, cwd=ROOT, env=env, text=True, capture_output=True, check=False)
    assert ordinary.returncode == 0, ordinary.stdout + ordinary.stderr
    assert "test_https_transport.py" in ordinary.stdout
    assert "deselected" in ordinary.stdout
    assert "test_d72_runtime_and_composition_pins_have_no_obsolete_externals" in ordinary.stdout

    for extra, overrides in [(["-m", "integration"], {}), ([], {"CI": "true"})]:
        result = subprocess.run(command + extra, cwd=ROOT, env={**env, **overrides},
                                text=True, capture_output=True, check=False)
        assert result.returncode != 0, result.stdout + result.stderr
        assert "runpod_lifecycle is a declared dev dependency and is not installed" in result.stdout + result.stderr
