"""Release-manifest defaults routed through the durable pack group transaction.

The release manifest is the sole source of default pack pins. Installed
presence and removal preference remain in the group collection record; this
module only translates the manifest into one M1-16 transaction request.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from astrid.core.pack.install import PackInstallError
from astrid.core.pack.manifest import load_manifest_mapping
from astrid.core.pack.transaction import GroupResult, install_group

_PACK_ID = re.compile(r"[a-z][a-z0-9_]*\Z")
_COMMIT = re.compile(r"[0-9a-f]{40}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_PIN_FIELDS = {"repo", "commit", "digest_rule", "digest"}
_GROUP_OPTIONS = {
    "selected", "release_pins", "import_contract", "builds", "toolchain",
    "refresh", "boundary",
}


def load_release_manifest(path: str | Path) -> dict[str, Any]:
    """Read the already-defined JSON/YAML release-manifest mapping."""
    try:
        return load_manifest_mapping(path, manifest_kind="release")
    except ValueError as exc:
        raise PackInstallError("invalid_release_manifest", "Release manifest must be a valid mapping.") from exc


def _default_pins(release_manifest: Mapping[str, Any]) -> tuple[str, str, dict[str, str], dict[str, str]]:
    if not isinstance(release_manifest, Mapping):
        raise PackInstallError("invalid_release_manifest", "Release manifest must be a mapping.")
    raw_packs = release_manifest.get("packs", {})
    if not isinstance(raw_packs, Mapping):
        raise PackInstallError("invalid_release_manifest", "Release manifest packs must be a mapping of pack ids to pins.")

    repositories: set[str] = set()
    revisions: set[str] = set()
    packs: dict[str, str] = {}
    digests: dict[str, str] = {}
    for pack_id, raw_pin in raw_packs.items():
        if not isinstance(pack_id, str) or not _PACK_ID.fullmatch(pack_id):
            raise PackInstallError("invalid_release_manifest", "Release manifest contains an invalid default pack id.")
        if not isinstance(raw_pin, Mapping) or set(raw_pin) != _PIN_FIELDS:
            raise PackInstallError("invalid_release_manifest", "Each default pack pin must contain repo, commit, digest_rule, and digest.")
        repo = raw_pin.get("repo")
        commit = raw_pin.get("commit")
        digest_rule = raw_pin.get("digest_rule")
        digest = raw_pin.get("digest")
        if (not isinstance(repo, str) or not repo.strip()
                or not isinstance(commit, str) or not _COMMIT.fullmatch(commit)
                or digest_rule != "pack-tree-v1"
                or not isinstance(digest, str) or not _DIGEST.fullmatch(digest)):
            raise PackInstallError("invalid_release_manifest", "Default pack pins require a repository, full commit, pack-tree-v1 rule, and full SHA-256 digest.")
        repositories.add(repo.strip())
        revisions.add(commit)
        packs[pack_id] = pack_id
        digests[pack_id] = digest
    if len(repositories) > 1 or len(revisions) > 1:
        raise PackInstallError("invalid_release_manifest", "Defaults in one release manifest must share one repository and commit for atomic group installation.")
    if not packs:
        return "", "", packs, digests
    return next(iter(repositories)), next(iter(revisions)), packs, digests


def install_defaults(
    release_manifest: Mapping[str, Any],
    *,
    packs_root: str | Path,
    python_packages: Mapping[str, str],
    **group_options: Any,
) -> GroupResult | None:
    """Install this release's defaults as one M1-16 group transaction.

    Empty/missing ``packs`` is a no-op. Existing user packs are not named in
    the transaction and therefore remain byte-for-byte unchanged. Group
    publication owns provenance, removal tombstones, and rollback recovery.
    """
    if set(group_options) - _GROUP_OPTIONS:
        raise PackInstallError("invalid_request", "Defaults accept only the group transaction's validation, build, refresh, and boundary options.")
    source, revision, packs, digests = _default_pins(release_manifest)
    if not packs:
        return None
    return install_group(
        source=source,
        revision=revision,
        packs=packs,
        packs_root=packs_root,
        python_packages=python_packages,
        origin="default",
        expected_digests=digests,
        intent="defaults",
        **group_options,
    )


__all__ = ["install_defaults", "load_release_manifest"]
