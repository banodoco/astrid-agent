"""Built wheels must carry the chat service used by installed declarations."""

import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).parents[1]
CHAT_RESOURCES = ("chat/service.mjs", "chat/astrid-acp-local-only.yml", "chat/THIRD_PARTY_LICENSES.txt")
OMP_RESOURCES = ("installer/omp/recipe.json",)
GENERATION_GUIDE_RESOURCES = (
    "packs/generation/pack.yaml",
    "packs/generation/docs/SKILL.md",
    "packs/generation/actions/generate_image/STAGE.md",
)


def _engine_resources():
    from astrid.core.generation.engine_recipe import load_recipe, load_recipe_index
    from astrid.core.generation.engine_runner import _A2_PATHS, _CODE_PATHS

    resources = {'packs/engine-index.json', *_CODE_PATHS, *_A2_PATHS}
    locks = set()
    for entry in load_recipe_index().entries:
        recipe = load_recipe(entry.engine, entry.platform)
        resources.update(ref.path for ref in (entry.recipe, recipe.pins, recipe.models_lock))
        for environment in recipe.environments:
            lock = environment.requirements.packaged_lock
            if lock is not None:
                resources.add(lock.path)
                locks.add(lock.path)
    assert locks  # This exercises actual recipe locks, not just JSON metadata.
    return resources, locks


def test_built_wheel_loads_every_engine_helper_and_packaged_lock(tmp_path):
    source = tmp_path / 'source'
    source.mkdir()
    shutil.copy2(ROOT / 'pyproject.toml', source / 'pyproject.toml')
    resources, locks = _engine_resources()
    for name in resources:
        destination = source / 'astrid' / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / 'astrid' / name, destination)
    built = subprocess.run(
        [sys.executable, '-m', 'build', '--wheel', '--no-isolation', '--outdir', str(tmp_path / 'dist')],
        cwd=source, capture_output=True, text=True, check=False,
    )
    assert built.returncode == 0, built.stdout + built.stderr
    installed = tmp_path / 'installed'
    wheel_path, = (tmp_path / 'dist').glob('*.whl')
    with zipfile.ZipFile(wheel_path) as wheel:
        for name in locks:
            assert wheel.read('astrid/' + name) == (ROOT / 'astrid' / name).read_bytes()
        wheel.extractall(installed)
    # No source checkout or ambient site packages can supply a missing resource.
    script = '''
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from astrid.core.generation import engine_recipe
from astrid.core.generation.engine_runner import helper_manifest
assert Path(engine_recipe.__file__).is_relative_to(Path(sys.argv[1]))
for entry in engine_recipe.load_recipe_index().entries:
    recipe = engine_recipe.load_recipe(entry.engine, entry.platform)
    manifest = helper_manifest(recipe)
    for environment in recipe.environments:
        lock = environment.requirements.packaged_lock
        if lock is not None:
            assert manifest[lock.path] == lock.sha256
'''
    checked = subprocess.run(
        [sys.executable, '-I', '-S', '-c', script, str(installed)],
        cwd=tmp_path, capture_output=True, text=True, check=False,
    )
    assert checked.returncode == 0, checked.stdout + checked.stderr


def test_built_wheel_declares_its_bundled_chat_service(tmp_path):
    # Use the real release configuration and source files without copying the
    # whole checkout or writing build/egg-info output into a developer's tree.
    source = tmp_path / "source"
    source.mkdir()
    shutil.copy2(ROOT / "pyproject.toml", source / "pyproject.toml")
    files = ("__init__.py", "version.py", "services.py", "chat/__init__.py", *CHAT_RESOURCES,
             "installer/__init__.py", "installer/tools.py", *OMP_RESOURCES, *GENERATION_GUIDE_RESOURCES,
             *_engine_resources()[0], "core/generation/vibecomfy_dependency.py",
             "chat/service.ts", "chat/service.test.ts")
    for name in files:
        destination = source / "astrid" / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / "astrid" / name, destination)
    excluded = [f"packs/example/{kind}/authoring.json" for kind in ("tests", "golden", "build")]
    for name in excluded:
        destination = source / "astrid" / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text('{"authoring_only":true}', encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--no-isolation", "--outdir", str(tmp_path / "dist")],
        cwd=source, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    wheels = list((tmp_path / "dist").glob("*.whl"))
    assert len(wheels) == 1
    installed = tmp_path / "unpacked-wheel"
    with zipfile.ZipFile(wheels[0]) as wheel:
        names = set(wheel.namelist())
        assert {f"astrid/{name}" for name in (*CHAT_RESOURCES, *OMP_RESOURCES, *GENERATION_GUIDE_RESOURCES)} <= names
        assert not {f"astrid/{name}" for name in (*excluded, "chat/service.ts", "chat/service.test.ts")} & names
        for name in (*CHAT_RESOURCES, *OMP_RESOURCES, *GENERATION_GUIDE_RESOURCES):
            assert wheel.read(f"astrid/{name}") == (ROOT / "astrid" / name).read_bytes()
        wheel.extractall(installed)
    script = """
import json,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import astrid.services
from astrid.installer.tools import load_omp_recipe
declaration=astrid.services.service_declaration(
    source_checkout=Path(sys.argv[2]),data_root=Path(sys.argv[3]),
    python=sys.executable,bun=sys.argv[4])
print(json.dumps({'module':astrid.services.__file__,'declaration':declaration,
                  'omp_recipe_version':load_omp_recipe()['version']}))
"""
    bun = str(tmp_path / "tools" / "bun")
    declared = subprocess.run(
        [sys.executable, "-I", "-c", script, str(installed), str(tmp_path / "candidate checkout"),
         str(tmp_path / "data"), bun],
        cwd=tmp_path, capture_output=True, text=True, check=False,
    )
    assert declared.returncode == 0, declared.stdout + declared.stderr
    value = json.loads(declared.stdout)
    assert value["omp_recipe_version"] == "18.12.6"
    assert Path(value["module"]) == installed / "astrid" / "services.py"
    chat = next(row for row in value["declaration"]["services"] if row["name"] == "chat")
    assert chat["argv"] == [bun, str(installed / "astrid" / "chat" / "service.mjs")]
    assert Path(chat["argv"][1]).is_file()
