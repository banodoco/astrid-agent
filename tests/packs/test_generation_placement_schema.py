"""Placement at the existing pack schema, loader, and validation boundaries."""
import json
from pathlib import Path

import jsonschema
import pytest
import yaml

from astrid.core.generation.backends.registry import descriptors_from_pack
from astrid.core.pack import PackValidationError, load_pack_manifest
from astrid.core.pack.permissions import _normalize_generation_backends
from astrid.core.pack.validate import validate_pack


PLACEMENT = {"schema": "astrid.generation-placement.v1", "supported": ["local", "runpod"],
             "default": "runpod", "engine": "vibecomfy-h3", "requires_endpoint": True}
SCHEMAS = Path(__file__).resolve().parents[2] / "astrid/core/pack/schemas"
INVALID = [
    None, False, "local", [], {},
    {**PLACEMENT, "extra": 1}, {**PLACEMENT, "schema": "unknown"},
    {**PLACEMENT, "supported": []}, {**PLACEMENT, "supported": ["runpod", "runpod"]},
    {**PLACEMENT, "supported": [True]}, {**PLACEMENT, "supported": "runpod"},
    {**PLACEMENT, "supported": ["remote"]}, {**PLACEMENT, "supported": ["local"]},
    {**PLACEMENT, "default": True}, {**PLACEMENT, "default": "remote"},
    {**PLACEMENT, "engine": None}, {**PLACEMENT, "engine": True},
    {**PLACEMENT, "engine": "Invalid"}, {**PLACEMENT, "engine": "a\n"},
    {**PLACEMENT, "engine": ""}, {**PLACEMENT, "requires_endpoint": 1},
    {**PLACEMENT, "requires_endpoint": "false"},
] + [{k: v for k, v in PLACEMENT.items() if k != missing} for missing in PLACEMENT]


def write_pack(tmp_path, backend):
    root = tmp_path / "probe"
    (root / "skill").mkdir(parents=True)
    (root / "skill/SKILL.md").write_text("# Probe\n")
    data = {"schema_version": 2, "id": "probe", "name": "Probe", "version": "1.0.0",
            "documentation": {"kind": "skill", "path": "skill/SKILL.md"},
            "extensions": {"generation": {"backends": [backend]}}}
    (root / "pack.yaml").write_text(yaml.safe_dump(data))
    return root


def backend(**extra):
    return {"id": "probe", "module": "never_import", "class": "Adapter", "label": "Probe",
            "init_kwargs": {"setting": "legacy"}, **extra}


@pytest.mark.parametrize("placement", [PLACEMENT,
    {**PLACEMENT, "default": "local"},
    {**PLACEMENT, "supported": ["runpod"]},
    {**PLACEMENT, "supported": ["local"], "default": "local", "engine": None, "requires_endpoint": False},
])
def test_explicit_placement_survives_actual_pack_loader_and_validator(tmp_path, placement):
    root = write_pack(tmp_path, backend(placement=placement))
    assert validate_pack(root)[0] == []
    pack = load_pack_manifest(root / "pack.yaml")
    descriptor, = descriptors_from_pack(pack)
    assert descriptor.to_dict() == backend(placement=placement)
    assert pack.to_dict()["extensions"]["generation"]["backends"][0]["placement"] == placement
    # Descriptor output can be written back to a real pack unchanged.
    payload = yaml.safe_load((root / "pack.yaml").read_text())
    payload["extensions"]["generation"]["backends"] = [descriptor.to_dict()]
    (root / "pack.yaml").write_text(yaml.safe_dump(payload))
    assert descriptors_from_pack(load_pack_manifest(root / "pack.yaml")) == (descriptor,)


def test_missing_legacy_placement_preserves_pack_shape_and_adapter_kwargs(tmp_path):
    raw = backend()
    root = write_pack(tmp_path, raw)
    assert validate_pack(root)[0] == []
    pack = load_pack_manifest(root / "pack.yaml")
    assert pack.extensions["generation"]["backends"] == [raw]
    descriptor, = descriptors_from_pack(pack)
    assert descriptor.placement.to_dict() == {"schema": "astrid.generation-placement.v1",
        "supported": ["local"], "default": "local", "engine": None, "requires_endpoint": False}
    assert descriptor.init_kwargs == {"setting": "legacy"}


@pytest.mark.parametrize("placement", INVALID)
def test_malformed_placement_fails_actual_loader_validator_and_normalizer(tmp_path, placement):
    root = write_pack(tmp_path, backend(placement=placement))
    with pytest.raises(PackValidationError, match="placement"):
        load_pack_manifest(root / "pack.yaml")
    errors, _ = validate_pack(root)
    assert errors and any("placement" in error for error in errors)
    with pytest.raises(PackValidationError, match="placement"):
        _normalize_generation_backends([backend(placement=placement)], path="extensions.generation.backends")


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("placement", INVALID)
def test_schema_itself_rejects_invalid_placement_without_python_normalizer(version, placement):
    schema = json.loads((SCHEMAS / f"v{version}/pack.json").read_text())
    node = schema["properties"]["extensions"]["properties"]["generation"]["properties"]["backends"]["items"]["properties"]["placement"]
    validator = jsonschema.validators.validator_for(schema)
    validator.check_schema(schema)
    assert list(validator(node).iter_errors(placement))


def test_unknown_backend_field_still_rejected(tmp_path):
    root = write_pack(tmp_path, backend(placement=PLACEMENT, surprise=True))
    with pytest.raises(PackValidationError, match="unknown field"):
        load_pack_manifest(root / "pack.yaml")
    assert validate_pack(root)[0]


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("placement", [PLACEMENT,
    {**PLACEMENT, "supported": ["local"], "default": "local", "engine": None, "requires_endpoint": False},
    {**PLACEMENT, "supported": ["runpod"]},
])
def test_schema_itself_accepts_supported_combinations(version, placement):
    schema = json.loads((SCHEMAS / f"v{version}/pack.json").read_text())
    node = schema["properties"]["extensions"]["properties"]["generation"]["properties"]["backends"]["items"]["properties"]["placement"]
    jsonschema.validators.validator_for(schema)(node).validate(placement)
