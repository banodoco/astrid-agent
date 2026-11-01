"""The fixed Release-1 installed-lifecycle receipt contract.

This is deliberately a small data-only module so the driver, evidence checker and
workflow tests share the same nine required jobs.  A mode name is an ownership
statement, not permission for a direct installer journey to fall back to an
unmanaged foreground process.
"""

from __future__ import annotations

from typing import Final

if __package__:
    from .journey_release2 import RELEASE2_SCENARIOS
else:
    try:
        from journey_release2 import RELEASE2_SCENARIOS
    except ModuleNotFoundError:
        from scripts.journey_release2 import RELEASE2_SCENARIOS

# Independently attributable direct Linux faults. Each row requires a fresh
# fixture installation; these contracts are not yet routed by the workflow.
DIRECT_LINUX_FAULT_SCENARIOS: Final = (
    {
        "scenario_id": "direct-linux-interrupted-update",
        "job_id": "direct-linux-interrupted-update",
        "scenario": "interrupted-update",
        "mode": "direct", "os": "linux", "runner": "ubuntu-24.04", "shell": "sh",
        "steps": ("baseline", "interrupt", "recover", "verify"),
        "required_evidence": (
            "direct-installed-release0-fixture-and-native-service",
            "populated-semantic-data-and-live-sentinel-before",
            "durable-interrupted-operation-and-same-operation-terminal-journal",
            "selected-installed-program-and-restored-healthy-native-service",
            "unchanged-semantic-data-and-same-live-sentinel-after",
            "per-step-command-and-inner-exits-and-sha256-evidence-binding",
            "candidate-tuple-and-runner-provenance",
        ),
    },
    {
        "scenario_id": "direct-linux-rollback",
        "job_id": "direct-linux-rollback",
        "scenario": "rollback",
        "mode": "direct", "os": "linux", "runner": "ubuntu-24.04", "shell": "sh",
        "steps": ("baseline", "fail_candidate", "verify"),
        "required_evidence": (
            "direct-installed-release0-fixture-and-native-service",
            "populated-semantic-data-and-live-sentinel-before",
            "observed-failed-candidate-operation-and-terminal-rollback-journal",
            "authentic-selected-release0-predecessor-and-restored-healthy-native-service",
            "unchanged-semantic-data-and-same-live-sentinel-after",
            "per-step-command-and-inner-exits-and-sha256-evidence-binding",
            "candidate-tuple-and-runner-provenance",
        ),
    },
)

# macOS fault declarations are separate from the existing nine routed jobs.
DIRECT_MACOS_FAULT_SCENARIOS: Final = (
    # macOS direct installed fault journeys. Each row requires its own receipt;
    # disposable executor observations cannot stand in for installed state.
    {
        "scenario_id": "direct-macos-interrupted-update",
        "job_id": "direct-macos-interrupted-update", "mode": "direct",
        "runner": "macos-14", "shell": "sh", "os": "macos",
        "leg": "interrupted-update", "manager": "launchd",
        "executor": "failure_mode_executor.execute_installed_fault",
        "steps": ("baseline", "interrupt", "recover", "verify"),
        "required_evidence": (
            "release0_fixture_and_installed_identity", "populated_data_before_and_after",
            "sentinel_identity_and_survival", "interruption_and_same_operation_terminal_journal",
            "installed_identity_after_recovery", "launchd_ownership_health_and_one_runtime",
            "keep_data_and_launchd_process_cleanup", "per_step_command_and_inner_exit",
            "scenario_tuple_runner_and_evidence_file_hashes",
        ),
    },
    {
        "scenario_id": "direct-macos-rollback",
        "job_id": "direct-macos-rollback", "mode": "direct",
        "runner": "macos-14", "shell": "sh", "os": "macos",
        "leg": "rollback", "manager": "launchd",
        "executor": "failure_mode_executor.execute_installed_fault",
        "steps": ("baseline", "fail_candidate", "verify"),
        "required_evidence": (
            "release0_fixture_and_installed_identity", "populated_data_before_and_after",
            "sentinel_identity_and_survival", "authenticated_selected_release0_predecessor_identity",
            "candidate_failure_and_rollback_transaction_journal", "restored_predecessor_identity",
            "launchd_ownership_health_and_one_runtime", "keep_data_and_launchd_process_cleanup",
            "per_step_command_and_inner_exit", "scenario_tuple_runner_and_evidence_file_hashes",
        ),
    },
)

RELEASE1_MATRIX: Final = (
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

RELEASE1_JOB_IDS: Final = tuple(row["job_id"] for row in RELEASE1_MATRIX)
RELEASE1_MODES: Final = tuple(sorted({row["mode"] for row in RELEASE1_MATRIX}))

# M4-4a is deliberately a narrower contract than the Release-1 matrix. Keep
# all of its immutable receipt vocabulary here so the driver and checker cannot
# silently drift apart.
M4_4A_OLD_LAUNCHER_REF: Final = "bbba620e3d89eeff8021417f365e6ea584203105"
M4_4A_OLD_UPDATE_SHA256: Final = "a0f998c03bbba57966c8863a3bd38411d7859d970ddc4c93af1e28b04a66291b"
M4_4A_PROVISIONAL_AGENT_REF: Final = "3cc84be5d8c565f1a4a500dcf713478498e34885"
M4_4A_REQUIRED_AGENT_ANCESTORS: Final = (
    "14d87abe38cd2545411a84549d5e4edc5a33561d",  # paired M1-4a prerequisite
    "b97a4757a908982c978e46c90898a816c0c5d9ee",  # M1-4b stopped-path implementation
)
M4_4A_BACKEND_SIDECARS: Final = (
    "action-array-first.json", "action-array-retry.json", "events.jsonl",
    "first-click.stdout.log", "first-click.stderr.log",
    "status-after-update.json", "health-after-update.json", "menu-after-update.json",
    "status-after-restart.json", "health-after-restart.json", "menu-after-restart.json",
    "processes-before.json", "processes-after-first-click.json",
    "processes-after-stop.json", "processes-after-retry-ready.json",
    "processes-after-final-stop.json", "processes-after-restart-ready.json",
    "negative-controls.json",
)
M4_4A_REQUIRED_EVENTS: Final = (
    "action_built", "launcher_ref_advanced", "first_click_begin",
    "launcher_pull_begin", "launcher_pull_end", "installer_begin",
    "installer_end", "ready_sentinel", "action_end", "health_probe_begin",
    "health_probe_pass", "stop_begin", "stop_end", "retry_begin", "retry_end",
    "start_begin", "start_end",
)
M4_4A_BACKEND_MATRIX: Final = tuple(
    {**row, "leg": row.get("leg", "fresh-install")}
    for row in RELEASE1_MATRIX if row["mode"] == "pinokio-backend"
)
M4_4A_BACKEND_JOB_IDS: Final = tuple(row["job_id"] for row in M4_4A_BACKEND_MATRIX)

# M4-4b is executable local-process smoke coverage.  Its evidence class is
# intentionally separate from every hosted Release-1 row.
M4_4B_LOCAL_SMOKE: Final = {
    "scenario_id": "m4-4b-fully-local-linux-smoke",
    "runner": "ubuntu-24.04",
    "shell": "sh",
    "os": "linux-or-macos",
    "evidence_class": "local_process",
    "hosted": False,
}

# M4-5 rows are keyed by scenario_id because the two direct channels share a
# job_id for each OS.  M4-6 reuses the nine Release-1 ownership/OS tuples.
M4_5_MATRIX: Final = RELEASE2_SCENARIOS
M4_5_SCENARIO_IDS: Final = tuple(row["scenario_id"] for row in M4_5_MATRIX)
M4_6_MATRIX: Final = RELEASE1_MATRIX
M4_6_SLOT_IDS: Final = RELEASE1_JOB_IDS


def m4_4a_backend_job(job_id: str) -> dict:
    for row in M4_4A_BACKEND_MATRIX:
        if row["job_id"] == job_id:
            return dict(row)
    raise ValueError(f"Unknown M4-4a backend job: {job_id}")


def validate_m4_4a_backend_job(job_id: str, mode: str) -> dict:
    row = m4_4a_backend_job(job_id)
    if row["mode"] != mode:
        raise ValueError(f"M4-4a backend job {job_id} requires mode {row['mode']}, not {mode}")
    return row


def release1_job(job_id: str) -> dict:
    """Return a copy so callers cannot mutate the checked-in matrix."""
    for row in RELEASE1_MATRIX:
        if row["job_id"] == job_id:
            return dict(row)
    raise ValueError(f"Unknown Release-1 journey job: {job_id}")


def validate_job(job_id: str, mode: str) -> dict:
    row = release1_job(job_id)
    if row["mode"] != mode:
        raise ValueError(f"Release-1 job {job_id} requires mode {row['mode']}, not {mode}")
    return row


def validate_m4_5_scenario(scenario_id: str) -> dict:
    for row in M4_5_MATRIX:
        if row["scenario_id"] == scenario_id:
            return dict(row)
    raise ValueError(f"Unknown M4-5 scenario: {scenario_id}")


def validate_m4_6_slot(slot_id: str) -> dict:
    for row in M4_6_MATRIX:
        if row["job_id"] == slot_id:
            return dict(row)
    raise ValueError(f"Unknown M4-6 failure-mode slot: {slot_id}")
