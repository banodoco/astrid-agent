"""Apply a pinned release pack list through the atomic group installer."""
from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

from astrid.core.pack.install import PackInstallError
from astrid.core.pack.identity import read_pin
from astrid.core.pack.transaction import GroupResult, install_group, read_group

_SHA = re.compile(r"[0-9a-f]{40}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_ID = re.compile(r"[a-z][a-z0-9_]*\Z")


def load_release_manifest(value: str | Path | Mapping[str, Any]) -> dict[str, Any]:
    """Load and strictly validate the small JSON release inventory format.

    ``packs`` maps pack ids to ``{path, sha256}``; the common source and full
    Git revision pin every entry. The bytes digest is required for both defaults
    and user-selected release packs.
    """
    try:
        if isinstance(value, Mapping):
            manifest = dict(value)
        else:
            manifest = json.loads(Path(value).read_text(encoding="utf-8"),
                                  object_pairs_hook=_unique_object)
    except (OSError, ValueError, TypeError) as exc:
        raise PackInstallError("invalid_release_manifest", "Release manifest must be valid UTF-8 JSON.") from exc
    if (not isinstance(manifest, dict) or set(manifest) != {"version", "source", "revision", "packs"}
            or type(manifest["version"]) is not int or manifest["version"] != 1
            or not isinstance(manifest["source"], str) or not manifest["source"]
            or any(c in manifest["source"] for c in "\x00\r\n")
            or not isinstance(manifest["revision"], str) or not _SHA.fullmatch(manifest["revision"])
            or not isinstance(manifest["packs"], dict)):
        raise PackInstallError("invalid_release_manifest", "Release manifest fields or version are invalid.")
    packs = manifest["packs"]
    for pack_id, entry in packs.items():
        if (not isinstance(pack_id, str) or not _ID.fullmatch(pack_id)
                or pack_id in {"personal", "core", "astrid"}
                or not isinstance(entry, dict) or set(entry) != {"path", "sha256"}
                or not isinstance(entry["path"], str) or not entry["path"]
                or not isinstance(entry["sha256"], str) or not _DIGEST.fullmatch(entry["sha256"])):
            raise PackInstallError("invalid_release_manifest", "Release manifest pack entries must pin safe paths and SHA256 digests.")
    return manifest


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, item in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = item
    return result


def install_release_manifest(*, manifest: str | Path | Mapping[str, Any], packs_root: str | Path,
                             origin: Literal["default", "user"] = "default",
                             python_packages: Mapping[str, str] | None = None,
                             refresh=None) -> GroupResult:
    """Reconcile a release pack list using the shared atomic group path.

    The same manifest parser and transaction route handle shipped defaults and
    explicit user selections. Existing packs of the selected provenance that
    disappear from the list are removed in the same atomic publication.
    """
    if origin not in {"default", "user"}:
        raise PackInstallError("invalid_origin", "Release manifests support default or user provenance.")
    data = load_release_manifest(manifest)
    desired = data["packs"]
    before = read_group(packs_root)
    stale: list[str] = []
    for pack_id, path in before.paths.items():
        if pack_id in desired:
            continue
        try:
            pin = read_pin(path / "PIN")
        except (OSError, ValueError):
            continue
        if pin.get("origin") == origin:
            stale.append(pack_id)
    digests = {pack_id: entry["sha256"] for pack_id, entry in desired.items()}
    release_pins = ({pack_id: {"commit": data["revision"], "digest": entry["sha256"]}
                     for pack_id, entry in desired.items()} if origin == "default" else None)
    return install_group(
        source=data["source"], revision=data["revision"],
        packs={pack_id: entry["path"] for pack_id, entry in desired.items()},
        packs_root=packs_root, python_packages=dict(python_packages or {}),
        origin=origin, expected_digests=digests,
        release_pins=release_pins, remove=tuple(stale),
        intent="defaults" if origin == "default" else "explicit", refresh=refresh,
    )


__all__ = ["load_release_manifest", "install_release_manifest"]
