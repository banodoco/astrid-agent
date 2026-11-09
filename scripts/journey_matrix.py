"""The fixed Release-1 installed-lifecycle receipt contract.

This is deliberately a small data-only module so the driver, evidence checker and
workflow tests share the same five required direct jobs.  The six Pinokio rows
are deferred to Release 1.1 and kept separately below.  A mode name is an ownership
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

_RELEASE1_DIRECT_LEGACY: Final = (
    {"job_id": "direct-macos", "mode": "direct", "runner": "macos-15", "shell": "sh", "os": "macos", "leg": "r0-handoff-then-update"},
    {"job_id": "direct-linux", "mode": "direct", "runner": "ubuntu-24.04", "shell": "sh", "os": "linux", "leg": "r0-handoff-then-update"},
    {"job_id": "direct-windows", "mode": "direct", "runner": "windows-2022", "shell": "powershell.exe", "os": "windows", "leg": "fresh-install"},
)
# The macOS and Linux upgrade rows start where real pre-R1 installs are: a
# frozen R0 handed off to R1 storage, then the candidate's install --update.
# Fresh install is the main Release-1 path, so Linux and macOS each have a
# distinct strict fresh-install row beside their release-0 upgrade row.
RELEASE1_DIRECT_FRESH_MATRIX: Final = (
    {"job_id": "direct-macos-fresh", "mode": "direct", "runner": "macos-15", "shell": "sh", "os": "macos", "leg": "fresh-install"},
    {"job_id": "direct-linux-fresh", "mode": "direct", "runner": "ubuntu-24.04", "shell": "sh", "os": "linux", "leg": "fresh-install"},
)
RELEASE1_DIRECT_FRESH_JOB_IDS: Final = tuple(row["job_id"] for row in RELEASE1_DIRECT_FRESH_MATRIX)
# The one fresh row whose update is the hosted App build's own Update now, clicked in a browser;
# the other fresh rows send the page's request to the Runtime door themselves.
HOSTED_BUTTON_JOB_IDS: Final = frozenset({"direct-linux-fresh"})
# The release9-then-install rows are the strict Release-9 leg on Linux and macOS: the pinned,
# published Release 9 installed plainly, then the candidate from the same seeded workspace.
# They are not the r0-handoff rows and are not keep-going diagnostics.
RELEASE9_INSTALL_MATRIX: Final = (
    {"job_id": "direct-linux-r9-install", "mode": "direct", "runner": "ubuntu-24.04", "shell": "sh", "os": "linux", "leg": "release9-then-install"},
    {"job_id": "direct-macos-r9-install", "mode": "direct", "runner": "macos-15", "shell": "sh", "os": "macos", "leg": "release9-then-install"},
)
# The release11-then-update rows start from the served Release 11, installed plainly from its
# pinned App commit, and take the candidate's install --update to Release 12.
RELEASE11_UPDATE_MATRIX: Final = (
    {"job_id": "direct-linux-r11-update", "mode": "direct", "runner": "ubuntu-24.04", "shell": "sh", "os": "linux", "leg": "release11-then-update"},
    {"job_id": "direct-macos-r11-update", "mode": "direct", "runner": "macos-15", "shell": "sh", "os": "macos", "leg": "release11-then-update"},
)
RELEASE11_UPDATE_JOB_IDS: Final = tuple(row["job_id"] for row in RELEASE11_UPDATE_MATRIX)
# The Release-1 matrix is the nine direct rows of one full run: three fresh-install rows, two
# r0-handoff-then-update rows, two release9-then-install rows and two release11-then-update rows.
# The fresh-install-autostart-off rows are a real fresh install from the candidate tuple with the
# autostart choice off, on Linux and macOS. They check the selection record, the Runtime's own update
# status and refusal, and the App's capture of the autostart-off card at deviceScaleFactor 2.
RELEASE1_AUTOSTART_MATRIX: Final = (
    {"job_id": "direct-linux-autostart-off", "mode": "direct", "runner": "ubuntu-24.04", "shell": "sh", "os": "linux", "leg": "fresh-install-autostart-off"},
    {"job_id": "direct-macos-autostart-off", "mode": "direct", "runner": "macos-15", "shell": "sh", "os": "macos", "leg": "fresh-install-autostart-off"},
)
RELEASE1_MATRIX: Final = (*_RELEASE1_DIRECT_LEGACY, *RELEASE1_DIRECT_FRESH_MATRIX,
                          *RELEASE9_INSTALL_MATRIX, *RELEASE11_UPDATE_MATRIX, *RELEASE1_AUTOSTART_MATRIX)
# An automatic push or schedule (no row input) runs the five rows that existed before the
# release9 and release11 legs. row=direct runs all of RELEASE1_MATRIX.
RELEASE1_AUTOMATIC_MATRIX: Final = (*_RELEASE1_DIRECT_LEGACY, *RELEASE1_DIRECT_FRESH_MATRIX)
# The three rows the keep-going diagnostic re-legs (macOS and Linux R0, and Windows fresh).
RELEASE1_LEGACY_JOB_IDS: Final = tuple(row["job_id"] for row in _RELEASE1_DIRECT_LEGACY)

# The R1.1 Pinokio journey gate is a separate three-OS hosted backend matrix.
# Keep its stable job IDs distinct from the older M4-4a evidence labels.
RELEASE1_1_PINOKIO_MATRIX: Final = (
    {"job_id": "pinokio-linux", "mode": "pinokio-backend", "runner": "ubuntu-24.04", "shell": "sh", "os": "linux", "leg": "fresh-install"},
    {"job_id": "pinokio-macos", "mode": "pinokio-backend", "runner": "macos-15", "shell": "sh", "os": "macos", "leg": "fresh-install"},
    {"job_id": "pinokio-windows", "mode": "pinokio-backend", "runner": "windows-2022", "shell": "powershell.exe", "os": "windows", "leg": "fresh-install"},
)

# Deferred to Release 1.1: not part of the Release-1 strict matrix or aggregate.
# The driver still admits these jobs so the Pinokio code paths stay runnable.
RELEASE1_1_DEFERRED_PINOKIO_MATRIX: Final = (
    {"job_id": "pinokio-backend-macos", "mode": "pinokio-backend", "runner": "macos-15", "shell": "sh", "os": "macos"},
    {"job_id": "pinokio-backend-linux", "mode": "pinokio-backend", "runner": "ubuntu-24.04", "shell": "sh", "os": "linux"},
    {"job_id": "pinokio-backend-windows", "mode": "pinokio-backend", "runner": "windows-2022", "shell": "powershell.exe", "os": "windows"},
    {"job_id": "pinokio-fully-local-macos", "mode": "pinokio-fully-local", "runner": "macos-15", "shell": "sh", "os": "macos"},
    {"job_id": "pinokio-fully-local-linux", "mode": "pinokio-fully-local", "runner": "ubuntu-24.04", "shell": "sh", "os": "linux"},
    {"job_id": "pinokio-fully-local-windows", "mode": "pinokio-fully-local", "runner": "windows-2022", "shell": "powershell.exe", "os": "windows"},
)
# The end-state inventory is built from the five fresh and legacy rows, so it keeps its fixed
# obligations. The nine-row Release-1 run is admitted through RELEASE1_MATRIX above.
_ADMITTED_JOBS: Final = (*_RELEASE1_DIRECT_LEGACY, *RELEASE1_DIRECT_FRESH_MATRIX, *RELEASE1_1_DEFERRED_PINOKIO_MATRIX)

RELEASE1_JOB_IDS: Final = tuple(row["job_id"] for row in RELEASE1_MATRIX)
RELEASE1_MODES: Final = tuple(sorted({row["mode"] for row in _ADMITTED_JOBS}))

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
    for row in RELEASE1_1_DEFERRED_PINOKIO_MATRIX if row["mode"] == "pinokio-backend"
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
# job_id for each OS.  M4-6 keeps the nine ownership/OS tuples (three direct,
# six deferred Pinokio); the direct fresh rows add no new failure-mode slot.
M4_5_MATRIX: Final = RELEASE2_SCENARIOS
M4_5_SCENARIO_IDS: Final = tuple(row["scenario_id"] for row in M4_5_MATRIX)
M4_6_MATRIX: Final = (*_RELEASE1_DIRECT_LEGACY, *RELEASE1_1_DEFERRED_PINOKIO_MATRIX)
M4_6_SLOT_IDS: Final = tuple(row["job_id"] for row in M4_6_MATRIX)

# Release end-state inventory.  These are obligations rather than receipts:
# route availability never promotes an obligation to a passed release gate.
# Keeping the mappings next to the matrices lets the one evidence checker show
# every required obligation even before a producer/executor exists for it.
END_STATE_PHASES: Final = (
    "fresh-install", "populated-r0-update", "interruption-recovery",
    "rollback-uninstall-stop", "r2", "linux-app-guard",
)

END_STATE_INVENTORY: Final = (
    *(
        {
            "id": f"fresh-install:{row['job_id']}", "phase": "fresh-install",
            "job_id": row["job_id"], "os": row["os"], "channel": row["mode"],
            "owner": "M-QUAL" if row["mode"] == "direct" else "M-PIN",
            "route": "ci_journey.py --leg fresh-install",
            "route_state": "execution-capable",
            "dependency": ("authenticated Pinokio adapter and hosted receipt (deferred to R1.1)" if row["mode"] != "direct"
                           else "hosted Windows fresh and refusal receipt" if row["job_id"] == "direct-windows"
                           else "hosted direct fresh-R1 receipt"),
            "predicate": "installed candidate, owned service, health, settled task, one Runtime, sentinel and cleanup",
            "source": "check_journey_evidence.py:evaluate_release1_matrix",
            "evidence_kind": "release1",
        } for row in _ADMITTED_JOBS if row.get("leg", "fresh-install") == "fresh-install"
    ),
    *(
        {
            "id": ("windows-r0-upgrade-refusal:direct-windows" if row["job_id"] == "direct-windows"
                   else f"populated-r0-update:{row['job_id']}"), "phase": "populated-r0-update",
            "job_id": row["job_id"], "os": row["os"], "channel": row["mode"], "owner": "M-QUAL",
            "route": ("journey_version_jumps.py:execute_windows_refusal" if row["job_id"] == "direct-windows"
                      else "journey_version_jumps.py:execute_r0_handoff_then_update"),
            "route_state": "execution-capable", "dependency": "authentic hosted exact-tuple receipt",
            "predicate": ("frozen R0 provenance, refusal, preserved data marker and unchanged scheduled task; Windows upgrade deferred to R1.1"
                          if row["job_id"] == "direct-windows" else
                          "frozen R0 seeded, handed off by the published Release 9, then the candidate's install --update: "
                          "exit 0, candidate release installed and live, semantic data, service, health, task and cleanup"),
            "source": "check_journey_evidence.py:evaluate_release1_matrix", "evidence_kind": "release1",
        } for row in _RELEASE1_DIRECT_LEGACY
    ),
    {
        "id": "populated-v31-to-v33-backup-restore", "phase": "populated-r0-update",
        "owner": "M-QUAL/M-RT", "route": None, "route_state": "unimplemented",
        "dependency": "hosted populated migration and backup/restore observations",
        "predicate": "authentic populated v31 migrates to v33 with semantic preservation and verified backup and restore",
        "source": "END-STATE.md; R-37", "evidence_kind": "unsupported",
    },
    *(
        {
            "id": f"interruption-recovery:{row['job_id']}", "phase": "interruption-recovery",
            "job_id": row["job_id"], "os": row["os"], "channel": row["mode"],
            "owner": "M-QUAL" if row["mode"] == "direct" else "M-PIN",
            "route": "journey_failure_modes.py:scenario_errors(interruption_recovery)",
            "route_state": "unimplemented", "dependency": "hosted native-service M4-6 adapter and receipt",
            "predicate": "injected interruption, journal, explicit same-operation recovery, terminal identity and preserved data",
            "source": "journey_failure_modes.py:_interruption_errors", "evidence_kind": "m4-6",
        } for row in M4_6_MATRIX
    ),
    {
        "id": "interrupted-migration-restore", "phase": "interruption-recovery", "owner": "M-QUAL/M-RT",
        "route": None, "route_state": "unimplemented",
        "dependency": "hosted migration interruption and restore observations",
        "predicate": "interrupt populated v31-to-v33 migration, restore and verify preserved usable data",
        "source": "END-STATE.md; R-37", "evidence_kind": "unsupported",
    },
    *(
        {
            "id": f"{scenario}:{row['job_id']}", "phase": "rollback-uninstall-stop", "job_id": row["job_id"],
            "os": row["os"], "channel": row["mode"],
            "owner": "M-QUAL" if row["mode"] == "direct" else "M-PIN",
            "route": f"journey_failure_modes.py:scenario_errors({failure_name})",
            "route_state": "unimplemented", "dependency": "hosted native-service M4-6 adapter and receipt",
            "predicate": predicate, "source": source, "evidence_kind": "m4-6",
        } for scenario, failure_name, predicate, source in (
            ("rollback", "failed_candidate_rollback", "controlled candidate failure; authentic real-R0 executable identity, healthy restart and semantic data preserved", "journey_failure_modes.py:_rollback_errors"),
            ("keep_data_uninstall", "keep_data_uninstall", "remove program and service; retain workspace/CAS/semantic data and sentinel", "journey_failure_modes.py:_uninstall_errors"),
            ("stop_safety", "stop_safety", "refused and unknown stop do not update, retry, activate or change data or R0 identity", "journey_failure_modes.py:_stop_safety_errors"),
        ) for row in M4_6_MATRIX
    ),
    *(
        {
            "id": row["scenario_id"], "phase": "r2", "job_id": row["job_id"], "os": row["os"],
            "channel": row["channel"], "mode": row["mode"],
            "owner": "M-QUAL" if row["mode"] == "direct" else "M-PIN",
            "route": "ci_journey.py --shared-executor m4-5", "route_state": "execution-capable",
            "dependency": "current-tuple M4-5 admission and authenticated hosted receipt",
            "predicate": "visible installed R2 stand-in identity, channel trigger, preserved data, service, task and cleanup",
            "source": "journey_release2.py:receipt_errors", "evidence_kind": "m4-5",
        } for row in M4_5_MATRIX
    ),
    {
        "id": "r0-to-r2-skip", "phase": "r2", "owner": "M-QUAL", "route": None,
        "route_state": "unimplemented", "dependency": "R-16 worker bridge execution and authentic R0-to-R2 receipt",
        "predicate": "actual worker executes authentic R0-to-R2 skip with bridge/job/journal linkage and preserved data",
        "source": "END-STATE.md; R-16", "evidence_kind": "unsupported",
    },
    {
        "id": "linux-unsaved-draft-browser", "phase": "linux-app-guard", "os": "linux", "channel": "button",
        "job_id": "direct-linux", "owner": "M-APP/M-QUAL", "route": None,
        "route_state": "unimplemented", "dependency": "current-tuple installed browser/Runtime receipt",
        "predicate": "failed save has zero POST/jobs; guarded save has one POST and native job; include chat drafts and stale marker",
        "source": "END-STATE.md; journey_release2.py:_button_errors", "evidence_kind": "unsupported",
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
    """Return a copy so callers cannot mutate the checked-in matrix.

    Deferred R1.1 Pinokio jobs stay admitted for the driver, but are not part
    of RELEASE1_MATRIX or its aggregate.
    """
    for row in (*_ADMITTED_JOBS, *RELEASE1_MATRIX, *RELEASE1_1_PINOKIO_MATRIX):
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

# RunPod is an executor for the same installed-product journey receipts.  Each
# scenario names its own observable assertions and command exits.
GPU_MATRIX: Final = tuple(
    {"scenario_id": name, "runner": "runpod", "shell": "sh", "os": "linux",
     "mode": mode, "checks": checks, "inner_exits": exits}
    for name, mode, checks, exits in (
        ("R1", "pod", ("install",), ("install",)),
        ("R2", "pod", ("managed_install", "low_disk_refusal"), ("install", "low_disk_refusal")),
        ("R3", "pod", ("readiness",), ("readiness",)),
        ("R4", "pod", ("image_512", "image_1024", "task_record"), ("image_512", "image_1024")),
        ("R5", "pod", ("workflow_artifact", "task_record"), ("workflow",)),
        ("R6", "pod", ("missing_model", "repair", "rerun"), ("repair", "rerun")),
        ("R7", "pod", ("cancelled", "pod_released", "no_orphans"), ("follow", "cancel", "release")),
        ("R8", "pod", ("h3",), ("generate",)),
        ("R9", "pod", ("smoke_2",), ("smoke_2",)),
        ("R10", "pod", ("product_target", "sole_claimant", "foreign_pods_preserved"), ("generate",)),
        ("PRIMARY", "attached", ("task_settled", "local_runtime", "file"), ("remote-target", "readiness", "generation")),
        ("SECONDARY", "pod", ("task_settled", "file"), ("generation",)),
    )
)
GPU_SCENARIO_IDS: Final = tuple(row["scenario_id"] for row in GPU_MATRIX)


def emit_gpu_receipt(evidence_dir, scenario_id, *, variant=None):
    """Translate recorded evidence only; missing observations remain null.

    A future runner writes journey-capture.json using the documented receipt
    fields. Historical row receipts are read without inferring release identity.
    For rows-final.json the variant must identify one replay explicitly.
    """
    import hashlib
    import json
    from pathlib import Path

    contract = next((r for r in GPU_MATRIX if r["scenario_id"] == scenario_id), None)
    if contract is None:
        raise ValueError(f"Unknown GPU scenario: {scenario_id}")
    root = Path(evidence_dir)
    def load(path):
        def unique(pairs):
            obj = {}
            for key, value in pairs:
                if key in obj:
                    raise ValueError(f"duplicate JSON field {key} in {path}")
                obj[key] = value
            return obj
        return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique)

    capture = root / "journey-capture.json"
    row = load(capture) if capture.is_file() else {}
    if not isinstance(row, dict):
        raise ValueError("journey-capture.json must be an object")
    if row and row.get("scenario") != scenario_id:
        raise ValueError("capture scenario differs from requested scenario")
    result = {key: row.get(key) for key in (
        "schema_version", "receipt_kind", "scenario", "mode", "status", "runner_image",
        "runner_provenance", "os_build", "shell", "source_override_test_mode", "final_cleanup",
        "manifest_sha256", "candidate_checkouts", "sources", "installed_tuple", "import_origins",
        "checks", "inner_exits")}
    # Scenario is the requested inventory key, not an observation of execution.
    result["scenario"] = scenario_id
    primary = root / "primary-receipt.json"
    replay = root / "rows-final.json"
    legacy = None
    if not row and primary.is_file() and scenario_id == "PRIMARY":
        legacy = load(primary)
        timings = legacy.get("timings", [])
        labels = [item.get("label") for item in timings]
        if len(set(labels)) != len(labels):
            raise ValueError("duplicate PRIMARY timing label")
        result["inner_exits"] = {t.get("label"): t.get("exit") for t in timings}
        checks = {}
        runtime = root / "verified-runtime-row.json"
        if runtime.is_file():
            task = load(runtime)
            if task.get("id") == legacy.get("task_id") and task.get("status") == "completed":
                checks["task_settled"] = "pass"
        opened = root / "verified-artifact-open.json"
        if opened.is_file():
            artifact = load(opened)
            data = artifact.get("data") or {}
            path = Path(data.get("local_path", ""))
            if (artifact.get("ok") is True and path.is_file()
                    and path.resolve().is_relative_to(root.resolve())
                    and hashlib.sha256(path.read_bytes()).hexdigest() == legacy.get("sha256")
                    and data.get("content_identity") == "sha256:" + legacy.get("sha256", "")):
                checks["file"] = "pass"
        result["checks"] = checks
    elif not row and replay.is_file() and scenario_id in {"R1", "R2", "R3"}:
        rows = load(replay)
        if variant is None or variant not in rows:
            raise ValueError("rows-final.json requires an explicit replay variant")
        matches = [r for r in rows[variant] if r.get("row") == scenario_id]
        if len(matches) != 1:
            raise ValueError("replay row is missing or duplicated")
        item = matches[0]
        # The index's receipt paths are relative to the task root (parent of out).
        path = (root.parent / item["receipt"]).resolve()
        if not path.is_relative_to(root.resolve()) or hashlib.sha256(path.read_bytes()).hexdigest() != item.get("receipt_sha256"):
            raise ValueError("replay receipt path or digest mismatch")
        legacy = load(path)
        if legacy.get("row") != scenario_id:
            raise ValueError("replay receipt row mismatch")
    elif not row and scenario_id in {"R4", "R6"}:
        files = sorted(root.glob("*receipt.json"))
        if variant is not None:
            files = [path for path in files if path.name == variant]
        else:
            files = [path for path in files if path.name.startswith(scenario_id)]
        if len(files) == 1:
            legacy = load(files[0])
            if not str(legacy.get("row", "")).startswith(scenario_id):
                raise ValueError("historical receipt row mismatch")
        elif len(files) > 1:
            raise ValueError("multiple historical row receipts; select a row evidence directory")
    if legacy is not None and not row:
        verdict = legacy.get("status", legacy.get("VERDICT"))
        result["status"] = {"PASS": "pass", "FAIL": "fail"}.get(verdict)
        result["manifest_sha256"] = legacy.get("manifest_sha256")
        if scenario_id != "PRIMARY" and "exit_code" in legacy:
            result["inner_exits"] = {"row": legacy["exit_code"]}
    return result
