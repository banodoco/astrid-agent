"""The two capability-matrix copies must stay byte-identical."""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_capability_matrix_copies_are_identical():
    source = REPO_ROOT / "config" / "astrid-beta-capabilities.json"
    packaged = REPO_ROOT / "astrid" / "resources" / "astrid-beta-capabilities.json"
    assert source.read_bytes() == packaged.read_bytes(), (
        "config/ and astrid/resources/ copies of astrid-beta-capabilities.json have drifted; "
        "update both together"
    )


def test_runpod_host_target_operations_have_cpu_matrix_rows():
    import json
    import yaml

    manifest = yaml.safe_load((REPO_ROOT / "astrid/packs/runpod/pack.yaml").read_text(encoding="utf-8"))
    operation_ids = {
        f"runpod.{name}"
        for name, action in manifest["actions"].items()
        if action.get("metadata", {}).get("host_target_operation")
    }
    matrix = json.loads((REPO_ROOT / "config/astrid-beta-capabilities.json").read_text(encoding="utf-8"))
    rows = {row["id"]: row for row in matrix["capabilities"]}

    assert operation_ids
    # Declared host-target operations and the RunPod CPU matrix rows are maintained apart; they must agree exactly,
    # so a lost declaration, a missing row and an orphan row all fail.
    assert operation_ids == {
        capability_id
        for capability_id, row in rows.items()
        if capability_id.startswith("runpod.") and row["adapter_family"] == "cpu"
    }
    for capability_id in operation_ids:
        assert rows[capability_id]["adapter_family"] == "cpu"
        assert rows[capability_id]["resource_keys"] == ["cpu"]
