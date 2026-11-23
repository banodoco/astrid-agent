"""Concurrency contracts for the shared element registry discovery cache."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

from astrid.core.element import registry


def _clear_cache():
    registry.clear_default_registry_cache()


def test_clear_waits_for_inflight_discovery_then_forces_new_catalog(monkeypatch, tmp_path: Path):
    old_started = threading.Event()
    release_old = threading.Event()
    clear_started = threading.Event()
    discovery_count = 0

    class FakeRegistry:
        def __init__(self, elements=(), **_kwargs):
            self.elements = tuple(elements)

    def discover_data(*_key):
        nonlocal discovery_count
        discovery_count += 1
        if discovery_count == 1:
            old_started.set()
            assert release_old.wait(10)
            return (("old",), registry.ELEMENT_KIND_REGISTRY, ())
        return (("new",), registry.ELEMENT_KIND_REGISTRY, ())

    monkeypatch.setattr(registry, "_pack_roots", lambda *_args: (tmp_path / "packs",))
    monkeypatch.setattr(registry, "_root_state", lambda _roots: (0,))
    monkeypatch.setattr(registry, "_load_default_registry_data", discover_data)
    monkeypatch.setattr(registry, "ElementRegistry", FakeRegistry)
    monkeypatch.setattr(registry, "create_shared_alias_resolver", lambda: None)
    monkeypatch.setattr(registry, "_register_pack_aliases", lambda *_args: None)
    _clear_cache()

    with ThreadPoolExecutor(max_workers=2) as pool:
        old_discovery = pool.submit(registry.load_default_registry, project_root=tmp_path)
        assert old_started.wait(10)
        clear = pool.submit(lambda: (clear_started.set(), registry.clear_default_registry_cache()))
        assert clear_started.wait(10)
        assert not clear.done()
        release_old.set()
        old_registry = old_discovery.result(timeout=10)
        assert old_registry.elements == ("old",)
        clear.result(timeout=10)

    newest = registry.load_default_registry(project_root=tmp_path)
    assert newest.elements == ("new",)
    assert discovery_count == 2
    _clear_cache()


def test_discovery_for_root_b_finishes_while_root_a_discovery_is_paused(monkeypatch, tmp_path: Path):
    root_a_started = threading.Event()
    release_root_a = threading.Event()

    def load_data(project_root_key, *_args):
        if Path(project_root_key).name == "root-a":
            root_a_started.set()
            assert release_root_a.wait(10)
        return ((), registry.ELEMENT_KIND_REGISTRY, ())

    monkeypatch.setattr(registry, "_pack_roots", lambda project_root, _extra: (Path(project_root),))
    monkeypatch.setattr(registry, "_root_state", lambda _roots: (0,))
    monkeypatch.setattr(registry, "_load_default_registry_data", load_data)
    _clear_cache()
    root_a, root_b = tmp_path / "root-a", tmp_path / "root-b"
    registry.load_default_registry(project_root=root_b)  # warm B before A starts

    with ThreadPoolExecutor(max_workers=1) as pool:
        paused = pool.submit(registry.load_default_registry, project_root=root_a)
        assert root_a_started.wait(10)
        # A warm or cold lookup for another root must not queue behind A's scan.
        result_b = registry.load_default_registry(project_root=root_b)
        assert result_b is not None
        release_root_a.set()
        assert paused.result(timeout=10) is not None
    _clear_cache()


def test_install_during_discovery_is_seen_on_the_next_lookup_retry(monkeypatch, tmp_path: Path):
    packs_root = tmp_path / "packs"
    packs_root.mkdir()
    discovery_started = threading.Event()
    release_discovery = threading.Event()
    calls = 0

    def load_data(*_key):
        nonlocal calls
        calls += 1
        if calls == 1:
            discovery_started.set()
            assert release_discovery.wait(10)
            return ((), registry.ELEMENT_KIND_REGISTRY, ())
        return (("installed",), registry.ELEMENT_KIND_REGISTRY, ())

    class FakeRegistry:
        def __init__(self, elements=(), **_kwargs):
            self.elements = set(elements)

        def register(self, element):
            self.elements.add(element)

        def get(self, _kind, element_id):
            return element_id if element_id in self.elements else None

    monkeypatch.setattr(registry, "ElementRegistry", FakeRegistry)
    monkeypatch.setattr(registry, "create_shared_alias_resolver", lambda: None)
    monkeypatch.setattr(registry, "_register_pack_aliases", lambda *_args: None)
    monkeypatch.setattr(registry, "_pack_roots", lambda *_args: (packs_root,))
    monkeypatch.setattr(registry, "_load_default_registry_data", load_data)
    _clear_cache()

    with ThreadPoolExecutor(max_workers=1) as pool:
        initial = pool.submit(registry.load_default_registry, project_root=tmp_path)
        assert discovery_started.wait(10)
        (packs_root / "installed-pack").mkdir()
        release_discovery.set()
        initial.result(timeout=10)

    found, refreshed = registry.lookup_default_registry(
        lambda loaded: loaded.get("effects", "installed"), project_root=tmp_path
    )
    assert found == "installed"
    assert "installed" in refreshed.elements
    assert calls == 2
    _clear_cache()


def test_catalog_old_assembly_cannot_refill_after_newer_registry_publication(monkeypatch):
    from dataclasses import replace

    from astrid.core.element import catalog

    base = registry.load_default_registry()
    template = base.get("effects", "text-card")
    old_definition = replace(
        template,
        id="catalog-race-old",
        metadata={**template.metadata, "publication": "old"},
    )
    new_definition = replace(
        template,
        id="catalog-race-new",
        metadata={**template.metadata, "publication": "new"},
    )
    old_registry = registry.ElementRegistry([old_definition])
    new_registry = registry.ElementRegistry([new_definition])
    old_assembly_started = threading.Event()
    release_old_assembly = threading.Event()
    current = {"registry": old_registry}
    assembly_count = 0

    def assemble(*, project_root):
        nonlocal assembly_count
        assembly_count += 1
        if assembly_count == 1:
            snapshot = old_registry
            old_assembly_started.set()
            assert release_old_assembly.wait(10)
            return snapshot
        return current["registry"]

    def lookup(lookup_fn, *, project_root=None):
        assembled = catalog._registry()
        return lookup_fn(assembled), assembled

    monkeypatch.setattr(catalog, "load_default_registry", assemble)
    monkeypatch.setattr(catalog, "lookup_default_registry", lookup)

    with ThreadPoolExecutor(max_workers=1) as pool:
        old_catalog_list = pool.submit(catalog.list_effect_ids)
        assert old_assembly_started.wait(10)
        current["registry"] = new_registry  # publish the newer registry while old assembly is paused
        release_old_assembly.set()
        assert "catalog-race-old" in old_catalog_list.result(timeout=10)

    ids = catalog.list_effect_ids()
    assert "catalog-race-new" in ids
    assert "catalog-race-old" not in ids
    assert catalog.read_effect_meta("catalog-race-new")["publication"] == "new"


def test_registry_root_state_decrease_still_triggers_refresh(monkeypatch, tmp_path: Path):
    states = iter(((100,), (99,)))
    loads = 0

    class FakeRegistry:
        def __init__(self, elements=(), **_kwargs):
            self.elements = tuple(elements)

        def get(self, _kind, element_id):
            return element_id if element_id in self.elements else None

    def load_data(*_key):
        nonlocal loads
        loads += 1
        return (("refreshed",) if loads > 1 else ("old",), registry.ELEMENT_KIND_REGISTRY, ())

    monkeypatch.setattr(registry, "_pack_roots", lambda *_args: (tmp_path / "packs",))
    monkeypatch.setattr(registry, "_root_state", lambda _roots: next(states))
    monkeypatch.setattr(registry, "_load_default_registry_data", load_data)
    monkeypatch.setattr(registry, "ElementRegistry", FakeRegistry)
    monkeypatch.setattr(registry, "create_shared_alias_resolver", lambda: None)
    monkeypatch.setattr(registry, "_register_pack_aliases", lambda *_args: None)
    _clear_cache()
    registry.load_default_registry(project_root=tmp_path)

    refreshed, _ = registry.lookup_default_registry(
        lambda loaded: loaded.get("effects", "refreshed"), project_root=tmp_path
    )
    assert refreshed == "refreshed"
    assert loads == 2  # the lower root stamp still triggers one refresh
    key = (str(tmp_path.resolve()), False, ())
    assert registry._default_registry_cache[key][1] == (99,)
    _clear_cache()


def test_blocked_miss_observes_registry_published_by_peer(monkeypatch, tmp_path: Path):
    miss_computed = threading.Event()
    release_miss = threading.Event()
    state = {"mtime": 0}
    discoveries = 0

    class FakeRegistry:
        def __init__(self, elements=(), **_kwargs):
            self.elements = set(elements)

        def register(self, element):
            self.elements.add(element)

        def get(self, _kind, element_id):
            return element_id if element_id in self.elements else None

    def load_data(*_key):
        nonlocal discoveries
        discoveries += 1
        return (("installed",) if discoveries > 1 else (), registry.ELEMENT_KIND_REGISTRY, ())

    monkeypatch.setattr(registry, "ElementRegistry", FakeRegistry)
    monkeypatch.setattr(registry, "create_shared_alias_resolver", lambda: None)
    monkeypatch.setattr(registry, "_register_pack_aliases", lambda *_args: None)
    monkeypatch.setattr(registry, "_pack_roots", lambda *_args: (tmp_path / "packs",))
    monkeypatch.setattr(registry, "_root_state", lambda _roots: (state["mtime"],))
    monkeypatch.setattr(registry, "_load_default_registry_data", load_data)
    _clear_cache()
    registry.load_default_registry(project_root=tmp_path)  # publish the old, empty registry

    def blocked_lookup(loaded):
        miss = loaded.get("effects", "installed")
        if threading.current_thread().name.startswith("reader_"):
            miss_computed.set()
            assert release_miss.wait(10)
        return miss

    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="reader") as pool:
        blocked_reader = pool.submit(registry.lookup_default_registry, blocked_lookup, project_root=tmp_path)
        assert miss_computed.wait(10)
        state["mtime"] = 1  # the pack install changes the discovery root
        found_by_peer, _ = registry.lookup_default_registry(
            lambda loaded: loaded.get("effects", "installed"), project_root=tmp_path
        )
        assert found_by_peer == "installed"
        release_miss.set()
        found_by_blocked, _ = blocked_reader.result(timeout=10)

    assert found_by_blocked == "installed"
    still_absent, _ = registry.lookup_default_registry(
        lambda loaded: loaded.get("effects", "still-absent"), project_root=tmp_path
    )
    assert still_absent is None
    assert discoveries == 2
    _clear_cache()


def test_miss_during_discovery_waits_for_and_returns_new_publication(monkeypatch, tmp_path: Path):
    """A concurrent miss waits for the discovery already refreshing its root."""
    discovery_started = threading.Event()
    release_discovery = threading.Event()
    arriving_started = threading.Event()
    state = {"mtime": 0}
    discoveries = 0

    class FakeRegistry:
        def __init__(self, elements=(), **_kwargs):
            self.elements = set(elements)

        def register(self, element):
            self.elements.add(element)

        def get(self, _kind, element_id):
            return element_id if element_id in self.elements else None

    def load_data(*_key):
        nonlocal discoveries
        discoveries += 1
        if discoveries == 1:
            return ((), registry.ELEMENT_KIND_REGISTRY, ())
        discovery_started.set()
        assert release_discovery.wait(10)
        return (("installed",), registry.ELEMENT_KIND_REGISTRY, ())

    monkeypatch.setattr(registry, "ElementRegistry", FakeRegistry)
    monkeypatch.setattr(registry, "create_shared_alias_resolver", lambda: None)
    monkeypatch.setattr(registry, "_register_pack_aliases", lambda *_args: None)
    monkeypatch.setattr(registry, "_pack_roots", lambda *_args: (tmp_path / "packs",))
    monkeypatch.setattr(registry, "_root_state", lambda _roots: (state["mtime"],))
    monkeypatch.setattr(registry, "_load_default_registry_data", load_data)
    _clear_cache()
    registry.load_default_registry(project_root=tmp_path)  # publish an empty registry
    state["mtime"] = 1  # the pack install changes the root before discovery starts

    def lookup(loaded):
        return loaded.get("effects", "installed")

    def arrive():
        arriving_started.set()
        return registry.lookup_default_registry(lookup, project_root=tmp_path)

    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="registry-miss") as pool:
        refreshing_miss = pool.submit(registry.lookup_default_registry, lookup, project_root=tmp_path)
        assert discovery_started.wait(10)
        arriving_miss = pool.submit(arrive)
        assert arriving_started.wait(10)
        # A second miss must queue behind discovery rather than start another scan.
        assert not arriving_miss.done()
        release_discovery.set()
        found_first, first_registry = refreshing_miss.result(timeout=10)
        found_second, second_registry = arriving_miss.result(timeout=10)

    assert found_first == found_second == "installed"
    assert "installed" in first_registry.elements
    assert "installed" in second_registry.elements
    assert discoveries == 2  # one initial publication and one shared refresh
    _clear_cache()
    # Publicly returned registries must not mutate the published snapshot.
    monkeypatch.undo()
    first = registry.load_default_registry()
    original = first.get("effects", "text-card")
    original_label = original.metadata["label"]
    original.metadata["label"] = "caller mutation"
    first.register(replace(original, id="caller-only-element"))

    second = registry.load_default_registry()
    assert second.get("effects", "text-card").metadata["label"] == original_label
    assert "caller-only-element" not in {item.id for item in second.list("effects")}
    _clear_cache()
