"""Root snapshots avoid repeat walks while detecting changes to pack trees."""

import mmap
import os
from pathlib import Path
import sys

import pytest

from astrid.core.pack.discovery import _DiscoveryCache, _RootSnapshot


pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux metadata snapshots")


def test_unchanged_root_reuses_snapshot_without_enumerating(tmp_path, monkeypatch):
    nested = tmp_path / "pack" / "nested"
    nested.mkdir(parents=True)
    (nested / "resource").write_text("original")
    cache = _DiscoveryCache()
    identity = cache.tree_fingerprint(tmp_path)
    snapshot = _RootSnapshot(tmp_path)

    def fail_iterdir(_path):
        raise AssertionError("unchanged lookup re-enumerated a directory")

    monkeypatch.setattr(Path, "iterdir", fail_iterdir)
    assert snapshot.unchanged()
    assert cache.tree_fingerprint(tmp_path) == identity


def test_root_changes_invalidate_snapshot(tmp_path):
    nested = tmp_path / "pack" / "nested"
    nested.mkdir(parents=True)
    resource = nested / "resource"
    resource.write_bytes(b"original")
    cache = _DiscoveryCache()
    identity = cache.tree_fingerprint(tmp_path)
    snapshot = _RootSnapshot(tmp_path)
    before = resource.stat()

    with resource.open("r+b") as stream:
        with mmap.mmap(stream.fileno(), 0) as mapped:
            mapped[:] = b"modified"
            mapped.flush()
    os.utime(resource, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert resource.stat().st_size == before.st_size
    assert resource.stat().st_mtime_ns == before.st_mtime_ns
    assert not snapshot.unchanged()
    changed = cache.tree_fingerprint(tmp_path)
    assert changed != identity

    added = nested / "added"
    added.write_text("new")
    after_add = cache.tree_fingerprint(tmp_path)
    assert after_add != changed
    added.unlink()
    assert cache.tree_fingerprint(tmp_path) != after_add


def test_discovery_callers_share_scan_and_rescan_changed_root(tmp_path, monkeypatch):
    from astrid.core.pack import discovery, loader, personal, source_setup

    source = tmp_path / "source"
    source.mkdir()
    personal_root = tmp_path / "personal-root"
    personal_root.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("ASTRID_PACKS_PATH", raising=False)
    monkeypatch.setattr(loader, "packs_root", lambda: source)
    monkeypatch.setattr(discovery, "PACKAGE_ROOT", tmp_path / "package")
    monkeypatch.setattr(personal, "resolve_personal_packs_root", lambda: personal_root)
    monkeypatch.setattr(source_setup, "active_sources", lambda **_kwargs: ())
    monkeypatch.setattr(
        source_setup, "active_source_inventory",
        lambda: source_setup.ManagedSourceInventory(sources=(), identity="empty"),
    )
    scan_calls = []
    original_scan = loader.scan_packs

    def scan(*args, **kwargs):
        scan_calls.append(args[0])
        return original_scan(*args, **kwargs)

    monkeypatch.setattr(loader, "scan_packs", scan)
    with discovery.pack_discovery_scope():
        assert discovery.discover_pack_metadata(project_root=tmp_path) == ()
        assert discovery.discover_packs_ordered(project_root=tmp_path) == ()
        assert scan_calls == [source]
        (source / "new-directory").mkdir()
        assert discovery.discover_pack_metadata(project_root=tmp_path) == ()
        assert scan_calls == [source, source]
