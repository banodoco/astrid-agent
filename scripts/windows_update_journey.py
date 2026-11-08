"""Same-label Windows fresh-install update qualification."""
from __future__ import annotations

from copy import deepcopy
from collections.abc import Mapping
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if __package__:
    from . import ci_windows_journey as windows
    from . import journey_hosted_update as hosted
    from . import windows_update_process_capture as process_capture
else:
    import ci_windows_journey as windows
    import journey_hosted_update as hosted
    import windows_update_process_capture as process_capture
from astrid.core.generation.vibecomfy_dependency import VIBECOMFY_ENGINE_REVISION

MARKER = "M-RT UPDATE TEST STAND-IN"
SCHEDULER_EVENTS = "windows-update-scheduler-events.json"
STOP_TASK_STATE = "windows-update-stop-task-state.json"


_JOB_NAME = re.compile(r"astrid-update-[0-9a-f]{32}")
WAIT_LOG_LIMIT = 256 * 1024


def _scheduled_task_state(job_name):
    """The update's scheduled task: state and last result. Diagnostics only."""
    script = (f"$t = Get-ScheduledTask -TaskName '{job_name}' -ErrorAction SilentlyContinue; "
              "if ($t) { $i = $t | Get-ScheduledTaskInfo; ConvertTo-Json -InputObject @{state=[string]$t.State; "
              "last_result=$i.LastTaskResult; last_run=[string]$i.LastRunTime} -Compress } else { '{\"state\":\"absent\"}' }")
    completed = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                               capture_output=True, text=True, timeout=30, check=False)
    return json.loads(completed.stdout) if completed.returncode == 0 and completed.stdout.strip() else {"state": "unavailable"}


def make_wait_observer(evidence, root):
    """Observe the Windows update each tick of the wait: status, process tree and scheduled task.

    At the end of a failed wait the update job's own log is kept, since that is where the worker's error goes.
    Every observation is best effort: diagnostics must never change the update result.
    """
    samples = Path(evidence) / "windows-update-wait.jsonl"
    seen = {"job": None}

    def tick(body, final=False):
        entry = {"at": datetime.now(timezone.utc).isoformat(), "final": final,
                 "status": None if body is None else {key: body.get(key) for key in (
                     "state", "outcome", "reason", "launch_status", "result", "job_name", "target_release", "self_update")}}
        try:
            entry["tree"] = process_capture.tree_snapshot()
        except Exception:
            entry["tree"] = {"status": "unavailable"}
        job = (body or {}).get("job_name") if isinstance(body, dict) else None
        if isinstance(job, str) and _JOB_NAME.fullmatch(job):
            seen["job"] = job
        job = seen["job"]
        if job is not None:
            try:
                entry["scheduled_task"] = _scheduled_task_state(job)
            except Exception:
                entry["scheduled_task"] = {"state": "unavailable"}
            if final:
                _keep_job_log(Path(evidence), Path(root), job)
        with samples.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(entry, sort_keys=True, default=str) + "\n")

    return tick


def _keep_job_log(evidence, root, job):
    source = root / "logs" / f"{job}.log"
    try:
        if source.is_symlink() or not source.is_file():
            return
        with source.open("rb") as stream:
            stream.seek(max(0, source.stat().st_size - WAIT_LOG_LIMIT))
            (evidence / "windows-update-job.log").write_bytes(stream.read(WAIT_LOG_LIMIT))
    except OSError:
        pass  # Keep what can be kept; the failure stands either way.


def capture_update_scheduler_events(evidence, started):
    """Keep only structured events for the owned task since update began.

    Task history may be disabled or inaccessible. Never retain event messages,
    action paths, user names, or command output from the machine-wide log.
    """
    stamp = started.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    script = r'''$ErrorActionPreference = 'Stop'
$log = 'Microsoft-Windows-TaskScheduler/Operational'
try {
  if (-not (Get-WinEvent -ListLog $log).IsEnabled) {
    @{status='disabled'; events=@()} | ConvertTo-Json -Depth 4 -Compress; exit 0
  }
  $query = "*[System[TimeCreated[@SystemTime >= 'START_TIME']] and EventData[Data[@Name='TaskName']='\Astrid Runtime']]"
  try { $events = @(Get-WinEvent -LogName $log -FilterXPath $query -MaxEvents 64 -ErrorAction Stop) }
  catch {
    if ($_.FullyQualifiedErrorId -like 'NoMatchingEventsFound*') { $events = @() }
    else { throw }
  }
  $rows = @($events | ForEach-Object {
    $e = $_; [xml]$xml = $e.ToXml()
    $result = @($xml.Event.EventData.Data | Where-Object { $_.Name -eq 'ResultCode' })
    $instance = @($xml.Event.EventData.Data | Where-Object { $_.Name -eq 'TaskInstanceId' })
    [pscustomobject]@{id=$e.Id; time=$e.TimeCreated.ToUniversalTime().ToString('o');
      result=if ($result.Count -eq 1) { $result[0].'#text' } else { $null };
      instance=if ($instance.Count -eq 1) { $instance[0].'#text' } else { $null }}
  })
  ConvertTo-Json -InputObject @{status='observed'; events=$rows} -Depth 4 -Compress
} catch { @{status='unavailable'; events=@()} | ConvertTo-Json -Depth 4 -Compress }
'''.replace("START_TIME", stamp)
    record = {"schema_version": 1, "status": "unavailable", "started_at_utc": stamp,
              "event_limit_reached": False, "events": [],
              "limitation": "Task history may be disabled, incomplete, or overwritten; absence is inconclusive."}
    try:
        completed = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                                   capture_output=True, text=True, timeout=30, check=False)
        value = json.loads(completed.stdout) if completed.returncode == 0 else {}
        if isinstance(value, dict) and value.get("status") in {"disabled", "unavailable", "observed"}:
            record["status"] = value["status"]
            if value["status"] == "observed" and isinstance(value.get("events"), list):
                record["event_limit_reached"] = len(value["events"]) >= 64
                for row in value["events"][:64]:
                    if not isinstance(row, dict) or type(row.get("id")) is not int or not 0 <= row["id"] <= 65535:
                        continue
                    when = row.get("time")
                    if not isinstance(when, str) or not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{1,7}Z", when):
                        continue
                    result = row.get("result")
                    result = int(result) if isinstance(result, str) and re.fullmatch(r"\d{1,10}", result) and int(result) <= 0xffffffff else None
                    try:
                        instance = str(uuid.UUID(row.get("instance")))
                    except (ValueError, TypeError, AttributeError):
                        instance = None
                    record["events"].append({"id": row["id"], "time": when, "result_code": result, "instance_id": instance})
    except (OSError, subprocess.SubprocessError, ValueError, TypeError):
        pass
    (evidence / SCHEDULER_EVENTS).write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")


def make_standin_release(candidate, *, marker=MARKER):
    """Piece A's make_standin_release interface, pending integration."""
    if not isinstance(candidate, Mapping):
        raise TypeError("candidate manifest must be a mapping")
    release = candidate.get("release")
    if isinstance(release, bool) or not isinstance(release, int) or release < 0:
        raise ValueError("candidate release must be a non-negative integer")
    if not isinstance(marker, str) or not marker.strip():
        raise ValueError("marker must be a non-empty string")
    value = deepcopy(dict(candidate))
    value.update(release=release + 1, qualification_marker=marker)
    value["vibecomfy"] = candidate.get("vibecomfy", {"sha": VIBECOMFY_ENGINE_REVISION})
    return value


def canonical_manifest_bytes(manifest):
    return (json.dumps(dict(manifest), indent=2, sort_keys=True) + "\n").encode("utf-8")


def task_labels():
    output = windows.powershell(
        "ConvertTo-Json -Compress -InputObject @(@(Get-ScheduledTask | Where-Object { $_.TaskName -like '*Astrid*' }) | "
        "ForEach-Object { [pscustomobject]@{name=$_.TaskName; path=$_.TaskPath} })",
        "update-task-labels.log", show=False, timeout=30)
    value = json.loads(output)
    if not isinstance(value, list):
        raise RuntimeError("Scheduled task label enumeration is malformed")
    return sorted((item["path"], item["name"]) for item in value)


def service_names():
    output = windows.powershell(
        "ConvertTo-Json -Compress -InputObject @(@(Get-Service | Where-Object { "
        "$_.Name -like '*Astrid*' -or $_.DisplayName -like '*Astrid*' }) | "
        "ForEach-Object { $_.Name })", "update-service-labels.log", show=False, timeout=30)
    value = json.loads(output)
    if not isinstance(value, list) or any(not isinstance(name, str) for name in value):
        raise RuntimeError("Windows service label enumeration is malformed")
    return sorted(value)


def assert_running_task():
    state = windows.powershell(
        "(Get-ScheduledTask -TaskName 'Astrid Runtime' -TaskPath '\\' -ErrorAction Stop).State.ToString()",
        "update-task-state.log", show=False, timeout=30).strip()
    if state != "Running":
        raise RuntimeError("Astrid Runtime scheduled task is not running")
    return state


def capture_stop_task_state(evidence, phase, state=None):
    """Record observed task state around update without changing update outcome."""
    path = evidence / STOP_TASK_STATE
    record = {"schema_version": 1, "stop_outcome": {"status": "not_observed", "value": None},
              "task_states": {}}
    try:
        prior = json.loads(path.read_text(encoding="utf-8"))
        if (isinstance(prior, dict) and prior.get("schema_version") == 1 and
                isinstance(prior.get("task_states"), dict)):
            record = prior
    except (OSError, ValueError, TypeError):
        pass
    observation = {"status": "unavailable"}
    if state is None:
        try:
            output = windows.powershell(
                "(Get-ScheduledTask -TaskName 'Astrid Runtime' -TaskPath '\\' -ErrorAction Stop).State.ToString()",
                "update-task-state.log", show=False, timeout=30,
            ).strip()
            if output in {"Unknown", "Disabled", "Queued", "Ready", "Running"}:
                state = output
            else:
                observation = {"status": "malformed"}
        except Exception as exc:
            observation = {"status": "error", "error": type(exc).__name__}
    if state is not None:
        observation = {"status": "observed", "state": state}
    record["task_states"][phase] = observation
    try:
        path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    except OSError:
        pass


def assert_no_boundary(journey, root, data, boundary):
    selection = journey.read_json(journey.selection_path())
    marker = journey.read_json(root / "install.json")
    if (any(os.path.lexists(p) for p in boundary) or marker.get("r1_data_root")
            or selection.get("r1_data_root") or os.path.lexists(Path(str(data) + "-r1"))):
        raise RuntimeError("Unexpected R0 boundary handoff state")


def wait_for_labels(labels, services, timeout=120, interval=2):
    """The update worker's own transient task is removed shortly after the result is recorded."""
    deadline = time.monotonic() + timeout
    while task_labels() != labels or service_names() != services:
        if time.monotonic() > deadline:
            raise RuntimeError("Update changed Astrid task or service labels")
        time.sleep(interval)


def run_update(journey, args, app, manifest, root, data, evidence, sentinel, birth, project, marker, digest):
    baseline = manifest.get("release")
    if isinstance(baseline, bool) or not isinstance(baseline, int) or baseline < 0:
        raise RuntimeError("Windows update requires a candidate with an integer release")
    old_path, _, old = journey.installation(root, data)
    if old.get("identity") != {"release": baseline, "managed_by": None}:
        raise RuntimeError(f"Windows update baseline is not direct release {baseline}")
    old_id = old_path.parent.name
    before_versions = {p.name for p in (root / "versions").iterdir()}
    before_health = journey.healthy(data)
    before_runtime_instance_id = before_health["runtime_instance_id"]
    journey.assert_direct_service_ownership(root, data, release=0)
    task_state_before = assert_running_task()
    capture_stop_task_state(evidence, "before", task_state_before)
    labels_before = task_labels()
    services_before = service_names()
    if ("\\", "Astrid Runtime") not in labels_before:
        raise RuntimeError("Owned Astrid Runtime task is absent")
    state_dir = Path(os.environ["LOCALAPPDATA"]) / "Astrid Installer"
    boundary = [state_dir / name for name in ("astrid-r0-upgrade-in-progress.json", "astrid-r1-handoff.json", "astrid-r1-registration.json")]
    assert_no_boundary(journey, root, data, boundary)
    journey.verify("observe", old_path, data, evidence)
    standin = make_standin_release(manifest)
    raw = canonical_manifest_bytes(standin)
    manifest_path = evidence / "windows-update-manifest.json"
    manifest_path.write_bytes(raw)
    sha = hashlib.sha256(raw).hexdigest()
    (evidence / "windows-update-SHA256SUMS").write_text(f"{sha}  {manifest_path.name}\n", encoding="utf-8")
    try:
        capture_pid = process_capture.host_pid(data)
        capture_before = process_capture.observe()
    except Exception:
        capture_pid, capture_before = None, None
    started = datetime.now(timezone.utc)
    # The direct Windows update path has its own lifecycle helper; keep the
    # bounded #277 pack-host snapshots around this actual update as well.
    # These are diagnostics only and must never affect the update guard/result.
    try:
        journey._capture_windows_pack_host_update_state(evidence, data, "before")
    except Exception:
        pass
    try:
        # The hosted page's own request: POST through the Runtime door, with the stand-in
        # release served from the intercepted astrid.haus. No bootstrap is run by the journey.
        hosted_record = hosted.run_hosted_update(
            python=old["python"], app=app, manifest=raw, door_base=journey.HEALTH_URL,
            root=root, evidence=evidence, tick=make_wait_observer(evidence, root))
    except Exception:
        try:
            if capture_before is not None:
                process_capture.capture_failure(evidence, capture_pid, capture_before, process_capture.observe())
        except Exception:
            pass  # Diagnostics must never replace the update failure.
        try:
            capture_update_scheduler_events(evidence, started)
        except Exception:
            pass  # Diagnostics must never replace the update failure.
        raise
    finally:
        try:
            journey._capture_windows_pack_host_update_state(evidence, data, "after")
        except Exception:
            pass  # Diagnostics must never replace the update result.
        capture_stop_task_state(evidence, "after")
    code = 0
    version_path, selected_data, version = journey.installation(root, data)
    new_id = version_path.parent.name
    if selected_data != data or not old_path.is_file():
        raise RuntimeError("Update changed the data root or discarded the prior version")
    if new_id == old_id or new_id in before_versions:
        raise RuntimeError("Update did not select a new version")
    installed_raw = (version_path.parent / "manifest.json").read_bytes()
    if installed_raw != raw:
        raise RuntimeError("Installed stand-in manifest differs from canonical candidate")
    if version.get("identity") != {"release": baseline + 1, "managed_by": None}:
        raise RuntimeError("Update selected the wrong release identity")
    wait_for_labels(labels_before, services_before)
    assert_no_boundary(journey, root, data, boundary)
    journey.assert_direct_service_ownership(root, data, release=0)
    journey.wait_ready(data, timeout=int(os.environ.get("ASTRID_CI_WAIT_READY_TIMEOUT", "900")))
    health = journey.healthy(data)
    if health["runtime_instance_id"] == before_runtime_instance_id:
        raise RuntimeError("Update did not restart the Runtime")
    if health["identity"]["release"] != baseline + 1:
        raise RuntimeError(f"Post-update Runtime health is not release {baseline + 1}")
    assert_running_task()
    journey.assert_content(version, evidence, project, marker, digest)
    if not journey.sentinel_observation(sentinel, birth)["alive"]:
        raise RuntimeError("Unrelated sentinel did not survive update")
    checks = {key: "pass" for key in ("running_before", "owned_task", "healthy", "same_labels", "no_r0_handoff", "workspace", "sentinel", "prior_version")}
    receipt = journey.verify("update", version_path, data, evidence, before=evidence / "verify-observe.json")
    if journey.read_json(receipt).get("passed") is not True:
        raise RuntimeError("verify-update did not pass")
    windows.wait_for("updated pack host ready", windows.host_ready)
    task_id = windows.task(project, create_project=False)
    if not journey.sentinel_observation(sentinel, birth)["alive"]:
        raise RuntimeError("Unrelated sentinel did not survive the updated task")
    checks.update(verify_update="pass", task_settled="pass")
    return {"outcome": "updated", "from_release": baseline, "to_release": baseline + 1, "hosted": hosted_record,
            "marker": MARKER, "manifest_sha256": sha, "old_version": old_id, "current_version": new_id,
            "task_labels": [{"path": path, "name": name} for path, name in labels_before],
            "service_names": services_before,
            "exit_code": code, "refusal": None, "task_id": task_id, "checks": checks}
