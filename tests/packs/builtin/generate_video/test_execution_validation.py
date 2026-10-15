"""Execution validation tests for generation.generate_video."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from astrid.core.contracts.errors import AstridError
from astrid.core.execution.executor.schema import load_executor_manifest
from astrid.core.generation.backends.base import GenerationResult
from astrid.core.model_catalog.schema import ModeSpec

_EXECUTOR_YAML = (
    Path(__file__).resolve().parents[4]
    / "astrid/packs/generation/executors/generate_video/executor.yaml"
)


def _assert_astrid_error(call, *cause_parts: str) -> AstridError:
    with pytest.raises(AstridError) as raised:
        call()
    error = raised.value
    for part in cause_parts:
        assert part in error.cause
    return error


def test_manifest_loads() -> None:
    manifest = load_executor_manifest(str(_EXECUTOR_YAML))
    assert manifest.id == "generation.generate_video"
    assert manifest.kind == "built_in"
    assert manifest.version == "2.0"
    assert manifest.metadata["runtime_entrypoint"] == "run_sdk"
    assert manifest.metadata["storage_estimate_required"] is True
    assert manifest.metadata["estimated_scratch_bytes"] > 0
    assert manifest.metadata["estimated_output_bytes"] > 0
    assert manifest.metadata["hc04_cas_param_ports"] == ["image_ref", "image_end_ref"]
    assert manifest.metadata["hc04_optional_cas_param_ports"] == ["image_end_ref"]


def test_generate_core_returns_enriched_generation_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """generate_core preserves video manifest writing and result enrichment."""
    from astrid.packs.iteration.actions.experiment_import import run as run_mod

    class FakeAdapter:
        def generate(
            self,
            *,
            entry: object,
            mode: str,
            params: dict[str, object],
            out_dir: Path,
        ) -> GenerationResult:
            video_path = out_dir / "generated_001.mp4"
            video_path.write_bytes(b"fake-video-bytes")
            assert mode == "i2v"
            assert params["prompt"] == "red kite takes off"
            assert params["frames"] == 48
            assert params["image_ref"] == str(image_ref)
            return GenerationResult(
                image_paths=[video_path],
                seed_used=int(params["seed"]),
                model_actual="fal-ai/wan/v2.2/i2v",
                cost_usd=0.84,
                duration_ms=654,
                applied_features=["prompt", "image_ref", "frames"],
                request_id="req-video-123",
                source_urls=["https://example.com/generated_001.mp4"],
            )

    class FakeBackendRegistry:
        def create(self, execution: str, *, env_file: Path | None) -> FakeAdapter:
            assert execution == "cloud"
            assert env_file is None
            return FakeAdapter()

    monkeypatch.setattr(
        run_mod,
        "load_default_generation_backend_registry",
        lambda: FakeBackendRegistry(),
    )
    from astrid.core.media import MediaProbe

    monkeypatch.setattr(
        run_mod,
        "ffprobe_metadata",
        lambda path: MediaProbe(
            duration_seconds=2.0,
            fps=24.0,
            resolution="1280x720",
            width=1280,
            height=720,
        ),
    )

    image_ref = tmp_path / "first.png"
    image_ref.write_bytes(b"png")
    out = tmp_path / "out"
    result = run_mod.generate_core(
        [
            "--model", "wan-2.2",
            "--mode", "i2v",
            "--execution", "cloud",
            "--prompt", "red kite takes off",
            "--image-ref", str(image_ref),
            "--fps", "24",
            "--duration", "2",
            "--out", str(out),
            "--seed", "7",
        ]
    )

    manifest_path = out / "manifest.json"
    assert manifest_path.exists()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert result.ok is True
    assert result.path == out / "videos" / "generated_001.mp4"
    assert result.image_paths == [out / "videos" / "generated_001.mp4"]
    assert result.video_paths == [out / "videos" / "generated_001.mp4"]
    assert result.seed_used == 7
    assert result.model_actual == "fal-ai/wan/v2.2/i2v"
    assert result.cost_usd == 0.84
    assert result.duration_ms == 654
    assert result.request_id == "req-video-123"
    assert result.source_urls == ["https://example.com/generated_001.mp4"]
    assert result.applied_features == ["prompt", "image_ref", "frames"]
    assert result.dropped_features == ["count", "fps", "duration"]
    assert result.run_dir == out.resolve()
    assert result.manifest == manifest
    assert manifest["request"]["frames"] == 48
    assert manifest["request"]["duration"] == 2.0
    assert manifest["request"]["image_ref_resolved"] == str(image_ref.resolve())
    assert manifest["outputs"][0]["path"] == "videos/generated_001.mp4"
    assert manifest["outputs"][0]["duration_seconds"] == 2.0
    assert manifest["outputs"][0]["fps"] == 24.0
    assert manifest["outputs"][0]["resolution"] == "1280x720"


def test_run_sdk_and_main_preserve_contracts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """run_sdk returns payload data while the CLI entry point stays usable."""
    from astrid.core.generation import GENERATION_RESULT_KEY
    from astrid.packs.iteration.actions.experiment_import import run as run_mod

    result = GenerationResult(
        image_paths=[tmp_path / "out" / "videos" / "generated_001.mp4"],
        seed_used=11,
        model_actual="fal-ai/wan/v2.2/t2v",
        manifest={"schema_version": 2},
        run_dir=(tmp_path / "out").resolve(),
    )

    monkeypatch.setattr(run_mod, "generate_core", lambda argv=None: result)

    payload = run_mod.run_sdk(["--model", "wan-2.2"])
    assert payload["returncode"] == 0
    assert payload[GENERATION_RESULT_KEY] is result

    code = run_mod.main(["--model", "wan-2.2"])
    captured = capsys.readouterr()

    assert code == 0
    assert (
        captured.err
        == "[astrid] running unledgered — use astrid.sdk.invoke to persist a run record\n"
    )
    assert captured.out == f"manifest={result.run_dir / 'manifest.json'}\n"


def test_execution_invalid_value(tmp_path: Path) -> None:
    """Invalid --execution is rejected after model/mode lookup."""
    from astrid.packs.generation.actions.generate_video.run import main

    out = tmp_path / "out"
    _assert_astrid_error(
        lambda: main(
            [
                "--model", "wan-2.2",
                "--mode", "t2v",
                "--execution", "both",
                "--prompt", "x",
                "--out", str(out),
            ]
        ),
        "model 'wan-2.2' mode 't2v' has no 'both' backend",
    )


def test_execution_invalid_value_lists_pair_specific_backends(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The error reports backends for the selected (model, mode) only."""
    from astrid.packs.generation.actions.generate_video.run import main

    out = tmp_path / "out"
    error = _assert_astrid_error(
        lambda: main(
            [
                "--model", "wan-2.2",
                "--mode", "t2v",
                "--execution", "local",
                "--prompt", "x",
                "--out", str(out),
            ]
        ),
        "model 'wan-2.2' mode 't2v' has no 'local' backend",
    )
    assert error.valid_options == ("cloud",)


def test_registry_lookup_failure_is_reported_as_cli_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Missing backend descriptors become CLI errors instead of raw exceptions."""
    from astrid.core.generation.backends.registry import GenerationBackendRegistry
    from astrid.packs.iteration.actions.experiment_import import run as run_mod

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
                "--model", "wan-2.2",
                "--mode", "t2v",
                "--execution", "cloud",
                "--prompt", "x",
                "--out", str(out),
            ]
        ),
        "generation backend 'cloud' is not registered",
    )


def test_video_prompt_file_custom_features_survive_helper_validation() -> None:
    """Video helper validation keeps declared custom features and drops extras."""
    from astrid.packs.generation.actions.generate_video.run import (
        _build_requested_params,
        _check_required,
        _drop_unsupported,
    )

    args = argparse.Namespace(
        prompt=None,
        negative_prompt=None,
        seed=None,
        count=1,
        resolution=None,
        image_ref=None,
        image_end_ref=None,
        frames=None,
        fps=None,
        duration=None,
        guidance_scale=None,
        steps=None,
        shift=None,
        loras=None,
        enable_safety_checker=None,
        enable_prompt_expansion=None,
        acceleration=None,
    )
    mode_spec = ModeSpec(
        supports=("prompt", "image_ref", "story_ref"),
        requires=("prompt", "image_ref", "story_ref"),
        backends={},
    )

    requested = _build_requested_params(
        args,
        prompt_text="row prompt",
        prompt_entry={
            "prompt": "row prompt",
            "image_ref": "first.png",
            "story_ref": "story.json",
            "rogue_feature": "ignore-me",
        },
    )
    _check_required(mode_spec, "i2v", "custom-video", requested)
    filtered, warnings, dropped = _drop_unsupported(
        mode_spec,
        "i2v",
        "custom-video",
        requested,
    )

    assert filtered["image_ref"] == "first.png"
    assert filtered["story_ref"] == "story.json"
    assert "rogue_feature" not in filtered
    assert set(dropped) == {"count", "rogue_feature"}
    assert {warning["feature"] for warning in warnings} == {"count", "rogue_feature"}
