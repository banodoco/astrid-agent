"""Agent selects models; VibeComfy alone declares their source and integrity."""
from dataclasses import asdict
import json

import pytest

from astrid.core.generation import engine_recipe


@pytest.mark.parametrize("pack", ["runpod", "vibecomfy"])
def test_packaged_models_contain_references_without_copied_facts(pack):
    raw = json.loads((engine_recipe._ROOT / "packs" / pack / "models.lock.json").read_text())
    assert raw["schema"] == "astrid.vibecomfy-model-refs.v1"
    assert raw["models"]
    for row in raw["models"]:
        assert set(row) == {"name", "template", "asset"}


@pytest.mark.parametrize("platform", ["linux-x86_64-cu130", "windows-x86_64-cu130"])
def test_every_verified_model_comes_from_its_vibecomfy_record(platform):
    from vibecomfy.model_records import ready_model_records

    recipe = engine_recipe.load_recipe("comfyui", platform)
    references = json.loads((engine_recipe._ROOT / recipe.models_lock.path).read_text())["models"]
    lock = engine_recipe.load_recipe_model_lock("comfyui", platform)
    assert len(lock.models) == len(references)
    for reference, model in zip(references, lock.models):
        records = {record.destination: record for record in ready_model_records(reference["template"])}
        record = records[reference["asset"]]
        assert asdict(model) == {"name": reference["name"], "repository": record.repository,
                               "revision": record.revision, "path": record.path, "url": None,
                               "destination": record.destination, "bytes": record.bytes,
                               "sha256": record.sha256}
