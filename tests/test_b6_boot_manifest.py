from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from astrid.core.execution import generic_host
from astrid.core.execution.generic_host import GenericPackHost, HostError
from astrid.core.gateway.dispatch import compose_profile_handoff
from astrid.core._shared.boot_manifest import (
    BootManifestCorrupt,
    BootManifestDrift,
    BootManifestError,
    assert_secret_free,
    build_manifest,
    load_boot_manifest_hash,
    manifest_hash,
    normalize_sha256_digest,
    validate_manifest_path,
)
from astrid.core.receipts.contract import CommandReceipt, RECEIPT_SHAPE_KEYS
from astrid.core.gateway.boot_conformance import (
    FROZEN_FIXTURE_DIGESTS,
    VIBE_PROFILE_ORDER,
    VIBE_PROFILE_REGISTRY,
    vibe_profile_specs,
)


def _paths(tmp_path: Path) -> tuple[Path, Path]:
    support = tmp_path / "runtime"
    support.mkdir()
    return support, support / "astrid-host" / "boot-manifest.json"


def test_manifest_is_canonical_and_profile_order_is_frozen() -> None:
    first = build_manifest(
        registry=VIBE_PROFILE_REGISTRY,
        fixtures=vibe_profile_specs(),
    )
    second = build_manifest(
        registry=VIBE_PROFILE_REGISTRY,
        fixtures=vibe_profile_specs(),
    )
    assert first == second
    assert first["profile_order"] == list(VIBE_PROFILE_ORDER)
    assert set(first["fixture_digests"]) == set(FROZEN_FIXTURE_DIGESTS)
    assert manifest_hash(first) == manifest_hash(second)


def test_manifest_requires_explicit_registry_and_fixtures() -> None:
    with pytest.raises(BootManifestError, match="explicit registry and fixtures"):
        build_manifest(fixtures=vibe_profile_specs())
    with pytest.raises(BootManifestError, match="explicit registry and fixtures"):
        build_manifest(registry=VIBE_PROFILE_REGISTRY)


@pytest.mark.parametrize("value", ["a" * 64, "sha256:" + "A" * 64])
def test_sha256_digest_normalization_accepts_wire_forms(value: str) -> None:
    assert normalize_sha256_digest(value) == "a" * 64


@pytest.mark.parametrize("value", ["", "sha256:short", "md5:" + "a" * 64, "g" * 64])
def test_sha256_digest_normalization_rejects_malformed_values(value: str) -> None:
    with pytest.raises(BootManifestError, match="SHA-256"):
        normalize_sha256_digest(value)


def test_composition_root_stamps_under_explicit_support_without_sqlite(tmp_path: Path) -> None:
    support, manifest = _paths(tmp_path)
    handoff = compose_profile_handoff(manifest, support_root=support)
    assert Path(handoff["path"]) == manifest
    assert manifest.is_file()
    assert not (tmp_path / ".astrid" / "astrid.sqlite3").exists()
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    assert_secret_free(payload)
    assert "secret" not in manifest.read_text(encoding="utf-8").lower()
    expected = build_manifest(
        registry=VIBE_PROFILE_REGISTRY,
        fixtures=vibe_profile_specs(),
    )
    assert payload == expected
    assert handoff["sha256"] == manifest_hash(expected)
    assert load_boot_manifest_hash(manifest, support_root=support) == handoff["sha256"]


def test_manifest_publish_retries_windows_sharing_violation(tmp_path: Path, monkeypatch) -> None:
    import os

    from astrid.core._shared import boot_manifest

    calls = []
    def move(source, destination):
        calls.append((source, destination))
        if len(calls) == 1:
            error = PermissionError("file is in use")
            error.winerror = 32
            raise error
        os.replace(source, destination)

    monkeypatch.setattr(boot_manifest.platform.sys, "platform", "win32")
    monkeypatch.setattr(boot_manifest.platform, "_move_file_write_through", move)
    monkeypatch.setattr(boot_manifest.platform.time, "sleep", lambda _: None)
    support, manifest = _paths(tmp_path)

    compose_profile_handoff(manifest, support_root=support)

    assert len(calls) == 2
    assert manifest.is_file()


def test_manifest_drift_refuses_existing_stamp(tmp_path: Path) -> None:
    support, manifest = _paths(tmp_path)
    compose_profile_handoff(manifest, support_root=support)
    registry = {key: dict(value) for key, value in VIBE_PROFILE_REGISTRY.items()}
    registry["pip_embedded"]["probe"] = "mutated"
    with pytest.raises(BootManifestDrift, match="registry/fixtures"):
        compose_profile_handoff(manifest, support_root=support, registry=registry)


def test_manifest_validation_preserves_out_of_root_and_symlink_guards(tmp_path: Path) -> None:
    support, manifest = _paths(tmp_path)
    compose_profile_handoff(manifest, support_root=support)
    outside = tmp_path / "outside" / "boot-manifest.json"
    outside.parent.mkdir()
    outside.write_text(manifest.read_text(encoding="utf-8"), encoding="utf-8")
    with pytest.raises(BootManifestError, match="contained"):
        validate_manifest_path(outside, support)
    symlink = support / "symlink-manifest.json"
    symlink.symlink_to(manifest)
    with pytest.raises(BootManifestError, match="named"):
        validate_manifest_path(symlink, support)
    intermediate = support / "linked-host"
    intermediate.symlink_to(manifest.parent, target_is_directory=True)
    with pytest.raises(BootManifestError, match="symlink component"):
        validate_manifest_path(intermediate / "boot-manifest.json", support)
    manifest.unlink()
    manifest.symlink_to(outside)
    with pytest.raises(BootManifestError, match="regular"):
        validate_manifest_path(manifest, support)


def test_generic_host_preserves_lexical_symlink_parent_for_validation(tmp_path: Path) -> None:
    support, manifest = _paths(tmp_path)
    compose_profile_handoff(manifest, support_root=support)
    linked = tmp_path / "linked-host"
    linked.symlink_to(manifest.parent, target_is_directory=True)
    host = GenericPackHost(pack_roots=[tmp_path], boot_manifest_path=linked / manifest.name)
    with pytest.raises(BootManifestError, match="symlink component"):
        host.boot_manifest_provenance()


def test_symlinked_support_root_ancestor_is_rejected_by_host_provenance(
    tmp_path: Path,
) -> None:
    real_base = tmp_path / "real-base"
    support = real_base / "support"
    support.mkdir(parents=True)
    manifest = support / "astrid-host" / "boot-manifest.json"
    compose_profile_handoff(manifest, support_root=support)
    alias = tmp_path / "alias"
    alias.symlink_to(real_base, target_is_directory=True)
    aliased_support = alias / "support"
    aliased_manifest = aliased_support / "astrid-host" / "boot-manifest.json"
    with pytest.raises(BootManifestError, match="symlink component"):
        validate_manifest_path(aliased_manifest, aliased_support)
    host = GenericPackHost(pack_roots=[tmp_path], boot_manifest_path=aliased_manifest)
    with pytest.raises(BootManifestError, match="symlink component"):
        host.boot_manifest_provenance()


def test_generic_host_cli_requires_existing_manifest_and_never_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    support, manifest = _paths(tmp_path)
    pack_root = tmp_path / "packs"
    pack_root.mkdir()
    monkeypatch.setattr(
        sys,
        "argv",
        ["astrid-generic-host", "--pack-root", str(pack_root), "--support-root", str(support)],
    )
    with pytest.raises(SystemExit):
        generic_host._cli()
    assert not manifest.exists()
    assert not (tmp_path / ".astrid" / "astrid.sqlite3").exists()


def test_generic_host_cli_reads_explicit_manifest_without_sqlite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    support, manifest = _paths(tmp_path)
    compose_profile_handoff(manifest, support_root=support)
    pack_root = tmp_path / "packs"
    pack_root.mkdir()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "astrid-generic-host",
            "--pack-root", str(pack_root),
            "--support-root", str(support),
            "--boot-manifest-path", str(manifest),
        ],
    )
    assert generic_host._cli() == 0
    expected_hash = load_boot_manifest_hash(manifest, support_root=support)
    host = GenericPackHost(
        pack_roots=[pack_root],
        boot_manifest_path=manifest,
        boot_manifest_hash="sha256:" + expected_hash,
    )
    assert host.boot_manifest_provenance() == {
        "kind": "astrid.boot_manifest",
        "sha256": expected_hash,
    }
    assert not (tmp_path / ".astrid" / "astrid.sqlite3").exists()


def test_host_provenance_detects_startup_manifest_drift(tmp_path: Path) -> None:
    support, manifest = _paths(tmp_path)
    compose_profile_handoff(manifest, support_root=support)
    expected = load_boot_manifest_hash(manifest, support_root=support)
    host = GenericPackHost(
        pack_roots=[tmp_path],
        boot_manifest_path=manifest,
        boot_manifest_hash=expected,
    )
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["profile_order"] = list(reversed(payload["profile_order"]))
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(HostError, match="changed"):
        host.boot_manifest_provenance()


def test_corrupt_manifest_is_read_only_failure(tmp_path: Path) -> None:
    support, manifest = _paths(tmp_path)
    manifest.parent.mkdir()
    manifest.write_text(json.dumps({"secret": "never"}), encoding="utf-8")
    with pytest.raises(BootManifestCorrupt):
        load_boot_manifest_hash(manifest, support_root=support)


def test_command_receipt_shape_remains_exactly_nine_keys() -> None:
    assert RECEIPT_SHAPE_KEYS == frozenset(
        {
            "receipt_id", "command_kind", "idempotency_key", "request_hash",
            "project_id", "project_seq", "event_ids", "result", "created_at",
        }
    )
    receipt = CommandReceipt(
        receipt_id="r1", command_kind="shots.create", idempotency_key="k1",
        request_hash="h1", project_id="p1", project_seq=(1, 1), event_ids=("e1",),
        result={"provenance": {"kind": "astrid.boot_manifest", "sha256": "a" * 64}},
        created_at="2026-09-04T00:00:00Z",
    )
    assert set(receipt.as_dict()) == RECEIPT_SHAPE_KEYS
