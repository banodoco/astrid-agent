"""Tests for DEFAULT_PARAM_MAP fallback in FalBackend and VibeComfyBackend.

Covers:
  - Fallback path: when BackendSpec.param_map is empty, DEFAULT_PARAM_MAP[mode] is used.
  - Override path: when BackendSpec.param_map is non-empty, it takes precedence.
  - Identical payload to current models.yaml-driven behavior per mode.
"""

from __future__ import annotations

import sys
import hashlib
from pathlib import Path
from unittest.mock import patch

import pytest

from astrid.core.generation.backends.fal import FalBackend
from astrid.core.generation.backends.vibecomfy import VibeComfyBackend
from astrid.core.model_catalog.schema import BackendSpec, ModelEntry, ModeSpec

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _pinned_template_hash(template: str) -> str:
    """Hash the repository template selected by VibeComfy's ready discovery."""
    from vibecomfy.registry.ready import (
        repo_ready_template_discovery,
        resolve_ready_template,
    )

    discovery = repo_ready_template_discovery()
    record = resolve_ready_template(template, discovery)
    return "sha256:" + hashlib.sha256(Path(record.path).read_bytes()).hexdigest()


def _make_fal_entry(
    *,
    model_id: str = "flux-dev",
    mode: str = "t2i",
    endpoint: str = "fal-ai/flux/dev",
    param_map: dict[str, str] | None = None,
    lora_endpoint: str | None = None,
) -> ModelEntry:
    """Build a minimal ModelEntry with a single cloud (fal) backend."""
    return ModelEntry(
        id=model_id,
        modality="image" if mode in ("t2i", "i2i", "edit") else "video",
        modes={
            mode: ModeSpec(
                supports=tuple(param_map.keys()) if param_map else ("prompt", "seed", "size"),
                requires=("prompt",),
                backends={
                    "cloud": BackendSpec(
                        endpoint=endpoint,
                        lora_endpoint=lora_endpoint,
                        param_map=param_map if param_map is not None else {},
                    )
                },
            )
        },
    )


def _make_vibecomfy_entry(
    *,
    model_id: str = "z-image",
    mode: str = "t2i",
    template: str = "image/z_image",
    param_map: dict[str, str] | None = None,
) -> ModelEntry:
    """Build a minimal ModelEntry with a single local (vibecomfy) backend."""
    return ModelEntry(
        id=model_id,
        modality="image" if mode in ("t2i", "i2i", "edit") else "video",
        modes={
            mode: ModeSpec(
                supports=tuple(param_map.keys()) if param_map else ("prompt", "seed", "size"),
                requires=("prompt",),
                backends={
                    "local": BackendSpec(
                        template=template,
                        template_hash=_pinned_template_hash(template),
                        param_map=param_map if param_map is not None else {},
                    )
                },
            )
        },
    )


# ---------------------------------------------------------------------------
# FalBackend DEFAULT_PARAM_MAP tests
# ---------------------------------------------------------------------------


class TestFalParamMapFallback:
    """FalBackend.generate() param_map fallback behaviour."""

    def test_fallback_uses_default_for_empty_param_map(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When BackendSpec.param_map is empty, DEFAULT_PARAM_MAP['t2i'] is used."""
        from astrid.core.generation.backends import fal as fal_mod

        monkeypatch.setenv("FAL_KEY", "test-key")
        out_dir = tmp_path / "out"

        # Build entry with empty param_map
        entry = _make_fal_entry(mode="t2i", param_map={})

        fal_result = {
            "images": [{"url": "https://example.com/output_000.png"}],
            "cost": 0.05,
            "request_id": "req-123",
        }

        with patch.object(fal_mod, "fal_submit_and_poll", return_value=fal_result):
            with patch.object(
                fal_mod.FalBackend,
                "_resolve_api_key",
                return_value="test-key",
            ):
                backend = FalBackend()
                with patch.object(
                    backend._client, "get_bytes", return_value=b"\x89PNG\r\n\x1a\n"
                ):
                    gen_result = backend.generate(
                        entry=entry,
                        mode="t2i",
                        params={"prompt": "test", "seed": 42, "size": "1024x1024"},
                        out_dir=out_dir,
                    )

        assert gen_result.error is None
        assert len(gen_result.image_paths) == 1
        assert gen_result.cost_usd == 0.05

    def test_fallback_t2i_prompt_mapping(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """DEFAULT_PARAM_MAP['t2i']['prompt'] -> 'prompt' is used correctly."""
        from astrid.core.generation.backends import fal as fal_mod

        monkeypatch.setenv("FAL_KEY", "test-key")
        out_dir = tmp_path / "out"

        entry = _make_fal_entry(mode="t2i", param_map={})

        captured_payload: dict = {}

        def capture_submit(client, endpoint, payload, api_key):
            captured_payload.update(payload)
            return {
                "images": [{"url": "https://example.com/output_000.png"}],
            }

        with patch.object(fal_mod, "fal_submit_and_poll", side_effect=capture_submit):
            with patch.object(
                fal_mod.FalBackend, "_resolve_api_key", return_value="test-key"
            ):
                backend = FalBackend()
                with patch.object(
                    backend._client, "get_bytes", return_value=b"\x89PNG\r\n\x1a\n"
                ):
                    backend.generate(
                        entry=entry,
                        mode="t2i",
                        params={
                            "prompt": "a cat",
                            "seed": 123,
                            "size": "512x512",
                            "guidance_scale": 7.5,
                            "steps": 20,
                        },
                        out_dir=out_dir,
                    )

        # Default t2i mapping: prompt->prompt, seed->seed, size->image_size, etc.
        assert captured_payload.get("prompt") == "a cat"
        assert captured_payload.get("seed") == 123
        assert captured_payload.get("image_size") == "512x512"
        assert captured_payload.get("guidance_scale") == 7.5
        assert captured_payload.get("num_inference_steps") == 20

    def test_fallback_i2i_mapping(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """DEFAULT_PARAM_MAP['i2i'] maps image_ref->image_url."""
        from astrid.core.generation.backends import fal as fal_mod

        monkeypatch.setenv("FAL_KEY", "test-key")
        out_dir = tmp_path / "out"

        entry = _make_fal_entry(mode="i2i", param_map={})
        source = tmp_path / "ref.png"
        source.write_bytes(b"\x89PNG\r\n\x1a\n")

        captured_payload: dict = {}

        def capture_submit(client, endpoint, payload, api_key):
            captured_payload.update(payload)
            return {"images": [{"url": "https://example.com/output_000.png"}]}

        with patch.object(fal_mod, "fal_submit_and_poll", side_effect=capture_submit):
            with patch.object(
                fal_mod.FalBackend, "_resolve_api_key", return_value="test-key"
            ):
                backend = FalBackend()
                with patch.object(
                    backend._client, "get_bytes", return_value=b"\x89PNG\r\n\x1a\n"
                ):
                    backend.generate(
                        entry=entry,
                        mode="i2i",
                        params={
                            "prompt": "enhance",
                            "seed": 42,
                            "image_ref": str(source),
                            "strength": 0.7,
                        },
                        out_dir=out_dir,
                    )

        assert str(captured_payload.get("image_url")).startswith("data:image/png;base64,")
        assert captured_payload.get("strength") == 0.7

    def test_fallback_t2v_mapping(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """DEFAULT_PARAM_MAP['t2v'] maps resolution->aspect_ratio, frames->num_frames."""
        from astrid.core.generation.backends import fal as fal_mod

        monkeypatch.setenv("FAL_KEY", "test-key")
        out_dir = tmp_path / "out"

        entry = _make_fal_entry(mode="t2v", param_map={})

        captured_payload: dict = {}

        def capture_submit(client, endpoint, payload, api_key):
            captured_payload.update(payload)
            return {"video": {"url": "https://example.com/output.mp4"}}

        with patch.object(fal_mod, "fal_submit_and_poll", side_effect=capture_submit):
            with patch.object(
                fal_mod.FalBackend, "_resolve_api_key", return_value="test-key"
            ):
                backend = FalBackend()
                with patch.object(
                    backend._client, "get_bytes", return_value=b"\x00\x00\x00\x1cftypmp42"
                ):
                    backend.generate(
                        entry=entry,
                        mode="t2v",
                        params={
                            "prompt": "a sunset",
                            "seed": 99,
                            "resolution": "1280x720",
                            "frames": 81,
                            "fps": 24,
                        },
                        out_dir=out_dir,
                    )

        assert captured_payload.get("aspect_ratio") == "1280x720"
        assert captured_payload.get("num_frames") == 81
        assert captured_payload.get("fps") == 24

    def test_fallback_flf_mapping(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """DEFAULT_PARAM_MAP['flf'] maps image_end_ref->end_image_url."""
        from astrid.core.generation.backends import fal as fal_mod

        monkeypatch.setenv("FAL_KEY", "test-key")
        out_dir = tmp_path / "out"

        entry = _make_fal_entry(mode="flf", param_map={})
        start = tmp_path / "start.png"
        end = tmp_path / "end.png"
        start.write_bytes(b"\x89PNG\r\n\x1a\nstart")
        end.write_bytes(b"\x89PNG\r\n\x1a\nend")

        captured_payload: dict = {}

        def capture_submit(client, endpoint, payload, api_key):
            captured_payload.update(payload)
            return {"video": {"url": "https://example.com/output.mp4"}}

        with patch.object(fal_mod, "fal_submit_and_poll", side_effect=capture_submit):
            with patch.object(
                fal_mod.FalBackend, "_resolve_api_key", return_value="test-key"
            ):
                backend = FalBackend()
                with patch.object(
                    backend._client, "get_bytes", return_value=b"\x00\x00\x00\x1cftypmp42"
                ):
                    backend.generate(
                        entry=entry,
                        mode="flf",
                        params={
                            "prompt": "morph",
                            "seed": 7,
                            "image_ref": str(start),
                            "image_end_ref": str(end),
                            "frames": 49,
                        },
                        out_dir=out_dir,
                    )

        assert str(captured_payload.get("image_url")).startswith("data:image/png;base64,")
        assert str(captured_payload.get("end_image_url")).startswith("data:image/png;base64,")

    def test_override_takes_precedence_over_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When BackendSpec.param_map is non-empty, it wins over DEFAULT_PARAM_MAP."""
        from astrid.core.generation.backends import fal as fal_mod

        monkeypatch.setenv("FAL_KEY", "test-key")
        out_dir = tmp_path / "out"

        # Custom param_map: "size" -> "dimensions" (overrides default image_size)
        custom_map = {
            "prompt": "text",
            "seed": "random_seed",
            "size": "dimensions",
        }
        entry = _make_fal_entry(mode="t2i", param_map=custom_map)

        captured_payload: dict = {}

        def capture_submit(client, endpoint, payload, api_key):
            captured_payload.update(payload)
            return {"images": [{"url": "https://example.com/output_000.png"}]}

        with patch.object(fal_mod, "fal_submit_and_poll", side_effect=capture_submit):
            with patch.object(
                fal_mod.FalBackend, "_resolve_api_key", return_value="test-key"
            ):
                backend = FalBackend()
                with patch.object(
                    backend._client, "get_bytes", return_value=b"\x89PNG\r\n\x1a\n"
                ):
                    backend.generate(
                        entry=entry,
                        mode="t2i",
                        params={"prompt": "test", "seed": 1, "size": "256x256"},
                        out_dir=out_dir,
                    )

        # Override wins: "text", "random_seed", "dimensions"
        assert captured_payload.get("text") == "test"
        assert captured_payload.get("random_seed") == 1
        assert captured_payload.get("dimensions") == "256x256"
        # Default keys should NOT be present
        assert "prompt" not in captured_payload
        assert "image_size" not in captured_payload

    def test_unknown_mode_falls_back_to_empty_map(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When mode is not in DEFAULT_PARAM_MAP, the fallback is an empty dict."""
        from astrid.core.generation.backends import fal as fal_mod

        monkeypatch.setenv("FAL_KEY", "test-key")
        out_dir = tmp_path / "out"

        entry = _make_fal_entry(mode="unmapped", param_map={})

        captured_payload: dict = {}

        def capture_submit(client, endpoint, payload, api_key):
            captured_payload.update(payload)
            return {"images": [{"url": "https://example.com/output_000.png"}]}

        with patch.object(fal_mod, "fal_submit_and_poll", side_effect=capture_submit):
            with patch.object(
                fal_mod.FalBackend, "_resolve_api_key", return_value="test-key"
            ):
                backend = FalBackend()
                with patch.object(
                    backend._client, "get_bytes", return_value=b"\x89PNG\r\n\x1a\n"
                ):
                    backend.generate(
                        entry=entry,
                        mode="unmapped",
                        params={"prompt": "test", "seed": 1},
                        out_dir=out_dir,
                    )

        # No param_map entries -> payload should be empty (except flux-schnell/ideogram)
        assert "prompt" not in captured_payload


# ---------------------------------------------------------------------------
# VibeComfyBackend DEFAULT_PARAM_MAP tests
# ---------------------------------------------------------------------------


@pytest.mark.integration  # needs vibecomfy from a second checkout
class TestVibeComfyParamMapFallback:
    """VibeComfyBackend.generate() param_map fallback behaviour.

    Tests use ``sys.modules`` patching because ``generate()`` performs
    lazy imports of ``vibecomfy.runtime.run.run_sync`` and
    ``vibecomfy.registry.ready.workflow_from_ready`` inside the method body,
    which re-binds local names from submodule filesystem resolution.
    """

    @staticmethod
    def _mock_vibecomfy_imports(wf):
        """Patch the vibecomfy imports that happen inside generate()."""
        # The generate() method does:
        #   from vibecomfy.registry.ready import workflow_from_ready
        #   from vibecomfy.runtime.run import run_sync
        # We need to patch the source modules before generate() runs.

        # Import the submodules explicitly so they are in sys.modules to patch
        # (``import vibecomfy`` alone does not import them).
        # ``vibecomfy.runtime.run`` is shadowed by a function of the same name
        # on the package, so fetch the module itself rather than ``import ... as``.
        import importlib

        ready_mod = importlib.import_module("vibecomfy.registry.ready")
        run_mod = importlib.import_module("vibecomfy.runtime.run")

        patch.object(ready_mod, "workflow_from_ready", return_value=wf).start()
        patch.object(run_mod, "run_sync", return_value=wf).start()

    @staticmethod
    def _unmock_vibecomfy_imports():
        """Stop all active patches from _mock_vibecomfy_imports."""
        patch.stopall()

    def test_fallback_uses_default_t2i_for_empty_param_map(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When BackendSpec.param_map is empty, DEFAULT_PARAM_MAP['t2i'] drives set_input."""
        entry = _make_vibecomfy_entry(mode="t2i", param_map={})
        out_dir = tmp_path / "out"

        wf = _MockWorkflow()
        self._mock_vibecomfy_imports(wf)

        try:
            backend = VibeComfyBackend()
            backend.generate(
                entry=entry,
                mode="t2i",
                params={
                    "prompt": "a cat",
                    "negative_prompt": "bad",
                    "seed": 42,
                    "size": "512x512",
                    "guidance_scale": 7.0,
                    "steps": 30,
                },
                out_dir=out_dir,
            )
        finally:
            self._unmock_vibecomfy_imports()

        # Default t2i mapping: prompt->prompt, negative_prompt->negative_prompt,
        # seed->seed, guidance_scale->guidance, steps->steps
        # (size and count are handled specially, not via param_map pass-through)
        assert wf.get_input("prompt") == "a cat"
        assert wf.get_input("negative_prompt") == "bad"
        assert wf.get_input("seed") == 42
        assert wf.get_input("guidance") == 7.0
        assert wf.get_input("steps") == 30

    def test_fallback_uses_default_i2i_mapping(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """DEFAULT_PARAM_MAP['i2i'] maps strength->denoise."""
        entry = _make_vibecomfy_entry(mode="i2i", param_map={})
        out_dir = tmp_path / "out"

        wf = _MockWorkflow()
        self._mock_vibecomfy_imports(wf)

        try:
            backend = VibeComfyBackend()
            backend.generate(
                entry=entry,
                mode="i2i",
                params={
                    "prompt": "enhance",
                    "seed": 1,
                    "image_ref": "/tmp/ref.png",
                    "strength": 0.6,
                },
                out_dir=out_dir,
            )
        finally:
            self._unmock_vibecomfy_imports()

        assert wf.get_input("strength") is None  # NOT the remote name
        assert wf.get_input("image_ref") == "/tmp/ref.png"

    def test_fallback_uses_default_flf_mapping(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """DEFAULT_PARAM_MAP['flf'] maps image_ref->start_image, image_end_ref->end_image."""
        entry = _make_vibecomfy_entry(mode="flf", param_map={})
        out_dir = tmp_path / "out"

        wf = _MockWorkflow()
        self._mock_vibecomfy_imports(wf)

        try:
            backend = VibeComfyBackend()
            backend.generate(
                entry=entry,
                mode="flf",
                params={
                    "prompt": "morph",
                    "seed": 3,
                    "image_ref": "/tmp/start.png",
                    "image_end_ref": "/tmp/end.png",
                    "resolution": "1280x720",
                    "frames": 49,
                    "fps": 24,
                },
                out_dir=out_dir,
            )
        finally:
            self._unmock_vibecomfy_imports()

        # flf default: image_ref->start_image, image_end_ref->end_image
        assert wf.get_input("start_image") == "/tmp/start.png"
        assert wf.get_input("end_image") == "/tmp/end.png"
        assert wf.get_input("prompt") == "morph"
        assert wf.get_input("seed") == 3

    def test_override_takes_precedence_over_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When BackendSpec.param_map is non-empty, it wins over DEFAULT_PARAM_MAP."""
        # Custom mapping: prompt->text_input (overrides default prompt->prompt)
        custom_map = {"prompt": "text_input", "seed": "my_seed"}
        entry = _make_vibecomfy_entry(mode="t2i", param_map=custom_map)
        out_dir = tmp_path / "out"

        wf = _MockWorkflow()
        self._mock_vibecomfy_imports(wf)

        try:
            backend = VibeComfyBackend()
            backend.generate(
                entry=entry,
                mode="t2i",
                params={"prompt": "test", "seed": 99, "size": "256x256"},
                out_dir=out_dir,
            )
        finally:
            self._unmock_vibecomfy_imports()

        # Override wins: text_input, my_seed
        assert wf.get_input("text_input") == "test"
        assert wf.get_input("my_seed") == 99
        # Default key should NOT be set
        assert wf.get_input("prompt") is None

    def test_t2v_default_mapping(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """DEFAULT_PARAM_MAP['t2v'] for VibeComfy maps resolution->resolution, frames->frames."""
        entry = _make_vibecomfy_entry(mode="t2v", param_map={})
        out_dir = tmp_path / "out"

        wf = _MockWorkflow()
        self._mock_vibecomfy_imports(wf)

        try:
            backend = VibeComfyBackend()
            backend.generate(
                entry=entry,
                mode="t2v",
                params={
                    "prompt": "sunset",
                    "negative_prompt": "blur",
                    "seed": 5,
                    "resolution": "1280x720",
                    "frames": 81,
                    "fps": 24,
                },
                out_dir=out_dir,
            )
        finally:
            self._unmock_vibecomfy_imports()

        # resolution is handled specially (split to width/height), so not directly set
        assert wf.get_input("prompt") == "sunset"
        assert wf.get_input("negative_prompt") == "blur"
        assert wf.get_input("seed") == 5
        # frames and fps are set via param_map
        assert wf.get_input("frames") == 81
        assert wf.get_input("fps") == 24


# ---------------------------------------------------------------------------
# Mock workflow for VibeComfy tests
# ---------------------------------------------------------------------------


class _MockWorkflow:
    """Minimal mock of a vibecomfy workflow for testing set_input."""

    def __init__(self) -> None:
        self._inputs: dict[str, object] = {}
        self.nodes: dict[str, object] = {}
        self.metadata: dict[str, object] = {"unbound_inputs": {}}
        self.outputs: list[str] = []

    def set_input(self, name: str, value: object) -> None:
        self._inputs[name] = value
        # Remove from unbound_inputs to mark as bound
        self.metadata["unbound_inputs"].pop(name, None)

    def get_input(self, name: str) -> object:
        return self._inputs.get(name)
