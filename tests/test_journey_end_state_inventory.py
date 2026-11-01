"""Protect the end-state mapping independently of the producer matrices."""

from collections import Counter

import pytest

from scripts.journey_matrix import (
    END_STATE_INVENTORY, END_STATE_PHASES, END_STATE_TUPLE,
    M4_5_SCENARIO_IDS, M4_6_SLOT_IDS, RELEASE1_JOB_IDS,
)

JOBS = {
    f"{mode}-{os_name}"
    for mode in ("direct", "pinokio-backend", "pinokio-fully-local")
    for os_name in ("macos", "linux", "windows")
}
R2 = {
    f"release2-{channel}-{mode}-{os_name}"
    for channel, mode in (("button", "direct"), ("install-line", "direct"),
                          ("pinokio-update", "pinokio-backend"),
                          ("pinokio-update", "pinokio-fully-local"))
    for os_name in ("macos", "linux", "windows")
}
REQUIRED = (
    {f"fresh-install:{job}" for job in JOBS}
    | {f"populated-r0-update:direct-{os_name}" for os_name in ("macos", "linux", "windows")}
    | {f"{scenario}:{job}" for scenario in
       ("interruption-recovery", "rollback", "keep_data_uninstall", "stop_safety") for job in JOBS}
    | R2
    | {"populated-v31-to-v33-backup-restore", "interrupted-migration-restore",
       "r0-to-r2-skip", "linux-unsaved-draft-browser"}
)


def assert_complete(rows):
    ids = [row["id"] for row in rows]
    assert len(ids) == len(set(ids))
    assert set(ids) == REQUIRED
    assert all(row.get("owner") and row.get("predicate") and row.get("source")
               and row.get("dependency") for row in rows)
    assert all(row.get("route_state") in {"execution-capable", "unimplemented"} for row in rows)
    assert all(row.get("route") for row in rows if row["route_state"] == "execution-capable")


def test_inventory_is_complete_ordered_and_bound_to_current_tuple():
    assert set(RELEASE1_JOB_IDS) == JOBS and len(RELEASE1_JOB_IDS) == 9
    assert set(M4_6_SLOT_IDS) == JOBS and len(M4_6_SLOT_IDS) == 9
    assert set(M4_5_SCENARIO_IDS) == R2 and len(M4_5_SCENARIO_IDS) == 12
    assert END_STATE_TUPLE == {
        "app": "36ff224e3c9ad862e3f807e2548b997a36799543",
        "agent": "020877f12ea490308a9e5f9564c8f4a4bf77ede1",
        "runtime": "47e5949465cb6f3afdfd1cb8a0e90009fd9d4020",
        "pinokio": "89ed7aacae72bb48eb97ff9f95c222bdd7e71e4",
    }
    assert_complete(END_STATE_INVENTORY)
    phases = [row["phase"] for row in END_STATE_INVENTORY]
    assert tuple(dict.fromkeys(phases)) == END_STATE_PHASES
    assert phases == sorted(phases, key=END_STATE_PHASES.index)
    assert Counter(phases) == dict(zip(END_STATE_PHASES, (9, 4, 10, 27, 13, 1)))


def test_every_omission_and_duplicate_is_detected():
    for index in range(len(END_STATE_INVENTORY)):
        with pytest.raises(AssertionError):
            assert_complete(END_STATE_INVENTORY[:index] + END_STATE_INVENTORY[index + 1:])
    with pytest.raises(AssertionError):
        assert_complete(END_STATE_INVENTORY + (END_STATE_INVENTORY[0],))


def test_missing_routes_do_not_pose_as_hosted_evidence():
    rows = {row["id"]: row for row in END_STATE_INVENTORY}
    for identity in ("fresh-install:direct-linux", "populated-v31-to-v33-backup-restore",
                     "interruption-recovery:direct-linux", "rollback:direct-linux",
                     "release2-button-direct-linux", "r0-to-r2-skip", "linux-unsaved-draft-browser"):
        assert rows[identity]["route_state"] == "unimplemented"
    assert rows["populated-r0-update:direct-linux"]["route_state"] == "execution-capable"
    assert all("receipt_status" not in row for row in rows.values())
