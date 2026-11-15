"""Deterministic, secret-free B-6 profile boot manifest.

The manifest is a derived handoff, not an authority.  It is stamped by the
application composition root under an explicit runtime support root.  The
generic host only reads its hash for completion provenance; it never emits or
discovers profiles.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from runtime_protocol import platform

from astrid.core.foundation.hash import canonical_json_digest
from astrid.installer.boot_restore import (
    _PRE_RESTAMP_SUFFIX,
    _load_stored,
    _reject_lexical_symlinks,
)
from astrid.installer.boot_restore import (
    BOOT_MANIFEST_FILENAME as BOOT_MANIFEST_FILENAME,
)
from astrid.installer.boot_restore import (
    BootManifestCorrupt as BootManifestCorrupt,
)
from astrid.installer.boot_restore import (
    BootManifestError as BootManifestError,
)
from astrid.installer.boot_restore import (
    assert_secret_free as assert_secret_free,
)
from astrid.installer.boot_restore import (
    restore_pre_restamp_boot_manifest as restore_pre_restamp_boot_manifest,
)
from astrid.installer.boot_restore import (
    validate_manifest_path as validate_manifest_path,
)
from astrid.installer.boot_restore import (
    validate_support_root as validate_support_root,
)

BOOT_MANIFEST_SCHEMA_VERSION = 1
_DEFAULT_PROFILE_ORDER: tuple[str, ...] = ()
# Exact stamp written by R9 709492fa5f4850b9caf0cea4a4c543023b73fa0a,
# R10 6e9c4f9c8f4b53aa7c16d4e3e098ab75218bdc45,
# R11 a105de3a4852cb61b9647dfe2c920118bd29f272, and
# R12 0e1543e0da2215db9cb3320cfd1083174c3c62a9 (byte-identical).
# Captures: tests/fixtures/boot_manifest/r9-stamp.json and sibling provenance.
# Only this complete historical identity may migrate to the empty R13 registry;
# recognizing a retired profile name alone does not authenticate its digests.
_LEGACY_BOOT_MANIFEST = {
    "conformance_digest": "6bb502e7e495fa1041cd2726561588326aaf1d97c34ba0c7d779bbd7a57460f3",
    "fixture_digests": {
        "attempt-output-fixture.json": "951b9b4ce85dfc3ab999af705c4a3732acaa1d45468fa0ce0eaaf9edc941b31e",
        "fake-runner-transcript.json": "379c7821ecf6213ced2ede36c2fb991d82c56167616d7e21aa23d7e3890e43e0",
        "host-instance-paired-fixture.json": "4171922e65ea94a2c1af9f9abb7c8d5559125e354ca6ebb7f5efe1266ca22b4f",
        "portable-capability-fixture.json": "9284b056aa8a38f884599a7634be0fe0cb7fbfbd117823960e563375b3aa43b7",
        "task-resource-fixture.json": "ddc2d00bb893ea214d000bf618f67785c89c81d68d892864d9676d3c8b8d5164",
    },
    "profile_digests": {
        "checkout_server": "3bc043a13d64fb7ae3efb683edadd4a95754aef2035e4fc626914ab37637dd18",
        "pip_embedded": "dce36735a51bf3a26a475666dcd54ed889414907d0bed6f2e472e711eca663f9",
    },
    "profile_order": ["pip_embedded", "checkout_server"],
    "registry_digest": "09cde0e29e4ea063f248153628182be23290d835acf4d2d8dfa701cb8be19dc3",
    "schema_version": 1,
}
_REGISTRY_ENTRY_FIELDS = ("definition_version", "binding", "output_policy", "probe")


class BootManifestDrift(BootManifestError):
    """Live registry/fixture identity differs from the stamped manifest."""


def _json_value(value: Any) -> Any:
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return _json_value(value.to_dict())
    if is_dataclass(value):
        return _json_value(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    if isinstance(value, set):
        return sorted(_json_value(item) for item in value)
    return value


def _entry_identity(entry: Any) -> dict[str, Any]:
    raw = _json_value(entry)
    if not isinstance(raw, Mapping):
        raise BootManifestError("profile registry entries must be objects")
    if any(not isinstance(key, str) for key in raw):
        raise BootManifestError("profile registry entry keys must be strings")
    keys = set(raw)
    expected = set(_REGISTRY_ENTRY_FIELDS)
    unknown = keys - expected
    missing = expected - keys
    if unknown:
        raise BootManifestError(
            f"profile registry entry carries unknown fields: {sorted(unknown)!r}"
        )
    if missing:
        raise BootManifestError(
            f"profile registry entry is missing fields: {sorted(missing)!r}"
        )
    definition_version = raw["definition_version"]
    if isinstance(definition_version, bool) or not isinstance(definition_version, int):
        raise BootManifestError("profile registry definition_version must be an integer")
    identity: dict[str, Any] = {"definition_version": definition_version}
    for field in _REGISTRY_ENTRY_FIELDS[1:]:
        value = raw[field]
        if not isinstance(value, str) or not value:
            raise BootManifestError(
                f"profile registry field {field!r} must be a non-empty string"
            )
        identity[field] = value
    return identity


def registry_scope(registry: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and return the complete declarative registry identity."""
    if not isinstance(registry, Mapping):
        raise BootManifestError("profile registry must be an object")
    result: dict[str, Any] = {}
    for profile_id, entry in sorted(registry.items(), key=lambda item: str(item[0])):
        if not isinstance(profile_id, str) or not profile_id:
            raise BootManifestError("profile registry IDs must be non-empty strings")
        if profile_id in result:
            raise BootManifestError(f"duplicate profile registry ID {profile_id!r}")
        result[profile_id] = _entry_identity(entry)
    return result


def fixture_digest(fixture: Any) -> str:
    """Hash one complete profile fixture, including its accepted evidence."""
    return canonical_json_digest(_json_value(fixture))


def _fixture_id(fixture: Any) -> str:
    value = _json_value(fixture)
    if not isinstance(value, Mapping):
        raise BootManifestError("profile fixtures must be objects")
    profile_id = value.get("profile_id", value.get("capability_id"))
    if not isinstance(profile_id, str) or not profile_id:
        raise BootManifestError("profile fixture requires profile_id")
    return profile_id


def fixture_scope(fixtures: Iterable[Any]) -> dict[str, str]:
    rows = tuple(fixtures)
    result: dict[str, str] = {}
    for fixture in rows:
        profile_id = _fixture_id(fixture)
        if profile_id in result:
            raise BootManifestError(f"duplicate profile fixture {profile_id!r}")
        result[profile_id] = fixture_digest(fixture)
    return {key: result[key] for key in sorted(result)}


def compute_registry_digest(registry: Mapping[str, Any], fixtures: Iterable[Any]) -> str:
    """Hash registry admission identity and fixture identity together."""
    return canonical_json_digest({"registry": registry_scope(registry), "fixtures": fixture_scope(fixtures)})


def compute_conformance_digest(fixtures: Iterable[Any]) -> str:
    """Hash the fixture scope independently for evidence and diagnostics."""
    return canonical_json_digest(fixture_scope(fixtures))


def _fixture_file_digests(fixtures: Iterable[Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    for fixture in fixtures:
        raw = _json_value(fixture)
        if not isinstance(raw, Mapping):
            continue
        declared = raw.get("fixture_digests", {})
        if isinstance(declared, Mapping):
            for name, digest in declared.items():
                if str(name) in result and result[str(name)] != str(digest):
                    raise BootManifestError(f"fixture digest disagreement for {name!r}")
                result[str(name)] = str(digest)
    return {key: result[key] for key in sorted(result)}


def consume_profiles(
    fixtures: Iterable[Any],
    *,
    profile_order: Iterable[str] = _DEFAULT_PROFILE_ORDER,
) -> tuple[Any, ...]:
    """Consume explicit profile rows without selecting an engine route."""
    rows = tuple(fixtures)
    expected = tuple(str(item) for item in profile_order)
    actual = tuple(_fixture_id(row) for row in rows)
    if actual != expected:
        raise BootManifestError(
            f"profile fixtures must be consumed in frozen order: expected={expected!r} actual={actual!r}"
        )
    return rows


def build_manifest(
    *,
    registry: Mapping[str, Any] | None = None,
    fixtures: Iterable[Any] | None = None,
    profile_order: Iterable[str] = _DEFAULT_PROFILE_ORDER,
) -> dict[str, Any]:
    """Build the frozen manifest from explicit registry and fixture inputs."""
    if registry is None or fixtures is None:
        raise BootManifestError(
            "build_manifest requires explicit registry and fixtures from the composition root"
        )
    fixture_rows = consume_profiles(fixtures, profile_order=profile_order)
    order = tuple(str(item) for item in profile_order)
    if len(order) != len(set(order)):
        raise BootManifestError("profile order contains duplicates")
    registry_ids = tuple(sorted(str(item) for item in registry))
    if set(order) != set(registry_ids):
        raise BootManifestError(
            f"profile order and registry disagree: order={order!r} registry={registry_ids!r}"
        )
    fixture_ids = set(fixture_scope(fixture_rows))
    if fixture_ids != set(order):
        raise BootManifestError(
            f"profile order and fixtures disagree: order={order!r} fixtures={sorted(fixture_ids)!r}"
        )
    profile_digests = fixture_scope(fixture_rows)
    manifest = {
        "schema_version": BOOT_MANIFEST_SCHEMA_VERSION,
        "profile_order": list(order),
        "profile_digests": {profile_id: profile_digests[profile_id] for profile_id in order},
        "registry_digest": compute_registry_digest(registry, fixture_rows),
        "conformance_digest": compute_conformance_digest(fixture_rows),
        "fixture_digests": _fixture_file_digests(fixture_rows),
    }
    assert_secret_free(manifest)
    return manifest


def manifest_hash(manifest: Mapping[str, Any]) -> str:
    """Return the canonical hash carried by completion provenance."""
    return canonical_json_digest(_json_value(dict(manifest)))


def normalize_sha256_digest(value: str, *, label: str = "digest") -> str:
    """Return bare lowercase SHA-256 hex from raw or ``sha256:`` input."""
    if not isinstance(value, str):
        raise BootManifestError(f"{label} must be a SHA-256 string")
    raw = value.strip().lower()
    if raw.startswith("sha256:"):
        raw = raw.removeprefix("sha256:")
    if len(raw) != 64 or any(char not in "0123456789abcdef" for char in raw):
        raise BootManifestError(f"{label} must be raw 64-character SHA-256 hex")
    return raw


def _preserve_pre_restamp(path: Path, stored: Mapping[str, Any]) -> None:
    """Publish a durable byte copy before restamping, without clobbering it."""
    backup = path.with_name(path.name + _PRE_RESTAMP_SUFFIX)
    _reject_lexical_symlinks(backup, label="pre-restamp backup")
    handle = tempfile.NamedTemporaryFile("wb", dir=path.parent, prefix=".boot-manifest-", suffix=".tmp", delete=False)
    try:
        handle.write(path.read_bytes())
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        try:
            # Linking a complete sibling publishes atomically and never replaces
            # an earlier backup, including on a replay after an interrupted update.
            os.link(handle.name, backup)
        except FileExistsError:
            if not backup.is_file() or _load_stored(backup) != stored:
                raise BootManifestCorrupt(f"pre-restamp backup disagrees with legacy stamp: {backup}") from None
    finally:
        handle.close()
        Path(handle.name).unlink(missing_ok=True)


def stamp_boot_manifest(
    manifest_path: str | Path,
    *,
    support_root: str | Path,
    registry: Mapping[str, Any] | None = None,
    fixtures: Iterable[Any] | None = None,
    profile_order: Iterable[str] = _DEFAULT_PROFILE_ORDER,
    write: bool = True,
) -> dict[str, Any]:
    """Validate live identity; publish only when the caller admits a new host.

    ``write=False`` previews the same drift checks and live identity without
    creating a stamp, backup or directory, or changing resident provenance.
    """
    raw_path = Path(manifest_path).expanduser()
    if raw_path.is_symlink() or (raw_path.exists() and not raw_path.is_file()):
        raise BootManifestCorrupt(
            f"stamped boot manifest must be a non-symlink regular file: {raw_path}"
        )
    path = validate_manifest_path(
        manifest_path, support_root, require_existing=False
    )
    current = build_manifest(
        registry=registry,
        fixtures=fixtures,
        profile_order=profile_order,
    )
    if path.is_symlink():
        raise BootManifestCorrupt(
            f"stamped boot manifest must be a non-symlink regular file: {path}"
        )
    if path.exists() and not path.is_file():
        raise BootManifestCorrupt(
            f"stamped boot manifest must be a regular file: {path}"
        )
    removed_profiles: list[str] = []
    if path.exists():
        stored = _load_stored(path)
        if stored == current:
            return current
        stale_legacy = (
            stored == _LEGACY_BOOT_MANIFEST
            and current["profile_order"] == []
        )
        if not stale_legacy:
            drift = sorted(set(stored) | set(current))
            details = "; ".join(f"key={key} stamped={stored.get(key)!r} live={current.get(key)!r}" for key in drift if stored.get(key) != current.get(key))
            raise BootManifestDrift(f"boot manifest disagrees with live registry/fixtures at {path}: {details}")
        removed_profiles = sorted(stored["profile_order"])
        if write:
            _preserve_pre_restamp(path, stored)
    if not write:
        return current
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=".boot-manifest-", suffix=".tmp", delete=False)
    try:
        json.dump(current, handle, sort_keys=True, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        platform.replace_path(handle.name, path)
    except BaseException:
        try:
            handle.close()
        finally:
            Path(handle.name).unlink(missing_ok=True)
        raise
    if removed_profiles:
        print(
            "boot manifest restamped after removing legacy profiles: "
            + ", ".join(removed_profiles),
            file=sys.stderr,
        )
    return current


def load_boot_manifest_hash(
    manifest_path: str | Path, *, support_root: str | Path
) -> str:
    """Read and hash an existing explicit manifest without writing it."""
    path = validate_manifest_path(manifest_path, support_root)
    if path.is_symlink() or not path.is_file():
        raise BootManifestCorrupt(
            f"stamped boot manifest must be a non-symlink regular file: {path}"
        )
    return manifest_hash(_load_stored(path))


__all__ = [
    "BOOT_MANIFEST_FILENAME",
    "BOOT_MANIFEST_SCHEMA_VERSION",
    "BootManifestCorrupt",
    "BootManifestDrift",
    "BootManifestError",
    "assert_secret_free",
    "validate_manifest_path",
    "validate_support_root",
    "build_manifest",
    "compute_conformance_digest",
    "compute_registry_digest",
    "consume_profiles",
    "fixture_digest",
    "fixture_scope",
    "load_boot_manifest_hash",
    "manifest_hash",
    "normalize_sha256_digest",
    "registry_scope",
    "restore_pre_restamp_boot_manifest",
    "stamp_boot_manifest",
]
