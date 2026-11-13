from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.core.generation import engine_recipe as recipes
from astrid.core.generation import engine_runner as runner
from astrid.core.generation.engine_contracts import UnsafePathError
from astrid.core.generation.engine_platforms import platform_adapter


@pytest.mark.parametrize("external", [False, True])
def test_start_passes_models_directory_created_before_server_start(tmp_path, external):
    recipe = recipes.load_recipe("comfyui", "linux-x86_64-cu130")
    root = tmp_path / "engine"
    configured = str(tmp_path / "volume") if external else None
    models = Path(configured) if external else root / "runtime/ComfyUI/models"
    instance = runner._Runner.__new__(runner._Runner)
    instance.recipe, instance.adapter = recipe, platform_adapter(recipe.platform)
    instance.plan = runner.plan_install(recipe, root=str(root), placement="local", models_root=configured)
    instance.deadline, instance.step_id, instance.visibility = 123.0, None, None
    instance.service_owner = None
    instance.context = SimpleNamespace(ensure_dir=lambda path, **kw: Path(path).mkdir(parents=True))
    operations = []

    def service(operation, *, launch):
        operations.append(operation)
        assert launch["argv"][-2:] == ["--models-directory", str(models)]
        if operation == "probe":
            assert not models.exists()
            return {"state": "absent"}
        assert operation == "start" and models.is_dir()
        assert not models.is_symlink()
        instance.service_owner = "fixture-owner"
        return {"state": "owned"}

    instance.service = service
    assert instance.start()[0] == "executed"
    assert operations == ["probe", "start"]


@pytest.mark.parametrize("platform", ["linux-x86_64-cu130", "windows-x86_64-cu130"])
def test_supported_recipes_pass_the_models_directory_flag(platform):
    recipe = recipes.load_recipe("comfyui", platform)
    assert recipe.start.argv[-2:] == ("--models-directory", "{models_root}")


def test_a_models_root_inside_the_engine_tree_is_still_refused(tmp_path):
    recipe = recipes.load_recipe("comfyui", "linux-x86_64-cu130")
    root = tmp_path / "engine"
    with pytest.raises(UnsafePathError):
        runner.plan_install(recipe, root=str(root), placement="local",
                            models_root=str(root / "runtime/ComfyUI/models"))


def test_source_cleans_legacy_links_before_validation(tmp_path):
    recipe = recipes.load_recipe("comfyui", "linux-x86_64-cu130")
    instance = runner._Runner.__new__(runner._Runner)
    instance.recipe, instance.adapter = recipe, platform_adapter(recipe.platform)
    instance.plan = runner.plan_install(recipe, root=str(tmp_path / "engine"), placement="local")
    instance.deadline, instance.step_id = 123.0, None
    operations = []

    def cleanup(operation, **kwargs):
        assert operation == "cleanup-legacy-model-links"
        assert kwargs == {"models_root": None}
        operations.append("cleanup")

    def fact(path):
        assert operations == ["cleanup"]
        raise UnsafePathError("fixture stops at source validation")

    instance.helper, instance.fact = cleanup, fact
    source = next(s for s in recipe.sources if s.name == "comfyui")
    with pytest.raises(UnsafePathError, match="source validation"):
        instance.source(source)
