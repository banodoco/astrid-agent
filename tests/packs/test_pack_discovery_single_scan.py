"""Per-call scan reuse must preserve strict admission and discovery authority."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from astrid.core.pack import PackValidationError, discover_packs, loader, source_setup
from astrid.core.pack.discovery import discover_pack_metadata
from astrid.core.rendering import registry


def _write_pack(parent: Path, pack_id: str, *, duplicate: bool = False, **fields) -> Path:
    root = parent / pack_id
    root.mkdir(parents=True)
    names = ["a.yaml", "b.yaml"] if duplicate else ["a.yaml"]
    manifest = {
        "schema_version": 2,
        "id": pack_id,
        "name": pack_id,
        "version": "1.0.0",
        "extensions": {"rendering": {"renderers": names}},
        "aliases": [{"kind": "renderer", "alias": f"{pack_id}.alias",
                     "canonical_id": f"{pack_id}.renderer"}],
        **fields,
    }
    (root / "pack.yaml").write_text(json.dumps(manifest), encoding="utf-8")
    for name in names:
        (root / name).write_text(json.dumps({
            "schema_version": 1,
            "id": f"{pack_id}.renderer",
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
        assert scans.call_count == 2
    with mock.patch.object(loader, "scan_packs", wraps=loader.scan_packs) as scans:
        actual = discover_pack_metadata(
            project_root=project, extra_pack_roots=extra_roots,
            discover_packs_fn=discover_packs if explicit_scanner else None,
        )
        assert scans.call_count == 1
    # Dataclass equality includes every PackDefinition field, aliases and source hashes.
    assert actual == expected
    assert [(row.id, row.source_kind, row.priority_index) for row in actual] == [
        ("alpha", "source", 0), ("zeta", "source", 1), ("local", "local", 2),
        ("alpha", "managed", 3), ("alpha", "extra", 4), ("alpha", "env", 5),
    ]

    with mock.patch.object(registry, "discover_packs", _two_call_scanner):
        expected_registries = registry.load_default_registries(project, extra_pack_roots=extra_roots)
    actual_registries = registry.load_default_registries(project, extra_pack_roots=extra_roots)
    for actual_registry, expected_registry in zip(actual_registries, expected_registries):
        assert actual_registry.candidates() == expected_registry.candidates()
        assert actual_registry.list() == expected_registry.list()
    renderers = actual_registries[0]
    assert len(renderers.candidates("alpha.renderer")) == 5
    assert [c.source_kind for c in renderers.candidates("alpha.renderer")] == [
        "source", "source", "managed", "extra", "env",
    ]
    assert [c.execution_eligible for c in renderers.candidates("alpha.renderer")] == [
        True, True, True, True, False,
    ]
    for alias in ("alpha.alias", "local.alias", "zeta.alias"):
        assert renderers.resolve_evidence(alias) == expected_registries[0].resolve_evidence(alias)
    assert renderers.get("alpha.alias").manifest.name == "a.yaml"


def test_different_roots_keep_project_scan_and_do_not_leak_source_local(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "installed/astrid/packs"
    _write_pack(source, "alpha")
    _write_pack(source, "local", description="source local must not leak")
    project = tmp_path / "project"
    project_packs = project / "astrid/packs"
    local = _write_pack(project_packs, "local", description="project local")
    _write_pack(project_packs, "stray")
    monkeypatch.setattr(loader, "packs_root", lambda: source)
    with mock.patch.object(loader, "scan_packs", wraps=loader.scan_packs) as scans:
        rows = discover_pack_metadata(project_root=project)
        assert scans.call_args_list == [mock.call(None, include_hidden=False, strict=True),
                                       mock.call(project_packs, include_hidden=False, strict=True)]
    assert [(r.id, r.source_kind) for r in rows] == [("alpha", "source"), ("local", "local")]
    assert rows[1].pack.root == local
    assert rows == discover_pack_metadata(project_root=project, discover_packs_fn=_two_call_scanner)
    (project_packs / "stray/pack.yaml").write_text("schema_version: 999\n", encoding="utf-8")
    with pytest.raises(PackValidationError):
        discover_pack_metadata(project_root=project)


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
    scanner = mock.Mock(side_effect=[(alpha, local), ()])
    rows = discover_pack_metadata(project_root=project, discover_packs_fn=scanner)
    assert scanner.call_args_list == [mock.call(), mock.call(packs)]
    assert [r.id for r in rows] == ["alpha"]


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
            "schema_version: 999\nvisibility: hidden\n", encoding="utf-8",
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
    assert [(r.id, r.source_kind) for r in rows] == [("local", "local")]


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
