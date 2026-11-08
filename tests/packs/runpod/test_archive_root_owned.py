"""Every source archive member is root-owned, whatever uid owns the local tree."""
import os
import tarfile

import pytest

from astrid.packs.runpod import remote_placement as rp


@pytest.fixture
def tree(tmp_path, monkeypatch):
    src = tmp_path / "src"
    for directory in rp.SOURCE_DIRS:
        (src / directory / ".git").mkdir(parents=True)
        (src / directory / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
        (src / directory / "mod.py").write_text("x = 1\n")
    monkeypatch.setattr(rp, "pod_source_manifest", lambda root, pins=None: b"{}\n")
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        uid, gid = 1000, 1000
        for path in [src, *src.rglob("*")]:
            os.chown(path, uid, gid)
    return src


def test_every_member_is_root_owned(tree, tmp_path):
    archive_path = rp._build_source_archive(tree, tmp_path / "pod.tgz")
    with tarfile.open(archive_path, "r:gz") as archive:
        members = archive.getmembers()
    assert members, "archive is empty"
    assert any(m.name.endswith(".git/HEAD") for m in members)
    assert any(m.name == "src/.pod-source.json" for m in members)
    for member in members:
        assert (member.uid, member.gid) == (0, 0), member.name
        assert (member.uname, member.gname) == ("root", "root"), member.name


def test_pyc_and_pycache_still_dropped(tree, tmp_path):
    first = rp.SOURCE_DIRS[0]
    (tree / first / "__pycache__").mkdir()
    (tree / first / "__pycache__" / "m.cpython-311.pyc").write_bytes(b"\0")
    (tree / first / "stale.pyc").write_bytes(b"\0")
    archive_path = rp._build_source_archive(tree, tmp_path / "pod.tgz")
    with tarfile.open(archive_path, "r:gz") as archive:
        names = archive.getnames()
    assert not any(n.endswith(".pyc") or "__pycache__" in n for n in names)
