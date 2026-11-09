"""Apply the frozen C6 release pack pins through the group transaction."""
from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

from astrid.core.pack.install import PackInstallError
from astrid.core.pack.manifest import load_manifest_mapping
from astrid.core.pack.transaction import GroupResult, install_group

_SHA = re.compile(r"[0-9a-f]{40}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_ID = re.compile(r"[a-z][a-z0-9_]*\Z")
_PIN_FIELDS = {"repo", "commit", "digest_rule", "digest"}


def load_release_manifest(value: str | Path | Mapping[str, Any]) -> dict[str, Any]:
    """Read a release mapping with the shared JSON/YAML parser."""
    try:
        data = dict(value) if isinstance(value, Mapping) else load_manifest_mapping(value, manifest_kind="release")
    except (OSError, ValueError, TypeError) as exc:
        raise PackInstallError("invalid_release_manifest", "Release manifest must be a valid mapping.") from exc
    if not isinstance(data.get("packs", {}), Mapping):
        raise PackInstallError("invalid_release_manifest", "Release manifest packs must be a mapping.")
    return data


def _default_pins(manifest: Mapping[str, Any]) -> tuple[str, str, dict[str, str], dict[str, str]]:
    raw_packs = manifest.get("packs", {})
    repositories: set[str] = set()
    revisions: set[str] = set()
    packs: dict[str, str] = {}
    digests: dict[str, str] = {}
    for pack_id, raw_pin in raw_packs.items():
        if (not isinstance(pack_id, str) or not _ID.fullmatch(pack_id)
                or pack_id in {"personal", "core", "astrid"}):
            raise PackInstallError("invalid_release_manifest", "Release manifest contains an invalid default pack id.")
        if not isinstance(raw_pin, Mapping) or set(raw_pin) != _PIN_FIELDS:
            raise PackInstallError("invalid_release_manifest", "Each default pin must contain repo, commit, digest_rule, and digest.")
        repo, commit = raw_pin.get("repo"), raw_pin.get("commit")
        digest_rule, digest = raw_pin.get("digest_rule"), raw_pin.get("digest")
        if (not isinstance(repo, str) or not repo.strip()
                or not isinstance(commit, str) or not _SHA.fullmatch(commit)
                or digest_rule != "pack-tree-v1"
                or not isinstance(digest, str) or not _DIGEST.fullmatch(digest)):
            raise PackInstallError("invalid_release_manifest", "Default pins require a repository, full commit, pack-tree-v1 rule, and full SHA-256 digest.")
        repositories.add(repo.strip())
        revisions.add(commit)
        packs[pack_id] = pack_id
        digests[pack_id] = digest
    if len(repositories) > 1 or len(revisions) > 1:
        raise PackInstallError("invalid_release_manifest", "Defaults in one release must share a repository and commit for atomic group installation.")
    if not packs:
        return "", "", packs, digests
    return next(iter(repositories)), next(iter(revisions)), packs, digests


def install_release_manifest(*, manifest: str | Path | Mapping[str, Any], packs_root: str | Path,
                             origin: Literal["default", "user"] = "default",
                             python_packages: Mapping[str, str] | None = None,
                             refresh=None, **group_options: Any) -> GroupResult:
    """Apply one release list; default-list reconciliation is lock-scoped.

    Omitted default ids are identified from the locked current collection by
    ``install_group`` and removed in the same publication. They do not create
    user-removal tombstones. User-origin calls install only named packs and
    never bulk-remove other user packs.
    """
    if origin not in {"default", "user"}:
        raise PackInstallError("invalid_origin", "Release manifests support default or user provenance.")
    if set(group_options) - {"selected", "import_contract", "builds", "toolchain", "consent", "boundary"}:
        raise PackInstallError("invalid_request", "Release manifests accept only frozen group validation/build options.")
    data = load_release_manifest(manifest)
    source, revision, packs, digests = _default_pins(data)
    if origin == "user" and not packs:
        raise PackInstallError("invalid_release_manifest", "An explicit user release selection must name at least one pack.")
    release_pins = ({pack_id: {"commit": revision, "digest": digests[pack_id]}
                     for pack_id in packs} if origin == "default" else None)
    return install_group(
        source=source, revision=revision or None, packs=packs, packs_root=packs_root,
        python_packages=dict(python_packages or {}), origin=origin,
        expected_digests=digests, release_pins=release_pins,
        intent="defaults" if origin == "default" else "explicit",
        reconcile_defaults=origin == "default", refresh=refresh, **group_options,
    )


__all__ = ["load_release_manifest", "install_release_manifest"]
