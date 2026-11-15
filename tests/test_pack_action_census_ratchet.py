"""Bundled pack actions must be reviewed in both approved capability projections."""

import json
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def uncovered_pack_actions(pack_root, executor_inventory, capability_rows):
    inventory = set(executor_inventory)
    matrix = {row["id"] for row in capability_rows}
    missing = []
    for manifest_path in sorted(pack_root.glob("*/pack.yaml")):
        manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
        pack_id = manifest["id"]
        for action_id in manifest.get("actions", {}):
            capability_id = f"{pack_id}.{action_id}"
            if capability_id not in inventory:
                missing.append((capability_id, "executor_inventory"))
            if capability_id not in matrix:
                missing.append((capability_id, "capability_matrix"))
    return missing


def test_every_bundled_pack_action_is_in_approved_inventory_and_matrix():
    lock = json.loads((ROOT / "config/astrid-capability-census.lock.json").read_text(encoding="utf-8"))
    matrix = json.loads((ROOT / "config/astrid-beta-capabilities.json").read_text(encoding="utf-8"))
    missing = uncovered_pack_actions(
        ROOT / "astrid/packs", lock["executor_inventory"], matrix["capabilities"]
    )
    assert not missing, (
        f"Bundled actions are absent from reviewed capability projections: {missing}. "
        "If this is an intentional addition, run `python -m astrid.core.execution.capability_ledger approve` "
        "and review its diff."
    )


def test_action_census_ratchet_detects_a_missing_reviewed_row():
    pack_root = ROOT / "astrid/packs"
    lock = json.loads((ROOT / "config/astrid-capability-census.lock.json").read_text(encoding="utf-8"))
    matrix = json.loads((ROOT / "config/astrid-beta-capabilities.json").read_text(encoding="utf-8"))
    action = "runpod.claim"
    assert (action, "executor_inventory") in uncovered_pack_actions(
        pack_root, [item for item in lock["executor_inventory"] if item != action], matrix["capabilities"]
    )
