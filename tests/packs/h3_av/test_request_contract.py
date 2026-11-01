from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest
import yaml

from astrid.packs.h3_av.actions.compile.compile import compile_preparation
from astrid.packs.h3_av.shared.request import H3RequestError, normalize_request


PACK = Path(__file__).resolve().parents[3] / "astrid/packs/h3_av"
SCHEMA = json.loads((PACK / "schemas/request.v1.json").read_text())


def _request(operation: str = "continue", **extra: object) -> dict[str, object]:
    request: dict[str, object] = {
        "version": 1,
        "operation": operation,
        "source": {"asset": "source.mp4", "range": [0, 4]},
        "output": {"duration": 8},
        "content": {"prompt": "Continue the source-backed audiovisual scene."},
        "changes": {
            "video": [{"during": [4, 8], "area": {"full_frame": True}, "action": "generate"}],
            "audio": [{"during": [4, 8], "action": "generate"}],
        },
        "references": [],
        "overrides": {},
    }
    request.update(extra)
    return request


@pytest.mark.parametrize("operation", ["edit", "continue"])
def test_source_backed_operations_are_normalized(operation: str) -> None:
    jsonschema.validate(_request(operation=operation), SCHEMA)
    request = normalize_request(_request(operation=operation))
    assert request.value["operation"] == operation
    assert request.value["source"] == {"asset": "source.mp4", "range": [0.0, 4.0]}


def test_source_free_generate_is_admitted_with_a_bounded_output() -> None:
    raw = _request(operation="generate", source=None, changes={"video": [], "audio": []},
                   references=[{"asset": "image", "purpose": "appearance"}])
    jsonschema.validate(raw, SCHEMA)
    assert normalize_request(raw).value["source"] is None


def test_fixture_specific_reference_strategy_is_rejected() -> None:
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(_request(reference_strategy="chain"), SCHEMA)
    with pytest.raises(H3RequestError, match="unsupported field.*reference_strategy"):
        normalize_request(_request(reference_strategy="chain"))


@pytest.mark.parametrize("operation", ["edit", "continue"])
@pytest.mark.parametrize("missing", [False, True])
def test_source_is_required_by_schema_and_runtime(operation: str, missing: bool) -> None:
    request = _request(operation=operation, source=None)
    if missing:
        del request["source"]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(request, SCHEMA)
    with pytest.raises(H3RequestError, match=f"source is required for {operation}"):
        normalize_request(request)


def test_profile_and_schema_agree_on_operations_and_route_capacity() -> None:
    profile = yaml.safe_load((PACK / "profiles/native-v1.yaml").read_text())
    assert set(profile["operations"]) == set(SCHEMA["properties"]["operation"]["enum"]) == {"edit", "continue", "generate"}
    assert profile["reference_capacity"] == 2
    assert profile["source_free_generation"]["reference_capacity"] == 9


def test_compile_rejects_source_free_masks_before_assets(tmp_path: Path) -> None:
    with pytest.raises(H3RequestError, match="changes/masks require"):
        compile_preparation(
            {
                "kind": "h3_av_preparation",
                "status": "prepared",
                "runtime_submission": "eligible",
                "request": _request(operation="generate", source=None),
            },
            out_dir=tmp_path / "compiled",
        )
    assert not (tmp_path / "compiled").exists()
