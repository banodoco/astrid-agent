from pathlib import Path
import json
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
