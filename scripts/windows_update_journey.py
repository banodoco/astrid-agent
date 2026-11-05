"""Same-label Windows fresh-install update qualification."""
from __future__ import annotations

from copy import deepcopy
from collections.abc import Mapping
import hashlib
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if __package__:
    from . import ci_windows_journey as windows
else:
    import ci_windows_journey as windows
from astrid.core.generation.vibecomfy_dependency import VIBECOMFY_ENGINE_REVISION

MARKER = "M-RT UPDATE TEST STAND-IN"


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


def assert_no_boundary(journey, root, data, boundary):
    selection = journey.read_json(Path(os.environ["LOCALAPPDATA"]) / "astrid-install.json")
    marker = journey.read_json(root / "install.json")
    if (any(os.path.lexists(p) for p in boundary) or marker.get("r1_data_root")
            or selection.get("r1_data_root") or os.path.lexists(Path(str(data) + "-r1"))):
        raise RuntimeError("Unexpected R0 boundary handoff state")


def run_update(journey, args, app, manifest, root, data, evidence, sentinel, birth, project, marker, digest):
    if manifest.get("release") != 1:
        raise RuntimeError("Windows update requires a release-1 candidate")
    old_path, _, old = journey.installation(root, data)
    if old.get("identity") != {"release": 1, "managed_by": None}:
        raise RuntimeError("Windows update baseline is not direct release 1")
    old_id = old_path.parent.name
    before_versions = {p.name for p in (root / "versions").iterdir()}
    before_health = journey.healthy(data)
    before_runtime_instance_id = before_health["runtime_instance_id"]
    journey.assert_direct_service_ownership(root, data, release=0)
    assert_running_task()
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
    journey.bootstrap(app, manifest_path, args.shell, "update", evidence, root, data)
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
    if version.get("identity") != {"release": 2, "managed_by": None}:
        raise RuntimeError("Update selected the wrong release identity")
    if task_labels() != labels_before or service_names() != services_before:
        raise RuntimeError("Update changed Astrid task or service labels")
    assert_no_boundary(journey, root, data, boundary)
    journey.assert_direct_service_ownership(root, data, release=0)
    journey.wait_ready(data, timeout=int(os.environ.get("ASTRID_CI_WAIT_READY_TIMEOUT", "900")))
    health = journey.healthy(data)
    if health["runtime_instance_id"] == before_runtime_instance_id:
        raise RuntimeError("Update did not restart the Runtime")
    if health["identity"]["release"] != 2:
        raise RuntimeError("Post-update Runtime health is not release 2")
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
    return {"outcome": "updated", "from_release": 1, "to_release": 2,
            "marker": MARKER, "manifest_sha256": sha, "old_version": old_id, "current_version": new_id,
            "task_labels": [{"path": path, "name": name} for path, name in labels_before],
            "service_names": services_before,
            "exit_code": code, "refusal": None, "task_id": task_id, "checks": checks}
