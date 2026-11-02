"""Per-call scan reuse must preserve strict admission and discovery authority."""

from __future__ import annotations

import json
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
        '"pack_api":"1.0","visibility":"hidden","rendering":{}}',
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
    selected = _write_pack(extra_root, "selected")
    selected_asset = selected / "assets/payload.bin"
    selected_asset.parent.mkdir()
    selected_asset.write_bytes(b"selected asset v1")
    monkeypatch.setattr(loader, "packs_root", lambda: source)
    monkeypatch.setattr(source_setup, "active_source_inventory", lambda: SimpleNamespace(
        identity="empty-inventory", sources=(),
    ))
    fingerprint = pack_discovery._tree_fingerprint
    real_file_digest = pack_discovery.hashlib.file_digest
    hashed_files = []

    def record_file_digest(stream, algorithm, *args, **kwargs):
        hashed_files.append(Path(stream.name).resolve())
        return real_file_digest(stream, algorithm, *args, **kwargs)

    with mock.patch(
        "astrid.core.pack.discovery._tree_fingerprint", wraps=fingerprint,
    ) as fingerprint_calls, mock.patch.object(
        loader, "scan_packs", wraps=loader.scan_packs,
    ) as scans, mock.patch.object(
        pack_discovery.hashlib, "file_digest", side_effect=record_file_digest,
    ), pack_discovery_scope():
        first = discover_pack_metadata(project_root=project, extra_pack_roots=(str(extra_root),))
        assert "local" not in [row.id for row in first]
        assert selected_asset.resolve() in hashed_files
        assert hidden_asset.resolve() not in hashed_files
        assert all(path.resolve() not in hashed_files for path in ignored_assets)
        project_bytes.write_text("changed but still unselected", encoding="utf-8")
        hidden_asset.write_text("hidden asset changed", encoding="utf-8")
        unchanged = discover_pack_metadata(project_root=project, extra_pack_roots=(str(extra_root),))
        assert unchanged == first
        assert scans.call_count == 1

        selected_asset.write_bytes(b"selected asset v2")
        invalidated = discover_pack_metadata(project_root=project, extra_pack_roots=(str(extra_root),))
        assert scans.call_count == 2
        assert any(row.id == "selected" for row in invalidated)

        manifest_path = hidden_local / "pack.yaml"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["visibility"] = "visible"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        visible = discover_pack_metadata(project_root=project, extra_pack_roots=(str(extra_root),))
        assert scans.call_count == 3
        assert [row.id for row in visible if row.id == "local"] == ["local"]
        assert hidden_asset.resolve() in hashed_files

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
