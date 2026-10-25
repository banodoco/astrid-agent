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


def test_built_wheel_declares_its_bundled_chat_service(tmp_path):
    # Use the real release configuration and source files without copying the
    # whole checkout or writing build/egg-info output into a developer's tree.
    source = tmp_path / "source"
    source.mkdir()
    shutil.copy2(ROOT / "pyproject.toml", source / "pyproject.toml")
    files = ("__init__.py", "version.py", "services.py", "chat/__init__.py", *CHAT_RESOURCES,
             "installer/__init__.py", "installer/tools.py", *OMP_RESOURCES,
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
        assert {f"astrid/{name}" for name in (*CHAT_RESOURCES, *OMP_RESOURCES)} <= names
        assert not {f"astrid/{name}" for name in (*excluded, "chat/service.ts", "chat/service.test.ts")} & names
        for name in (*CHAT_RESOURCES, *OMP_RESOURCES):
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
