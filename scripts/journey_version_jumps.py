"""C1 named legs inside ci_journey's scope, environment, sentinel and cleanup.

Call only through ci_journey.execute: this module does not admit lifecycle roots.
"""
from __future__ import annotations

import hashlib
import html
import json
import os
from pathlib import Path
import platform
import re
import secrets
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request

if __package__:
    from . import check_journey_evidence as evidence_gate
    from . import ci_journey as journey, upgrade_release, journey_workspace, journey_live_workspace as live
    from . import journey_data_roots
else:
    import check_journey_evidence as evidence_gate
    import ci_journey as journey
    import upgrade_release
    import journey_workspace
    import journey_live_workspace as live
    import journey_data_roots


RELEASE0_FIXTURE = journey.ROOT / "tests" / "fixtures" / "release0"
RELEASE0_SUMS_SHA256 = "58221a50aa7940e3121d6239ddeb92d6409df3711096aac1161c7ec130b755c0"
RELEASE0_REFS = {
    "agent": "3204bb00135bec8ee2d2d3fb21eed5abfceca649",
    "runtime": "6d53a19070faf63fc15ae1f81417722edc2c8a5f",
}
RELEASE0_REQUIRED = frozenset({
    "provenance.json", "served/install", "served/install.ps1", "served/install-manifest.json",
})
# The published Release 9 is the starting install of the release9-then-install
# leg and the installer that performs the R0 handoff in r0-handoff-then-update.
# Its bootstrap and manifest come from the App commit that pinned it.
RELEASE9_APP_COMMIT = "546c91b7aaf90835445136eb6e93459385ffe21c"
RELEASE9_RELEASE = 9
RELEASE9_IDENTITY = {
    "agent": "709492fa5f4850b9caf0cea4a4c543023b73fa0a",
    "runtime": "f4c7a4623c84da077daac3e3a9366fc2e6a55051",
    "schema_digest": "sha256:9628bb5fd6e1dbbec392dd39f2e01031622dcf52d7ccff7cd1f0ad10743dcb1c",
}
# The served Release 11 is the starting install of the release11-then-update leg.
# Its manifest and bootstrap are the git blobs at its pinned App commit, and the
# candidate's install --update must take that install to Release 12.
RELEASE11_LEG = "release11-then-update"
RELEASE11_APP_COMMIT = "57e200b45615a59181920192697a932400d985fc"
RELEASE11_RELEASE = 11
RELEASE11_IDENTITY = {
    "agent": "a105de3a4852cb61b9647dfe2c920118bd29f272",
    "runtime": "4f5477cb0f5b03d28bec991a1fe34edcd8fc309f",
    "schema_digest": "sha256:9628bb5fd6e1dbbec392dd39f2e01031622dcf52d7ccff7cd1f0ad10743dcb1c",
}
RELEASE11_TARGET_RELEASE = 12
CHECKSUM = re.compile(r"([0-9a-f]{64})  ([A-Za-z0-9][A-Za-z0-9._/-]*)$")
DIAGNOSTIC_START_STAGE = "release0-api-seed"
DIAGNOSTIC_STATE_NAME = "release0-api-seed-preconditions.json"


def _bytes_sha256(path: Path, label: str) -> str:
    if path.is_symlink() or not path.is_file():
        raise RuntimeError(f"Diagnostic {label} must be a regular file")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _target_tuple(manifest: dict) -> dict:
    """Return the source identity that a retained release-0 state is bound to."""
    try:
        agent = manifest["agent"]["sha"]
        runtime = manifest["runtime"]["sha"]
    except (KeyError, TypeError) as exc:
        raise RuntimeError("Release-1 manifest does not contain an Agent/Runtime tuple") from exc
    if not all(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}", value)
               for value in (agent, runtime)):
        raise RuntimeError("Release-1 manifest has an invalid Agent/Runtime tuple")
    tools = manifest.get("tools", {})
    if not isinstance(tools, dict):
        raise RuntimeError("Release-1 manifest tools must be an object")
    return {
        "agent": agent,
        "runtime": runtime,
        "schema_digest": manifest["runtime"].get("schema_digest"),
        "tools": {name: row.get("version") for name, row in tools.items()
                  if isinstance(row, dict) and row.get("version")},
    }


def _candidate_sources(checkouts: dict | None) -> dict:
    if not isinstance(checkouts, dict) or set(checkouts) != {"app", "agent", "runtime", "pinokio"}:
        raise RuntimeError("Diagnostic start requires all four verified candidate checkouts")
    sources = {}
    for name, row in checkouts.items():
        sha = row.get("sha") if isinstance(row, dict) else None
        if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise RuntimeError(f"Diagnostic candidate {name} checkout has no immutable commit")
        sources[name] = sha
    return sources


def _diagnostic_state_path(args, root: Path) -> Path:
    supplied = getattr(args, "diagnostic_state", None)
    expected = root.parent / DIAGNOSTIC_STATE_NAME
    if supplied is None:
        raise ValueError("--diagnostic-state is required for a diagnostic stage start")
    path = Path(supplied)
    if path.is_symlink() or path != expected:
        raise ValueError("--diagnostic-state must be the exact run-owned release-0 precondition file")
    return path


def _preseed_workspace_state(data: Path) -> dict:
    """Return a fail-closed fingerprint of the selected release-0 realm.

    The diagnostic seed starts after the normal lifecycle has retained the
    release-0 installation.  Its checkpoint must therefore bind the actual
    workspace bytes as well as the selected program version. The R0 realm's
    root owner.lock is a live synchronization file, not workspace data: Windows
    locks its first byte, so record its presence and size without reading it.
    The journey owns
    this realm and no API operation is in flight at either call site; if it is
    nevertheless changing while it is read, decline the diagnostic rather than
    treating an uncertain state as the original pre-seed realm.
    """
    realm = journey.workspace_root(data)

    def entries() -> list[dict]:
        found = []

        def visit(directory: Path) -> None:
            # Unlike Path.rglob on some Python versions, scandir propagates
            # enumeration errors instead of silently returning a partial tree.
            with os.scandir(directory) as children:
                paths = sorted((Path(child.path) for child in children), key=lambda path: path.name)
            for path in paths:
                relative = path.relative_to(realm).as_posix()
                info = path.lstat()
                if (stat.S_ISLNK(info.st_mode)
                        or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
                    raise RuntimeError("Diagnostic pre-seed workspace contains a symlink or reparse point")
                if stat.S_ISDIR(info.st_mode):
                    found.append({"path": relative, "kind": "directory"})
                    visit(path)
                elif stat.S_ISREG(info.st_mode):
                    if relative == "owner.lock":
                        found.append({"path": relative, "kind": "live-owner-lock", "size": info.st_size})
                    else:
                        found.append({"path": relative, "kind": "file",
                                      "sha256": _bytes_sha256(path, "pre-seed workspace file")})
                else:
                    raise RuntimeError("Diagnostic pre-seed workspace contains a non-regular entry")

        visit(realm)
        found.sort(key=lambda entry: entry["path"])
        return found

    first = entries()
    second = entries()
    if first != second:
        raise RuntimeError("Diagnostic pre-seed workspace changed while checkpointing")
    return {
        "realm_root": str(realm),
        "entry_count": len(first),
        "tree_sha256": semantic_digest({"entries": first}),
    }


def diagnostic_release0_identity(args, root: Path, data: Path, *, fixture_receipt: dict,
                                 release0_manifest: Path, target_manifest: dict,
                                 candidate: dict, candidate_checkouts: dict,
                                 version_path: Path, version: dict) -> dict:
    """Build the minimum identity required to resume the release-0 seed stage.

    It intentionally binds both sides of the transition: the live selected
    release-0 version and the immutable release-1 candidate it was prepared
    to update to.  The identity is re-derived before every diagnostic action;
    the file is therefore a checkpoint, not a substitute for live validation.
    """
    owner_path = root.parent / journey.OWNER_FILE
    if owner_path.is_symlink() or not owner_path.is_file():
        raise RuntimeError("Diagnostic start has no run-owned journey identity")
    owner = journey.read_json(owner_path)
    if (owner.get("schema_version") != 1 or not isinstance(owner.get("run_id"), str)
            or not re.fullmatch(r"[0-9a-f]{32}", owner["run_id"])
            or owner.get("root") != str(root) or owner.get("data_root") != str(data)):
        raise RuntimeError("Diagnostic start journey identity is invalid")
    current_path = root / "current"
    if current_path.is_symlink() or not current_path.is_file():
        raise RuntimeError("Diagnostic start has no selected version")
    current = current_path.read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]+", current):
        raise RuntimeError("Diagnostic start selected version is invalid")
    installed_manifest = Path(version["root"]) / "manifest.json"
    release0_tuple = journey.selected_tuple(journey.read_json(release0_manifest), version)
    return {
        "schema_version": 2,
        "kind": "release0-api-seed-diagnostic-preconditions",
        "stage": DIAGNOSTIC_START_STAGE,
        "run_id": owner["run_id"],
        "roots": {"program": str(root), "data": str(data)},
        "release0_fixture": fixture_receipt,
        "release0_version": {
            "current": current,
            "version_sha256": _bytes_sha256(version_path, "version record"),
            "manifest_sha256": _bytes_sha256(installed_manifest, "installed manifest"),
            "tuple": release0_tuple,
        },
        "preseed_workspace": _preseed_workspace_state(data),
        "release1_candidate": {
            "candidate_sha256": semantic_digest(candidate),
            "manifest_sha256": candidate.get("manifest_sha256"),
            "tuple": _target_tuple(target_manifest),
            "checkouts": _candidate_sources(candidate_checkouts),
        },
    }


def write_diagnostic_release0_preconditions(args, root: Path, data: Path, *, fixture_receipt: dict,
                                            release0_manifest: Path, target_manifest: dict,
                                            candidate: dict, version_path: Path,
                                            version: dict) -> Path:
    """Checkpoint a fully validated release-0 state before the seed stage."""
    path = root.parent / DIAGNOSTIC_STATE_NAME
    if path.exists() or path.is_symlink():
        raise RuntimeError("Diagnostic release-0 precondition file already exists")
    identity = diagnostic_release0_identity(
        args, root, data, fixture_receipt=fixture_receipt,
        release0_manifest=release0_manifest, target_manifest=target_manifest,
        candidate=candidate, candidate_checkouts=getattr(args, "candidate_checkouts", None),
        version_path=version_path, version=version,
    )
    journey.write_json(path, identity)
    return path


def _diagnostic_release0_context(args, root: Path, data: Path, evidence: Path) -> tuple[dict, dict]:
    """Validate the exact retained state without invoking a lifecycle command."""
    if (getattr(args, "diagnostic_start_at", None) != DIAGNOSTIC_START_STAGE
            or args.leg != "release9-then-install" or args.mode != "direct"
            or getattr(args, "job_id", None) or getattr(args, "source_override", False)):
        raise ValueError("Diagnostic start is only admitted for the direct release-0 seed stage")
    source, release9_manifest, fixture_receipt = release9_admission(args, root.parent / "release9-source")
    del source  # Bootstrap bytes were authenticated by release9_admission above.
    app = args.app_checkout.resolve(strict=True)
    candidate_path, target_manifest = journey.selected_manifest(
        app, source_override=False, agent=None, runtime=None, evidence=evidence)
    candidate = journey.read_json(args.candidate)
    if evidence_gate.validate_tuple(candidate, app, candidate_path) != target_manifest:
        raise RuntimeError("Diagnostic start requires a frozen candidate manifest")
    candidate_release(target_manifest)
    checkouts = journey.candidate_checkouts(args)
    version_path, selected_data, version = journey.installation(root, data)
    if selected_data != data or installed_release(version) != RELEASE9_RELEASE:
        raise RuntimeError("Diagnostic start requires the retained pinned Release-9 installation")
    identity = diagnostic_release0_identity(
        args, root, data, fixture_receipt=fixture_receipt,
        release0_manifest=release9_manifest, target_manifest=target_manifest,
        candidate=candidate, candidate_checkouts=checkouts, version_path=version_path, version=version,
    )
    state_path = _diagnostic_state_path(args, root)
    if state_path.is_symlink() or not state_path.is_file():
        raise RuntimeError("Diagnostic start precondition file is absent")
    checkpoint = journey.read_json(state_path)
    if checkpoint != identity:
        if checkpoint.get("preseed_workspace") != identity["preseed_workspace"]:
            raise RuntimeError("Diagnostic start refuses a workspace changed after its pre-seed checkpoint")
        raise RuntimeError("Diagnostic start preconditions are stale or do not match the live version/tuple")
    service = journey.assert_direct_service_ownership(root, data)
    health = journey.healthy(data)
    return version, {"state_path": state_path, "identity": identity,
                     "service": service, "health": health}


def execute_diagnostic_release0_stage(args, root: Path, data: Path, evidence: Path) -> dict:
    """Run only the retained release-0 seed block; this is never qualification evidence."""
    evidence.mkdir(parents=True, exist_ok=True)
    result = {
        "schema_version": 1,
        "receipt_kind": "release1-diagnostic-stage",
        "evidence_class": "diagnostic-only",
        "qualification": {"eligible": False,
                            "reason": "stage-start diagnostics do not execute the complete lifecycle"},
        "scenario": args.leg,
        "start_at": DIAGNOSTIC_START_STAGE,
        "status": "not-run",
        "checks": {"preconditions": "not-run", "seed": "not-run", "snapshot": "not-run"},
    }
    result_path = evidence / "journey-result.json"
    journey.write_json(result_path, result)
    try:
        version, context = _diagnostic_release0_context(args, root, data, evidence)
        result["preconditions"] = {
            "state_path": str(context["state_path"]),
            "state_sha256": _bytes_sha256(context["state_path"], "precondition file"),
            "release0_tuple": context["identity"]["release0_version"]["tuple"],
            "release1_tuple": context["identity"]["release1_candidate"]["tuple"],
            "preseed_workspace": context["identity"]["preseed_workspace"],
        }
        result["checks"]["preconditions"] = "pass"
        baseline = evidence / DIAGNOSTIC_START_STAGE
        journey.verify("observe", journey.installation(root, data)[0], data, baseline)
        project = "tst-m4-3b-release0"
        keys = live_workspace(root, data, baseline, "seed", {
            "project": project, "generation": "release0", "task_id": None, "create_project": True})
        result["checks"]["seed"] = "pass"
        keys["task_id"] = task_after_update(version, baseline / "release0-task", project)
        live_workspace(root, data, baseline, "snapshot", keys)
        result["checks"]["snapshot"] = "pass"
        result["status"] = "pass"
    except Exception as exc:
        result.update(status="fail", error=f"{type(exc).__name__}: {exc}")
        raise
    finally:
        journey.write_json(result_path, result)
    return result


def workspace(version: dict, data: Path, evidence: Path, action: str, request: dict,
              *, realm: Path | None = None, diagnostic_args: list[str | Path] | None = None) -> dict:
    evidence.mkdir(parents=True, exist_ok=True)
    request_path = evidence / (action + "-request.json")
    journey.write_json(request_path, request)
    if action == "busy":
        # Persist the recovery identity before the helper can commit a fixture.
        with request_path.open("rb") as stream:
            os.fsync(stream.fileno())
        if os.name != "nt":
            directory = os.open(evidence, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    env = {key: value for key, value in os.environ.items() if key not in {"PYTHONPATH", "PYTHONHOME"}}
    env.update(version["env"])
    log = evidence / (action + ".json")
    output = journey.run([version["python"], "-I", journey.ROOT / "scripts/journey_workspace.py",
                          action, "--run-root", data.parent, "--realm", realm or journey.workspace_root(data),
                          "--request", request_path, *(diagnostic_args or [])],
                         log, env=env, timeout=120)
    return json.loads(output)


def live_workspace(root: Path, data: Path, evidence: Path, action: str, request: dict,
                   *, release: int = 0, ownership=None) -> dict:
    """Observe or seed through the selected live owner without releasing it.

    ``ownership(root, owner_data)`` replaces the direct service-ownership check; only the
    autostart-off leg passes one (its detached Runtime check).
    """
    owner_data = selected_data_root(root, data, allow_r1=False)
    if ownership is None:
        journey.assert_direct_service_ownership(root, owner_data, release=release)
    else:
        ownership(root, owner_data)
    discovery = journey.healthy(data)
    selection = live.selection_for(data, root, discovery)
    # The old offline path imposed this gate; also ensure its second discovery
    # read cannot silently choose another realm.
    if journey.workspace_root(data) != Path(selection["realm_root"]):
        raise RuntimeError("Runtime owner changed during live workspace access")
    live.check_selection(data, root, selection)
    evidence.mkdir(parents=True, exist_ok=True)
    if action == "snapshot":
        _, selected_data, version = journey.installation(root, owner_data)
        if selected_data != owner_data:
            raise RuntimeError("Selected Runtime data root differs from this journey")
        manifest = journey.read_json(Path(version["root"]) / "manifest.json")
        runtime = manifest.get("runtime")
        schema_digest = runtime.get("schema_digest") if isinstance(runtime, dict) else None
        if not isinstance(schema_digest, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", schema_digest):
            raise RuntimeError("Selected installation lacks its Runtime schema digest")
        request = {**request, "expected_schema_digest": schema_digest}
    request_path = evidence / (action + "-request.json")
    selection_path = evidence / (action + "-selection.json")
    journey.write_json(request_path, request)
    journey.write_json(selection_path, selection)
    diagnostic_path = evidence / f"live-{action}-failure.json"
    if action in {"seed", "snapshot"}:
        diagnostic_path.unlink(missing_ok=True)
    diagnostic_args = ["--diagnostic", diagnostic_path] if action in {"seed", "snapshot"} else []
    try:
        output = journey.run([sys.executable, journey.ROOT / "scripts/journey_live_workspace.py",
                              action, "--data", data, "--root", root, "--selection", selection_path,
                              "--request", request_path, *diagnostic_args], evidence / (action + ".json"), timeout=120)
    except RuntimeError:
        # Preserve the helper's normal failure while giving turnover a clear error.
        live.check_selection(data, root, selection)
        raise
    live.check_selection(data, root, selection)
    journey.assert_direct_service_ownership(root, owner_data, release=release)
    return json.loads(output)


def selected_data_root(root: Path, data: Path, *, allow_r1: bool = True) -> Path:
    """Resolve the active R1 copy root while keeping R0 as install provenance."""
    try:
        recorded, active = journey_data_roots.selection(root, data)
    except (ValueError, KeyError, TypeError, OSError) as exc:
        raise RuntimeError("Selected data root is outside this journey or invalid") from exc
    return active if allow_r1 else recorded


def assert_preserved(before: dict, after: dict) -> None:
    for key in ("project", "timeline", "preferences", "task", "cas_sha256"):
        if key not in before or key not in after:
            raise RuntimeError("Update did not retain seeded workspace " + key)
        if before[key] != after[key]:
            raise RuntimeError("Update changed seeded workspace " + key)
    for observation in (before, after):
        if "schema_digest" in observation or "target_schema_digest" in observation:
            matches = (observation.get("schema_digest") is not None
                       and observation.get("schema_digest") == observation.get("target_schema_digest"))
        else:
            matches = (observation.get("schema_version") is not None
                       and observation.get("schema_version") == observation.get("target_schema_version"))
        if not matches:
            raise RuntimeError("Selected realm does not match its Runtime schema")


def schema_summary(before: dict, after: dict) -> dict:
    if "schema_digest" in before and "schema_digest" in after:
        return {"before": before["schema_digest"], "after": after["schema_digest"],
                "target": after["target_schema_digest"]}
    return {"before": before["schema_version"], "after": after["schema_version"],
            "target": after["target_schema_version"]}


def service_action(root: Path, data: Path, version: dict, old: dict | None = None) -> dict:
    selection = journey.read_json(journey.selection_path())
    if (selection.get("program_root") != str(root) or selection.get("data_root") != str(data)
            or selection.get("identity") != version.get("identity")
            or not isinstance(version.get("identity"), dict)):
        raise RuntimeError("Service selection does not identify the selected candidate")
    if sys.platform == "darwin":
        path = Path.home() / "Library/LaunchAgents/haus.astrid.runtime.plist"
    elif sys.platform == "linux":
        path = Path(os.environ["XDG_CONFIG_HOME"]) / "systemd/user/astrid.service"
    else:
        path = root / "task.xml"
    text = html.unescape(path.read_text(encoding="utf-8")).replace("\\\\", "\\")
    if str(version["root"]) not in text or (old and str(old["root"]) != str(version["root"])
                                          and str(old["root"]) in text):
        raise RuntimeError("Registered service action did not migrate to the selected candidate")
    return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "identity": selection["identity"]}


def seed_old_declaration(old: dict, root: Path, kind: str) -> dict:
    declaration = Path(journey.read_json(Path(old["profile"]))["service_declaration"])
    if (declaration.is_symlink() or not declaration.resolve().is_relative_to(Path(old["root"]).resolve())):
        raise ValueError("Old service declaration is outside the selected run-owned version")
    value = journey.read_json(declaration)
    chat = next(row for row in value["services"] if row["name"] == "chat")
    if kind == "bare":
        command = "astrid"
    elif kind == "pruned":
        executable = "Scripts/astrid.exe" if sys.platform == "win32" else "bin/astrid"
        command = str(root / "versions" / "tst-c1-pruned" / "py" / executable)
        if Path(command).exists():
            raise ValueError("The deliberately pruned chat command unexpectedly exists")
    else:
        raise ValueError("Unknown old declaration kind")
    chat.setdefault("env", {})["ASTRID_ACP_COMMAND"] = command
    journey.write_json(declaration, value)
    return {"kind": kind, "path": str(declaration), "command": command}


def selected_candidate(manifest: dict, version: dict, before: dict) -> dict:
    selected = journey.selected_tuple(manifest, version)
    if all(selected[name] == before[name] for name in ("agent", "runtime")):
        raise RuntimeError("Version-jump requires a candidate distinct from the resolved release")
    expected_identity = {"release": manifest.get("release", 0), "managed_by": None}
    if version.get("identity") != expected_identity:
        raise RuntimeError("Selected version.json identity differs from the candidate manifest")
    return selected


def fixture_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verified_release0_fixture(fixture: Path) -> tuple[Path, Path, dict]:
    """Authenticate M4-2's byte-frozen release-0 inputs before bootstrap."""
    if fixture.is_symlink() or not fixture.is_dir():
        raise ValueError("release-0 fixture must be a non-symlink directory")
    root = fixture.resolve(strict=True)
    if root != RELEASE0_FIXTURE.resolve(strict=True):
        raise ValueError("the Windows refusal requires the checked-in frozen release-0 fixture")
    sums = root / "SHA256SUMS"
    if sums.is_symlink() or not sums.is_file() or fixture_digest(sums) != RELEASE0_SUMS_SHA256:
        raise ValueError("Frozen release-0 checksum manifest differs from the approved M4-2 bytes")
    entries: dict[str, str] = {}
    for line in sums.read_text(encoding="utf-8").splitlines():
        matched = CHECKSUM.fullmatch(line)
        if matched is None:
            raise ValueError("Frozen release-0 checksum manifest has an invalid entry")
        expected, relative = matched.groups()
        target = root.joinpath(*Path(relative).parts)
        if (relative in entries or ".." in Path(relative).parts or target.is_symlink()
                or not target.is_file() or not target.resolve().is_relative_to(root)):
            raise ValueError("Frozen release-0 checksum manifest has an unsafe entry")
        if fixture_digest(target) != expected:
            raise ValueError(f"Frozen release-0 fixture checksum mismatch: {relative}")
        entries[relative] = expected
    if not RELEASE0_REQUIRED <= set(entries):
        raise ValueError("Frozen release-0 fixture is missing bootstrap provenance")
    manifest_path = root / "served" / "install-manifest.json"
    manifest = journey.read_json(manifest_path)
    if manifest.get("release", 0) != 0 or {
            name: (manifest.get(name) or {}).get("sha") for name in RELEASE0_REFS} != RELEASE0_REFS:
        raise ValueError("Frozen release-0 manifest source refs differ from M4-2 provenance")
    provenance = journey.read_json(root / "provenance.json")
    refs = ((provenance.get("served_capture") or {}).get("manifest_refs"))
    if refs != RELEASE0_REFS:
        raise ValueError("Frozen release-0 provenance source refs differ from its manifest")
    receipt = {
        "fixture_sums_sha256": fixture_digest(sums),
        "manifest_sha256": entries["served/install-manifest.json"],
        "provenance_sha256": entries["provenance.json"],
        "bootstrap_sha256": {"install": entries["served/install"],
                              "install.ps1": entries["served/install.ps1"]},
        "sources": RELEASE0_REFS,
    }
    return root, manifest_path, receipt


def bootstrap_release0(fixture: Path, manifest: Path, shell: str, evidence: Path,
                       root: Path, data: Path) -> None:
    """Run the frozen public bootstrap bytes directly, with no live-URL fallback."""
    flags: list[str | Path] = ["--manifest", manifest, "--root", root, "--data-root", data]
    env = dict(os.environ)
    env["BROWSER"] = f'"{sys.executable}" -c "pass" %s'
    if sys.platform == "win32":
        # Hosted pwsh can leak its module directory into Windows PowerShell 5.1,
        # where Get-Acl's Microsoft.PowerShell.Security module may then fail to load.
        env = {key: value for key, value in env.items() if key.casefold() != "psmodulepath"}
        journey.run([shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File",
                     fixture / "served" / "install.ps1", *flags], evidence / "release0-bootstrap.log", env=env)
    else:
        journey.run([shell, fixture / "served" / "install", *flags],
                    evidence / "release0-bootstrap.log", env=env)


def start_direct_runtime(evidence: Path, *, task_name: str = "Astrid Runtime") -> None:
    if sys.platform == "win32":
        journey.windows.EVIDENCE = evidence
        journey.windows.powershell(f"Start-ScheduledTask -TaskName '{task_name}' -TaskPath '\\'",
                                   "start-scheduled-task.log", show=False, timeout=30)


def direct_workspace(root: Path, version: dict, data: Path, evidence: Path,
                     action: str, request: dict) -> dict:
    """Temporarily release the installed owner for the local workspace helper."""
    owner_data = selected_data_root(root, data, allow_r1=False)
    active_data = selected_data_root(root, data)
    service = journey.assert_direct_service_ownership(root, owner_data)
    journey.healthy(active_data)
    # Shutdown can remove discovery.json, so resolve and validate its realm while live.
    realm = journey.workspace_root(active_data)
    evidence.mkdir(parents=True, exist_ok=True)
    manager = service["manager"]
    if manager == "systemd-user":
        stop = ["systemctl", "--user", "stop", "astrid.service"]
        start = ["systemctl", "--user", "start", "astrid.service"]
    elif manager == "launchd":
        domain = f"gui/{os.getuid()}"
        stop = ["launchctl", "bootout", f"{domain}/haus.astrid.runtime"]
        start = ["launchctl", "bootstrap", domain, service["service_path"]]
    elif manager != "task-scheduler":
        raise RuntimeError(f"Unsupported direct service manager: {manager}")
    try:
        if manager == "task-scheduler":
            journey.windows.EVIDENCE = evidence
            journey.windows.powershell("$ErrorActionPreference='Stop'; Stop-ScheduledTask "
                                       "-TaskName 'Astrid Runtime' -TaskPath '\\'",
                                       f"{action}-stop-service.log", show=False, timeout=30)
        else:
            journey.run(stop, evidence / f"{action}-stop-service.log", timeout=120)
        deadline = time.monotonic() + 120
        while journey.runtime_processes(active_data):
            if time.monotonic() >= deadline:
                raise RuntimeError("Installed Runtime did not stop for workspace access")
            time.sleep(1)
        diagnostic_args = []
        if action == "snapshot" and evidence.name == "release1-api-observe":
            diagnostic_args = ["--diagnostic", evidence / "live-snapshot-failure.json"]
        return workspace(version, active_data, evidence, action, request, realm=realm,
                         diagnostic_args=diagnostic_args)
    finally:
        if manager == "task-scheduler":
            journey.windows.EVIDENCE = evidence
            journey.windows.powershell("$ErrorActionPreference='Stop'; Start-ScheduledTask "
                                       "-TaskName 'Astrid Runtime' -TaskPath '\\'",
                                       f"{action}-start-service.log", show=False, timeout=30)
        else:
            journey.run(start, evidence / f"{action}-start-service.log", timeout=120)
        journey.wait_ready(selected_data_root(root, data))
        journey.assert_direct_service_ownership(root, owner_data)


def installed_release(version: dict) -> int:
    release = journey.read_json(Path(version["root"]) / "manifest.json").get("release", 0)
    if type(release) is not int:
        raise RuntimeError("Installed manifest has an invalid release identity")
    return release


def semantic_digest(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def task_after_update(version: dict, evidence: Path, project: str) -> str:
    if sys.platform == "win32":
        journey.windows.EVIDENCE = evidence
        journey.windows.wait_for("pack-host is running with its current ready file", journey.windows.host_ready)
        journey.windows.snapshot("processes-host-ready.json")
        return journey.windows.task(project, create_project=False)
    return journey.task(version, evidence, project, create_project=False)


def release0_admission(args) -> tuple[Path, Path, dict]:
    """Make the harness exception uncallable outside its exact direct scope."""
    diagnostic = getattr(args, "diagnostic_start_at", None) == DIAGNOSTIC_START_STAGE
    if (getattr(args, "mode", None) != "direct" or (not getattr(args, "job_id", None) and not diagnostic)
            or not getattr(args, "candidate", None) or not getattr(args, "allow_release0_to_release1", False)
            or getattr(args, "release0_fixture", None) is None):
        raise ValueError("the release-0 start requires its scoped frozen-fixture direct admission")
    if getattr(args, "source_override", False):
        raise ValueError("the release-0 start never admits a source override")
    return verified_release0_fixture(args.release0_fixture)


def release9_admission(args, output: Path) -> tuple[Path, Path, dict]:
    """Admit the pinned, published Release 9 as the release9-then-install start."""
    diagnostic = getattr(args, "diagnostic_start_at", None) == DIAGNOSTIC_START_STAGE
    if (getattr(args, "mode", None) != "direct" or (not getattr(args, "job_id", None) and not diagnostic)
            or not getattr(args, "candidate", None) or not getattr(args, "allow_release0_to_release1", False)
            or getattr(args, "release9_app_checkout", None) is None):
        raise ValueError("release9-then-install requires its scoped pinned Release-9 direct admission")
    if getattr(args, "source_override", False):
        raise ValueError("release9-then-install never admits a source override")
    return verified_release9_source(args.release9_app_checkout, output)


def verified_release9_source(app: Path, output: Path) -> tuple[Path, Path, dict]:
    """Export Release 9's manifest and bootstrap from the pinned App commit.

    The bytes are the git blobs at RELEASE9_APP_COMMIT, not whatever the live
    endpoint serves now. The export is then run by the plain install command.
    """
    if app.is_symlink() or not app.is_dir():
        raise ValueError("Release-9 App checkout must be a non-symlink directory")
    resolved = upgrade_release.resolve("release9", output, app_repo=app.resolve(strict=True))
    if resolved.get("status") != "verified" or resolved.get("app_manifest_commit") != RELEASE9_APP_COMMIT:
        raise RuntimeError("Release 9 could not be resolved from its pinned App commit")
    if resolved.get("identity") != RELEASE9_IDENTITY:
        raise RuntimeError("Pinned Release 9 identity differs from the published release")
    manifest_path = output / "install-manifest.json"
    manifest = journey.read_json(manifest_path)
    if manifest.get("release") != RELEASE9_RELEASE:
        raise RuntimeError("Pinned Release 9 manifest does not declare release 9")
    receipt = {
        "app_commit": resolved["app_manifest_commit"],
        "manifest_sha256": resolved["manifest_sha256"],
        "bootstrap_sha256": {row["path"]: row["sha256"] for row in resolved["bootstrap"]},
        "identity": resolved["identity"],
    }
    return output, manifest_path, receipt


def release9_consent_agent(args) -> Path | None:
    """The Agent checkout whose history holds the pinned Release 9 Agent.

    The CI leg passes ``--candidate-agent-checkout`` (a partial clone with full history,
    so the pinned Release 9 CLI can be read on demand); ``--agent-checkout`` is unset there.
    """
    return getattr(args, "candidate_agent_checkout", None) or getattr(args, "agent_checkout", None)


def bootstrap_release9(source: Path, manifest: Path, shell: str, evidence: Path,
                       root: Path, data: Path, agent: Path | None = None) -> None:
    """Install the exported Release 9 with the plain install command, no --update.

    ``agent`` is the checkout whose history holds the pinned Release 9 Agent. Without it
    the consent probe cannot read that Agent's CLI, so a fresh install that needs setup
    consent would stop with no terminal (exit 2).
    """
    journey.bootstrap(source, manifest, shell, "install", evidence, root, data, agent=agent)


def candidate_release(manifest: dict) -> int:
    """Return the candidate release, which the plain install must upgrade to.

    The installer's plain install only updates an existing install when the
    manifest's release is newer than the installed one, so the candidate must
    be strictly newer than Release 9.
    """
    release = manifest.get("release")
    if type(release) is not int or release <= RELEASE9_RELEASE:
        raise RuntimeError("release9-then-install requires a frozen candidate newer than Release 9")
    return release


def execute_release9_then_install(args, root: Path, data: Path, evidence: Path, sentinel, birth: str) -> dict:
    """Pinned Release 9 direct install, API seed, and plain install upgrade to the candidate."""
    evidence.mkdir(parents=True, exist_ok=True)
    result = {
        "schema_version": 3, "receipt_kind": "release1-installed-lifecycle",
        "job_id": args.job_id, "mode": args.mode, "scenario": args.leg, "status": "not-run",
        "os_build": platform.platform(), "shell": args.shell, **journey.runner_identity(),
        "source_override_test_mode": False, "candidate_checkouts": getattr(args, "candidate_checkouts", None),
        "service_manager_prerequisite": getattr(args, "service_manager_prerequisite", None),
        "sources": None, "installed_tuple": None, "import_origins": None,
        "run_roots": {"program": str(root), "data": str(data)},
        "sentinel": {"pid": sentinel.pid, "birth": birth},
        "release9_source": None, "release_transition": None, "semantic_data": None,
        "checks": {key: "not-run" for key in (
            "release", "service", "health", "semantic_data", "task_settled",
            "unrelated_sentinel")},
    }
    result_path = evidence / "journey-result.json"
    journey.write_json(result_path, result)
    try:
        source, release9_manifest, source_receipt = release9_admission(args, root.parent / "release9-source")
        result["release9_source"] = source_receipt
        app = args.app_checkout.resolve(strict=True)
        candidate_path, manifest = journey.selected_manifest(
            app, source_override=False, agent=None, runtime=None, evidence=evidence)
        candidate = journey.read_json(args.candidate)
        if evidence_gate.validate_tuple(candidate, app, candidate_path) != manifest:
            raise RuntimeError("release9-then-install requires a frozen candidate manifest")
        target_release = candidate_release(manifest)
        result["candidate_bootstrap"] = {"manifest_sha256": candidate["manifest_sha256"],
                                         "artifacts": candidate["artifacts"]}
        result["sources"] = {"app": journey.git_source(app), "agent": manifest["agent"],
                             "runtime": manifest["runtime"]}
        before_sentinel = journey.sentinel_observation(sentinel, birth)
        result["sentinel"]["before_lifecycle"] = before_sentinel
        if not before_sentinel["alive"]:
            raise RuntimeError("Unrelated sentinel exited before the release-0 bootstrap")
        bootstrap_release9(source, release9_manifest, args.shell, evidence / "release9", root, data,
                           agent=release9_consent_agent(args))
        start_direct_runtime(evidence / "release9")
        journey.wait_ready(selected_data_root(root, data))
        old_path, old_data, old = journey.installation(root, data)
        if old_data != data or installed_release(old) != RELEASE9_RELEASE:
            # Only the pinned Release 9 manifest attests this starting identity.
            raise RuntimeError("Pinned Release-9 bootstrap did not select release 9")
        old_tuple = journey.selected_tuple(journey.read_json(release9_manifest), old)
        old_service = journey.assert_direct_service_ownership(root, data)
        # A retained run may be used to isolate this one block for diagnostics,
        # but only after this authentic bootstrap has established the complete
        # release-9 and candidate tuple binding.
        write_diagnostic_release0_preconditions(
            args, root, data, fixture_receipt=source_receipt,
            release0_manifest=release9_manifest, target_manifest=manifest,
            candidate=candidate, version_path=old_path, version=old,
        )
        baseline = evidence / "release0-api-seed"
        journey.verify("observe", old_path, data, baseline)
        project = "tst-m4-3b-release0"
        keys = live_workspace(root, data, baseline, "seed", {
            "project": project, "generation": "release0", "task_id": None, "create_project": True})
        keys["task_id"] = task_after_update(old, baseline / "release0-task", project)
        before = live_workspace(root, data, baseline, "snapshot", keys)
        old_health = journey.healthy(data)
        # The plain install (no --update) upgrades the pinned Release 9 install
        # because the candidate is newer. It keeps the default service identity.
        journey.bootstrap(app, candidate_path, args.shell, "install", evidence / "release1", root, data)
        start_direct_runtime(evidence / "release1")
        active_data = selected_data_root(root, data)
        journey.wait_ready(active_data)
        version_path, selected_data, version = journey.installation(root, data)
        if selected_data != old_data or version_path == old_path:
            raise RuntimeError("Candidate upgrade did not retain data and activate a new version")
        selected = selected_candidate(manifest, version, old_tuple)
        if installed_release(version) != target_release:
            raise RuntimeError("Candidate upgrade did not select the candidate release")
        new_service = journey.assert_direct_service_ownership(root, data)
        new_health = journey.healthy(active_data)
        if new_health["runtime_instance_id"] == old_health["runtime_instance_id"]:
            raise RuntimeError("Candidate upgrade retained the release-9 Runtime instance")
        result["release_transition"] = {"from": RELEASE9_RELEASE, "to": target_release,
                                        "source": "pinned-release9-plain-install"}
        result["service_transition"] = {"release9": old_service, "candidate": new_service}
        result["service_manager"] = new_service
        result["health_transition"] = {
            "release9_runtime_instance_id": old_health["runtime_instance_id"],
            "candidate_runtime_instance_id": new_health["runtime_instance_id"],
        }
        result["checks"].update(release="pass", service="pass", health="pass")
        result["installed_tuple"] = {"app": result["sources"]["app"]["sha"], **selected}
        journey.verify("update", version_path, active_data, evidence, before=baseline / "verify-observe.json")
        journey.verify("install", version_path, active_data, evidence)
        result["import_origins"] = (journey.read_json(evidence / "verify-install.json").get("details") or {}).get("origin.imports")
        after = live_workspace(root, active_data, evidence / "release1-api-observe", "snapshot", keys)
        assert_preserved(before, after)
        semantic_keys = ("project", "timeline", "preferences", "task", "cas_sha256")
        before_sha = semantic_digest({k: before[k] for k in semantic_keys})
        after_sha = semantic_digest({k: after[k] for k in semantic_keys})
        if before_sha != after_sha:
            raise RuntimeError("Release-1 semantic snapshot digest differs after exact preservation")
        result["semantic_data"] = {"identical": True, "before_sha256": before_sha, "after_sha256": after_sha}
        result["checks"]["semantic_data"] = "pass"
        task_after_update(version, evidence / "release1-working-task", project)
        result["checks"]["task_settled"] = "pass"
        settled_sentinel = journey.sentinel_observation(sentinel, birth)
        result["sentinel"]["at_settlement"] = settled_sentinel
        if not settled_sentinel["alive"]:
            raise RuntimeError("Unrelated sentinel did not survive the direct release-1 update")
        result["checks"]["unrelated_sentinel"] = "pass"
        result["status"] = "pass"
    except Exception as exc:
        result.update(status="fail", error=f"{type(exc).__name__}: {exc}")
        raise
    finally:
        journey.write_json(result_path, result)
    return result


R0_HANDOFF_LEG = "r0-handoff-then-update"
# The handoff is performed by the published Release 9 installer, so the start
# state is a completed R0 to R1 storage handoff that landed on Release 9.
R0_HANDOFF_RELEASE = RELEASE9_RELEASE


def r0_handoff_admission(args, output: Path) -> tuple[tuple[Path, Path, dict], tuple[Path, Path, dict]]:
    """Admit the frozen R0 fixture and the pinned Release 9 installer that hands it off."""
    if getattr(args, "job_id", None) is None or getattr(args, "diagnostic_start_at", None):
        raise ValueError(f"{R0_HANDOFF_LEG} is admitted only for a disposable direct qualification job")
    return release0_admission(args), release9_admission(args, output)


def handoff_journal() -> dict:
    """The completed handoff journal, read from beside the installer's selection record."""
    path = journey.selection_path().with_name("astrid-r1-handoff.json")
    if path.is_symlink() or not path.is_file():
        raise RuntimeError("R0 handoff journal is absent")
    return journey.read_json(path)


def handed_off_start(root: Path, data: Path) -> dict:
    """Prove the install is in the field state: R0 handed off, R0 data retained, R1 active."""
    if __package__:
        from . import journey_fresh_update as fresh
    else:
        import journey_fresh_update as fresh
    boundary = fresh.boundary_state(root, data)
    if not fresh.is_handed_off(boundary, data):
        raise RuntimeError(f"R0 handoff did not leave the handed-off start state: {boundary}")
    journal = handoff_journal()
    if (journal.get("phase") != "complete" or journal.get("r0_data_root") != str(data)
            or journal.get("r1_data_root") != str(Path(str(data) + "-r1")) or not journal.get("receipt")):
        raise RuntimeError("R0 handoff journal is not a completed handoff of this journey's data root")
    recorded, active = journey_data_roots.selection(root, data)
    if recorded != data or active != Path(str(data) + "-r1") or not data.is_dir() or not active.is_dir():
        raise RuntimeError("R0 handoff did not retain R0's data and select the R1 copy")
    return {"boundary": boundary, "phase": journal["phase"],
            "r0_data_root": str(recorded), "r1_data_root": str(active)}


def run_receipt(data: Path) -> dict | None:
    """The candidate installer's ``last-run.json``, beside its install.log under the run home."""
    home = data.parent / "home"
    log_dir = (home / "Library" / "Logs" / "Astrid Runtime" if sys.platform == "darwin"
               else home / ".local" / "state" / "astrid-runtime" / "logs")
    path = log_dir / "last-run.json"
    if path.is_symlink() or not path.is_file():
        return None
    return journey.read_json(path)


def live_runtime_owners(data: Path) -> list[dict]:
    """Discovery owners whose process is alive, at the R0 root and the R1 copy."""
    owners = []
    for root in (data, Path(str(data) + "-r1")):
        try:
            discovery = json.loads((root / "runtime" / "discovery.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        pid = discovery.get("pid") if isinstance(discovery, dict) else None
        if type(pid) is not int or pid <= 0:
            continue
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            continue
        except PermissionError:
            pass
        owners.append({"data_root": str(root), "pid": pid,
                       "runtime_instance_id": discovery.get("runtime_instance_id")})
    return owners


def candidate_update(app: Path, manifest_path: Path, shell: str, evidence: Path,
                     root: Path, data: Path) -> dict:
    """Run the candidate's public ``install --update`` once and return what it reported.

    The exit code is returned, not raised, so the leg can assert it beside the
    installed release: an update that exits 0 and installs nothing is the
    failure this leg exists to catch.
    """
    if sys.platform == "win32":
        raise RuntimeError(f"{R0_HANDOFF_LEG} is not admitted on Windows")
    env = dict(os.environ)
    env["BROWSER"] = f'"{sys.executable}" -c "pass" %s'
    log = evidence / "update-bootstrap.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    argv = [shell, str(app / "public" / "install"), "--manifest", str(manifest_path),
            "--root", str(root), "--data-root", str(data), "--update"]
    with log.open("w", encoding="utf-8") as stream:
        process = subprocess.run(argv, env=env, stdout=stream, stderr=subprocess.STDOUT,
                                 timeout=3600, check=False)
    lines = [line for line in log.read_text(encoding="utf-8-sig", errors="replace").splitlines() if line.strip()]
    return {"command": "install --update", "exit_code": process.returncode,
            "log": str(log), "last_lines": lines[-8:]}


def execute_r0_handoff_then_update(args, root: Path, data: Path, evidence: Path, sentinel, birth: str) -> dict:
    """Frozen R0, seeded, handed off by the published Release 9, then the candidate's ``install --update``.

    This is the start state of real pre-R1 installs in the field: a completed
    R0 to R1 storage handoff, with R0's data kept under the R0 root and the
    active copy beside it. The candidate's ``--update`` must install the
    candidate release and exit 0.
    """
    evidence.mkdir(parents=True, exist_ok=True)
    result = {
        "schema_version": 1, "receipt_kind": "r0-handoff-then-update",
        "job_id": args.job_id, "mode": args.mode, "scenario": args.leg, "status": "not-run",
        "os_build": platform.platform(), "shell": args.shell, **journey.runner_identity(),
        "source_override_test_mode": False, "candidate_checkouts": getattr(args, "candidate_checkouts", None),
        "service_manager_prerequisite": getattr(args, "service_manager_prerequisite", None),
        "sources": None, "installed_tuple": None, "import_origins": None,
        "run_roots": {"program": str(root), "data": str(data)},
        "sentinel": {"pid": sentinel.pid, "birth": birth},
        "release0_fixture": None, "handoff_source": None, "handoff": None, "update": None,
        "release_transition": None, "semantic_data": None,
        "checks": {key: "not-run" for key in (
            "handed_off_start", "update_exit", "run_receipt", "release", "one_runtime", "service", "health",
            "semantic_data", "task_settled", "unrelated_sentinel")},
    }
    result_path = evidence / "journey-result.json"
    journey.write_json(result_path, result)
    try:
        (fixture, r0_manifest, fixture_receipt), (handoff_app, handoff_manifest, handoff_receipt) = (
            r0_handoff_admission(args, root.parent / "release9-source"))
        result["release0_fixture"] = fixture_receipt
        result["handoff_source"] = handoff_receipt
        app = args.app_checkout.resolve(strict=True)
        candidate_path, manifest = journey.selected_manifest(
            app, source_override=False, agent=None, runtime=None, evidence=evidence)
        candidate = journey.read_json(args.candidate)
        if evidence_gate.validate_tuple(candidate, app, candidate_path) != manifest:
            raise RuntimeError(f"{R0_HANDOFF_LEG} requires a frozen candidate manifest")
        target_release = candidate_release(manifest)
        result["candidate_bootstrap"] = {"manifest_sha256": candidate["manifest_sha256"],
                                         "artifacts": candidate["artifacts"]}
        result["sources"] = {"app": journey.git_source(app), "agent": manifest["agent"],
                             "runtime": manifest["runtime"]}
        before_sentinel = journey.sentinel_observation(sentinel, birth)
        result["sentinel"]["before_lifecycle"] = before_sentinel
        if not before_sentinel["alive"]:
            raise RuntimeError("Unrelated sentinel exited before the release-0 bootstrap")

        # 1. The frozen R0 install, seeded through its live Runtime.
        bootstrap_release0(fixture, r0_manifest, args.shell, evidence / "release0", root, data)
        journey.wait_ready(data)
        r0_path, r0_data, r0 = journey.installation(root, data)
        if r0_data != data or installed_release(r0) != 0:
            raise RuntimeError("Frozen release-0 bootstrap did not select release 0")
        journey.selected_tuple(journey.read_json(r0_manifest), r0)
        journey.assert_direct_service_ownership(root, data)
        baseline = evidence / "release0-api-seed"
        journey.verify("observe", r0_path, data, baseline)
        project = "tst-m4-3b-release0"
        keys = live_workspace(root, data, baseline, "seed", {
            "project": project, "generation": "release0", "task_id": None, "create_project": True})
        keys["task_id"] = task_after_update(r0, baseline / "release0-task", project)
        before = live_workspace(root, data, baseline, "snapshot", keys)

        # 2. The published Release 9 installer hands R0 off, as it did on real installs.
        journey.bootstrap(handoff_app, handoff_manifest, args.shell, "update", evidence / "handoff", root, data)
        handoff_data = selected_data_root(root, data)
        journey.wait_ready(handoff_data)
        old_path, old_data, old = journey.installation(root, data)
        if old_data != data or old_path == r0_path or installed_release(old) != R0_HANDOFF_RELEASE:
            raise RuntimeError(f"R0 handoff did not select release {R0_HANDOFF_RELEASE}")
        old_tuple = journey.selected_tuple(journey.read_json(handoff_manifest), old)
        old_service = journey.assert_direct_service_ownership(root, data, release=1)
        result["handoff"] = {**handed_off_start(root, data), "release": R0_HANDOFF_RELEASE,
                             "service": old_service}
        result["checks"]["handed_off_start"] = "pass"
        old_health = journey.healthy(handoff_data)

        # 3. The candidate's public update command, from the handed-off state.
        update = result["update"] = candidate_update(app, candidate_path, args.shell, evidence / "update",
                                                     root, data)
        receipt = update["receipt"] = run_receipt(data)
        if update["exit_code"] != 0:
            raise RuntimeError(f"Candidate install --update exited {update['exit_code']} "
                               f"from the handed-off Release {R0_HANDOFF_RELEASE} install")
        result["checks"]["update_exit"] = "pass"
        version_path, selected_data, version = journey.installation(root, data)
        if installed_release(version) != target_release:
            raise RuntimeError(
                f"Candidate install --update exited 0 but installed nothing: the selected release is "
                f"still {installed_release(version)}, not the candidate release {target_release}")
        if version_path == old_path:
            raise RuntimeError("Candidate install --update did not activate a new version")
        # The candidate's own receipt (UR-15a) must agree: updated, exit 0, the target live.
        if (not isinstance(receipt, dict) or receipt.get("kind") != "updated" or receipt.get("exit_code") != 0
                or receipt.get("still") != target_release or receipt.get("target") != target_release):
            raise RuntimeError(f"The candidate's run receipt is not updated to release {target_release}: {receipt}")
        result["checks"]["run_receipt"] = "pass"
        selected = selected_candidate(manifest, version, old_tuple)
        active_data = selected_data_root(root, data)
        journey.wait_ready(active_data)
        new_health = journey.healthy(active_data)
        live_release = (new_health.get("identity") or {}).get("release")
        if live_release != target_release:
            raise RuntimeError(f"The live Runtime reports release {live_release}, not the candidate release {target_release}")
        if new_health["runtime_instance_id"] == old_health["runtime_instance_id"]:
            raise RuntimeError("Candidate update retained the handed-off Runtime instance")
        live_owners = live_runtime_owners(data)
        result["live_runtimes"] = live_owners
        if [owner["data_root"] for owner in live_owners] != [str(active_data)]:
            raise RuntimeError(f"Exactly one live Runtime (at {active_data}) is required after the update: {live_owners}")
        result["checks"]["one_runtime"] = "pass"
        result["release_transition"] = {"from": R0_HANDOFF_RELEASE, "to": target_release,
                                        "start_state": "r0-handoff-complete", "source": "candidate-install-update"}
        result["checks"]["release"] = "pass"
        r1_service = (data.parent / "home/.config/systemd/user/astrid-r1.service" if sys.platform == "linux"
                      else data.parent / "home/Library/LaunchAgents/haus.astrid.runtime.r1.plist")
        new_service = journey.assert_direct_service_ownership(root, selected_data,
                                                              release=1 if r1_service.exists() else 0)
        result["service_transition"] = {"handed_off": old_service, "candidate": new_service}
        result["service_manager"] = new_service
        result["health_transition"] = {
            "handed_off_runtime_instance_id": old_health["runtime_instance_id"],
            "candidate_runtime_instance_id": new_health["runtime_instance_id"],
            "candidate_live_release": live_release,
        }
        result["checks"].update(service="pass", health="pass")
        result["installed_tuple"] = {"app": result["sources"]["app"]["sha"], **selected}
        journey.verify("update", version_path, active_data, evidence, before=baseline / "verify-observe.json")
        journey.verify("install", version_path, active_data, evidence)
        result["import_origins"] = (journey.read_json(evidence / "verify-install.json").get("details") or {}).get("origin.imports")
        after = live_workspace(root, active_data, evidence / "release1-api-observe", "snapshot", keys,
                               release=1 if r1_service.exists() else 0)
        assert_preserved(before, after)
        semantic_keys = ("project", "timeline", "preferences", "task", "cas_sha256")
        before_sha = semantic_digest({k: before[k] for k in semantic_keys})
        after_sha = semantic_digest({k: after[k] for k in semantic_keys})
        if before_sha != after_sha:
            raise RuntimeError("Semantic snapshot digest differs after the handoff and the update")
        result["semantic_data"] = {"identical": True, "before_sha256": before_sha, "after_sha256": after_sha}
        result["checks"]["semantic_data"] = "pass"
        task_after_update(version, evidence / "candidate-working-task", project)
        result["checks"]["task_settled"] = "pass"
        settled_sentinel = journey.sentinel_observation(sentinel, birth)
        result["sentinel"]["at_settlement"] = settled_sentinel
        if not settled_sentinel["alive"]:
            raise RuntimeError("Unrelated sentinel did not survive the handoff and the update")
        result["checks"]["unrelated_sentinel"] = "pass"
        result["status"] = "pass"
    except Exception as exc:
        result.update(status="fail", error=f"{type(exc).__name__}: {exc}")
        raise
    finally:
        journey.write_json(result_path, result)
    return result


def release11_admission(args, output: Path) -> tuple[Path, Path, dict]:
    """Admit the pinned, served Release 11 as the release11-then-update start."""
    if (getattr(args, "mode", None) != "direct" or not getattr(args, "job_id", None)
            or not getattr(args, "candidate", None) or not getattr(args, "allow_release11_to_candidate", False)
            or getattr(args, "release11_app_checkout", None) is None):
        raise ValueError(f"{RELEASE11_LEG} requires its scoped pinned Release-11 direct admission")
    if getattr(args, "source_override", False):
        raise ValueError(f"{RELEASE11_LEG} never admits a source override")
    return verified_release11_source(args.release11_app_checkout, output)


def verified_release11_source(app: Path, output: Path) -> tuple[Path, Path, dict]:
    """Export Release 11's manifest and bootstrap from its pinned App commit.

    The bytes are the git blobs at RELEASE11_APP_COMMIT, not whatever the live
    endpoint serves now. The export is then run by the plain install command.
    """
    if app.is_symlink() or not app.is_dir():
        raise ValueError("Release-11 App checkout must be a non-symlink directory")
    resolved = upgrade_release.resolve("release11", output, app_repo=app.resolve(strict=True))
    if resolved.get("status") != "verified" or resolved.get("app_manifest_commit") != RELEASE11_APP_COMMIT:
        raise RuntimeError("Release 11 could not be resolved from its pinned App commit")
    if resolved.get("identity") != RELEASE11_IDENTITY:
        raise RuntimeError("Pinned Release 11 identity differs from the served release")
    manifest_path = output / "install-manifest.json"
    manifest = journey.read_json(manifest_path)
    if manifest.get("release") != RELEASE11_RELEASE:
        raise RuntimeError("Pinned Release 11 manifest does not declare release 11")
    receipt = {
        "app_commit": resolved["app_manifest_commit"],
        "manifest_sha256": resolved["manifest_sha256"],
        "bootstrap_sha256": {row["path"]: row["sha256"] for row in resolved["bootstrap"]},
        "identity": resolved["identity"],
    }
    return output, manifest_path, receipt


def bootstrap_release11(source: Path, manifest: Path, shell: str, evidence: Path,
                        root: Path, data: Path, agent: Path | None = None) -> None:
    """Install the exported Release 11 with the plain install command, never --update."""
    journey.bootstrap(source, manifest, shell, "install", evidence, root, data, agent=agent)


def execute_release11_then_update(args, root: Path, data: Path, evidence: Path, sentinel, birth: str) -> dict:
    """Pinned Release 11 plain install, API seed, then the candidate's install --update to Release 12.

    The candidate's update must exit 0 and install Release 12. Its own run receipt
    must say updated to 12, exactly one Runtime may be live afterwards, and the
    seeded user data must survive the update unchanged.
    """
    evidence.mkdir(parents=True, exist_ok=True)
    result = {
        "schema_version": 1, "receipt_kind": RELEASE11_LEG,
        "job_id": args.job_id, "mode": args.mode, "scenario": args.leg, "status": "not-run",
        "os_build": platform.platform(), "shell": args.shell, **journey.runner_identity(),
        "source_override_test_mode": False, "candidate_checkouts": getattr(args, "candidate_checkouts", None),
        "service_manager_prerequisite": getattr(args, "service_manager_prerequisite", None),
        "sources": None, "installed_tuple": None, "import_origins": None,
        "run_roots": {"program": str(root), "data": str(data)},
        "sentinel": {"pid": sentinel.pid, "birth": birth},
        "release11_source": None, "candidate_bootstrap": None, "update": None,
        "release_transition": None, "live_runtimes": None, "semantic_data": None,
        "checks": {key: "not-run" for key in (
            "release11_install", "update_exit", "run_receipt", "release", "one_runtime", "service", "health",
            "semantic_data", "task_settled", "unrelated_sentinel")},
    }
    result_path = evidence / "journey-result.json"
    journey.write_json(result_path, result)
    try:
        source, release11_manifest, source_receipt = release11_admission(args, root.parent / "release11-source")
        result["release11_source"] = source_receipt
        app = args.app_checkout.resolve(strict=True)
        candidate_path, manifest = journey.selected_manifest(
            app, source_override=False, agent=None, runtime=None, evidence=evidence)
        candidate = journey.read_json(args.candidate)
        if evidence_gate.validate_tuple(candidate, app, candidate_path) != manifest:
            raise RuntimeError(f"{RELEASE11_LEG} requires a frozen candidate manifest")
        if manifest.get("release") != RELEASE11_TARGET_RELEASE:
            raise RuntimeError(f"{RELEASE11_LEG} requires the candidate to be Release {RELEASE11_TARGET_RELEASE}, "
                               f"not {manifest.get('release')}")
        target_release = RELEASE11_TARGET_RELEASE
        result["candidate_bootstrap"] = {"manifest_sha256": candidate["manifest_sha256"],
                                         "artifacts": candidate["artifacts"]}
        result["sources"] = {"app": journey.git_source(app), "agent": manifest["agent"],
                             "runtime": manifest["runtime"]}
        before_sentinel = journey.sentinel_observation(sentinel, birth)
        result["sentinel"]["before_lifecycle"] = before_sentinel
        if not before_sentinel["alive"]:
            raise RuntimeError("Unrelated sentinel exited before the release-11 install")

        # 1. The pinned Release 11, installed plainly, then seeded through its live Runtime.
        bootstrap_release11(source, release11_manifest, args.shell, evidence / "release11", root, data,
                            agent=release9_consent_agent(args))
        start_direct_runtime(evidence / "release11")
        journey.wait_ready(selected_data_root(root, data))
        old_path, old_data, old = journey.installation(root, data)
        if old_data != data or installed_release(old) != RELEASE11_RELEASE:
            raise RuntimeError(f"Pinned Release-11 install did not select release {RELEASE11_RELEASE}")
        old_tuple = journey.selected_tuple(journey.read_json(release11_manifest), old)
        old_service = journey.assert_direct_service_ownership(root, data)
        baseline = evidence / "release11-api-seed"
        journey.verify("observe", old_path, data, baseline)
        project = "tst-release11-update"
        keys = live_workspace(root, data, baseline, "seed", {
            "project": project, "generation": "release11", "task_id": None, "create_project": True})
        keys["task_id"] = task_after_update(old, baseline / "release11-task", project)
        before = live_workspace(root, data, baseline, "snapshot", keys)
        old_health = journey.healthy(data)
        result["checks"]["release11_install"] = "pass"

        # 2. The candidate's public update command, from the Release 11 install.
        update = result["update"] = candidate_update(app, candidate_path, args.shell, evidence / "update",
                                                     root, data)
        receipt = update["receipt"] = run_receipt(data)
        if update["exit_code"] != 0:
            raise RuntimeError(f"Candidate install --update exited {update['exit_code']} "
                               f"from the Release {RELEASE11_RELEASE} install")
        result["checks"]["update_exit"] = "pass"
        version_path, selected_data, version = journey.installation(root, data)
        if installed_release(version) != target_release:
            raise RuntimeError(
                f"Candidate install --update exited 0 but installed nothing: the selected release is "
                f"still {installed_release(version)}, not the candidate release {target_release}")
        if version_path == old_path:
            raise RuntimeError("Candidate install --update did not activate a new version")
        # The candidate's own receipt (UR-15a) must agree: updated, exit 0, the target live.
        if (not isinstance(receipt, dict) or receipt.get("kind") != "updated" or receipt.get("exit_code") != 0
                or receipt.get("still") != target_release or receipt.get("target") != target_release):
            raise RuntimeError(f"The candidate's run receipt is not updated to release {target_release}: {receipt}")
        result["checks"]["run_receipt"] = "pass"
        selected = selected_candidate(manifest, version, old_tuple)
        active_data = selected_data_root(root, data)
        journey.wait_ready(active_data)
        new_health = journey.healthy(active_data)
        live_release = (new_health.get("identity") or {}).get("release")
        if live_release != target_release:
            raise RuntimeError(f"The live Runtime reports release {live_release}, not the candidate release {target_release}")
        if new_health["runtime_instance_id"] == old_health["runtime_instance_id"]:
            raise RuntimeError(f"Candidate update retained the Release {RELEASE11_RELEASE} Runtime instance")
        live_owners = live_runtime_owners(data)
        result["live_runtimes"] = live_owners
        if [owner["data_root"] for owner in live_owners] != [str(active_data)]:
            raise RuntimeError(f"Exactly one live Runtime (at {active_data}) is required after the update: {live_owners}")
        result["checks"]["one_runtime"] = "pass"
        result["release_transition"] = {"from": RELEASE11_RELEASE, "to": target_release,
                                        "source": "candidate-install-update"}
        result["checks"]["release"] = "pass"
        new_service = journey.assert_direct_service_ownership(root, selected_data)
        result["service_transition"] = {"release11": old_service, "candidate": new_service}
        result["service_manager"] = new_service
        result["health_transition"] = {
            "release11_runtime_instance_id": old_health["runtime_instance_id"],
            "candidate_runtime_instance_id": new_health["runtime_instance_id"],
            "candidate_live_release": live_release,
        }
        result["checks"].update(service="pass", health="pass")
        result["installed_tuple"] = {"app": result["sources"]["app"]["sha"], **selected}
        journey.verify("update", version_path, active_data, evidence, before=baseline / "verify-observe.json")
        journey.verify("install", version_path, active_data, evidence)
        result["import_origins"] = (journey.read_json(evidence / "verify-install.json").get("details") or {}).get("origin.imports")

        # 3. The seeded user data survives the update exactly.
        after = live_workspace(root, active_data, evidence / "release11-api-observe", "snapshot", keys)
        assert_preserved(before, after)
        semantic_keys = ("project", "timeline", "preferences", "task", "cas_sha256")
        before_sha = semantic_digest({k: before[k] for k in semantic_keys})
        after_sha = semantic_digest({k: after[k] for k in semantic_keys})
        if before_sha != after_sha:
            raise RuntimeError("Semantic snapshot digest differs after the candidate update")
        result["semantic_data"] = {"identical": True, "before_sha256": before_sha, "after_sha256": after_sha}
        result["checks"]["semantic_data"] = "pass"
        task_after_update(version, evidence / "candidate-working-task", project)
        result["checks"]["task_settled"] = "pass"
        settled_sentinel = journey.sentinel_observation(sentinel, birth)
        result["sentinel"]["at_settlement"] = settled_sentinel
        if not settled_sentinel["alive"]:
            raise RuntimeError("Unrelated sentinel did not survive the Release 11 install and the update")
        result["checks"]["unrelated_sentinel"] = "pass"
        result["status"] = "pass"
    except Exception as exc:
        result.update(status="fail", error=f"{type(exc).__name__}: {exc}")
        raise
    finally:
        journey.write_json(result_path, result)
    return result


AUTOSTART_LEG = "fresh-install-autostart-off"
AUTOSTART_CHECKS = ("install_exit", "selection_autostart_false", "update_status_autostart_off",
                    "post_update_refused_unchanged", "autostart_card_capture", "service", "health",
                    "unrelated_sentinel")
AUTOSTART_SELECTION_FILE = "astrid-install.json"
AUTOSTART_DEVICE_SCALE_FACTOR = 2
# The approved card sentence, the App's own copy (installerUpdateReasons.ts, autostart-off).
AUTOSTART_CARD_TEXT = ("Start at sign-in is off, so update from a terminal. This command updates Astrid and starts it; "
                       "your choice stays off.")
AUTOSTART_ORIGIN = "https://astrid.haus"
AUTOSTART_ENTRY = "/tools/video-editor?settings=updates"
AUTOSTART_CAPTURE_SCRIPT = journey.ROOT / "scripts" / "journey_autostart_capture.mjs"
AUTOSTART_CAPTURE_TIMEOUT_MS = 120000
UPDATE_STATUS_PATH = "/update/status"
UPDATE_PATH = "/update"
DIGEST_PATTERN = re.compile(r"[0-9a-f]{64}")


def candidate_install_autostart_off(app: Path, manifest_path: Path, shell: str, evidence: Path,
                                    root: Path, data: Path) -> dict:
    """Run the candidate's public install once with ``--autostart off`` and return what it reported.

    This is a real fresh install, not an update: the autostart choice is recorded as off.
    """
    if sys.platform == "win32":
        raise RuntimeError(f"{AUTOSTART_LEG} is not admitted on Windows")
    env = dict(os.environ)
    env["BROWSER"] = f'"{sys.executable}" -c "pass" %s'
    log = evidence / "install-bootstrap.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    argv = [shell, str(app / "public" / "install"), "--manifest", str(manifest_path),
            "--root", str(root), "--data-root", str(data), "--autostart", "off",
            # A fresh install asks for setup consent and the journey has no terminal, so it
            # accepts the recommended choices as a person would (see ci_journey.bootstrap_flags).
            "--yes"]
    with log.open("w", encoding="utf-8") as stream:
        process = subprocess.run(argv, env=env, stdout=stream, stderr=subprocess.STDOUT,
                                 timeout=3600, check=False)
    lines = [line for line in log.read_text(encoding="utf-8-sig", errors="replace").splitlines() if line.strip()]
    return {"command": "install --autostart off", "exit_code": process.returncode,
            "log": str(log), "last_lines": lines[-8:]}


def runtime_request(method: str, path: str) -> tuple[int, dict]:
    """One request to the live Runtime door, at the harness health prefix. A refusal is returned, not raised."""
    base = journey.HEALTH_URL.rsplit("/health", 1)[0]
    request = urllib.request.Request(base + path, method=method, data=b"" if method == "POST" else None,
                                     headers={"Origin": "https://astrid.haus"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=30) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read() or b"{}")
        except ValueError:
            body = {}
        return exc.code, body


def read_selection() -> tuple[dict, str]:
    """The installer's selection record and its SHA-256, read beside the lock."""
    path = journey.selection_path()
    if path.is_symlink() or not path.is_file():
        raise RuntimeError("installer selection record is absent")
    return journey.read_json(path), hashlib.sha256(path.read_bytes()).hexdigest()


def _hosted_update():
    """The journey's hosted-App intercept module, which the button row also uses."""
    if __package__:
        from . import journey_hosted_update as hosted
    else:
        import journey_hosted_update as hosted
    return hosted


def build_hosted_app(app: Path, evidence: Path) -> Path:
    """The candidate's hosted App, built the way the button row builds it: npm ci, then npm run build:hosted."""
    evidence.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, NODE_OPTIONS="--max-old-space-size=4096")
    env.pop("ASTRID_APP_DIRECT_DOOR_PORT", None)
    log = evidence / "app-build.log"
    with log.open("w", encoding="utf-8") as stream:
        for argv in (["npm", "ci", "--legacy-peer-deps"], ["npm", "run", "build:hosted"]):
            process = subprocess.run(argv, cwd=app, env=env, stdout=stream, stderr=subprocess.STDOUT,
                                     timeout=1800, check=False)
            if process.returncode:
                raise RuntimeError(f"App build failed: {' '.join(argv)} exited {process.returncode}")
    dist = app / "dist"
    if not (dist / "index.html").is_file() or not (dist / "build.json").is_file():
        raise RuntimeError("App build did not produce index.html and build.json")
    return dist


def install_capture_browser(root: Path) -> Path:
    """The pinned Playwright and its Chromium, installed beside the run for the autostart capture only."""
    directory = root.parent / "autostart-capture"
    directory.mkdir(parents=True, exist_ok=True)
    steps = (["npm", "install", "--prefix", str(directory), "--no-audit", "--no-fund", "playwright@1.47.2"],
             [str(directory / "node_modules" / ".bin" / "playwright"), "install", "--with-deps", "chromium"])
    for argv in steps:
        process = subprocess.run(argv, env=dict(os.environ), timeout=1800, check=False)
        if process.returncode:
            raise RuntimeError(f"capture browser install failed: {argv[0]} exited {process.returncode}")
    return directory


def capture_autostart_card(evidence: Path, app: Path, dist: Path, capture_dir: Path) -> dict:
    """Serve the candidate's hosted App at astrid.haus through the intercept, then capture the autostart-off card.

    The page is the one the button row serves, with the candidate's own bootstrap and manifest routes. The
    browser pins the intercept's leaf and waits for the approved card sentence; if the card is not shown the
    capture fails.
    """
    hosted = _hosted_update()
    evidence.mkdir(parents=True, exist_ok=True)
    routes, types = hosted.page_routes(dist)
    routes.update(hosted.release_routes(app, (app / "public" / "install-manifest.json").read_bytes()))
    png, meta = evidence / "autostart-off-card.png", evidence / "autostart-off-capture.json"
    config_path = evidence / "autostart-off-capture-config.json"
    with hosted.Intercept(routes, types=types) as intercept:
        playwright = str(capture_dir / "node_modules" / "playwright" / "index.mjs")
        config = {"playwright": playwright, "origin": AUTOSTART_ORIGIN, "entry": AUTOSTART_ENTRY,
                  "spki": intercept.leaf_spki, "text": AUTOSTART_CARD_TEXT,
                  "dpr": AUTOSTART_DEVICE_SCALE_FACTOR, "screenshot": str(png), "result": str(meta),
                  "timeoutMs": AUTOSTART_CAPTURE_TIMEOUT_MS}
        config_path.write_text(json.dumps(config), encoding="utf-8")
        process = subprocess.run(["node", str(AUTOSTART_CAPTURE_SCRIPT), str(config_path)], env=dict(os.environ),
                                 capture_output=True, text=True,
                                 timeout=AUTOSTART_CAPTURE_TIMEOUT_MS // 1000 + 300, check=False)
    if (process.returncode != 0 or png.is_symlink() or not png.is_file()
            or meta.is_symlink() or not meta.is_file()):
        raise RuntimeError(f"autostart card capture failed: {process.stderr.strip()[-400:]}")
    info = journey.read_json(meta)
    return {"path": png.name, "sha256": hashlib.sha256(png.read_bytes()).hexdigest(),
            "device_scale_factor": info.get("device_scale_factor"), "surface": "autostart-off card",
            "text": info.get("text"), "url": info.get("url")}


def execute_fresh_install_autostart_off(args, root: Path, data: Path, evidence: Path, sentinel, birth: str) -> dict:
    """A real fresh install with --autostart off, checked against the live Runtime and the App capture.

    Four assertions are recorded by name: the selection record says autostart is false; GET update/status
    reports reason autostart-off; a POST update is refused with autostart-off and changes nothing (the
    selection, the seeded data and the live Runtime identity); and the autostart-off card is captured at
    deviceScaleFactor 2.
    """
    evidence.mkdir(parents=True, exist_ok=True)
    result = {
        "schema_version": 1, "receipt_kind": AUTOSTART_LEG,
        "job_id": args.job_id, "mode": args.mode, "scenario": args.leg, "status": "not-run",
        "os_build": platform.platform(), "shell": args.shell, **journey.runner_identity(),
        "source_override_test_mode": False, "candidate_checkouts": getattr(args, "candidate_checkouts", None),
        "service_manager_prerequisite": getattr(args, "service_manager_prerequisite", None),
        "sources": None, "installed_tuple": None, "import_origins": None,
        "run_roots": {"program": str(root), "data": str(data)},
        "sentinel": {"pid": sentinel.pid, "birth": birth},
        "install": None, "installed_release": None, "selection": None, "update_status": None,
        "post_update": None, "capture": None,
        "checks": {key: "not-run" for key in AUTOSTART_CHECKS},
    }
    result_path = evidence / "journey-result.json"
    journey.write_json(result_path, result)
    try:
        app = args.app_checkout.resolve(strict=True)
        candidate_path, manifest = journey.selected_manifest(
            app, source_override=False, agent=None, runtime=None, evidence=evidence)
        candidate = journey.read_json(args.candidate)
        if evidence_gate.validate_tuple(candidate, app, candidate_path) != manifest:
            raise RuntimeError(f"{AUTOSTART_LEG} requires a frozen candidate manifest")
        result["sources"] = {"app": journey.git_source(app), "agent": manifest["agent"],
                             "runtime": manifest["runtime"]}
        before_sentinel = journey.sentinel_observation(sentinel, birth)
        result["sentinel"]["before_lifecycle"] = before_sentinel
        if not before_sentinel["alive"]:
            raise RuntimeError("Unrelated sentinel exited before the autostart-off install")

        # 1. The real fresh install with the autostart choice off.
        install = result["install"] = candidate_install_autostart_off(
            app, candidate_path, args.shell, evidence / "install", root, data)
        if install["exit_code"] != 0:
            raise RuntimeError(f"install --autostart off exited {install['exit_code']}")
        result["checks"]["install_exit"] = "pass"
        start_direct_runtime(evidence / "install")
        journey.wait_ready(selected_data_root(root, data))
        version_path, selected_data, version = journey.installation(root, data)
        result["installed_release"] = installed_release(version)
        if result["installed_release"] != manifest.get("release"):
            raise RuntimeError(f"fresh install selected release {result['installed_release']}, "
                               f"not the candidate release {manifest.get('release')}")
        # Autostart off deliberately runs a detached Runtime with no sign-in registration, so this
        # leg alone replaces the direct service-manager check (and its foreground ban).
        def detached(check_root: Path, owner_data: Path) -> dict:
            return journey.assert_detached_autostart_off_runtime(check_root, owner_data, evidence)

        result["service_manager"] = detached(root, selected_data)
        result["checks"]["service"] = "pass"
        selected = journey.selected_tuple(manifest, version)
        result["installed_tuple"] = {"app": result["sources"]["app"]["sha"], **selected}

        # 2. The installer's selection record says autostart is off.
        record, selection_digest = read_selection()
        choices = record.get("choices") if isinstance(record.get("choices"), dict) else {}
        result["selection"] = {"file": AUTOSTART_SELECTION_FILE, "choices_autostart": choices.get("autostart"),
                               "sha256": selection_digest}
        if choices.get("autostart") is not False:
            raise RuntimeError("selection record does not have choices.autostart false")
        result["checks"]["selection_autostart_false"] = "pass"

        # 3. Seed the workspace, so the refusal is checked against real data.
        project = "tst-autostart-off"
        baseline = evidence / "autostart-seed"
        keys = live_workspace(root, selected_data, baseline, "seed", {
            "project": project, "generation": "autostart-off", "task_id": None, "create_project": True},
            ownership=detached)
        keys["task_id"] = task_after_update(version, baseline / "autostart-task", project)
        before = live_workspace(root, selected_data, baseline, "snapshot", keys, ownership=detached)
        before_health = journey.healthy(selected_data)

        # 4. The live Runtime reports the autostart-off reason on its update status.
        status_code, status_body = runtime_request("GET", UPDATE_STATUS_PATH)
        result["update_status"] = {"method": "GET", "path": UPDATE_STATUS_PATH, "status": status_code,
                                   "reason": status_body.get("reason")}
        if status_body.get("reason") != "autostart-off":
            raise RuntimeError("GET /update/status does not report reason autostart-off")
        result["checks"]["update_status_autostart_off"] = "pass"

        # 5. A POST update is refused with autostart-off and changes nothing.
        post_code, post_body = runtime_request("POST", UPDATE_PATH)
        after_digest = read_selection()[1]
        after_health = journey.healthy(selected_data)
        after = live_workspace(root, selected_data, evidence / "autostart-after", "snapshot", keys,
                               ownership=detached)
        semantic_keys = ("project", "timeline", "preferences", "task", "cas_sha256")
        data_same = (semantic_digest({k: before[k] for k in semantic_keys})
                     == semantic_digest({k: after[k] for k in semantic_keys}))
        refused = type(post_code) is int and post_code >= 400 and post_body.get("reason") == "autostart-off"
        unchanged = (after_digest == selection_digest
                     and after_health["runtime_instance_id"] == before_health["runtime_instance_id"] and data_same)
        result["post_update"] = {"method": "POST", "path": UPDATE_PATH, "status": post_code,
                                 "reason": post_body.get("reason"), "refused": refused, "unchanged": unchanged}
        if not (refused and unchanged):
            raise RuntimeError("POST update was not refused with autostart-off, or it changed the selection, "
                               "the data or the live Runtime")
        result["checks"]["post_update_refused_unchanged"] = "pass"
        result["checks"]["health"] = "pass"

        # The gating assertions are on disk before the App build, so a failing or slow build cannot lose them.
        journey.write_json(result_path, result)

        # 7. The App is built, served at astrid.haus through the intercept, and the card captured at
        #    deviceScaleFactor 2. On macOS an unverified build or capture is recorded; the assertions above gate.
        gated = sys.platform != "darwin"
        try:
            dist = build_hosted_app(app, evidence / "app-build")
            capture_dir = install_capture_browser(root)
            capture = result["capture"] = capture_autostart_card(evidence / "capture", app, dist, capture_dir)
        except Exception as exc:
            if gated:
                raise
            result["capture"] = {"status": "unverified", "reason": "macOS App build or capture not verified in CI",
                                 "error": f"{type(exc).__name__}: {exc}"}
            result["checks"]["autostart_card_capture"] = "unverified"
        else:
            if (capture.get("device_scale_factor") != AUTOSTART_DEVICE_SCALE_FACTOR
                    or not DIGEST_PATTERN.fullmatch(str(capture.get("sha256", "")))):
                raise RuntimeError("autostart card capture is absent or not at deviceScaleFactor 2")
            result["checks"]["autostart_card_capture"] = "pass"

        # 8. The unrelated sentinel survives the whole leg.
        settled = journey.sentinel_observation(sentinel, birth)
        result["sentinel"]["at_settlement"] = settled
        if not settled["alive"]:
            raise RuntimeError("Unrelated sentinel did not survive the autostart-off install")
        result["checks"]["unrelated_sentinel"] = "pass"
        result["status"] = "pass"
    except Exception as exc:
        result.update(status="fail", error=f"{type(exc).__name__}: {exc}")
        raise
    finally:
        journey.write_json(result_path, result)
    return result


WINDOWS_REFUSAL = "Upgrading from an earlier Windows version isn't supported yet. Your data was kept; reinstall to get the new version."


def windows_task_definition(evidence: Path, phase: str) -> str:
    """Export configuration only; do not query task readiness or process ownership."""
    journey.windows.EVIDENCE = evidence
    xml = journey.windows.powershell(
        "$ErrorActionPreference='Stop'; Export-ScheduledTask -TaskName 'Astrid Runtime' -TaskPath '\\'",
        f"scheduled-task-{phase}.xml", show=False, timeout=30)
    if not xml.strip():
        raise RuntimeError("R0 scheduled task definition is absent")
    return hashlib.sha256(xml.encode("utf-8")).hexdigest()


def execute_windows_refusal(args, root: Path, data: Path, evidence: Path, sentinel, birth: str) -> dict:
    """Check refusal against an authentic R0 without exercising its runtime."""
    result = {"schema_version": 1, "receipt_kind": "windows-upgrade-refusal",
              "job_id": args.job_id, "scenario": args.leg, "mode": args.mode, "status": "not-run",
              "shell": args.shell, **journey.runner_identity(), "os_build": platform.platform(),
              "source_override_test_mode": False, "candidate_checkouts": args.candidate_checkouts,
              "service_manager_prerequisite": args.service_manager_prerequisite,
              "checks": {key: "not-run" for key in ("refusal", "data_marker", "r0_selected", "scheduled_task")}}
    path = evidence / "journey-result.json"
    journey.write_json(path, result)
    try:
        fixture, r0_manifest, result["release0_fixture"] = release0_admission(args)
        app = args.app_checkout.resolve(strict=True)
        candidate_path, manifest = journey.selected_manifest(app, source_override=False, agent=None, runtime=None, evidence=evidence)
        candidate = journey.read_json(args.candidate)
        if evidence_gate.validate_tuple(candidate, app, candidate_path) != manifest:
            raise RuntimeError("Refusal requires the frozen candidate")
        # The refusal is evidence about the earlier Windows version, whatever release the candidate is.
        if type(manifest.get("release")) is not int or manifest["release"] < 0:
            raise RuntimeError("Refusal requires a frozen candidate with an integer release")
        bootstrap_release0(fixture, r0_manifest, args.shell, evidence / "release0", root, data)
        old_path, old_data, old = journey.installation(root, data)
        if old_data != data or installed_release(old) != 0:
            raise RuntimeError("Frozen bootstrap did not select R0")
        journey.selected_tuple(journey.read_json(r0_manifest), old)
        # A run-owned marker proves a concrete file in the data root survived.
        # It does not claim semantic coverage of an active workspace.
        data.mkdir(parents=True, exist_ok=True)
        marker = data / "tst-windows-refusal-marker.txt"
        marker_value = secrets.token_hex(32)
        with marker.open("x", encoding="ascii") as stream:
            stream.write(marker_value)
        before_task = windows_task_definition(evidence, "before")
        env = {k: v for k, v in os.environ.items() if k.casefold() != "psmodulepath"}
        env["BROWSER"] = f'"{sys.executable}" -c "pass" %s'
        log = evidence / "refusal-update-bootstrap.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("w", encoding="utf-8") as stream:
            process = subprocess.run([args.shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File",
                                      str(app / "public" / "install.ps1"), "--manifest", str(candidate_path),
                                      "--root", str(root), "--data-root", str(data), "--update"],
                                     env=env, stdout=stream, stderr=subprocess.STDOUT, timeout=3600, check=False)
        output = log.read_text(encoding="utf-8-sig", errors="replace")
        if process.returncode == 0 or WINDOWS_REFUSAL not in output.splitlines():
            raise RuntimeError("Candidate update did not fail with the required Windows refusal")
        result["refusal"] = {"exit_code": process.returncode, "message": WINDOWS_REFUSAL}
        result["checks"]["refusal"] = "pass"
        selected_path, selected_data, selected = journey.installation(root, data)
        if selected_path != old_path or selected_data != old_data or installed_release(selected) != 0:
            raise RuntimeError("Refusal did not retain the selected R0 installation and data root")
        journey.selected_tuple(journey.read_json(r0_manifest), selected)
        result["checks"]["r0_selected"] = "pass"
        if marker.is_symlink() or not marker.is_file() or marker.read_text(encoding="ascii") != marker_value:
            raise RuntimeError("Refusal did not retain the data marker")
        result["data_marker"] = {"sha256": hashlib.sha256(marker_value.encode("ascii")).hexdigest()}
        result["checks"]["data_marker"] = "pass"
        after_task = windows_task_definition(evidence, "after")
        if before_task != after_task:
            raise RuntimeError("Refusal changed the R0 scheduled task definition")
        result["scheduled_task"] = {"before_sha256": before_task, "after_sha256": after_task}
        result["checks"]["scheduled_task"] = "pass"
        result["status"] = "pass"
    except Exception as exc:
        result.update(status="fail", error=f"{type(exc).__name__}: {exc}")
        raise
    finally:
        journey.write_json(path, result)
    return result


def execute_owned(args, root: Path, data: Path, evidence: Path, sentinel, birth: str) -> dict:
    if args.leg == "windows-upgrade-refusal":
        return execute_windows_refusal(args, root, data, evidence, sentinel, birth)
    if args.leg == R0_HANDOFF_LEG:
        if sys.platform == "win32":
            raise RuntimeError("Windows release-0 upgrade only admits the narrowed refusal leg")
        return execute_r0_handoff_then_update(args, root, data, evidence, sentinel, birth)
    if args.leg == "release9-then-install":
        if sys.platform == "win32":
            raise RuntimeError("Windows release-0 upgrade only admits the narrowed refusal leg")
        return execute_release9_then_install(args, root, data, evidence, sentinel, birth)
    if args.leg == RELEASE11_LEG:
        if sys.platform == "win32":
            raise RuntimeError("Windows release-0 upgrade only admits the narrowed refusal leg")
        return execute_release11_then_update(args, root, data, evidence, sentinel, birth)
    if args.leg == AUTOSTART_LEG:
        if sys.platform == "win32":
            raise RuntimeError("Windows release-0 upgrade only admits the narrowed refusal leg")
        return execute_fresh_install_autostart_off(args, root, data, evidence, sentinel, birth)
    evidence.mkdir(parents=True, exist_ok=True)
    result = {"schema_version": 1, "scenario": args.leg, "status": "not-run",
              "os_build": platform.platform(), "shell": args.shell, **journey.runner_identity(),
              "source_override_test_mode": args.source_override,
              "retained_for_next_leg": getattr(args, "retain_for_next_leg", False),
              "sources": None, "installed_tuple": None, "import_origins": None,
              "run_roots": {"program": str(root), "data": str(data)},
              "sentinel": {"pid": sentinel.pid, "birth": birth},
              "checks": {key: "not-run" for key in (
                  "version_changed", "service_action", "workspace_content", "schema_migrated",
                  "task_settled", "unrelated_sentinel", "same_release_rerun",
                  "old_runtime_stopped")}}
    result_path = evidence / "journey-result.json"
    journey.write_json(result_path, result)
    try:
        if sys.platform == "win32":
            raise RuntimeError("TST-B3: historical Windows bootstrap containment is unqualified; "
                               "C1 native Windows lifecycle is written, not admitted")
        app = args.app_checkout.resolve(strict=True)
        candidate_path, manifest = journey.selected_manifest(
            app, source_override=args.source_override, agent=args.agent_checkout,
            runtime=args.runtime_checkout, evidence=evidence)
        release_id = "served" if args.leg == "update-from-served" else "pre-updater"
        exported = evidence / "initial-release"
        release = upgrade_release.resolve(release_id, exported, app_repo=app)
        result["initial_release"] = release
        if release["status"] != "verified":
            raise RuntimeError("Initial release could not be resolved from exact A2 provenance")
        # Exported scripts and manifest are from the resolved historical release.
        old_manifest_path = exported / "install-manifest.json"
        journey.bootstrap(exported, old_manifest_path, args.shell, "install", evidence / "initial", root, data)
        active_data = selected_data_root(root, data)
        journey.wait_ready(active_data)
        old_path, _, old = journey.installation(root, data)
        before_tuple = journey.selected_tuple(journey.read_json(old_manifest_path), old)
        old_owner = journey.healthy(active_data)
        old_pid = old_owner["pid"]
        old_birth = journey.process_birth(old_pid)
        if not old_birth:
            raise RuntimeError("Cannot establish the historical Runtime process identity")
        baseline = evidence / "baseline"
        baseline.mkdir()
        journey.verify("observe", old_path, data, baseline)
        project = "tst-c1-before"
        journey.task(old, baseline, project)
        task_id = journey.read_json(baseline / "task-settled.json")["data"]["task_id"]
        keys = workspace(old, data, baseline, "seed", {"project": project, "generation": "before",
                                                       "task_id": task_id})
        before = workspace(old, data, baseline, "snapshot", keys)
        if args.leg == "update-over-old-declaration":
            result["old_declaration"] = seed_old_declaration(old, root, args.old_declaration)
        if args.leg == "old-runtime-busy":
            busy_refusal(args, root, data, evidence, candidate_path, old,
                         old_pid, old_birth, sentinel, birth, result)
            result["checks"]["workspace_content"] = "pass"
            assert_preserved(before, workspace(old, data, evidence / "after-busy", "snapshot", keys))
            result["status"] = "pass"
            return result
        result["sources"] = {"app": journey.git_source(app), "agent": manifest["agent"],
                             "runtime": manifest["runtime"]}
        journey.bootstrap(app, candidate_path, args.shell, "update", evidence, root, data)
        active_data = selected_data_root(root, data)
        journey.wait_ready(active_data)
        version_path, _, version = journey.installation(root, data)
        result["installed_tuple"] = {"app": result["sources"]["app"]["sha"],
                                     **selected_candidate(manifest, version, before_tuple)}
        if version_path == old_path or journey.healthy(active_data)["runtime_instance_id"] == old_owner["runtime_instance_id"]:
            raise RuntimeError("The update did not activate a new version and Runtime instance")
        if journey.process_birth(old_pid) == old_birth:
            raise RuntimeError("The real historical Runtime survived the candidate update")
        result["checks"]["version_changed"] = "pass"
        result["checks"]["old_runtime_stopped"] = "pass"
        journey.verify("update", version_path, active_data, evidence, before=baseline / "verify-observe.json")
        # C1 needs fully healthy candidate/chat observations, including stale declarations.
        journey.verify("install", version_path, active_data, evidence)
        result["import_origins"] = journey.read_json(evidence / "verify-install.json")["details"].get("origin.imports")
        result["service_action"] = service_action(root, data, version, old)
        result["checks"]["service_action"] = "pass"
        after = workspace(version, active_data, evidence / "after-update", "snapshot", keys)
        assert_preserved(before, after)
        result["schema"] = schema_summary(before, after)
        result["checks"]["workspace_content"] = result["checks"]["schema_migrated"] = "pass"
        journey.task(version, evidence / "candidate-task", "tst-c1-after")
        result["checks"]["task_settled"] = "pass"
        rerun(args, root, data, evidence / "rerun", app, candidate_path, manifest, version, keys)
        result["checks"]["same_release_rerun"] = "pass"
        if not journey.sentinel_alive(sentinel, birth):
            raise RuntimeError("Unrelated sentinel did not survive version update and rerun")
        result["checks"]["unrelated_sentinel"] = "pass"
        result["status"] = "pass"
    except Exception as exc:
        result.update(status="fail", error=f"{type(exc).__name__}: {exc}")
        raise
    finally:
        journey.write_json(result_path, result)
    return result


def rerun(args, root, data, evidence, app, manifest_path, manifest, version, old_keys):
    evidence.mkdir(parents=True, exist_ok=True)
    active_data = selected_data_root(root, data)
    journey.task(version, evidence, "tst-c1-fresh-user-changes")
    task_id = journey.read_json(evidence / "task-settled.json")["data"]["task_id"]
    keys = workspace(version, active_data, evidence, "seed", {"project": "tst-c1-fresh-user-changes",
                                                      "generation": "fresh", "task_id": task_id})
    # Preferences are shared user content: capture both sets after the new edit.
    before_old = workspace(version, active_data, evidence / "before-old", "snapshot", old_keys)
    before_fresh = workspace(version, active_data, evidence / "before-fresh", "snapshot", keys)
    selected_path, _, _ = journey.installation(root, data)
    journey.bootstrap(app, manifest_path, args.shell, "install", evidence, root, data)
    active_data = selected_data_root(root, data)
    journey.wait_ready(active_data)
    after_path, _, after = journey.installation(root, data)
    if after_path != selected_path or after.get("identity") != version.get("identity"):
        raise RuntimeError("Same-release rerun unexpectedly changed selected version")
    journey.selected_tuple(manifest, after)
    journey.verify("install", after_path, active_data, evidence)
    service_action(root, data, after)
    assert_preserved(before_old, workspace(after, active_data, evidence / "after-old", "snapshot", old_keys))
    assert_preserved(before_fresh, workspace(after, active_data, evidence / "after-fresh", "snapshot", keys))
    journey.task(after, evidence / "settlement", "tst-c1-rerun-after")


def busy_refusal(args, root, data, evidence, manifest_path, old,
                 old_pid, old_birth, sentinel, birth, result):
    realm = journey.workspace_root(data)
    identity = journey_workspace.realm_identity(realm)
    operation = {"id": secrets.token_hex(32), "lease_token": secrets.token_hex(32)}
    def cleanup():
        workspace(old, data, evidence / "busy-release", "release",
                  {"operation": operation, "realm_identity": identity}, realm=realm)
    try:
        keys = workspace(old, data, evidence / "busy", "busy",
                         {"operation": operation, "realm_identity": identity}, realm=realm)
        protected = {path: path.read_bytes() for path in (root / "current", Path(old["root"]) / "version.json",
                                                         data / "runtime/discovery.json")}
        try:
            journey.bootstrap(args.app_checkout, manifest_path, args.shell, "update", evidence / "busy", root, data)
        except RuntimeError:
            log = (evidence / "busy/update-bootstrap.log").read_text(encoding="utf-8-sig", errors="replace")
            if "busy" not in log.lower() or keys["task_id"] not in log:
                raise RuntimeError("Busy update refusal omitted task-specific guidance")
        else:
            raise RuntimeError("Candidate updated a busy real historical Runtime")
        if journey.process_birth(old_pid) != old_birth:
            raise RuntimeError("Busy historical Runtime exited or was replaced")
        if any(path.read_bytes() != value for path, value in protected.items()):
            raise RuntimeError("Busy refusal mutated selection/version/discovery")
        if not journey.sentinel_alive(sentinel, birth):
            raise RuntimeError("Busy refusal disturbed the unrelated sentinel")
    except BaseException as primary:
        try:
            cleanup()
        except Exception as cleanup_error:
            result["busy_cleanup_error"] = f"{type(cleanup_error).__name__}: {cleanup_error}"
            raise primary from cleanup_error
        raise
    else:
        try:
            cleanup()
        except Exception as cleanup_error:
            result["busy_cleanup_error"] = f"{type(cleanup_error).__name__}: {cleanup_error}"
            raise
        result["checks"].update(busy_refusal="pass", unrelated_sentinel="pass")
