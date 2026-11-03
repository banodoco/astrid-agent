"""One identity source for managed media: the digest comes from the media record."""
from __future__ import annotations

import hashlib
import json

import pytest

from astrid.core.execution.generic_host import HostError, _managed_asset_identity
from astrid.core.rendering.assets import AssetMaterializer, managed_asset_digest

PAYLOAD = b"managed media bytes"
HEX = hashlib.sha256(PAYLOAD).hexdigest()
OTHER = hashlib.sha256(b"other bytes").hexdigest()


def test_digest_is_derived_from_the_content_addressed_media_id():
    assert managed_asset_digest({"media_id": "sha256:" + HEX}) == HEX
    assert managed_asset_digest({"media_id": HEX, "file": "/host/path.png"}) == HEX


def test_explicit_digest_must_agree_with_the_media_record():
    assert managed_asset_digest({"media_id": "sha256:" + HEX, "digest": "sha256:" + HEX})
    with pytest.raises(ValueError, match="contradicts"):
        managed_asset_digest({"media_id": "sha256:" + HEX, "digest": "sha256:" + OTHER})
    with pytest.raises(HostError, match="contradicts"):
        _managed_asset_identity("clip", {"media_id": "sha256:" + HEX, "content_sha256": OTHER})


def test_opaque_media_id_still_needs_an_explicit_digest():
    assert managed_asset_digest({"media_id": "opaque-1"}) is None
    assert managed_asset_digest({"media_id": "opaque-1", "digest": "sha256:" + HEX}) == "sha256:" + HEX
    with pytest.raises(HostError, match="requires object_id and digest"):
        _managed_asset_identity("clip", {"media_id": "opaque-1"})


def _materialize(tmp_path, entry, objects):
    root = tmp_path / "objects"
    root.mkdir(exist_ok=True)
    staged = {}
    for name, payload in objects.items():
        (root / name).write_bytes(payload)
        staged[name] = str(root / name)
    registry = tmp_path / "assets.json"
    registry.write_text(json.dumps({"assets": {"clip": entry}}), encoding="utf-8")
    return AssetMaterializer(registry, materialized_objects=staged, materialized_root=root,
                             staging_parent=tmp_path / "stage")


def test_materializer_accepts_a_media_id_only_entry(tmp_path):
    with _materialize(tmp_path, {"media_id": "sha256:" + HEX}, {HEX: PAYLOAD}) as materializer:
        assert materializer.assets["clip"].local_path.read_bytes() == PAYLOAD


def test_materializer_refuses_bytes_that_do_not_match_the_media_record(tmp_path):
    with pytest.raises((ValueError, FileNotFoundError)):
        _materialize(tmp_path, {"media_id": "sha256:" + HEX}, {HEX: b"tampered"})


def test_materializer_refuses_an_opaque_media_id_without_a_digest(tmp_path):
    with pytest.raises(ValueError, match="requires a runtime-managed digest"):
        _materialize(tmp_path, {"media_id": "opaque-1"}, {"opaque-1": PAYLOAD})
