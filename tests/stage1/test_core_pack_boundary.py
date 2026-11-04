"""Real host composition with exactly the four core pack resource directories."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys



_CORE = ("generation", "rendering", "timeline", "vibecomfy")


def _core_tree(tmp_path):
    source = Path(__file__).resolve().parents[2]
    root = tmp_path / "core-only"
    root.mkdir()
    shutil.copytree(source / "astrid", root / "astrid", ignore=shutil.ignore_patterns("packs", "__pycache__"))
    packs = root / "astrid" / "packs"
    packs.mkdir()
    for owner in _CORE:
        shutil.copytree(source / "astrid" / "packs" / owner, packs / owner,
                        ignore=shutil.ignore_patterns("__pycache__"))
    for name in ("__init__.py", "engine-index.json"):
        path = source / "astrid" / "packs" / name
        if path.is_file():
            shutil.copy2(path, packs / name)
    shutil.copytree(source / "astrid_runtime_client", root / "astrid_runtime_client",
                    ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy2(source / "pyproject.toml", root / "pyproject.toml")
    assert sorted(path.name for path in packs.iterdir() if path.is_dir()) == sorted(_CORE)
    return root


def _probe(root):
    # Only declared source roots enter the child. No parent pack selector or
    # editable Agent checkout can rescue a missing optional resource.
    runtime = Path(os.environ["ASTRID_RUNTIME_CHECKOUT"])
    env = {key: os.environ[key] for key in ("PATH", "TMPDIR", "SYSTEMROOT") if key in os.environ}
    env.update(PYTHONPATH=os.pathsep.join((str(root), str(runtime))),
               ASTRID_RUNTIME_CHECKOUT=str(runtime), ASTRID_PACKS_PATH="", PYTHONDONTWRITEBYTECODE="1")
    script = r'''
import importlib.util, json, pathlib, sys
root = pathlib.Path.cwd()
core = ('generation', 'rendering', 'timeline', 'vibecomfy')
reads = set()
def audit(event, args):
    if event not in ('open', 'os.listdir', 'os.scandir') or not args:
        return
    if not isinstance(args[0], (str, bytes)):
        return
    path = pathlib.Path(args[0].decode() if isinstance(args[0], bytes) else args[0])
    if not path.is_absolute():
        path = root / path
    try:
        relative = path.relative_to(root / 'astrid' / 'packs')
    except ValueError:
        return
    reads.add(relative.as_posix())
    assert not relative.parts or relative.parts[0] in (*core, '__init__.py', '__pycache__', 'engine-index.json'), str(path)
sys.addaudithook(audit)
import astrid
assert pathlib.Path(astrid.__file__).is_relative_to(root)
import astrid.core.gateway
from astrid.core.execution.generic_host import GenericPackHost
from astrid.core.generation import engine_recipe as recipes
from astrid.core.pack.canonical import BundledCatalog, read_normalize_validate
packs = root / 'astrid' / 'packs'
entries = tuple(read_normalize_validate(packs / owner / 'pack.yaml', source='local') for owner in core)
selected = BundledCatalog(packs, entries)
recipe = recipes.load_recipe('vibecomfy', 'windows-x86_64-cu130', registry=selected)
models = recipes.load_model_lock(recipe.models_lock, registry=selected)
plain = recipes.load_recipe('comfyui', 'linux-x86_64-cu130', registry=selected)
assert plain.pins == recipe.pins and plain.models_lock == recipe.models_lock
assert plain.environments[0].requirements.packaged_lock.path == 'packs/vibecomfy/requirements.linux-cu130.lock'
host = GenericPackHost(pack_roots=[packs / owner for owner in core], credential_source={})
records = host.discover()
assert records and any(record.id.startswith('vibecomfy.') for record in records)
assert recipe.pins.path == 'packs/vibecomfy/pins.json'
assert {s.name for s in recipe.sources} == {'comfyui', 'vibecomfy'}
assert len(models.models) == 3
for name in ('runpod', 'wan2gp', 'h3_av', 'comfyui'):
    assert not (packs / name).exists()
    assert importlib.util.find_spec('astrid.packs.' + name) is None
assert not any(name.startswith(('astrid.packs.runpod', 'astrid.packs.wan2gp', 'astrid.packs.h3_av')) for name in sys.modules)
print(json.dumps({'capabilities': len(records), 'engine': recipe.engine, 'reads': sorted(reads)}))
'''
    return subprocess.run([sys.executable, "-c", script], cwd=root, env=env, text=True, capture_output=True)


def test_core_only_real_host_and_selected_recipe_composition(tmp_path):
    result = _probe(_core_tree(tmp_path))
    assert result.returncode == 0, result.stderr
    proof = json.loads(result.stdout)
    assert proof["capabilities"] > 0
    assert proof["engine"] == "vibecomfy"
    assert "vibecomfy/pins.json" in proof["reads"]


def test_core_only_composition_detects_an_optional_import(tmp_path):
    root = _core_tree(tmp_path)
    gateway = root / "astrid" / "core" / "gateway" / "__init__.py"
    gateway.write_text(gateway.read_text() + "\nimport astrid.packs.h3_av\n")
    result = _probe(root)
    assert result.returncode != 0
    assert "ModuleNotFoundError" in result.stderr
    assert "astrid.packs.h3_av" in result.stderr
