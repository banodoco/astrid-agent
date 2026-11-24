"""Round-3 freshness witnesses: bytecode, metadata, records and cleanup."""

from __future__ import annotations

import os
import py_compile
import stat
import sys
from dataclasses import replace

import pytest

from astrid.installer import _install_stamp, core
from astrid.installer._pack_cleanup import remove_pack_tree
from tests.installer.test_install_stamp import (
    _enable_write,
    _file,
    _readonly,
    _relative,
    installed,  # noqa: F401 - pytest fixture
)


@pytest.mark.posix_native
def test_default_stamp_uses_no_shipped_byte_hashes(installed, monkeypatch):  # noqa: F811
    _, _, stage = installed
    version, package = stage("cheap")
    expected = core.install_stamp(package)
    assert expected[0] and expected[1] is None
    digest = _install_stamp._digest
    observed = []

    def record_digest(path):
        observed.append(_relative(version, path))
        return digest(path)

    monkeypatch.setattr(_install_stamp, "_digest", record_digest)
    assert core.install_stamp(package) == expected
    assert observed == []
    assert core.install_stamp(package, verify=True) == expected
    assert sorted(observed) == sorted(core.read_json(version.root / _install_stamp.STAMP_NAME)["files"])


@pytest.mark.posix_native
def test_stat_stamp_rejects_same_size_edit_with_restored_mtime(installed):  # noqa: F811
    _, _, stage = installed
    version, package = stage("mtime")
    path = _file(package)
    original = path.stat()
    data = path.read_bytes()
    _enable_write(path)
    path.write_bytes(b"x" * len(data))
    os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))
    _readonly(path)
    after = path.stat()
    assert after.st_size == original.st_size
    assert after.st_mtime_ns == original.st_mtime_ns
    assert stat.S_IMODE(after.st_mode) == stat.S_IMODE(original.st_mode)
    assert after.st_ctime_ns != original.st_ctime_ns
    expected = (None, "shipped_file_modified:" + _relative(version, path))
    assert core.install_stamp(package) == expected
    assert core.install_stamp(package, verify=True) == expected


@pytest.mark.posix_native
def test_explicit_verify_does_not_reseal_metadata_churn(installed):  # noqa: F811
    _, _, stage = installed
    version, package = stage("churn")
    path = _file(package)
    before = core.install_stamp(package)
    assert before[0] and before[1] is None
    manifest = version.root / _install_stamp.STAMP_NAME
    manifest_bytes = manifest.read_bytes()
    _enable_write(path)
    _readonly(path)
    refusal = (None, "shipped_file_modified:" + _relative(version, path))
    assert core.install_stamp(package) == refusal
    assert core.install_stamp(package, verify=True) == before
    assert core.install_stamp(package) == refusal
    assert manifest.read_bytes() == manifest_bytes


@pytest.mark.parametrize("damage", ["injected", "replaced"])
def test_stamp_covers_pyc_and_refuses_injection(installed, damage):  # noqa: F811
    _, _, stage = installed
    version, package = stage("pyc", seal=False)
    source = package / "packs/sample/action.py"
    pyc = source.parent / "__pycache__" / (
        "action." + sys.implementation.cache_tag + ".pyc")
    py_compile.compile(str(source), cfile=str(pyc), doraise=True)
    stamp = _install_stamp.write_install_stamp(version, package.parent)
    assert core.install_stamp(package) == (stamp, None)
    doc = core.read_json(version.root / _install_stamp.STAMP_NAME)
    assert _relative(version, pyc) in doc["files"]
    assert _relative(version, pyc.parent) in doc["directories"]
    assert not pyc.stat().st_mode & 0o222
    assert not pyc.parent.stat().st_mode & 0o222
    if damage == "injected":
        target = pyc.with_name("unlisted.pyc")
        _enable_write(pyc.parent)
        target.write_bytes(pyc.read_bytes())
        _readonly(target)
        _readonly(pyc.parent)
    else:
        target = pyc
        payload = target.read_bytes()
        _enable_write(target)
        target.write_bytes(payload[:-1] + bytes([payload[-1] ^ 1]))
        _readonly(target)
    expected = (None, "shipped_file_modified:" + _relative(version, target))
    assert core.install_stamp(package) == expected
    assert core.install_stamp(package, verify=True) == expected


@pytest.mark.posix_native
def test_stamp_refuses_tree_link_and_never_touches_target(installed, tmp_path):  # noqa: F811
    _, _, stage = installed
    version, package = stage("link")
    outside = tmp_path / "outside.py"
    outside.write_bytes(b"outside")
    original = outside.stat()
    parent = package / "packs/sample"
    target = parent / "alias.py"
    _enable_write(parent)
    target.symlink_to(outside)
    _readonly(parent)
    assert core.install_stamp(package) == (None, "shipped_file_modified:" + _relative(version, target))
    assert outside.stat().st_mode == original.st_mode
    assert outside.read_bytes() == b"outside"


@pytest.mark.posix_native
def test_stamp_names_link_in_ancestor(installed, tmp_path):  # noqa: F811
    program, _, stage = installed
    _, package = stage("ancestor")
    alias = tmp_path / "alias-program"
    alias.symlink_to(program, target_is_directory=True)
    assert core.install_stamp(alias / package.relative_to(program)) == (None, f"linked_ancestor:{alias}")


@pytest.mark.posix_native
def test_stamp_names_uid_mismatch_and_foreign_install_owner(installed, monkeypatch):  # noqa: F811
    _, _, stage = installed
    version, package = stage("owner")
    uid = os.geteuid()
    assert core.install_stamp(package)[0]
    with monkeypatch.context() as patch:
        patch.setattr(_install_stamp.os, "getuid", lambda: uid + 1)
        assert core.install_stamp(package) == (None, "install_uid_mismatch")
    with monkeypatch.context() as patch:
        patch.setattr(_install_stamp, "_effective_uid", lambda: uid + 1)
        assert core.install_stamp(package) == (None, f"install_owner_mismatch:{version.root}")


def test_pinokio_adoption_keeps_same_shipped_stamp(installed):  # noqa: F811
    program, _, stage = installed
    version, package = stage("adopt")
    before = core.install_stamp(package)
    assert before[0] and before[1] is None
    marker = core.read_json(program / "install.json")
    marker["managed_by"] = "pinokio"
    core.write_json(program / "install.json", marker)
    assert core.install_stamp(package) == before
    adopted = replace(version, managed_by="pinokio")
    core.persist_install_identity(adopted, program)
    assert core.install_stamp(package) == before
    assert core.install_stamp(package, verify=True) == before
    record = core.read_json(version.root / "version.json")
    record["identity"]["release"] += 1
    core.write_json(version.root / "version.json", record)
    assert core.install_stamp(package) == (None, "stamp_manifest_mismatch")


@pytest.mark.parametrize("name", ["install-stamp.json", "version.json", "profile.json"])
def test_stamp_names_bad_json(installed, name):  # noqa: F811
    _, _, stage = installed
    version, package = stage("bad-json")
    path = version.root / name
    _enable_write(path)
    path.write_text("{", encoding="utf-8")
    if name == _install_stamp.STAMP_NAME:
        _readonly(path)
    assert core.install_stamp(package) == (None, f"invalid_stamp_json:{name}")


def test_stamp_names_missing_version_key(installed):  # noqa: F811
    _, _, stage = installed
    version, package = stage("missing-key")
    record_path = version.root / "version.json"
    record = core.read_json(record_path)
    del record["identity"]["release"]
    core.write_json(record_path, record)
    assert core.install_stamp(package) == (None, "missing_version_key:identity.release")


@pytest.mark.posix_native
@pytest.mark.non_root
def test_cleanup_repairs_unreadable_private_subdirectory(tmp_path):
    tree = tmp_path / "owned"
    child = tree / "unreadable"
    child.mkdir(parents=True)
    (child / "payload").write_bytes(b"sealed")
    outside = tmp_path / "external"
    outside.mkdir()
    (outside / "keep").write_bytes(b"external")
    (tree / "alias").symlink_to(outside, target_is_directory=True)
    external_mode = outside.stat().st_mode
    child.chmod(0)
    tree.chmod(0o500)
    remove_pack_tree(tree)
    assert not tree.exists()
    assert outside.stat().st_mode == external_mode
    assert (outside / "keep").read_bytes() == b"external"
