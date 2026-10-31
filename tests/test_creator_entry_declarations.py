"""Creator-facing intent stays descriptive; declared actions define runnable work."""

from __future__ import annotations

from pathlib import Path

import yaml

from astrid.core.pack.agent_index import build_agent_index
from astrid.core.pack.validate import validate_pack


ROOT = Path(__file__).resolve().parents[1]


def _manifest(pack_id: str) -> dict:
    path = ROOT / "astrid" / "packs" / pack_id / "pack.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_pack_intent_projection_keeps_creator_selection_clear() -> None:
    expected = {
        "vibecomfy": {
            "entrypoints": [
                "vibecomfy.import", "vibecomfy.inspect", "vibecomfy.edit",
                "vibecomfy.run", "vibecomfy.validate", "vibecomfy.video_enhance",
                "vibecomfy.character_animation",
            ],
            "intent": "registered catalog or workflow modes",
        },
        "wan2gp": {
            "entrypoints": ["wan2gp.generate_video", "wan2gp.validate_settings"],
            "intent": "explicitly selected",
        },
        "fal": {
            "entrypoints": ["fal.fal_foley", "fal.h3_video"],
            "intent": "explicitly selected",
        },
    }

    for pack_id, contract in expected.items():
        projected = build_agent_index(pack_id=pack_id)
        assert projected is not None
        assert projected["normal_entrypoints"] == contract["entrypoints"]
        assert contract["intent"] in projected["purpose"]
        assert projected["required_context"] == ["docs/SKILL.md"]

    fal = build_agent_index(pack_id="fal")
    assert "never an automatic default provider" in fal["purpose"]
    assert "automatic/default provider" in fal["do_not_use_for"]


def test_only_declared_generation_actions_carry_generation_publication() -> None:
    declared = {
        "vibecomfy": {"video_enhance", "character_animation"},
        "wan2gp": {"generate_video"},
        "fal": {"fal_foley", "h3_video"},
    }

    for pack_id, action_ids in declared.items():
        actions = _manifest(pack_id)["actions"]
        published = {
            action_id
            for action_id, action in actions.items()
            if action.get("metadata", {}).get("generation_publication")
        }
        assert published == action_ids

    vibecomfy = _manifest("vibecomfy")["actions"]
    assert not vibecomfy["import"].get("metadata", {}).get("generation_publication")
    assert not vibecomfy["inspect"].get("metadata", {}).get("generation_publication")
    assert not vibecomfy["edit"].get("metadata", {}).get("generation_publication")
    assert not vibecomfy["validate"].get("metadata", {}).get("generation_publication")
    assert not vibecomfy["run"].get("metadata", {}).get("generation_publication")
    wan2gp = _manifest("wan2gp")["actions"]
    assert not wan2gp["validate_settings"].get("metadata", {}).get("generation_publication")


def test_changed_pack_declarations_pass_pack_validation() -> None:
    for pack_id in ("vibecomfy", "wan2gp", "fal"):
        errors, _warnings = validate_pack(ROOT / "astrid" / "packs" / pack_id)
        assert errors == [], f"{pack_id}: {errors}"
