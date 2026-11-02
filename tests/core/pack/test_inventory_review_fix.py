from pathlib import Path

import pytest
import yaml

from astrid.core.pack import loader
from astrid.core.pack._common import PackValidationError
from astrid.core.pack.discovery import discover_pack_metadata


def _make(root: Path, pack_id: str, *, dependencies=(), source="") -> Path:
    path = root / pack_id
    path.mkdir(parents=True)
    manifest = {"schema_version": 3, "id": pack_id, "name": pack_id,
                "version": "1.0.0", "pack_api": "1.0", "resources": [],
                "dependencies": {"packs": list(dependencies)}}
    (path / "pack.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
    if source:
        (path / "action.py").write_text(source, encoding="utf-8")
    return path


def test_dependency_resolves_across_configured_roots(tmp_path, monkeypatch):
    bundled, first, second = (tmp_path / name for name in ("bundled", "first", "second"))
    bundled.mkdir()
    _make(first, "consumer", dependencies=("provider",))
    _make(second, "provider")
    monkeypatch.setattr(loader, "packs_root", lambda: bundled)
    monkeypatch.setenv("ASTRID_PACKS_PATH", str(second))
    result = discover_pack_metadata(project_root=tmp_path, extra_pack_roots=(str(first),))
    assert {item.id for item in result} == {"consumer", "provider"}


def test_checker_refuses_forbidden_import(tmp_path, monkeypatch):
    bundled = tmp_path / "bundled"
    bundled.mkdir()
    root = tmp_path / "external"
    _make(root, "bad", source="from astrid.core.pack import loader\n")
    _make(root, "good", source="from astrid.pack_api import VERSION\n")
    monkeypatch.setattr(loader, "packs_root", lambda: bundled)
    scan = loader.scan_packs(root, strict=False)
    assert [pack.id for pack in scan.packs] == ["good"]
    assert [record.pack_id for record in scan.quarantined] == ["bad"]
    assert "imports code outside" in scan.quarantined[0].error


def test_default_source_pin_conflict(tmp_path, monkeypatch):
    bundled = tmp_path / "bundled"
    bundled.mkdir()
    root = tmp_path / "external"
    _make(root, "consumer", dependencies=("hivemind",))
    _make(root, "hivemind")
    monkeypatch.setattr(loader, "packs_root", lambda: bundled)
    scan = loader.scan_packs(root, strict=False)
    assert "consumer" not in {pack.id for pack in scan.packs}
    assert any(record.pack_id == "consumer" and "release default pin" in record.error
               for record in scan.quarantined)


def test_malformed_optional_and_required(tmp_path, monkeypatch):
    bundled = tmp_path / "bundled"
    bundled.mkdir()
    root = tmp_path / "external"
    _make(root, "good")
    bad = root / "bad"
    bad.mkdir()
    (bad / "pack.yaml").write_text("schema_version: [\n", encoding="utf-8")
    monkeypatch.setattr(loader, "packs_root", lambda: bundled)
    scan = loader.scan_packs(root, strict=False)
    assert [pack.id for pack in scan.packs] == ["good"]
    assert [record.pack_id for record in scan.quarantined] == ["bad"]
    assert "\n" not in scan.quarantined[0].error
    assert [pack.id for pack in loader.discover_packs(root)] == ["good"]
    with pytest.raises(PackValidationError):
        loader.scan_packs(root, strict=False, required_pack_ids=frozenset({"bad"}))


def test_duplicate_ids_across_roots_are_refused(tmp_path, monkeypatch):
    bundled, first, second = (tmp_path / name for name in ("bundled", "first", "second"))
    bundled.mkdir()
    _make(first, "same")
    _make(second, "same")
    monkeypatch.setattr(loader, "packs_root", lambda: bundled)
    monkeypatch.setenv("ASTRID_PACKS_PATH", str(second))
    with pytest.raises(PackValidationError, match="duplicate pack id"):
        discover_pack_metadata(project_root=tmp_path, extra_pack_roots=(str(first),))


def test_default_scan_collects_roots_before_closure(tmp_path, monkeypatch):
    bundled = tmp_path / "bundled"
    bundled.mkdir()
    home = tmp_path / "home"
    defaults = home / "Astrid" / "packs"
    extra = tmp_path / "extra"
    _make(defaults, "consumer", dependencies=("provider",))
    _make(extra, "provider")
    monkeypatch.setattr(loader, "packs_root", lambda: bundled)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("ASTRID_PACKS_PATH", str(extra))
    assert {pack.id for pack in loader.discover_packs()} == {"consumer", "provider"}


def test_rejected_dependency_quarantines_dependent(tmp_path, monkeypatch):
    bundled = tmp_path / "bundled"
    bundled.mkdir()
    root = tmp_path / "external"
    _make(root, "consumer", dependencies=("provider",))
    _make(root, "provider", source="import astrid.core.pack.loader\n")
    monkeypatch.setattr(loader, "packs_root", lambda: bundled)
    scan = loader.scan_packs(root, strict=False)
    assert not scan.packs
    assert {record.pack_id for record in scan.quarantined} == {"consumer", "provider"}
    assert any(record.pack_id == "consumer" and "dependency is unavailable" in record.error
               for record in scan.quarantined)
