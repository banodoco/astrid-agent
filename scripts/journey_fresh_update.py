"""R1 fresh-install update through the hosted page's Update request, within the journey-owned scope."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

if __package__:
    from . import ci_journey as journey
    from . import journey_hosted_update as hosted
    from .journey_matrix import HOSTED_BUTTON_JOB_IDS
else:
    import ci_journey as journey
    import journey_hosted_update as hosted
    from journey_matrix import HOSTED_BUTTON_JOB_IDS


DIST_ENV = "ASTRID_CI_HOSTED_APP_DIST"


def hosted_dist(job_id) -> Path | None:
    """The built hosted App for a button row (required there); None for a row that sends the request itself."""
    if job_id not in HOSTED_BUTTON_JOB_IDS:
        return None
    value = os.environ.get(DIST_ENV, "").strip()
    if not value or not (Path(value) / "index.html").is_file():
        raise RuntimeError(f"{job_id} clicks the hosted App's Update now and needs its build in {DIST_ENV}")
    return Path(value)


CHECKS = ("new_version", "old_version_retained", "same_service", "no_boundary_handoff",
          "health", "workspace_content", "unrelated_sentinel", "verify_update", "task_settled")


def candidate_release(candidate: dict) -> int:
    """The release the fresh install lands on; the stand-in is always the next one."""
    release = candidate.get("release")
    if type(release) is not int or release < 0:
        raise RuntimeError("Fresh update requires a candidate with an integer release")
    return release


def standin(candidate: dict, installed: dict, directory: Path) -> tuple[Path, dict]:
    """Temporary fixture implementation of piece A's canonical manifest contract."""
    candidate_release(candidate)
    manifest = dict(candidate)
    # The installer adds the effective vibecomfy pin when older App manifests
    # omit it; use the pin actually selected by this installation.
    if "vibecomfy" not in manifest:
        manifest["vibecomfy"] = installed["vibecomfy"]
    manifest.update(release=candidate["release"] + 1,
                    qualification_marker="fresh-install-update-stand-in")
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "install-manifest.json"
    raw = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")
    path.write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    (directory / "SHA256SUMS").write_text(f"{digest}  install-manifest.json\n", encoding="utf-8")
    return path, manifest


def service_names(data: Path) -> list[str]:
    home = data.parent / "home"
    if sys.platform == "linux":
        directory, pattern = home / ".config/systemd/user", "astrid*.service"
    elif sys.platform == "darwin":
        directory, pattern = home / "Library/LaunchAgents", "haus.astrid.runtime*.plist"
    else:
        raise RuntimeError("Fresh update is only implemented on Linux and macOS")
    return sorted(path.name for path in directory.glob(pattern))


def active_service(root: Path, data: Path) -> dict:
    service = journey.assert_direct_service_ownership(root, data, release=0)
    if sys.platform == "darwin":
        domain = f"gui/{os.getuid()}"
        current = subprocess.run(["launchctl", "print", f"{domain}/haus.astrid.runtime"],
                                 capture_output=True, text=True, timeout=30, check=False)
        if current.returncode or not re.search(r"(?m)^\s*state = running\s*$", current.stdout):
            raise RuntimeError("Fresh service is not running under its original label")
        alternate = subprocess.run(["launchctl", "print", f"{domain}/haus.astrid.runtime.r1"],
                                   capture_output=True, text=True, timeout=30, check=False)
        if alternate.returncode == 0 or "Could not find service" not in (alternate.stdout + alternate.stderr):
            raise RuntimeError("Unexpected R1 boundary service is registered")
    elif sys.platform == "linux":
        alternate = subprocess.run(["systemctl", "--user", "show", "astrid-r1.service",
                                    "--property=LoadState,ActiveState"],
                                   capture_output=True, text=True, timeout=30, check=False)
        fields = dict(line.split("=", 1) for line in alternate.stdout.splitlines() if "=" in line)
        if alternate.returncode or fields.get("LoadState") != "not-found" or fields.get("ActiveState") != "inactive":
            raise RuntimeError("Unexpected R1 boundary service is registered")
    return service


# The boundary state of an install that never met the R0 handoff.
NO_BOUNDARY = {"journals": [], "r1_data_root": None, "selection_r1_data_root": None, "r1_data_exists": False}


def is_handed_off(state: dict, data: Path) -> bool:
    """Whether ``state`` is what a completed R0 to R1 storage handoff leaves behind.

    R0's data stays at ``data`` and the active copy is ``data-r1``. This is the
    state real pre-R1 installs are in, so a leg may start from it on purpose.
    """
    r1 = str(Path(str(data) + "-r1"))
    return ("astrid-r1-handoff.json" in state.get("journals", [])
            and state.get("r1_data_root") == r1 and state.get("selection_r1_data_root") == r1
            and state.get("r1_data_exists") is True)


def boundary_state(root: Path, data: Path) -> dict:
    parent = journey.program_root().parent
    names = ("astrid-r0-upgrade-in-progress.json", "astrid-r1-handoff.json",
             "astrid-r1-registration.json")
    marker = journey.read_json(root / "install.json")
    selection = journey.read_json(journey.selection_path())
    return {"journals": [name for name in names if os.path.lexists(parent / name)],
            "r1_data_root": marker.get("r1_data_root"),
            "selection_r1_data_root": selection.get("r1_data_root"),
            "r1_data_exists": os.path.lexists(Path(str(data) + "-r1"))}


def assert_transition(root: Path, old_path: Path, new_path: Path, versions_before: set[str],
                      old_service: dict, new_service: dict, names_before: list[str],
                      names_after: list[str], boundary_before: dict, boundary_after: dict,
                      *, expected_boundary: dict = NO_BOUNDARY) -> dict:
    old_id, new_id = old_path.parent.name, new_path.parent.name
    versions_after = {path.name for path in (root / "versions").iterdir() if path.is_dir()}
    if (old_path.parent.parent != root / "versions" or new_path.parent.parent != root / "versions"
            or new_id == old_id or new_id in versions_before or new_id not in versions_after
            or (root / "current").read_text(encoding="utf-8").strip() != new_id):
        raise RuntimeError("Update did not create and select a new version")
    if old_id not in versions_after or not old_path.is_file():
        raise RuntimeError("Update did not retain the old version")
    if (old_service != new_service or not names_before or names_before != names_after):
        raise RuntimeError("Update changed the service label or added a service")
    if boundary_before != expected_boundary or boundary_after != expected_boundary:
        raise RuntimeError("Update changed the R0 boundary handoff state it started from")
    return {"old_version": old_id, "new_version": new_id,
            "service": new_service, "service_names": names_after}


def execute(args, root: Path, data: Path, evidence: Path, app: Path, manifest: dict,
            version_path: Path, version: dict, project: str, marker: Path, digest: str,
            sentinel, birth: str, result: dict, *, expected_boundary: dict = NO_BOUNDARY) -> None:
    """Update a fresh install through the hosted request.

    ``expected_boundary`` is the R0 handoff state the install starts in. The
    fresh rows start from none; it is a parameter, not a precondition, so a
    handed-off install is a start state rather than a forbidden one.
    """
    update = result["update"] = {"status": "not-run", "checks": {key: "not-run" for key in CHECKS}}
    try:
        dist = hosted_dist(getattr(args, "job_id", None))
        installed = journey.read_json(Path(version["root"]) / "manifest.json")
        baseline = candidate_release(manifest)
        if version.get("identity") != {"release": baseline, "managed_by": None}:
            raise RuntimeError(f"Fresh update baseline is not direct release {baseline}")
        path, target = standin(manifest, installed, evidence / "fresh-update-standin")
        before = journey.verify("observe", version_path, data, evidence)
        versions_before = {p.name for p in (root / "versions").iterdir() if p.is_dir()}
        service_before = active_service(root, data)
        names_before = service_names(data)
        boundary_before = boundary_state(root, data)
        if boundary_before != expected_boundary:
            raise RuntimeError("Update baseline is not the expected R0 boundary handoff state")
        before_health = journey.healthy(data)
        before_runtime_instance_id = before_health["runtime_instance_id"]
        # The hosted page's own request: POST through the Runtime door, with the stand-in
        # release served from the intercepted astrid.haus. No bootstrap is run by the journey.
        # On a button row the page itself is served there too, and a browser clicks its Update now.
        update["hosted"] = hosted.run_hosted_update(
            python=version["python"], app=app, manifest=path.read_bytes(),
            door_base=journey.HEALTH_URL, root=root, evidence=evidence, dist=dist)
        journey.wait_ready(data, timeout=int(os.environ.get("ASTRID_CI_WAIT_READY_TIMEOUT", "900")))
        new_path, new_data, new_version = journey.installation(root, data)
        if new_data != data or new_version.get("identity") != {"release": baseline + 1, "managed_by": None}:
            raise RuntimeError("Update selected the wrong data root or release")
        journey.selected_tuple(target, new_version)
        installed_bytes = (Path(new_version["root"]) / "manifest.json").read_bytes()
        if installed_bytes != path.read_bytes():
            raise RuntimeError("Installed stand-in manifest bytes differ from the selected manifest")
        service_after = active_service(root, data)
        update.update(assert_transition(root, version_path, new_path, versions_before, service_before,
                                        service_after, names_before, service_names(data), boundary_before,
                                        boundary_state(root, data), expected_boundary=expected_boundary))
        update["checks"].update(new_version="pass", old_version_retained="pass", same_service="pass",
                                no_boundary_handoff="pass")
        health = journey.healthy(data)
        if health["runtime_instance_id"] == before_runtime_instance_id:
            raise RuntimeError("Update did not restart the Runtime")
        if health["identity"]["release"] != baseline + 1:
            raise RuntimeError(f"Post-update Runtime health is not release {baseline + 1}")
        update["checks"]["health"] = "pass"
        journey.assert_content(new_version, evidence, project, marker, digest)
        update["checks"]["workspace_content"] = "pass"
        if not journey.sentinel_alive(sentinel, birth):
            raise RuntimeError("Unrelated sentinel did not survive the fresh update")
        update["checks"]["unrelated_sentinel"] = "pass"
        journey.verify("update", new_path, data, evidence, before=before)
        update["checks"]["verify_update"] = "pass"
        task_evidence = evidence / "fresh-update-task"
        task_evidence.mkdir(parents=True, exist_ok=True)
        journey.task(new_version, task_evidence, project, create_project=False)
        if not journey.sentinel_alive(sentinel, birth):
            raise RuntimeError("Unrelated sentinel did not survive the post-update task")
        update["checks"]["task_settled"] = "pass"
        update.update(status="pass", release_from=baseline, release_to=baseline + 1,
                      manifest_sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    except Exception as exc:
        update.update(status="fail", error=f"{type(exc).__name__}: {exc}")
        raise
