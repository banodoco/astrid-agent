"""Duplicate directory admission precedes managed inventory verification."""

from contextlib import nullcontext

import pytest

from astrid.core.pack import discovery, loader, personal, source_setup
from astrid.core.pack._common import PackValidationError


@pytest.fixture
def roots(tmp_path, monkeypatch):
    source = tmp_path / "source"
    extra = tmp_path / "extra"
    source.mkdir()
    extra.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("ASTRID_PACKS_PATH", raising=False)
    monkeypatch.setattr(loader, "packs_root", lambda: source)
    monkeypatch.setattr(discovery, "PACKAGE_ROOT", tmp_path / "package")
    monkeypatch.setattr(personal, "resolve_personal_packs_root", lambda: tmp_path / "personal")
    monkeypatch.setattr(source_setup, "active_sources", lambda **kwargs: ())
    calls = []

    def inventory():
        calls.append("inventory")
        return source_setup.ManagedSourceInventory(sources=(), identity="empty")

    monkeypatch.setattr(source_setup, "active_source_inventory", inventory)
    return source, extra, calls


@pytest.mark.parametrize("scoped", [False, True])
def test_duplicate_refused_before_inventory_on_repeated_attempts(tmp_path, roots, scoped):
    source, extra, calls = roots
    (source / "same").mkdir()
    (extra / "same").mkdir()
    with discovery.pack_discovery_scope() if scoped else nullcontext():
        for _ in range(2):
            with pytest.raises(PackValidationError, match="duplicate pack"):
                discovery.discover_pack_metadata(project_root=tmp_path, extra_pack_roots=(extra,))
            assert calls == []


def test_duplicate_added_after_cached_discovery_is_refused_before_inventory(tmp_path, roots):
    source, extra, calls = roots
    with discovery.pack_discovery_scope():
        assert discovery.discover_pack_metadata(project_root=tmp_path, extra_pack_roots=(extra,)) == ()
        assert calls
        calls.clear()
        (source / "same").mkdir()
        (extra / "same").mkdir()
        with pytest.raises(PackValidationError, match="duplicate pack"):
            discovery.discover_pack_metadata(project_root=tmp_path, extra_pack_roots=(extra,))
        assert calls == []


def test_unresolvable_declarations_path_does_not_change_empty_discovery(tmp_path, roots, monkeypatch):
    _, extra, _ = roots
    loop = tmp_path / "declarations-loop"
    loop.symlink_to(loop)
    monkeypatch.setenv("ASTRID_SOURCE_DECLARATIONS", str(loop))
    unscoped = discovery.discover_pack_metadata(project_root=tmp_path, extra_pack_roots=(extra,))
    with discovery.pack_discovery_scope():
        scoped = discovery.discover_pack_metadata(project_root=tmp_path, extra_pack_roots=(extra,))
        assert discovery.discover_pack_metadata(project_root=tmp_path, extra_pack_roots=(extra,)) == scoped
    assert scoped == unscoped == ()
