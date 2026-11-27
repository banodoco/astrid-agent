"""Changes to configured source declarations invalidate discovery reuse."""

import json

import pytest

from astrid.core.pack import discovery, loader, personal, source_setup


def test_changed_source_declarations_are_read_again_in_scope(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    personal_root = tmp_path / "personal-root"
    personal_root.mkdir()
    declarations = tmp_path / "declarations.json"
    declarations.write_text("[]", encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("ASTRID_SOURCE_DECLARATIONS", str(declarations))
    monkeypatch.delenv("ASTRID_PACKS_PATH", raising=False)
    monkeypatch.setattr(loader, "packs_root", lambda: source)
    monkeypatch.setattr(discovery, "PACKAGE_ROOT", tmp_path / "package")
    monkeypatch.setattr(personal, "resolve_personal_packs_root", lambda: personal_root)
    monkeypatch.setattr(source_setup, "active_sources", lambda **_kwargs: ())
    monkeypatch.setattr(
        source_setup, "active_source_inventory",
        lambda: source_setup.ManagedSourceInventory(sources=(), identity="empty"),
    )
    calls = []

    def isolate(packs, **_kwargs):
        calls.append(1)
        source_setup.declarations_from_json()
        return packs, []

    monkeypatch.setattr(loader, "_isolate_incompatible_packs", isolate)
    with discovery.pack_discovery_scope():
        assert discovery.discover_pack_metadata(project_root=tmp_path) == ()
        assert discovery.discover_pack_metadata(project_root=tmp_path) == ()
        assert len(calls) == 1
        declarations.write_text("[invalid", encoding="utf-8")
        with pytest.raises(json.JSONDecodeError):
            discovery.discover_pack_metadata(project_root=tmp_path)
        assert len(calls) == 2
