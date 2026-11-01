from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

import pytest

from astrid.core.execution.generic_host import GenericPackHost
from astrid.sdk.exceptions import CapabilityValidationError
from astrid.sdk.invocation import get_capability
from astrid.sdk.generation_publication import (
    CAPABILITY_ID,
    compose_image_publication_request,
)


def _actual_capability():
    capability = get_capability(CAPABILITY_ID, kind="executor", include_elements=False)
    assert isinstance(capability.definition, Mapping)
    assert any(output.name == "generated_images" for output in capability.outputs)
    return capability


def _actual_host_record():
    host = GenericPackHost(
        pack_roots=[Path(__file__).resolve().parents[2] / "astrid/packs/generation"]
    )
    host.discover()
    return host.capabilities[CAPABILITY_ID]


def _request(capability, *, count: int = 3) -> dict[str, object]:
    return {
        "project": "project-ordinary-image",
        "capability_digest": _actual_host_record().capability_digest,
        "params": {
            "model": "z-image",
            "mode": "t2i",
            "execution": "cloud",
            "prompt": "a blue ceramic vase on a wooden table",
            "count": count,
            "size": "1024x1024",
            "seed": 42,
            "negative_prompt": "blurry",
            "guidance_scale": 7.5,
            "steps": 28,
        },
    }


def test_compose_is_deterministic_and_uses_real_capability_outputs() -> None:
    capability = _actual_capability()
    host_record = _actual_host_record()
    request = _request(capability)

    first = compose_image_publication_request(request)
    second = compose_image_publication_request(request)

    assert first == second
    assert first["project"] == request["project"]
    assert first["capability_id"] == CAPABILITY_ID
    assert first["capability_digest"] == request["capability_digest"]
    assert first["capability_digest"] == host_record.capability_digest
    assert first["input_object_ids"] == []
    assert first["storage_estimate"] == {
        "scratch_bytes": 69_206_016,
        "output_bytes": 269_484_032,
    }
    intent = first["generation_intent"]
    assert intent == {
        "version": 1,
        "modality": "image",
        "partial_success_policy": "reject",
        "groups": [
            {
                "group_key": "main",
                "selectors": [
                    {"selector": "main-0", "ordinal": 0, "variant_key": "original"},
                    {"selector": "main-1", "ordinal": 1, "variant_key": "variant-1"},
                    {"selector": "main-2", "ordinal": 2, "variant_key": "variant-2"},
                ],
            }
        ],
    }
    effect = first["settlement_effect"]
    assert effect["effect_type"] == "generation.publish_v1"
    assert effect["target_id"] == request["project"]
    assert effect["payload"]["groups"][0]["selectors"] == [
        {
            "selector": "main-0",
            "ordinal": 0,
            "variant_key": "original",
            "output_port": "generated_images",
        },
        {
            "selector": "main-1",
            "ordinal": 1,
            "variant_key": "variant-1",
            "output_port": "generated_images",
        },
        {
            "selector": "main-2",
            "ordinal": 2,
            "variant_key": "variant-2",
            "output_port": "generated_images",
        },
    ]
    assert "image_manifest" not in json.dumps(effect)


def test_compose_digest_matches_generic_pack_host_registration() -> None:
    capability = _actual_capability()
    host_record = _actual_host_record()
    composed = compose_image_publication_request(_request(capability, count=1))

    assert composed["capability_digest"] == host_record.capability_digest


@pytest.mark.parametrize(
    "mutator, match",
    [
        (lambda request: request.update({"settlement_effect": {}}), "unsupported field"),
        (
            lambda request: request["params"].update({"generation_intent": {}}),
            "unsupported parameter",
        ),
        (
            lambda request: request["params"].update({"output_selector": "main-0"}),
            "unsupported parameter",
        ),
        (
            lambda request: request["params"].update({"count": 5}),
            "count must be between",
        ),
        (
            lambda request: request["params"].update({"size": "4096x4096"}),
            "dimensions must be at most",
        ),
    ],
)
def test_compose_rejects_effect_or_unbounded_route_fields(mutator, match) -> None:
    capability = _actual_capability()
    request = _request(capability, count=1)
    mutator(request)
    with pytest.raises(CapabilityValidationError, match=match):
        compose_image_publication_request(request)


def test_compose_rejects_digest_mismatch_before_building_admission_document() -> None:
    capability = _actual_capability()
    request = _request(capability, count=1)
    request["capability_digest"] = "sha256:" + "0" * 64
    with pytest.raises(CapabilityValidationError, match="capability_digest"):
        compose_image_publication_request(request)


def test_fixed_host_adapter_serializes_one_complete_request() -> None:
    capability = _actual_capability()
    request = _request(capability, count=2)
    result = subprocess.run(
        [sys.executable, "-m", "astrid.sdk.generation_publication", "--json"],
        input=json.dumps(request),
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    response = json.loads(result.stdout)
    assert response["ok"] is True
    serialized = response["request"]
    assert serialized["capability_id"] == CAPABILITY_ID
    assert serialized["spec"]["family"] == CAPABILITY_ID
    assert serialized["spec"]["params"]["count"] == 2
    assert serialized["settlement_effect"]["payload"]["groups"][0]["selectors"][1][
        "output_port"
    ] == "generated_images"
    assert result.stderr == ""
