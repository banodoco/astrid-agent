"""Read-only model storage audit against a synthetic volume tree."""
from __future__ import annotations

import json
from pathlib import Path

from scripts.model_storage_audit import audit


def _json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def test_unions_current_lock_old_release_and_engine_receipt_without_deletion(tmp_path):
    mount = tmp_path / "volume"
    models = mount / "astrid/engines/comfyui/models"
    current = models / "diffusion_models/current.bin"
    current.parent.mkdir(parents=True)
    current.write_bytes(b"current")
    alias = models / "alias.bin"
    alias.hardlink_to(current)

    old_root = mount / "h3-golden/releases/v1"
    old_model = old_root / "models/vae/old.bin"
    old_model.parent.mkdir(parents=True)
    old_model.write_bytes(b"old-version")
    _json(old_root / "release-manifest.json", {
        "release_id": "v1", "model_root": str(old_root / "models"),
        "models": [{"path": "vae/old.bin", "bytes": len(b"old-version"), "sha256": "old"}],
    })

    engine = mount / "engine"
    _json(engine / ".astrid-engine/install.json", {
        "schema": "astrid.engine-install.v1", "models_root": str(models),
        "models": [{"path": str(current), "size": len(b"current"), "sha256": "current"}],
    })
    lock = tmp_path / "current.lock.json"
    _json(lock, {"schema": "astrid.models-lock.v1", "models": [
        {"path": "remote/original.bin", "destination": "diffusion_models/current.bin",
         "bytes": len(b"current"), "sha256": "current"},
    ]})

    result = audit(mount, models, locks=(lock,))
    sources = {Path(row["source"]).name: row for row in result["sources"]}
    assert sources["release-manifest.json"]["models"][0]["status"] == "regular"
    assert sources["install.json"]["models"][0]["status"] == "regular"
    assert sources["current.lock.json"]["models"][0]["status"] == "regular"
    assert sources["release-manifest.json"]["root"].endswith("/h3-golden/releases/v1/models")
    assert set(result["not_in_scanned_references"]) == {str(alias)}
    assert result["scanned_file_bytes"] == len(b"current") + len(b"old-version")
    assert result["deletable"] == []
    assert result["reference_coverage_complete"] is False


def test_symlink_directories_are_not_followed_and_referenced_symlink_is_marked(tmp_path):
    mount = tmp_path / "volume"
    models = mount / "models"
    models.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "unseen.bin"
    secret.write_bytes(b"outside")
    (models / "linked").symlink_to(outside, target_is_directory=True)
    (models / "alias.bin").symlink_to(secret)

    lock = tmp_path / "lock.json"
    _json(lock, {"models": [
        {"path": "alias.bin", "bytes": 7},
        {"path": "linked/unseen.bin", "bytes": 7},
    ]})
    result = audit(mount, models, locks=(lock,))
    statuses = {row["file"]: row["status"] for row in result["sources"][0]["models"]}
    assert statuses[str(models / "alias.bin")] == "symlink_or_other_device_or_type"
    assert statuses[str(models / "linked/unseen.bin")] == "symlink_or_other_device_or_type"
    assert all(str(secret) != row["path"] for row in result["files"])


def test_rejects_traversing_manifest_model_root(tmp_path):
    mount = tmp_path / "volume"
    version = mount / "releases/v1"
    _json(version / "release-manifest.json", {
        "model_root": str(mount) + "/../outside", "models": [],
    })
    try:
        audit(mount, mount / "models")
    except ValueError as exc:
        assert "normalized" in str(exc)
    else:
        raise AssertionError("traversing model root was accepted")


def test_incomplete_conventional_model_root_is_scanned_as_unclassified(tmp_path):
    mount = tmp_path / "volume"
    orphan = mount / "h3-golden/incomplete/releases/v2/models/orphan.bin"
    orphan.parent.mkdir(parents=True)
    orphan.write_bytes(b"partial or unreferenced")
    result = audit(mount, mount / "astrid/engines/comfyui/models")
    assert str(orphan.parent) in result["models_roots"]
    assert str(orphan) in result["not_in_scanned_references"]
    assert result["deletable"] == []
    assert result["reference_coverage_complete"] is False


def test_null_models_root_receipt_keeps_legacy_engine_models_reference(tmp_path):
    mount = tmp_path / "volume"
    engine = mount / "legacy-engine"
    model = engine / "models/legacy.bin"
    model.parent.mkdir(parents=True)
    model.write_bytes(b"legacy")
    _json(engine / ".astrid-engine/install.json", {
        "schema": "astrid.engine-install.v1", "models_root": None,
        "models": [{"path": str(model), "bytes": 6, "sha256": "legacy"}],
    })
    result = audit(mount, mount / "cache/models")
    receipt = next(row for row in result["sources"] if row["kind"] == "engine_receipt")
    assert receipt["models"][0]["file"] == str(model)
    assert receipt["models"][0]["status"] == "regular"
