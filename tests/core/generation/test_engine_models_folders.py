import json
from dataclasses import replace

import pytest

from astrid.core.generation import engine_recipe as recipes
from astrid.core.generation import engine_runner as runner
from astrid.core.generation import engine_runner_target as target
from astrid.core.generation.engine_contracts import IntegrityError


@pytest.fixture
def config(tmp_path, monkeypatch):
    root = tmp_path / "engine"
    root.mkdir()
    recipe = recipes.load_recipe("comfyui", "linux-x86_64-cu130")
    lock = recipes.load_model_lock(recipe.models_lock)
    example = lock.models[0]
    lock = replace(lock, models=tuple(replace(example, name=name, destination=f"{name}/file")
                                      for name in ("hunyuan_foley", "TTS", "foo")))
    monkeypatch.setattr(recipes, "load_model_lock", lambda ref: lock)
    payload = {"schema": runner._HELPER_SCHEMA, "engine": recipe.engine, "platform": recipe.platform,
               "root": str(root), "recipe_sha256": recipes.recipe_digest(recipe),
               "helper_sha256": runner._sha(runner.helper_manifest(recipe)),
               "models_root": str(tmp_path / "volume")}
    return root, payload


def test_config_writes_lock_folders_and_links(config):
    root, payload = config
    assert target.dispatch("models-config", payload)["facts"] == {"written": True}
    paths = json.loads((root / "runtime/extra_model_paths.yaml").read_text())["astrid_engine"]
    assert paths == {"base_path": payload["models_root"], "TTS": "TTS", "foo": "foo",
                     "hunyuan_foley": "hunyuan_foley"}
    for name in ("hunyuan_foley", "TTS", "foo"):
        link = root / "runtime/ComfyUI/models" / name
        assert link.is_symlink() and link.readlink() == tmp_path_for(payload, name)
    assert target.dispatch("models-config-check", payload)["facts"] == {"valid": True}


def tmp_path_for(payload, name):
    from pathlib import Path
    return Path(payload["models_root"]) / name


def test_empty_directory_is_replaced_and_nonempty_is_preserved(config):
    root, payload = config
    folder = root / "runtime/ComfyUI/models/hunyuan_foley"
    folder.mkdir(parents=True)
    target.dispatch("models-config", payload)
    assert folder.is_symlink()
    folder.unlink()
    folder.mkdir()
    (folder / "keep").write_text("keep")
    with pytest.raises(IntegrityError, match="hunyuan_foley"):
        target.dispatch("models-config", payload)
    assert (folder / "keep").read_text() == "keep"


def test_wrong_link_invalidates_check(config):
    root, payload = config
    target.dispatch("models-config", payload)
    link = root / "runtime/ComfyUI/models/TTS"
    link.unlink()
    link.symlink_to(root / "elsewhere")
    assert target.dispatch("models-config-check", payload)["facts"] == {"valid": False}
    with pytest.raises(IntegrityError, match="TTS"):
        target.dispatch("models-config", payload)


def test_without_a_volume_pack_folders_link_into_the_engine_models_directory(config):
    """No saved volume: the base is <root>/models, still not ComfyUI's own models directory."""
    root, payload = config
    payload["models_root"] = None
    target.dispatch("models-config", payload)
    base = json.loads((root / "runtime/extra_model_paths.yaml").read_text())["astrid_engine"]["base_path"]
    assert base == str(root / "models")
    link = root / "runtime/ComfyUI/models/hunyuan_foley"
    assert link.is_symlink() and str(link.readlink()) == str(root / "models/hunyuan_foley")
    assert target.dispatch("models-config-check", payload)["facts"] == {"valid": True}


def test_a_models_root_inside_the_engine_tree_is_still_refused(config):
    from astrid.core.generation.engine_contracts import UnsafePathError

    root, payload = config
    payload["models_root"] = str(root / "runtime/ComfyUI/models")
    with pytest.raises(UnsafePathError):
        target.dispatch("models-config", payload)


def test_tracked_folders_use_search_paths_and_source_exemptions_are_exact(config):
    import subprocess

    root, payload = config
    comfy = root / "runtime/ComfyUI"
    tracked = comfy / "models/foo"
    tracked.mkdir(parents=True)
    (tracked / ".keep").write_text("")
    subprocess.run(["git", "init", "-q", str(comfy)], check=True)
    subprocess.run(["git", "-C", str(comfy), "add", "models/foo/.keep"], check=True)
    subprocess.run(["git", "-C", str(comfy), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                    "commit", "-qm", "fixture"], check=True)
    target.dispatch("models-config", payload)
    assert tracked.is_dir() and not tracked.is_symlink()
    assert target.dispatch("models-config-check", payload)["facts"] == {"valid": True}
    check = {**payload, "entries": ["?? models/hunyuan_foley"]}
    assert target.dispatch("models-source-check", check)["facts"] == {"valid": True}
    for entry in ("?? stray", "?? models/foo", " M models/foo/.keep"):
        assert target.dispatch("models-source-check", {**check, "entries": [entry]})["facts"] == {"valid": False}
    link = comfy / "models/hunyuan_foley"
    link.unlink()
    link.symlink_to(root / "wrong")
    assert target.dispatch("models-source-check", check)["facts"] == {"valid": False}
    link.unlink()
    link.mkdir()
    assert target.dispatch("models-source-check", check)["facts"] == {"valid": False}


def test_a_destination_that_is_itself_a_link_is_refused(config, tmp_path):
    root, payload = config
    target.dispatch("models-config", payload)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    destination = tmp_path_for(payload, "hunyuan_foley")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.symlink_to(elsewhere, target_is_directory=True)
    assert target.dispatch("models-config-check", payload)["facts"] == {"valid": False}
    entries = ["?? models/hunyuan_foley"]
    source = {**payload, "entries": entries}
    assert target.dispatch("models-source-check", source)["facts"] == {"valid": False}


def test_a_destination_that_is_a_file_is_refused(config):
    root, payload = config
    target.dispatch("models-config", payload)
    destination = tmp_path_for(payload, "TTS")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("not a folder")
    assert target.dispatch("models-config-check", payload)["facts"] == {"valid": False}
