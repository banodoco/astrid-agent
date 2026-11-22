"""A copied context cannot extend the lifetime of a discovery cache."""
from contextvars import copy_context

import pytest

from astrid.core.pack import discovery


@pytest.mark.parametrize("fail", [False, True])
def test_copied_context_loses_owner_on_scope_exit(fail, monkeypatch):
    monkeypatch.setattr(discovery, "_discovery_fingerprint", lambda *args, **kwargs: "same")
    from astrid.core.pack import source_setup
    monkeypatch.setattr(source_setup, "active_source_inventory", lambda: object())
    calls = []

    def projection():
        calls.append(len(calls))
        return calls[-1]

    def command():
        with discovery.pack_discovery_scope():
            owner = discovery._discovery_cache.get()
            copied = copy_context()
            assert discovery.reuse_pack_projection(projection) == 0
            with discovery.pack_discovery_scope():
                assert discovery._discovery_cache.get() is owner
                assert copied.run(discovery.reuse_pack_projection, projection) == 0
            assert owner.active and owner
            if fail:
                raise RuntimeError("command failure")
            return owner, copied

    if fail:
        # Capture a copied context and its owner before the exceptional exit.
        captured = []
        def failing_command():
            with discovery.pack_discovery_scope():
                owner = discovery._discovery_cache.get()
                copied = copy_context()
                captured.extend((owner, copied))
                assert discovery.reuse_pack_projection(projection) == 0
                raise RuntimeError("command failure")
        with pytest.raises(RuntimeError, match="command failure"):
            failing_command()
        owner, copied = captured
    else:
        owner, copied = command()

    assert not owner.active
    assert owner == {}
    assert copied.run(discovery.reuse_pack_projection, projection) == 1
    assert copied.run(discovery.reuse_pack_projection, projection) == 2
    assert owner == {}

    def new_command():
        with discovery.pack_discovery_scope():
            fresh = discovery._discovery_cache.get()
            assert fresh is not owner and fresh.active
            assert discovery.reuse_pack_projection(projection) == 3
            assert discovery.reuse_pack_projection(projection) == 3
        assert not fresh.active and fresh == {}
        assert discovery._discovery_cache.get() is owner

    copied.run(new_command)
    assert copied.run(discovery.reuse_pack_projection, projection) == 4


def test_expired_context_does_not_reuse_discovered_metadata(tmp_path, monkeypatch):
    import json
    from astrid.core.pack import loader

    root = tmp_path / "source"
    pack = root / "demo"
    pack.mkdir(parents=True)
    (pack / "pack.yaml").write_text(json.dumps({
        "schema_version": 3, "id": "demo", "name": "Demo", "version": "1.0.0", "pack_api": "2.0",
        "documentation": {"kind": "none", "reason": "Metadata fixture."},
    }))
    monkeypatch.setattr(loader, "packs_root", lambda: root)
    monkeypatch.setattr(type(tmp_path), "home", classmethod(lambda cls: tmp_path / "home"))
    monkeypatch.setenv("ASTRID_PACKS_PATH", "")
    calls = []
    original = loader.load_pack_manifest

    def load(*args, **kwargs):
        if args[0] == pack / "pack.yaml":
            calls.append(args[0])
        return original(*args, **kwargs)

    monkeypatch.setattr(loader, "load_pack_manifest", load)
    with discovery.pack_discovery_scope():
        copied = copy_context()
        owner = discovery._discovery_cache.get()
        discovery.discover_pack_metadata(project_root=tmp_path)
        copied.run(discovery.discover_pack_metadata, project_root=tmp_path)
        assert len(calls) == 1
    assert not owner and not owner.active
    copied.run(discovery.discover_pack_metadata, project_root=tmp_path)
    copied.run(discovery.discover_pack_metadata, project_root=tmp_path)
    assert len(calls) == 3
    assert not owner
