"""A copied context cannot extend the lifetime of a discovery cache."""
from contextvars import copy_context
from threading import Event, Thread

import pytest

from astrid.core.pack import discovery


def test_inflight_projection_cannot_publish_after_scope_revocation(monkeypatch):
    monkeypatch.setattr(discovery, "_discovery_fingerprint", lambda *args, **kwargs: "same")
    from astrid.core.pack import source_setup

    monkeypatch.setattr(source_setup, "active_source_inventory", lambda: object())
    entered_projection = Event()
    release_projection = Event()
    calls = []
    results = []
    failures = []

    def projection():
        calls.append(len(calls) + 1)
        call_number = calls[-1]
        if call_number == 1:
            entered_projection.set()
            if not release_projection.wait(timeout=5):
                raise AssertionError("test did not release the in-flight projection")
        return f"result-{call_number}"

    worker = None
    try:
        with discovery.pack_discovery_scope():
            owner = discovery._discovery_cache.get()
            copied = copy_context()

            def run_projection():
                try:
                    results.append(copied.run(discovery.reuse_pack_projection, projection))
                except BaseException as exc:
                    failures.append(exc)

            worker = Thread(target=run_projection, daemon=True)
            worker.start()
            assert entered_projection.wait(timeout=5), "worker did not enter the cache-miss projection"
    finally:
        # The scope has exited and revoked its owner before the blocked
        # projection is released, even when the synchronization assertion fails.
        release_projection.set()
        if worker is not None:
            worker.join(timeout=5)

    assert not worker.is_alive(), "in-flight projection did not finish"
    assert failures == []
    assert results == ["result-1"]
    assert not owner.active
    assert owner == {}, "a result completing after revocation must not repopulate the owner"

    # The copied context still carries the revoked owner, so a later call must
    # recompute instead of observing the late result.
    assert copied.run(discovery.reuse_pack_projection, projection) == "result-2"
    assert calls == [1, 2]
    assert owner == {}


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
