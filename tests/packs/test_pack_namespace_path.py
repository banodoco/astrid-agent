from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _write_folder_pack(root: Path, pack_id: str, *, marker: str) -> Path:
    pack = root / pack_id
    pack.mkdir(parents=True)
    (pack / "__init__.py").write_text(f"ORIGIN = {marker!r}\n", encoding="utf-8")
    (pack / "pack.yaml").write_text("[this is not a pack manifest\n", encoding="utf-8")
    return pack


def _run_fresh_import(home: Path, *, packs_root: str = "", script: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["ASTRID_PACKS_PATH"] = packs_root
    env["PYTHONPATH"] = os.pathsep.join((str(REPO_ROOT), "/home/user/machine/ws-C/src/astrid-runtime"))
    return subprocess.run([sys.executable, "-c", script], cwd=REPO_ROOT, env=env,
                          text=True, capture_output=True, check=False)


def test_default_visible_root_imports_folder_pack_without_reading_manifest(tmp_path: Path) -> None:
    home = tmp_path / "home"
    root = home / "Astrid" / "packs"
    _write_folder_pack(root, "hello_ext", marker="visible")
    result = _run_fresh_import(home, script="import astrid.packs.hello_ext as p; print(p.ORIGIN, p.__file__)")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"visible {root / 'hello_ext' / '__init__.py'}"


def test_environment_list_adds_roots_and_keeps_default(tmp_path: Path) -> None:
    home = tmp_path / "home"
    default = home / "Astrid" / "packs"
    first, second = tmp_path / "first", tmp_path / "second"
    for root, name in ((default, "default_only"), (first, "first_only"), (second, "second_only")):
        _write_folder_pack(root, name, marker=name)
    result = _run_fresh_import(home, packs_root=os.pathsep.join((str(first), str(second), str(first))), script="""
import json
import astrid.packs.default_only, astrid.packs.first_only, astrid.packs.second_only
import astrid.packs
print(json.dumps(list(astrid.packs.__path__)))
""")
    assert result.returncode == 0, result.stderr
    paths = [Path(path).resolve() for path in json.loads(result.stdout)]
    assert paths == [(REPO_ROOT / "astrid" / "packs").resolve(), default.resolve(), first.resolve(), second.resolve()]


@pytest.mark.parametrize("name", ["blender", "astrid_motion"])
def test_core_collision_refused_before_pack_import(tmp_path: Path, name: str) -> None:
    home = tmp_path / "home"
    external = _write_folder_pack(home / "Astrid" / "packs", name, marker="external")
    result = _run_fresh_import(home, script=f"import astrid.packs.{name}")
    assert result.returncode != 0
    assert "collides" in result.stderr and name in result.stderr
    assert str(external) in result.stderr and str(REPO_ROOT / "astrid" / "packs" / name) in result.stderr


def test_canonical_personal_import_and_competing_personal_refusal(tmp_path: Path) -> None:
    home = tmp_path / "home"
    canonical = _write_folder_pack(home / "Astrid" / "packs", "personal", marker="canonical")
    result = _run_fresh_import(home, script="import astrid.packs.personal as p; print(p.ORIGIN, p.__file__)")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"canonical {canonical / '__init__.py'}"
    competing = _write_folder_pack(tmp_path / "extra", "personal", marker="competing")
    result = _run_fresh_import(home, packs_root=str(competing.parent), script="import astrid.packs.personal")
    assert result.returncode != 0
    assert "reserved personal" in result.stderr and str(competing) in result.stderr


@pytest.mark.parametrize("name", ["duplicate", "Upper-case"])
def test_collision_between_additional_roots_refused_with_reason(tmp_path: Path, name: str) -> None:
    first = _write_folder_pack(tmp_path / "first", name, marker="first")
    second = _write_folder_pack(tmp_path / "second", name, marker="second")
    result = _run_fresh_import(tmp_path / "home", packs_root=os.pathsep.join((str(first.parent), str(second.parent))), script="import astrid.packs")
    assert result.returncode != 0
    assert "collides" in result.stderr and name in result.stderr
    assert str(first) in result.stderr and str(second) in result.stderr


def test_fresh_home_initialize_create_and_import_without_reload(tmp_path: Path) -> None:
    home = tmp_path / "home"
    result = _run_fresh_import(home, script="""
from pathlib import Path
import astrid.packs
from astrid.core.pack.personal import ensure_personal_pack
root = Path.home() / 'Astrid' / 'packs'
assert not root.exists()
assert str(root) in astrid.packs.__path__
ensure_personal_pack()
import astrid.packs.personal
pack = root / 'new_pack'
pack.mkdir()
(pack / '__init__.py').write_text('VALUE = 42\\n')
import astrid.packs.new_pack
assert astrid.packs.new_pack.VALUE == 42
""")
    assert result.returncode == 0, result.stderr


def test_late_collision_refused_before_new_pack_import(tmp_path: Path) -> None:
    home = tmp_path / "home"
    extra = tmp_path / "extra"
    extra.mkdir()
    result = _run_fresh_import(home, packs_root=str(extra), script=f"""
from pathlib import Path
import astrid.packs
root = Path.home() / 'Astrid' / 'packs'
for parent in (root, Path({str(extra)!r})):
    (parent / 'later').mkdir(parents=True)
import astrid.packs.later
""")
    assert result.returncode != 0
    assert "collides" in result.stderr and "later" in result.stderr
