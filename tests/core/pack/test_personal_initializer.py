from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
RUNTIME_ROOT = Path("/home/user/astrid/proof-codex/src/astrid-runtime")


def _import_packs(home: Path, *, packs_root: Path | None = None) -> None:
    env = os.environ.copy()
    env["HOME"] = str(home)
    env.pop("ASTRID_PACKS_PATH", None)
    if packs_root is not None:
        env["ASTRID_PACKS_PATH"] = str(packs_root)
    env["PYTHONPATH"] = os.pathsep.join((str(REPO_ROOT), str(RUNTIME_ROOT)))
    subprocess.run(
        [sys.executable, "-c", "import astrid.packs; import importlib; importlib.reload(astrid.packs)"],
        cwd=REPO_ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )


def test_first_import_creates_empty_personal_pack_in_home(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _import_packs(home)

    personal = home / "Astrid" / "packs" / "personal"
    assert sorted(path.name for path in personal.iterdir()) == ["pack.yaml"]
    assert (personal / "pack.yaml").read_bytes() == (
        b"schema_version: 3\n"
        b"id: personal\n"
        b"name: Personal\n"
        b"version: 0.1.0\n"
        b'pack_api: "1.0"\n'
        b"actions: {}\n"
    )


def test_existing_personal_pack_bytes_survive_reimport(tmp_path: Path) -> None:
    home = tmp_path / "home"
    personal = home / "Astrid" / "packs" / "personal"
    personal.mkdir(parents=True)
    manifest_bytes = b"user-authored:\x00\xff\n"
    other_bytes = b"personal user data\x00"
    (personal / "pack.yaml").write_bytes(manifest_bytes)
    (personal / "notes.bin").write_bytes(other_bytes)

    _import_packs(home)

    assert (personal / "pack.yaml").read_bytes() == manifest_bytes
    assert (personal / "notes.bin").read_bytes() == other_bytes


def test_packs_path_override_gets_its_own_personal_pack(tmp_path: Path) -> None:
    home = tmp_path / "home"
    override = tmp_path / "configured-packs"
    _import_packs(home, packs_root=override)

    assert (override / "personal" / "pack.yaml").is_file()
    assert not (home / "Astrid" / "packs").exists()
