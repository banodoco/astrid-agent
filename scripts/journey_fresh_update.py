"""R1 fresh-install public install-line update, within the journey-owned scope."""
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
else:
    import ci_journey as journey


CHECKS = ("new_version", "old_version_retained", "same_service", "no_boundary_handoff",
          "health", "workspace_content", "unrelated_sentinel", "verify_update", "task_settled")


def standin(candidate: dict, installed: dict, directory: Path) -> tuple[Path, dict]:
    """Temporary fixture implementation of piece A's canonical manifest contract."""
    if type(candidate.get("release")) is not int or candidate["release"] != 1:
        raise RuntimeError("Fresh update requires a release-1 candidate")
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


def boundary_state(root: Path, data: Path) -> dict:
    parent = journey.program_root().parent
    names = ("astrid-r0-upgrade-in-progress.json", "astrid-r1-handoff.json",
             "astrid-r1-registration.json")
    marker = journey.read_json(root / "install.json")
    selection = journey.read_json(parent / "astrid-install.json")
    return {"journals": [name for name in names if os.path.lexists(parent / name)],
            "r1_data_root": marker.get("r1_data_root"),
            "selection_r1_data_root": selection.get("r1_data_root"),
            "r1_data_exists": os.path.lexists(Path(str(data) + "-r1"))}


def assert_transition(root: Path, old_path: Path, new_path: Path, versions_before: set[str],
                      old_service: dict, new_service: dict, names_before: list[str],
                      names_after: list[str], boundary_before: dict, boundary_after: dict) -> dict:
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
    empty = {"journals": [], "r1_data_root": None, "selection_r1_data_root": None, "r1_data_exists": False}
    if boundary_before != empty or boundary_after != empty:
        raise RuntimeError("Fresh update entered the R0 boundary handoff")
    return {"old_version": old_id, "new_version": new_id,
            "service": new_service, "service_names": names_after}


def execute(args, root: Path, data: Path, evidence: Path, app: Path, manifest: dict,
            version_path: Path, version: dict, project: str, marker: Path, digest: str,
            sentinel, birth: str, result: dict) -> None:
    update = result["update"] = {"status": "not-run", "checks": {key: "not-run" for key in CHECKS}}
    try:
        installed = journey.read_json(Path(version["root"]) / "manifest.json")
        if version.get("identity") != {"release": 1, "managed_by": None}:
            raise RuntimeError("Fresh update baseline is not direct release 1")
        path, target = standin(manifest, installed, evidence / "fresh-update-standin")
        before = journey.verify("observe", version_path, data, evidence)
        versions_before = {p.name for p in (root / "versions").iterdir() if p.is_dir()}
        service_before = active_service(root, data)
        names_before = service_names(data)
        boundary_before = boundary_state(root, data)
        if boundary_before != {"journals": [], "r1_data_root": None, "selection_r1_data_root": None, "r1_data_exists": False}:
            raise RuntimeError("Fresh update baseline has R0 boundary handoff state")
        journey.healthy(data)
        # This is the same public bootstrap and --update invocation as the R0 journey.
        journey.bootstrap(app, path, args.shell, "update", evidence, root, data)
        journey.wait_ready(data, timeout=int(os.environ.get("ASTRID_CI_WAIT_READY_TIMEOUT", "900")))
        new_path, new_data, new_version = journey.installation(root, data)
        if new_data != data or new_version.get("identity") != {"release": 2, "managed_by": None}:
            raise RuntimeError("Update selected the wrong data root or release")
        journey.selected_tuple(target, new_version)
        installed_bytes = (Path(new_version["root"]) / "manifest.json").read_bytes()
        if installed_bytes != path.read_bytes():
            raise RuntimeError("Installed stand-in manifest bytes differ from the selected manifest")
        service_after = active_service(root, data)
        update.update(assert_transition(root, version_path, new_path, versions_before, service_before,
                                        service_after, names_before, service_names(data), boundary_before,
                                        boundary_state(root, data)))
        update["checks"].update(new_version="pass", old_version_retained="pass", same_service="pass",
                                no_boundary_handoff="pass")
        journey.healthy(data)
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
        update.update(status="pass", release_from=1, release_to=2,
                      manifest_sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    except Exception as exc:
        update.update(status="fail", error=f"{type(exc).__name__}: {exc}")
        raise
