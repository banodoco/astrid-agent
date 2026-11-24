"""RA-0b stamp, immutable shipped inputs and installer cleanup/rollback."""
from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.installer import core
from astrid.installer._install_stamp import STAMP_NAME, write_install_stamp
from astrid.installer._pack_cleanup import remove_pack_tree
from astrid.installer.backend import Backend


@pytest.fixture
def installed(tmp_path):
    program, data = tmp_path / "program", tmp_path / "data"
    data.mkdir()
    core.write_json(program / "install.json", {
        "owner": "haus.astrid.runtime", "data_root": str(data),
    })

    def stage(name, *, release=15, content="model: one\n", seal=True, shared=None):
        folder = program / "versions" / name
        python = folder / "py" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        python.parent.mkdir(parents=True)
        python.touch()
        product = folder / "py" / "lib" / "site-packages"
        package = product / "astrid"
        files = {
            package / "__init__.py": "# installed Python\n",
            package / "packs" / "sample" / "pack.yaml": "id: sample\n",
            package / "packs" / "sample" / "action.py": "VALUE = 1\n",
            package / "core" / "model_catalog" / "models.yaml": content,
            product / "astrid_runtime_client" / "__init__.py": "# installed client\n",
            product / "runtime.lock.json": "{}\n",
        }
        for path, value in files.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            if shared is not None and path.name == "action.py":
                os.link(shared, path)
            else:
                path.write_text(value, encoding="utf-8")
        profile = folder / "profile.json"
        core.write_json(folder / "manifest.json", {"release": release, "agent": {"sha": "a" * 40}})
        core.write_json(profile, {"source_checkout": str(product)})
        version = core.Version(folder, python, profile, {"ASTRID_HOST_EXECUTION_PROFILE": "installed"}, release)
        core.persist_install_identity(version, program)
        if seal:
            write_install_stamp(version, product)
        return version, package

    try:
        yield program, data, stage
    finally:
        if program.exists():
            remove_pack_tree(program)


def _file(package):
    return package / "core" / "model_catalog" / "models.yaml"


def _relative(version, path):
    return path.relative_to(version.root).as_posix()


def _enable_write(path):
    path.chmod(stat.S_IMODE(path.stat().st_mode) | stat.S_IWUSR)


def _readonly(path):
    path.chmod(stat.S_IMODE(path.stat().st_mode) & ~0o222)


def test_stamp_covers_shipped_files_directories_and_manifest(installed):
    _, data, stage = installed
    version, package = stage("first")
    stamp, reason = core.install_stamp(package)
    assert stamp and reason is None
    doc = core.read_json(version.root / STAMP_NAME)
    assert doc["stamp"] == stamp
    for relative, digest in doc["files"].items():
        path = version.root / relative
        assert not path.stat().st_mode & 0o222
        assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
    for relative in doc["directories"]:
        assert not (version.root / relative).stat().st_mode & 0o222
    assert _relative(version, _file(package)) in doc["files"]
    assert _relative(version, package / "packs/sample/pack.yaml") in doc["files"]
    assert _relative(version, package / "__init__.py") in doc["files"]
    assert "manifest.json" in doc["files"]
    assert not (version.root / STAMP_NAME).stat().st_mode & 0o222
    # Own/project packs and Runtime data live elsewhere and never enter a stamp.
    own = data / "own" / "pack.yaml"
    project = data / "project" / "astrid" / "packs" / "local" / "pack.yaml"
    for path in (own, project):
        path.parent.mkdir(parents=True)
        path.write_text("changed")
    assert core.install_stamp(package) == (stamp, None)
    assert core.install_stamp_line(package) == stamp


def test_stamp_refuses_writable_and_edited_shipped_file(installed):
    _, _, stage = installed
    version, package = stage("first")
    path = _file(package)
    before = path.read_bytes()
    modified = b"x" * len(before)
    reason = "shipped_file_modified:" + _relative(version, path)
    try:
        path.write_bytes(modified)
    except PermissionError:
        assert path.read_bytes() == before
        assert core.install_stamp(package)[0]
    else:
        # Root/admin may bypass read-only: the content digest still catches it.
        assert core.install_stamp(package) == (None, reason)
        path.write_bytes(before)
    _enable_write(path)
    assert core.install_stamp(package) == (None, reason)
    path.write_bytes(modified)
    _readonly(path)
    assert core.install_stamp(package) == (None, reason)
    assert core.install_stamp_line(package) == "None:" + reason
    assert path.read_bytes() == modified  # checking never repairs the file


@pytest.mark.parametrize("change", ["added", "removed"])
def test_stamp_refuses_added_or_removed_shipped_leaf(installed, change):
    _, _, stage = installed
    version, package = stage("first")
    path = _file(package)
    parent = path.parent
    _enable_write(parent)
    if change == "added":
        path = parent / "another.yaml"
        path.write_text("new")
        _readonly(path)
    else:
        _enable_write(path)  # Windows refuses unlink of a read-only file.
        path.unlink()
    _readonly(parent)
    assert core.install_stamp(package) == (None, "shipped_file_modified:" + _relative(version, path))


def test_stamp_never_chmods_uv_cache_inode(installed, tmp_path):
    _, _, stage = installed
    cache = tmp_path / "cached-action.py"
    cache.write_text("VALUE = 1\n")
    mode = cache.stat().st_mode
    _, package = stage("first", shared=cache)
    installed_path = package / "packs/sample/action.py"
    assert not os.path.samefile(cache, installed_path)
    assert cache.stat().st_mode == mode
    assert cache.read_bytes() == installed_path.read_bytes()
    assert core.install_stamp(package)[0]


def test_stamp_refuses_development_and_legacy_or_missing_manifest(installed, tmp_path):
    _, _, stage = installed
    dev = tmp_path / "checkout" / "astrid"
    dev.mkdir(parents=True)
    assert core.install_stamp(dev) == (None, "not_managed_install")
    assert core.install_stamp() == (None, "not_managed_install")
    _, package = stage("legacy", seal=False)
    assert core.install_stamp(package) == (None, "missing_stamp_manifest")


def test_stamp_refuses_tampered_manifest(installed):
    _, _, stage = installed
    version, package = stage("first")
    target = version.root / STAMP_NAME
    _enable_write(target)
    doc = core.read_json(target)
    doc["stamp"] = "0" * 64
    target.write_text(json.dumps(doc), encoding="utf-8")
    _readonly(target)
    assert core.install_stamp(package) == (None, "stamp_manifest_mismatch")


def test_stamp_same_in_second_process(installed):
    _, _, stage = installed
    _, package = stage("first")
    stamp, reason = core.install_stamp(package)
    assert stamp and reason is None
    output = subprocess.check_output(
        [sys.executable, "-B", "-c",
         "import json,sys; from pathlib import Path; from astrid.installer.core import install_stamp; "
         "print(json.dumps(install_stamp(Path(sys.argv[1]))))", str(package)],
        text=True,
    )
    assert json.loads(output) == [stamp, None]


def test_update_new_stamp_and_finish_removes_older_readonly_version(installed):
    program, _, stage = installed
    first, first_package = stage("first")
    second, second_package = stage("second", content="model: two\n")
    third, third_package = stage("third", content="model: two\n")
    stamps = [core.install_stamp(package)[0] for package in (first_package, second_package, third_package)]
    assert all(stamps) and len(set(stamps)) == 3
    core.switch(program, first)
    core.switch(program, second)
    core.switch(program, third)
    backend = SimpleNamespace(root=program, managed_by="pinokio")
    Backend.finish(backend, second, third, None)
    assert not first.root.exists()
    assert second.root.exists()
    assert core.current(program) == third
    assert core.install_stamp(third_package) == (stamps[2], None)


def test_rollback_selects_legacy_runtime_without_requiring_stamp(installed):
    program, data, stage = installed
    old, old_package = stage("old", release=14, seal=False)
    candidate, candidate_package = stage("candidate")
    stamp = core.install_stamp(candidate_package)[0]
    assert stamp
    core.switch(program, candidate)
    actions = []
    core.rollback_transaction(
        program, data, SimpleNamespace(),
        {"old": old.document(), "candidate": candidate.document(),
         "realm": {"realm_id": "realm"}, "transaction_kind": "app", "step": "selected"},
        stop=lambda version, _data: actions.append(("stop", version.root)),
        switch_old=lambda previous, _candidate: core.switch(program, previous),
        activate_old=lambda version, _data, _realm: actions.append(("activate", version.root)),
        save=lambda step: actions.append(("step", step)),
    )
    assert core.current(program) == old
    assert actions == [("stop", candidate.root), ("step", "recovering"),
                       ("step", "rollback_selected"), ("activate", old.root), ("step", "rolled_back")]
    assert core.install_stamp(old_package) == (None, "missing_stamp_manifest")
    assert core.install_stamp(candidate_package) == (stamp, None)


def test_uninstall_removes_sealed_program_and_keeps_workspace(installed, tmp_path, monkeypatch):
    program, data, stage = installed
    version, package = stage("first")
    assert core.install_stamp(package)[0]
    core.switch(program, version)
    retained = data / "user.txt"
    retained.write_text("keep")
    home = tmp_path / "home"
    home.mkdir()
    selection = home / "selection.json"
    core.write_json(selection, {"owner": "haus.astrid.runtime", "program_root": str(program),
                                "data_root": str(data)})
    service = SimpleNamespace(home=home, system="win32" if os.name == "nt" else "linux", selection=selection)
    backend = SimpleNamespace(service=service, managed_by="pinokio", uv=tmp_path / "uv",
                              preflight_remove=lambda *_args, **_kwargs: None)
    monkeypatch.setattr("astrid.installer.backend.remove_owned_command", lambda *_args: None)
    assert Backend.remove(backend, program, data, delete_data=False) == []
    assert not program.exists()
    assert retained.read_text() == "keep"
