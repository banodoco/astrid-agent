"""The Windows release13-then-update row: the published Release 13, then the candidate's update.

A Windows user who has Release 13 installed gets the candidate by running the candidate's install
script with --update. This leg does exactly that on a disposable windows-2022 runner:

1. the pinned Release 13 (the git blobs of its App commit, resolved by upgrade_release exactly as the
   Linux and macOS r13 rows do) is installed with its install.ps1, run through Windows PowerShell 5.1
   with the plain install command and no --update, the same entry the fresh-install row uses;
2. the Runtime is started through its Task Scheduler task, a project and a workspace marker are seeded
   through the installed Release 13 command line;
3. the candidate's public/install.ps1 --update takes that install to the candidate release;
4. the installed release is the candidate's, with the candidate's Agent and Runtime commits from its
   frozen receipt, the live Runtime reports it and is healthy, and exactly one Runtime is live;
5. ``astrid doctor`` passes and a dry-run generation runs (no network provider, no GPU, no task);
6. the seeded project and marker survived and the unrelated sentinel is still alive.

journey-result.json is rewritten after every step, and a failure names the step: candidate admission,
R13 bootstrap, R13 start, R13 seed, update, release check, doctor, dry-run or data check.

Call only through journey_version_jumps.execute_owned, inside ci_journey's scope, environment, sentinel
and cleanup: this module does not admit lifecycle roots.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request

if __package__:
    from . import check_journey_evidence as evidence_gate
    from . import ci_journey as journey
    from . import journey_version_jumps as jumps
else:
    import check_journey_evidence as evidence_gate
    import ci_journey as journey
    import journey_version_jumps as jumps

LEG = "release13-then-update"
KILLED_LEG = "release13-then-killed-update"
PROJECT = "windows-r13-update"
MARKER_NAME = "tst-b1-workspace-marker.txt"
MARKER_TEXT = "Windows release-13 update workspace marker\n"
DOCTOR_EVIDENCE = "windows-r13-doctor.json"
DRY_RUN_EVIDENCE = "windows-r13-dry-run.json"
# The scratch outputs the two commands write; only the structured evidence files above are retained.
DOCTOR_OUTPUT = "windows-r13-doctor-output.txt"
DRY_RUN_OUTPUT = "windows-r13-dry-run-output.txt"
# Cold Windows starts are slower than the 180 seconds a single-row dispatch gives the other rows.
WAIT_FLOOR_SECONDS = 600
OUTPUT_TAIL_LINES = 40
OUTPUT_LINE_LIMIT = 400

# The dry-run generation: the documented SDK preview of an executor capability. dry_run=True builds the
# preview from the capability's manifest and admits nothing: no task, no run, no provider and no GPU.
DRY_RUN_CAPABILITY = "generation.generate_speech"
DRY_RUN_INPUTS = {"text": "Astrid journey dry run", "provider": "edge-tts"}
DRY_RUN_CODE = (
    "import json, sys\n"
    "import astrid.sdk as sdk\n"
    "capability, project, inputs = sys.argv[1], sys.argv[2], json.loads(sys.argv[3])\n"
    "result = sdk.invoke(capability, kind='executor', project=project, inputs=inputs, dry_run=True)\n"
    "print(json.dumps(result.to_dict(), default=str, sort_keys=True))\n"
)

# Step keys, in order, with the label every failure message and receipt row carries.
STEPS = (
    ("admission", "candidate admission"),
    ("r13_bootstrap", "R13 bootstrap"),
    ("r13_start", "R13 start"),
    ("r13_seed", "R13 seed"),
    ("update", "update"),
    ("release_check", "release check"),
    ("doctor", "doctor"),
    ("dry_run", "dry-run"),
    ("data_check", "data check"),
    ("save_timeline", "save timeline on installed candidate"),
    ("reopen_timeline", "reload and reopen saved timeline"),
    ("interrupted_update", "kill update at observed durable journal checkpoint"),
    ("recover_old", "recover Release 13 installation and intact workspace"),
)
STEP_LABELS = {**dict(STEPS), "retry_candidate": "retry public update and reopen the recovered composition"}
# The receipt's assertions; the evidence checker names the same ones.
CHECKS = ("release13_install", "update_exit", "release", "one_runtime", "service", "health",
          "doctor", "dry_run", "data_marker", "timeline_saved", "timeline_reopened", "unrelated_sentinel")
KILLED_CHECKS = ("release13_install", "interruption_checkpoint", "process_killed", "recovery_exit",
                 "rollback_release", "one_runtime", "service", "health", "data_marker",
                 "timeline_reopened", "retry_exit", "retry_live_candidate", "unrelated_sentinel")


class WindowsR13StepError(RuntimeError):
    """A failure that names the journey step it happened in."""

    def __init__(self, label: str, cause: BaseException):
        super().__init__(f"Windows release-13 update failed at step '{label}': {type(cause).__name__}: {cause}")
        self.label = label


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def step(result: dict, result_path: Path, key: str):
    """Record one step in the receipt before and after it runs; a failure names the step."""
    label = STEP_LABELS[key]
    record = {"step": key, "label": label, "status": "running", "started_at": _now()}
    result["steps"].append(record)
    journey.write_json(result_path, result)
    print(f"[windows-r13] {label}: start", flush=True)
    try:
        yield record
    except Exception as exc:
        record.update(status="fail", ended_at=_now(), error=f"{type(exc).__name__}: {exc}",
                      child_exit_code=getattr(exc, "child_exit_code", None),
                      child_stderr_tail=output_tail(getattr(exc, "child_stderr", "")))
        journey.write_json(result_path, result)
        print(f"[windows-r13] {label}: FAILED: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        if isinstance(exc, WindowsR13StepError):
            raise
        raise WindowsR13StepError(label, exc) from exc
    record.update(status="pass", ended_at=_now())
    journey.write_json(result_path, result)
    print(f"[windows-r13] {label}: pass", flush=True)


def wait_timeout() -> int:
    return max(int(os.environ.get("ASTRID_CI_WAIT_READY_TIMEOUT", "900")), WAIT_FLOOR_SECONDS)


def installed_env(version: dict) -> dict:
    env = {key: value for key, value in os.environ.items() if key not in {"PYTHONPATH", "PYTHONHOME"}}
    env.update(version["env"])
    # The installed-profile execution the other Windows task legs run under.
    env["ASTRID_HOST_EXECUTION_PROFILE"] = "installed"
    return env


def run_captured(argv: list, log: Path, *, env: dict, timeout: int) -> tuple[int, str]:
    """Run one installed command, keeping both streams; the exit code is returned, not raised."""
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("w", encoding="utf-8") as stream:
        completed = subprocess.run([str(part) for part in argv], env=env, stdout=stream,
                                   stderr=subprocess.STDOUT, timeout=timeout, check=False)
    return completed.returncode, log.read_text(encoding="utf-8-sig", errors="replace")


def output_tail(output: str) -> list[str]:
    lines = [jumps._safe_runtime_body(line)[:OUTPUT_LINE_LIMIT]
             for line in output.splitlines() if line.strip()]
    return lines[-OUTPUT_TAIL_LINES:]


def last_json_object(output: str):
    """The last line of the output that is a JSON object (diagnostic lines may precede it)."""
    for line in reversed([line.strip() for line in output.splitlines() if line.strip()]):
        if line.startswith("{"):
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict):
                return value
    return None


def json_document(output: str):
    """The whole output as one JSON document, or the first document that starts a line."""
    text = output.lstrip("﻿")
    try:
        return json.loads(text)
    except ValueError:
        pass
    decoder = json.JSONDecoder()
    for match in re.finditer(r"(?m)^[{\[]", text):
        try:
            value, _ = decoder.raw_decode(text[match.start():])
        except ValueError:
            continue
        return value
    return None


def write_command_evidence(path: Path, command: str, exit_code, output: str, *, report=None, error=None) -> None:
    """The structured evidence of one installed command: its exit, the report and a bounded output tail."""
    journey.write_json(path, {"schema_version": 1, "command": command, "exit_code": exit_code,
                              "error": error, "report": report, "output_tail": output_tail(output)})


def windows_log_dir(run_root: Path) -> Path:
    """The directory holding install.log and last-run.json (the installer's own log directory)."""
    if journey.hosted_windows_runner():
        return journey._windows_local_application_data() / "Astrid Runtime" / "logs"
    return run_root / "localappdata" / "Astrid Runtime" / "logs"


def windows_run_receipt(run_root: Path) -> dict | None:
    """The candidate installer's last-run.json, or None where this runner did not write one."""
    path = windows_log_dir(run_root) / "last-run.json"
    if path.is_symlink() or not path.is_file():
        return None
    return journey.read_json(path)


def windows_live_runtime_owners(data: Path) -> list[dict]:
    """Discovery owners whose process is alive, at the data root and its R1 copy.

    os.kill(pid, 0) would send CTRL_C_EVENT on Windows, so liveness is the process birth query.
    """
    owners = []
    for root in (data, Path(str(data) + "-r1")):
        try:
            discovery = json.loads((root / "runtime" / "discovery.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        pid = discovery.get("pid") if isinstance(discovery, dict) else None
        if type(pid) is not int or pid <= 0 or not journey.process_birth(pid):
            continue
        owners.append({"data_root": str(root), "pid": pid,
                       "runtime_instance_id": discovery.get("runtime_instance_id")})
    return owners


def runtime_identity_probe(discovery: dict, expected: dict, *, receipt: dict | None = None) -> dict:
    """Read the live Runtime's own health identity and bind it to discovery and pins.

    ``journey.healthy`` intentionally returns the discovery document after checking only the
    endpoint's status and instance id. The schema digest lives in the HTTP response.
    """
    endpoint = discovery.get("endpoint")
    try:
        parsed = urllib.parse.urlsplit(endpoint) if isinstance(endpoint, str) else None
        host = parsed.hostname if parsed else None
        loopback = host == "localhost" or (host is not None and ipaddress.ip_address(host).is_loopback)
        valid = (parsed is not None and parsed.scheme == "http" and loopback
                 and parsed.port is not None and parsed.username is None and parsed.password is None
                 and parsed.path in ("", "/") and not parsed.query and not parsed.fragment)
    except ValueError:
        valid = False
    if not valid:
        raise RuntimeError("Runtime discovery has no loopback health endpoint")
    health_url = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "", "", "")).rstrip("/") + "/v1/health"
    request = urllib.request.Request(health_url, headers={"Origin": "https://astrid.haus"})
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=10) as response:
        health = json.load(response)
    expected_identity = {"release": expected["release"], "managed_by": None}
    # Keep only the identity fields needed to classify a mismatch. Health can
    # grow operational fields, including sensitive values, so never dump it whole.
    def safe_identity(value):
        return ({key: value.get(key) for key in ("release", "managed_by")}
                if isinstance(value, dict) else None)

    if receipt is not None:
        receipt["live_probe"] = {
            "endpoint": {"scheme": parsed.scheme, "host": host, "port": parsed.port},
            "discovery": {"pid": discovery.get("pid"),
                          "runtime_instance_id": discovery.get("runtime_instance_id"),
                          "identity": safe_identity(discovery.get("identity"))},
            "health": {"status": health.get("status"),
                       "runtime_instance_id": health.get("runtime_instance_id"),
                       "identity": safe_identity(health.get("identity")),
                       "schema_digest": health.get("schema_digest")},
        }
    matches = (health.get("status") == "ok"
               and health.get("runtime_instance_id") == discovery.get("runtime_instance_id")
               and health.get("identity") == expected_identity
               and discovery.get("identity") == expected_identity
               and health.get("schema_digest") == expected["schema_digest"])
    if not matches:
        raise RuntimeError("Live Runtime identity does not match the exact selected release")
    return health


def candidate_update_windows(app: Path, manifest_path: Path, shell: str, evidence: Path,
                             root: Path, data: Path) -> dict:
    """Run the candidate's public install.ps1 --update once and return what it reported.

    The exit code is returned, not raised, so the leg asserts it beside the installed release: an
    update that exits 0 and installs nothing is the failure this leg exists to catch.
    """
    previous_receipt = windows_run_receipt(root.parent)
    invocation_started = time.time()
    error = None
    try:
        journey.bootstrap(app, manifest_path, shell, "update", evidence, root, data)
    except RuntimeError as exc:
        error = exc
    process = evidence / "update-bootstrap-process.json"
    code = journey.read_json(process).get("exit_code") if process.is_file() else None
    if type(code) is not int:
        raise RuntimeError(f"The candidate update left no exit code: {error or 'no process receipt'}")
    if error is not None and code == 0:
        raise error
    log = evidence / "update-bootstrap.log"
    lines = ([line for line in log.read_text(encoding="utf-8-sig", errors="replace").splitlines() if line.strip()]
             if log.is_file() else [])
    receipt = windows_run_receipt(root.parent)
    invocation = {"started_at": int(invocation_started), "finished_at": int(time.time()),
                  "previous_operation_id": (previous_receipt or {}).get("operation_id")}
    invocation_tied = evidence_gate._windows_receipt_matches_invocation(receipt, invocation)
    stderr_log = evidence / "update-bootstrap-stderr.log"
    stderr = stderr_log.read_text(encoding="utf-8-sig", errors="replace") if stderr_log.is_file() else ""
    return {"command": "install --update", "exit_code": code, "log": str(log),
            "last_lines": output_tail("\n".join(lines[-8:])), "stderr_tail": output_tail(stderr),
            "installer_receipt": receipt, "invocation": invocation,
            "receipt_invocation_tied": invocation_tied}


def check_dry_run(report) -> dict:
    """The dry-run envelope must be a preview that admitted nothing."""
    raw = report.get("raw_result") if isinstance(report, dict) else None
    if not isinstance(raw, dict):
        raise RuntimeError("The dry-run printed no invocation result")
    problems = []
    if report.get("ok") is not True or raw.get("ok") is not True:
        problems.append(f"not ok (error: {report.get('error') or raw.get('error')})")
    if raw.get("dry_run") is not True:
        problems.append("the result is not marked dry_run")
    if raw.get("executor_id") != DRY_RUN_CAPABILITY or report.get("capability_id") != DRY_RUN_CAPABILITY:
        problems.append("the result is for another capability")
    if raw.get("returncode") is not None or raw.get("skipped") is True:
        problems.append("the capability appears to have run or been skipped")
    if any(report.get(name) for name in ("run_id", "run_root", "kernel_run_id", "kernel_task_id")) or report.get("outputs"):
        problems.append("a run, task or output was created")
    preview = ((raw.get("payload") or {}).get("preview") or {}) if isinstance(raw.get("payload"), dict) else {}
    if preview.get("kind") != "manifest-ledger" or preview.get("capability_id") != DRY_RUN_CAPABILITY:
        problems.append("the manifest preview is absent")
    if problems:
        raise RuntimeError("Dry-run generation is not a clean preview: " + "; ".join(problems))
    return {"capability_id": DRY_RUN_CAPABILITY, "ok": True, "dry_run": True,
            "command": raw.get("command"), "preview_inputs": preview.get("inputs")}


def kill_update_at_owned_checkpoint(app: Path, candidate_manifest: Path, shell: str,
                                    evidence: Path, root: Path, data: Path) -> dict:
    """Kill only this row's newly spawned installer, after observing its durable journal.

    A timer-only kill is not proof. An already-completed update is a failure,
    not a successful interrupted-update result. The run uses only the isolated
    row-owned program/data and native Windows PowerShell 5.1.
    """
    flags = journey.bootstrap_flags(candidate_manifest, "update", root, data)
    argv = [shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-File", app / "public" / "install.ps1", *flags]
    env = {k: v for k, v in os.environ.items() if k.casefold() != "psmodulepath"}
    env["UV_LINK_MODE"] = "copy"
    env["BROWSER"] = '"' + sys.executable + '" -c "pass" %s'
    journal_path = root / "update-transaction.json"
    log_path = evidence / "killed-update-bootstrap.log"
    stderr_path = evidence / "killed-update-bootstrap.stderr.txt"
    evidence.mkdir(parents=True, exist_ok=True)
    caught = None
    with log_path.open("wb") as log, stderr_path.open("wb") as stderr:
        process = subprocess.Popen([str(item) for item in argv], env=env,
                                   stdout=log, stderr=stderr)
        try:
            until = time.monotonic() + 180
            while time.monotonic() < until:
                if process.poll() is not None:
                    break
                if journal_path.is_file():
                    try:
                        value = journey.read_json(journal_path)
                    except (OSError, ValueError):
                        value = None
                    if (isinstance(value, dict) and value.get("step") in {"stopped", "backed_up"}
                            and isinstance(value.get("operation_id"), str) and value["operation_id"]):
                        caught = {"phase": value["step"], "operation_id": value["operation_id"],
                                  "installer_pid": process.pid}
                        break
                time.sleep(0.02)
            if caught is None:
                code = process.poll()
                error = RuntimeError("Updater completed before a killable durable checkpoint" if code is not None
                                     else "No durable stopped/backed_up update checkpoint before deadline")
                error.child_exit_code = code
                error.child_stderr = stderr_path.read_text(encoding="utf-8", errors="replace")
                raise error
            # taskkill /T acts on the just-created process and its children,
            # not on a journal PID or a guessed process name.
            killed = subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                capture_output=True, timeout=30, check=False)
            exit_code = process.wait(timeout=30)
            if killed.returncode != 0 or exit_code == 0:
                error = RuntimeError("Owned update process was not demonstrably terminated")
                error.child_exit_code = exit_code
                error.child_stderr = stderr_path.read_text(encoding="utf-8", errors="replace")
                raise error
            return {**caught, "taskkill_exit_code": killed.returncode,
                    "installer_exit_code": exit_code, "killed": True}
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=30)


def recover_killed_r13_update(args, result: dict, result_path: Path, *,
                              app: Path, candidate_path: Path, root: Path, data: Path,
                              evidence: Path, original: dict, marker: Path, digest: str,
                              sentinel, birth: str) -> dict:
    """Recover the original R13 on exit 3, then retry successfully on the same roots."""
    original_composition = result["timeline_seed"]["composition"]
    with step(result, result_path, "interrupted_update"):
        interrupted = kill_update_at_owned_checkpoint(
            app, candidate_path, args.shell, evidence, root, data)
        result["interruption"] = interrupted
        result["checks"]["interruption_checkpoint"] = "pass"
        result["checks"]["process_killed"] = "pass"

    with step(result, result_path, "recover_old"):
        # Use the same native Windows capture root as the original driver.
        # A nonexistent nested evidence dir would otherwise hide last-run.json
        # and make an exit-3 recovery look like a missing process receipt.
        retry = candidate_update_windows(app, candidate_path, args.shell,
                                         evidence, root, data)
        result["recovery"] = {"exit_code": retry["exit_code"],
                              "command": retry["command"], "interruption": interrupted,
                              "stderr_tail": retry["stderr_tail"],
                              "installer_receipt": retry.get("installer_receipt"),
                              "invocation": retry["invocation"],
                              "receipt_invocation_tied": retry.get("receipt_invocation_tied")}
        # Recovery-only is *not* a successful update. Exit 3 was introduced
        # specifically so this state is not mistaken for a published candidate.
        recovery_receipt = retry.get("installer_receipt")
        if (retry["exit_code"] != 3 or retry.get("receipt_invocation_tied") is not True
                or not isinstance(recovery_receipt, dict) or recovery_receipt.get("exit_code") != 3
                or recovery_receipt.get("kind") != "not-updated"):
            raise RuntimeError("Interrupted candidate update did not give a recovery-only exit 3")
        result["checks"]["recovery_exit"] = "pass"

        version_path, selected_data, version = journey.installation(root, data)
        if (jumps.installed_release(version) != jumps.RELEASE13_RELEASE
                or selected_data != data or version["root"] != original["root"]):
            raise RuntimeError("Recovered Windows installation is not published Release 13")
        previous_manifest = journey.read_json(Path(version["root"]) / "manifest.json")
        if (previous_manifest.get("agent", {}).get("sha") != jumps.RELEASE13_IDENTITY["agent"]
                or previous_manifest.get("runtime", {}).get("sha") != jumps.RELEASE13_IDENTITY["runtime"]):
            raise RuntimeError("Recovered Windows installation does not match published R13 pins")
        result["checks"]["rollback_release"] = "pass"
        recovery = result["recovery"]
        recovery["release_transition"] = {"from": jumps.RELEASE13_RELEASE, "to": jumps.RELEASE13_RELEASE,
                                          "source": "interrupted-candidate-recovered-previous"}
        journey.windows.powershell("Start-ScheduledTask -TaskName 'Astrid Runtime' -TaskPath '\\'", "recovery-start.log")
        journey.wait_ready(data, timeout=wait_timeout())
        owners = recovery["live_runtimes"] = windows_live_runtime_owners(data)
        if [item["data_root"] for item in owners] != [str(data)]:
            raise RuntimeError("Recovered R13 must have exactly one live owned Runtime")
        result["checks"]["one_runtime"] = "pass"
        recovery["service_manager"] = journey.assert_direct_service_ownership(root, data)
        result["checks"]["service"] = "pass"
        discovery = journey.healthy(data)
        health = runtime_identity_probe(
            discovery, {"release": jumps.RELEASE13_RELEASE, **jumps.RELEASE13_IDENTITY},
            receipt=recovery)
        if owners[0].get("runtime_instance_id") != health.get("runtime_instance_id"):
            raise RuntimeError("Recovered Runtime does not report Release 13")
        result["checks"]["health"] = "pass"
        recovery["live_release"] = jumps.RELEASE13_RELEASE
        recovery["installed_tuple"] = {"app": jumps.RELEASE13_APP_COMMIT, **jumps.RELEASE13_IDENTITY}
        (evidence / "recovery").mkdir(parents=True, exist_ok=True)
        journey.verify("install", version_path, data, evidence / "recovery")
        recovery["import_origins"] = (
            journey.read_json(evidence / "recovery" / "verify-install.json").get("details") or {}).get("origin.imports")
        journey.assert_content(version, evidence, PROJECT, marker, digest)
        result["data_marker"]["survived"] = True
        recovery["data_marker"] = dict(result["data_marker"])
        result["checks"]["data_marker"] = "pass"
        recovery["timeline_reopen"] = jumps.live_workspace(
            root, data, evidence / "recovery-reopen", "reopen_saved_composition", original_composition)
        result["checks"]["timeline_reopened"] = "pass"

    with step(result, result_path, "retry_candidate"):
        retry_dir = evidence / "public-retry"
        retry = candidate_update_windows(app, candidate_path, args.shell, retry_dir, root, data)
        result["retry"] = retry
        retry_receipt = retry.get("installer_receipt")
        if (retry["exit_code"] != 0 or retry.get("receipt_invocation_tied") is not True
                or not isinstance(retry_receipt, dict) or retry_receipt.get("kind") != "updated"
                or retry_receipt.get("exit_code") != 0):
            raise RuntimeError("Public update retry after recovery did not exit 0")
        manifest = journey.read_json(candidate_path)
        version_path, selected_data, version = journey.installation(root, data)
        target_release = manifest.get("release")
        if (retry_receipt.get("still") != target_release or retry_receipt.get("target") != target_release):
            raise RuntimeError("Public update retry receipt does not match the candidate manifest release")
        if jumps.installed_release(version) != target_release or selected_data != data:
            raise RuntimeError("Public update retry did not select the exact live R14 installation")
        selected = jumps.selected_candidate(manifest, version, jumps.RELEASE13_IDENTITY)
        active_data = jumps.selected_data_root(root, data, allow_r1=False)
        journey.wait_ready(active_data, timeout=wait_timeout())
        live_discovery = journey.healthy(active_data)
        live = runtime_identity_probe(
            live_discovery, {"release": target_release, **selected}, receipt=retry)
        owners = windows_live_runtime_owners(active_data)
        if ((live.get("identity") or {}).get("release") != target_release
                or live.get("schema_digest") != selected["schema_digest"]
                or not live.get("runtime_instance_id") or len(owners) != 1
                or owners[0].get("runtime_instance_id") != live.get("runtime_instance_id")
                or owners[0].get("data_root") != str(data)
                or live.get("runtime_instance_id") == health.get("runtime_instance_id")):
            raise RuntimeError("Public update retry left no healthy live candidate Runtime")
        service = journey.assert_direct_service_ownership(root, active_data)
        verified = journey.verify("install", version_path, active_data, retry_dir)
        result["import_origins"] = (journey.read_json(verified).get("details") or {}).get("origin.imports")
        result["installed_tuple"] = {"app": result["sources"]["app"]["sha"], **selected}
        result["release_transition"] = {"from": jumps.RELEASE13_RELEASE, "to": target_release,
                                        "source": "candidate-install-update"}
        result["live_runtimes"] = owners
        result["service_manager"] = service
        retry.update(installed_tuple=result["installed_tuple"], live_release=target_release,
                     live_runtimes=owners, candidate_manifest_sha256=hashlib.sha256(candidate_path.read_bytes()).hexdigest())
        journey.assert_content(version, evidence, PROJECT, marker, digest)
        retry["data_marker"] = dict(result["data_marker"])
        retry["timeline_reopen"] = jumps.live_workspace(
            root, active_data, evidence / "retry-composition-reopen", "reopen_saved_composition", original_composition)
        result["checks"]["retry_exit"] = "pass"
        result["checks"]["retry_live_candidate"] = "pass"
        settled = journey.sentinel_observation(sentinel, birth)
        result["sentinel"]["at_settlement"] = settled
        if not settled["alive"]:
            raise RuntimeError("Interrupted update disturbed an unrelated process")
        result["checks"]["unrelated_sentinel"] = "pass"
    result["status"] = "pass"
    return result


def execute(args, root: Path, data: Path, evidence: Path, sentinel, birth: str) -> dict:
    """Pinned Release 13 plain install, seed, the candidate's install.ps1 --update, then the checks."""
    evidence.mkdir(parents=True, exist_ok=True)
    journey.windows.EVIDENCE = evidence
    killed = args.leg == KILLED_LEG
    result = {
        "schema_version": 1, "receipt_kind": KILLED_LEG if killed else LEG,
        "job_id": args.job_id, "mode": args.mode, "scenario": args.leg, "status": "not-run",
        "os_build": platform.platform(), "shell": args.shell, **journey.runner_identity(),
        "source_override_test_mode": False, "candidate_checkouts": getattr(args, "candidate_checkouts", None),
        "service_manager_prerequisite": getattr(args, "service_manager_prerequisite", None),
        "sources": None, "installed_tuple": None, "import_origins": None,
        "run_roots": {"program": str(root), "data": str(data)},
        "sentinel": {"pid": sentinel.pid, "birth": birth},
        "release13_source": None, "candidate_bootstrap": None, "update": None,
        "release_transition": None, "live_runtimes": None, "health_transition": None,
        "doctor": None, "dry_run": None, "data_marker": None,
        "timeline_seed": None, "timeline_save": None, "timeline_reopen": None,
        "interruption": None, "recovery": None, "steps": [],
        "checks": {key: "not-run" for key in (KILLED_CHECKS if killed else CHECKS)},
    }
    result_path = evidence / "journey-result.json"
    journey.write_json(result_path, result)
    try:
        # Candidate admission: the frozen candidate manifest this leg updates to.
        with step(result, result_path, "admission"):
            app = args.app_checkout.resolve(strict=True)
            candidate_path, manifest = journey.selected_manifest(
                app, source_override=False, agent=None, runtime=None, evidence=evidence)
            candidate = journey.read_json(args.candidate)
            if evidence_gate.validate_tuple(candidate, app, candidate_path) != manifest:
                raise RuntimeError(f"{LEG} requires a frozen candidate manifest")
            target_release = manifest.get("release")
            if type(target_release) is not int or target_release <= jumps.RELEASE13_RELEASE:
                raise RuntimeError(f"{LEG} requires a candidate newer than Release {jumps.RELEASE13_RELEASE}")
            result["candidate_bootstrap"] = {"manifest_sha256": candidate["manifest_sha256"],
                                             "artifacts": candidate["artifacts"]}
            result["sources"] = {"app": journey.git_source(app), "agent": manifest["agent"],
                                 "runtime": manifest["runtime"]}
            before_sentinel = journey.sentinel_observation(sentinel, birth)
            result["sentinel"]["before_lifecycle"] = before_sentinel
            if not before_sentinel["alive"]:
                raise RuntimeError("Unrelated sentinel exited before the release-13 install")

        # 1. The pinned Release 13, installed with its own install.ps1 and the plain install command.
        with step(result, result_path, "r13_bootstrap"):
            source, release13_manifest, source_receipt = jumps.release13_admission(
                args, root.parent / "release13-source")
            result["release13_source"] = source_receipt
            jumps.bootstrap_release13(source, release13_manifest, args.shell, evidence, root, data,
                                      agent=jumps.release9_consent_agent(args))

        # 2. The Release 13 Runtime, started the way the fresh-install row starts its own.
        with step(result, result_path, "r13_start"):
            journey.windows.powershell("Start-ScheduledTask -TaskName 'Astrid Runtime' -TaskPath '\\'", "start.log")
            journey.wait_ready(data, timeout=wait_timeout())
            old_path, old_data, old = journey.installation(root, data)
            if old_data != data or jumps.installed_release(old) != jumps.RELEASE13_RELEASE:
                raise RuntimeError(f"Pinned Release-13 install did not select release {jumps.RELEASE13_RELEASE}")
            old_tuple = journey.selected_tuple(journey.read_json(release13_manifest), old)
            old_service = journey.assert_direct_service_ownership(root, data)
            old_health = journey.healthy(data)
            old_pid = old_health.get("pid")
            old_birth = journey.process_birth(old_pid) if type(old_pid) is int else None
            if not old_birth:
                raise RuntimeError("Cannot establish the Release-13 Runtime process identity")
            result["checks"]["release13_install"] = "pass"

        # 3. User data on the Release 13 install: a project and a marker in the workspace.
        with step(result, result_path, "r13_seed"):
            journey.verify("observe", old_path, data, evidence)
            created = json.loads(journey.installed_cli(
                old, ["projects", "create", PROJECT, "--name", "Windows release 12 update", "--json"],
                evidence, "release13-project.json"))
            if created.get("ok") is not True:
                raise RuntimeError("Project creation on the Release-13 install failed")
            marker = journey.workspace_root(data) / MARKER_NAME
            marker.write_text(MARKER_TEXT, encoding="utf-8")
            digest = hashlib.sha256(marker.read_bytes()).hexdigest()
            result["data_marker"] = {"project": PROJECT, "sha256": digest, "survived": None}
            # An authentic Release 13 timeline is created in the same selected
            # installed Runtime, before installing the candidate.
            result["timeline_seed"] = jumps.live_workspace(
                root, data, evidence / "release13-timeline-seed", "seed_authored_timeline",
                {"project": PROJECT + "-timeline", "generation": "release13-windows",
                 "track_label": "Original R13 recovery edit" if killed else None})

        if killed:
            return recover_killed_r13_update(
                args, result, result_path, app=app, candidate_path=candidate_path,
                root=root, data=data, evidence=evidence, original=old, marker=marker,
                digest=digest, sentinel=sentinel, birth=birth)

        # 4. The candidate's public update command, from the Release 13 install.
        with step(result, result_path, "update"):
            update = result["update"] = candidate_update_windows(app, candidate_path, args.shell, evidence, root, data)
            if update["exit_code"] != 0:
                raise RuntimeError(f"Candidate install.ps1 --update exited {update['exit_code']} from the "
                                   f"Release {jumps.RELEASE13_RELEASE} install: {update['last_lines']}")
            result["checks"]["update_exit"] = "pass"
            receipt = update["receipt"] = update.get("installer_receipt")
            if (not update.get("receipt_invocation_tied") or not isinstance(receipt, dict)
                    or receipt.get("kind") != "updated" or receipt.get("exit_code") != 0
                    or receipt.get("still") != target_release or receipt.get("target") != target_release):
                raise RuntimeError(f"The candidate's run receipt is not updated to release {target_release}: {receipt}")

        # 5. The installed and live release are the candidate's.
        with step(result, result_path, "release_check"):
            version_path, selected_data, version = journey.installation(root, data)
            if jumps.installed_release(version) != target_release:
                raise RuntimeError(
                    f"Candidate install.ps1 --update exited 0 but installed nothing: the selected release is "
                    f"still {jumps.installed_release(version)}, not the candidate release {target_release}")
            if version_path == old_path:
                raise RuntimeError("Candidate install.ps1 --update did not activate a new version")
            selected = jumps.selected_candidate(manifest, version, old_tuple)
            active_data = jumps.selected_data_root(root, data)
            journey.wait_ready(active_data, timeout=wait_timeout())
            new_health = journey.healthy(active_data)
            live_release = (new_health.get("identity") or {}).get("release")
            if live_release != target_release:
                raise RuntimeError(
                    f"The live Runtime reports release {live_release}, not the candidate release {target_release}")
            if new_health["runtime_instance_id"] == old_health["runtime_instance_id"]:
                raise RuntimeError(f"Candidate update retained the Release {jumps.RELEASE13_RELEASE} Runtime instance")
            if journey.process_birth(old_pid) == old_birth:
                raise RuntimeError(f"The Release {jumps.RELEASE13_RELEASE} Runtime process survived the update")
            owners = result["live_runtimes"] = windows_live_runtime_owners(data)
            if [owner["data_root"] for owner in owners] != [str(active_data)]:
                raise RuntimeError(f"Exactly one live Runtime (at {active_data}) is required after the update: {owners}")
            result["checks"]["one_runtime"] = "pass"
            result["release_transition"] = {"from": jumps.RELEASE13_RELEASE, "to": target_release,
                                            "source": "candidate-install-update"}
            result["checks"]["release"] = "pass"
            new_service = journey.assert_direct_service_ownership(root, selected_data)
            result["service_transition"] = {"release13": old_service, "candidate": new_service}
            result["service_manager"] = new_service
            result["health_transition"] = {
                "release13_runtime_instance_id": old_health["runtime_instance_id"],
                "candidate_runtime_instance_id": new_health["runtime_instance_id"],
                "candidate_live_release": live_release,
            }
            result["checks"].update(service="pass", health="pass")
            result["installed_tuple"] = {"app": result["sources"]["app"]["sha"], **selected}
            journey.verify("update", version_path, active_data, evidence, before=evidence / "verify-observe.json")
            journey.verify("install", version_path, active_data, evidence)
            result["import_origins"] = (
                journey.read_json(evidence / "verify-install.json").get("details") or {}).get("origin.imports")

        # 6. astrid doctor, from the installed candidate.
        with step(result, result_path, "doctor"):
            result["doctor"] = run_doctor(version, evidence)
            result["checks"]["doctor"] = "pass"

        # 7. A dry-run generation, from the installed candidate.
        with step(result, result_path, "dry_run"):
            result["dry_run"] = run_dry_run(version, evidence)
            result["checks"]["dry_run"] = "pass"

        # 8. The seeded data survived and the unrelated sentinel is alive.
        with step(result, result_path, "data_check"):
            journey.assert_content(version, evidence, PROJECT, marker, digest)
            result["data_marker"]["survived"] = True
            result["checks"]["data_marker"] = "pass"

            settled = journey.sentinel_observation(sentinel, birth)
            result["sentinel"]["at_settlement"] = settled
            if not settled["alive"]:
                raise RuntimeError("Unrelated sentinel did not survive the Release 13 install and the update")
            result["checks"]["unrelated_sentinel"] = "pass"
        with step(result, result_path, "save_timeline"):
            seed = result["timeline_seed"]
            saved = jumps.live_workspace(
                root, active_data, evidence / "candidate-save",
                "save_composition_after_update", seed)
            if saved.get("saved") is not True:
                raise RuntimeError("Installed candidate did not save the timeline edit")
            result["timeline_save"] = saved
            result["checks"]["timeline_saved"] = "pass"

        with step(result, result_path, "reopen_timeline"):
            reopened = jumps.live_workspace(
                root, active_data, evidence / "candidate-reopen",
                "reopen_saved_composition", saved)
            if (reopened.get("reopened") is not True
                    or reopened.get("accepted_revision_id") != saved.get("accepted_revision_id")
                    or reopened.get("project_id") != seed.get("project")
                    or reopened.get("timeline_id") != seed.get("timeline")):
                raise RuntimeError("The installed candidate did not reopen the accepted composition revision")
            result["timeline_reopen"] = reopened
            result["checks"]["timeline_reopened"] = "pass"
        result["status"] = "pass"
    except Exception as exc:
        result.update(status="fail", error=f"{type(exc).__name__}: {exc}")
        raise
    finally:
        journey.write_json(result_path, result)
    return result


def run_doctor(version: dict, evidence: Path) -> dict:
    """``astrid doctor --json`` from the installed candidate must exit 0 and report ok."""
    argv = [version["python"], "-I", "-m", "astrid", "doctor", "--json"]
    code, output = run_captured(argv, evidence / DOCTOR_OUTPUT, env=installed_env(version), timeout=300)
    report = json_document(output)
    write_command_evidence(evidence / DOCTOR_EVIDENCE, "astrid doctor --json", code, output,
                           report=report if isinstance(report, dict) else None)
    if code != 0:
        raise RuntimeError(f"astrid doctor exited {code}; see {DOCTOR_EVIDENCE}: {output_tail(output)[-3:]}")
    if not isinstance(report, dict):
        raise RuntimeError(f"astrid doctor printed no JSON report; see {DOCTOR_EVIDENCE}")
    if report.get("ok") is not True:
        raise RuntimeError(f"astrid doctor reports ok={report.get('ok')!r} "
                           f"(issues: {report.get('issues')}); see {DOCTOR_EVIDENCE}")
    return {"exit_code": code, "ok": True, "state": report.get("state"), "healthy": report.get("healthy"),
            "problem_code": report.get("problem_code"), "evidence": DOCTOR_EVIDENCE}


def run_dry_run(version: dict, evidence: Path) -> dict:
    """The documented SDK dry-run of a generation capability, on the installed candidate."""
    argv = [version["python"], "-I", "-c", DRY_RUN_CODE, DRY_RUN_CAPABILITY, PROJECT, json.dumps(DRY_RUN_INPUTS)]
    code, output = run_captured(argv, evidence / DRY_RUN_OUTPUT, env=installed_env(version), timeout=300)
    report = last_json_object(output)
    write_command_evidence(evidence / DRY_RUN_EVIDENCE, f"sdk.invoke({DRY_RUN_CAPABILITY}, dry_run=True)", code,
                           output, report=report)
    if code != 0:
        raise RuntimeError(f"The dry-run generation exited {code}; see {DRY_RUN_EVIDENCE}: {output_tail(output)[-3:]}")
    summary = check_dry_run(report)
    return {"exit_code": code, **summary, "evidence": DRY_RUN_EVIDENCE}
