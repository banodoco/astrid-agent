"""Canonical source-normalized-snapshot (SNS) identity."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any, Final

SNS_SCHEMA_VERSION: Final[int] = 1

_REQUIRED_FIELDS: Final[tuple[str, ...]] = (
    "schema_version",
    "project_slug",
    "timeline_uuid",
    "timeline_ulid",
    "head_version",
    "head_last_event_id",
    "head_last_hash",
    "assembly_sha256",
    "registry_sha256",
    "media_hashes",
)
_OPTIONAL_IDENTITY_FIELDS: Final[frozenset[str]] = frozenset({"transcript_sha256"})
_OPERATIONAL_FIELDS: Final[frozenset[str]] = frozenset({"created_at", "frozen_at"})
_HEX_RE = re.compile(r"^[0-9a-f]{64}$", flags=re.ASCII)
_ULID_RE = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$", flags=re.ASCII)
_SLUG_RE = re.compile(r"^[a-z0-9]+(?:[-_][a-z0-9]+)*$", flags=re.ASCII)


def canonical_json_bytes(obj: Any) -> bytes:
    """Serialize exactly like the timeline projection canonicalization.

    The defaults intentionally retain ``ensure_ascii=True``.  This mirrors
    ``astrid.core.timeline.projection`` rather than the event serializer,
    which has different normalization rules.
    """

    return json.dumps(
        obj,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def sha256_bytes(payload: bytes) -> str:
    """Return the lowercase bare-hex SHA-256 digest of *payload*."""

    if not isinstance(payload, bytes):
        raise TypeError("sha256_bytes payload must be bytes")
    return hashlib.sha256(payload).hexdigest()


def _require_string(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def _require_sha256(value: Any, field_name: str) -> str:
    candidate = _require_string(value, field_name)
    if _HEX_RE.fullmatch(candidate) is None:
        raise ValueError(f"{field_name} must be a lowercase bare SHA-256 hex digest")
    return candidate


def _snapshot_envelope(snapshot_fields: Mapping[str, Any]) -> dict[str, Any]:
    missing = [field for field in _REQUIRED_FIELDS if field not in snapshot_fields]
    if missing:
        raise ValueError(f"snapshot fields missing required key(s): {', '.join(missing)}")

    allowed = set(_REQUIRED_FIELDS) | _OPTIONAL_IDENTITY_FIELDS | _OPERATIONAL_FIELDS
    unexpected = sorted(key for key in snapshot_fields if key not in allowed)
    if unexpected:
        raise ValueError(f"unexpected snapshot field(s): {', '.join(unexpected)}")

    schema_version = snapshot_fields["schema_version"]
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version != SNS_SCHEMA_VERSION
    ):
        raise ValueError(f"schema_version must be {SNS_SCHEMA_VERSION}")

    project_slug = _require_string(snapshot_fields["project_slug"], "project_slug")
    if _SLUG_RE.fullmatch(project_slug) is None:
        raise ValueError("project_slug must be a lowercase slug")

    # Runtime's canonical timeline_id is an opaque non-empty string.  The
    # historical SNS field remains named ``timeline_uuid`` for schema-v1
    # compatibility, but carries the exact runtime identity without rewriting.
    timeline_uuid = _require_string(snapshot_fields["timeline_uuid"], "timeline_uuid")

    timeline_ulid = _require_string(snapshot_fields["timeline_ulid"], "timeline_ulid")
    if _ULID_RE.fullmatch(timeline_ulid) is None:
        raise ValueError("timeline_ulid must be an uppercase canonical ULID")

    head_version = snapshot_fields["head_version"]
    if isinstance(head_version, bool) or not isinstance(head_version, int) or head_version < 0:
        raise ValueError("head_version must be a non-negative integer")

    head_last_event_id_raw = snapshot_fields["head_last_event_id"]
    head_last_hash_raw = snapshot_fields["head_last_hash"]
    if head_version == 0:
        if head_last_event_id_raw is not None or head_last_hash_raw is not None:
            raise ValueError("version-zero event head must have null tail fields")
        head_last_event_id = None
        head_last_hash = None
    else:
        head_last_event_id = _require_string(head_last_event_id_raw, "head_last_event_id")
        if _ULID_RE.fullmatch(head_last_event_id) is None:
            raise ValueError("head_last_event_id must be an uppercase canonical ULID")
        head_last_hash = _require_sha256(head_last_hash_raw, "head_last_hash")

    media_hashes_raw = snapshot_fields["media_hashes"]
    if not isinstance(media_hashes_raw, Mapping):
        raise ValueError("media_hashes must be an object")
    media_hashes: dict[str, str] = {}
    for asset_key, digest in sorted(media_hashes_raw.items(), key=lambda item: str(item[0])):
        if not isinstance(asset_key, str) or not asset_key:
            raise ValueError("media_hashes keys must be non-empty strings")
        media_hashes[asset_key] = _require_sha256(digest, f"media_hashes[{asset_key!r}]")

    envelope: dict[str, Any] = {
        "schema_version": SNS_SCHEMA_VERSION,
        "project_slug": project_slug,
        "timeline_uuid": timeline_uuid,
        "timeline_ulid": timeline_ulid,
        "head_version": head_version,
        "head_last_event_id": head_last_event_id,
        "head_last_hash": head_last_hash,
        "assembly_sha256": _require_sha256(snapshot_fields["assembly_sha256"], "assembly_sha256"),
        "registry_sha256": _require_sha256(snapshot_fields["registry_sha256"], "registry_sha256"),
        "media_hashes": media_hashes,
    }

    transcript_sha256 = snapshot_fields.get("transcript_sha256")
    if transcript_sha256 is not None:
        envelope["transcript_sha256"] = _require_sha256(transcript_sha256, "transcript_sha256")
    return envelope


def sns_digest(snapshot_fields: dict[str, Any]) -> str:
    """Return ``SNS:<hex>`` for the whitelisted canonical snapshot envelope.

    ``created_at`` and ``frozen_at`` are accepted only as operational metadata
    and are deliberately excluded.  Any other unknown key is rejected so a
    new identity-bearing fact cannot be silently omitted from the digest.
    """

    if not isinstance(snapshot_fields, dict):
        raise TypeError("snapshot_fields must be a dict")
    return f"SNS:{sha256_bytes(canonical_json_bytes(_snapshot_envelope(snapshot_fields)))}"


__all__ = [
    "SNS_SCHEMA_VERSION",
    "canonical_json_bytes",
    "sha256_bytes",
    "sns_digest",
]
