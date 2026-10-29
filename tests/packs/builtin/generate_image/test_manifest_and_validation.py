"""Executor-manifest + execution validation tests for generation.generate_image.

Covers:
  (b) Invalid execution value — rejected after model/mode lookup with
      pair-specific available backends named.
  (c) Requires violation — i2i without image_ref fails BEFORE HTTP.
  (d) V1 rejection — passing a v1 model-id raises KeyError.
  (e) Edit mode drops negative_prompt with warning (SD-003).
  (f) Edit mode drops strength with warning (SD-003).
  (g) Per-entry model override without mode field rejected (FLAG-004).
  (h) Per-entry model override with mode mismatch rejected (FLAG-004).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from astrid.core.contracts.errors import AstridError
from astrid.core.generation.backends import GenerationResult
from astrid.core.model_catalog.schema import ModeSpec


@pytest.fixture(autouse=True)
def _fake_fal_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAL_KEY", "test-fal-key")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _logging_transport_with_upload(request: object) -> tuple[int, bytes]:
    """Transport that logs calls and handles fal-upload + submission + image download."""
    import json

    url = request.full_url if hasattr(request, "full_url") else str(request)
    method = request.method if hasattr(request, "method") else "GET"
    _logging_transport_with_upload.calls.append(url)  # type: ignore[attr-defined]

    # fal-upload endpoint
    if "fal-upload" in str(url):
        return 200, json.dumps({"url": "https://fal.media/files/mock-upload.png"}).encode("utf-8")

    # fal submission POST — return a valid submit response
    if method == "POST" and "fal.run" in str(url):
        return 200, json.dumps({
            "request_id": "mock-req-001",
            "status_url": "https://queue.fal.run/fal-ai/test/requests/mock-req-001/status",
            "response_url": "https://queue.fal.run/fal-ai/test/requests/mock-req-001",
        }).encode("utf-8")

    # Status poll — return COMPLETED
    if "/status" in str(url):
        return 200, json.dumps({"status": "COMPLETED"}).encode("utf-8")

    # Response poll — return result with images
    if "/requests/" in str(url) and "/status" not in str(url):
        return 200, json.dumps({
            "images": [{"url": "https://fal.media/files/mock-result.png"}],
        }).encode("utf-8")

    # Image download — return tiny PNG
    if method == "GET" and "fal.media" in str(url):
        return 200, (
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
            b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f"
            b"\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
        )

    return 200, b"{}"


_logging_transport_with_upload.calls = []  # type: ignore[attr-defined]


def _assert_astrid_error(call, *cause_parts: str) -> AstridError:
    with pytest.raises(AstridError) as raised:
        call()
    error = raised.value
    for part in cause_parts:
        assert part in error.cause
    return error



def test_bounded_profile_rejects_zero_negative_and_fanout_counts_before_http(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from astrid.packs.generation.actions.generate_image.run import main
    import astrid.core.util.http as http_mod

    instrumented = http_mod.HttpClient(transport=_logging_transport_with_upload)
    monkeypatch.setattr(http_mod, "_default_client", instrumented)
    for count in (0, -1, 2):
        _logging_transport_with_upload.calls.clear()
        with pytest.raises(AstridError, match="one output for the whole task"):
            main(
                [
                    "--model", "z-image",
                    "--mode", "i2i",
                    "--execution", "cloud",
                    "--storage-policy-version", "astrid.cloud-i2i.z-image.v1",
                    "--prompt", "bounded count proof",
                    "--image-ref", str((Path(__file__).parent / "fixtures/input.png").resolve()),
                    "--size", "1024x1024",
                    "--strength", "0.5",
                    "--count", str(count),
                    "--out", str(tmp_path / f"out-{count}"),
                ]
            )
        assert _logging_transport_with_upload.calls == []


def test_generate_core_returns_enriched_generation_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """generate_core preserves manifest writing while returning a typed result."""
    from astrid.packs.generation.actions.generate_image import run as run_mod

    class FakeAdapter:
        def generate(
            self,
            *,
            entry: object,
            mode: str,
            params: dict[str, object],
            out_dir: Path,
        ) -> GenerationResult:
            image_path = out_dir / "generated_001.png"
            image_path.write_bytes(
                b"\x89PNG\r\n\x1a\n"
                b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
                b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f"
                b"\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
            )
            assert mode == "t2i"
            assert params["prompt"] == "red kite"
            assert params["loras"] == ["flux-realism"]
            return GenerationResult(
                image_paths=[image_path],
                seed_used=int(params["seed"]),
                model_actual="fal-ai/flux/dev",
                cost_usd=0.42,
                duration_ms=321,
                applied_features=["prompt", "seed", "loras"],
                request_id="req-123",
                source_urls=["https://example.com/generated_001.png"],
            )

    class FakeBackendRegistry:
        def create(self, execution: str, *, env_file: Path | None) -> FakeAdapter:
            assert execution == "cloud"
            assert env_file is None
            return FakeAdapter()

    embedded_calls: list[tuple[Path, dict[str, str]]] = []

    def _fake_embed_png_text(path: Path, fields: dict[str, str]) -> None:
        embedded_calls.append((path, fields))

    monkeypatch.setattr(
        run_mod,
        "load_default_generation_backend_registry",
        lambda: FakeBackendRegistry(),
    )
    monkeypatch.setattr(run_mod, "embed_png_text", _fake_embed_png_text)

    out = tmp_path / "out"
    result = run_mod.generate_core(
        [
            "--model", "flux-dev",
            "--mode", "t2i",
            "--execution", "cloud",
            "--prompt", "red kite",
            "--out", str(out),
            "--seed", "7",
            "--loras", "flux-realism",
        ]
    )

    manifest_path = out / "manifest.json"
    assert manifest_path.exists()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert result.ok is True
    assert result.path == out / "images" / "generated_001.png"
    assert result.image_paths == [out / "images" / "generated_001.png"]
    # A scalar request echo from a legacy adapter is not output attestation.
    assert result.seed_used is None
    assert result.output_seeds == {}
    assert manifest["seed"] == manifest["request"]["seed"] == 7
    assert manifest["outputs"][0]["seed_used"] is None
    assert result.model_actual == "fal-ai/flux/dev"
    assert result.cost_usd == 0.42
    assert result.duration_ms == 321
    assert result.request_id == "req-123"
    assert result.source_urls == ["https://example.com/generated_001.png"]
    assert result.applied_features == ["prompt", "seed", "loras"]
    assert result.dropped_features == []
    assert result.run_dir == out.resolve()
    assert result.manifest == manifest
    assert manifest["outputs"][0]["path"] == "images/generated_001.png"
    assert manifest["loras"] == ["flux-realism"]
    assert len(embedded_calls) == 1
    embedded_path, embedded_fields = embedded_calls[0]
    assert embedded_path == out / "images" / "generated_001.png"
    assert embedded_fields["prompt"] == "red kite"
    assert embedded_fields["negative_prompt"] == ""
    assert embedded_fields["model"] == "flux-dev"
    assert embedded_fields["model_actual"] == "fal-ai/flux/dev"
    assert embedded_fields["seed"] == "7"
    assert embedded_fields["request_id"] == "req-123"
    assert embedded_fields["loras"] == "['flux-realism']"
    assert embedded_fields["created"]


def test_run_sdk_and_main_preserve_contracts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """run_sdk returns payload data while the CLI entry point stays usable."""
    from astrid.core.generation import GENERATION_RESULT_KEY
    from astrid.packs.generation.actions.generate_image import run as run_mod

    result = GenerationResult(
        image_paths=[tmp_path / "out" / "images" / "generated_001.png"],
        seed_used=11,
        model_actual="fal-ai/flux/dev",
        manifest={"schema_version": 2},
        run_dir=(tmp_path / "out").resolve(),
    )

    monkeypatch.setattr(run_mod, "generate_core", lambda argv=None: result)

    payload = run_mod.run_sdk(["--model", "flux-dev"])
    assert payload["returncode"] == 0
    assert payload[GENERATION_RESULT_KEY] is result

    monkeypatch.setenv("ASTRID_PROJECT_RUN", "test-run")
    code = run_mod.main(["--model", "flux-dev"])
    captured = capsys.readouterr()

    assert code == 0
    assert captured.err == ""
    assert captured.out == f"manifest={result.run_dir / 'manifest.json'}\n"


# ---------------------------------------------------------------------------
# (b) Invalid execution value
# ---------------------------------------------------------------------------


def test_execution_invalid_value(tmp_path: Path) -> None:
    """Invalid --execution is rejected after model/mode lookup with available ids."""
    from astrid.packs.generation.actions.generate_image.run import main

    out = tmp_path / "out"
    _assert_astrid_error(
        lambda: main(
            [
                "--model", "flux-dev",
                "--mode", "t2i",
                "--execution", "both",
                "--prompt", "x",
                "--out", str(out),
            ]
        ),
        "model 'flux-dev' mode 't2i' has no 'both' backend",
    )


def test_execution_invalid_value_lists_pair_specific_backends(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The error names only the backend ids available for the selected pair."""
    from astrid.packs.generation.actions.generate_image.run import main

    out = tmp_path / "out"
    error = _assert_astrid_error(
        lambda: main(
            [
                "--model", "flux-dev",
                "--mode", "t2i",
                "--execution", "local",
                "--prompt", "x",
                "--out", str(out),
            ]
        ),
        "model 'flux-dev' mode 't2i' has no 'local' backend",
    )
    assert error.valid_options == ("cloud", "codex")


def test_registry_lookup_failure_is_reported_as_cli_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Missing registry entries surface as clear CLI errors, not raw exceptions."""
    from astrid.core.generation.backends.registry import GenerationBackendRegistry
    from astrid.packs.generation.actions.generate_image import run as run_mod

    class EmptyBackendRegistry(GenerationBackendRegistry):
        def __init__(self) -> None:
            self._descriptors = {}

    monkeypatch.setattr(
        run_mod,
        "load_default_generation_backend_registry",
        lambda: EmptyBackendRegistry(),
    )

    out = tmp_path / "out"
    _assert_astrid_error(
        lambda: run_mod.main(
            [
                "--model", "flux-dev",
                "--mode", "t2i",
                "--execution", "cloud",
                "--prompt", "x",
                "--out", str(out),
            ]
        ),
        "generation backend 'cloud' is not registered",
    )


# ---------------------------------------------------------------------------
# (c) Requires violation — hard-fail BEFORE any HTTP call
# ---------------------------------------------------------------------------


def test_requires_violation_no_http(tmp_path: Path) -> None:
    """flux-dev --mode i2i without --image-ref exits non-zero BEFORE any HTTP call.

    We inject a transport that records every call and assert the log is empty.
    """
    from astrid.packs.generation.actions.generate_image.run import main

    class CallLog:
        def __init__(self) -> None:
            self.calls: list[object] = []

    log = CallLog()

    def _logging_transport(request: object) -> tuple[int, bytes]:
        url = request.full_url if hasattr(request, "full_url") else ""
        if isinstance(url, str) and "fal-upload" in url:
            return 200, b'{"url": "https://fal.media/files/test-uploaded-image.png"}'
        log.calls.append(request)
        return 200, b"{}"

    # Inject the logging transport into the default (singleton) client *and*
    # also patch default_client to return our instrumented instance.  The
    # executor calls default_client() for cloud execution, so we must ensure
    # that path never fires before the requires check.
    import astrid.core.util.http as http_mod
    original_default = http_mod._default_client
    instrumented = http_mod.HttpClient(transport=_logging_transport)
    http_mod._default_client = instrumented

    try:
        out = tmp_path / "out"
        with pytest.raises(SystemExit) as exc:
            main(
                [
                    "--model", "flux-dev",
                    "--mode", "i2i",
                    "--execution", "cloud",
                    "--prompt", "a test prompt",
                    "--out", str(out),
                ]
            )
        # Should exit non-zero on requires violation (SystemExit carries the
        # error message string as its code — truthy means failure)
        assert exc.value.code, f"Expected non-zero exit, got {exc.value.code!r}"
        # Zero HTTP calls — the requires check happens BEFORE the loop
        assert len(log.calls) == 0, (
            f"Expected zero HTTP calls before requires check, got {len(log.calls)}"
        )
    finally:
        http_mod._default_client = original_default


# ---------------------------------------------------------------------------
# (d) V1 rejection — v1 model-id raises KeyError
# ---------------------------------------------------------------------------


def test_v1_model_id_rejected(tmp_path: Path) -> None:
    """Passing a v1 model-id like 'flux-dev-img2img' raises KeyError.

    In v2, modes are separate from model IDs.  'flux-dev-img2img' is not a
    valid v2 model ID — the correct v2 invocation is 'flux-dev --mode i2i'.
    """
    from astrid.packs.generation.actions.generate_image.run import main

    out = tmp_path / "out"
    # flux-dev-img2img does not exist in the v2 registry — should fail
    _assert_astrid_error(
        lambda: main(
            [
                "--model", "flux-dev-img2img",
                "--mode", "t2i",
                "--execution", "cloud",
                "--prompt", "a test",
                "--out", str(out),
            ]
        ),
        "Unknown model 'flux-dev-img2img'",
    )


# ---------------------------------------------------------------------------
# (e) Edit mode drops negative_prompt with warning
# ---------------------------------------------------------------------------


def test_edit_mode_rejects_negative_prompt(tmp_path: Path) -> None:
    """qwen-image-edit in edit mode drops --negative-prompt as unsupported (SD-003)."""
    import astrid.core.util.http as http_mod
    from astrid.packs.generation.actions.generate_image.run import main

    original_default = http_mod._default_client
    instrumented = http_mod.HttpClient(transport=_logging_transport_with_upload)
    http_mod._default_client = instrumented

    try:
        out = tmp_path / "out"
        ref_image = tmp_path / "ref.png"
        ref_image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
        code = main(
            [
                "--model", "qwen-image-edit",
                "--mode", "edit",
                "--execution", "cloud",
                "--prompt", "replace the background with a forest",
                "--image-ref", str(ref_image),
                "--negative-prompt", "blurry",
                "--out", str(out),
            ]
        )
        assert code == 0

        import json
        manifest_path = out / "manifest.json"
        assert manifest_path.exists()
        manifest = json.loads(manifest_path.read_text())
        assert manifest["schema_version"] == 2
        assert manifest["mode_used"] == "edit"

        # negative_prompt should be dropped with warning
        warning_features = [w["feature"] for w in manifest.get("warnings", [])]
        assert "negative_prompt" in warning_features, (
            f"Expected negative_prompt in warnings, got {warning_features}"
        )
        assert "dropped_features" in manifest
        assert "negative_prompt" in manifest["dropped_features"]
    finally:
        http_mod._default_client = original_default


# ---------------------------------------------------------------------------
# (f) Edit mode drops strength with warning
# ---------------------------------------------------------------------------


def test_edit_mode_rejects_strength(tmp_path: Path) -> None:
    """qwen-image-edit in edit mode drops --strength as unsupported (SD-003)."""
    import astrid.core.util.http as http_mod
    from astrid.packs.generation.actions.generate_image.run import main

    original_default = http_mod._default_client
    instrumented = http_mod.HttpClient(transport=_logging_transport_with_upload)
    http_mod._default_client = instrumented

    try:
        out = tmp_path / "out"
        ref_image = tmp_path / "ref.png"
        ref_image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
        code = main(
            [
                "--model", "qwen-image-edit",
                "--mode", "edit",
                "--execution", "cloud",
                "--prompt", "make it brighter",
                "--image-ref", str(ref_image),
                "--strength", "0.7",
                "--out", str(out),
            ]
        )
        assert code == 0

        import json
        manifest_path = out / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        assert manifest["schema_version"] == 2
        assert manifest["mode_used"] == "edit"

        # strength should be dropped with warning
        warning_features = [w["feature"] for w in manifest.get("warnings", [])]
        assert "strength" in warning_features, (
            f"Expected strength in warnings, got {warning_features}"
        )
        assert "dropped_features" in manifest
        assert "strength" in manifest["dropped_features"]
    finally:
        http_mod._default_client = original_default


# ---------------------------------------------------------------------------
# (g) Per-entry model override without mode field rejected
# ---------------------------------------------------------------------------


def test_per_entry_model_override_without_mode_rejected(tmp_path: Path) -> None:
    """Per-entry model override without 'mode' field is rejected (FLAG-004)."""
    from astrid.packs.generation.actions.generate_image.run import main

    prompts_file = tmp_path / "prompts.jsonl"
    prompts_file.write_text(
        '{"prompt": "test", "model": "z-image"}\n'
    )

    out = tmp_path / "out"
    with pytest.raises(SystemExit) as exc:
        main(
            [
                "--model", "flux-dev",
                "--mode", "t2i",
                "--execution", "cloud",
                "--prompts-file", str(prompts_file),
                "--out", str(out),
            ]
        )
    assert exc.value.code, f"Expected non-zero exit, got {exc.value.code!r}"


# ---------------------------------------------------------------------------
# (h) Per-entry model override with mode mismatch rejected
# ---------------------------------------------------------------------------


def test_per_entry_model_override_mode_mismatch_rejected(tmp_path: Path) -> None:
    """Per-entry model override with mode different from CLI --mode is rejected (FLAG-004)."""
    from astrid.packs.generation.actions.generate_image.run import main

    prompts_file = tmp_path / "prompts.jsonl"
    prompts_file.write_text(
        '{"prompt": "test", "model": "z-image", "mode": "i2i"}\n'
    )

    out = tmp_path / "out"
    with pytest.raises(SystemExit) as exc:
        main(
            [
                "--model", "flux-dev",
                "--mode", "t2i",
                "--execution", "cloud",
                "--prompts-file", str(prompts_file),
                "--out", str(out),
            ]
        )
    assert exc.value.code, f"Expected non-zero exit, got {exc.value.code!r}"


def test_prompt_file_required_and_supported_custom_features_are_merged() -> None:
    """Row-scoped/custom features survive merged validation for prompts-file runs."""
    from astrid.packs.generation.actions.generate_image.run import (
        _build_requested_params,
        _check_required,
        _drop_unsupported,
    )

    args = argparse.Namespace(
        prompt=None,
        negative_prompt=None,
        seed=None,
        count=1,
        size=None,
        image_ref=None,
        strength=None,
        guidance_scale=None,
        steps=None,
    )
    mode_spec = ModeSpec(
        supports=("prompt", "image_ref", "mask_ref"),
        requires=("prompt", "image_ref", "mask_ref"),
        backends={},
    )

    requested = _build_requested_params(
        args,
        prompt_text="row prompt",
        prompt_entry={
            "prompt": "row prompt",
            "image_ref": "row.png",
            "mask_ref": "mask.png",
            "rogue_feature": "ignore-me",
        },
    )
    _check_required(mode_spec, "i2i", "custom-image", requested)
    filtered, warnings, dropped = _drop_unsupported(
        mode_spec,
        "i2i",
        "custom-image",
        requested,
    )

    assert filtered["image_ref"] == "row.png"
    assert filtered["mask_ref"] == "mask.png"
    assert "rogue_feature" not in filtered
    assert set(dropped) == {"count", "rogue_feature"}
    assert {warning["feature"] for warning in warnings} == {"count", "rogue_feature"}


# ---------------------------------------------------------------------------
# LoRA parsing tests
# ---------------------------------------------------------------------------


class TestLoraArgParsing:
    """Tests for _parse_loras_arg in the generate_image executor."""

    def test_empty_loras(self) -> None:
        from astrid.packs.generation.actions.generate_image.run import _parse_loras_arg
        assert _parse_loras_arg(None) == []
        assert _parse_loras_arg("") == []
        assert _parse_loras_arg("  ") == []

    def test_registry_ids(self) -> None:
        from astrid.packs.generation.actions.generate_image.run import _parse_loras_arg
        result = _parse_loras_arg("flux-realism,z-realgen-v2")
        assert result == ["flux-realism", "z-realgen-v2"]

    def test_inline_path_at_scale(self) -> None:
        from astrid.packs.generation.actions.generate_image.run import _parse_loras_arg
        result = _parse_loras_arg("https://example.com/lora.safetensors@0.8")
        assert len(result) == 1
        assert result[0] == {"path": "https://example.com/lora.safetensors", "scale": 0.8}

    def test_mixed_registry_and_inline(self) -> None:
        from astrid.packs.generation.actions.generate_image.run import _parse_loras_arg
        result = _parse_loras_arg("flux-realism,https://x.com/lora.safetensors@0.5")
        assert result == [
            "flux-realism",
            {"path": "https://x.com/lora.safetensors", "scale": 0.5},
        ]

    def test_inline_without_scale_defaults(self) -> None:
        from astrid.packs.generation.actions.generate_image.run import _parse_loras_arg
        result = _parse_loras_arg("https://example.com/lora.safetensors")
        assert result == ["https://example.com/lora.safetensors"]


# ---------------------------------------------------------------------------
# Fal backend LoRA routing tests (unit-level, no network)
# ---------------------------------------------------------------------------


class TestFalLoraRouting:
    """Tests for LoRA routing + validation in FalBackend.generate()."""

    def test_missing_lora_endpoint_raises(self) -> None:
        """When a model has no lora_endpoint, requesting loras raises ValueError."""
        from astrid.core.generation.backends.fal import FalBackend
        from astrid.core.model_catalog.schema import BackendSpec, ModelEntry, ModeSpec
        from astrid.core.util.http import HttpClient

        entry = ModelEntry(
            id="no-lora-model",
            modality="image",
            modes={
                "t2i": ModeSpec(
                    supports=("prompt",),
                    requires=("prompt",),
                    backends={
                        "cloud": BackendSpec(
                            endpoint="fal-ai/test",
                            lora_endpoint="",  # no lora endpoint
                            param_map={"prompt": "prompt"},
                        )
                    },
                )
            },
        )

        class FakeClient(HttpClient):
            def __init__(self) -> None:
                super().__init__()

        backend = FalBackend(client=FakeClient())
        import os
        os.environ["FAL_KEY"] = "test-key"

        with pytest.raises(ValueError, match="has no LoRA endpoint"):
            backend.generate(
                entry=entry,
                mode="t2i",
                params={"prompt": "test", "loras": ["some-id"]},
                out_dir=Path("/tmp"),
            )

    def test_base_mismatch_raises(self) -> None:
        """When a LoRA's base_model != entry.id, ValueError is raised."""
        from astrid.core.generation.backends.fal import FalBackend
        from astrid.core.model_catalog.schema import BackendSpec, ModelEntry, ModeSpec
        from astrid.core.util.http import HttpClient

        entry = ModelEntry(
            id="flux-dev",
            modality="image",
            modes={
                "t2i": ModeSpec(
                    supports=("prompt",),
                    requires=("prompt",),
                    backends={
                        "cloud": BackendSpec(
                            endpoint="fal-ai/flux/dev",
                            lora_endpoint="fal-ai/flux-lora",
                            param_map={"prompt": "prompt"},
                        )
                    },
                )
            },
        )

        class FakeClient(HttpClient):
            def __init__(self) -> None:
                super().__init__()

        backend = FalBackend(client=FakeClient())
        import os
        os.environ["FAL_KEY"] = "test-key"

        # flux-realism is for flux-dev, but we request it on z-image... 
        # Actually, registry lookup will fail first because base_model mismatch.
        # Let's request a LoRA that IS in the registry but for a different model.
        # We use "z-realgen-v2" which has base_model=z-image, not flux-dev.
        with pytest.raises(ValueError, match="does not match"):
            backend.generate(
                entry=entry,
                mode="t2i",
                params={"prompt": "test", "loras": ["z-realgen-v2"]},
                out_dir=Path("/tmp"),
            )
