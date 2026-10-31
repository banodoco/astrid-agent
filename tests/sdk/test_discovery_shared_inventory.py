"""Discovery snapshots retain admission semantics without process-wide caching."""
import json

import pytest

from astrid.core.element import registry as elements
from astrid.core.execution.executor import registry as executors
from astrid.core.execution.orchestrator import registry as orchestrators
from astrid.core.pack.definition import PackDefinition, PackPermission
from astrid.core.pack.discovery import DiscoveredPack
from astrid.sdk import invocation


def _snapshot(tmp_path, pack_id):
    pack = PackDefinition(
        id=pack_id, name=pack_id, version="1.0.0", root=tmp_path / pack_id,
        manifest_path=tmp_path / pack_id / "pack.yaml", metadata={},
        permissions=(PackPermission("network", "Call the declared service"),),
    )
    pack.root.mkdir()
    pack.manifest_path.write_text(json.dumps({
        "id": pack_id, "permissions": [permission.to_dict() for permission in pack.permissions],
    }))
    return (DiscoveredPack(pack, "extra", 0),)


def _forbid_scan(*args, **kwargs):
    pytest.fail("a supplied admitted snapshot triggered another pack scan")


def test_empty_snapshot_is_authoritative_for_all_registry_loaders(monkeypatch):
    monkeypatch.setattr(executors, "discover_packs_ordered", _forbid_scan)
    monkeypatch.setattr(orchestrators, "discover_packs_ordered", _forbid_scan)
    monkeypatch.setattr(elements, "discover_pack_metadata", _forbid_scan)
    monkeypatch.setattr(elements, "_load_default_registry_data", _forbid_scan)
    assert not executors.load_default_registry(discovered_packs=()).list()
    # Also exercises the orchestrator's dependent executor loader.
    assert not orchestrators.load_default_registry(discovered_packs=()).list()
    assert not elements.load_default_registry(discovered_packs=()).list()


def test_discover_observes_each_new_snapshot_and_preserves_permissions(monkeypatch, tmp_path):
    first = _snapshot(tmp_path, "first")
    second = _snapshot(tmp_path, "second")
    snapshots = iter((first, second))
    monkeypatch.setattr("astrid.sdk._discover_pack_inventory", lambda **kwargs: next(snapshots))
    monkeypatch.setattr(executors, "discover_packs_ordered", _forbid_scan)
    monkeypatch.setattr(orchestrators, "discover_packs_ordered", _forbid_scan)
    monkeypatch.setattr(elements, "discover_pack_metadata", _forbid_scan)
    before = invocation.discover()
    after = invocation.discover()
    assert [pack["id"] for pack in before.packs] == ["first"]
    assert [pack["id"] for pack in after.packs] == ["second"]
    for result in (before, after):
        assert result.packs[0]["permissions"] == [{"id": "network", "reason": "Call the declared service"}]
        assert result.packs[0]["permission_ids"] == ["network"]
        assert result.packs[0]["trust"]["permission_enforcement"] == "disclosure_only"


def test_shared_executor_inventory_still_validates_dependency_graph():
    with pytest.raises(executors.ExecutorRegistryError, match="depends on unknown executor"):
        executors.load_default_registry(discovered_packs=(), additional_executors=({
            "id": "demo.run", "name": "Run", "kind": "built_in", "version": "1.0.0",
            "command": {"argv": ["echo", "test"]},
            "graph": {"depends_on": ["demo.missing"]},
        },))


def test_pack_disclosure_preserves_manifest_permission_order(tmp_path):
    from dataclasses import replace
    from astrid.sdk.discovery import _pack_record

    snapshot = _snapshot(tmp_path, "ordered")[0]
    network = snapshot.pack.permissions[0]
    subprocess = PackPermission("subprocess", "Start the declared tool")
    pack = replace(snapshot.pack, permissions=(network, subprocess))
    pack.manifest_path.write_text(json.dumps({
        "id": pack.id, "permissions": [subprocess.to_dict(), network.to_dict()],
    }))
    record = _pack_record(replace(snapshot, pack=pack))
    assert record["permission_ids"] == ["subprocess", "network"]
    assert record["permissions"] == [subprocess.to_dict(), network.to_dict()]


@pytest.mark.parametrize("same_project", [True, False])
def test_pack_inventory_reuses_only_identical_scan_roots(monkeypatch, tmp_path, same_project):
    from dataclasses import replace
    from astrid.sdk.discovery import _discover_pack_inventory
    from astrid.core.pack import discovery as pack_discovery

    source_root = tmp_path / "source" / "astrid" / "packs"
    project_root = source_root.parents[1] if same_project else tmp_path / "project"
    local_root = project_root / "astrid" / "packs" / "local"
    local_root.mkdir(parents=True)
    (local_root / "pack.yaml").touch()
    source = _snapshot(tmp_path, "demo")[0].pack
    local = replace(source, id="local", root=local_root, manifest_path=local_root / "pack.yaml")
    calls = []
    def strict_scan(root=None):
        calls.append(root)
        return (source, local) if root is None else (local,)
    monkeypatch.setattr("astrid.core.pack.packs_root", lambda: source_root)
    monkeypatch.setattr(pack_discovery, "discover_packs", strict_scan)
    before = _discover_pack_inventory(project_root=project_root)
    assert [(item.id, item.source_kind, item.priority_index) for item in before] == [
        ("demo", "source", 0), ("local", "local", 1),
    ]
    assert len(calls) == (1 if same_project else 2)
    # Per-call reuse must not hide later edits or failures.
    def broken_scan(root=None):
        from astrid.core.pack import PackValidationError
        raise PackValidationError("manifest changed")
    monkeypatch.setattr(pack_discovery, "discover_packs", broken_scan)
    from astrid.core.pack import PackValidationError
    with pytest.raises(PackValidationError, match="manifest changed"):
        _discover_pack_inventory(project_root=project_root)
