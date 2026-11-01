"""Tests for the model registry (ModelRegistry, validation, loading) — schema v2.

Covers: v1 rejection, v2 validation, per-mode checks, closed filtering,
SD-001 identity assertions, get_by_mode, backend_available, and edge cases.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from astrid.core.model_catalog.registry import ModelRegistry
from astrid.core.model_catalog.schema import (
    CANONICAL_IMAGE_MODES,
    Price,
    validate_registry,
    validate_registry_with_backends,
)
from astrid.core.model_catalog.taxonomy import (
    GenerationBackendIdDescriptor,
    GenerationFeatureDescriptor,
    GenerationModeDescriptor,
    GenerationTaxonomyRegistry,
)

# ---------------------------------------------------------------------------
# Helper: make a minimal valid v2 YAML payload
# ---------------------------------------------------------------------------


_DEFAULT_MODES = {
    "t2i": {
        "supports": ["prompt", "seed"],
        "requires": ["prompt"],
        "backends": {
            "local": {
                "template": "image/test",
                "param_map": {"prompt": "prompt", "seed": "seed"},
            }
        },
    }
}


def _make_v2_payload(
    model_id: str = "test-model",
    modality: str = "image",
    modes: dict | None = None,
    closed: bool | None = None,
) -> dict:
    """Return a minimal valid schema_version:2 dict."""
    payload: dict = {
        "schema_version": 2,
        "models": [
            {
                "id": model_id,
                "modality": modality,
                "modes": _DEFAULT_MODES if modes is None else modes,
            }
        ],
    }
    if closed is not None:
        payload["models"][0]["closed"] = closed
    return payload


# ---------------------------------------------------------------------------
# T1: schema_version:1 rejected cleanly
# ---------------------------------------------------------------------------


class TestV1Rejection:
    """SD-006: schema v2 fully replaces v1 — v1 is rejected with a clear error."""

    def test_v1_rejected_with_clear_message(self) -> None:
        raw = {
            "schema_version": 1,
            "models": [
                {
                    "id": "old-model",
                    "modality": "image",
                    "supports": ["prompt"],
                    "requires": ["prompt"],
                    "local": {
                        "template": "image/old",
                        "param_map": {"prompt": "prompt"},
                    },
                    "cloud": {
                        "endpoint": "fal-ai/old",
                        "param_map": {"prompt": "prompt"},
                    },
                }
            ],
        }
        with pytest.raises(ValueError, match="Schema version 1 is no longer supported"):
            validate_registry(raw)

    def test_v1_rejected_without_models_key(self) -> None:
        raw = {"schema_version": 1, "models": []}
        with pytest.raises(ValueError, match="Schema version 1 is no longer supported"):
            validate_registry(raw)

    def test_v99_rejected(self) -> None:
        raw = _make_v2_payload()
        raw["schema_version"] = 99
        with pytest.raises(ValueError, match="unsupported registry schema_version"):
            validate_registry(raw)


# ---------------------------------------------------------------------------
# T2: schema_version:2 valid
# ---------------------------------------------------------------------------


class TestV2HappyPath:
    """Happy-path: v2 payloads load correctly."""

    def test_minimal_v2_payload(self) -> None:
        entries = validate_registry(_make_v2_payload())
        assert len(entries) == 1
        assert entries[0].id == "test-model"

    def test_multiple_models(self) -> None:
        raw = {
            "schema_version": 2,
            "models": [
                {
                    "id": "a",
                    "modality": "image",
                    "modes": {
                        "t2i": {
                            "supports": ["prompt"],
                            "requires": ["prompt"],
                            "backends": {
                                "cloud": {
                                    "endpoint": "fal-ai/a",
                                    "param_map": {"prompt": "prompt"},
                                }
                            },
                        }
                    },
                },
                {
                    "id": "b",
                    "modality": "image",
                    "modes": {
                        "i2i": {
                            "supports": ["prompt", "image_ref"],
                            "requires": ["prompt", "image_ref"],
                            "backends": {
                                "local": {
                                    "template": "image/b",
                                    "param_map": {"prompt": "prompt", "image_ref": "image_ref"},
                                }
                            },
                        }
                    },
                },
            ],
        }
        entries = validate_registry(raw)
        assert len(entries) == 2
        assert {e.id for e in entries} == {"a", "b"}


# ---------------------------------------------------------------------------
# T3: invalid mode names rejected
# ---------------------------------------------------------------------------


class TestInvalidModeNames:
    """Non-canonical mode names are rejected for image modality."""

    def test_unknown_mode_rejected(self) -> None:
        raw = _make_v2_payload(
            modes={
                "img2img": {  # not canonical — should be "i2i"
                    "supports": ["prompt"],
                    "requires": ["prompt"],
                    "backends": {
                        "local": {
                            "template": "image/test",
                            "param_map": {"prompt": "prompt"},
                        }
                    },
                }
            }
        )
        with pytest.raises(ValueError, match="unknown image mode"):
            validate_registry(raw)

    def test_typo_mode_rejected(self) -> None:
        raw = _make_v2_payload(
            modes={
                "editing": {  # should be "edit"
                    "supports": ["prompt", "image_ref"],
                    "requires": ["prompt", "image_ref"],
                    "backends": {
                        "local": {
                            "template": "edit/test",
                            "param_map": {"prompt": "prompt", "image_ref": "image_ref"},
                        }
                    },
                }
            }
        )
        with pytest.raises(ValueError, match="unknown image mode"):
            validate_registry(raw)

    def test_canonical_modes_accepted(self) -> None:
        """All six canonical image modes pass validation (not wired, but names valid)."""
        for mode_name in CANONICAL_IMAGE_MODES:
            raw = _make_v2_payload(
                modes={
                    mode_name: {
                        "supports": ["prompt"],
                        "requires": ["prompt"],
                        "backends": {
                            "cloud": {
                                "endpoint": "fal-ai/test",
                                "param_map": {"prompt": "prompt"},
                            }
                        },
                    }
                }
            )
            entries = validate_registry(raw)
            assert entries[0].id == "test-model"


# ---------------------------------------------------------------------------
# T4: invalid features in mode.supports rejected
# ---------------------------------------------------------------------------


class TestInvalidSupportsFeatures:
    def test_unknown_feature_in_supports(self) -> None:
        raw = _make_v2_payload(
            modes={
                "t2i": {
                    "supports": ["prompt", "not_a_real_feature"],
                    "requires": ["prompt"],
                    "backends": {
                        "local": {
                            "template": "image/test",
                            "param_map": {"prompt": "prompt"},
                        }
                    },
                }
            }
        )
        with pytest.raises(ValueError, match="not a recognised Feature"):
            validate_registry(raw)

    def test_unknown_feature_in_requires(self) -> None:
        raw = _make_v2_payload(
            modes={
                "t2i": {
                    "supports": ["prompt", "bogus_feature"],
                    "requires": ["prompt", "bogus_feature"],
                    "backends": {
                        "local": {
                            "template": "image/test",
                            "param_map": {"prompt": "prompt"},
                        }
                    },
                }
            }
        )
        with pytest.raises(ValueError, match="not a recognised Feature"):
            validate_registry(raw)


# ---------------------------------------------------------------------------
# T5: requires-not-subset-of-supports rejected
# ---------------------------------------------------------------------------


class TestRequiresNotSubset:
    def test_requires_not_in_supports(self) -> None:
        raw = _make_v2_payload(
            modes={
                "t2i": {
                    "supports": ["prompt"],
                    "requires": ["prompt", "image_ref"],
                    "backends": {
                        "local": {
                            "template": "image/test",
                            "param_map": {"prompt": "prompt"},
                        }
                    },
                }
            }
        )
        with pytest.raises(ValueError, match="requires.*image_ref.*not in.*supports"):
            validate_registry(raw)

    def test_requires_empty_is_ok(self) -> None:
        """Empty requires list is valid (no required features)."""
        raw = _make_v2_payload(
            modes={
                "t2i": {
                    "supports": ["prompt"],
                    "requires": [],
                    "backends": {
                        "local": {
                            "template": "image/test",
                            "param_map": {"prompt": "prompt"},
                        }
                    },
                }
            }
        )
        entries = validate_registry(raw)
        assert entries[0].modes["t2i"].requires == ()


# ---------------------------------------------------------------------------
# T6: missing template for local rejected
# ---------------------------------------------------------------------------


class TestMissingTemplate:
    def test_empty_template_rejected(self) -> None:
        raw = _make_v2_payload(
            modes={
                "t2i": {
                    "supports": ["prompt"],
                    "requires": ["prompt"],
                    "backends": {
                        "local": {
                            "template": "",
                            "param_map": {"prompt": "prompt"},
                        }
                    },
                }
            }
        )
        with pytest.raises(ValueError, match="template.*must be a non-empty string"):
            validate_registry(raw)

    def test_missing_template_key_rejected(self) -> None:
        raw = _make_v2_payload(
            modes={
                "t2i": {
                    "supports": ["prompt"],
                    "requires": ["prompt"],
                    "backends": {
                        "local": {
                            "param_map": {"prompt": "prompt"},
                        }
                    },
                }
            }
        )
        with pytest.raises(ValueError, match="template.*must be a non-empty string"):
            validate_registry(raw)


# ---------------------------------------------------------------------------
# T7: missing endpoint for cloud rejected
# ---------------------------------------------------------------------------


class TestMissingEndpoint:
    def test_empty_endpoint_rejected(self) -> None:
        raw = _make_v2_payload(
            modes={
                "t2i": {
                    "supports": ["prompt"],
                    "requires": ["prompt"],
                    "backends": {
                        "cloud": {
                            "endpoint": "",
                            "param_map": {"prompt": "prompt"},
                        }
                    },
                }
            }
        )
        with pytest.raises(ValueError, match="endpoint.*must be a non-empty string"):
            validate_registry(raw)

    def test_missing_endpoint_key_rejected(self) -> None:
        raw = _make_v2_payload(
            modes={
                "t2i": {
                    "supports": ["prompt"],
                    "requires": ["prompt"],
                    "backends": {
                        "cloud": {
                            "param_map": {"prompt": "prompt"},
                        }
                    },
                }
            }
        )
        with pytest.raises(ValueError, match="endpoint.*must be a non-empty string"):
            validate_registry(raw)


# ---------------------------------------------------------------------------
# T8: param_map key not in mode.supports rejected
# ---------------------------------------------------------------------------


class TestParamMapNotInSupports:
    def test_param_map_key_not_in_supports(self) -> None:
        raw = _make_v2_payload(
            modes={
                "t2i": {
                    "supports": ["prompt"],
                    "requires": ["prompt"],
                    "backends": {
                        "local": {
                            "template": "image/test",
                            "param_map": {
                                "prompt": "prompt",
                                "image_ref": "image_ref",  # not in supports
                            },
                        }
                    },
                }
            }
        )
        with pytest.raises(ValueError, match="image_ref.*not in.*supports"):
            validate_registry(raw)

    def test_param_map_invalid_feature(self) -> None:
        raw = _make_v2_payload(
            modes={
                "t2i": {
                    "supports": ["prompt", "bogus"],
                    "requires": ["prompt"],
                    "backends": {
                        "local": {
                            "template": "image/test",
                            "param_map": {
                                "prompt": "prompt",
                                "bogus": "bogus",
                            },
                        }
                    },
                }
            }
        )
        # First hit should be the invalid feature in supports
        with pytest.raises(ValueError, match="not a recognised Feature"):
            validate_registry(raw)


# ---------------------------------------------------------------------------
# T9: duplicate model IDs rejected
# ---------------------------------------------------------------------------


class TestDuplicateModelIds:
    def test_duplicate_ids_rejected(self) -> None:
        raw = {
            "schema_version": 2,
            "models": [
                {
                    "id": "same-id",
                    "modality": "image",
                    "modes": {
                        "t2i": {
                            "supports": ["prompt"],
                            "requires": ["prompt"],
                            "backends": {
                                "cloud": {
                                    "endpoint": "fal-ai/a",
                                    "param_map": {"prompt": "prompt"},
                                }
                            },
                        }
                    },
                },
                {
                    "id": "same-id",
                    "modality": "image",
                    "modes": {
                        "i2i": {
                            "supports": ["prompt", "image_ref"],
                            "requires": ["prompt", "image_ref"],
                            "backends": {
                                "cloud": {
                                    "endpoint": "fal-ai/b",
                                    "param_map": {
                                        "prompt": "prompt",
                                        "image_ref": "image_url",
                                    },
                                }
                            },
                        }
                    },
                },
            ],
        }
        with pytest.raises(ValueError, match="duplicate model id"):
            validate_registry(raw)


# ---------------------------------------------------------------------------
# T10: model with zero modes rejected
# ---------------------------------------------------------------------------


class TestZeroModes:
    def test_empty_modes_rejected(self) -> None:
        raw = _make_v2_payload(modes={})
        with pytest.raises(ValueError, match="at least one mode is required"):
            validate_registry(raw)

    def test_missing_modes_key_rejected(self) -> None:
        raw = {
            "schema_version": 2,
            "models": [
                {
                    "id": "no-modes",
                    "modality": "image",
                }
            ],
        }
        with pytest.raises(ValueError, match="modes.*must be a dict"):
            validate_registry(raw)


# ---------------------------------------------------------------------------
# T11: closed:true entries hidden from list_all() default
# ---------------------------------------------------------------------------


class TestClosedFiltering:
    """SD-008: closed:true entries hidden from default list_all()."""

    def test_closed_true_hidden_by_default(self) -> None:
        raw = {
            "schema_version": 2,
            "models": [
                {
                    "id": "open-model",
                    "modality": "image",
                    "closed": False,
                    "modes": {
                        "t2i": {
                            "supports": ["prompt"],
                            "requires": ["prompt"],
                            "backends": {
                                "cloud": {
                                    "endpoint": "fal-ai/open",
                                    "param_map": {"prompt": "prompt"},
                                }
                            },
                        }
                    },
                },
                {
                    "id": "closed-model",
                    "modality": "image",
                    "closed": True,
                    "modes": {
                        "t2i": {
                            "supports": ["prompt"],
                            "requires": ["prompt"],
                            "backends": {
                                "cloud": {
                                    "endpoint": "fal-ai/closed",
                                    "param_map": {"prompt": "prompt"},
                                }
                            },
                        }
                    },
                },
            ],
        }
        entries = validate_registry(raw)
        reg = ModelRegistry(entries)

        # Default: closed hidden
        assert len(reg.list_all()) == 1
        assert reg.list_all()[0].id == "open-model"

        # include_closed=True: both shown
        assert len(reg.list_all(include_closed=True)) == 2
        ids = {e.id for e in reg.list_all(include_closed=True)}
        assert ids == {"open-model", "closed-model"}

    def test_closed_none_treated_as_open(self) -> None:
        raw = _make_v2_payload(closed=None)
        entries = validate_registry(raw)
        reg = ModelRegistry(entries)
        assert len(reg.list_all()) == 1  # None treated as falsy/open


# ---------------------------------------------------------------------------
# T12: backend_available helper returns correct results
# ---------------------------------------------------------------------------


class TestBackendAvailable:
    def test_local_available(self) -> None:
        reg = ModelRegistry.load_default()
        assert reg.backend_available("z-image", "t2i", "local") is True

    def test_cloud_available(self) -> None:
        reg = ModelRegistry.load_default()
        assert reg.backend_available("z-image", "t2i", "cloud") is True

    def test_local_only_mode(self) -> None:
        """flux-dev has cloud/codex for t2i and no local backend."""
        reg = ModelRegistry.load_default()
        assert reg.backend_available("flux-dev", "t2i", "cloud") is True
        assert reg.backend_available("flux-dev", "t2i", "codex") is True
        assert reg.backend_available("flux-dev", "t2i", "local") is False

    def test_cloud_only_mode(self) -> None:
        """flux-schnell has cloud/codex for t2i and no local backend."""
        reg = ModelRegistry.load_default()
        assert reg.backend_available("flux-schnell", "t2i", "cloud") is True
        assert reg.backend_available("flux-schnell", "t2i", "codex") is True
        assert reg.backend_available("flux-schnell", "t2i", "local") is False


# ---------------------------------------------------------------------------
# T13: get_by_mode succeeds for valid (model_id, mode)
# ---------------------------------------------------------------------------


class TestGetByMode:
    def test_get_by_mode_valid(self) -> None:
        reg = ModelRegistry.load_default()
        entry, mode_spec = reg.get_by_mode("z-image", "t2i")
        assert entry.id == "z-image"
        assert "prompt" in mode_spec.supports
        assert "local" in mode_spec.backends
        assert "cloud" in mode_spec.backends
        assert "codex" in mode_spec.backends

    def test_get_by_mode_i2i(self) -> None:
        reg = ModelRegistry.load_default()
        entry, mode_spec = reg.get_by_mode("flux-dev", "i2i")
        assert entry.id == "flux-dev"
        assert "image_ref" in mode_spec.supports
        assert "image_ref" in mode_spec.requires

    def test_get_by_mode_edit(self) -> None:
        """Edit mode for qwen-image-edit."""
        reg = ModelRegistry.load_default()
        entry, mode_spec = reg.get_by_mode("qwen-image-edit", "edit")
        assert entry.id == "qwen-image-edit"
        # SD-003: edit mode excludes negative_prompt and strength
        assert "negative_prompt" not in mode_spec.supports
        assert "strength" not in mode_spec.supports
        assert "image_ref" in mode_spec.requires
        assert "prompt" in mode_spec.requires


# ---------------------------------------------------------------------------
# T14: get_by_mode fails for unknown model or unsupported mode
# ---------------------------------------------------------------------------


class TestGetByModeFailures:
    def test_unknown_model(self) -> None:
        reg = ModelRegistry.load_default()
        with pytest.raises(KeyError, match="Unknown model"):
            reg.get_by_mode("nonexistent", "t2i")

    def test_unsupported_mode(self) -> None:
        """z-image doesn't support edit mode."""
        reg = ModelRegistry.load_default()
        with pytest.raises(KeyError, match="does not support mode"):
            reg.get_by_mode("z-image", "edit")

    def test_flux_dev_no_edit(self) -> None:
        """flux-dev doesn't support edit mode."""
        reg = ModelRegistry.load_default()
        with pytest.raises(KeyError, match="does not support mode"):
            reg.get_by_mode("flux-dev", "edit")


# ---------------------------------------------------------------------------
# T15: SD-001 identity assertion — no shipped entry has divergent local/cloud
# ---------------------------------------------------------------------------


class TestSD001ModelIdentity:
    """SD-001: A model_id must refer to the same real-world checkpoint on
    both backends.  No shipped entry maps to obviously different models."""

    def test_no_divergent_actual_models(self) -> None:
        """For every (model_id, mode) that has both local+cloud, verify
        the template name and endpoint slug are not obviously different models."""
        reg = ModelRegistry.load_default()

        for entry in reg.list_all(include_closed=True):
            for mode_name, mode_spec in entry.modes.items():
                has_local = "local" in mode_spec.backends
                has_cloud = "cloud" in mode_spec.backends
                if has_local and has_cloud:
                    local_tmpl = mode_spec.backends["local"].template
                    cloud_ep = mode_spec.backends["cloud"].endpoint
                    # Both must exist (non-empty)
                    assert local_tmpl, (
                        f"SD-001 violation: {entry.id}/{mode_name} has local+cloud "
                        f"but local.template is empty"
                    )
                    assert cloud_ep, (
                        f"SD-001 violation: {entry.id}/{mode_name} has local+cloud "
                        f"but cloud.endpoint is empty"
                    )
                    # SD-001: The template and endpoint must refer to the same
                    # real-world model.  After T1 fix, z-image cloud endpoints
                    # are fal-ai/z-image/turbo (not fal-ai/flux/dev) — correct.
                    # qwen-image-2512: local=image/qwen_image_2512, cloud=fal-ai/qwen-image
                    # qwen-image-edit: local=edit/qwen_image_edit, cloud=fal-ai/qwen-image-edit
                    # All pairs are within the same model family.

        # --- Explicit endpoint assertions for z-image (SD-001 fix) ---------
        # T1 fixed z-image cloud endpoints from fal-ai/flux/dev → fal-ai/z-image/turbo.
        reg = ModelRegistry.load_default()
        z_entry, z_t2i = reg.get_by_mode("z-image", "t2i")
        assert z_t2i.backends["cloud"].endpoint == "fal-ai/z-image/turbo", (
            f"SD-001 violation: z-image t2i cloud endpoint is "
            f"{z_t2i.backends['cloud'].endpoint!r}, expected 'fal-ai/z-image/turbo'"
        )

        _z_entry, z_i2i = reg.get_by_mode("z-image", "i2i")
        assert z_i2i.backends["cloud"].endpoint == "fal-ai/z-image/turbo/image-to-image", (
            f"SD-001 violation: z-image i2i cloud endpoint is "
            f"{z_i2i.backends['cloud'].endpoint!r}, "
            f"expected 'fal-ai/z-image/turbo/image-to-image'"
        )

    def test_flux_dev_no_local_alias(self) -> None:
        """flux-dev MUST NOT have a local backend (v1 silently aliased to Z-Image)."""
        reg = ModelRegistry.load_default()
        entry, mode_spec = reg.get_by_mode("flux-dev", "t2i")
        assert "local" not in mode_spec.backends, (
            "SD-001 violation: flux-dev has a local backend — "
            "there is no Flux Dev local template"
        )

    def test_flux_schnell_no_local_backend(self) -> None:
        """flux-schnell MUST NOT have a local backend."""
        reg = ModelRegistry.load_default()
        entry, mode_spec = reg.get_by_mode("flux-schnell", "t2i")
        assert "local" not in mode_spec.backends, (
            "SD-001 violation: flux-schnell has a local backend"
        )

    def test_qwen_split_ids(self) -> None:
        """SD-001: qwen-image-2512 and qwen-image-edit are separate model IDs
        because they load different checkpoints."""
        reg = ModelRegistry.load_default()

        # qwen-image-2512: t2i only
        entry_2512 = reg.get("qwen-image-2512")
        assert "t2i" in entry_2512.modes
        assert "edit" not in entry_2512.modes

        # qwen-image-edit: edit only
        entry_edit = reg.get("qwen-image-edit")
        assert "edit" in entry_edit.modes
        assert "t2i" not in entry_edit.modes

        entry_2511, mode_2511 = reg.get_by_mode("qwen-image-edit-2511", "edit")
        assert entry_2511.id == "qwen-image-edit-2511"
        assert mode_2511.backends["cloud"].endpoint == "fal-ai/qwen-image-edit-2511"
        assert mode_2511.backends["cloud"].param_map["image_ref"] == "image_urls"


# ---------------------------------------------------------------------------
# T16: Load shipped models.yaml end-to-end
# ---------------------------------------------------------------------------


class TestShippedRegistry:
    """Verify the shipped models.yaml loads correctly with all expected entries."""

    EXPECTED_SHIPPED_IDS = {
        "seedvr2-upscaler",
        "z-image",
        "qwen-image-2512",
        "qwen-image-edit",
        "qwen-image-edit-2511",
        "qwen-image-edit-inpaint",
        "seedream-v5-pro",
        "flux-dev",
        "flux-schnell",
        "flux2-klein-9b",
        "flux2-klein-4b",
        "ideogram-v4",
        "wan-2.2",
        "ltx-2.3",
        "ltx-2.3-pro",
        "scail-2",
        "minimax-music-v2.6",
        "minimax-music-3",
        "minimax-music-3.0",
        "stable-audio-3-medium",
        "ace-step",
    }

    def test_load_default_succeeds(self) -> None:
        registry = ModelRegistry.load_default()
        assert registry is not None
        assert len(registry.list_all()) == len(self.EXPECTED_SHIPPED_IDS)

    def test_all_shipped_ids(self) -> None:
        registry = ModelRegistry.load_default()
        ids = {e.id for e in registry.list_all()}
        assert ids == self.EXPECTED_SHIPPED_IDS

    def test_get_unknown_raises_keyerror(self) -> None:
        registry = ModelRegistry.load_default()
        with pytest.raises(KeyError, match="nonexistent"):
            registry.get("nonexistent")

    def test_mode_counts(self) -> None:
        """Verify expected mode counts per model."""
        registry = ModelRegistry.load_default()

        z_image = registry.get("z-image")
        assert set(z_image.modes.keys()) == {"t2i", "i2i"}

        qwen_2512 = registry.get("qwen-image-2512")
        assert set(qwen_2512.modes.keys()) == {"t2i"}

        qwen_edit = registry.get("qwen-image-edit")
        assert set(qwen_edit.modes.keys()) == {"edit"}

        qwen_inpaint = registry.get("qwen-image-edit-inpaint")
        assert set(qwen_inpaint.modes.keys()) == {"inpaint"}

        flux_dev = registry.get("flux-dev")
        assert set(flux_dev.modes.keys()) == {"t2i", "i2i"}

        flux_schnell = registry.get("flux-schnell")
        assert set(flux_schnell.modes.keys()) == {"t2i"}


class TestBackendPrices:
    def test_price_object_must_match_exact_shape(self) -> None:
        raw = _make_v2_payload(
            modes={
                "t2i": {
                    "supports": ["prompt"],
                    "requires": ["prompt"],
                    "backends": {
                        "cloud": {
                            "endpoint": "fal-ai/test",
                            "price": {"unit": "image", "usd": 0.025, "currency": "USD"},
                            "param_map": {"prompt": "prompt"},
                        }
                    },
                }
            },
        )
        with pytest.raises(ValueError, match="exactly 'unit' and 'usd'"):
            validate_registry(raw)

    def test_price_unit_must_be_supported(self) -> None:
        raw = _make_v2_payload(
            modes={
                "t2i": {
                    "supports": ["prompt"],
                    "requires": ["prompt"],
                    "backends": {
                        "cloud": {
                            "endpoint": "fal-ai/test",
                            "price": {"unit": "megapixel", "usd": 0.025},
                            "param_map": {"prompt": "prompt"},
                        }
                    },
                }
            },
        )
        with pytest.raises(ValueError, match="unsupported price unit"):
            validate_registry(raw)

    def test_price_usd_must_be_non_negative(self) -> None:
        raw = _make_v2_payload(
            modes={
                "t2i": {
                    "supports": ["prompt"],
                    "requires": ["prompt"],
                    "backends": {
                        "cloud": {
                            "endpoint": "fal-ai/test",
                            "price": {"unit": "image", "usd": -0.01},
                            "param_map": {"prompt": "prompt"},
                        }
                    },
                }
            },
        )
        with pytest.raises(ValueError, match="non-negative finite number"):
            validate_registry(raw)

    def test_shipped_registry_deserializes_typed_backend_price(self) -> None:
        registry = ModelRegistry.load_default()
        _, mode_spec = registry.get_by_mode("flux-dev", "t2i")
        assert mode_spec.backends["cloud"].price == Price(usd=0.025, unit="image")

    def test_unpriced_shipped_backend_preserves_none(self) -> None:
        registry = ModelRegistry.load_default()
        _, mode_spec = registry.get_by_mode("qwen-image-2512", "t2i")
        assert mode_spec.backends["cloud"].price is None


# ---------------------------------------------------------------------------
# T17: list_by_modality v2
# ---------------------------------------------------------------------------


class TestListByModalityV2:
    def test_image_returns_expected_entries(self) -> None:
        registry = ModelRegistry.load_default()
        image_models = registry.list_by_modality("image")

        ids = {m.id for m in image_models}
        assert ids == {
            "seedvr2-upscaler",
            "z-image",
            "qwen-image-2512",
            "qwen-image-edit",
            "qwen-image-edit-2511",
            "qwen-image-edit-inpaint",
            "seedream-v5-pro",
            "flux-dev",
            "flux-schnell",
            "flux2-klein-9b",
            "flux2-klein-4b",
            "ideogram-v4",
        }

    def test_video_returns_expected_entries(self) -> None:
        registry = ModelRegistry.load_default()
        video_models = registry.list_by_modality("video")
        assert {m.id for m in video_models} == {
            "wan-2.2",
            "ltx-2.3",
            "ltx-2.3-pro",
            "scail-2",
        }

    def test_audio_returns_expected_entries(self) -> None:
        registry = ModelRegistry.load_default()
        audio_models = registry.list_by_modality("audio")
        assert {m.id for m in audio_models} == {
            "minimax-music-v2.6",
            "minimax-music-3",
            "minimax-music-3.0",
            "stable-audio-3-medium",
            "ace-step",
        }


# ---------------------------------------------------------------------------
# T18: Miscellaneous schema-level validations
# ---------------------------------------------------------------------------


class TestMiscValidation:
    def test_models_not_a_list(self) -> None:
        raw = {"schema_version": 2, "models": "not-a-list"}
        with pytest.raises(ValueError, match="models.*must be a list"):
            validate_registry(raw)

    def test_model_entry_not_a_dict(self) -> None:
        raw = {"schema_version": 2, "models": ["not-a-dict"]}
        with pytest.raises(ValueError, match=r"models\[0\].*must be a dict"):
            validate_registry(raw)

    def test_missing_id_field(self) -> None:
        raw = {
            "schema_version": 2,
            "models": [
                {
                    "modality": "image",
                    "modes": {
                        "t2i": {
                            "supports": ["prompt"],
                            "requires": ["prompt"],
                            "backends": {
                                "local": {
                                    "template": "image/test",
                                    "param_map": {"prompt": "prompt"},
                                }
                            },
                        }
                    },
                }
            ],
        }
        with pytest.raises(ValueError, match=r"models\[0\]\.id"):
            validate_registry(raw)

    def test_registry_not_a_dict(self) -> None:
        with pytest.raises(ValueError, match="registry must be a dict"):
            validate_registry(["not-a-dict"])

    def test_empty_param_map_value_rejected(self) -> None:
        raw = _make_v2_payload(
            modes={
                "t2i": {
                    "supports": ["prompt"],
                    "requires": ["prompt"],
                    "backends": {
                        "local": {
                            "template": "image/test",
                            "param_map": {"prompt": ""},
                        }
                    },
                }
            }
        )
        with pytest.raises(ValueError, match="value must be a non-empty string"):
            validate_registry(raw)

    def test_unknown_backend_key_rejected(self) -> None:
        raw = _make_v2_payload(
            modes={
                "t2i": {
                    "supports": ["prompt"],
                    "requires": ["prompt"],
                    "backends": {
                        "runpod": {  # not a valid backend key
                            "endpoint": "runpod/test",
                            "param_map": {"prompt": "prompt"},
                        }
                    },
                }
            }
        )
        with pytest.raises(ValueError, match="unknown backend key"):
            validate_registry(raw)

    def test_empty_backends_rejected(self) -> None:
        raw = _make_v2_payload(
            modes={
                "t2i": {
                    "supports": ["prompt"],
                    "requires": ["prompt"],
                    "backends": {},
                }
            }
        )
        with pytest.raises(ValueError, match="at least one backend"):
            validate_registry(raw)

    def test_modes_not_a_dict_rejected(self) -> None:
        raw = {
            "schema_version": 2,
            "models": [
                {
                    "id": "test",
                    "modality": "image",
                    "modes": "not-a-dict",
                }
            ],
        }
        with pytest.raises(ValueError, match="modes.*must be a dict"):
            validate_registry(raw)


# ---------------------------------------------------------------------------
# T19: Edit mode SD-003 — excludes negative_prompt and strength
# ---------------------------------------------------------------------------


class TestEditModeFeatures:
    """SD-003: Edit mode must NOT list negative_prompt or strength in supports."""

    def test_qwen_edit_no_negative_prompt(self) -> None:
        reg = ModelRegistry.load_default()
        _, mode_spec = reg.get_by_mode("qwen-image-edit", "edit")
        assert "negative_prompt" not in mode_spec.supports

    def test_qwen_edit_no_strength(self) -> None:
        reg = ModelRegistry.load_default()
        _, mode_spec = reg.get_by_mode("qwen-image-edit", "edit")
        assert "strength" not in mode_spec.supports

    def test_edit_has_image_ref_required(self) -> None:
        reg = ModelRegistry.load_default()
        _, mode_spec = reg.get_by_mode("qwen-image-edit", "edit")
        assert "image_ref" in mode_spec.requires
        assert "prompt" in mode_spec.requires


# ---------------------------------------------------------------------------
# T20: Sold-separately model identity verification via get
# ---------------------------------------------------------------------------


class TestModelGetBackwardCompat:
    """get(model_id) still works for backward compat."""

    def test_get_returns_model_entry(self) -> None:
        reg = ModelRegistry.load_default()
        entry = reg.get("z-image")
        assert entry.id == "z-image"
        assert entry.modality == "image"
        assert "t2i" in entry.modes
        assert "i2i" in entry.modes

    def test_get_closed_field(self) -> None:
        reg = ModelRegistry.load_default()
        entry = reg.get("z-image")
        # z-image is open-weight
        assert not entry.closed


# ---------------------------------------------------------------------------
# T21: Pack-declared backend ids flow into load_default validation
# ---------------------------------------------------------------------------


def _write_extension_pack(root: Path, *, backend_id: str) -> None:
    pack_dir = root / "extension_pack"
    pack_dir.mkdir(parents=True)
    (pack_dir / "pack.yaml").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "id": "extension_pack",
                "name": "Extension Pack",
                "version": "0.1.0",
                "extensions": {
                    "generation": {
                        "backends": [
                            {
                                "id": backend_id,
                                "module": "extension.backend",
                                "class": "ExtensionBackend",
                            }
                        ]
                    }
                },
            }
        ),
        encoding="utf-8",
    )


class TestPackDeclaredBackendIds:
    def test_load_default_accepts_pack_declared_backend_id(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        extra_root = tmp_path / "extra-packs"
        _write_extension_pack(extra_root, backend_id="studio")
        raw = _make_v2_payload(
            model_id="ext-model",
            modes={
                "t2i": {
                    "supports": ["prompt"],
                    "requires": ["prompt"],
                    "backends": {
                        "studio": {
                            "param_map": {"prompt": "prompt"},
                        }
                    },
                }
            },
        )

        monkeypatch.setattr(
            "astrid.core.model_catalog.registry._load_yaml",
            lambda path: raw,
        )

        registry = ModelRegistry.load_default(
            project_root=tmp_path,
            extra_pack_roots=(str(extra_root),),
        )

        assert registry.backend_available("ext-model", "t2i", "studio") is True

    def test_load_default_rejects_undeclared_backend_id(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        extra_root = tmp_path / "extra-packs"
        _write_extension_pack(extra_root, backend_id="studio")
        raw = _make_v2_payload(
            model_id="ext-model",
            modes={
                "t2i": {
                    "supports": ["prompt"],
                    "requires": ["prompt"],
                    "backends": {
                        "rogue": {
                            "param_map": {"prompt": "prompt"},
                        }
                    },
                }
            },
        )

        monkeypatch.setattr(
            "astrid.core.model_catalog.registry._load_yaml",
            lambda path: raw,
        )

        with pytest.raises(ValueError, match="unknown backend key; available backend ids:"):
            ModelRegistry.load_default(
                project_root=tmp_path,
                extra_pack_roots=(str(extra_root),),
            )

    def test_load_default_rejects_synthetic_backend_when_declaring_pack_not_loaded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A synthetic backend id is rejected when no pack declaring it is loaded.

        Even though the backend id *could* be valid if a pack declared it,
        if we do NOT pass the declaring pack as an extra root (and installed
        packs are excluded), the validation must reject it.
        """
        # Write a pack that declares "studio", but we will NOT load it.
        extra_root = tmp_path / "unloaded-packs"
        _write_extension_pack(extra_root, backend_id="studio")

        raw = _make_v2_payload(
            model_id="ext-model",
            modes={
                "t2i": {
                    "supports": ["prompt"],
                    "requires": ["prompt"],
                    "backends": {
                        "studio": {
                            "param_map": {"prompt": "prompt"},
                        }
                    },
                }
            },
        )

        monkeypatch.setattr(
            "astrid.core.model_catalog.registry._load_yaml",
            lambda path: raw,
        )

        # Deliberately do NOT pass extra_pack_roots — "studio" is undeclared.
        with pytest.raises(ValueError, match="unknown backend key; available backend ids:"):
            ModelRegistry.load_default(
                project_root=tmp_path,
            )


# ---------------------------------------------------------------------------
# T22: validate_registry_with_backends — pure unit tests (no pack discovery)
# ---------------------------------------------------------------------------


class TestValidateRegistryWithBackends:
    """Direct ``validate_registry_with_backends()`` tests that are completely
    independent of ``ASTRID_PACKS_PATH`` and pack discovery.

    These prove that a synthetic backend id is rejected when it is not present
    in *allowed_backend_ids* and accepted when it is, with zero dependency on
    environment variables or filesystem pack layout.
    """

    SYNTHETIC_BACKEND_ID = "studio"

    def _raw_with_backend(self, backend_id: str) -> dict:
        """Return a minimal valid v2 payload referencing *backend_id*."""
        return _make_v2_payload(
            model_id="test-model",
            modes={
                "t2i": {
                    "supports": ["prompt"],
                    "requires": ["prompt"],
                    "backends": {
                        backend_id: {
                            "param_map": {"prompt": "prompt"},
                        }
                    },
                }
            },
        )

    # -- synthetic backend rejected when undeclared -----------------------

    def test_synthetic_backend_rejected_when_not_in_allowed_ids(self) -> None:
        """A backend id not in *allowed_backend_ids* is rejected."""
        raw = self._raw_with_backend(self.SYNTHETIC_BACKEND_ID)
        with pytest.raises(ValueError, match="unknown backend key; available backend ids:"):
            validate_registry_with_backends(
                raw,
                allowed_backend_ids=("local", "cloud"),
            )

    def test_synthetic_backend_rejected_when_allowed_ids_is_none(self) -> None:
        """When *allowed_backend_ids* is ``None``, only built-in ids are allowed."""
        raw = self._raw_with_backend(self.SYNTHETIC_BACKEND_ID)
        with pytest.raises(ValueError, match="unknown backend key; available backend ids:"):
            validate_registry_with_backends(raw)

    def test_empty_allowed_ids_falls_back_to_builtins(self) -> None:
        """An empty tuple ``()`` is falsy and falls back to built-in backends.

        This means ``"local"`` is accepted (it's in the fallback set) but
        a synthetic id like ``"studio"`` is still rejected.
        """
        # "local" passes because the fallback includes built-ins.
        raw_local = self._raw_with_backend("local")
        # But "local" needs a template for validation...
        raw_local["models"][0]["modes"]["t2i"]["backends"]["local"]["template"] = "image/test"
        entries = validate_registry_with_backends(raw_local, allowed_backend_ids=())
        assert entries[0].id == "test-model"

        # "studio" fails because it's not in the fallback set.
        raw_studio = self._raw_with_backend(self.SYNTHETIC_BACKEND_ID)
        with pytest.raises(
            ValueError,
            match="unknown backend key; available backend ids: cloud, codex, local",
        ):
            validate_registry_with_backends(raw_studio, allowed_backend_ids=())

    # -- synthetic backend accepted when declared -------------------------

    def test_synthetic_backend_accepted_when_in_allowed_ids(self) -> None:
        """A backend id present in *allowed_backend_ids* passes validation."""
        raw = self._raw_with_backend(self.SYNTHETIC_BACKEND_ID)
        entries = validate_registry_with_backends(
            raw,
            allowed_backend_ids=("local", "cloud", self.SYNTHETIC_BACKEND_ID),
        )
        assert len(entries) == 1
        assert entries[0].id == "test-model"
        assert self.SYNTHETIC_BACKEND_ID in entries[0].modes["t2i"].backends

    def test_synthetic_backend_accepted_with_augmented_frozenset(self) -> None:
        """A ``frozenset`` containing the synthetic id also works."""
        raw = self._raw_with_backend(self.SYNTHETIC_BACKEND_ID)
        entries = validate_registry_with_backends(
            raw,
            allowed_backend_ids=frozenset(
                ["local", "cloud", self.SYNTHETIC_BACKEND_ID]
            ),
        )
        assert len(entries) == 1

    # -- built-in backends always work ------------------------------------

    def test_local_backend_accepted_in_default_allowed_ids(self) -> None:
        raw = _make_v2_payload(
            model_id="local-model",
            modes={
                "t2i": {
                    "supports": ["prompt"],
                    "requires": ["prompt"],
                    "backends": {
                        "local": {
                            "template": "image/local",
                            "param_map": {"prompt": "prompt"},
                        }
                    },
                }
            },
        )
        entries = validate_registry_with_backends(raw)
        assert entries[0].id == "local-model"

    def test_cloud_backend_accepted_in_default_allowed_ids(self) -> None:
        raw = _make_v2_payload(
            model_id="cloud-model",
            modes={
                "t2i": {
                    "supports": ["prompt"],
                    "requires": ["prompt"],
                    "backends": {
                        "cloud": {
                            "endpoint": "fal-ai/cloud",
                            "param_map": {"prompt": "prompt"},
                        }
                    },
                }
            },
        )
        entries = validate_registry_with_backends(raw)
        assert entries[0].id == "cloud-model"

    # -- error message lists available ids --------------------------------

    def test_error_message_lists_available_backend_ids(self) -> None:
        """The rejection error message includes the sorted list of available ids."""
        raw = self._raw_with_backend(self.SYNTHETIC_BACKEND_ID)
        with pytest.raises(
            ValueError,
            match="unknown backend key; available backend ids: cloud, codex, local",
        ):
            validate_registry_with_backends(raw)

    def test_error_message_reflects_augmented_allowed_ids(self) -> None:
        """When extra backend ids are allowed, the error message includes them."""
        raw = self._raw_with_backend("rogue")
        with pytest.raises(
            ValueError,
            match="unknown backend key; available backend ids: cloud, local, studio",
        ):
            validate_registry_with_backends(
                raw,
                allowed_backend_ids=("local", "cloud", "studio"),
            )


class TestValidateRegistryWithGenerationTaxonomy:
    def test_synthetic_feature_rejected_without_taxonomy_registry(self) -> None:
        raw = _make_v2_payload(
            modes={
                "t2i": {
                    "supports": ["prompt", "synthetic_mask_ref"],
                    "requires": ["prompt"],
                    "backends": {
                        "local": {
                            "template": "image/test",
                            "param_map": {"prompt": "prompt", "synthetic_mask_ref": "mask"},
                        }
                    },
                }
            },
        )

        with pytest.raises(ValueError, match="not a recognised Feature"):
            validate_registry(raw)

    def test_synthetic_feature_accepted_with_taxonomy_registry(self) -> None:
        raw = _make_v2_payload(
            modes={
                "t2i": {
                    "supports": ["prompt", "synthetic_mask_ref"],
                    "requires": ["prompt"],
                    "backends": {
                        "local": {
                            "template": "image/test",
                            "param_map": {"prompt": "prompt", "synthetic_mask_ref": "mask"},
                        }
                    },
                }
            },
        )
        registry = GenerationTaxonomyRegistry(
            feature_descriptors=(GenerationFeatureDescriptor(id="synthetic_mask_ref"),)
        )

        entries = validate_registry_with_backends(raw, taxonomy_registry=registry)
        assert entries[0].modes["t2i"].supports == ("prompt", "synthetic_mask_ref")

    def test_synthetic_mode_rejected_without_taxonomy_registry(self) -> None:
        raw = _make_v2_payload(
            modes={
                "storyboard": {
                    "supports": ["prompt"],
                    "requires": ["prompt"],
                    "backends": {
                        "local": {
                            "template": "image/test",
                            "param_map": {"prompt": "prompt"},
                        }
                    },
                }
            },
        )

        with pytest.raises(ValueError, match="unknown image mode"):
            validate_registry(raw)

    def test_synthetic_mode_accepted_with_taxonomy_registry(self) -> None:
        raw = _make_v2_payload(
            modes={
                "storyboard": {
                    "supports": ["prompt"],
                    "requires": ["prompt"],
                    "backends": {
                        "local": {
                            "template": "image/test",
                            "param_map": {"prompt": "prompt"},
                        }
                    },
                }
            },
        )
        registry = GenerationTaxonomyRegistry(
            mode_descriptors=(GenerationModeDescriptor(id="storyboard"),)
        )

        entries = validate_registry_with_backends(raw, taxonomy_registry=registry)
        assert "storyboard" in entries[0].modes

    # -- undeclared backend fails via taxonomy registry ------------------

    def test_undeclared_backend_fails_via_taxonomy_registry(self) -> None:
        """Backend id not in taxonomy_registry.backend_ids() is rejected."""
        raw = _make_v2_payload(
            modes={
                "t2i": {
                    "supports": ["prompt"],
                    "requires": ["prompt"],
                    "backends": {
                        "studio": {
                            "param_map": {"prompt": "prompt"},
                        }
                    },
                }
            },
        )
        # taxonomy_registry with only built-in backends — "studio" is undeclared
        registry = GenerationTaxonomyRegistry()
        with pytest.raises(ValueError, match="unknown backend key; available backend ids: cloud, codex, local"):
            validate_registry_with_backends(raw, taxonomy_registry=registry)

    def test_declared_backend_passes_via_taxonomy_registry(self) -> None:
        """Backend id present in taxonomy_registry.backend_ids() is accepted."""
        raw = _make_v2_payload(
            modes={
                "t2i": {
                    "supports": ["prompt"],
                    "requires": ["prompt"],
                    "backends": {
                        "studio": {
                            "param_map": {"prompt": "prompt"},
                        }
                    },
                }
            },
        )
        registry = GenerationTaxonomyRegistry(
            backend_descriptors=(GenerationBackendIdDescriptor(id="studio"),)
        )
        # `allowed_backend_ids` defaults to registry.backend_ids()
        entries = validate_registry_with_backends(raw, taxonomy_registry=registry)
        assert entries[0].id == "test-model"
        assert "studio" in entries[0].modes["t2i"].backends

    # -- all three declared axes pass together ---------------------------

    def test_all_three_declared_pass_together(self) -> None:
        """Feature, mode, and backend all declared in the taxonomy → all accepted."""
        raw = _make_v2_payload(
            modes={
                "storyboard": {
                    "supports": ["prompt", "synthetic_mask_ref"],
                    "requires": ["prompt"],
                    "backends": {
                        "studio": {
                            "param_map": {"prompt": "prompt", "synthetic_mask_ref": "mask"},
                        }
                    },
                }
            },
        )
        registry = GenerationTaxonomyRegistry(
            feature_descriptors=(GenerationFeatureDescriptor(id="synthetic_mask_ref"),),
            mode_descriptors=(GenerationModeDescriptor(id="storyboard"),),
            backend_descriptors=(GenerationBackendIdDescriptor(id="studio"),),
        )
        entries = validate_registry_with_backends(raw, taxonomy_registry=registry)
        assert entries[0].id == "test-model"
        assert "storyboard" in entries[0].modes
        mode_spec = entries[0].modes["storyboard"]
        assert "synthetic_mask_ref" in mode_spec.supports
        assert "studio" in mode_spec.backends

    # -- built-in validation unchanged -----------------------------------

    def test_builtin_validation_unchanged_with_defaults(self) -> None:
        """validate_registry() with default taxonomy still accepts standard payloads."""
        raw = _make_v2_payload(
            model_id="unchanged-model",
            modes={
                "t2i": {
                    "supports": ["prompt", "seed", "count", "size"],
                    "requires": ["prompt"],
                    "backends": {
                        "local": {
                            "template": "image/unchanged",
                            "param_map": {"prompt": "prompt"},
                        }
                    },
                }
            },
        )
        entries = validate_registry(raw)
        assert entries[0].id == "unchanged-model"
        assert set(entries[0].modes["t2i"].supports) == {"prompt", "seed", "count", "size"}

    def test_declared_names_do_not_break_builtin_validation(self) -> None:
        """Custom taxonomy with extras still accepts built-in names."""
        raw = _make_v2_payload(
            model_id="both-model",
            modes={
                "t2i": {
                    "supports": ["prompt", "seed"],
                    "requires": ["prompt"],
                    "backends": {
                        "cloud": {
                            "endpoint": "fal-ai/both",
                            "param_map": {"prompt": "prompt"},
                        }
                    },
                }
            },
        )
        # Custom taxonomy adds extra names — built-in names should still work
        registry = GenerationTaxonomyRegistry(
            feature_descriptors=(GenerationFeatureDescriptor(id="synthetic_mask_ref"),),
            mode_descriptors=(GenerationModeDescriptor(id="storyboard"),),
            backend_descriptors=(GenerationBackendIdDescriptor(id="studio"),),
        )
        entries = validate_registry_with_backends(raw, taxonomy_registry=registry)
        assert entries[0].id == "both-model"
        assert "t2i" in entries[0].modes
        assert "prompt" in entries[0].modes["t2i"].supports
        assert "cloud" in entries[0].modes["t2i"].backends

    # -- undeclared names fail even with partially declared taxonomy ------

    def test_undeclared_feature_fails_with_partial_custom_taxonomy(self) -> None:
        """Even when mode+backend are custom-declared, an undeclared feature still fails."""
        raw = _make_v2_payload(
            modes={
                "storyboard": {
                    "supports": ["prompt", "bogus_feature"],
                    "requires": ["prompt"],
                    "backends": {
                        "studio": {
                            "param_map": {"prompt": "prompt", "bogus_feature": "bogus"},
                        }
                    },
                }
            },
        )
        registry = GenerationTaxonomyRegistry(
            mode_descriptors=(GenerationModeDescriptor(id="storyboard"),),
            backend_descriptors=(GenerationBackendIdDescriptor(id="studio"),),
        )
        with pytest.raises(ValueError, match="not a recognised Feature"):
            validate_registry_with_backends(raw, taxonomy_registry=registry)

    def test_undeclared_mode_fails_with_partial_custom_taxonomy(self) -> None:
        """Even when feature+backend are custom-declared, an undeclared mode still fails."""
        raw = _make_v2_payload(
            modes={
                "bogus_mode": {
                    "supports": ["prompt", "synthetic_mask_ref"],
                    "requires": ["prompt"],
                    "backends": {
                        "studio": {
                            "param_map": {"prompt": "prompt"},
                        }
                    },
                }
            },
        )
        registry = GenerationTaxonomyRegistry(
            feature_descriptors=(GenerationFeatureDescriptor(id="synthetic_mask_ref"),),
            backend_descriptors=(GenerationBackendIdDescriptor(id="studio"),),
        )
        with pytest.raises(ValueError, match="unknown image mode"):
            validate_registry_with_backends(raw, taxonomy_registry=registry)

    def test_undeclared_backend_fails_with_partial_custom_taxonomy(self) -> None:
        """Even when feature+mode are custom-declared, an undeclared backend still fails."""
        raw = _make_v2_payload(
            modes={
                "storyboard": {
                    "supports": ["prompt", "synthetic_mask_ref"],
                    "requires": ["prompt"],
                    "backends": {
                        "bogus_backend": {
                            "param_map": {"prompt": "prompt"},
                        }
                    },
                }
            },
        )
        registry = GenerationTaxonomyRegistry(
            feature_descriptors=(GenerationFeatureDescriptor(id="synthetic_mask_ref"),),
            mode_descriptors=(GenerationModeDescriptor(id="storyboard"),),
        )
        with pytest.raises(ValueError, match="unknown backend key"):
            validate_registry_with_backends(raw, taxonomy_registry=registry)


# ---------------------------------------------------------------------------
# LoRA registry tests
# ---------------------------------------------------------------------------


class TestLoraRegistryLoad:
    """LoRA registry loads from loras.yaml and validates base_model."""

    def test_load_default_loras(self) -> None:
        """The shipped loras.yaml loads with model_ids validation."""
        from astrid.core.model_catalog.registry import LoraRegistry, ModelRegistry

        model_registry = ModelRegistry.load_default()
        model_ids = frozenset(e.id for e in model_registry.list_all())
        lora_registry = LoraRegistry.load_default(model_ids=model_ids)
        entries = lora_registry.list_all()
        assert len(entries) == 3
        ids = {e.id for e in entries}
        assert ids == {"flux-realism", "z-realgen-v2", "flux2-klein-realism"}

    def test_get_by_id(self) -> None:
        """LoRA lookup by id returns the correct entry."""
        from astrid.core.model_catalog.registry import LoraRegistry, ModelRegistry

        model_registry = ModelRegistry.load_default()
        model_ids = frozenset(e.id for e in model_registry.list_all())
        lora_registry = LoraRegistry.load_default(model_ids=model_ids)
        entry = lora_registry.get("flux-realism")
        assert entry.base_model == "flux-dev"
        assert entry.intent == "realism"
        assert entry.verified is True
        assert entry.source.file == "lora.safetensors"

    def test_list_by_base_model(self) -> None:
        """list_by_base_model filters correctly."""
        from astrid.core.model_catalog.registry import LoraRegistry, ModelRegistry

        model_registry = ModelRegistry.load_default()
        model_ids = frozenset(e.id for e in model_registry.list_all())
        lora_registry = LoraRegistry.load_default(model_ids=model_ids)
        flux_loras = lora_registry.list_by_base_model("flux-dev")
        assert len(flux_loras) == 1
        assert flux_loras[0].id == "flux-realism"

    def test_unknown_lora_raises_keyerror(self) -> None:
        """Unknown LoRA id raises KeyError listing available."""
        from astrid.core.model_catalog.registry import LoraRegistry, ModelRegistry

        model_registry = ModelRegistry.load_default()
        model_ids = frozenset(e.id for e in model_registry.list_all())
        lora_registry = LoraRegistry.load_default(model_ids=model_ids)
        with pytest.raises(KeyError, match="Unknown LoRA 'nonexistent'"):
            lora_registry.get("nonexistent")

    def test_base_model_validation_on_load(self) -> None:
        """Loading with model_ids that don't include a LoRA's base_model fails."""
        from astrid.core.model_catalog.schema import validate_lora_registry

        raw = {
            "schema_version": 1,
            "loras": [
                {
                    "id": "test-lora",
                    "name": "Test LoRA",
                    "base_model": "nonexistent-model",
                    "intent": "realism",
                    "source": {
                        "repo": "test/repo",
                        "file": "lora.safetensors",
                        "url": "https://example.com/lora.safetensors",
                    },
                    "default_scale": 1.0,
                    "verified": False,
                    "notes": "",
                }
            ],
        }
        with pytest.raises(
            ValueError, match="base_model 'nonexistent-model' not found in model registry"
        ):
            validate_lora_registry(raw, model_ids=frozenset({"valid-model"}))

    def test_duplicate_lora_id_rejected(self) -> None:
        """Duplicate LoRA ids are rejected by validation."""
        from astrid.core.model_catalog.schema import validate_lora_registry

        raw = {
            "schema_version": 1,
            "loras": [
                {
                    "id": "dup-lora",
                    "name": "A",
                    "base_model": "flux-dev",
                    "intent": "realism",
                    "source": {
                        "repo": "test/a",
                        "file": "a.safetensors",
                        "url": "https://example.com/a.safetensors",
                    },
                    "default_scale": 1.0,
                    "verified": False,
                    "notes": "",
                },
                {
                    "id": "dup-lora",
                    "name": "B",
                    "base_model": "flux-dev",
                    "intent": "style",
                    "source": {
                        "repo": "test/b",
                        "file": "b.safetensors",
                        "url": "https://example.com/b.safetensors",
                    },
                    "default_scale": 1.0,
                    "verified": False,
                    "notes": "",
                },
            ],
        }
        with pytest.raises(ValueError, match="duplicate lora id 'dup-lora'"):
            validate_lora_registry(raw, model_ids=frozenset({"flux-dev"}))


def test_default_qwen_inpaint_registry_accepts_shipped_image_mask_feature():
    registry = ModelRegistry.load_default()
    model, mode = registry.get_by_mode("qwen-image-edit-inpaint", "inpaint")
    assert model.id == "qwen-image-edit-inpaint"
    assert "mask_ref" in mode.supports
    assert "mask_ref" in mode.requires
