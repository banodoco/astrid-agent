from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import FrozenInstanceError

import pytest

from astrid.core.generation import engine_recipe as recipes
from astrid.core.generation.engine_contracts import DataRef, ModelLockValidationError, RecipeValidationError, UnknownEngine, UnsupportedPlatform, UnsafePathError


def _model_ref(tmp_path, value):
    path = tmp_path / "models.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    return DataRef("models.json", hashlib.sha256(path.read_bytes()).hexdigest())


def test_index_roundtrips_windows_and_linux_recipes_with_immutable_values():
    windows = recipes.load_recipe("vibecomfy", "windows-x86_64-cu130")
    wan_windows = recipes.load_recipe("wan2gp", "windows-x86_64-cu128")
    linux = recipes.load_recipe("vibecomfy-h3", "linux-x86_64-cu130")
    assert recipes.serialize_recipe(windows) == recipes.serialize_recipe(recipes.load_recipe(windows.engine, windows.platform))
    assert recipes.serialize_recipe(wan_windows) == recipes.serialize_recipe(recipes.load_recipe(wan_windows.engine, wan_windows.platform))
    assert recipes.serialize_recipe(linux) == recipes.serialize_recipe(recipes.load_recipe(linux.engine, linux.platform))
    assert windows.start.bind_host == wan_windows.start.bind_host == linux.start.bind_host == "127.0.0.1"
    assert windows.start.port == linux.start.port == 8188
    assert wan_windows.platform == "windows-x86_64-cu128"
    assert [(wheel.name, wheel.version) for wheel in wan_windows.environments[0].final_wheels] == [
        ("torch", "2.8.0+cu128"), ("torchvision", "0.23.0+cu128"), ("torchaudio", "2.8.0+cu128")]
    assert len(windows.environments[0].final_wheels) == 3
    assert windows.environments[0].requirements.mode == "resolve-once"
    assert {source.name for source in windows.sources} == {"comfyui", "vibecomfy", "h3-motion-context", "videohelpersuite"}
    assert len(recipes.load_recipe_model_lock(linux.engine, linux.platform).models) == 5
    assert [model.bytes for model in recipes.load_recipe_model_lock(linux.engine, linux.platform).models] == [
        20970379616, 27141342152, 2811065184, 605254808, 1956193000,
    ]
    models = recipes.load_recipe_model_lock(linux.engine, linux.platform).models
    assert [model.revision for model in models] == ["e5eb578a89295337b8ff433a035929ce0279e0b6"] * 5
    assert [model.sha256 for model in models] == [
        "9255f52b6677845ad238f20dfaafa94727053694127ab7f255c048f0f9365779",
        "bc2ced0fbea64757fa9acddccfc0b3f4819d1dcf1da6c124d690d368be283923",
        "52a2c8c73583c86e4f41cdcce3a6ad0ea562987bc0bf3d60a0cef5f5c8e60c0e",
        "8e505d95dd1561d47abd43d4238fd40d9bb1ae9e147ed0a4cba778d76ae4db48",
        "2339acdf19bfe123f46b971ea35d367a84adb85de43627e1eceafa5a5b2b111e",
    ]
    with pytest.raises(TypeError):
        windows.start.env["VIBECOMFY_HEADLESS"] = "0"
    with pytest.raises(FrozenInstanceError):
        windows.sources[0].pin.revision = "0" * 40


def test_unknown_fields_are_rejected_in_recipe_index(monkeypatch, tmp_path):
    index = tmp_path / "engine-index.json"
    index.write_text(json.dumps({"schema": "astrid.engine-index.v1", "entries": [], "surprise": True}))
    monkeypatch.setattr(recipes, "_INDEX_PATH", index)
    with pytest.raises(RecipeValidationError, match="unknown surprise"):
        recipes.load_recipe_index()


def test_unknown_engine_and_known_unsupported_platform_are_distinct():
    with pytest.raises(UnknownEngine):
        recipes.load_recipe("not-a-pack", "linux-x86_64-cu130")
    with pytest.raises(UnsupportedPlatform):
        recipes.load_recipe("wan2gp", "windows-x86_64-cu130")


def test_invalid_model_lock_schema_is_rejected(monkeypatch, tmp_path):
    monkeypatch.setattr(recipes, "_ROOT", tmp_path)
    ref = _model_ref(tmp_path, {"schema": "wrong", "models": []})
    with pytest.raises(ModelLockValidationError, match="unknown model lock schema"):
        recipes.load_model_lock(ref)


def test_model_path_traversal_is_rejected(monkeypatch, tmp_path):
    monkeypatch.setattr(recipes, "_ROOT", tmp_path)
    lock = {"schema": "astrid.models-lock.v1", "models": [{
        "name": "unsafe", "repository": "owner/repo", "revision": "a" * 40,
        "path": "weights/model.safetensors", "url": None, "destination": "../outside",
        "bytes": 3, "sha256": "b" * 64,
    }]}
    with pytest.raises(UnsafePathError):
        recipes.load_model_lock(_model_ref(tmp_path, lock))


def test_duplicate_model_destinations_are_rejected(monkeypatch, tmp_path):
    monkeypatch.setattr(recipes, "_ROOT", tmp_path)
    row = {"name": "one", "repository": "owner/repo", "revision": "a" * 40,
           "path": "weights/model.safetensors", "url": None, "destination": "weights/model.safetensors",
           "bytes": 3, "sha256": "b" * 64}
    lock = {"schema": "astrid.models-lock.v1", "models": [row, {**row, "name": "two"}]}
    with pytest.raises(ModelLockValidationError, match="duplicate model destination"):
        recipes.load_model_lock(_model_ref(tmp_path, lock))


def test_local_recipe_lookup_does_not_clone_or_call_network(monkeypatch):
    import subprocess
    import urllib.request

    monkeypatch.setattr(subprocess, "run", lambda *_a, **_k: pytest.fail("lookup spawned a process"))
    monkeypatch.setattr(urllib.request, "urlopen", lambda *_a, **_k: pytest.fail("lookup accessed the network"))
    recipe = recipes.load_recipe("vibecomfy-h3", "linux-x86_64-cu130")
    assert recipe.engine == "vibecomfy-h3"


def test_engine_errors_expose_the_stable_public_fields():
    with pytest.raises(UnsupportedPlatform) as caught:
        recipes.load_recipe("wan2gp", "linux-x86_64-cu130")
    error = caught.value
    assert (error.code, error.message, error.step_id, error.retryable, error.recovery_handle) == (
        "unsupported_platform", str(error), None, False, None,
    )


def _recipe_workspace(tmp_path, monkeypatch, mutate):
    tmp_path.mkdir(parents=True, exist_ok=True)
    source_root = recipes._ROOT
    packs = tmp_path / "packs"
    shutil.copytree(source_root / "packs", packs)
    index_path = packs / "engine-index.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    entry = next(item for item in index["entries"] if item["engine"] == "vibecomfy")
    recipe_path = packs / entry["recipe"]["path"].removeprefix("packs/")
    recipe = json.loads(recipe_path.read_text(encoding="utf-8"))
    mutate(recipe)
    recipe_bytes = json.dumps(recipe, separators=(",", ":"), ensure_ascii=False).encode()
    recipe_path.write_bytes(recipe_bytes)
    entry["recipe"]["sha256"] = hashlib.sha256(recipe_bytes).hexdigest()
    index_path.write_text(json.dumps(index), encoding="utf-8")
    monkeypatch.setattr(recipes, "_ROOT", tmp_path)
    monkeypatch.setattr(recipes, "_INDEX_PATH", index_path)


@pytest.mark.parametrize("schema", ["astrid.engine-recipe.v2", "wrong"])
def test_invalid_recipe_schema_is_rejected(monkeypatch, tmp_path, schema):
    _recipe_workspace(tmp_path, monkeypatch, lambda recipe: recipe.update(schema=schema))
    with pytest.raises(RecipeValidationError, match="unknown recipe schema"):
        recipes.load_recipe("vibecomfy", "windows-x86_64-cu130")


def test_unknown_recipe_field_is_rejected(monkeypatch, tmp_path):
    _recipe_workspace(tmp_path, monkeypatch, lambda recipe: recipe.update(unreviewed=True))
    with pytest.raises(RecipeValidationError, match="unknown unreviewed"):
        recipes.load_recipe("vibecomfy", "windows-x86_64-cu130")


@pytest.mark.parametrize("path", ["//outside.invalid/system_stats", "https://outside.invalid/system_stats",
                                    "/system_stats?next=//outside.invalid", "/system_stats#fragment",
                                    r"/system_stats\..\outside", "/system_stats/../outside"])
def test_health_paths_reject_cross_origin_and_non_path_references(monkeypatch, tmp_path, path):
    _recipe_workspace(tmp_path, monkeypatch, lambda recipe: recipe["health"].update(path=path))
    with pytest.raises(RecipeValidationError, match="same-origin absolute URL path"):
        recipes.load_recipe("vibecomfy", "windows-x86_64-cu130")


@pytest.mark.parametrize("field,value", [
    ("timeout_seconds", True), ("timeout_seconds", 0), ("timeout_seconds", float("nan")),
    ("timeout_seconds", float("inf")), ("poll_interval_seconds", True),
    ("poll_interval_seconds", 0), ("poll_interval_seconds", float("nan")),
    ("poll_interval_seconds", float("inf")),
])
def test_health_durations_must_be_finite_positive_numbers(monkeypatch, tmp_path, field, value):
    _recipe_workspace(tmp_path, monkeypatch, lambda recipe: recipe["health"].update({field: value}))
    with pytest.raises(RecipeValidationError, match="health timeouts"):
        recipes.load_recipe("vibecomfy", "windows-x86_64-cu130")


def test_pin_set_mapping_is_immutable(monkeypatch):
    recipe = recipes.load_recipe("vibecomfy", "windows-x86_64-cu130")
    pins = recipes.load_pin_set(recipe.pins)
    with pytest.raises(TypeError):
        pins["other"] = recipe.sources[0].pin


def test_duplicate_source_paths_are_rejected(monkeypatch, tmp_path):
    def duplicate(recipe):
        recipe["source_bindings"][1]["relative_path"] = recipe["source_bindings"][0]["relative_path"]
    _recipe_workspace(tmp_path, monkeypatch, duplicate)
    with pytest.raises(RecipeValidationError, match="duplicate source path"):
        recipes.load_recipe("vibecomfy", "windows-x86_64-cu130")


def test_malformed_recipe_placeholders_are_rejected(monkeypatch, tmp_path):
    def malformed(recipe):
        recipe["start"]["argv"][0] = "{python"
    _recipe_workspace(tmp_path, monkeypatch, malformed)
    with pytest.raises(RecipeValidationError, match="malformed recipe placeholder"):
        recipes.load_recipe("vibecomfy", "windows-x86_64-cu130")


def test_duplicate_requirements_source_paths_are_rejected(monkeypatch, tmp_path):
    def duplicate(recipe):
        inputs = recipe["environments"][0]["requirements"]["input_paths"]
        inputs.append(dict(inputs[0]))
    _recipe_workspace(tmp_path, monkeypatch, duplicate)
    with pytest.raises(RecipeValidationError, match="duplicate requirements source path"):
        recipes.load_recipe("vibecomfy", "windows-x86_64-cu130")
