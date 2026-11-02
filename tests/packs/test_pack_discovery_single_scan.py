"""Per-call scan reuse must preserve strict admission and discovery authority."""

from __future__ import annotations

import json
import builtins
import hashlib
import io
import os
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

import astrid.core.pack.discovery as pack_discovery
from astrid.core.pack import PackValidationError, discover_packs, loader, source_setup
from astrid.core.pack.discovery import discover_pack_metadata, pack_discovery_scope
from astrid.core.rendering import registry


def _write_pack(parent: Path, pack_id: str, *, duplicate: bool = False, **fields) -> Path:
    root = parent / pack_id
    root.mkdir(parents=True)
    names = ["a.yaml", "b.yaml"] if duplicate else ["a.yaml"]
    rendering_root = root / "rendering"
    rendering_root.mkdir()
    manifest = {
        "schema_version": 3,
        "id": pack_id,
        "name": pack_id,
        "version": "1.0.0",
        "pack_api": "1.0",
        "documentation": {"kind": "none", "reason": "Test fixture."},
        "rendering": {
            f"renderer_{index}": {"type": "renderer", "path": f"rendering/{name}"}
            for index, name in enumerate(names)
        },
        "aliases": [{"kind": "renderer", "alias": f"{pack_id}.alias",
                     "canonical_id": f"{pack_id}.renderer_0"}],
        **fields,
    }
    (root / "pack.yaml").write_text(json.dumps(manifest), encoding="utf-8")
    for index, name in enumerate(names):
        (rendering_root / name).write_text(json.dumps({
            "schema_version": 1,
            "id": f"{pack_id}.renderer_{index}",
            "name": name,
            "version": "1.0.0",
            "protocol_version": 1,
            "command": ["python3", "backend.py"],
            "operations": ["render", "support"],
        }), encoding="utf-8")
    return root


@pytest.fixture(autouse=True)
def _isolated_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ASTRID_PACKS_PATH", raising=False)
    monkeypatch.setattr(source_setup, "active_source_inventory", lambda: None)


def _two_call_scanner(root=None):
    # The historical algorithm remains available through the injected seam.
    return discover_packs(root)


@pytest.fixture
def resource_accesses(monkeypatch):
    """Audit Path/io/builtin/descriptor opens and both SHA-256 entry paths."""
    events = {"opens": [], "file_digests": [], "hash_bytes": []}

    def wrap_open(real):
        def record(file, *args, **kwargs):
            if not isinstance(file, int):
                events["opens"].append(Path(file).absolute())
            return real(file, *args, **kwargs)
        return record

    for module in (builtins, io, os):
        monkeypatch.setattr(module, "open", wrap_open(module.open))
    real_sha256 = hashlib.sha256
    real_file_digest = hashlib.file_digest
    real_new = hashlib.new

    class HashAudit:
        def __init__(self, digest):
            self.digest_object = digest

        def update(self, data):
            events["hash_bytes"].append(bytes(data))
            return self.digest_object.update(data)

        def __getattr__(self, name):
            return getattr(self.digest_object, name)

    def sha256(data=b"", **kwargs):
        events["hash_bytes"].append(bytes(data))
        return HashAudit(real_sha256(data, **kwargs))

    def file_digest(stream, *args, **kwargs):
        events["file_digests"].append(Path(stream.name).absolute())
        return real_file_digest(stream, *args, **kwargs)

    def new(name, data=b"", **kwargs):
        digest = real_new(name, data, **kwargs)
        if name.lower().replace("-", "") == "sha256":
            events["hash_bytes"].append(bytes(data))
            return HashAudit(digest)
        return digest

    monkeypatch.setattr(hashlib, "sha256", sha256)
    monkeypatch.setattr(hashlib, "file_digest", file_digest)
    monkeypatch.setattr(hashlib, "new", new)
    return events


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
    monkeypatch.setattr(source_setup, "active_source_inventory", lambda: SimpleNamespace(
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
    monkeypatch.setattr(source_setup, "active_source_inventory", lambda: SimpleNamespace(
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


@pytest.mark.parametrize("directory_kind", ["resource", "content"])
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
    monkeypatch.setattr(source_setup, "active_source_inventory", lambda: SimpleNamespace(
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


@pytest.mark.parametrize("source_kind", ["source", "extra"])
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
    monkeypatch.setattr(source_setup, "active_source_inventory", lambda: SimpleNamespace(
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
    monkeypatch.setattr(source_setup, "active_source_inventory", lambda: SimpleNamespace(
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


@pytest.mark.parametrize("cached", [False, True])
def test_source_symlink_add_remove_and_retarget_keep_strict_refusal(
    tmp_path: Path, monkeypatch, resource_accesses, cached: bool,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    target = _write_pack(tmp_path / "targets", "target")
    other = _write_pack(tmp_path / "other", "target")
    link = source / "linked"
    monkeypatch.setattr(loader, "packs_root", lambda: source)
    monkeypatch.setattr(source_setup, "active_source_inventory", lambda: SimpleNamespace(
        identity="empty-inventory", sources=(),
    ))
    for values in resource_accesses.values():
        values.clear()
    with pack_discovery_scope() if cached else nullcontext():
        assert discover_pack_metadata(project_root=tmp_path) == ()
        empty_fingerprint = pack_discovery._tree_fingerprint(source, source_root=True)
        fingerprints = []
        for destination in (target, other):
            link.symlink_to(destination, target_is_directory=True)
            fingerprints.append(pack_discovery._tree_fingerprint(source, source_root=True))
            assert fingerprints[-1] != empty_fingerprint
            with pytest.raises(PackValidationError, match="symlink") as actual:
                discover_pack_metadata(project_root=tmp_path)
            with pytest.raises(PackValidationError) as strict:
                loader.discover_packs()
            assert str(actual.value) == str(strict.value)
            link.unlink()
            assert pack_discovery._tree_fingerprint(source, source_root=True) == empty_fingerprint
            assert discover_pack_metadata(project_root=tmp_path) == ()
        assert fingerprints[0] != fingerprints[1]
    assert not any(path.is_relative_to(target) or path.is_relative_to(other)
                   for path in resource_accesses["opens"])


@pytest.mark.parametrize("layout", ["checkout", "site-packages"])
@pytest.mark.parametrize("explicit_scanner", [False, True])
def test_same_authority_reuses_full_scan_only_with_builtin_scanner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, layout: str, explicit_scanner: bool,
) -> None:
    project = tmp_path / layout
    packs = project / "astrid/packs"
    _write_pack(packs, "zeta")
    _write_pack(packs, "local")
    _write_pack(packs, "alpha", duplicate=True)
    _write_pack(packs, "hidden", visibility="hidden")
    managed = _write_pack(tmp_path / "managed", "alpha")
    extra = _write_pack(tmp_path / "extra", "alpha")
    env = _write_pack(tmp_path / "env", "alpha")
    inventory = SimpleNamespace(identity="inventory-control", sources=(SimpleNamespace(
        pack_root=managed, pack_id="alpha", revision="a" * 40,
        manifest_sha256="b" * 64, tree_sha256="c" * 64,
    ),))
    monkeypatch.setattr(source_setup, "active_source_inventory", lambda: inventory)
    monkeypatch.setattr(loader, "packs_root", lambda: packs)
    monkeypatch.setenv("ASTRID_PACKS_PATH", str(env))
    extra_roots = (str(extra), str(managed))
    with mock.patch.object(loader, "scan_packs", wraps=loader.scan_packs) as scans:
        expected = discover_pack_metadata(
            project_root=project, extra_pack_roots=extra_roots,
            discover_packs_fn=_two_call_scanner,
        )
        assert scans.call_count == 1
    with mock.patch.object(loader, "scan_packs", wraps=loader.scan_packs) as scans:
        actual = discover_pack_metadata(
            project_root=project, extra_pack_roots=extra_roots,
            discover_packs_fn=discover_packs if explicit_scanner else None,
        )
        assert scans.call_count == 1
    # Dataclass equality includes every PackDefinition field, aliases and source hashes.
    assert actual == expected
    assert [(row.id, row.source_kind, row.priority_index) for row in actual] == [
        ("alpha", "source", 0), ("local", "source", 1), ("zeta", "source", 2),
        ("alpha", "managed", 3), ("alpha", "extra", 4), ("alpha", "env", 5),
    ]

    with mock.patch.object(registry, "discover_packs", _two_call_scanner):
        expected_registries = registry.load_default_registries(project, extra_pack_roots=extra_roots)
    actual_registries = registry.load_default_registries(project, extra_pack_roots=extra_roots)
    for actual_registry, expected_registry in zip(actual_registries, expected_registries):
        assert actual_registry.candidates() == expected_registry.candidates()
        assert actual_registry.list() == expected_registry.list()
    renderers = actual_registries[0]
    assert len(renderers.candidates("alpha.renderer_0")) == 4
    assert [c.source_kind for c in renderers.candidates("alpha.renderer_0")] == [
        "source", "managed", "extra", "env",
    ]
    assert [c.execution_eligible for c in renderers.candidates("alpha.renderer_0")] == [
        True, True, True, False,
    ]
    for alias in ("alpha.alias", "local.alias", "zeta.alias"):
        assert renderers.resolve_evidence(alias) == expected_registries[0].resolve_evidence(alias)
    assert renderers.get("alpha.alias").manifest.name == "a.yaml"


def test_different_roots_keep_source_packs_without_project_local_injection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "installed/astrid/packs"
    _write_pack(source, "alpha")
    _write_pack(source, "local", description="ordinary source pack")
    project = tmp_path / "project"
    project_packs = project / "astrid/packs"
    _write_pack(project_packs, "local", description="project local")
    _write_pack(project_packs, "stray")
    monkeypatch.setattr(loader, "packs_root", lambda: source)
    with mock.patch.object(loader, "scan_packs", wraps=loader.scan_packs) as scans:
        rows = discover_pack_metadata(project_root=project)
        assert scans.call_args_list == [mock.call(None, include_hidden=False, strict=True)]
    assert [(r.id, r.source_kind) for r in rows] == [("alpha", "source"), ("local", "source")]
    assert rows[1].pack.root == source / "local"
    assert rows == discover_pack_metadata(project_root=project, discover_packs_fn=_two_call_scanner)
    (project_packs / "stray/pack.yaml").write_text("schema_version: 999\n", encoding="utf-8")
    assert discover_pack_metadata(project_root=project) == rows


def test_injected_scanner_keeps_argument_specific_authority_even_with_equal_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    packs = project / "astrid/packs"
    local_root = _write_pack(packs, "local")
    local = loader.load_pack_manifest(local_root / "pack.yaml")
    alpha_root = _write_pack(packs, "alpha")
    alpha = loader.load_pack_manifest(alpha_root / "pack.yaml")
    monkeypatch.setattr(loader, "packs_root", lambda: packs)
    scanner = mock.Mock(return_value=(alpha, local))
    rows = discover_pack_metadata(project_root=project, discover_packs_fn=scanner)
    assert scanner.call_args_list == [mock.call()]
    assert [(r.id, r.source_kind) for r in rows] == [
        ("alpha", "source"), ("local", "source"),
    ]


@pytest.mark.parametrize("local_kind", ["absent", "manifestless", "hidden"])
def test_local_eligibility_is_observational(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, local_kind: str,
) -> None:
    packs = tmp_path / "astrid/packs"
    _write_pack(packs, "alpha")
    if local_kind == "hidden":
        _write_pack(packs, "local", visibility="hidden")
    elif local_kind == "manifestless":
        (packs / "local").mkdir()
    monkeypatch.setattr(loader, "packs_root", lambda: packs)
    rows = discover_pack_metadata(project_root=tmp_path)
    assert rows == discover_pack_metadata(project_root=tmp_path, discover_packs_fn=_two_call_scanner)
    assert [r.id for r in rows] == ["alpha"]
    if local_kind != "hidden":
        assert not (packs / "local/pack.yaml").exists()


@pytest.mark.parametrize("invalid", ["source", "local", "hidden", "alternate", "path", "symlink"])
def test_scan_reuse_preserves_strict_manifest_and_path_guards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, invalid: str,
) -> None:
    packs = tmp_path / "astrid/packs"
    local = _write_pack(packs, "local")
    broken = _write_pack(packs, "broken")
    if invalid in {"source", "local", "hidden"}:
        root = local if invalid == "local" else broken
        (root / "pack.yaml").write_text(
            "schema_version: 3\nid: invalid id\nname: Broken\nversion: 1.0.0\n"
            "pack_api: \"1.0\"\nrendering: {}\n",
            encoding="utf-8",
        )
    elif invalid == "alternate":
        (local / "pack.yml").write_text("{}\n", encoding="utf-8")
    elif invalid == "path":
        _write_pack(packs, "escape", documentation={"kind": "agents", "path": "../local/a.yaml"})
    else:
        (local / "linked.yaml").symlink_to(broken / "a.yaml")
    monkeypatch.setattr(loader, "packs_root", lambda: packs)
    with pytest.raises(PackValidationError) as expected:
        discover_pack_metadata(project_root=tmp_path, discover_packs_fn=_two_call_scanner)
    with pytest.raises(PackValidationError) as actual:
        discover_pack_metadata(project_root=tmp_path)
    assert str(actual.value) == str(expected.value)


def test_cached_scan_does_not_hide_new_alternate_source_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, resource_accesses,
) -> None:
    packs = tmp_path / "astrid/packs"
    _write_pack(packs, "local")
    manifestless = packs / "legacy"
    manifestless.mkdir()
    monkeypatch.setattr(loader, "packs_root", lambda: packs)
    monkeypatch.setattr(source_setup, "active_source_inventory", lambda: SimpleNamespace(
        identity="empty-inventory", sources=(),
    ))
    with pack_discovery_scope():
        assert [row.id for row in discover_pack_metadata(project_root=tmp_path)] == ["local"]
        (manifestless / "pack.yml").write_text("{}\n", encoding="utf-8")
        for values in resource_accesses.values():
            values.clear()
        with pytest.raises(PackValidationError, match="alternate manifest"):
            discover_pack_metadata(project_root=tmp_path)
        assert manifestless / "pack.yml" not in resource_accesses["opens"]
        assert manifestless / "pack.yml" not in resource_accesses["file_digests"]
    with pytest.raises(PackValidationError, match="alternate manifest"):
        discover_pack_metadata(project_root=tmp_path)
    assert manifestless / "pack.yml" not in resource_accesses["opens"]
    assert manifestless / "pack.yml" not in resource_accesses["file_digests"]


def test_managed_identity_guard_is_unchanged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    packs = tmp_path / "astrid/packs"
    _write_pack(packs, "local")
    managed = _write_pack(tmp_path / "managed", "alpha")
    monkeypatch.setattr(loader, "packs_root", lambda: packs)
    monkeypatch.setattr(source_setup, "active_source_inventory", lambda: SimpleNamespace(
        identity="inventory", sources=(SimpleNamespace(pack_root=managed, pack_id="wrong"),),
    ))
    rows = discover_pack_metadata(project_root=tmp_path)
    assert rows == discover_pack_metadata(project_root=tmp_path, discover_packs_fn=_two_call_scanner)
    assert [(r.id, r.source_kind) for r in rows] == [("local", "source")]


def test_discovery_observes_edits_on_every_call(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    packs = tmp_path / "astrid/packs"
    local = _write_pack(packs, "local", description="before")
    monkeypatch.setattr(loader, "packs_root", lambda: packs)
    with mock.patch.object(loader, "scan_packs", wraps=loader.scan_packs) as scans:
        before = discover_pack_metadata(project_root=tmp_path)
        path = local / "pack.yaml"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        manifest["description"] = "after"
        path.write_text(json.dumps(manifest), encoding="utf-8")
        after = discover_pack_metadata(project_root=tmp_path)
        assert scans.call_count == 2
    assert before[0].pack.description == "before"
    assert after[0].pack.description == "after"


def test_discovery_cache_hashes_only_selected_roots_and_invalidates_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source/astrid/packs"
    _write_pack(source, "bundled")
    hidden_local = source / "local"
    hidden_local.mkdir()
    (hidden_local / "pack.yaml").write_text(
        '{"schema_version":3,"id":"local","name":"Local","version":"1.0.0",'
        '"pack_api":"1.0","visibility":"hidden","rendering":{},'
        '"resources":[{"path":"elements/effects/legacy/element.yaml","kind":"example"}]}',
        encoding="utf-8",
    )
    hidden_asset = hidden_local / "elements/effects/legacy/element.yaml"
    hidden_asset.parent.mkdir(parents=True)
    hidden_asset.write_text("hidden legacy asset", encoding="utf-8")
    ignored_assets = []
    for ignored_name in (".ignored", "__pycache__"):
        ignored = source / ignored_name
        ignored.mkdir()
        ignored_manifest = ignored / "pack.yaml"
        ignored_manifest.write_text("not a pack", encoding="utf-8")
        ignored_assets.append(ignored_manifest)
    project = tmp_path / "project"
    unselected_project_packs = project / "astrid/packs"
    unselected_project_packs.mkdir(parents=True)
    project_bytes = unselected_project_packs / "unselected.py"
    project_bytes.write_text("initial", encoding="utf-8")
    extra_root = tmp_path / "extra"
    selected = _write_pack(extra_root, "selected", resources=[
        {"path": "assets/payload.bin", "kind": "example"},
    ])
    selected_asset = selected / "assets/payload.bin"
    selected_asset.parent.mkdir()
    selected_asset.write_bytes(b"selected asset v1")
    quarantined = source / "legacy_v2"
    quarantined.mkdir()
    (quarantined / "pack.yaml").write_text(json.dumps({
        "schema_version": 2, "id": "legacy_v2", "name": "Legacy", "version": "1.0.0",
        "capabilities": [],
    }), encoding="utf-8")
    quarantined_asset = quarantined / "payload.bin"
    quarantined_asset.write_bytes(b"v2 payload")
    unsupported = _write_pack(source, "unsupported", resources=[
        {"path": "payload.bin", "kind": "example"},
    ])
    unsupported_manifest = json.loads((unsupported / "pack.yaml").read_text())
    unsupported_manifest["schema_version"] = 999
    (unsupported / "pack.yaml").write_text(json.dumps(unsupported_manifest), encoding="utf-8")
    unsupported_asset = unsupported / "payload.bin"
    unsupported_asset.write_bytes(b"unsupported payload")
    for candidate_name, fields in (
        ("external_hidden", {"visibility": "hidden"}),
        ("external_mismatched_id", {"id": "selected"}),
    ):
        candidate = _write_pack(extra_root, candidate_name, **fields)
        (candidate / "payload.bin").write_bytes(candidate_name.encode())
    malformed_external = _write_pack(extra_root, "malformed_external")
    (malformed_external / "pack.yaml").write_text("schema_version: [\n", encoding="utf-8")
    (malformed_external / "payload.bin").write_bytes(b"malformed")
    incompatible_external = _write_pack(extra_root, "incompatible_external", pack_api=[])
    (incompatible_external / "payload.bin").write_bytes(b"incompatible")
    monkeypatch.setattr(loader, "packs_root", lambda: source)
    monkeypatch.setattr(source_setup, "active_source_inventory", lambda: SimpleNamespace(
        identity="empty-inventory", sources=(),
    ))
    fingerprint = pack_discovery._tree_fingerprint
    real_file_digest = pack_discovery.hashlib.file_digest
    real_path_open = Path.open
    hashed_files = []
    opened_files = []

    def record_file_digest(stream, algorithm, *args, **kwargs):
        hashed_files.append(Path(stream.name).resolve())
        return real_file_digest(stream, algorithm, *args, **kwargs)

    def record_path_open(path, *args, **kwargs):
        opened_files.append(path.resolve())
        return real_path_open(path, *args, **kwargs)

    with mock.patch(
        "astrid.core.pack.discovery._tree_fingerprint", wraps=fingerprint,
    ) as fingerprint_calls, mock.patch.object(
        loader, "scan_packs", wraps=loader.scan_packs,
    ) as scans, mock.patch.object(
        pack_discovery.hashlib, "file_digest", side_effect=record_file_digest,
    ), mock.patch.object(
        Path, "open", record_path_open,
    ), pack_discovery_scope():
        # The repeated external root is one scanner input, not two candidates.
        extra_roots = (str(extra_root), str(extra_root))
        first = discover_pack_metadata(project_root=project, extra_pack_roots=extra_roots)
        assert "local" not in [row.id for row in first]
        assert sum(row.id == "selected" for row in first) == 1
        assert selected_asset.resolve() in hashed_files
        assert hashed_files.count(selected_asset.resolve()) == 2
        assert hidden_asset.resolve() not in hashed_files
        assert quarantined_asset.resolve() not in opened_files
        assert unsupported_asset.resolve() not in opened_files
        assert quarantined_asset.resolve() not in hashed_files
        assert unsupported_asset.resolve() not in hashed_files
        unselected_external_payloads = {
            quarantined_asset, unsupported_asset,
            extra_root / "external_hidden/payload.bin",
            extra_root / "external_mismatched_id/payload.bin",
            malformed_external / "payload.bin", incompatible_external / "payload.bin",
        }
        assert all(path.resolve() not in opened_files for path in unselected_external_payloads)
        assert all(path.resolve() not in hashed_files for path in unselected_external_payloads)
        assert all(path.resolve() not in hashed_files for path in ignored_assets)
        project_bytes.write_text("changed but still unselected", encoding="utf-8")
        hidden_asset.write_text("hidden asset changed", encoding="utf-8")
        unchanged = discover_pack_metadata(project_root=project, extra_pack_roots=extra_roots)
        assert unchanged == first
        assert scans.call_count == 1

        selected_asset.write_bytes(b"selected asset v2")
        invalidated = discover_pack_metadata(project_root=project, extra_pack_roots=extra_roots)
        assert scans.call_count == 2
        assert any(row.id == "selected" for row in invalidated)

        manifest_path = hidden_local / "pack.yaml"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["visibility"] = "visible"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        visible = discover_pack_metadata(project_root=project, extra_pack_roots=extra_roots)
        assert scans.call_count == 3
        assert [row.id for row in visible if row.id == "local"] == ["local"]
        assert hidden_asset.resolve() in hashed_files

        unsupported_manifest["schema_version"] = 3
        (unsupported / "pack.yaml").write_text(json.dumps(unsupported_manifest), encoding="utf-8")
        admitted = discover_pack_metadata(project_root=project, extra_pack_roots=extra_roots)
        assert scans.call_count == 4
        assert any(row.id == "unsupported" for row in admitted)
        assert unsupported_asset.resolve() in opened_files
        assert unsupported_asset.resolve() in hashed_files

        hashed_roots = {Path(call.args[0]).resolve() for call in fingerprint_calls.call_args_list}
        assert unselected_project_packs.resolve() not in hashed_roots


def test_production_support_caller_uses_single_scan_without_a_backend_process(tmp_path: Path) -> None:
    from astrid.core.foundation.paths import REPO_ROOT
    from astrid.core.rendering.contracts import SupportReport
    from astrid.sdk import rendering

    report = SupportReport(
        schema_version=1, backend="rendering.remotion", backend_version="1.0.0",
        supported=True, reasons=[], features={}, alternatives=[],
    )
    transport = mock.Mock()
    transport.run.return_value = report
    with mock.patch.object(loader, "scan_packs", wraps=loader.scan_packs) as scans:
        actual = rendering.support(
            "rendering.remotion", timeline_path=tmp_path / "timeline.json",
            project_root=REPO_ROOT, transport=transport,
        )
        assert scans.call_count == 1
    assert actual is report
    assert transport.run.call_count == 1
    assert transport.run.call_args.args[0] == "support"
    assert transport.run.call_args.kwargs["backend"] == "rendering.remotion"
