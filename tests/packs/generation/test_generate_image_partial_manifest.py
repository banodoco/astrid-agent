"""Focused tests for T24: partial-output manifest on generate_image loop failure.

Validates:
- When generate_image's adapter fails after producing at least one output,
  the manifest.json is atomically written with the partial successful outputs.
- The original exception is preserved and re-raised.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Bypass the canonical-entrypoint guard so the module can be imported.
os.environ.setdefault("ASTRID_INTERNAL_INVOCATION", "1")

from astrid.core.generation.backends.base import GenerationResult  # noqa: E402
from astrid.packs.generation.actions.generate_image.run import (  # noqa: E402
    generate_core as generate_image_core,
)


class TestPartialOutputManifestOnLoopFailure:
    """T24: When the generation loop fails after partial success, manifest is written."""

    def test_partial_output_manifest_atomic_write_and_original_exception_reraised(
        self, tmp_path: Path,
    ) -> None:
        """Adapter succeeds on iteration 0, fails on iteration 1.

        The manifest must be atomically written with iteration 0's output,
        and the original ValueError must be re-raised.
        """
        out = tmp_path / "out"
        out.mkdir()
        images_dir = out / "images"
        images_dir.mkdir()

        # Create a fake image file for the first (successful) iteration.
        fake_img = images_dir / "fake_001.png"
        fake_img.write_bytes(b"fake_png_data")

        # --- mock model registry ------------------------------------------
        mock_entry = MagicMock()
        mock_entry.id = "test-model"
        mock_entry.modality = "image"

        mock_mode_spec = MagicMock()
        mock_mode_spec.requires = ()
        mock_mode_spec.supports = {"prompt", "negative_prompt", "seed", "count", "size"}
        mock_mode_spec.backends = {"local": MagicMock()}

        mock_registry = MagicMock()
        mock_registry.get_by_mode.return_value = (mock_entry, mock_mode_spec)
        mock_registry.backend_available.return_value = True

        # --- mock backend adapter: succeed once, fail on second call ------
        result1 = GenerationResult(
            image_paths=[fake_img],
            seed_used=42,
            model_actual="test-model-actual",
            applied_features=["prompt"],
        )
        mock_adapter = MagicMock()
        mock_adapter.generate.side_effect = [
            result1,
            ValueError("simulated generation failure"),
        ]

        # --- build minimal args namespace --------------------------------
        import argparse

        args = argparse.Namespace()
        args.model = "test-model"
        args.mode = "t2i"
        args.prompt = "test prompt"
        args.count = 2
        args.execution = "local"
        args.out = out
        args.prompts_file = None
        args.env_file = None
        args.seed = 42
        args.image_ref = None
        args.negative_prompt = None
        args.size = None
        args.loras = None
        args.strength = None
        args.guidance_scale = None
        args.steps = None

        with patch(
            "astrid.packs.generation.actions.generate_image.run.ModelRegistry.load_default",
            return_value=mock_registry,
        ), patch(
            "astrid.packs.generation.actions.generate_image.run."
            "load_default_generation_backend_registry",
            return_value=MagicMock(),
        ), patch(
            "astrid.packs.generation.actions.generate_image.run._create_backend_adapter",
            return_value=mock_adapter,
        ), patch(
            "astrid.packs.generation.actions.generate_image.run.embed_png_text",
        ), patch(
            "astrid.packs.generation.actions.generate_image.run.write_json_atomic",
        ) as mock_write:
            # --- act: should raise the original ValueError ---------------
            with pytest.raises(ValueError, match="simulated generation failure"):
                generate_image_core(args)

            # --- assert: manifest was atomically written with partial outputs
            mock_write.assert_called_once()
            (_manifest_path, manifest_data) = mock_write.call_args[0]
            assert isinstance(manifest_data, dict)
            assert manifest_data.get("outputs"), "manifest must contain partial outputs"
            assert len(manifest_data["outputs"]) == 1
            assert manifest_data["outputs"][0]["path"].startswith("images/")
            assert manifest_data["model_actual"] == "test-model-actual"
            assert manifest_data["seed"] == 42
            assert manifest_data["outputs"][0]["seed_used"] is None

    def test_no_manifest_written_when_no_outputs_produced(
        self, tmp_path: Path,
    ) -> None:
        """When the very first adapter.generate() fails, no manifest is written."""
        out = tmp_path / "out"
        out.mkdir()
        (out / "images").mkdir()

        # --- mock model registry ------------------------------------------
        mock_entry = MagicMock()
        mock_entry.id = "test-model"
        mock_entry.modality = "image"

        mock_mode_spec = MagicMock()
        mock_mode_spec.requires = ()
        mock_mode_spec.supports = {"prompt", "negative_prompt", "seed", "count", "size"}
        mock_mode_spec.backends = {"local": MagicMock()}

        mock_registry = MagicMock()
        mock_registry.get_by_mode.return_value = (mock_entry, mock_mode_spec)
        mock_registry.backend_available.return_value = True

        # --- mock backend adapter: fails on first call --------------------
        mock_adapter = MagicMock()
        mock_adapter.generate.side_effect = ValueError("first call failure")

        import argparse

        args = argparse.Namespace()
        args.model = "test-model"
        args.mode = "t2i"
        args.prompt = "test prompt"
        args.count = 1
        args.execution = "local"
        args.out = out
        args.prompts_file = None
        args.env_file = None
        args.seed = None
        args.image_ref = None
        args.negative_prompt = None
        args.size = None
        args.loras = None
        args.strength = None
        args.guidance_scale = None
        args.steps = None

        with patch(
            "astrid.packs.generation.actions.generate_image.run.ModelRegistry.load_default",
            return_value=mock_registry,
        ), patch(
            "astrid.packs.generation.actions.generate_image.run."
            "load_default_generation_backend_registry",
            return_value=MagicMock(),
        ), patch(
            "astrid.packs.generation.actions.generate_image.run._create_backend_adapter",
            return_value=mock_adapter,
        ), patch(
            "astrid.packs.generation.actions.generate_image.run.embed_png_text",
        ), patch(
            "astrid.packs.generation.actions.generate_image.run.write_json_atomic",
        ) as mock_write:
            with pytest.raises(ValueError, match="first call failure"):
                generate_image_core(args)

            # No manifest should be written when zero outputs were produced.
            mock_write.assert_not_called()

    def test_bounded_partial_manifest_is_removed_when_it_exceeds_limit(
        self, tmp_path: Path,
    ) -> None:
        """A failed bounded task never leaves an over-limit partial receipt."""
        from astrid.core.generation.storage_policy import CLOUD_T2I_STORAGE_POLICY
        from astrid.packs.generation.actions.generate_image import run as run_mod

        out = tmp_path / "out"
        (out / "images").mkdir(parents=True)
        fake_img = out / "images" / "fake_001.png"
        fake_img.write_bytes(b"fake_png_data")

        mock_entry = MagicMock(id="flux-dev", modality="image")
        mock_mode_spec = MagicMock(
            requires=(),
            supports={"prompt", "negative_prompt", "seed", "count", "size"},
            backends={"cloud": MagicMock()},
        )
        mock_registry = MagicMock()
        mock_registry.get_by_mode.return_value = (mock_entry, mock_mode_spec)
        mock_registry.backend_available.return_value = True
        mock_adapter = MagicMock()
        mock_adapter.generate.side_effect = [
            GenerationResult(
                image_paths=[fake_img],
                seed_used=42,
                model_actual="flux-dev",
            ),
            ValueError("bounded failure"),
        ]

        import argparse

        args = argparse.Namespace(
            model="flux-dev",
            mode="t2i",
            prompt="bounded partial proof",
            count=2,
            execution="cloud",
            out=out,
            prompts_file=None,
            env_file=None,
            seed=42,
            image_ref=None,
            negative_prompt=None,
            size="1536x1024",
            loras=None,
            strength=None,
            guidance_scale=None,
            steps=28,
            storage_policy_version=CLOUD_T2I_STORAGE_POLICY.version,
        )
        real_build_manifest = run_mod.build_generation_manifest

        def oversized_manifest(**kwargs):
            manifest = real_build_manifest(**kwargs)
            manifest["padding"] = "x" * CLOUD_T2I_STORAGE_POLICY.manifest_max_bytes
            return manifest

        with patch(
            "astrid.packs.generation.actions.generate_image.run.ModelRegistry.load_default",
            return_value=mock_registry,
        ), patch(
            "astrid.packs.generation.actions.generate_image.run."
            "load_default_generation_backend_registry",
            return_value=MagicMock(),
        ), patch(
            "astrid.packs.generation.actions.generate_image.run._create_backend_adapter",
            return_value=mock_adapter,
        ), patch(
            "astrid.packs.generation.actions.generate_image.run.embed_png_text",
        ), patch.object(run_mod, "build_generation_manifest", oversized_manifest):
            with pytest.raises(ValueError, match="bounded failure"):
                run_mod.generate_core(args)

        assert not (out / "manifest.json").exists()

    def test_manifest_content_matches_success_path_equivalent(
        self, tmp_path: Path,
    ) -> None:
        """Partial-output manifest has the same shape as a success-path manifest."""
        out = tmp_path / "out"
        out.mkdir()
        images_dir = out / "images"
        images_dir.mkdir()

        fake_img = images_dir / "fake_001.png"
        fake_img.write_bytes(b"fake_png_data")

        # --- mock model registry ------------------------------------------
        mock_entry = MagicMock()
        mock_entry.id = "test-model"
        mock_entry.modality = "image"

        mock_mode_spec = MagicMock()
        mock_mode_spec.requires = ()
        mock_mode_spec.supports = {"prompt", "negative_prompt", "seed", "count", "size"}
        mock_mode_spec.backends = {"local": MagicMock()}

        mock_registry = MagicMock()
        mock_registry.get_by_mode.return_value = (mock_entry, mock_mode_spec)
        mock_registry.backend_available.return_value = True

        # --- partial path: one success, then failure ----------------------
        result1 = GenerationResult(
            image_paths=[fake_img],
            seed_used=42,
            model_actual="test-model-actual",
            applied_features=["prompt"],
        )
        mock_adapter_partial = MagicMock()
        mock_adapter_partial.generate.side_effect = [
            result1,
            ValueError("fail"),
        ]

        # --- full success path: one success, count=1 ----------------------
        result_single = GenerationResult(
            image_paths=[fake_img],
            seed_used=42,
            model_actual="test-model-actual",
            applied_features=["prompt"],
        )
        mock_adapter_full = MagicMock()
        mock_adapter_full.generate.return_value = result_single

        import argparse

        args_partial = argparse.Namespace()
        args_partial.model = "test-model"
        args_partial.mode = "t2i"
        args_partial.prompt = "test prompt"
        args_partial.count = 2
        args_partial.execution = "local"
        args_partial.out = out
        args_partial.prompts_file = None
        args_partial.env_file = None
        args_partial.seed = None
        args_partial.image_ref = None
        args_partial.negative_prompt = None
        args_partial.size = None
        args_partial.loras = None
        args_partial.strength = None
        args_partial.guidance_scale = None
        args_partial.steps = None

        args_full = argparse.Namespace()
        args_full.model = "test-model"
        args_full.mode = "t2i"
        args_full.prompt = "test prompt"
        args_full.count = 1
        args_full.execution = "local"
        args_full.out = out
        args_full.prompts_file = None
        args_full.env_file = None
        args_full.seed = None
        args_full.image_ref = None
        args_full.negative_prompt = None
        args_full.size = None
        args_full.loras = None
        args_full.strength = None
        args_full.guidance_scale = None
        args_full.steps = None

        # --- get partial manifest via exception path ----------------------
        captured_partial: list[dict] = []

        def _capture_partial(manifest_path, data):
            captured_partial.append(data)

        with patch(
            "astrid.packs.generation.actions.generate_image.run.ModelRegistry.load_default",
            return_value=mock_registry,
        ), patch(
            "astrid.packs.generation.actions.generate_image.run."
            "load_default_generation_backend_registry",
            return_value=MagicMock(),
        ), patch(
            "astrid.packs.generation.actions.generate_image.run._create_backend_adapter",
            return_value=mock_adapter_partial,
        ), patch(
            "astrid.packs.generation.actions.generate_image.run.embed_png_text",
        ), patch(
            "astrid.packs.generation.actions.generate_image.run.write_json_atomic",
            side_effect=_capture_partial,
        ):
            with pytest.raises(ValueError, match="fail"):
                generate_image_core(args_partial)

        assert len(captured_partial) == 1
        partial_manifest = captured_partial[0]

        # --- get full success manifest -----------------------------------
        captured_full: list[dict] = []

        def _capture_full(manifest_path, data):
            captured_full.append(data)

        with patch(
            "astrid.packs.generation.actions.generate_image.run.ModelRegistry.load_default",
            return_value=mock_registry,
        ), patch(
            "astrid.packs.generation.actions.generate_image.run."
            "load_default_generation_backend_registry",
            return_value=MagicMock(),
        ), patch(
            "astrid.packs.generation.actions.generate_image.run._create_backend_adapter",
            return_value=mock_adapter_full,
        ), patch(
            "astrid.packs.generation.actions.generate_image.run.embed_png_text",
        ), patch(
            "astrid.packs.generation.actions.generate_image.run.write_json_atomic",
            side_effect=_capture_full,
        ):
            generate_image_core(args_full)

        assert len(captured_full) == 1
        full_manifest = captured_full[0]

        # --- compare shapes: same keys, outputs have same structure ------
        assert set(partial_manifest.keys()) == set(full_manifest.keys()), (
            f"partial keys {set(partial_manifest.keys())} != "
            f"full keys {set(full_manifest.keys())}"
        )
        assert len(partial_manifest["outputs"]) == len(full_manifest["outputs"]) == 1

        partial_out = partial_manifest["outputs"][0]
        full_out = full_manifest["outputs"][0]
        assert set(partial_out.keys()) == set(full_out.keys())
        assert partial_out["path"] == full_out["path"]
        assert "sha256:" in partial_out["content_hash"]
        assert partial_out["bytes"] == full_out["bytes"]
