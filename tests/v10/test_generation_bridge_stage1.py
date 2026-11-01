"""Stage 1-native generation-to-runtime bridge contracts."""

from __future__ import annotations

from pathlib import Path

import pytest

from astrid.core.execution.executor.registry import load_default_registry
from astrid.core.execution.executor.runner import ExecutorRunRequest, build_executor_command
from astrid.packs.generation.actions.generate_image.run import build_parser
from astrid.packs.generation.actions.generate_image.task_adapter import (
    GenerateImageAdapterError,
    _decode_inputs,
    validate_shot_generation_recipe,
)
from astrid.core.shots.dependencies import analyze_invalidation
from astrid.sdk import get_capability
from astrid.sdk.invocation import _manifest_preview_command


def _recipe() -> dict:
    return {
        "schema": "astrid.shot-generation-recipe/v1",
        "project_id": "project-1",
        "shot_id": "shot-02",
        "target_role": "primary_visual",
        "prompt_binding": {
            "id": "binding-1",
            "head": 3,
            "media_id": "prompt-media",
            "content_sha256": "a" * 64,
        },
        "generator": {
            "capability_id": "generation.generate_image",
            "model": "z-image",
            "backend": "cloud",
            "mode": "t2i",
            "settings": {"seed": 42},
        },
        "inputs": [
            {
                "ordinal": 0,
                "role": "character",
                "reference_id": "reference-1",
                "media_id": "reference-media",
                "content_sha256": "b" * 64,
            }
        ],
        "parent_media_id": "parent-media",
        "parent_content_sha256": "c" * 64,
    }


@pytest.mark.parametrize(
    "path",
    [
        ("extra",),
        ("prompt_binding", "extra"),
        ("generator", "extra"),
        ("generator", "settings", "extra"),
        ("inputs", 0, "extra"),
    ],
)
def test_shot_recipe_is_closed_at_every_nested_boundary(path: tuple[object, ...]) -> None:
    recipe = _recipe()
    target = recipe
    for key in path[:-1]:
        target = target[key]  # type: ignore[index]
    target[path[-1]] = True  # type: ignore[index]
    with pytest.raises(GenerateImageAdapterError, match="unknown key"):
        validate_shot_generation_recipe(recipe)


@pytest.mark.parametrize(
    ("field", "value"),
    [("model", "other-model"), ("mode", "i2i"), ("backend", "local")],
)
def test_shot_recipe_matches_resolved_identity(field: str, value: str) -> None:
    recipe = _recipe()
    with pytest.raises(GenerateImageAdapterError, match="does not match"):
        validate_shot_generation_recipe(
            recipe,
            model=recipe["generator"]["model"] if field != "model" else value,
            mode=recipe["generator"]["mode"] if field != "mode" else value,
            execution=recipe["generator"]["backend"] if field != "backend" else value,
        )


def test_shot_recipe_rejects_setting_and_ordinal_mismatch() -> None:
    with pytest.raises(GenerateImageAdapterError, match="resolved setting"):
        validate_shot_generation_recipe(_recipe(), resolved_settings={"seed": 43})
    recipe = _recipe()
    recipe["inputs"][0]["ordinal"] = 1
    with pytest.raises(GenerateImageAdapterError, match="ordinals"):
        validate_shot_generation_recipe(recipe)


@pytest.mark.parametrize("path", [("prompt_binding", "content_sha256"), ("inputs", 0, "content_sha256"), ("parent_content_sha256",)])
def test_shot_recipe_rejects_non_sha256_content_identity(path: tuple[object, ...]) -> None:
    recipe = _recipe()
    target = recipe
    for key in path[:-1]:
        target = target[key]  # type: ignore[index]
    target[path[-1]] = "not-a-sha"  # type: ignore[index]
    with pytest.raises(GenerateImageAdapterError, match="SHA-256"):
        validate_shot_generation_recipe(recipe)


def test_task_adapter_and_cli_preserve_recipe_without_mutation() -> None:
    recipe = _recipe()
    spec = {
        "model": "z-image", "mode": "t2i", "execution": "cloud",
        "prompt": "a boat", "seed": 42, "shot_generation_recipe": recipe,
    }
    decoded = _decode_inputs(spec, project_id="project-1")
    assert decoded.shot_generation_recipe == recipe
    assert "--shot-generation-recipe" in decoded.to_argv(Path("/tmp/stage"))
    parsed = build_parser().parse_args(
        ["--model", "z-image", "--mode", "t2i", "--execution", "cloud",
         "--prompt", "a boat", "--shot-generation-recipe", '{"schema": "bad"}']
    )
    assert parsed.shot_generation_recipe == {"schema": "bad"}


def test_recipe_round_trips_through_normal_executor_argv() -> None:
    recipe = _recipe()
    command = build_executor_command(
        ExecutorRunRequest(
            executor_id="generation.generate_image",
            out="/tmp/stage",
            inputs={
                "model": "z-image",
                "mode": "t2i",
                "execution": "cloud",
                "prompt": "a boat",
                "seed": 42,
                "shot_generation_recipe": recipe,
            },
        ),
        load_default_registry(),
    )
    parsed = build_parser().parse_args(list(command[command.index("-m") + 2:]))
    assert parsed.shot_generation_recipe == recipe


def test_recipe_round_trips_through_generic_dry_run_argv() -> None:
    recipe = _recipe()
    capability = get_capability("generation.generate_image")
    command = _manifest_preview_command(
        capability,
        inputs={
            "model": "z-image",
            "mode": "t2i",
            "execution": "cloud",
            "prompt": "a boat",
            "seed": 42,
            "shot_generation_recipe": recipe,
        },
        outputs=None,
        brief=None,
        python_exec="python",
        out="/tmp/stage",
    )
    parsed = build_parser().parse_args(command[command.index("-m") + 2:])
    assert parsed.shot_generation_recipe == recipe


def test_image_facade_forwards_recipe_and_keeps_promptless_upscale(monkeypatch, tmp_path: Path) -> None:
    import astrid
    import astrid.sdk as sdk
    from astrid.core.generation import GENERATION_RESULT_KEY
    from astrid.core.generation.backends.base import GenerationResult

    seen: list[dict] = []

    def fake_invoke(capability_id: str, **kwargs):
        seen.append({"capability_id": capability_id, **kwargs})
        return astrid.InvocationResult(
            capability_id=capability_id, capability_type="executor", native_kind="built_in",
            ok=True, raw_result={"payload": {GENERATION_RESULT_KEY: GenerationResult(run_dir=tmp_path).to_dict(), "returncode": 0}},
        )

    monkeypatch.setattr(sdk, "invoke", fake_invoke)
    astrid.generate.image(
        model="z-image", mode="t2i", execution="cloud", project="demo",
        prompt="a boat", seed=42, shot_generation_recipe=_recipe(), out=tmp_path,
    )
    assert seen[-1]["inputs"]["shot_generation_recipe"] == _recipe()
    astrid.generate.image(
        model="seedvr2-upscaler", mode="upscale", execution="cloud",
        project="demo", image_ref="input.png", out=tmp_path,
    )
    assert seen[-1]["inputs"]["mode"] == "upscale"
    assert "prompt" not in seen[-1]["inputs"]


def test_generic_invoke_validates_and_forwards_recipe(monkeypatch, tmp_path: Path) -> None:
    import astrid
    import astrid.sdk.invocation as invocation

    captured: dict = {}

    def fake_preview(capability, **kwargs):
        captured.update(kwargs)
        return ({"outputs": {}, "payload": {}}, True)

    monkeypatch.setattr(invocation, "_manifest_dry_run_result", fake_preview)
    result = astrid.invoke(
        "generation.generate_image", kind="executor", project="demo",
        inputs={"model": "z-image", "mode": "t2i", "execution": "cloud",
                "prompt": "a boat", "seed": 42, "shot_generation_recipe": _recipe()},
        dry_run=True, project_root=tmp_path,
    )
    assert result.ok is True
    assert captured["inputs"]["shot_generation_recipe"] == _recipe()


def test_invalidation_reaches_fixed_point_independent_of_input_order() -> None:
    items = [
        {"id": "proxy", "media_id": "proxy-media", "metadata": {"kind": "proxy", "source_item_id": "plate", "source_media_id": "plate-media", "source_content_sha256": "plate-hash"}},
        {"id": "plate", "media_id": "plate-media", "metadata": {"kind": "plate", "source_item_id": "old", "source_media_id": "old-media", "source_content_sha256": "old-hash"}},
        {"id": "old", "media_id": "old-media", "metadata": {"role": "primary_visual", "status": "superseded"}},
        {"id": "new", "media_id": "new-media", "metadata": {"role": "primary_visual", "status": "primary"}},
    ]
    media = [{"id": "old-media", "content_hash": "old-hash"}, {"id": "new-media", "content_hash": "new-hash"}, {"id": "plate-media", "content_hash": "plate-hash"}]
    first = analyze_invalidation(items, media)
    second = analyze_invalidation(list(reversed(items)), list(reversed(media)))
    assert first == second
    assert [entry["item_id"] for entry in first["stale"]] == ["plate", "proxy"]


def test_deleted_surface_guard_keeps_shots_runtime_native() -> None:
    root = Path(__file__).resolve().parents[2]
    shots = root / "astrid" / "packs" / "shots"
    assert not (shots / "repository.py").exists()
    assert not (shots / "schema-pack.yaml").exists()
    assert not (root / "astrid" / "sdk" / "shots.py").exists()
    source = (shots / "dependencies.py").read_text(encoding="utf-8")
    assert "sqlite" not in source.lower()
