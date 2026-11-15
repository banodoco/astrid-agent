from dataclasses import replace

import pytest

from astrid.core.generation import engine_recipe as recipes
from astrid.core.generation import engine_runner as runner
from astrid.core.generation import engine_runner_target as target


@pytest.fixture
def config(tmp_path, monkeypatch):
    root = tmp_path / "engine"
    root.mkdir()
    recipe = recipes.load_recipe("comfyui", "linux-x86_64-cu130")
    lock = recipes.load_model_lock(recipe.models_lock)
    example = lock.models[0]
    lock = replace(example, name="fixture", destination="diffusion_models/file")
    lock = replace(recipes.load_model_lock(recipe.models_lock), models=(lock,))
    monkeypatch.setattr(recipes, "load_model_lock", lambda ref: lock)
    payload = {"schema": runner._HELPER_SCHEMA, "engine": recipe.engine, "platform": recipe.platform,
               "root": str(root), "recipe_sha256": recipes.recipe_digest(recipe),
               "helper_sha256": runner._sha(runner.helper_manifest(recipe)),
               "models_root": str(tmp_path / "volume")}
    return root, payload


def test_cleanup_removes_only_exact_legacy_model_links(config, tmp_path):
    root, payload = config
    comfy_models = root / "runtime/ComfyUI/models"
    comfy_models.mkdir(parents=True)
    models_root = tmp_path / "volume"
    expected = models_root / "diffusion_models"
    expected.parent.mkdir(parents=True)
    expected.mkdir()
    legacy = comfy_models / "diffusion_models"
    legacy.symlink_to(expected, target_is_directory=True)

    foreign = tmp_path / "foreign"
    foreign.mkdir()
    unrelated = comfy_models / "checkpoints"
    unrelated.symlink_to(foreign, target_is_directory=True)

    result = target.dispatch("cleanup-legacy-model-links", payload)

    assert result["facts"] == {"removed": ["diffusion_models"]}
    assert not legacy.is_symlink()
    assert unrelated.is_symlink() and unrelated.resolve() == foreign
    assert expected.is_dir()


def test_cleanup_preserves_comfyui_owned_directories_and_wrong_target_links(config, tmp_path):
    root, payload = config
    comfy_models = root / "runtime/ComfyUI/models"
    comfy_models.mkdir(parents=True)
    owned = comfy_models / "diffusion_models"
    owned.mkdir()
    (owned / "model.bin").write_bytes(b"keep")
    wrong_target = tmp_path / "other-models/diffusion_models"
    wrong_target.mkdir(parents=True)
    link = comfy_models / "loras"
    link.symlink_to(wrong_target, target_is_directory=True)

    target.dispatch("cleanup-legacy-model-links", payload)

    assert (owned / "model.bin").read_bytes() == b"keep"
    assert link.is_symlink() and link.resolve() == wrong_target


def test_cleanup_does_not_follow_a_linked_models_destination(config, tmp_path):
    root, payload = config
    comfy_models = root / "runtime/ComfyUI/models"
    comfy_models.mkdir(parents=True)
    destination = tmp_path / "volume/diffusion_models"
    destination.parent.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    destination.symlink_to(outside, target_is_directory=True)
    legacy = comfy_models / "diffusion_models"
    legacy.symlink_to(destination, target_is_directory=True)

    target.dispatch("cleanup-legacy-model-links", payload)

    assert legacy.is_symlink()
    assert destination.is_symlink() and outside.is_dir()


def test_cleanup_is_idempotent(config):
    root, payload = config
    comfy_models = root / "runtime/ComfyUI/models"
    comfy_models.mkdir(parents=True)
    destination = tmp_path_for(payload) / "diffusion_models"
    destination.mkdir(parents=True)
    (comfy_models / "diffusion_models").symlink_to(destination, target_is_directory=True)

    first = target.dispatch("cleanup-legacy-model-links", payload)
    second = target.dispatch("cleanup-legacy-model-links", payload)

    assert first["facts"] == {"removed": ["diffusion_models"]}
    assert second["facts"] == {"removed": []}


def tmp_path_for(payload):
    from pathlib import Path
    return Path(payload["models_root"])
