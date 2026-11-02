from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from astrid.core.generation import engine_recipe as recipes
from astrid.core.generation.engine_contracts import IntegrityError, DataRef, ModelLockValidationError, RecipeValidationError, UnknownEngine, UnsupportedPlatform, UnsafePathError


def _model_ref(tmp_path, value):
    path = tmp_path / "models.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    return DataRef("models.json", hashlib.sha256(path.read_bytes()).hexdigest())


def test_index_roundtrips_windows_and_linux_recipes_with_immutable_values():
    windows = recipes.load_recipe("vibecomfy", "windows-x86_64-cu130")
    wan_windows = recipes.load_recipe("wan2gp", "windows-x86_64-cu128")
    linux = recipes.load_recipe("vibecomfy-h3", "linux-x86_64-cu130")
    h3_windows = recipes.load_recipe("vibecomfy-h3", "windows-x86_64-cu130")
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
    assert {source.name for source in windows.sources} == {"comfyui", "vibecomfy"}
    assert {source.name for source in h3_windows.sources} == {"comfyui", "vibecomfy", "h3-motion-context", "videohelpersuite"}
    assert h3_windows.environments[0].final_wheels == windows.environments[0].final_wheels
    assert len(h3_windows.environments[0].final_wheels) == 3
    assert h3_windows.environments[0].requirements.mode == "resolve-once"
    assert h3_windows.health.required_nodes == ("MiniMaxH3ReferenceToVideo", "VHS_VideoCombine")
    assert windows.health.required_nodes == ("UNETLoader", "VAELoader")
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


def test_comfyui_linux_recipe_is_package_owned_and_h3_entry_is_pinned():
    platform = "linux-x86_64-cu130"
    index = recipes.load_recipe_index()
    entries = {(entry.engine, entry.platform): entry for entry in index.entries}
    comfy_entry = entries[("comfyui", platform)]
    h3_entry = entries[("vibecomfy-h3", platform)]
    assert comfy_entry.recipe.path == "packs/vibecomfy/recipe.linux-cu130.json"
    assert comfy_entry.recipe.sha256 == hashlib.sha256(
        (recipes._ROOT / comfy_entry.recipe.path).read_bytes()
    ).hexdigest()
    assert h3_entry.recipe.path == "packs/runpod/recipe.linux-cu130.json"
    assert h3_entry.recipe.sha256 == "3084f3b0e4bac3879e6f3d888f3a441cf502b3b03c3c00eecebbcb898991e823"

    recipe = recipes.load_recipe("comfyui", platform)
    lock = recipes.load_recipe_model_lock("comfyui", platform)
    requirements = recipe.environments[0].requirements.packaged_lock
    assert requirements.path == "packs/vibecomfy/requirements.linux-cu130.lock"
    assert requirements.sha256 == "75f69f564bab7b45e50cb11ef5b2a5bade979e4dd76400393404c0d211a670a7"
    assert hashlib.sha256((recipes._ROOT / requirements.path).read_bytes()).hexdigest() == requirements.sha256
    lock_text = (recipes._ROOT / requirements.path).read_text(encoding="utf-8")
    for pin in (
        "comfyui-frontend-package==1.57.4", "comfyui-workflow-templates==0.11.100",
        "comfyui-embedded-docs==0.5.15", "comfy-aimdo==0.6.0", "comfy-kitchen==0.2.40",
        "opencv-python-headless==5.0.0.93",
    ):
        assert any(line.startswith(pin + " ") for line in lock_text.splitlines())
    h3_requirements = recipes.load_recipe("vibecomfy-h3", platform).environments[0].requirements.packaged_lock
    assert h3_requirements.path == "packs/runpod/requirements.linux-cu130.lock"
    assert h3_requirements.sha256 == "ffc717b5cb0520584ff0d7a79e440f6d922727a952572778c6eb6eb7cbd5a73a"
    assert hashlib.sha256((recipes._ROOT / h3_requirements.path).read_bytes()).hexdigest() == h3_requirements.sha256
    assert recipe.models_lock.path == "packs/vibecomfy/models.lock.json"
    assert recipe.models_lock.sha256 == hashlib.sha256(
        (recipes._ROOT / recipe.models_lock.path).read_bytes()
    ).hexdigest()
    assert recipe.sources[0].name == "comfyui"
    assert recipe.start.argv[-1] == "{models_config}"
    assert [model.destination for model in lock.models] == [
        "diffusion_models/qwen_image_2.1_turbo_int8_convrot.safetensors",
        "text_encoders/qwen3vl_8b_int8_convrot.safetensors",
        "vae/qwen_image_2.1_vae_bf16.safetensors",
    ]


def test_plain_comfyui_and_packaged_client_share_vibecomfy_authority():
    plain = recipes.load_recipe("comfyui", "linux-x86_64-cu130")
    packaged = recipes.load_recipe("vibecomfy", "windows-x86_64-cu130")
    pins = recipes.load_pin_set(plain.pins)
    assert plain.pins == packaged.pins
    for recipe in (plain, packaged):
        bindings = [source for source in recipe.sources if source.name == "vibecomfy"]
        assert len(bindings) == 1
        assert bindings[0].relative_path == "runtime/vibecomfy"
        assert bindings[0].pin == pins["vibecomfy"]
        assert bindings[0].pin.repository == "https://github.com/banodoco/VibeComfy.git"
        assert bindings[0].pin.revision == "2967033970cbb5884651c9565265778482d3b44e"
        assert next(s.pin.revision for s in recipe.sources if s.name == "comfyui") == (
            "423cbd20511becb1f16122ab518e92e58bfa03d9")
    declarations = sum(path.read_text().count('https://github.com/banodoco/VibeComfy.git')
                       for path in (recipes._ROOT / "packs").rglob("*.json"))
    assert declarations == 1


def test_runpod_linux_recipe_binds_vibecomfy_to_h3_pin():
    recipe = recipes.load_recipe("vibecomfy-h3", "linux-x86_64-cu130")
    pins = recipes.load_pin_set(recipe.pins)
    bindings = [source for source in recipe.sources if source.name == "vibecomfy"]
    assert len(bindings) == 1
    assert bindings[0].relative_path == "runtime/vibecomfy"
    assert bindings[0].pin == pins["vibecomfy"]
    assert {s.name: (s.pin.repository, s.pin.revision) for s in recipe.sources} == {
        "comfyui": ("https://github.com/Comfy-Org/ComfyUI.git", "ee71d5c4993f29086b27fde1629a945ae48425bf"),
        "vibecomfy": ("https://github.com/banodoco/VibeComfy.git", "2967033970cbb5884651c9565265778482d3b44e"),
        "h3-motion-context": ("https://github.com/seitanism/ComfyUI-H3-Motion-Context-MultiRef.git", "361624fb406b63eb6694442eac6c895fc1533a70"),
        "videohelpersuite": ("https://github.com/Kosinkadink/ComfyUI-VideoHelperSuite.git", "4ee72c065db22c9d96c2427954dc69e7b908444b"),
    }


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


def _selected_catalog(*owners):
    from astrid.core.pack.canonical import BundledCatalog, read_normalize_validate

    return BundledCatalog(recipes._ROOT / "packs", tuple(
        read_normalize_validate(recipes._ROOT / "packs" / owner / "pack.yaml", source="local")
        for owner in owners
    ))


def _owned_workspace(tmp_path, monkeypatch):
    root = recipes._ROOT
    for owner in ("vibecomfy", "runpod", "wan2gp"):
        shutil.copytree(root / "packs" / owner, tmp_path / "packs" / owner)
    shutil.copy2(root / "packs" / "engine-index.json", tmp_path / "packs" / "engine-index.json")
    monkeypatch.setattr(recipes, "_ROOT", tmp_path)
    monkeypatch.setattr(recipes, "_INDEX_PATH", tmp_path / "packs" / "engine-index.json")


def test_selected_catalog_has_no_ambient_optional_fallback(monkeypatch, tmp_path):
    _owned_workspace(tmp_path, monkeypatch)
    core = _selected_catalog("vibecomfy")
    for owner in ("runpod", "wan2gp"):
        for path in (tmp_path / "packs" / owner).glob("*.json"):
            path.write_bytes(b"malformed unselected optional data")
    assert recipes.load_recipe("vibecomfy", "windows-x86_64-cu130", registry=core).engine == "vibecomfy"
    assert recipes.load_recipe("vibecomfy", "windows-x86_64-cu130").engine == "vibecomfy"
    assert {row.engine for row in recipes.load_recipe_index(registry=core).entries} == {"comfyui", "vibecomfy"}
    with pytest.raises(UnknownEngine, match="selected catalog"):
        recipes.load_recipe("vibecomfy-h3", "linux-x86_64-cu130", registry=core)
    with pytest.raises(UnknownEngine, match="selected catalog"):
        recipes.load_recipe("wan2gp", "windows-x86_64-cu128", registry=core)


def test_selected_h3_reads_only_declared_shared_and_optional_pin_graph(monkeypatch):
    selected = _selected_catalog("vibecomfy", "runpod")
    reads = []
    original = Path.read_bytes

    def record(path):
        reads.append(path)
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", record)
    recipe = recipes.load_recipe("vibecomfy-h3", "linux-x86_64-cu130", registry=selected)
    models = recipes.load_model_lock(recipe.models_lock, registry=selected)
    assert len(models.models) == 5
    assert {ref.path for ref in recipes.pin_set_refs(recipe.pins)} == {
        "packs/runpod/pins.json", "packs/vibecomfy/pins.json",
    }
    assert not any("wan2gp" in path.parts for path in reads)
    assert all(path.is_relative_to(recipes._ROOT / "packs" / "vibecomfy") or
               path.is_relative_to(recipes._ROOT / "packs" / "runpod") for path in reads)
    # The shared client revision has one authored authority, also for H3.
    assert next(s.pin for s in recipe.sources if s.name == "vibecomfy") == recipes.load_pin_set(
        recipes.load_recipe("vibecomfy", "windows-x86_64-cu130", registry=selected).pins,
        registry=selected,
    )["vibecomfy"]


@pytest.mark.parametrize("engine,platform,owner", [
    ("vibecomfy-h3", "linux-x86_64-cu130", "runpod"),
    ("wan2gp", "windows-x86_64-cu128", "wan2gp"),
])
@pytest.mark.parametrize("resource", ["recipe", "pins", "models_lock"])
@pytest.mark.parametrize("damage", ["missing", "hash-mismatch", "malformed"])
def test_selected_optional_resources_fail_closed(monkeypatch, tmp_path, engine, platform, owner, resource, damage):
    _owned_workspace(tmp_path, monkeypatch)
    selected = _selected_catalog("vibecomfy", owner)
    index = json.loads(recipes._INDEX_PATH.read_text())
    row = next(row for row in index["entries"] if (row["engine"], row["platform"]) == (engine, platform))
    recipe_path = tmp_path / row["recipe"]["path"]
    recipe = json.loads(recipe_path.read_text())
    ref = row["recipe"] if resource == "recipe" else recipe[resource]
    path = tmp_path / ref["path"]
    if damage == "missing":
        path.unlink()
        error, reason = IntegrityError, "packaged data is unavailable: " + ref["path"]
    elif damage == "hash-mismatch":
        path.write_bytes(path.read_bytes() + b" ")
        error, reason = IntegrityError, "packaged data digest mismatch: " + ref["path"]
    else:
        path.write_bytes(b"{")
        ref["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        if resource != "recipe":
            recipe_path.write_text(json.dumps(recipe))
            row["recipe"]["sha256"] = hashlib.sha256(recipe_path.read_bytes()).hexdigest()
        recipes._INDEX_PATH.write_text(json.dumps(index))
        selected = _selected_catalog("vibecomfy", owner)
        error = ModelLockValidationError if resource == "models_lock" else RecipeValidationError
        reason = "invalid JSON"
    with pytest.raises(error, match=reason):
        if resource == "models_lock":
            recipes.load_recipe_model_lock(engine, platform, registry=selected)
        else:
            recipes.load_recipe(engine, platform, registry=selected)


def test_imported_shared_pins_cannot_be_omitted_from_selected_catalog():
    with pytest.raises(RecipeValidationError, match="not declared.*packs/vibecomfy/pins.json"):
        recipes.load_recipe("vibecomfy-h3", "linux-x86_64-cu130", registry=_selected_catalog("runpod"))


def test_unknown_engine_is_not_established_by_an_ambient_pin_file(monkeypatch, tmp_path):
    _owned_workspace(tmp_path, monkeypatch)
    path = tmp_path / "packs" / "ambient-engine" / "pins.json"
    path.parent.mkdir()
    path.write_text('{"schema":"astrid.engine-pins.v1","sources":{}}')
    with pytest.raises(UnknownEngine):
        recipes.load_recipe("ambient-engine", "linux-x86_64-cu130")


def test_duplicate_pin_authority_is_rejected(monkeypatch, tmp_path):
    monkeypatch.setattr(recipes, "_ROOT", tmp_path)
    pin = {"repository": "https://example.invalid/repo.git", "revision": "a" * 40}
    child = _model_ref(tmp_path, {"schema": "astrid.engine-pins.v1", "sources": {"shared": pin}})
    parent = tmp_path / "pins.json"
    parent.write_text(json.dumps({"schema": "astrid.engine-pins.v1", "sources": {"shared": pin},
                                  "includes": [{"path": child.path, "sha256": child.sha256}]}))
    ref = DataRef("pins.json", hashlib.sha256(parent.read_bytes()).hexdigest())
    with pytest.raises(RecipeValidationError, match="duplicate pin authority: shared"):
        recipes.load_pin_set(ref)


def test_cyclic_pin_import_is_rejected_before_reopening_it(monkeypatch, tmp_path):
    monkeypatch.setattr(recipes, "_ROOT", tmp_path)
    path = tmp_path / "pins.json"
    path.write_text(json.dumps({"schema": "astrid.engine-pins.v1", "sources": {},
                                "includes": [{"path": "pins.json", "sha256": "a" * 64}]}))
    with pytest.raises(RecipeValidationError, match="cyclic pin set reference: pins.json"):
        recipes.load_pin_set(DataRef("pins.json", hashlib.sha256(path.read_bytes()).hexdigest()))


def test_helper_manifest_preserves_transitive_pin_identity():
    from astrid.core.generation.engine_runner import helper_manifest

    recipe = recipes.load_recipe("vibecomfy-h3", "linux-x86_64-cu130")
    closure = helper_manifest(recipe)
    assert closure["packs/vibecomfy/pins.json"] == recipes.load_recipe(
        "vibecomfy", "windows-x86_64-cu130").pins.sha256
    assert closure["packs/runpod/pins.json"] == recipe.pins.sha256
    assert "packs/wan2gp/pins.json" not in closure


def test_windows_h3_policy_is_retained_by_its_optional_owner():
    selected = _selected_catalog("vibecomfy", "runpod")
    recipe = recipes.load_recipe("vibecomfy-h3", "windows-x86_64-cu130", registry=selected)
    pins = recipes.load_pin_set(recipe.pins, registry=selected)
    assert {s.name: s.pin for s in recipe.sources} == {
        "comfyui": pins["comfyui-plain"], "vibecomfy": pins["vibecomfy"],
        "h3-motion-context": pins["h3-motion-context"], "videohelpersuite": pins["videohelpersuite"],
    }
    assert [(x.source, x.path) for x in recipe.environments[0].requirements.input_paths] == [
        ("comfyui", "requirements.txt"), ("vibecomfy", "pyproject.toml"),
        ("h3-motion-context", "requirements.txt"), ("videohelpersuite", "requirements.txt"),
    ]
    assert recipe.start.env == {"VIBECOMFY_HEADLESS": "1"}
    assert len(recipes.load_model_lock(recipe.models_lock, registry=selected).models) == 3


@pytest.mark.parametrize("engine,platform", [
    ("vibecomfy", "windows-x86_64-cu130"),
    ("vibecomfy-h3", "linux-x86_64-cu130"),
])
def test_unselected_resource_declaration_is_not_resolved(monkeypatch, tmp_path, engine, platform):
    _owned_workspace(tmp_path, monkeypatch)
    index = json.loads(recipes._INDEX_PATH.read_text())
    unselected = next(row for row in index["entries"] if row["engine"] == "vibecomfy-h3" and
                      row["platform"] == "windows-x86_64-cu130")
    unselected["recipe"] = {"path": "../unselected", "sha256": "invalid"}
    recipes._INDEX_PATH.write_text(json.dumps(index))
    assert recipes.load_recipe(engine, platform).engine == engine
    with pytest.raises(UnsafePathError, match="safe normalized POSIX relative path"):
        recipes.load_recipe("vibecomfy-h3", "windows-x86_64-cu130")


@pytest.mark.parametrize("damage", ["missing", "hash-mismatch"])
def test_shared_pin_import_is_required_and_hash_verified(monkeypatch, tmp_path, damage):
    _owned_workspace(tmp_path, monkeypatch)
    selected = _selected_catalog("vibecomfy", "runpod")
    path = tmp_path / "packs" / "vibecomfy" / "pins.json"
    if damage == "missing":
        path.unlink()
    else:
        path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(IntegrityError, match="packs/vibecomfy/pins.json"):
        recipes.load_recipe("vibecomfy-h3", "linux-x86_64-cu130", registry=selected)


@pytest.mark.parametrize("damage", ["missing", "hash-mismatch"])
def test_selected_optional_requirements_lock_fails_closed(monkeypatch, tmp_path, damage):
    from astrid.core.generation.engine_runner import helper_manifest

    _owned_workspace(tmp_path, monkeypatch)
    recipe = recipes.load_recipe("vibecomfy-h3", "linux-x86_64-cu130")
    ref = recipe.environments[0].requirements.packaged_lock
    path = tmp_path / ref.path
    if damage == "missing":
        path.unlink()
    else:
        path.write_bytes(path.read_bytes() + b" ")
    # Supply the existing helper code closure from the real package. Only its
    # packaged data root is redirected by this fixture.
    import astrid.core.generation.engine_runner as runner
    original_root = recipes._ROOT
    # helper_manifest validates code as well, so copy exactly its declared code.
    source_root = Path(runner.__file__).resolve().parents[2]
    for relative in (*runner._CODE_PATHS, *runner._A2_PATHS):
        source = source_root / relative
        if source.is_file():
            destination = original_root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
    with pytest.raises(IntegrityError, match=ref.path):
        helper_manifest(recipe)


def test_shared_engine_facts_have_one_writable_owner():
    plain = recipes.load_recipe("comfyui", "linux-x86_64-cu130")
    core = recipes.load_recipe("vibecomfy", "windows-x86_64-cu130")
    assert plain.pins == core.pins
    assert plain.models_lock == core.models_lock
    assert not (recipes._ROOT / "packs/comfyui/pins.json").exists()
    assert not (recipes._ROOT / "packs/comfyui/models.lock.json").exists()
    shared = json.loads((recipes._ROOT / core.pins.path).read_text())
    optional = json.loads((recipes._ROOT / "packs/runpod/pins.json").read_text())
    assert set(shared["sources"]) == {"comfyui-plain", "vibecomfy"}
    assert set(optional["sources"]) == {"comfyui", "h3-motion-context", "videohelpersuite"}
    assert optional["includes"] == [{"path": core.pins.path, "sha256": core.pins.sha256}]


def test_selected_manifest_is_rechecked_without_reading_unselected_sources(monkeypatch, tmp_path):
    _owned_workspace(tmp_path, monkeypatch)
    selected = _selected_catalog("vibecomfy", "runpod")
    manifest = tmp_path / "packs" / "runpod" / "pack.yaml"
    manifest.write_bytes(manifest.read_bytes() + b"\n")
    with pytest.raises(IntegrityError, match="packaged data digest mismatch: packs/runpod/pack.yaml"):
        recipes.load_recipe("vibecomfy-h3", "linux-x86_64-cu130", registry=selected)


@pytest.mark.parametrize("declaration", ["absent", "documentation"])
def test_reachable_recipe_without_selected_resource_declaration_is_refused(monkeypatch, tmp_path, declaration):
    from astrid.core.pack.manifest import load_manifest_mapping, dump_manifest_payload

    _owned_workspace(tmp_path, monkeypatch)
    manifest = tmp_path / "packs" / "runpod" / "pack.yaml"
    payload = load_manifest_mapping(manifest)
    if declaration == "absent":
        payload["resources"] = [r for r in payload["resources"] if not r["path"].startswith("recipe.")]
    else:
        for resource in payload["resources"]:
            if resource["path"].startswith("recipe."):
                resource["kind"] = declaration
    dump_manifest_payload(manifest, payload)
    selected = _selected_catalog("vibecomfy", "runpod")
    assert (manifest.parent / "recipe.linux-cu130.json").is_file()
    with pytest.raises(UnknownEngine, match="not registered in the selected catalog"):
        recipes.load_recipe("vibecomfy-h3", "linux-x86_64-cu130", registry=selected)
