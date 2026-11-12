"""Plan 32 T2b: one base engine, keyed ``comfyui``; the two retired packs and the engine index are gone."""
from __future__ import annotations

import json
from pathlib import Path

from astrid.core.generation import entries
from astrid.core.generation.engine_recipe import load_recipe, load_recipe_index

ROOT = Path(__file__).resolve().parents[3]
# Built from parts so this file is not a hit for its own grep.
GONE = ("vibecomfy" + "-h3", "comfy" + "_wrap", "packs/" + "comfyui")
COMFY_PIN = "6bfaacc67c2103481e5f0c84d75257cd0581d86a"


def test_no_removed_name_survives_in_astrid_or_tests():
    hits = []
    for base in ("astrid", "tests"):
        for path in (ROOT / base).rglob("*"):
            if not path.is_file() or path == Path(__file__) or "__pycache__" in path.parts:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            hits += [f"{path.relative_to(ROOT)}: {name}" for name in GONE if name in text]
    assert hits == []


def test_the_removed_packs_and_the_index_are_deleted():
    for gone in ("comfyui", "comfy" + "_wrap", "engine-index.json"):
        assert not (ROOT / "astrid/packs" / gone).exists()


def test_one_engine_row_per_platform():
    rows = [(row.engine, row.platform) for row in load_recipe_index().entries]
    assert len({platform for _, platform in rows}) == len(rows)
    assert ("comfyui", entries.RUNPOD_PLATFORM) in rows
    assert entries.runpod_engines() == ("comfyui",)


def test_the_base_engine_carries_the_h3_nodes_and_vibecomfys_comfyui_pin():
    recipe = load_recipe("comfyui", entries.RUNPOD_PLATFORM)
    assert {"MiniMaxH3ReferenceToVideo", "VHS_VideoCombine", "UNETLoader", "VAELoader"} <= set(recipe.health.required_nodes)
    pins = json.loads((ROOT / "astrid/packs/runpod/pins.json").read_text())["sources"]
    assert pins["comfyui"]["revision"] == COMFY_PIN
    assert "comfyui-plain" not in pins
