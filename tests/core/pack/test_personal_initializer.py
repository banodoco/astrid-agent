from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
RUNTIME_ROOT = Path("/home/user/machine/ws-C/src/astrid-runtime")


def _run_python(home: Path, script: str, *, packs_root: Path | None = None) -> None:
    env = os.environ.copy()
    env["HOME"] = str(home)
    env.pop("ASTRID_PACKS_PATH", None)
    if packs_root is not None:
        env["ASTRID_PACKS_PATH"] = str(packs_root)
    env["PYTHONPATH"] = os.pathsep.join((str(REPO_ROOT), str(RUNTIME_ROOT)))
    subprocess.run(
        [sys.executable, "-c", script],
        cwd=REPO_ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )


def test_import_with_read_only_home_does_not_create_files(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    home.chmod(0o555)
    try:
        _run_python(home, "import astrid.packs")
    finally:
        home.chmod(0o755)
    assert list(home.iterdir()) == []


def test_explicit_initializer_creates_minimal_personal_pack_once(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _run_python(
        home,
        "from astrid.core.pack.personal import ensure_personal_pack; "
        "ensure_personal_pack(); ensure_personal_pack()",
    )

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


def test_existing_personal_pack_bytes_survive_initialization(tmp_path: Path) -> None:
    home = tmp_path / "home"
    personal = home / "Astrid" / "packs" / "personal"
    personal.mkdir(parents=True)
    manifest_bytes = b"user-authored:\x00\xff\n"
    other_bytes = b"personal user data\x00"
    (personal / "pack.yaml").write_bytes(manifest_bytes)
    (personal / "notes.bin").write_bytes(other_bytes)

    _run_python(home, "from astrid.core.pack.personal import ensure_personal_pack; ensure_personal_pack()")

    assert (personal / "pack.yaml").read_bytes() == manifest_bytes
    assert (personal / "notes.bin").read_bytes() == other_bytes


def test_additional_packs_path_does_not_change_personal_pack(tmp_path: Path) -> None:
    home = tmp_path / "home"
    override = tmp_path / "configured-packs"
    _run_python(home, "from astrid.core.pack.personal import ensure_personal_pack; ensure_personal_pack()", packs_root=override)

    assert (home / "Astrid" / "packs" / "personal" / "pack.yaml").is_file()
    assert not override.exists()
