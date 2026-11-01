from __future__ import annotations

import re
from typing import Any

from astrid.core.timeline.banodoco_schema import _ASSET_ENTRY_ALLOWED, _raise_unknown_keys
from astrid.core.timeline.validators.metadata import _validate_generated_at


def _animation_ids() -> set[str]:
    from astrid.core.element import catalog as effects_catalog
    return set(effects_catalog.list_animation_ids())


def _transition_ids() -> set[str]:
    from astrid.core.element import catalog as effects_catalog
    return set(effects_catalog.list_transition_ids())


def _animation_meta(animation_id: str) -> dict[str, Any]:
    from astrid.core.element import catalog as effects_catalog

    try:
        return effects_catalog.read_animation_meta(animation_id)
    except Exception:  # noqa: BLE001 - optional catalog metadata must not block validation
        return {}


def validate_registry(registry: Any) -> None:
    if not isinstance(registry, dict):
        raise ValueError("Asset registry must be a JSON object")
    _raise_unknown_keys("Asset registry", registry, frozenset({"assets"}))
    assets = registry.get("assets")
    if not isinstance(assets, dict):
        raise ValueError("Asset registry.assets must be an object")
    for key, entry in assets.items():
        if not isinstance(entry, dict):
            raise ValueError(f"Asset registry.assets[{key!r}] must be an object")
        _raise_unknown_keys(f"Asset registry.assets[{key!r}]", entry, _ASSET_ENTRY_ALLOWED)
        media_id = entry.get("media_id")
        if media_id is not None and (
            not isinstance(media_id, str) or not media_id.strip()
        ):
            raise ValueError(f"Asset {key!r}.media_id must be a non-empty string")
        if "file" not in entry and "url" not in entry and media_id is None:
            raise ValueError(f"Asset {key!r} must have 'file', 'url', or 'media_id'")
        url = entry.get("url")
        if url is not None and (not isinstance(url, str) or not url.startswith(("http://", "https://"))):
            raise ValueError(f"Asset {key!r}.url must be an http(s) URL")
        origin = entry.get("origin")
        if origin is not None and origin not in {"immutable-public", "refreshable-from-generation", "opaque-foreign"}:
            raise ValueError(
                f"Asset {key!r}.origin must be one of immutable-public, refreshable-from-generation, opaque-foreign"
            )
        content_sha256 = entry.get("content_sha256")
        if content_sha256 is not None and (
            not isinstance(content_sha256, str) or re.fullmatch(r"[0-9a-f]{64}", content_sha256) is None
        ):
            raise ValueError(f"Asset {key!r}.content_sha256 must be a 64-character lowercase hex string")
        derived_from = entry.get("derivedFrom")
        if derived_from is not None:
            if not isinstance(derived_from, dict):
                raise ValueError(f"Asset {key!r}.derivedFrom must be an object")
            role = derived_from.get("role")
            if role not in {"thumbnail", "proxy", "render-output"}:
                raise ValueError(f"Asset {key!r}.derivedFrom.role must be thumbnail, proxy, or render-output")
            asset_id = derived_from.get("assetId")
            if asset_id is not None and (not isinstance(asset_id, str) or not asset_id):
                raise ValueError(f"Asset {key!r}.derivedFrom.assetId must be a non-empty string")
            parent_sha = derived_from.get("content_sha256")
            if parent_sha is not None and (
                not isinstance(parent_sha, str) or re.fullmatch(r"[0-9a-f]{64}", parent_sha) is None
            ):
                raise ValueError(
                    f"Asset {key!r}.derivedFrom.content_sha256 must be a 64-character lowercase hex string"
                )
        if "url_expires_at" in entry:
            _validate_generated_at(entry.get("url_expires_at"), f"Asset {key!r}.url_expires_at")
        etag = entry.get("etag")
        if etag is not None and (not isinstance(etag, str) or not etag):
            raise ValueError(f"Asset {key!r}.etag must be a non-empty string")
