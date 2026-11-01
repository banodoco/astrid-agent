"""Focused inventory contract checks for the installed-App update guard."""

from scripts import journey_matrix


def test_linux_installed_app_update_guard_is_unique_and_separate():
    rows = journey_matrix.APP_UPDATE_GUARD_MATRIX
    assert len(rows) == 1
    row = rows[0]

    assert row["scenario_id"] == "installed-app-update-guard-linux"
    assert journey_matrix.APP_UPDATE_GUARD_SCENARIO_IDS == (row["scenario_id"],)
    assert row["section"] == "installed-app-update-guard"
    assert row["runner"] == "ubuntu-24.04"
    assert row["runner_os"] == "Linux"
    assert row["evidence_class"] == "installed_app"
    assert row["shell"] == "sh"


def test_guard_inventory_keeps_both_exact_post_count_predicates():
    row = journey_matrix.APP_UPDATE_GUARD_MATRIX[0]
    phases = {phase["phase_id"]: phase for phase in row["phases"]}

    assert phases["unresolved-save"] == {
        "phase_id": "unresolved-save",
        "save_state": "unresolved",
        "expected_update_post_count": 0,
    }
    assert phases["settled-save"] == {
        "phase_id": "settled-save",
        "save_state": "settled",
        "expected_update_post_count": 1,
    }


def test_guard_evidence_requires_installed_tuple_and_fails_closed_when_missing():
    row = journey_matrix.APP_UPDATE_GUARD_MATRIX[0]

    assert row["evidence_class"] == "installed_app"
    assert row["tuple_binding"] == {
        "candidate_tuple_fields": ("app", "agent", "runtime", "pinokio"),
        "installed_tuple_must_equal_candidate": True,
        "manifest_digest_required": True,
        "installed_app_identity_required": True,
    }
    assert set(row["required_observations"]) >= {
        "app_update_guard_phase_results",
        "update_post_outbox_observation",
        "runner_provenance.RUNNER_OS",
        "runner_provenance.ImageOS",
        "runner_provenance.ImageVersion",
        "candidate_tuple",
        "installed_tuple",
        "manifest_sha256",
        "installed_app_identity",
    }
    assert row["missing_observation"] == "fail_closed"


def test_i_ap_08_is_contract_reference_only():
    provenance = journey_matrix.APP_UPDATE_GUARD_MATRIX[0]["evidence_provenance"]

    assert provenance["owner"] == "M-APP"
    assert provenance["contract_reference"] == "I-AP-08"
    assert provenance["reference_role"] == "contract_only_not_final_tuple_execution"
