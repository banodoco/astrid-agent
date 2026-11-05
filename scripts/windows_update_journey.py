"""Same-label Windows fresh-install update qualification."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
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


def refusal_sentence(output):
    """Accept only a single explicit lock-related refusal, never a traceback."""
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if any("traceback" in line.lower() for line in lines):
        return None
    matches = [line for line in lines if re.fullmatch(
        r"(?:Error: )?[^.!?\r\n]*(?:locked|in use|being used by another process)[^.!?\r\n]*[.!?]",
        line, re.IGNORECASE)]
    return matches[0] if len(matches) == 1 else None


def public_update(app, shell, manifest_path, root, data, evidence):
    # Execute the documented public line; bind its downloaded script to the
    # frozen App checkout so a changed deployment cannot qualify this candidate.
    script = ("$ErrorActionPreference='Stop'; "
              "$s=irm https://astrid.haus/install.ps1; "
              "$expected=[IO.File]::ReadAllText($env:ASTRID_CI_EXPECTED_BOOTSTRAP); "
              "if ($s -cne $expected) { throw 'Public bootstrap differs from candidate' }; "
              "& ([scriptblock]::Create($s)) --update --root $env:ASTRID_CI_PROGRAM_ROOT "
              "--data-root $env:ASTRID_CI_DATA_ROOT; if ($LASTEXITCODE) { exit $LASTEXITCODE }")
    env = {k: v for k, v in os.environ.items() if k.casefold() != "psmodulepath"}
    env.update(ASTRID_INSTALL_MANIFEST_URL=manifest_path.resolve().as_uri(),
               ASTRID_CI_EXPECTED_BOOTSTRAP=str(app / "public" / "install.ps1"),
               ASTRID_CI_PROGRAM_ROOT=str(root), ASTRID_CI_DATA_ROOT=str(data),
               UV_LINK_MODE="copy", BROWSER=f'"{sys.executable}" -c "pass" %s')
    log = evidence / "update-bootstrap.log"
    with log.open("w", encoding="utf-8") as stream:
        completed = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                                    "-Command", script], stdout=stream, stderr=subprocess.STDOUT,
                                   env=env, timeout=3600, check=False)
    return completed.returncode, log.read_text(encoding="utf-8-sig", errors="replace")


def run_update(journey, args, app, manifest, root, data, evidence, sentinel, birth, project, marker, digest):
    if manifest.get("release") != 1:
        raise RuntimeError("Windows update requires a release-1 candidate")
    old_path, _, old = journey.installation(root, data)
    if old.get("identity") != {"release": 1, "managed_by": None}:
        raise RuntimeError("Windows update baseline is not direct release 1")
    old_id = old_path.parent.name
    before_versions = {p.name for p in (root / "versions").iterdir()}
    journey.healthy(data)
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
    code, output = public_update(app, args.shell, manifest_path, root, data, evidence)
    refusal = None if code == 0 else refusal_sentence(output)
    if code and refusal is None:
        raise RuntimeError(f"Windows update failed without a clean lock refusal (exit {code}); see {evidence / 'update-bootstrap.log'}")
    version_path, selected_data, version = journey.installation(root, data)
    new_id = version_path.parent.name
    if selected_data != data or not old_path.is_file():
        raise RuntimeError("Update changed the data root or discarded the prior version")
    if code == 0:
        if new_id == old_id or new_id in before_versions:
            raise RuntimeError("Update did not select a new version")
        installed_raw = (version_path.parent / "manifest.json").read_bytes()
        if installed_raw != raw:
            raise RuntimeError("Installed stand-in manifest differs from canonical candidate")
        if version.get("identity") != {"release": 2, "managed_by": None}:
            raise RuntimeError("Update selected the wrong release identity")
    elif new_id != old_id:
        raise RuntimeError("Refused update changed current selection")
    if task_labels() != labels_before or service_names() != services_before:
        raise RuntimeError("Update changed Astrid task or service labels")
    assert_no_boundary(journey, root, data, boundary)
    journey.assert_direct_service_ownership(root, data, release=0)
    journey.wait_ready(data, timeout=int(os.environ.get("ASTRID_CI_WAIT_READY_TIMEOUT", "900")))
    assert_running_task()
    journey.assert_content(version, evidence, project, marker, digest)
    if not journey.sentinel_observation(sentinel, birth)["alive"]:
        raise RuntimeError("Unrelated sentinel did not survive update")
    checks = {key: "pass" for key in ("running_before", "owned_task", "healthy", "same_labels", "no_r0_handoff", "workspace", "sentinel", "prior_version")}
    task_id = None
    if not code:
        receipt = journey.verify("update", version_path, data, evidence, before=evidence / "verify-observe.json")
        if journey.read_json(receipt).get("passed") is not True:
            raise RuntimeError("verify-update did not pass")
        windows.wait_for("updated pack host ready", windows.host_ready)
        task_id = windows.task(project, create_project=False)
        if not journey.sentinel_observation(sentinel, birth)["alive"]:
            raise RuntimeError("Unrelated sentinel did not survive the updated task")
        checks.update(verify_update="pass", task_settled="pass")
    return {"outcome": "refused" if code else "updated", "from_release": 1, "to_release": 2,
            "marker": MARKER, "manifest_sha256": sha, "old_version": old_id, "current_version": new_id,
            "task_labels": [{"path": path, "name": name} for path, name in labels_before],
            "service_names": services_before,
            "exit_code": code, "refusal": refusal, "task_id": task_id, "checks": checks}
