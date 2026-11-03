from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def _write_folder_pack(root: Path, pack_id: str, *, marker: str) -> Path:
    pack = root / pack_id
    pack.mkdir(parents=True)
    (pack / "__init__.py").write_text(f"ORIGIN = {marker!r}\n", encoding="utf-8")
    # Import-path setup must not inspect manifests for packs that have not been
    # selected. Deliberately invalid YAML makes accidental validation visible.
    (pack / "pack.yaml").write_text("[this is not a pack manifest\n", encoding="utf-8")
    return pack


def _run_fresh_import(home: Path, *, packs_root: Path | None = None, script: str) -> str:
    env = os.environ.copy()
    env["HOME"] = str(home)
    env.pop("ASTRID_PACKS_PATH", None)
    if packs_root is not None:
        env["ASTRID_PACKS_PATH"] = str(packs_root)
    env["PYTHONPATH"] = str(REPO_ROOT)
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout.strip()


def test_default_visible_root_imports_folder_pack_without_reading_manifest(tmp_path: Path) -> None:
    home = tmp_path / "home"
    visible_root = home / "Astrid" / "packs"
    _write_folder_pack(visible_root, "hello_ext", marker="visible")

    result = _run_fresh_import(
        home,
        script=(
            "import astrid.packs.hello_ext as pack; "
            "print(pack.ORIGIN + '|' + str(pack.__file__))"
        ),
    )
    origin, filename = result.split("|", 1)
    assert origin == "visible"
    assert Path(filename).resolve().is_relative_to(visible_root.resolve())


def test_override_is_the_single_canonical_visible_root(tmp_path: Path) -> None:
    home = tmp_path / "home"
    default_root = home / "Astrid" / "packs"
    override_root = tmp_path / "other-packs"
    _write_folder_pack(default_root, "default_only", marker="default")
    _write_folder_pack(override_root, "override_only", marker="override")

    result = _run_fresh_import(
        home,
        packs_root=override_root,
        script=(
            "import json; "
            "import astrid.packs.override_only as pack; "
            "import astrid.packs; "
            "print(pack.ORIGIN + '|' + json.dumps(list(astrid.packs.__path__)))"
        ),
    )
    origin, raw_paths = result.split("|", 1)
    paths = [Path(path).resolve() for path in json.loads(raw_paths)]
    assert origin == "override"
    assert override_root.resolve() in paths
    assert default_root.resolve() not in paths
    assert len(paths) == len(set(paths))
    assert paths.index((REPO_ROOT / "astrid" / "packs").resolve()) < paths.index(override_root.resolve())


def test_core_pack_wins_external_name_collision(tmp_path: Path) -> None:
    home = tmp_path / "home"
    visible_root = home / "Astrid" / "packs"
    external = _write_folder_pack(visible_root, "blender", marker="external")

    result = _run_fresh_import(
        home,
        script=(
            "import astrid.packs.blender as pack; "
            "print(str(pack.__file__))"
        ),
    )
    assert Path(result).resolve() != (external / "__init__.py").resolve()
    assert Path(result).resolve().is_relative_to((REPO_ROOT / "astrid" / "packs").resolve())


def test_core_namespace_pack_cannot_be_replaced_by_external_regular_package(tmp_path: Path) -> None:
    home = tmp_path / "home"
    visible_root = home / "Astrid" / "packs"
    external = _write_folder_pack(visible_root, "astrid_motion", marker="external")
    (external / "foreign.py").write_text("VALUE = 'external'\n", encoding="utf-8")

    result = _run_fresh_import(
        home,
        script="""
import importlib
pack = importlib.import_module('astrid.packs.astrid_motion')
assert pack.__file__ is None
assert all('Astrid/packs/astrid_motion' not in str(path) for path in pack.__path__)
try:
    importlib.import_module('astrid.packs.astrid_motion.foreign')
except ModuleNotFoundError:
    print('core namespace retained')
else:
    raise AssertionError('external regular package replaced the core namespace')
""",
    )
    assert result == "core namespace retained"


def test_personal_directory_is_reserved_and_not_an_importable_pack(tmp_path: Path) -> None:
    home = tmp_path / "home"
    visible_root = home / "Astrid" / "packs"
    _write_folder_pack(visible_root, "personal", marker="should-not-load")

    result = _run_fresh_import(
        home,
        script="""
import importlib
try:
    importlib.import_module('astrid.packs.personal')
except ModuleNotFoundError as exc:
    print(str(exc))
else:
    raise AssertionError('reserved personal directory imported')
""",
    )
    assert "personal" in result
