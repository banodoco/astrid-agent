"""One identity source for managed media: the digest comes from the media record."""
from __future__ import annotations

import hashlib
import json

import pytest

from astrid.core.execution.generic_host import HostError, _managed_asset_identity
from astrid.core.rendering.assets import (
    AssetMaterializer,
    managed_asset_digest,
    without_redundant_object_route,
)

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



# banodoco/astrid-dev#1238: the editor stores ``file: /v1/objects/<media_id>``
# on media it imports. That route repeats the entry's own content address; the
# Runtime's render admission drops it, and so may a registry the Runtime will
# freeze again. A route to any other object, or a real path, is kept for the
# caller to refuse. The generic host still refuses every locator it is handed.
ROUTE = "/v1/objects/sha256%3A" + HEX


@pytest.mark.parametrize("route", [ROUTE, "/api/runtime/v1/objects/sha256%3A" + HEX, "/v1/objects/sha256:" + HEX])
def test_redundant_own_object_route_is_dropped(route):
    entry = {"media_id": "sha256:" + HEX, "file": route, "type": "image/png"}
    assert without_redundant_object_route(entry) == {"media_id": "sha256:" + HEX, "type": "image/png"}
    assert entry["file"] == route  # the caller's record is not mutated


@pytest.mark.parametrize("entry", [
    {"media_id": "sha256:" + HEX, "file": "/v1/objects/sha256%3A" + OTHER},
    {"media_id": "sha256:" + HEX, "file": "/Users/someone/hero.png"},
    {"media_id": "sha256:" + HEX, "file": "https://elsewhere.example/v1/objects/sha256%3A" + HEX},
    {"media_id": "opaque-1", "digest": HEX, "file": "/v1/objects/opaque-1"},
    {"media_id": "sha256:" + HEX, "digest": OTHER, "file": ROUTE},
])
def test_other_locators_are_kept_for_the_caller_to_refuse(entry):
    assert without_redundant_object_route(entry) == entry


def test_realm_is_never_dropped_and_the_host_still_refuses_the_route():
    entry = {"media_id": "sha256:" + HEX, "url": ROUTE, "realm": "managed_local"}
    assert without_redundant_object_route(entry) == {"media_id": "sha256:" + HEX, "realm": "managed_local"}
    with pytest.raises(HostError, match="unmanaged locator"):
        _managed_asset_identity("clip", {"media_id": "sha256:" + HEX, "file": ROUTE})
