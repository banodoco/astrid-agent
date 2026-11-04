from __future__ import annotations

import json
from typing import Any

import pytest

from astrid.core.timeline import validate_timeline, validate_timeline_config_for_container
from astrid.core.timeline.validators.meta import MetaValidationError, validate_meta


def _timeline() -> dict[str, Any]:
    return {
        "tracks": [{"id": "track", "kind": "visual", "label": "Picture"}],
        "clips": [{"id": "clip", "at": 0, "track": "track"}],
    }


def _canonical_size(value: object) -> int:
    return len(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode())


def _value_at_size(target: int) -> dict[str, str]:
    value = {"value": ""}
    value["value"] = "x" * (target - _canonical_size(value))
    assert _canonical_size(value) == target
    return value


def test_meta_preserves_opaque_canonical_json() -> None:
    meta = {
        "org.example.notes": {
            "missing_asset_id": "asset-not-in-registry",
            "nullable": None,
            "ordered": [2, 1],
        },
    }
    original = json.loads(json.dumps(meta))

    validate_meta(meta)

    assert meta == original


def test_meta_reports_structured_invalid_and_limit_errors() -> None:
    with pytest.raises(MetaValidationError) as invalid:
        validate_meta({"org.example.notes": None})
    assert invalid.value.code == "invalid_meta"
    assert invalid.value.path == "meta.org.example.notes"
    assert invalid.value.hint

    with pytest.raises(MetaValidationError) as limited:
        validate_meta({"org.example.notes": _value_at_size(65_537)})
    assert limited.value.code == "meta_limit_exceeded"
    assert limited.value.path == "meta.org.example.notes"
    assert limited.value.hint


def test_meta_accepts_exact_size_and_depth_boundaries() -> None:
    validate_meta({"org.example.notes": _value_at_size(65_536)})

    whole = {f"org.example.n{index}": {"value": ""} for index in range(4)}
    fill = 262_144 - _canonical_size(whole)
    each, remainder = divmod(fill, 4)
    for index in range(4):
        whole[f"org.example.n{index}"]["value"] = "x" * (each + (index == 0) * remainder)
    assert _canonical_size(whole) == 262_144
    validate_meta(whole)

    whole["org.example.n0"]["value"] += "x"
    with pytest.raises(MetaValidationError, match="canonical UTF-8 bytes"):
        validate_meta(whole)

    value: dict[str, object] = {}
    for _ in range(31):
        value = {"nested": value}
    validate_meta({"org.example.notes": value})

    with pytest.raises(MetaValidationError, match="nested containers"):
        validate_meta({"org.example.notes": {"nested": value}})


@pytest.mark.parametrize("location", ["top", "track", "clip"])
def test_invalid_meta_is_refused_at_each_known_timeline_location(location: str) -> None:
    config = _timeline()
    if location == "top":
        config["meta"] = {"org.example.notes": None}
    elif location == "track":
        config["tracks"][0]["meta"] = {"org.example.notes": None}
    else:
        config["clips"][0]["meta"] = {"org.example.notes": None}

    with pytest.raises(MetaValidationError) as error:
        validate_timeline(config)
    assert error.value.code == "invalid_meta"


def test_meta_limits_are_refused_without_broadening_unknown_fields() -> None:
    with pytest.raises(MetaValidationError) as error:
        validate_meta({"org.example.notes": {"value": "x" * 65_537}})
    assert error.value.code == "meta_limit_exceeded"

    with pytest.raises(MetaValidationError) as error:
        validate_meta({"org.example.notes": {"value": "x" * 300_000}})
    assert error.value.code == "meta_limit_exceeded"

    config = _timeline()
    config["unexpected"] = True
    # Timeline's domain loader keeps its existing passthrough behavior; the
    # strict container validator remains the refusal boundary for unknown keys.
    with pytest.raises(ValueError, match="unknown key"):
        validate_timeline_config_for_container(config)


def test_known_timeline_meta_locations_are_admitted() -> None:
    meta = {"org.example.notes": {"id_like": "missing-is-allowed"}}
    config = {
        "meta": meta,
        "tracks": [{"id": "track", "kind": "visual", "label": "Picture", "meta": meta}],
        "clips": [{"id": "clip", "at": 0, "track": "track", "meta": meta}],
    }
    validate_timeline(config)
