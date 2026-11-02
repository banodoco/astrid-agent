"""The real installer layout must bootstrap engines without seeded pip."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


@pytest.mark.integration
@pytest.mark.linux_native
def test_unseeded_installer_environment_bootstraps_recipe_uv(tmp_path):
    uv = shutil.which("uv")
    assert uv, "The installer integration proof requires uv"
    uv = str(Path(uv).resolve())
    product = tmp_path / "program/versions/candidate/py"
    subprocess.run([uv, "venv", "--python", sys.executable, str(product)], check=True, capture_output=True)
    python = product / "bin/python"
    root = tmp_path / "engine with spaces"
    env = {**os.environ, "ASTRID_INSTALL_UV": uv,
           "PATH": str(product / "bin") + os.pathsep + os.defpath,
           "UV_CACHE_DIR": str(tmp_path / "uv-cache")}
    code = '''
import importlib.util,json,sys
assert importlib.util.find_spec('pip') is None, 'proof must start unseeded'
sys.path.insert(0, sys.argv[1])
from astrid.core.generation.engine_recipe import load_recipe
from astrid.core.generation.engine_platforms import platform_adapter
recipe=load_recipe('comfyui','linux-x86_64-cu130')
adapter=platform_adapter(recipe.platform)
commands=adapter.environment_commands(recipe,recipe.environments[0],sys.argv[2])
print(json.dumps(commands))
'''
    commands = json.loads(subprocess.check_output(
        [str(python), "-I", "-c", code, str(Path(__file__).parents[1]), str(root)], env=env, text=True))
    # Execute the real provisioning command: no seeded pip, fake uv or fake wheel.
    assert commands[0][0] == uv and commands[0][commands[0].index("--python") + 1] == str(python)
    subprocess.run(commands[0], env=env, check=True, capture_output=True, timeout=120)
    result = subprocess.check_output([str(root / "runtime/bootstrap/bin/uv"), "--version"], text=True)
    assert result.split()[:2] == ["uv", "0.11.32"]
    subprocess.run([str(python), "-I", "-c",
                    "import importlib.util; assert importlib.util.find_spec('pip') is None"],
                   env=env, check=True)
