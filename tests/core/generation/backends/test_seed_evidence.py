"""Offline provider-boundary tests for actual, output-bound seed evidence."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from astrid.core.generation.backends.base import GenerationResult
from astrid.core.generation.backends.fal import FalBackend
from astrid.core.generation.backends.wavespeed import WavespeedBackend
from astrid.core.model_catalog.schema import BackendSpec, ModelEntry, ModeSpec
from astrid.core.util.http import HttpClient


_PNG = b"\x89PNG\r\n\x1a\n"
_IMAGE = {"url": "https://cdn.example/output.png"}


def _entry(
    endpoint: str = "fal-ai/flux/dev",
    *,
    mode: str = "t2i",
    modality: str = "image",
    backend: str = "cloud",
    lora_endpoint: str | None = None,
) -> ModelEntry:
    # Same minimal model/provider fake pattern as test_fal_cost.py.
    return ModelEntry(
        id="seed-evidence-fixture",
        modality=modality,
        modes={
            mode: ModeSpec(
                supports=("prompt", "seed", "loras"),
                requires=("prompt",),
                backends={
                    backend: BackendSpec(
                        endpoint=endpoint,
                        lora_endpoint=lora_endpoint,
                        param_map={"prompt": "prompt", "seed": "seed"},
                    ),
                },
            ),
        },
    )


def _fal_generate(
    tmp_path: Path,
    response: dict,
    *,
    entry: ModelEntry | None = None,
    mode: str = "t2i",
    params: dict | None = None,
) -> GenerationResult:
    client = HttpClient()
    backend = FalBackend(client=client)
    with (
        patch.object(FalBackend, "_resolve_api_key", return_value="fixture-key"),
        patch(
            "astrid.core.generation.backends.fal.fal_submit_and_poll",
            return_value=response,
        ) as submit,
        patch.object(client, "get_bytes", return_value=_PNG),
    ):
        result = backend.generate(
            entry or _entry(),
            mode,
            params or {"prompt": "offline seed evidence", "seed": 9},
            tmp_path / "out",
        )
    # Keep request seed provenance visible, separate from provider output seed.
    assert submit.call_args.args[2]["seed"] == 9
    return result


@pytest.mark.parametrize(
    ("endpoint", "mode"),
    [
        ("fal-ai/flux/dev", "t2i"),
        ("fal-ai/flux/schnell", "t2i"),
        ("fal-ai/flux/dev/image-to-image", "i2i"),
        ("fal-ai/z-image/turbo", "t2i"),
        ("fal-ai/z-image/turbo/image-to-image", "i2i"),
    ],
)
@pytest.mark.parametrize("actual_seed", [0, 708])
def test_fal_documented_single_image_response_binds_actual_seed(
    tmp_path: Path, endpoint: str, mode: str, actual_seed: int,
) -> None:
    result = _fal_generate(
        tmp_path,
        {"images": [_IMAGE], "seed": actual_seed},
        entry=_entry(endpoint, mode=mode),
        mode=mode,
    )

    assert result.seed_used == actual_seed
    assert result.output_seeds == {result.image_paths[0]: actual_seed}
    assert result.image_paths[0].read_bytes() == _PNG


@pytest.mark.parametrize(
    "seed_fields",
    [{}, {"seed": None}, {"seed": True}, {"seed": False}, {"seed": "9"},
     {"seed": 9.0}, {"seed": -1}],
)
def test_fal_unattested_or_invalid_response_seed_stays_unknown(
    tmp_path: Path, seed_fields: dict,
) -> None:
    result = _fal_generate(tmp_path, {"images": [_IMAGE], **seed_fields})

    assert result.seed_used is None
    assert result.output_seeds == {}


def test_fal_scalar_seed_does_not_guess_seeds_for_multiple_images(tmp_path: Path) -> None:
    result = _fal_generate(
        tmp_path,
        {"images": [_IMAGE, {"url": "https://cdn.example/other.png"}], "seed": 708},
    )

    assert len(result.image_paths) == 2
    assert result.seed_used is None
    assert result.output_seeds == {}


@pytest.mark.parametrize(
    "assets",
    [
        {"image": _IMAGE},
        {"images": [_IMAGE["url"]]},
        {"video": {"url": "https://cdn.example/output.mp4"}},
        {"audio": {"url": "https://cdn.example/output.mp3"}},
    ],
)
def test_fal_seed_requires_documented_image_response_shape(
    tmp_path: Path, assets: dict,
) -> None:
    result = _fal_generate(tmp_path, {**assets, "seed": 708})

    assert result.seed_used is None
    assert result.output_seeds == {}


def test_fal_unverified_endpoint_seed_stays_unknown(tmp_path: Path) -> None:
    result = _fal_generate(
        tmp_path,
        {"images": [_IMAGE], "seed": 708},
        entry=_entry("fal-ai/unverified-image-model"),
    )

    assert result.seed_used is None
    assert result.output_seeds == {}


def test_fal_routed_lora_endpoint_needs_its_own_seed_contract(tmp_path: Path) -> None:
    result = _fal_generate(
        tmp_path,
        {"images": [_IMAGE], "seed": 708},
        entry=_entry(lora_endpoint="fal-ai/unverified-lora-model"),
        params={"prompt": "offline", "seed": 9, "loras": [{"path": "fixture.safetensors"}]},
    )

    assert result.model_actual == "fal-ai/unverified-lora-model"
    assert result.seed_used is None
    assert result.output_seeds == {}


@pytest.mark.parametrize(("mode", "modality"), [("t2v", "video"), ("music", "audio")])
def test_fal_image_contract_does_not_attest_other_modalities(
    tmp_path: Path, mode: str, modality: str,
) -> None:
    result = _fal_generate(
        tmp_path,
        {"images": [_IMAGE], "seed": 708},
        entry=_entry(mode=mode, modality=modality),
        mode=mode,
    )

    assert result.seed_used is None
    assert result.output_seeds == {}


def test_wavespeed_request_seed_and_unverified_response_seed_stay_unknown(
    tmp_path: Path,
) -> None:
    client = HttpClient()
    backend = WavespeedBackend(client=client)
    with (
        patch.object(WavespeedBackend, "_resolve_api_key", return_value="fixture-key"),
        patch(
            "astrid.core.generation.backends.wavespeed.wavespeed_submit_and_poll",
            return_value={"outputs": [_IMAGE["url"]], "seed": 708},
        ),
        patch.object(client, "get_bytes", return_value=_PNG),
    ):
        result = backend.generate(
            _entry("unverified/music", mode="music", modality="audio", backend="wavespeed"),
            "music",
            {"prompt": "offline", "seed": 9},
            tmp_path / "out",
        )

    assert len(result.image_paths) == 1
    assert result.seed_used is None
    assert result.output_seeds == {}


def test_generation_result_defaults_do_not_claim_seed_zero() -> None:
    result = GenerationResult()

    assert result.seed_used is None
    assert result.output_seeds == {}
    assert GenerationResult.from_dict({}).seed_used is None


def test_generation_result_output_seed_evidence_json_roundtrip(tmp_path: Path) -> None:
    first, second = tmp_path / "one.png", tmp_path / "two.png"
    original = GenerationResult(
        image_paths=[first, second],
        output_seeds={first: 0, second: 708},
    )

    payload = json.loads(json.dumps(original.to_dict()))
    assert payload["output_seeds"] == {str(first): 0, str(second): 708}
    restored = GenerationResult.from_dict(payload)
    assert restored.output_seeds == {first: 0, second: 708}
    assert restored.seed_used is None
    assert restored.to_dict() == original.to_dict()


def test_legacy_scalar_seed_does_not_invent_output_seed_association(tmp_path: Path) -> None:
    result = GenerationResult.from_dict({
        "image_paths": [str(tmp_path / "one.png"), str(tmp_path / "two.png")],
        "seed_used": 9,
    })

    assert result.seed_used == 9
    assert result.output_seeds == {}
