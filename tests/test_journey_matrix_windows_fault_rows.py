"""The Windows fault inventory must not alter the existing journey inventory."""

from scripts import journey_matrix as matrix


def test_windows_fault_rows_are_unique_and_bound_to_the_installed_helper():
    rows = matrix.DIRECT_WINDOWS_FAULT_SCENARIOS
    assert [(row["scenario_id"], row["scenario"], row["steps"]) for row in rows] == [
        ("direct-windows-interrupted-update", "interrupted-update", ("baseline", "interrupt", "recover", "verify")),
        ("direct-windows-rollback", "rollback", ("baseline", "fail_candidate", "verify")),
    ]
    identifiers = [row["scenario_id"] for row in rows]
    assert len(set(identifiers)) == len(identifiers)
    assert set(identifiers).isdisjoint(matrix.RELEASE1_JOB_IDS)
    assert set(identifiers).isdisjoint(matrix.M4_5_SCENARIO_IDS)
    assert matrix.M4_4B_LOCAL_SMOKE["scenario_id"] not in identifiers
    for row in rows:
        assert row["job_id"] == row["scenario_id"]
        assert (row["mode"], row["os"], row["runner"], row["shell"], row["manager"]) == (
            "direct", "windows", "windows-2022", "powershell.exe", "task-scheduler"
        )
        assert row["helper"] == "failure_mode_executor.execute_installed_fault"
        assert set(row["required_evidence"]) >= {
            "direct-installed-release0-fixture-and-native-service",
            "populated-semantic-data-and-live-sentinel-before",
            "unchanged-semantic-data-and-same-live-sentinel-after",
            "task-scheduler-ownership-and-keep-data-cleanup",
            "per-step-command-and-inner-exits-and-sha256-evidence-binding",
            "candidate-tuple-and-runner-provenance",
        }
    assert "durable-interrupted-operation-and-same-operation-terminal-journal" in rows[0]["required_evidence"]
    assert "selected-installed-program-and-restored-healthy-native-service" in rows[0]["required_evidence"]
    assert "observed-failed-candidate-operation-and-terminal-rollback-journal" in rows[1]["required_evidence"]
    assert "authentic-selected-release0-predecessor-and-restored-healthy-native-service" in rows[1]["required_evidence"]


def test_existing_journey_inventory_is_preserved():
    assert matrix.RELEASE1_MATRIX == (
        {"job_id": "direct-macos", "mode": "direct", "runner": "macos-14", "shell": "sh", "os": "macos", "leg": "release0-to-release1"},
        {"job_id": "direct-linux", "mode": "direct", "runner": "ubuntu-24.04", "shell": "sh", "os": "linux", "leg": "release0-to-release1"},
        {"job_id": "direct-windows", "mode": "direct", "runner": "windows-2022", "shell": "powershell.exe", "os": "windows", "leg": "release0-to-release1"},
        {"job_id": "pinokio-backend-macos", "mode": "pinokio-backend", "runner": "macos-14", "shell": "sh", "os": "macos"},
        {"job_id": "pinokio-backend-linux", "mode": "pinokio-backend", "runner": "ubuntu-24.04", "shell": "sh", "os": "linux"},
        {"job_id": "pinokio-backend-windows", "mode": "pinokio-backend", "runner": "windows-2022", "shell": "powershell.exe", "os": "windows"},
        {"job_id": "pinokio-fully-local-macos", "mode": "pinokio-fully-local", "runner": "macos-14", "shell": "sh", "os": "macos"},
        {"job_id": "pinokio-fully-local-linux", "mode": "pinokio-fully-local", "runner": "ubuntu-24.04", "shell": "sh", "os": "linux"},
        {"job_id": "pinokio-fully-local-windows", "mode": "pinokio-fully-local", "runner": "windows-2022", "shell": "powershell.exe", "os": "windows"},
    )
    assert matrix.M4_5_SCENARIO_IDS == tuple(
        f"release2-{channel}-{mode}-{os_name}"
        for os_name in ("macos", "linux", "windows")
        for channel, mode in (
            ("button", "direct"),
            ("install-line", "direct"),
            ("pinokio-update", "pinokio-backend"),
            ("pinokio-update", "pinokio-fully-local"),
        )
    )
    assert matrix.M4_6_MATRIX == matrix.RELEASE1_MATRIX
