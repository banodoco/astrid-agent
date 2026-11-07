"""Python imports can load uniquely named packs from personal and env roots."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
RUNTIME_ROOT = Path("/home/user/machine/ws-C/src/astrid-runtime")


def _run_import(home: Path, *roots: Path, module: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["ASTRID_PACKS_PATH"] = os.pathsep.join(str(root) for root in roots)
    env["PYTHONPATH"] = os.pathsep.join((str(REPO_ROOT), str(RUNTIME_ROOT)))
    env["ASTRID_RUNTIME_CHECKOUT"] = str(RUNTIME_ROOT)
    return subprocess.run(
        [sys.executable, "-c", f"import astrid.packs.{module} as pack; print(pack.VALUE)"],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def _make_pack(root: Path, pack_id: str, value: str = "external") -> None:
    package = root / pack_id
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(f"VALUE = {value!r}\n", encoding="utf-8")


def test_external_pack_folder_is_importable_through_env_path(tmp_path: Path) -> None:
    external = tmp_path / "external"
    _make_pack(external, "outside_pack")

    result = _run_import(tmp_path / "home", external, module="outside_pack")

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "external"


def test_personal_pack_folder_is_importable(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _make_pack(home / "Astrid" / "packs", "personal_pack", "personal")

    result = _run_import(home, module="personal_pack")

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "personal"


def test_core_pack_id_clash_is_refused_with_reason(tmp_path: Path) -> None:
    external = tmp_path / "external"
    _make_pack(external, "media")

    result = _run_import(tmp_path / "home", external, module="outside_pack")

    assert result.returncode != 0
    assert "refusing astrid.packs path extension" in result.stderr
    assert "'media'" in result.stderr
    assert "core pack id" in result.stderr


def test_personal_and_env_duplicate_id_is_refused_with_reason(tmp_path: Path) -> None:
    home = tmp_path / "home"
    personal = home / "Astrid" / "packs"
    external = tmp_path / "external"
    _make_pack(personal, "same_pack", "personal")
    _make_pack(external, "same_pack", "external")

    result = _run_import(home, external, module="same_pack")

    assert result.returncode != 0
    assert "refusing astrid.packs path extension" in result.stderr
    assert "'same_pack'" in result.stderr
    assert "duplicates personal/external pack id" in result.stderr
