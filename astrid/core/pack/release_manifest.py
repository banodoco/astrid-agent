"""Apply the frozen C6 release pack pins through the group transaction."""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import sys
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


def release_pins(manifest: Mapping[str, Any]) -> dict[str, dict[str, str]]:
    """The compatibility snapshot of the manifest's validated default list."""
    _source, revision, packs, digests = _default_pins(manifest)
    return {pack_id: {"commit": revision, "digest": digests[pack_id]} for pack_id in packs}


def installed_release_manifest(*, selected: bool = True) -> tuple[Path, dict[str, Any]] | None:
    """Read the executing noneditable install's version-owned selection.

    Source checkouts keep their existing explicit policy. Neither an environment
    variable nor a pack PIN can select a managed release. The installer uses
    ``selected=False`` only inside the genuine candidate interpreter, before
    its already-validated version is selected by the shared update transaction.
    """
    prefix = Path(sys.prefix)
    module = Path(__file__)
    if (not prefix.is_absolute() or prefix.name != "py"
            or prefix.parent.parent.name != "versions"
            or not module.is_absolute() or not module.is_relative_to(prefix)):
        return None
    from astrid.installer.core import Version, current, read_json
    from astrid.installer.tools import validate_manifest
    from runtime_protocol import platform

    folder = prefix.parent
    root = folder.parent.parent
    manifest_path = folder / "manifest.json"
    record_path = folder / "version.json"
    paths = [root, folder.parent, folder, prefix, manifest_path, record_path]
    if selected:
        paths.append(root / "current")
    try:
        for path in paths:
            info = path.lstat()
            if (stat.S_ISLNK(info.st_mode)
                    or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
                    or (hasattr(os, "getuid") and info.st_uid != os.getuid())
                    or (os.name != "nt" and info.st_mode & 0o022)
                    or (os.name == "nt" and not platform.is_owner_only(path))):
                raise ValueError("Untrusted installed release path.")
        if (prefix.resolve() != prefix or module.resolve() != module
                or not manifest_path.is_file() or manifest_path.stat().st_nlink != 1
                or not record_path.is_file() or record_path.stat().st_nlink != 1):
            raise ValueError("Aliased installed release input.")
        version = Version.load(read_json(record_path))
        if (version.root != folder or version.python != Path(sys.executable)
                or not version.profile.is_relative_to(folder)):
            raise ValueError("Installed version and interpreter disagree.")
        if selected and current(root) != version:
            raise ValueError("The executing install is not selected.")
        manifest = validate_manifest(load_release_manifest(manifest_path))
        if manifest.get("release", 0) != version.release:
            raise ValueError("Installed version and release manifest disagree.")
        # This also checks the canonical same-source/same-commit group shape.
        _default_pins(manifest)
        if selected and current(root) != version:
            raise ValueError("The selected install changed while reading its manifest.")
        return folder, manifest
    except (OSError, ValueError, TypeError, KeyError, RuntimeError) as exc:
        raise PackInstallError("invalid_release_manifest", "The installed release manifest is unavailable or disagrees with the selected version.") from exc


def installed_release_pins() -> dict[str, dict[str, str]] | None:
    """Return managed authority, or None for a source/legacy explicit policy."""
    installed = installed_release_manifest()
    if installed is None or "packs" not in installed[1]:
        return None
    return release_pins(installed[1])


def verify_release_default(path: Path, expected: Mapping[str, str]) -> dict[str, Any]:
    """Verify canonical admission and actual bytes, not just a requested PIN.

    This is a read-only projection of the existing group record. The caller
    selects the collection; the loader restricts it to the canonical home
    collection. Builders and the group transaction remain the only producers.
    """
    from astrid.core.pack.identity import pack_digest, read_pin
    from astrid.core.pack.install import _git_source
    from astrid.core.pack.transaction import group_read_lock, read_group, _retained_build_records

    path = Path(path)
    with group_read_lock(path.parent):
        snapshot = read_group(path.parent)
        pin = read_pin(path / "PIN")
        source = _git_source(expected["repo"])
        admission = snapshot.admissions.get(path.name)
        if (snapshot.paths.get(path.name) != path or path.name in snapshot.removals
                or pin["origin"] != "default" or pin["source"] != source + "#" + path.name
                or pin["commit"] != expected["commit"] or pin["digest"] != expected["digest"]
                or pack_digest(path) != expected["digest"]
                or not isinstance(admission, dict)
                or admission.get("packId") != path.name
                or admission.get("sourceDigest") != expected["digest"]
                or type(admission.get("committedRevision")) is not int
                or not 1 <= admission["committedRevision"] <= snapshot.revision
                or not isinstance(admission.get("transactionId"), str)
                or not re.fullmatch(r"[0-9a-f]{32}", admission["transactionId"])
                or (admission["committedRevision"] == snapshot.revision
                    and admission["transactionId"] != snapshot.transaction_id)):
            raise PackInstallError("pin_mismatch", "Default source or canonical admission differs from the selected release.")
        _retained_build_records(path.parent, {path.name: admission}, snapshot.revision)
        raw = (path / ".build/definitions.json").read_bytes()
        record = json.loads(raw)
        if (not isinstance(record, dict)
                or hashlib.sha256(raw).hexdigest() != admission.get("definitionsDigest")
                or record.get("version") != 1 or record.get("repository") != source
                or record.get("revision") != expected["commit"] or record.get("subpath") != path.name
                or not isinstance(record.get("tree"), str) or not _SHA.fullmatch(record["tree"])
                or record.get("source") != {"kind": "git", "repository": source,
                                             "revision": expected["commit"], "tree": record["tree"]}):
            raise PackInstallError("pin_mismatch", "Default definitions provenance differs from its canonical source admission.")
        return pin


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
    if set(group_options) - {"selected", "import_contract", "builds", "toolchain", "consent", "boundary", "on_prepared"}:
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


__all__ = ["load_release_manifest", "install_release_manifest", "release_pins",
           "installed_release_manifest", "installed_release_pins"]
