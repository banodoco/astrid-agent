from pathlib import Path
import json
import os
import subprocess
import sys

import pytest

from astrid.core.pack.imports import pack_command_args


def test_pack_command_script_is_resolved_from_selected_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "hivemind"
    script = root / "actions" / "search" / "run.py"
    script.parent.mkdir(parents=True)
    script.write_text("pass\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    assert pack_command_args(["actions/search/run.py"], "hivemind", root) == ["actions/search/run.py"]


def test_pack_command_rejects_script_outside_selected_root(tmp_path: Path) -> None:
    root = tmp_path / "hivemind"
    root.mkdir()
    foreign = tmp_path / "foreign.py"
    foreign.write_text("pass\n", encoding="utf-8")

    with pytest.raises(ValueError, match="outside selected pack"):
        pack_command_args([str(foreign)], "hivemind", root)


@pytest.mark.parametrize("case", ["parent_escape", "outward_symlink", "missing", "non_python"])
def test_pack_command_refuses_unsafe_script_paths(tmp_path: Path, case: str) -> None:
    root = tmp_path / "hivemind"
    root.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text("pass\n", encoding="utf-8")
    if case == "parent_escape":
        argument = "../outside.py"
    elif case == "outward_symlink":
        alias = root / "alias.py"
        alias.symlink_to(outside)
        argument = "alias.py"
    elif case == "missing":
        argument = "missing.py"
    else:
        (root / "run.txt").write_text("pass\n", encoding="utf-8")
        argument = "run.txt"

    with pytest.raises(ValueError):
        pack_command_args([argument], "hivemind", root)


@pytest.mark.parametrize("excluded", [".generated", "__pycache__"])
def test_pack_command_rejects_digest_excluded_script_directory(tmp_path: Path, excluded: str) -> None:
    root = tmp_path / "hivemind"
    script = root / excluded / "run.py"
    script.parent.mkdir(parents=True)
    script.write_text("pass\n", encoding="utf-8")

    with pytest.raises(ValueError, match="digest-excluded"):
        pack_command_args([f"{excluded}/run.py"], "hivemind", root)


def test_selected_source_finder_rejects_digest_excluded_module(tmp_path: Path) -> None:
    from astrid.core.pack.imports import _PackSourceFinder, pack_module_path

    root = tmp_path / "hivemind"
    source = root / ".generated" / "secret.py"
    source.parent.mkdir(parents=True)
    source.write_text("pass\n", encoding="utf-8")
    module_path, aliases = pack_module_path(Path(".generated/secret.py"))
    finder = _PackSourceFinder({"hivemind": {"root": str(root)}}, {"hivemind": aliases})

    with pytest.raises(ValueError, match="digest-excluded"):
        finder.find_spec(f"astrid.packs.hivemind.{module_path}")


def test_pack_worker_runs_relative_script_from_selected_root(tmp_path: Path) -> None:
    from astrid.core.pack.identity import pack_digest

    root = tmp_path / "testpack"
    script = root / "actions" / "search" / "run.py"
    script.parent.mkdir(parents=True)
    script.write_text("print('pack script ran')\n", encoding="utf-8")
    control = {"pack_id": root.name, "root": str(root), "source_digest": pack_digest(root),
               "argv": ["actions/search/run.py"]}

    result = subprocess.run(
        [sys.executable, "-m", "astrid.core.execution.generic_host_worker", "--pack-command", json.dumps(control)],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "pack script ran\n"


def test_core_pack_does_not_add_core_modules_as_external_pack_collection() -> None:
    from astrid.core.pack.imports import selected_pack_environment

    core_root = Path(__file__).resolve().parents[3] / "astrid" / "core" / "hivemind"
    selected = {"hivemind": {"root": str(core_root), "source_digest": "a" * 64}}

    environment = selected_pack_environment(selected)

    assert environment["ASTRID_PACKS_PATH"] == ""


def test_external_pack_collection_cannot_reuse_core_pack_id(tmp_path: Path) -> None:
    from astrid.core.pack.imports import selected_pack_environment
    from astrid.packs import _check_collisions

    core_root = Path(__file__).resolve().parents[3] / "astrid" / "core" / "hivemind"
    external_root = tmp_path / "another"
    external_root.mkdir(parents=True)
    (tmp_path / "hivemind").mkdir()
    selected = {
        "hivemind": {"root": str(core_root), "source_digest": "a" * 64},
        "another": {"root": str(external_root), "source_digest": "b" * 64},
    }

    environment = selected_pack_environment(selected)
    roots = tuple(Path(item) for item in environment["ASTRID_PACKS_PATH"].split(os.pathsep) if item)
    with pytest.raises(ImportError, match="collides"):
        _check_collisions(roots, selected=selected)
