"""The Windows release12-then-update row: its matrix row, argument admission, evidence expectations and steps."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap
from types import SimpleNamespace

import pytest

from scripts import ci_journey as journey
from scripts import journey_matrix
from scripts import journey_version_jumps as jumps
from scripts import journey_windows_r12_update as wr
from tests import test_journey_evidence as rows

gate = rows.gate
ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = (ROOT / ".github" / "workflows" / "journey.yml").read_text(encoding="utf-8")
JOB = "direct-windows-r12-update"
LEG = "release12-then-update"
TARGET = 13

# ---------------------------------------------------------------------------------------------
# The matrix row
# ---------------------------------------------------------------------------------------------


def test_the_windows_row_is_the_one_windows_release12_row_in_the_full_matrix():
    row = journey_matrix.validate_job(JOB, "direct")
    assert row == {"job_id": JOB, "mode": "direct", "runner": "windows-2022", "shell": "powershell.exe",
                   "os": "windows", "leg": LEG}
    assert journey_matrix.RELEASE12_WINDOWS_JOB_ID == JOB
    assert JOB in journey_matrix.RELEASE12_UPDATE_JOB_IDS
    assert JOB in journey_matrix.RELEASE1_JOB_IDS
    # An automatic push or schedule still runs only the five rows that existed before.
    assert JOB not in {item["job_id"] for item in journey_matrix.RELEASE1_AUTOMATIC_MATRIX}
    assert len(journey_matrix.RELEASE1_MATRIX) == 14
    assert len(set(journey_matrix.RELEASE1_JOB_IDS)) == 14
    windows_rows = [item for item in journey_matrix.RELEASE1_MATRIX if item["os"] == "windows"]
    assert {item["job_id"] for item in windows_rows} == {"direct-windows", JOB}
    # The existing Windows row keeps its own leg.
    assert journey_matrix.validate_job("direct-windows", "direct")["leg"] == "fresh-install"


def run_config(tmp_path, **overrides):
    """Run the workflow's checked-in config program and return (process, outputs)."""
    code = WORKFLOW.split("      - id: config\n", 1)[1].split("          python3 - <<'PY'\n", 1)[1].split("\n          PY\n", 1)[0]
    (tmp_path / "driver").symlink_to(ROOT, target_is_directory=True)
    output = tmp_path / "output"
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "GITHUB_OUTPUT": str(output),
           "CANDIDATE": json.dumps(dict.fromkeys(("app", "agent", "runtime", "pinokio"), "a" * 40)),
           "DRIVER_AGENT_REF": "b" * 40, "WORKFLOW_REPOSITORY": "banodoco/astrid-agent",
           "OLD_LAUNCHER_REF": "bbba620e3d89eeff8021417f365e6ea584203105",
           "STOPPED_PATH_AGENT_REF": "a" * 40, "M4_4A_EXECUTOR_CONFIGS": '{"linux":{},"macos":{},"windows":{}}',
           "DIAGNOSTIC_KEEP_GOING": "false", "DIAGNOSTIC_LEG": "release9-then-install", "DIRECT_ONLY": "false",
           "JOURNEY_ROW": "direct", "M4_4A_BACKEND_ONLY": "false", "M4_5_ENABLED": "false",
           "M4_5_ONLY": "false", "M4_5_BOUNDARY": "", "M4_5_BOUNDARY_ARGS": "[]",
           "M4_4B_ENABLED": "false", "M4_6_ENABLED": "false", "M4_6_CANDIDATE": "", **overrides}
    result = subprocess.run([sys.executable, "-c", textwrap.dedent(code)], cwd=tmp_path, env=env,
                            capture_output=True, text=True)
    outputs = dict(line.split("=", 1) for line in output.read_text().splitlines()) if output.exists() else {}
    return result, outputs


def test_the_row_is_selectable_by_name_and_included_in_row_direct(tmp_path):
    for name in ("one", "all", "keepgoing", "automatic"):
        (tmp_path / name).mkdir()
    result, outputs = run_config(tmp_path / "one", JOURNEY_ROW=JOB)
    assert result.returncode == 0, result.stderr
    assert json.loads(outputs["matrix"])["include"] == [
        {"job_id": JOB, "mode": "direct", "runner": "windows-2022", "shell": "powershell.exe",
         "os": "windows", "leg": LEG}]
    assert outputs["release1_scope"] == "nine"
    result, outputs = run_config(tmp_path / "all", JOURNEY_ROW="direct")
    assert result.returncode == 0, result.stderr
    included = json.loads(outputs["matrix"])["include"]
    assert JOB in {item["job_id"] for item in included} and len(included) == 14
    # Neither a keep-going diagnostic nor an automatic run admits it.
    result, _ = run_config(tmp_path / "keepgoing", JOURNEY_ROW=JOB, DIAGNOSTIC_KEEP_GOING="true")
    assert result.returncode != 0 and "do not support non-proof keep-going" in result.stderr
    result, outputs = run_config(tmp_path / "automatic", JOURNEY_ROW="")
    assert JOB not in {item["job_id"] for item in json.loads(outputs["matrix"])["include"]}


def test_the_workflow_wires_the_windows_row_without_touching_the_other_windows_row():
    options = WORKFLOW.split("        type: choice\n        options:\n", 1)[1].split("\n\nconcurrency:", 1)[0]
    assert f"          - {JOB}\n" in options and "          - direct-windows\n" in options
    assert "timeout-minutes: ${{ startsWith(matrix.job_id, 'direct-windows') && 300 || 150 }}" in WORKFLOW
    assert WORKFLOW.count("if: startsWith(matrix.job_id, 'direct-windows')") == 1
    assert "if: always() && startsWith(matrix.job_id, 'direct-windows')" in WORKFLOW
    # The Windows refusal leg, which only the fresh Windows row runs, and the hosted intercept stay as they were.
    assert "$runWindowsRefusal = '${{ matrix.job_id }}' -eq 'direct-windows' -and (" in WORKFLOW
    assert "matrix.job_id == 'direct-windows' || matrix.job_id == 'direct-linux-fresh'" in WORKFLOW
    # The leg's own release-12 checkout and admission flags are selected by leg, so the Windows row gets them.
    assert "if: matrix.leg == 'release12-then-update'" in WORKFLOW
    assert "'--release12-app-checkout', \"$pwd/release12-app\"," in WORKFLOW
    # Both Windows rows keep their prerequisite diagnostics in the one folder the diagnostic steps write.
    assert "journey-manager-direct-windows" in WORKFLOW
    assert "startsWith(matrix.job_id, 'direct-windows') && 'direct-windows' || matrix.job_id" in WORKFLOW


def test_the_two_new_evidence_names_are_in_both_allowlists_and_nothing_else_was_added():
    block = WORKFLOW.split("$allowedNames = @(", 1)[1].split("\n          )", 1)[0]
    staged = set(__import__("re").findall(r"'([^']+)'", block))
    for name in ("windows-r12-doctor.json", "windows-r12-dry-run.json"):
        assert name in staged and name in journey.JOURNEY_EVIDENCE_NAMES
    # The scratch outputs the commands write are not staged.
    for name in (wr.DOCTOR_OUTPUT, wr.DRY_RUN_OUTPUT):
        assert name not in staged and name not in journey.JOURNEY_EVIDENCE_NAMES
    assert wr.DOCTOR_EVIDENCE == "windows-r12-doctor.json" and wr.DRY_RUN_EVIDENCE == "windows-r12-dry-run.json"
    assert journey.JOURNEY_EVIDENCE_NAMES - staged == set()
    # The row's install and update bootstrap captures reuse existing names.
    for name in ("install-bootstrap.log", "install-bootstrap-process.json", "update-bootstrap.log",
                 "update-bootstrap-process.json", "verify-observe.json", "verify-update.json",
                 "verify-install.json", "start.log", "journey-result.json"):
        assert name in staged and name in journey.JOURNEY_EVIDENCE_NAMES


# ---------------------------------------------------------------------------------------------
# Argument validation: the Windows release12 leg is admitted for this job on Windows only
# ---------------------------------------------------------------------------------------------


def argv_for(tmp_path, job_id, *, shell, extra=()):
    argv = ["--leg", LEG, "--mode", "direct", "--job-id", job_id,
            "--app-checkout", str(tmp_path), "--run-root", str(tmp_path / "run"),
            "--root", str(tmp_path / "run" / "program"), "--data-root", str(tmp_path / "run" / "data"),
            "--candidate", str(tmp_path / "candidate.json"),
            "--candidate-agent-checkout", str(tmp_path / "agent"),
            "--candidate-runtime-checkout", str(tmp_path / "runtime"),
            "--pinokio-checkout", str(tmp_path / "pinokio"), "--shell", shell,
            "--release12-app-checkout", str(tmp_path / "release12"), "--allow-release12-to-candidate"]
    return [*argv, *extra]


def test_the_driver_hands_the_windows_release12_leg_to_execution_on_its_row(tmp_path, monkeypatch):
    recorded = []
    monkeypatch.setattr(journey, "execute", lambda args: recorded.append((args.leg, args.job_id)) or {"status": "pass"})
    monkeypatch.setattr(journey.sys, "platform", "win32")
    assert journey.main(argv_for(tmp_path, JOB, shell="powershell.exe")) == 0
    assert recorded == [(LEG, JOB)]


@pytest.mark.parametrize(("platform", "job_id", "shell"), [
    ("win32", "direct-linux-r12-update", "powershell.exe"),
    ("win32", "direct-macos-r12-update", "powershell.exe"),
    ("win32", "direct-windows", "powershell.exe"),
    ("linux", JOB, "sh"),
    ("darwin", JOB, "sh"),
])
def test_the_release12_leg_is_refused_on_the_wrong_job_or_operating_system(tmp_path, monkeypatch, platform,
                                                                          job_id, shell, capsys):
    monkeypatch.setattr(journey, "execute", lambda *unused: pytest.fail("must reject before execution"))
    monkeypatch.setattr(journey.sys, "platform", platform)
    with pytest.raises(SystemExit) as failure:
        journey.main(argv_for(tmp_path, job_id, shell=shell))
    assert failure.value.code == 2
    assert "release12-then-update on Windows is admitted only for the direct-windows-r12-update job" in capsys.readouterr().err


@pytest.mark.parametrize("platform, job_id, shell", [
    ("linux", "direct-linux-r12-update", "sh"), ("darwin", "direct-macos-r12-update", "sh"),
])
def test_the_linux_and_macos_release12_rows_are_still_admitted(tmp_path, monkeypatch, platform, job_id, shell):
    recorded = []
    monkeypatch.setattr(journey, "execute", lambda args: recorded.append(args.job_id) or {"status": "pass"})
    monkeypatch.setattr(journey.sys, "platform", platform)
    assert journey.main(argv_for(tmp_path, job_id, shell=shell)) == 0
    assert recorded == [job_id]


@pytest.mark.parametrize("drop", ["--release12-app-checkout", "--allow-release12-to-candidate"])
def test_the_windows_row_needs_its_pinned_release12_admission(tmp_path, monkeypatch, drop):
    monkeypatch.setattr(journey, "execute", lambda *unused: pytest.fail("must reject before execution"))
    monkeypatch.setattr(journey.sys, "platform", "win32")
    argv = argv_for(tmp_path, JOB, shell="powershell.exe")
    index = argv.index(drop)
    del argv[index:index + (1 if drop.startswith("--allow") else 2)]
    with pytest.raises(SystemExit) as failure:
        journey.main(argv)
    assert failure.value.code == 2


@pytest.mark.parametrize("extra", [
    ["--diagnostic-keep-going"], ["--retain-for-next-leg"],
    ["--release0-fixture", "fixture"], ["--release9-app-checkout", "r9"],
])
def test_the_windows_row_admits_no_other_release_source_or_diagnostic(tmp_path, monkeypatch, extra):
    monkeypatch.setattr(journey, "execute", lambda *unused: pytest.fail("must reject before execution"))
    monkeypatch.setattr(journey.sys, "platform", "win32")
    with pytest.raises(SystemExit) as failure:
        journey.main(argv_for(tmp_path, JOB, shell="powershell.exe", extra=extra))
    assert failure.value.code == 2


def test_the_windows_release12_row_requires_the_windows_shell(tmp_path, monkeypatch):
    monkeypatch.setattr(journey, "execute", lambda *unused: pytest.fail("must reject before execution"))
    monkeypatch.setattr(journey.sys, "platform", "win32")
    with pytest.raises(SystemExit):
        journey.main(argv_for(tmp_path, JOB, shell="sh"))


def test_execute_owned_dispatches_the_windows_row_and_nothing_else_on_windows(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(wr, "execute", lambda *args: seen.append(args[0].job_id) or "dispatched")
    monkeypatch.setattr(jumps.sys, "platform", "win32")
    args = argparse.Namespace(leg=LEG, job_id=JOB)
    assert jumps.execute_owned(args, tmp_path / "p", tmp_path / "d", tmp_path / "e", SimpleNamespace(pid=1), "b") == "dispatched"
    assert seen == [JOB]
    with pytest.raises(RuntimeError, match="only admitted for direct-windows-r12-update"):
        jumps.execute_owned(argparse.Namespace(leg=LEG, job_id="direct-linux-r12-update"), tmp_path / "p",
                            tmp_path / "d", tmp_path / "e", SimpleNamespace(pid=1), "b")
    # The older Windows refusals are unchanged.
    for leg in ("release11-then-update", "release9-then-install", "r0-handoff-then-update"):
        with pytest.raises(RuntimeError, match="narrowed refusal"):
            jumps.execute_owned(argparse.Namespace(leg=leg, job_id=JOB), tmp_path / "p", tmp_path / "d",
                                tmp_path / "e", SimpleNamespace(pid=1), "b")


def test_execute_owned_keeps_the_posix_release12_leg_on_its_own_function(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(jumps, "execute_release12_then_update", lambda *args: seen.append(args) or "posix")
    monkeypatch.setattr(wr, "execute", lambda *args: pytest.fail("the Windows leg must not run on POSIX"))
    for platform in ("linux", "darwin"):
        monkeypatch.setattr(jumps.sys, "platform", platform)
        assert jumps.execute_owned(argparse.Namespace(leg=LEG, job_id="direct-linux-r12-update"), tmp_path / "p",
                                   tmp_path / "d", tmp_path / "e", SimpleNamespace(pid=1), "b") == "posix"
    assert len(seen) == 2


# ---------------------------------------------------------------------------------------------
# Evidence expectations
# ---------------------------------------------------------------------------------------------


def test_a_complete_run_with_the_windows_row_passes_the_aggregate(tmp_path):
    candidate = rows.release1_receipts(tmp_path)
    assert {path.parent.name for path in tmp_path.rglob("journey-result.json")} == set(gate.RELEASE1_JOB_IDS)
    assert len(gate.RELEASE1_MATRIX) == 14
    assert gate.evaluate_release1_matrix(candidate, tmp_path) == []
    (tmp_path / JOB / "journey-result.json").unlink()
    assert gate.evaluate_release1_matrix(candidate, tmp_path) == [f"{JOB}: missing receipt"]


def mutate(tmp_path, change, job_id=JOB):
    path = tmp_path / job_id / "journey-result.json"
    row = json.loads(path.read_text(encoding="utf-8"))
    change(row)
    path.write_text(json.dumps(row), encoding="utf-8")


BREAKERS = [
    (lambda row: row["release12_source"].update(app_commit="e" * 40),
     "pinned release-12 source identity is absent or wrong"),
    (lambda row: row["release_transition"].update({"from": 11}), "installed release is not 12"),
    (lambda row: row["health_transition"].update(candidate_live_release=11), "installed release is not 12"),
    (lambda row: row["update"].update(exit_code=1), "install --update receipt does not say updated"),
    (lambda row: row["update"].update(command="install"), "install --update receipt does not say updated"),
    (lambda row: row["update"]["receipt"].update(target=11), "install --update receipt does not say updated"),
    (lambda row: row.update(live_runtimes=[]), "exactly one live Runtime is not recorded"),
    (lambda row: row.update(live_runtimes=[{"data_root": "/run/data"}] * 2), "exactly one live Runtime is not recorded"),
    (lambda row: row["doctor"].update(ok=False), "astrid doctor did not exit 0 and report ok"),
    (lambda row: row["doctor"].update(exit_code=1), "astrid doctor did not exit 0 and report ok"),
    (lambda row: row.pop("doctor"), "astrid doctor did not exit 0 and report ok"),
    (lambda row: row["dry_run"].update(dry_run=False), "the dry-run generation did not run as a clean dry run"),
    (lambda row: row["dry_run"].update(capability_id="other.capability"), "dry-run generation did not run"),
    (lambda row: row.pop("dry_run"), "the dry-run generation did not run as a clean dry run"),
    (lambda row: row["data_marker"].update(survived=False), "seeded project and workspace marker did not survive"),
    (lambda row: row.pop("data_marker"), "seeded project and workspace marker did not survive"),
    (lambda row: row["service_manager"].update(manager="systemd-user"), "Task Scheduler service ownership"),
    (lambda row: row["steps"][2].update(status="fail"), "a journey step did not pass"),
    (lambda row: row.update(steps=[]), "a journey step did not pass"),
    (lambda row: row.update(receipt_kind="release1-installed-lifecycle"), "not a Release-1 lifecycle receipt"),
    (lambda row: row.update(scenario="fresh-install"), "scenario does not match the required matrix leg"),
    (lambda row: row.update(runner_image="ubuntu-24.04"), "runner does not match the required matrix"),
    (lambda row: row.update(shell="sh"), "shell does not match the required matrix"),
    (lambda row: row.update(final_cleanup="fail"), "final cleanup is not pass"),
    (lambda row: row["installed_tuple"].update(agent="f" * 40), "agent installed/source identity differs"),
    (lambda row: row["installed_tuple"].update(runtime="f" * 40), "runtime installed/source identity differs"),
    (lambda row: row.update(import_origins=None), "installed import origins are absent"),
]


@pytest.mark.parametrize(("change", "expected"), BREAKERS)
def test_the_windows_row_is_rejected_when_one_of_its_assertions_is_missing(tmp_path, change, expected):
    candidate = rows.release1_receipts(tmp_path)
    mutate(tmp_path, change)
    errors = gate.evaluate_release1_matrix(candidate, tmp_path)
    assert any(error.startswith(f"{JOB}: ") and expected in error for error in errors), errors
    assert all(error.startswith(f"{JOB}: ") for error in errors), errors


@pytest.mark.parametrize("check", wr.CHECKS)
def test_the_windows_row_is_rejected_when_a_named_check_did_not_pass(tmp_path, check):
    assert tuple(wr.CHECKS) == tuple(gate.RELEASE12_WINDOWS_CHECKS)
    candidate = rows.release1_receipts(tmp_path)
    mutate(tmp_path, lambda row: row["checks"].update({check: "not-run"}))
    assert f"{JOB}: {check} assertion did not pass" in gate.evaluate_release1_matrix(candidate, tmp_path)


def test_the_installed_release_is_checked_against_the_candidate_release(tmp_path):
    candidate = rows.release1_receipts(tmp_path)
    mutate(tmp_path, lambda row: (row["release_transition"].update(to=13),
                                  row["health_transition"].update(candidate_live_release=13)))
    errors = gate.evaluate_release1_matrix(candidate, tmp_path)
    assert f"{JOB}: installed release 13 is not the candidate release 12" in errors
    assert gate.evaluate_release1_matrix({**candidate, "release": 13}, tmp_path) != []
    mutate(tmp_path, lambda row: row["update"]["receipt"].update(still=13, target=13))
    assert [error for error in gate.evaluate_release1_matrix({**candidate, "release": 13}, tmp_path)
            if error.startswith(f"{JOB}: ") and "release 12" not in error] == []


def test_the_windows_run_receipt_is_read_when_present_but_not_required(tmp_path):
    candidate = rows.release1_receipts(tmp_path)
    mutate(tmp_path, lambda row: row["update"].update(receipt=None))
    assert gate.evaluate_release1_matrix(candidate, tmp_path) == []
    # The POSIX rows still require the candidate's own run receipt.
    mutate(tmp_path, lambda row: row["update"].update(receipt=None), "direct-linux-r12-update")
    assert any("direct-linux-r12-update: candidate install --update receipt" in error
               for error in gate.evaluate_release1_matrix(candidate, tmp_path))


def test_the_posix_release12_rows_do_not_need_the_windows_assertions(tmp_path):
    candidate = rows.release1_receipts(tmp_path)
    for job_id in ("direct-linux-r12-update", "direct-macos-r12-update"):
        path = tmp_path / job_id / "journey-result.json"
        row = json.loads(path.read_text())
        assert "doctor" not in row and "dry_run" not in row and "steps" not in row
    assert gate.evaluate_release1_matrix(candidate, tmp_path) == []
    # And the Windows contract is not satisfied by a POSIX-shaped receipt.
    posix = json.loads((tmp_path / "direct-linux-r12-update" / "journey-result.json").read_text())
    shaped = dict(posix, job_id=JOB, runner_image="windows-2022", shell="powershell.exe",
                  runner_provenance={"RUNNER_OS": "Windows", "ImageOS": "win22", "ImageVersion": "test"},
                  os_build="Windows-test")
    (tmp_path / JOB / "journey-result.json").write_text(json.dumps(shaped), encoding="utf-8")
    errors = gate.evaluate_release1_matrix(candidate, tmp_path)
    assert any("astrid doctor" in error for error in errors) and any("dry-run" in error for error in errors)


def test_release12_row_errors_keep_their_three_argument_form_for_posix(tmp_path):
    candidate = rows.release1_receipts(tmp_path)
    row = json.loads((tmp_path / "direct-linux-r12-update" / "journey-result.json").read_text())
    assert gate._release12_row_errors("direct-linux-r12-update", row, candidate["release"]) == []


# ---------------------------------------------------------------------------------------------
# The leg itself, with the install and the product replaced by recorded fakes
# ---------------------------------------------------------------------------------------------

CANDIDATE = {key: letter * 40 for key, letter in zip(("app", "agent", "runtime", "pinokio"), "abcd")}
MANIFEST = {"release": TARGET, "agent": {"sha": CANDIDATE["agent"]},
            "runtime": {"sha": CANDIDATE["runtime"], "schema_digest": "sha256:" + "9" * 64}}
DRY_RUN_REPORT = {
    "capability_id": wr.DRY_RUN_CAPABILITY, "ok": True, "error": None, "outputs": {}, "run_id": None,
    "run_root": None, "kernel_run_id": None, "kernel_task_id": None,
    "raw_result": {"command": ["python", "-m", "generate_speech", "--text", "t"], "dry_run": True,
                   "executor_id": wr.DRY_RUN_CAPABILITY, "ok": True, "returncode": None, "skipped": False,
                   "outputs": {}, "error": None,
                   "payload": {"preview": {"kind": "manifest-ledger", "capability_id": wr.DRY_RUN_CAPABILITY,
                                           "inputs": dict(wr.DRY_RUN_INPUTS), "outputs": {}}}},
}
DOCTOR_REPORT = {"ok": True, "healthy": True, "state": "ready", "problem_code": None}


class Scenario:
    """One fake Windows machine: the install, the Runtime and the installed commands, recorded as they are used."""

    def __init__(self, tmp_path, monkeypatch, **fail):
        self.fail = fail
        self.phase = "release12"
        self.calls = []
        run = tmp_path / "run"
        self.root, self.data, self.evidence = run / "program", run / "data", run / "evidence" / LEG
        for path in (self.root, self.data / "runtime", tmp_path / "app"):
            path.mkdir(parents=True, exist_ok=True)
        (self.data / "runtime" / "discovery.json").write_text(
            json.dumps({"pid": 222, "runtime_instance_id": "new"}), encoding="utf-8")
        (tmp_path / "candidate.json").write_text(json.dumps(
            {**CANDIDATE, "manifest_sha256": "f" * 64, "artifacts": {"public/install.ps1": "1" * 64}}), encoding="utf-8")
        self.args = SimpleNamespace(
            job_id=JOB, mode="direct", leg=LEG, shell="powershell.exe", app_checkout=tmp_path / "app",
            candidate=tmp_path / "candidate.json", candidate_agent_checkout=tmp_path / "agent", agent_checkout=None,
            candidate_checkouts={name: {"sha": sha} for name, sha in CANDIDATE.items()},
            service_manager_prerequisite={"required": True, "outcome": "pass", "version": "5.1"})
        self.sentinel = SimpleNamespace(pid=77)
        real = journey
        scenario = self

        def hook(name, *args):
            scenario.calls.append(name)
            if name in scenario.fail:
                raise scenario.fail[name]

        def installation(root, data):
            scenario.calls.append("installation")
            release = TARGET if scenario.phase == "candidate" else 12
            path = root / "versions" / scenario.phase / "version.json"
            return path, data, {"root": str(path.parent), "python": "python.exe", "env": {"A": "1"},
                                "release": release, "identity": {"release": release, "managed_by": None}}

        def healthy(data):
            hook("healthy")
            if scenario.phase == "candidate":
                return {"pid": 222, "runtime_instance_id": "new", "identity": {"release": TARGET}}
            return {"pid": 111, "runtime_instance_id": "old", "identity": {"release": 12}}

        def process_birth(pid):
            if pid == 111:
                return "birth-old" if scenario.phase == "release12" else (
                    "birth-old" if "old_process_survives" in scenario.fail else None)
            return "birth-new"

        def bootstrap(app, manifest_path, shell, mode, evidence, root, data, agent=None):
            hook("bootstrap_" + mode)
            scenario.calls.append(("bootstrap", mode, str(app), shell))
            code = scenario.fail.get("update_exit", 0) if mode == "update" else 0
            if mode == "update":
                scenario.phase = "candidate"
                (evidence / "update-bootstrap-process.json").write_text(json.dumps({"exit_code": code}), encoding="utf-8")
                (evidence / "update-bootstrap.log").write_text("=== stdout ===\nUpdating Astrid\n", encoding="utf-8")
                if code:
                    raise RuntimeError(f"update-bootstrap.log: exit {code}")
                if "installs_nothing" in scenario.fail:
                    scenario.phase = "release12"

        def verify(mode, version_path, data, evidence, before=None):
            hook("verify_" + mode)
            document = {"passed": True, "details": {"origin.imports": {"runtime": "/owned/runtime"}}}
            real.write_json(evidence / f"verify-{mode}.json", document)
            return evidence / f"verify-{mode}.json"

        def installed_cli(version, argv, evidence, name, **kwargs):
            hook("installed_cli")
            scenario.calls.append(("cli", tuple(argv)))
            return json.dumps({"ok": True})

        self.journey = SimpleNamespace(
            write_json=real.write_json, read_json=real.read_json,
            runner_identity=lambda: {"runner_image": "windows-2022",
                                     "runner_provenance": {"RUNNER_OS": "Windows", "ImageOS": "win22",
                                                           "ImageVersion": "test"}},
            selected_manifest=lambda app, **kw: (hook("selected_manifest"),
                                                 (app / "public" / "install-manifest.json", MANIFEST))[1],
            git_source=lambda app: {"sha": CANDIDATE["app"], "repo": str(app)},
            sentinel_observation=lambda process, birth: {"pid": process.pid, "alive": True},
            windows=SimpleNamespace(EVIDENCE=None, powershell=lambda code, name, **kw: scenario.calls.append(("powershell", name, code))),
            wait_ready=lambda data, timeout=0: (hook("wait_ready"), scenario.calls.append(("wait_ready", timeout))),
            installation=installation,
            selected_tuple=lambda manifest, version: {
                "agent": (manifest.get("agent") or {"sha": "1" * 40})["sha"],
                "runtime": (manifest.get("runtime") or {"sha": "2" * 40})["sha"], "schema_digest": None, "tools": {}},
            assert_direct_service_ownership=lambda root, data, **kw: {"manager": "task-scheduler", "foreground_fallback": False},
            healthy=healthy, process_birth=process_birth, bootstrap=bootstrap, verify=verify,
            installed_cli=installed_cli, workspace_root=lambda data: data,
            assert_content=lambda *a: hook("assert_content"),
            hosted_windows_runner=lambda: False,
        )
        self.jumps = SimpleNamespace(
            RELEASE12_RELEASE=12,
            release12_admission=scenario.release12_admission,
            bootstrap_release12=lambda source, manifest, shell, evidence, root, data, agent=None: scenario.journey.bootstrap(
                source, manifest, shell, "install", evidence, root, data, agent=agent),
            release9_consent_agent=lambda args: args.candidate_agent_checkout,
            installed_release=lambda version: version["release"],
            selected_candidate=lambda manifest, version, before: {"agent": manifest["agent"]["sha"],
                                                                  "runtime": manifest["runtime"]["sha"],
                                                                  "schema_digest": None, "tools": {}},
            selected_data_root=lambda root, data: data,
        )

        def run_captured(argv, log, *, env, timeout):
            if "doctor" in argv:
                hook("doctor")
                code, text = scenario.fail.get("doctor_exit", 0), json.dumps(DOCTOR_REPORT, indent=2)
            else:
                hook("dry_run")
                report = scenario.fail.get("dry_run_report", DRY_RUN_REPORT)
                code = 0
                text = '{"diagnostic": "noise"}\n' + json.dumps(report)
            log.write_text(text, encoding="utf-8")
            scenario.calls.append(("run", tuple(str(part) for part in argv[:5])))
            return code, text

        monkeypatch.setattr(wr, "journey", self.journey)
        monkeypatch.setattr(wr, "jumps", self.jumps)
        monkeypatch.setattr(wr, "evidence_gate", SimpleNamespace(validate_tuple=lambda candidate, app, path: MANIFEST))
        monkeypatch.setattr(wr, "run_captured", run_captured)
        monkeypatch.setattr(wr, "windows_run_receipt", lambda run_root: scenario.fail.get(
            "receipt", {"kind": "updated", "exit_code": 0, "still": TARGET, "target": TARGET}))

    def release12_admission(self, args, output):
        self.calls.append("release12_admission")
        if "release12_admission" in self.fail:
            raise self.fail["release12_admission"]
        output.mkdir(parents=True, exist_ok=True)
        (output / "install-manifest.json").write_text(json.dumps({"release": 12}), encoding="utf-8")
        return output, output / "install-manifest.json", {"app_commit": jumps.RELEASE12_APP_COMMIT,
                                                          "identity": jumps.RELEASE12_IDENTITY}

    def run(self):
        return wr.execute(self.args, self.root, self.data, self.evidence, self.sentinel, "birth")

    def receipt(self):
        return json.loads((self.evidence / "journey-result.json").read_text(encoding="utf-8"))


def test_the_leg_runs_every_step_in_order_and_its_receipt_passes_the_checker(tmp_path, monkeypatch):
    scenario = Scenario(tmp_path, monkeypatch)
    result = scenario.run()
    assert result["status"] == "pass"
    assert [item["label"] for item in result["steps"]] == [
        "candidate admission", "R12 bootstrap", "R12 start", "R12 seed", "update", "release check", "doctor",
        "dry-run", "data check"]
    assert {item["status"] for item in result["steps"]} == {"pass"}
    assert result["checks"] == {name: "pass" for name in wr.CHECKS}
    assert scenario.receipt() == json.loads(json.dumps(result))
    assert gate._release12_row_errors(JOB, result, TARGET, "windows") == []
    assert result["receipt_kind"] == LEG and result["scenario"] == LEG and result["job_id"] == JOB
    assert result["release_transition"] == {"from": 12, "to": TARGET, "source": "candidate-install-update"}
    assert result["installed_tuple"] == {"app": CANDIDATE["app"], "agent": CANDIDATE["agent"],
                                         "runtime": CANDIDATE["runtime"], "schema_digest": None, "tools": {}}
    assert result["sources"]["agent"] == MANIFEST["agent"] and result["sources"]["runtime"] == MANIFEST["runtime"]
    assert result["doctor"]["ok"] is True and result["dry_run"]["capability_id"] == wr.DRY_RUN_CAPABILITY
    assert result["data_marker"]["survived"] is True and result["live_runtimes"][0]["pid"] == 222
    assert result["release12_source"]["app_commit"] == jumps.RELEASE12_APP_COMMIT


def test_the_leg_installs_release12_plainly_then_updates_and_checks_in_the_right_order(tmp_path, monkeypatch):
    scenario = Scenario(tmp_path, monkeypatch)
    scenario.run()
    calls = scenario.calls
    bootstraps = [call for call in calls if isinstance(call, tuple) and call[0] == "bootstrap"]
    app = str(tmp_path / "app")
    assert [(call[1], call[2] == app) for call in bootstraps] == [("install", False), ("update", True)]
    assert all(call[3] == "powershell.exe" for call in bootstraps)
    assert bootstraps[0][2].endswith("release12-source")
    names = [call if isinstance(call, str) else call[0] for call in calls]
    for earlier, later in (("release12_admission", "bootstrap_install"), ("bootstrap_install", "bootstrap_update"),
                           ("verify_observe", "bootstrap_update"), ("bootstrap_update", "verify_update"),
                           ("verify_update", "doctor"), ("doctor", "dry_run"), ("dry_run", "assert_content")):
        assert names.index(earlier) < names.index(later), (earlier, later)
    # The Release 12 Runtime is started through its scheduled task and given the Windows wait floor.
    start = next(call for call in calls if isinstance(call, tuple) and call[0] == "powershell")
    assert start[1] == "start.log" and "Start-ScheduledTask -TaskName 'Astrid Runtime'" in start[2]
    assert all(call[1] >= wr.WAIT_FLOOR_SECONDS for call in calls if isinstance(call, tuple) and call[0] == "wait_ready")
    # The seed goes through the installed Release 12 command line; nothing is generated for real.
    cli = [call for call in calls if isinstance(call, tuple) and call[0] == "cli"]
    assert cli[0][1][:3] == ("projects", "create", wr.PROJECT)
    runs = [call[1] for call in calls if isinstance(call, tuple) and call[0] == "run"]
    assert runs[0][2:] == ("-m", "astrid", "doctor") and runs[1][:2] == ("python.exe", "-I") and "-c" in runs[1]
    assert (scenario.data / wr.MARKER_NAME).read_text(encoding="utf-8") == wr.MARKER_TEXT
    # Structured evidence for the doctor and the dry run is written under the two staged names.
    doctor = json.loads((scenario.evidence / wr.DOCTOR_EVIDENCE).read_text())
    dry_run = json.loads((scenario.evidence / wr.DRY_RUN_EVIDENCE).read_text())
    assert doctor["exit_code"] == 0 and doctor["report"]["ok"] is True
    assert dry_run["exit_code"] == 0 and dry_run["report"]["raw_result"]["dry_run"] is True


FAILURES = [
    ("selected_manifest", RuntimeError("no manifest"), "candidate admission"),
    ("release12_admission", RuntimeError("no pinned source"), "R12 bootstrap"),
    ("bootstrap_install", RuntimeError("install-bootstrap.log: exit 1"), "R12 bootstrap"),
    ("wait_ready", RuntimeError("never healthy"), "R12 start"),
    ("installed_cli", RuntimeError("projects create failed"), "R12 seed"),
    ("verify_observe", RuntimeError("observe failed"), "R12 seed"),
    ("update_exit", 3, "update"),
    ("verify_update", RuntimeError("verify failed"), "release check"),
    ("installs_nothing", True, "release check"),
    ("old_process_survives", True, "release check"),
    ("receipt", {"kind": "not-updated", "exit_code": 3, "still": 12, "target": TARGET}, "update"),
    ("doctor_exit", 1, "doctor"),
    ("dry_run_report", {**DRY_RUN_REPORT, "ok": False}, "dry-run"),
    ("assert_content", RuntimeError("marker missing"), "data check"),
]


@pytest.mark.parametrize(("hook", "failure", "label"), FAILURES)
def test_a_failure_names_the_step_it_happened_in_and_is_recorded(tmp_path, monkeypatch, hook, failure, label):
    scenario = Scenario(tmp_path, monkeypatch, **{hook: failure})
    with pytest.raises(wr.WindowsR12StepError) as raised:
        scenario.run()
    assert f"failed at step '{label}'" in str(raised.value) and raised.value.label == label
    receipt = scenario.receipt()
    assert receipt["status"] == "fail" and f"step '{label}'" in receipt["error"]
    last = receipt["steps"][-1]
    assert last["label"] == label and last["status"] == "fail" and last["error"]
    assert all(item["status"] == "pass" for item in receipt["steps"][:-1])
    assert receipt["checks"]["unrelated_sentinel"] == "not-run"


def test_a_nonzero_update_exit_reports_the_exit_and_the_log_tail(tmp_path, monkeypatch):
    scenario = Scenario(tmp_path, monkeypatch, update_exit=3)
    with pytest.raises(wr.WindowsR12StepError, match=r"exited 3 from the Release 12 install"):
        scenario.run()
    update = scenario.receipt()["update"]
    assert update["exit_code"] == 3 and update["command"] == "install --update"
    assert update["last_lines"] == ["=== stdout ===", "Updating Astrid"]


def test_a_missing_run_receipt_is_recorded_not_invented(tmp_path, monkeypatch):
    scenario = Scenario(tmp_path, monkeypatch, receipt=None)
    result = scenario.run()
    assert result["update"]["receipt"] is None and result["status"] == "pass"
    assert gate._release12_row_errors(JOB, result, TARGET, "windows") == []
    assert gate._release12_row_errors(JOB, result, TARGET, "linux") != []


def test_a_candidate_that_is_not_newer_than_release12_is_refused(tmp_path, monkeypatch):
    scenario = Scenario(tmp_path, monkeypatch)
    monkeypatch.setattr(wr, "evidence_gate", SimpleNamespace(validate_tuple=lambda *unused: {**MANIFEST, "release": 12}))
    monkeypatch.setattr(scenario.journey, "selected_manifest",
                        lambda app, **kw: (app / "m.json", {**MANIFEST, "release": 12}))
    with pytest.raises(wr.WindowsR12StepError, match="candidate admission.*newer than Release 12"):
        scenario.run()


# ---------------------------------------------------------------------------------------------
# The user's path: the real bootstrap argv on Windows, and the helpers
# ---------------------------------------------------------------------------------------------


def test_the_release12_install_and_the_candidate_update_are_the_two_install_ps1_commands(tmp_path, monkeypatch):
    captured = []
    monkeypatch.setattr(journey.sys, "platform", "win32")
    monkeypatch.setattr(journey.windows, "EVIDENCE", tmp_path)
    monkeypatch.setattr(journey.windows, "run_bootstrap_process",
                        lambda argv, log, mode, **kw: captured.append((mode, [str(part) for part in argv],
                                                                       kw["env"])))
    monkeypatch.setattr(journey, "installer_has_consent_gate", lambda agent, manifest: True)
    source, manifest = tmp_path / "release12-source", tmp_path / "release12-source" / "install-manifest.json"
    jumps.bootstrap_release12(source, manifest, "powershell.exe", tmp_path, tmp_path / "program",
                              tmp_path / "data", agent=tmp_path / "agent")
    app = tmp_path / "app"
    journey.bootstrap(app, app / "public" / "install-manifest.json", "powershell.exe", "update", tmp_path,
                      tmp_path / "program", tmp_path / "data")
    (install_mode, install_argv, install_env), (update_mode, update_argv, update_env) = captured
    assert (install_mode, update_mode) == ("install", "update")
    prefix = ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File"]
    assert install_argv[:6] == prefix and update_argv[:6] == prefix
    assert Path(install_argv[6]) == source / "public" / "install.ps1"
    assert Path(update_argv[6]) == app / "public" / "install.ps1"
    assert "--update" not in install_argv and "--yes" in install_argv
    assert update_argv[-1] == "--update" and "--yes" not in update_argv
    assert all(key.casefold() != "psmodulepath" for env in (install_env, update_env) for key in env)
    assert install_env["UV_LINK_MODE"] == update_env["UV_LINK_MODE"] == "copy"


def test_candidate_update_windows_returns_the_exit_code_it_did_not_raise(tmp_path, monkeypatch):
    def failing(app, manifest, shell, mode, evidence, root, data):
        assert mode == "update"
        (evidence / "update-bootstrap-process.json").write_text(json.dumps({"exit_code": 1}), encoding="utf-8")
        (evidence / "update-bootstrap.log").write_text("line one\n\nline two\n", encoding="utf-8")
        raise RuntimeError("update-bootstrap.log: exit 1")

    monkeypatch.setattr(wr.journey, "bootstrap", failing)
    update = wr.candidate_update_windows(tmp_path, tmp_path / "m.json", "powershell.exe", tmp_path, tmp_path / "p", tmp_path / "d")
    assert update == {"command": "install --update", "exit_code": 1, "log": str(tmp_path / "update-bootstrap.log"),
                      "last_lines": ["line one", "line two"]}
    # A bootstrap that raised without ever recording an exit is a failure, not a pass.
    (tmp_path / "update-bootstrap-process.json").unlink()
    monkeypatch.setattr(wr.journey, "bootstrap", lambda *unused: (_ for _ in ()).throw(RuntimeError("no receipt")))
    with pytest.raises(RuntimeError, match="left no exit code"):
        wr.candidate_update_windows(tmp_path, tmp_path / "m.json", "powershell.exe", tmp_path, tmp_path / "p", tmp_path / "d")


def test_live_runtime_owners_use_the_process_birth_never_os_kill(tmp_path, monkeypatch):
    data = tmp_path / "data"
    for root, pid in ((data, 41), (Path(str(data) + "-r1"), 42)):
        (root / "runtime").mkdir(parents=True)
        (root / "runtime" / "discovery.json").write_text(json.dumps({"pid": pid, "runtime_instance_id": f"i{pid}"}))
    monkeypatch.setattr(wr.journey, "process_birth", lambda pid: "born" if pid == 42 else None)
    monkeypatch.setattr(os, "kill", lambda *unused: pytest.fail("os.kill(pid, 0) sends CTRL_C_EVENT on Windows"))
    assert wr.windows_live_runtime_owners(data) == [
        {"data_root": str(data) + "-r1", "pid": 42, "runtime_instance_id": "i42"}]


def test_the_wait_floor_never_lowers_a_longer_configured_wait(monkeypatch):
    monkeypatch.setenv("ASTRID_CI_WAIT_READY_TIMEOUT", "180")
    assert wr.wait_timeout() == wr.WAIT_FLOOR_SECONDS
    monkeypatch.setenv("ASTRID_CI_WAIT_READY_TIMEOUT", "900")
    assert wr.wait_timeout() == 900
    monkeypatch.delenv("ASTRID_CI_WAIT_READY_TIMEOUT")
    assert wr.wait_timeout() == 900


def test_the_dry_run_must_be_a_clean_preview_that_admitted_nothing():
    summary = wr.check_dry_run(DRY_RUN_REPORT)
    assert summary["dry_run"] is True and summary["capability_id"] == wr.DRY_RUN_CAPABILITY
    assert summary["preview_inputs"] == wr.DRY_RUN_INPUTS
    def broken(**changes):
        raw = {**DRY_RUN_REPORT["raw_result"], **changes.pop("raw", {})}
        return {**DRY_RUN_REPORT, **changes, "raw_result": raw}
    for report, message in (
        (None, "no invocation result"),
        (broken(ok=False, error="boom"), "not ok"),
        (broken(raw={"dry_run": False}), "not marked dry_run"),
        (broken(raw={"executor_id": "other.capability"}), "another capability"),
        (broken(raw={"returncode": 0}), "run or been skipped"),
        (broken(run_id="01HZ"), "run, task or output was created"),
        (broken(kernel_task_id="task"), "run, task or output was created"),
        (broken(outputs={"wav": {}}), "run, task or output was created"),
        (broken(raw={"payload": {}}), "manifest preview is absent"),
    ):
        with pytest.raises(RuntimeError, match=message):
            wr.check_dry_run(report)


def test_the_dry_run_uses_the_documented_preview_of_a_generation_capability_and_no_provider_model():
    assert wr.DRY_RUN_CAPABILITY == "generation.generate_speech"
    assert "dry_run=True" in wr.DRY_RUN_CODE and "kind='executor'" in wr.DRY_RUN_CODE
    assert set(wr.DRY_RUN_INPUTS) == {"text", "provider"}
    compile(wr.DRY_RUN_CODE, "dry-run", "exec")


def test_json_helpers_find_the_report_among_diagnostic_lines():
    text = '{"diagnostic": "noise"}\n{"ok": true, "n": 2}\n'
    assert wr.last_json_object(text) == {"ok": True, "n": 2}
    assert wr.last_json_object("no json here\n") is None
    assert wr.json_document('﻿{"ok": true}') == {"ok": True}
    assert wr.json_document('warning line\n{\n  "ok": true\n}\n') == {"ok": True}
    assert wr.json_document("nothing") is None
    assert wr.output_tail("a\n\n" + "\n".join(str(i) for i in range(100)))[-1] == "99"
    assert len(wr.output_tail("x" * 5000)[0]) == wr.OUTPUT_LINE_LIMIT


def test_the_run_receipt_is_read_from_the_installer_log_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(wr.journey, "hosted_windows_runner", lambda: False)
    logs = tmp_path / "localappdata" / "Astrid Runtime" / "logs"
    assert wr.windows_log_dir(tmp_path) == logs
    assert wr.windows_run_receipt(tmp_path) is None
    logs.mkdir(parents=True)
    (logs / "last-run.json").write_text(json.dumps({"kind": "updated"}), encoding="utf-8")
    assert wr.windows_run_receipt(tmp_path) == {"kind": "updated"}
    monkeypatch.setattr(wr.journey, "hosted_windows_runner", lambda: True)
    monkeypatch.setattr(wr.journey, "_windows_local_application_data", lambda: tmp_path / "real")
    assert wr.windows_log_dir(tmp_path) == tmp_path / "real" / "Astrid Runtime" / "logs"


def test_command_evidence_is_bounded_json(tmp_path):
    path = tmp_path / "evidence.json"
    wr.write_command_evidence(path, "astrid doctor --json", 1, "\n".join(f"line {i}" for i in range(200)),
                              report={"ok": False}, error="exit 1")
    value = json.loads(path.read_text())
    assert value["exit_code"] == 1 and value["report"] == {"ok": False} and value["error"] == "exit 1"
    assert len(value["output_tail"]) == wr.OUTPUT_TAIL_LINES and value["output_tail"][-1] == "line 199"
