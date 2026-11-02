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

RELEASE1_MATRIX: Final = (
    {"job_id": "direct-macos", "mode": "direct", "runner": "macos-15", "shell": "sh", "os": "macos", "leg": "release0-to-release1"},
    {"job_id": "direct-linux", "mode": "direct", "runner": "ubuntu-24.04", "shell": "sh", "os": "linux", "leg": "release0-to-release1"},
    {"job_id": "direct-windows", "mode": "direct", "runner": "windows-2022", "shell": "powershell.exe", "os": "windows", "leg": "release0-to-release1"},
    {"job_id": "pinokio-backend-macos", "mode": "pinokio-backend", "runner": "macos-15", "shell": "sh", "os": "macos"},
    {"job_id": "pinokio-backend-linux", "mode": "pinokio-backend", "runner": "ubuntu-24.04", "shell": "sh", "os": "linux"},
    {"job_id": "pinokio-backend-windows", "mode": "pinokio-backend", "runner": "windows-2022", "shell": "powershell.exe", "os": "windows"},
    {"job_id": "pinokio-fully-local-macos", "mode": "pinokio-fully-local", "runner": "macos-15", "shell": "sh", "os": "macos"},
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

# Release end-state inventory, ordered by the orchestration request. These are
# obligations, not receipts: route availability never implies a hosted pass.
# The end-state document named by orchestration was unavailable; this inventory
# uses the explicit R-37/review requirements and the checked-in contracts.
END_STATE_PHASES: Final = (
    "fresh-install", "populated-r0-update", "interruption-recovery",
    "rollback", "r2", "linux-app-guard",
)
END_STATE_TUPLE: Final = {
    "app": "36ff224e3c9ad862e3f807e2548b997a36799543",
    "agent": "020877f12ea490308a9e5f9564c8f4a4bf77ede1",
    "runtime": "47e5949465cb6f3afdfd1cb8a0e90009fd9d4020",
    "pinokio": "89ed7aacae72bb48eb97ff9f95c222bdd7e71e4",
}

# Each obligation has a stable ID, an actual route/assertion where one exists,
# and a separate route state. "unimplemented" names the missing acceptance
# route, even where a weaker executable or receipt predicate already exists.
END_STATE_INVENTORY: Final = (
    *(
        {
            "id": f"fresh-install:{row['job_id']}", "phase": "fresh-install",
            "job_id": row["job_id"], "os": row["os"], "channel": row["mode"],
            "owner": "M-QUAL" if row["mode"] == "direct" else "M-PIN",
            "route": "ci_journey.py --leg fresh-install",
            "route_state": "execution-capable" if row["mode"] != "direct" else "unimplemented",
            "dependency": ("authenticated Pinokio adapter and hosted receipt" if row["mode"] != "direct"
                           else "M-QUAL/M-CI: distinct direct fresh-install workflow row and receipt identity"),
            "predicate": "hosted installed candidate, owned service, health, settled task, one Runtime, sentinel and cleanup",
            "source": "ci_journey.py; check_journey_evidence.py:evaluate_release1_matrix",
        } for row in RELEASE1_MATRIX
    ),
    *(
        {
            "id": f"populated-r0-update:{row['job_id']}", "phase": "populated-r0-update",
            "job_id": row["job_id"], "os": row["os"], "channel": row["mode"], "owner": "M-QUAL",
            "route": "journey_version_jumps.py:execute_release0_to_release1",
            "route_state": "execution-capable", "dependency": "authentic hosted exact-tuple receipt",
            "predicate": "frozen R0 provenance, seeded semantic data preserved, R1 identity, service, health, task and cleanup",
            "source": "check_journey_evidence.py:evaluate_release1_matrix",
        } for row in RELEASE1_MATRIX if row["mode"] == "direct"
    ),
    {
        "id": "populated-v31-to-v33-backup-restore", "phase": "populated-r0-update",
        "owner": "M-QUAL/M-RT", "route": None, "route_state": "unimplemented",
        "dependency": "M-QUAL/M-RT: bind populated authentic R0 schema versions and backup/restore observations to a hosted journey",
        "predicate": "authentic populated v31 migrates to v33 with semantic preservation and verified backup and restore",
        "source": "ASTRA-REVIEW.md: migration; journey_version_jumps.py:execute_release0_to_release1",
    },
    *(
        {
            "id": f"interruption-recovery:{row['job_id']}", "phase": "interruption-recovery",
            "job_id": row["job_id"], "os": row["os"], "channel": row["mode"],
            "owner": "M-QUAL" if row["mode"] == "direct" else "M-PIN",
            "route": "journey_failure_modes.py:scenario_errors(interruption_recovery)",
            "route_state": "unimplemented", "dependency": "hosted native-service M4-6 adapter and authenticated receipt",
            "predicate": "injected interruption, journal, explicit same-operation recovery, terminal identity and preserved data",
            "source": "journey_failure_modes.py:_interruption_errors",
        } for row in M4_6_MATRIX
    ),
    {
        "id": "interrupted-migration-restore", "phase": "interruption-recovery", "owner": "M-QUAL/M-RT",
        "route": None, "route_state": "unimplemented",
        "dependency": "M-QUAL/M-RT: hosted migration interruption and restore observations",
        "predicate": "interrupt populated v31-to-v33 migration, restore and verify preserved usable data",
        "source": "ASTRA-REVIEW.md: migration",
    },
    *(
        {
            "id": f"rollback:{row['job_id']}", "phase": "rollback", "job_id": row["job_id"],
            "os": row["os"], "channel": row["mode"],
            "owner": "M-QUAL" if row["mode"] == "direct" else "M-PIN",
            "route": "journey_failure_modes.py:scenario_errors(failed_candidate_rollback)",
            "route_state": "unimplemented", "dependency": "hosted native-service M4-6 adapter and authenticated receipt",
            "predicate": "controlled candidate failure; actual R0 executable identity, healthy restart and semantic data preserved",
            "source": "journey_failure_modes.py:_rollback_errors",
        } for row in M4_6_MATRIX
    ),
    *(
        {
            "id": f"{scenario}:{row['job_id']}", "phase": "rollback", "job_id": row["job_id"],
            "os": row["os"], "channel": row["mode"],
            "owner": "M-QUAL" if row["mode"] == "direct" else "M-PIN",
            "route": f"journey_failure_modes.py:scenario_errors({scenario})", "route_state": "unimplemented",
            "dependency": "hosted native-service M4-6 adapter and authenticated receipt",
            "predicate": predicate, "source": f"journey_failure_modes.py:_{scenario.replace('keep_data_uninstall', 'uninstall')}_errors",
        } for scenario, predicate in (
            ("keep_data_uninstall", "remove program and service, retain workspace/CAS and semantic data, preserve sentinel"),
            ("stop_safety", "refused and unknown stop do not update, retry, activate or change data or R0 identity"),
        ) for row in M4_6_MATRIX
    ),
    *(
        {
            "id": row["scenario_id"], "phase": "r2", "job_id": row["job_id"], "os": row["os"],
            "channel": row["channel"], "mode": row["mode"],
            "owner": "M-QUAL" if row["mode"] == "direct" else "M-PIN",
            "route": "ci_journey.py --shared-executor m4-5", "route_state": "unimplemented",
            "dependency": "current-tuple M4-5 admission and authenticated installed boundary/hosted receipt",
            "predicate": "visible installed R2 stand-in identity, channel trigger, preserved data, service, task and cleanup",
            "source": "journey_release2.py:receipt_errors",
        } for row in M4_5_MATRIX
    ),
    {
        "id": "r0-to-r2-skip", "phase": "r2", "owner": "M-QUAL",
        "route": None, "route_state": "unimplemented",
        "dependency": "R-16 actual worker bridge execution and authentic R0-to-stand-in-R2 skip receipt",
        "predicate": "actual worker executes authentic R0-to-R2 skip with bridge/job/journal linkage and preserved data",
        "source": "ASTRA-TASK-REVIEWS.md: R-16",
    },
    {
        "id": "linux-unsaved-draft-browser", "phase": "linux-app-guard", "os": "linux", "channel": "button",
        "job_id": "direct-linux", "owner": "M-APP/M-QUAL",
        "route": "release2-button-direct-linux:unsaved-draft", "route_state": "unimplemented",
        "dependency": "current-tuple real installed browser/Runtime execution and authenticated receipt (I-AP-03/I-AP-08)",
        "predicate": "failed save: zero POST/jobs; successful save: one POST and native job, installed R2; include chat drafts and stale marker",
        "source": "journey_release2.py:_button_errors; ASTRA-TASK-REVIEWS.md: I-AP-03/I-AP-08",
    },
)


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
