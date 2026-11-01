"""Tests for audio modality/mode/price validation in the model registry."""

from __future__ import annotations

import pytest

from astrid.core.model_catalog.registry import ModelRegistry
from astrid.core.model_catalog.schema import (
    AUDIO_MODALITY,
    Price,
    validate_registry,
)
from astrid.core.model_catalog.taxonomy import (
    AUDIO_FEATURES,
    CANONICAL_AUDIO_MODES,
    GenerationTaxonomyRegistry,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_audio_payload(
    model_id: str = "test-audio",
    mode: str = "music",
    supports: list[str] | None = None,
    requires: list[str] | None = None,
    price: dict[str, object] | None = None,
) -> dict:
    """Return a minimal schema_version:2 payload with one audio model."""
    if supports is None:
        supports = ["prompt"]
    if requires is None:
        requires = ["prompt"]
    backend: dict = {
        "endpoint": "fal-ai/test/audio",
        "param_map": {feature: feature for feature in supports},
    }
    if price is not None:
        backend["price"] = price
    return {
        "schema_version": 2,
        "models": [
            {
                "id": model_id,
                "modality": "audio",
                "modes": {
                    mode: {
                        "supports": supports,
                        "requires": requires,
                        "backends": {"cloud": backend},
                    }
                },
            }
        ],
    }


# ---------------------------------------------------------------------------
# Taxonomy constants
# ---------------------------------------------------------------------------


class TestAudioTaxonomyConstants:
    def test_audio_modality_constant(self) -> None:
        assert AUDIO_MODALITY == "audio"

    def test_canonical_audio_modes(self) -> None:
        assert set(CANONICAL_AUDIO_MODES) == {"tts", "music", "sfx"}

    def test_audio_features_include_music_features(self) -> None:
        expected = {
            "prompt",
            "negative_prompt",
            "seed",
            "count",
            "duration",
            "guidance_scale",
            "steps",
            "lyrics_prompt",
            "instrumental",
            "output_format",
        }
        assert set(AUDIO_FEATURES) == expected


# ---------------------------------------------------------------------------
# Taxonomy registry
# ---------------------------------------------------------------------------


class TestAudioTaxonomyRegistry:
    def test_audio_modality_accepted(self) -> None:
        registry = GenerationTaxonomyRegistry()
        # Should not raise
        registry.require_mode("audio", "music", path="test")

    def test_audio_features_recognised(self) -> None:
        registry = GenerationTaxonomyRegistry()
        for feature in AUDIO_FEATURES:
            assert registry.require_feature(feature, path="test") == feature

    def test_audio_modes_recognised(self) -> None:
        registry = GenerationTaxonomyRegistry()
        mode_ids = registry.mode_ids("audio")
        for mode in CANONICAL_AUDIO_MODES:
            assert mode in mode_ids


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------


class TestAudioSchemaValidation:
    def test_audio_modality_validates(self) -> None:
        raw = _make_audio_payload()
        entries = validate_registry(raw)
        assert len(entries) == 1
        assert entries[0].modality == "audio"

    def test_music_mode_validates(self) -> None:
        raw = _make_audio_payload(mode="music")
        entries = validate_registry(raw)
        assert "music" in entries[0].modes

    def test_tts_mode_validates(self) -> None:
        raw = _make_audio_payload(mode="tts")
        entries = validate_registry(raw)
        assert "tts" in entries[0].modes

    def test_sfx_mode_validates(self) -> None:
        raw = _make_audio_payload(mode="sfx")
        entries = validate_registry(raw)
        assert "sfx" in entries[0].modes

    def test_audio_features_in_supports_validate(self) -> None:
        raw = _make_audio_payload(
            supports=[
                "prompt",
                "negative_prompt",
                "seed",
                "count",
                "duration",
                "guidance_scale",
                "steps",
                "lyrics_prompt",
                "instrumental",
                "output_format",
            ],
            requires=["prompt"],
        )
        entries = validate_registry(raw)
        mode_spec = entries[0].modes["music"]
        assert "lyrics_prompt" in mode_spec.supports
        assert "instrumental" in mode_spec.supports

    def test_audio_price_unit_validates(self) -> None:
        raw = _make_audio_payload(price={"unit": "audio", "usd": 0.15})
        entries = validate_registry(raw)
        price = entries[0].modes["music"].backends["cloud"].price
        assert price == Price(usd=0.15, unit="audio")

    def test_second_price_unit_validates(self) -> None:
        raw = _make_audio_payload(price={"unit": "second", "usd": 0.0002})
        entries = validate_registry(raw)
        price = entries[0].modes["music"].backends["cloud"].price
        assert price == Price(usd=0.0002, unit="second")

    def test_unknown_price_unit_rejected(self) -> None:
        raw = _make_audio_payload(price={"unit": "byte", "usd": 0.0001})
        with pytest.raises(ValueError, match="unsupported price unit"):
            validate_registry(raw)


# ---------------------------------------------------------------------------
# Shipped registry
# ---------------------------------------------------------------------------


class TestShippedAudioRegistry:
    def test_shipped_registry_loads_audio_models(self) -> None:
        registry = ModelRegistry.load_default()
        audio_models = registry.list_by_modality("audio")
        ids = {m.id for m in audio_models}
        assert ids == {"minimax-music-v2.6", "minimax-music-3", "minimax-music-3.0", "stable-audio-3-medium", "ace-step"}

    def test_minimax_music_price(self) -> None:
        registry = ModelRegistry.load_default()
        _, mode_spec = registry.get_by_mode("minimax-music-v2.6", "music")
        price = mode_spec.backends["cloud"].price
        assert price == Price(usd=0.15, unit="audio")
        assert "lyrics_prompt" in mode_spec.supports
        assert "instrumental" in mode_spec.supports

    def test_stable_audio_3_price(self) -> None:
        registry = ModelRegistry.load_default()
        _, mode_spec = registry.get_by_mode("stable-audio-3-medium", "music")
        price = mode_spec.backends["cloud"].price
        assert price == Price(usd=0.0479, unit="audio")
        assert "negative_prompt" in mode_spec.supports
        assert "output_format" in mode_spec.supports

    def test_ace_step_price(self) -> None:
        registry = ModelRegistry.load_default()
        _, mode_spec = registry.get_by_mode("ace-step", "music")
        price = mode_spec.backends["cloud"].price
        assert price == Price(usd=0.0002, unit="second")
        assert "instrumental" in mode_spec.supports
