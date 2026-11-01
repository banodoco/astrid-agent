"""Read-only local generation readiness checks."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from astrid.core.generation.preflight import check_local_generation_readiness
from astrid.core.model_catalog.registry import ModelRegistry


@pytest.fixture
def z_image_entry() -> Any:
    return ModelRegistry.load_default().get("z-image")


def test_missing_vibecomfy_is_actionable_and_does_not_probe_comfyui(
    z_image_entry: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from astrid.core.generation import preflight

    checked: list[str] = []

    def fake_module_available(name: str) -> bool:
        checked.append(name)
        return False

    monkeypatch.setattr(preflight, "_module_available", fake_module_available)
    result = check_local_generation_readiness(z_image_entry, "t2i")

    assert result.ready is False
    assert "vibecomfy" in result.reason
    assert "VibeComfy ships with Astrid" in result.recovery_command
    assert "curl -fsSL https://astrid.haus/install | sh -s -- --update" in result.recovery_command
    assert "install.ps1))) --update" in result.recovery_command
    assert "outside chat" in result.recovery_command
    assert "pip" not in result.recovery_command
    assert checked == ["vibecomfy"]


def test_missing_comfyui_runtime_is_actionable(
    z_image_entry: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from astrid.core.generation import preflight

    monkeypatch.setattr(
        preflight,
        "_module_available",
        lambda name: name == "vibecomfy",
    )
    monkeypatch.setattr(preflight.shutil, "which", lambda name: None)

    result = check_local_generation_readiness(z_image_entry, "t2i")

    assert result.ready is False
    assert "ComfyUI runtime" in result.reason
    assert "existing ComfyUI" in result.recovery_command
    assert "vibecomfy.run" in result.recovery_command
    assert "pip" not in result.recovery_command


def test_ready_structural_local_path_requires_no_network_probe(
    z_image_entry: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from astrid.core.generation import preflight

    monkeypatch.setattr(preflight, "_module_available", lambda _name: True)
    monkeypatch.setattr(preflight.shutil, "which", lambda _name: None)
    result = check_local_generation_readiness(z_image_entry, "t2i")

    assert result.ready is True


def test_typed_local_generation_rejects_before_invoke_and_output_staging(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import astrid
    import astrid.sdk as sdk
    from astrid.core.generation import preflight

    monkeypatch.setattr(preflight, "_module_available", lambda _name: False)
    invoked = False

    def unexpected_invoke(*_args: Any, **_kwargs: Any) -> Any:
        nonlocal invoked
        invoked = True
        raise AssertionError("local readiness must fail before SDK invoke")

    monkeypatch.setattr(sdk, "invoke", unexpected_invoke)
    out = tmp_path / "out"
    with pytest.raises(astrid.CapabilityPreconditionError, match="vibecomfy"):
        astrid.generate.image(
            model="z-image",
            mode="t2i",
            execution="local",
            project="demo",
            out=out,
            prompt="a red paper boat",
        )

    assert invoked is False
    assert not out.exists()


def test_typed_local_generation_admits_when_structural_runtime_is_ready(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import astrid
    import astrid.sdk as sdk
    from astrid.core.generation import GENERATION_RESULT_KEY, preflight
    from astrid.core.generation.backends.base import GenerationResult

    monkeypatch.setattr(preflight, "_module_available", lambda _name: True)
    monkeypatch.setattr(preflight.shutil, "which", lambda _name: None)
    invoked = False

    def fake_invoke(capability_id: str, **_kwargs: Any) -> Any:
        nonlocal invoked
        invoked = True
        raw = GenerationResult(run_dir=tmp_path).to_dict()
        return astrid.InvocationResult(
            capability_id=capability_id,
            capability_type="executor",
            native_kind="built_in",
            ok=True,
            raw_result={
                "payload": {GENERATION_RESULT_KEY: raw, "returncode": 0},
            },
        )

    monkeypatch.setattr(sdk, "invoke", fake_invoke)
    result = astrid.generate.image(
        model="z-image",
        mode="t2i",
        execution="local",
        project="demo",
        out=tmp_path,
        prompt="a red paper boat",
    )

    assert result.ok is True
    assert invoked is True


@pytest.mark.parametrize("dry_run", [True, False])
def test_video_flf_missing_end_frame_is_rejected_before_invoke(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    dry_run: bool,
) -> None:
    """FLF's defining end frame has identical dry/live preflight semantics."""

    import astrid
    import astrid.sdk as sdk

    def unexpected_invoke(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("invalid FLF request must not reach invoke")

    monkeypatch.setattr(sdk, "invoke", unexpected_invoke)
    with pytest.raises(astrid.CapabilityMissingInputError, match="image_end_ref"):
        astrid.generate.video(
            model="ltx-2.3",
            mode="flf",
            execution="local",
            project="demo",
            project_root=tmp_path,
            out=tmp_path / "out",
            prompt="a red paper boat",
            image_ref="start.png",
            dry_run=dry_run,
        )
    assert not (tmp_path / "out").exists()


def test_generic_audio_matrix_rejects_local_before_dry_run_runner(
    tmp_path: Path,
) -> None:
    """A cloud-only model/backend cell fails before generic admission too."""

    import astrid.sdk as sdk

    with pytest.raises(sdk.CapabilityValidationError, match="Available backends: cloud"):
        sdk.invoke(
            "generation.generate_audio",
            kind="executor",
            project="demo",
            project_root=tmp_path,
            dry_run=True,
            inputs={
                "model": "stable-audio-3-medium",
                "mode": "music",
                "execution": "local",
                "prompt": "gentle water splash",
            },
        )
    assert not (tmp_path / ".astrid" / "astrid.sqlite3").exists()


def test_audio_facade_is_symmetric_and_forwards_resolved_inputs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import astrid
    import astrid.sdk as sdk
    from astrid.core.generation import GENERATION_RESULT_KEY
    from astrid.core.generation.backends.base import GenerationResult

    seen: dict[str, Any] = {}

    def fake_invoke(capability_id: str, **kwargs: Any) -> Any:
        seen["capability_id"] = capability_id
        seen["kwargs"] = kwargs
        return astrid.InvocationResult(
            capability_id=capability_id,
            capability_type="executor",
            native_kind="built_in",
            ok=True,
            raw_result={
                "payload": {
                    GENERATION_RESULT_KEY: GenerationResult(
                        run_dir=tmp_path,
                    ).to_dict(),
                    "returncode": 0,
                }
            },
        )

    monkeypatch.setattr(sdk, "invoke", fake_invoke)
    result = astrid.generate.audio(
        model="stable-audio-3-medium",
        execution="cloud",
        project="demo",
        out=tmp_path,
        prompt="gentle water splash",
        duration=2.0,
        dry_run=True,
    )
    assert result.ok is True
    assert seen["capability_id"] == "generation.generate_audio"
    assert seen["kwargs"]["inputs"]["mode"] == "music"
    assert seen["kwargs"]["inputs"]["execution"] == "cloud"
