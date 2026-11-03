"""Selected-source admission and cache custody, preserving the base policies."""
from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

import astrid.core.pack.discovery as pack_discovery
from astrid.core.pack import discover_packs, loader, source_setup, PackValidationError
from astrid.core.pack.discovery import discover_pack_metadata, pack_discovery_scope
from tests.packs.test_pack_discovery_single_scan import (
    _write_selected_pack as _write_pack, resource_accesses,
)

@pytest.mark.parametrize("rejection", ["source_hidden", "external_hidden", "missing", "overlap", "descriptor"])
def test_rejected_declared_resources_stay_unopened_in_warm_cache(
    tmp_path: Path, monkeypatch, resource_accesses, rejection: str,
) -> None:
    source = tmp_path / "source"
    _write_pack(source, "bundled")
    extras = tmp_path / "extras"
    extras.mkdir()
    fields = {"resources": [{"path": "aa_payload.bin", "kind": "example"}]}
    if rejection.endswith("hidden"):
        fields["visibility"] = "hidden"
    if rejection == "missing":
        fields["resources"].append({"path": "zz_missing.bin", "kind": "example"})
    if rejection == "overlap":
        fields["authoring_only"] = [{"path": "rendering", "kind": "example", "reason": "Excluded"}]
    rejected = _write_pack(source if rejection == "source_hidden" else extras, "rejected", **fields)
    payload = rejected / "aa_payload.bin"
    payload_bytes = b"UNSELECTED declared resource, never acquire these bytes"
    payload.write_bytes(payload_bytes)
    descriptor = rejected / "rendering/a.yaml"
    valid_descriptor = descriptor.read_bytes()
    if rejection == "descriptor":
        descriptor.write_text('{"id":"wrong.renderer"}', encoding="utf-8")
    monkeypatch.setattr(loader, "packs_root", lambda: source)
    monkeypatch.setattr(source_setup, "active_source_inventory", lambda **kwargs: SimpleNamespace(
        identity="empty-inventory", sources=(),
    ))
    extras_arg = (str(extras),)

    def clear():
        for values in resource_accesses.values():
            values.clear()

    def assert_unacquired(*, warm):
        assert payload not in resource_accesses["opens"]
        assert payload not in resource_accesses["file_digests"]
        assert not any(payload_bytes in chunk for chunk in resource_accesses["hash_bytes"])
        if warm or rejection != "descriptor":
            assert descriptor not in resource_accesses["opens"]
            assert descriptor not in resource_accesses["file_digests"]

    clear()
    with mock.patch.object(loader, "scan_packs", wraps=loader.scan_packs) as scans, pack_discovery_scope():
        first = discover_pack_metadata(project_root=tmp_path, extra_pack_roots=extras_arg)
        assert [row.id for row in first] == ["bundled"]
        assert_unacquired(warm=False)
        clear()
        assert discover_pack_metadata(project_root=tmp_path, extra_pack_roots=extras_arg) == first
        assert scans.call_count == 1
        assert_unacquired(warm=True)
        if rejection.endswith("hidden"):
            assert pack_discovery.discover_canonical_pack_metadata(
                project_root=tmp_path, extra_pack_roots=(str(rejected),),
            ) == ()
            assert_unacquired(warm=True)

        # A rejected descriptor needs a stat-based edit signal; visibility,
        # missing paths and authoring overlap are metadata transitions too.
        if rejection == "descriptor":
            descriptor.write_bytes(valid_descriptor)
        elif rejection == "missing":
            (rejected / "zz_missing.bin").write_bytes(b"now present")
        else:
            manifest_path = rejected / "pack.yaml"
            manifest = json.loads(manifest_path.read_text())
            manifest.pop("authoring_only", None)
            manifest["visibility"] = "visible"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        clear()
        admitted = discover_pack_metadata(project_root=tmp_path, extra_pack_roots=extras_arg)
        assert {row.id for row in admitted} == {"bundled", "rejected"}
        assert scans.call_count == 2
        assert payload in resource_accesses["opens"]
        assert payload in resource_accesses["file_digests"]
        assert payload_bytes in resource_accesses["hash_bytes"]


def test_valid_incompatible_api_is_rejected_before_declared_resource_reads(
    tmp_path: Path, monkeypatch, resource_accesses,
) -> None:
    external = tmp_path / "external"
    root = _write_pack(external, "incompatible", pack_api="99.0")
    descriptor = root / "rendering/a.yaml"
    monkeypatch.setattr(loader, "packs_root", lambda: tmp_path / "bundled")
    for values in resource_accesses.values():
        values.clear()
    assert discover_packs(external) == ()
    assert descriptor not in resource_accesses["opens"]
    assert descriptor not in resource_accesses["file_digests"]


@pytest.mark.parametrize("source_kind", ["source", "extra"])
def test_cache_reads_only_declared_runtime_payloads(
    tmp_path: Path, monkeypatch, resource_accesses, source_kind: str,
) -> None:
    source = tmp_path / "source"
    _write_pack(source, "bundled")
    extras = tmp_path / "extras"
    extras.mkdir()
    root = _write_pack(source if source_kind == "source" else extras, "selected",
                       resources=[{"path": "assets/selected.bin", "kind": "example"}],
                       authoring_only=[{"path": "authoring", "kind": "source", "reason": "Drafts."}])
    selected = root / "assets/selected.bin"
    undeclared = root / "assets/undeclared.bin"
    authoring = root / "authoring/draft.bin"
    selected.parent.mkdir()
    authoring.parent.mkdir()
    selected.write_bytes(b"DECLARED runtime payload v1")
    unselected_bytes = {
        undeclared: b"UNDECLARED payload must never enter a digest",
        authoring: b"AUTHORING payload must never enter a digest",
    }
    for path, data in unselected_bytes.items():
        path.write_bytes(data)
    monkeypatch.setattr(loader, "packs_root", lambda: source)
    monkeypatch.setattr(source_setup, "active_source_inventory", lambda **kwargs: SimpleNamespace(
        identity="empty-inventory", sources=(),
    ))
    kwargs = {"project_root": tmp_path, "extra_pack_roots": (str(extras),)}

    def clear():
        for values in resource_accesses.values():
            values.clear()

    def assert_unselected():
        for path, data in unselected_bytes.items():
            assert path not in resource_accesses["opens"]
            assert path not in resource_accesses["file_digests"]
            assert not any(data in chunk for chunk in resource_accesses["hash_bytes"])

    clear()
    with mock.patch.object(loader, "scan_packs", wraps=loader.scan_packs) as scans, pack_discovery_scope():
        first = discover_pack_metadata(**kwargs)
        assert {row.id for row in first} == {"bundled", "selected"}
        assert selected in resource_accesses["opens"]
        assert selected in resource_accesses["file_digests"]
        assert b"DECLARED runtime payload v1" in resource_accesses["hash_bytes"]
        assert_unselected()
        clear()
        assert discover_pack_metadata(**kwargs) == first
        assert scans.call_count == 1
        assert_unselected()

        # Neither unselected edits nor new ordinary descendants change admission.
        for path, data in tuple(unselected_bytes.items()):
            unselected_bytes[path] = data + b" changed"
            path.write_bytes(unselected_bytes[path])
        added = root / "assets/new_undeclared.bin"
        unselected_bytes[added] = b"NEW UNDECLARED payload"
        added.write_bytes(unselected_bytes[added])
        clear()
        assert discover_pack_metadata(**kwargs) == first
        assert scans.call_count == 1
        assert_unselected()

        selected.write_bytes(b"DECLARED runtime payload v2")
        clear()
        assert discover_pack_metadata(**kwargs) == first
        assert scans.call_count == 2
        assert selected in resource_accesses["opens"]
        assert selected in resource_accesses["file_digests"]
        assert b"DECLARED runtime payload v2" in resource_accesses["hash_bytes"]
        assert_unselected()


@pytest.mark.parametrize("directory_kind", ["resource"])
def test_cache_uses_canonical_directory_selection(
    tmp_path: Path, monkeypatch, resource_accesses, directory_kind: str,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    extras = tmp_path / "extras"
    if directory_kind == "resource":
        root = _write_pack(
            extras, "selected", resources=[{"path": "assets", "kind": "example"}],
            authoring_only=[{"path": "assets/drafts", "kind": "source", "reason": "Drafts."}],
        )
    else:
        root = extras / "selected"
        root.mkdir(parents=True)
        (root / "pack.yaml").write_text(json.dumps({
            "schema_version": 2, "id": "selected", "name": "Selected", "version": "1.0.0",
            "content": {"examples": "assets"},
            "authoring_only": [{"path": "assets/drafts", "kind": "source", "reason": "Drafts."}],
        }), encoding="utf-8")
    nested = root / "assets/.hidden/payload.bin"
    nested.parent.mkdir(parents=True)
    payloads = {
        nested: b"EXPLICIT directory hidden payload",
        root / "assets/visible.bin": b"directory visible payload",
    }
    for path, data in payloads.items():
        path.write_bytes(data)
    authoring = root / "assets/drafts/source.bin"
    authoring.parent.mkdir()
    authoring_bytes = b"DIRECTORY AUTHORING bytes must stay excluded"
    authoring.write_bytes(authoring_bytes)
    monkeypatch.setattr(loader, "packs_root", lambda: source)
    monkeypatch.setattr(source_setup, "active_source_inventory", lambda **kwargs: SimpleNamespace(
        identity="empty-inventory", sources=(),
    ))
    kwargs = {"project_root": tmp_path, "extra_pack_roots": (str(extras),)}

    def clear():
        for values in resource_accesses.values():
            values.clear()

    def assert_payload_selection():
        assert authoring not in resource_accesses["opens"]
        assert authoring not in resource_accesses["file_digests"]
        assert not any(authoring_bytes in chunk for chunk in resource_accesses["hash_bytes"])
        for path, data in payloads.items():
            if directory_kind == "content":
                assert path in resource_accesses["opens"]
                assert path in resource_accesses["file_digests"]
            else:
                assert path not in resource_accesses["opens"]
                assert path not in resource_accesses["file_digests"]
                assert not any(data in chunk for chunk in resource_accesses["hash_bytes"])

    clear()
    with mock.patch.object(loader, "scan_packs", wraps=loader.scan_packs) as scans, pack_discovery_scope():
        first = discover_pack_metadata(**kwargs)
        assert [row.id for row in first] == ["selected"]
        assert_payload_selection()
        clear()
        assert discover_pack_metadata(**kwargs) == first
        assert scans.call_count == 1
        assert_payload_selection()
        authoring_bytes += b" changed"
        authoring.write_bytes(authoring_bytes)
        clear()
        assert discover_pack_metadata(**kwargs) == first
        assert scans.call_count == 1
        assert_payload_selection()
        nested.write_bytes(b"EXPLICIT directory hidden payload changed")
        payloads[nested] = b"EXPLICIT directory hidden payload changed"
        added = root / "assets/added.bin"
        payloads[added] = b"EXPLICIT directory added payload"
        added.write_bytes(payloads[added])
        clear()
        assert discover_pack_metadata(**kwargs) == first
        assert scans.call_count == (2 if directory_kind == "content" else 1)
        assert_payload_selection()


@pytest.mark.parametrize("failure", ["descriptor", "payload", "cycle"])
def test_optional_dependency_admission_precedes_dependent_payloads(
    tmp_path: Path, monkeypatch, resource_accesses, failure: str,
) -> None:
    external = tmp_path / "external"
    a = _write_pack(external, "a", dependencies={"packs": ["b"]},
                    resources=[{"path": "payload.bin", "kind": "example"}])
    b = _write_pack(external, "b", resources=[{"path": "payload.bin", "kind": "example"}])
    _write_pack(external, "c")
    a_payload, b_payload = a / "payload.bin", b / "payload.bin"
    a_bytes = b"DEPENDENT A payload must await B admission"
    a_payload.write_bytes(a_bytes)
    b_payload.write_bytes(b"DEPENDENCY B payload")
    descriptor = b / "rendering/a.yaml"
    valid_descriptor = descriptor.read_bytes()
    if failure == "descriptor":
        descriptor.write_text('{"id":"another.renderer"}', encoding="utf-8")
    elif failure == "cycle":
        manifest_path = b / "pack.yaml"
        manifest = json.loads(manifest_path.read_text())
        manifest["dependencies"] = {"packs": ["a"]}
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    real_read_bytes = Path.read_bytes
    fail_payload = failure == "payload"

    def read_bytes(path):
        if fail_payload and path == b_payload:
            raise OSError("fixture dependency payload cannot be read")
        return real_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    monkeypatch.setattr(loader, "packs_root", lambda: tmp_path / "bundled")
    for values in resource_accesses.values():
        values.clear()
    scan = loader.scan_packs(external, strict=False)
    assert [pack.id for pack in scan.packs] == ["c"]
    quarantined = {record.pack_id: record for record in scan.quarantined}
    assert set(quarantined) == {"a", "b"}
    expected_error = {"descriptor": "identity", "payload": "cannot be read", "cycle": "cycle"}[failure]
    assert expected_error in quarantined["b"].error
    assert a_payload not in resource_accesses["opens"]
    assert a_payload not in resource_accesses["file_digests"]
    assert not any(a_bytes in chunk for chunk in resource_accesses["hash_bytes"])

    descriptor.write_bytes(valid_descriptor)
    fail_payload = False
    if failure == "cycle":
        manifest.pop("dependencies")
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    for values in resource_accesses.values():
        values.clear()
    repaired = loader.scan_packs(external, strict=False)
    assert [pack.id for pack in repaired.packs] == ["a", "b", "c"]
    assert repaired.quarantined == ()
    assert a_payload in resource_accesses["opens"]
    assert a_bytes in resource_accesses["hash_bytes"]
    assert resource_accesses["opens"].index(b_payload) < resource_accesses["opens"].index(a_payload)


@pytest.mark.parametrize("strict", [False, True])
def test_dependency_descriptor_failure_preserves_strict_and_required_refusal(
    tmp_path: Path, monkeypatch, resource_accesses, strict: bool,
) -> None:
    external = tmp_path / "external"
    a = _write_pack(external, "a", dependencies={"packs": ["b"]},
                    resources=[{"path": "payload.bin", "kind": "example"}])
    b = _write_pack(external, "b")
    payload = a / "payload.bin"
    payload_bytes = b"STRICT dependent payload must stay unread"
    payload.write_bytes(payload_bytes)
    (b / "rendering/a.yaml").write_text('{"id":"wrong.renderer"}', encoding="utf-8")
    monkeypatch.setattr(loader, "packs_root", lambda: tmp_path / "bundled")
    for values in resource_accesses.values():
        values.clear()
    with pytest.raises(PackValidationError, match="identity"):
        loader.scan_packs(
            external, strict=strict, required_pack_ids=frozenset() if strict else frozenset({"b"}),
        )
    assert payload not in resource_accesses["opens"]
    assert payload not in resource_accesses["file_digests"]
    assert not any(payload_bytes in chunk for chunk in resource_accesses["hash_bytes"])


def test_selected_descriptor_rejection_precedes_other_resource_acquisition(
    tmp_path: Path, monkeypatch, resource_accesses,
) -> None:
    source = tmp_path / "source"
    _write_pack(source, "bundled")
    root = _write_pack(tmp_path / "extra", "changing", resources=[
        {"path": "aa_payload.bin", "kind": "example"},
    ])
    payload = root / "aa_payload.bin"
    payload_bytes = b"selected then rejected private resource"
    payload.write_bytes(payload_bytes)
    descriptor = root / "rendering/a.yaml"
    valid_descriptor = descriptor.read_bytes()
    monkeypatch.setattr(loader, "packs_root", lambda: source)
    monkeypatch.setattr(source_setup, "active_source_inventory", lambda **kwargs: SimpleNamespace(
        identity="empty-inventory", sources=(),
    ))
    kwargs = {"project_root": tmp_path, "extra_pack_roots": (str(root),)}
    with pack_discovery_scope():
        assert {row.id for row in discover_pack_metadata(**kwargs)} == {"bundled", "changing"}
        before = descriptor.stat()
        descriptor.write_text('{"id":"another.renderer"}', encoding="utf-8")
        os.utime(descriptor, ns=(before.st_atime_ns, before.st_mtime_ns))
        for values in resource_accesses.values():
            values.clear()
        assert [row.id for row in discover_pack_metadata(**kwargs)] == ["bundled"]
        assert payload not in resource_accesses["opens"]
        assert payload not in resource_accesses["file_digests"]
        assert not any(payload_bytes in chunk for chunk in resource_accesses["hash_bytes"])
        for values in resource_accesses.values():
            values.clear()
        assert [row.id for row in discover_pack_metadata(**kwargs)] == ["bundled"]
        assert descriptor not in resource_accesses["opens"]
        assert payload not in resource_accesses["opens"]
        descriptor.write_bytes(valid_descriptor)
        assert {row.id for row in discover_pack_metadata(**kwargs)} == {"bundled", "changing"}


@pytest.mark.parametrize("source_kind", ["extra"])
def test_undeclared_symlink_invalidates_cache_without_acquiring_target(
    tmp_path: Path, monkeypatch, resource_accesses, source_kind: str,
) -> None:
    source = tmp_path / "source"
    _write_pack(source, "bundled")
    extras = tmp_path / "extras"
    extras.mkdir()
    root = _write_pack(source if source_kind == "source" else extras, "selected")
    (root / "unselected").mkdir()
    target = tmp_path / "outside.bin"
    target_bytes = b"OUTSIDE SYMLINK TARGET must never be acquired"
    target.write_bytes(target_bytes)
    link = root / "unselected/linked.bin"
    monkeypatch.setattr(loader, "packs_root", lambda: source)
    monkeypatch.setattr(source_setup, "active_source_inventory", lambda **kwargs: SimpleNamespace(
        identity="empty-inventory", sources=(),
    ))
    kwargs = {"project_root": tmp_path, "extra_pack_roots": (str(extras),)}

    def clear():
        for values in resource_accesses.values():
            values.clear()

    def assert_target_unacquired():
        assert target not in resource_accesses["opens"]
        assert target not in resource_accesses["file_digests"]
        assert link not in resource_accesses["opens"]
        assert link not in resource_accesses["file_digests"]
        assert not any(target_bytes in chunk for chunk in resource_accesses["hash_bytes"])

    clear()
    with mock.patch.object(loader, "scan_packs", wraps=loader.scan_packs) as scans, pack_discovery_scope():
        first = discover_pack_metadata(**kwargs)
        assert {row.id for row in first} == {"bundled", "selected"}
        assert discover_pack_metadata(**kwargs) == first
        assert scans.call_count == 1
        assert_target_unacquired()
        link.symlink_to(target)
        clear()
        if source_kind == "source":
            with pytest.raises(PackValidationError, match="symlink"):
                discover_pack_metadata(**kwargs)
        else:
            assert [row.id for row in discover_pack_metadata(**kwargs)] == ["bundled"]
        assert scans.call_count == 2
        assert_target_unacquired()
        link.unlink()
        clear()
        assert discover_pack_metadata(**kwargs) == first
        calls_after_repair = scans.call_count
        assert discover_pack_metadata(**kwargs) == first
        assert scans.call_count == calls_after_repair
        assert_target_unacquired()


def test_strict_source_symlink_refusal_never_opens_target(tmp_path, monkeypatch, resource_accesses):
    source = tmp_path / "source"
    source.mkdir()
    first = _write_pack(tmp_path / "one", "target")
    second = _write_pack(tmp_path / "two", "target")
    link = source / "linked"
    monkeypatch.setattr(loader, "packs_root", lambda: source)
    for target in (first, second):
        link.symlink_to(target, target_is_directory=True)
        for events in resource_accesses.values():
            events.clear()
        with pytest.raises(PackValidationError, match="symlink"):
            loader.scan_packs(source, strict=True)
        assert not any(path.is_relative_to(first) or path.is_relative_to(second)
                       for path in resource_accesses["opens"])
        link.unlink()
        assert loader.scan_packs(source, strict=True).packs == ()


def test_local_visibility_and_eligibility_transitions_in_warm_cache(tmp_path, monkeypatch, resource_accesses):
    from astrid.core.pack.discovery import discover_canonical_pack_metadata

    bundled = tmp_path / "bundled"
    _write_pack(bundled, "bundled")
    local = _write_pack(tmp_path / "astrid/packs", "local", visibility="hidden",
                        resources=[{"path": "payload.bin", "kind": "example"}])
    payload = local / "payload.bin"
    payload.write_bytes(b"LOCAL selected only after visibility transition")
    manifest = local / "pack.yaml"
    data = json.loads(manifest.read_text())
    monkeypatch.setattr(loader, "packs_root", lambda: bundled)
    monkeypatch.setattr(source_setup, "active_source_inventory", lambda **kwargs: SimpleNamespace(
        identity="empty-inventory", sources=(),
    ))
    with pack_discovery_scope():
        for _ in range(2):
            for events in resource_accesses.values():
                events.clear()
            assert [row.id for row in discover_pack_metadata(project_root=tmp_path)] == ["bundled"]
            assert payload not in resource_accesses["opens"]
            assert payload not in resource_accesses["file_digests"]
        data["visibility"] = "visible"
        manifest.write_text(json.dumps(data))
        rows = discover_pack_metadata(project_root=tmp_path)
        assert [(row.id, row.source_kind) for row in rows] == [("bundled", "source"), ("local", "local")]
        assert [(row.id, row.source_kind) for row in discover_canonical_pack_metadata(project_root=tmp_path)] == [("local", "local")]
        assert payload in resource_accesses["opens"]
        saved = manifest.read_bytes()
        manifest.unlink()
        for events in resource_accesses.values():
            events.clear()
        assert [row.id for row in discover_pack_metadata(project_root=tmp_path)] == ["bundled"]
        assert payload not in resource_accesses["opens"]
        manifest.write_bytes(saved)
        assert [row.id for row in discover_pack_metadata(project_root=tmp_path)] == ["bundled", "local"]
