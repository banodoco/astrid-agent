import json
from pathlib import Path

import pytest

from astrid.installer import cli, service


def _install_r1(root: Path) -> None:
    version = root / "versions" / "r1"
    version.mkdir(parents=True)
    (root / "install.json").write_text(json.dumps({"owner": service.LABEL, "data_root": "unused"}))
    (root / "current").write_text("r1\n")
    (version / "version.json").write_text(json.dumps({
        "root": str(version),
        "python": str(version / "py" / "python.exe"),
        "profile": str(version / "profile"),
        "env": {},
        "identity": {"release": 1, "managed_by": None},
    }))


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {str(path.relative_to(root)): path.read_bytes()
            for path in root.rglob("*") if path.is_file()}


@pytest.mark.parametrize("update_args", [["--update"], []], ids=["explicit-update", "install-line"])
def test_windows_r1_update_refuses_before_installer_or_workspace_mutation(
        tmp_path, monkeypatch, capsys, update_args):
    root = tmp_path / "program"
    data = tmp_path / "workspace"
    _install_r1(root)
    data.mkdir()
    (data / "sentinel.txt").write_text("keep me")
    selection = tmp_path / "astrid-install.json"
    selection.write_text(json.dumps({"owner": service.LABEL, "program_root": str(root),
                                    "data_root": str(data)}))
    monkeypatch.setattr(service, "install_selection", lambda *_: selection)
    monkeypatch.setattr(cli.sys, "platform", "win32")
    monkeypatch.setattr(cli, "Backend", lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("installer/backend work happened before the refusal")))
    before = (_tree_bytes(root), _tree_bytes(data), selection.read_bytes())

    assert cli.main(["--root", str(root), "--data-root", str(data), *update_args]) == 1

    assert capsys.readouterr().out == cli.WINDOWS_UPDATE_REFUSAL + "\n"
    assert (_tree_bytes(root), _tree_bytes(data), selection.read_bytes()) == before
    assert not (root / ".app-update.lock").exists()


def test_windows_fresh_install_and_non_windows_r1_are_not_refused(tmp_path, monkeypatch):
    root = tmp_path / "program"
    selection = tmp_path / "astrid-install.json"
    monkeypatch.setattr(service, "install_selection", lambda *_: selection)
    monkeypatch.setattr(cli.sys, "platform", "win32")
    args = cli.parser().parse_args(["--root", str(root)])
    assert cli._windows_r1_update_refused(args, "install") is False

    _install_r1(root)
    monkeypatch.setattr(cli.sys, "platform", "linux")
    assert cli._windows_r1_update_refused(args, "update") is False
