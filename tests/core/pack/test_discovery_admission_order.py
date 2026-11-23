"""Duplicate directory admission precedes managed inventory verification."""

from contextlib import nullcontext
from pathlib import Path

import pytest

from astrid.core.pack import discovery, loader, personal, source_setup
from astrid.core.pack import transaction
from astrid.core.pack._common import PackValidationError
from astrid.core.pack.install import PackInstallError


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


@pytest.mark.parametrize("scoped", [False, True])
def test_action_entries_refuse_duplicate_before_dirty_managed_inventory(monkeypatch, scoped):
    from astrid.core.generation import entries

    project = Path(__file__).resolve().parents[3]
    monkeypatch.setenv("HOME", "/nonexistent-pb7-review-home")
    monkeypatch.setenv("ASTRID_PACKS_PATH", str(project / "tests" / "core"))
    monkeypatch.setattr(loader, "packs_root", lambda: project / "astrid" / "core")
    monkeypatch.setattr(source_setup, "active_sources", lambda **kwargs: ())

    def dirty_inventory():
        raise source_setup.SourceSetupError("dirty managed checkout")

    monkeypatch.setattr(source_setup, "active_source_inventory", dirty_inventory)
    with discovery.pack_discovery_scope() if scoped else nullcontext():
        with pytest.raises(PackValidationError, match="duplicate pack id 'generation'"):
            entries.action_entries()


@pytest.mark.parametrize("scoped", [False, True])
def test_discovery_refuses_busy_collection_after_warm_metadata(tmp_path, roots, monkeypatch, scoped):
    _, extra, _ = roots
    with discovery.pack_discovery_scope() if scoped else nullcontext():
        assert discovery.discover_pack_metadata(project_root=tmp_path, extra_pack_roots=(extra,)) == ()
        _set_pending_exchange(tmp_path, extra, monkeypatch)
        with pytest.raises(PackInstallError) as caught:
            discovery.discover_pack_metadata(project_root=tmp_path, extra_pack_roots=(extra,))
    assert caught.value.code == "transaction_busy"


def _set_pending_exchange(tmp_path, collection, monkeypatch):
    journal = tmp_path / "busy-journal"
    journal.write_text("pending")
    original_exchange_journal = transaction._exchange_journal
    monkeypatch.setattr(
        transaction,
        "_exchange_journal",
        lambda root: journal if root == collection
        else original_exchange_journal(root),
    )


@pytest.mark.parametrize("scoped", [False, True])
def test_action_entries_refuse_busy_collection_after_warm_projection(tmp_path, roots, monkeypatch, scoped):
    from astrid.core.generation import entries

    _, extra, _ = roots
    monkeypatch.setenv("ASTRID_PACKS_PATH", str(extra))
    with discovery.pack_discovery_scope() if scoped else nullcontext():
        assert entries.action_entries() == []
        _set_pending_exchange(tmp_path, extra, monkeypatch)
        with pytest.raises(PackInstallError) as caught:
            entries.action_entries()
    assert caught.value.code == "transaction_busy"
