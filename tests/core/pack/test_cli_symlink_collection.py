"""packs add refuses a symlinked personal collection, matching the Runtime's _no_alias walk."""
from __future__ import annotations

from pathlib import Path

import pytest

from astrid.core.pack import cli_manage
from astrid.core.pack.install import PackInstallError


def _home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    (home / "Astrid").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    return home


def test_symlinked_personal_collection_is_refused_with_the_runtime_text(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (home / "Astrid" / "packs").symlink_to(elsewhere, target_is_directory=True)

    with pytest.raises(PackInstallError) as refusal:
        cli_manage._selected_root(None)
    assert refusal.value.code == "unsafe_path"
    assert str(refusal.value).startswith(cli_manage.SYMLINKED_COLLECTION_REFUSAL + ": ")
    assert str(home / "Astrid" / "packs") in str(refusal.value)
    assert list(elsewhere.iterdir()) == []


def test_symlinked_parent_of_the_collection_is_refused(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch)
    alias = tmp_path / "alias"
    alias.symlink_to(home, target_is_directory=True)
    monkeypatch.setenv("HOME", str(alias))

    with pytest.raises(PackInstallError) as refusal:
        cli_manage._selected_root(None)
    assert str(refusal.value).startswith(cli_manage.SYMLINKED_COLLECTION_REFUSAL + ": ")
    assert str(alias) in str(refusal.value)


def test_plain_personal_collection_is_selected(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch)
    root = cli_manage._selected_root(None)
    assert root == (home / "Astrid" / "packs").resolve()


def test_refusal_text_is_the_runtime_text():
    # Keep in step with runtime_protocol/verified_host_launch.py _no_alias.
    assert cli_manage.SYMLINKED_COLLECTION_REFUSAL == "import materialization contains a symlink"
