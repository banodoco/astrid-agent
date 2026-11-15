"""Plan 32 T2: one requirements manifest shape for every default slot, built by the engine."""
from __future__ import annotations

import pytest

import astrid.sdk.generation as generation
from astrid.core.generation import entries
from astrid.core.model_catalog.registry import ModelRegistry

MANIFEST_KEYS = {"engine", "base", "models", "extra"}
MODEL_KEYS = {"name", "subdir", "url", "sha256", "size"}


def _entry(entry_id: str) -> dict:
    try:  # a catalog id: its local template owns the models
        template = ModelRegistry.load_default().get(entry_id).modes
        entry_id = next(iter(template.values())).backends["local"].template
    except KeyError:
        pass
    return entries.find_entry(entry_id)


from tests.helpers.generation_defaults import default_entries
SLOT_DEFAULTS = default_entries()
SLOT_ENTRIES = {"/".join(slot): ids[0] for slot, ids in SLOT_DEFAULTS.items()}
SLOT_ENTRIES["video/h3"] = "h3"


@pytest.mark.parametrize("slot", sorted(SLOT_ENTRIES))
def test_every_default_slot_has_one_manifest_shape(slot):
    manifest = entries.requirements(_entry(SLOT_ENTRIES[slot]), {})
    assert set(manifest) == MANIFEST_KEYS
    assert manifest["engine"] == "comfyui"
    assert manifest["models"], slot
    for row in manifest["models"]:
        assert set(row) == MODEL_KEYS, row
        assert row["name"] and row["subdir"] and row["url"]
        assert isinstance(row["sha256"], str) and len(row["sha256"]) == 64
        assert isinstance(row["size"], int)
    nodes = manifest["extra"]["nodes"]
    assert all(set(node) == {"repo", "commit"} and all(node.values()) for node in nodes)


def test_comfyui_extra_carries_the_custom_node_pins_of_h3():
    nodes = entries.requirements(_entry("h3"), {})["extra"]["nodes"]
    assert nodes, "h3 pins custom nodes"


def test_the_engine_table_is_built_from_generation_engine_contributions():
    assert "comfyui" in entries.engine_table()


def test_the_engine_validates_its_own_extra():
    engine = entries.engine_table()["comfyui"]
    with pytest.raises(ValueError):
        engine.validate_extra({"nodes": [{"repo": "x"}]})
