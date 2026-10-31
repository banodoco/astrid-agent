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
import sys

if __package__:
    from . import check_journey_evidence as evidence_gate
    from . import ci_journey as journey, upgrade_release
else:
    import check_journey_evidence as evidence_gate
    import ci_journey as journey
    import upgrade_release


RELEASE0_FIXTURE = journey.ROOT / "tests" / "fixtures" / "release0"
RELEASE0_SUMS_SHA256 = "58221a50aa7940e3121d6239ddeb92d6409df3711096aac1161c7ec130b755c0"
RELEASE0_REFS = {
    "agent": "3204bb00135bec8ee2d2d3fb21eed5abfceca649",
    "runtime": "6d53a19070faf63fc15ae1f81417722edc2c8a5f",
}
RELEASE0_REQUIRED = frozenset({
    "provenance.json", "served/install", "served/install.ps1", "served/install-manifest.json",
})
CHECKSUM = re.compile(r"([0-9a-f]{64})  ([A-Za-z0-9][A-Za-z0-9._/-]*)$")


def workspace(version: dict, data: Path, evidence: Path, action: str, request: dict) -> dict:
    evidence.mkdir(parents=True, exist_ok=True)
    request_path = evidence / (action + "-request.json")
    journey.write_json(request_path, request)
    env = {key: value for key, value in os.environ.items() if key not in {"PYTHONPATH", "PYTHONHOME"}}
    env.update(version["env"])
    output = journey.run([version["python"], "-I", journey.ROOT / "scripts/journey_workspace.py",
                          action, "--run-root", data.parent, "--realm", journey.workspace_root(data),
                          "--request", request_path], evidence / (action + ".json"), env=env, timeout=120)
    return json.loads(output)


def assert_preserved(before: dict, after: dict) -> None:
    for key in ("project", "timeline", "preferences", "task", "cas_sha256"):
        if key not in before or key not in after:
            raise RuntimeError("Update did not retain seeded workspace " + key)
        if before[key] != after[key]:
            raise RuntimeError("Update changed seeded workspace " + key)
    if after["schema_version"] != after["target_schema_version"]:
        raise RuntimeError("Selected realm was not migrated to the candidate schema")


def service_action(root: Path, data: Path, version: dict, old: dict | None = None) -> dict:
    selection = journey.read_json(journey.program_root().parent / "astrid-install.json")
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
        raise ValueError("release0-to-release1 requires the checked-in frozen release-0 fixture")
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
        journey.run([shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File",
                     fixture / "served" / "install.ps1", *flags], evidence / "release0-bootstrap.log", env=env)
    else:
        journey.run([shell, fixture / "served" / "install", *flags],
                    evidence / "release0-bootstrap.log", env=env)


def start_direct_runtime(evidence: Path) -> None:
    if sys.platform == "win32":
        journey.windows.EVIDENCE = evidence
        journey.windows.powershell("Start-ScheduledTask -TaskName 'Astrid Runtime' -TaskPath '\\'",
                                   "start-scheduled-task.log", show=False, timeout=30)


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
    if (getattr(args, "mode", None) != "direct" or not getattr(args, "job_id", None)
            or not getattr(args, "candidate", None) or not getattr(args, "allow_release0_to_release1", False)
            or getattr(args, "release0_fixture", None) is None):
        raise ValueError("release0-to-release1 requires its scoped frozen-fixture direct admission")
    if getattr(args, "source_override", False):
        raise ValueError("release0-to-release1 never admits a source override")
    return verified_release0_fixture(args.release0_fixture)


def execute_release0_to_release1(args, root: Path, data: Path, evidence: Path, sentinel, birth: str) -> dict:
    """Authentic release-0 direct install, API seed, and frozen release-1 update."""
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
        "release0_fixture": None, "release_transition": None, "semantic_data": None,
        "checks": {key: "not-run" for key in (
            "release", "service", "health", "semantic_data", "task_settled", "one_runtime",
            "unrelated_sentinel")},
    }
    result_path = evidence / "journey-result.json"
    journey.write_json(result_path, result)
    try:
        fixture, release0_manifest, fixture_receipt = release0_admission(args)
        result["release0_fixture"] = fixture_receipt
        app = args.app_checkout.resolve(strict=True)
        candidate_path, manifest = journey.selected_manifest(
            app, source_override=False, agent=None, runtime=None, evidence=evidence)
        candidate = journey.read_json(args.candidate)
        if evidence_gate.validate_tuple(candidate, app, candidate_path) != manifest or manifest.get("release") != 1:
            raise RuntimeError("release0-to-release1 requires a frozen release-1 manifest")
        result["candidate_bootstrap"] = {"manifest_sha256": candidate["manifest_sha256"],
                                         "artifacts": candidate["artifacts"]}
        result["sources"] = {"app": journey.git_source(app), "agent": manifest["agent"],
                             "runtime": manifest["runtime"]}
        before_sentinel = journey.sentinel_observation(sentinel, birth)
        result["sentinel"]["before_lifecycle"] = before_sentinel
        if not before_sentinel["alive"]:
            raise RuntimeError("Unrelated sentinel exited before the release-0 bootstrap")
        bootstrap_release0(fixture, release0_manifest, args.shell, evidence / "release0", root, data)
        start_direct_runtime(evidence / "release0")
        journey.wait_ready(data)
        old_path, old_data, old = journey.installation(root, data)
        if old_data != data or installed_release(old) != 0:
            # Release 0 predates identity-bearing version records; only its
            # frozen manifest can attest the old release identity.
            raise RuntimeError("Frozen release-0 bootstrap did not select release 0")
        old_tuple = journey.selected_tuple(journey.read_json(release0_manifest), old)
        old_service = journey.assert_direct_service_ownership(root, data)
        old_health = journey.healthy(data)
        old_pid = int(old_health["pid"])
        old_birth = journey.process_birth(old_pid)
        if not old_birth:
            raise RuntimeError("Cannot establish the release-0 Runtime process identity")
        journey.assert_one_runtime(data)
        baseline = evidence / "release0-api-seed"
        journey.verify("observe", old_path, data, baseline)
        project = "tst-m4-3b-release0"
        keys = workspace(old, data, baseline, "seed", {
            "project": project, "generation": "release0", "task_id": None, "create_project": True})
        # The API is authoritative for the project identity.  Exercise both
        # task legs against that exact seeded project, not merely its requested
        # slug.
        project = keys["project"]
        keys["task_id"] = task_after_update(old, baseline / "release0-task", project)
        before = workspace(old, data, baseline, "snapshot", keys)
        journey.bootstrap(app, candidate_path, args.shell, "update", evidence / "release1", root, data)
        start_direct_runtime(evidence / "release1")
        journey.wait_ready(data)
        version_path, selected_data, version = journey.installation(root, data)
        if selected_data != old_data or version_path == old_path:
            raise RuntimeError("Release-1 update did not retain data and activate a new version")
        selected = selected_candidate(manifest, version, old_tuple)
        if installed_release(version) != 1:
            raise RuntimeError("Release-1 update did not select release 1")
        new_service = journey.assert_direct_service_ownership(root, data)
        new_health = journey.healthy(data)
        if new_health["runtime_instance_id"] == old_health["runtime_instance_id"]:
            raise RuntimeError("Release-1 update retained the release-0 Runtime instance")
        if journey.process_birth(old_pid) == old_birth:
            raise RuntimeError("Release-0 Runtime survived the direct release-1 update")
        result["release_transition"] = {"from": 0, "to": 1,
                                        "harness_override": "frozen-release0-direct-to-release1"}
        result["service_transition"] = {"release0": old_service, "release1": new_service}
        result["service_manager"] = new_service
        result["health_transition"] = {
            "release0_runtime_instance_id": old_health["runtime_instance_id"],
            "release1_runtime_instance_id": new_health["runtime_instance_id"],
        }
        result["checks"].update(release="pass", service="pass", health="pass")
        result["installed_tuple"] = {"app": result["sources"]["app"]["sha"], **selected}
        journey.verify("update", version_path, data, evidence, before=baseline / "verify-observe.json")
        journey.verify("install", version_path, data, evidence)
        result["import_origins"] = (journey.read_json(evidence / "verify-install.json").get("details") or {}).get("origin.imports")
        after = workspace(version, data, evidence / "release1-api-observe", "snapshot", keys)
        assert_preserved(before, after)
        before_sha, after_sha = semantic_digest(before), semantic_digest(after)
        if before_sha != after_sha:
            raise RuntimeError("Release-1 semantic snapshot digest differs after exact preservation")
        result["semantic_data"] = {"identical": True, "before_sha256": before_sha, "after_sha256": after_sha}
        result["checks"]["semantic_data"] = "pass"
        task_after_update(version, evidence / "release1-working-task", project)
        result["checks"]["task_settled"] = "pass"
        journey.assert_one_runtime(data)
        result["checks"]["one_runtime"] = "pass"
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


def execute_owned(args, root: Path, data: Path, evidence: Path, sentinel, birth: str) -> dict:
    if args.leg == "release0-to-release1":
        # This lifts only the legacy C1 Windows harness refusal: the admitted
        # path has exact release-0 bytes, an exact release-1 candidate, direct
        # ownership and native-manager checks. The historical resolver below
        # remains refused on Windows.
        return execute_release0_to_release1(args, root, data, evidence, sentinel, birth)
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
                  "task_settled", "one_runtime", "unrelated_sentinel", "same_release_rerun",
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
        journey.wait_ready(data)
        old_path, _, old = journey.installation(root, data)
        before_tuple = journey.selected_tuple(journey.read_json(old_manifest_path), old)
        journey.assert_one_runtime(data)
        old_owner = journey.healthy(data)
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
        journey.wait_ready(data)
        version_path, _, version = journey.installation(root, data)
        result["installed_tuple"] = {"app": result["sources"]["app"]["sha"],
                                     **selected_candidate(manifest, version, before_tuple)}
        if version_path == old_path or journey.healthy(data)["runtime_instance_id"] == old_owner["runtime_instance_id"]:
            raise RuntimeError("The update did not activate a new version and Runtime instance")
        if journey.process_birth(old_pid) == old_birth:
            raise RuntimeError("The real historical Runtime survived the candidate update")
        result["checks"]["version_changed"] = "pass"
        result["checks"]["old_runtime_stopped"] = "pass"
        journey.verify("update", version_path, data, evidence, before=baseline / "verify-observe.json")
        # C1 needs fully healthy candidate/chat observations, including stale declarations.
        journey.verify("install", version_path, data, evidence)
        result["import_origins"] = journey.read_json(evidence / "verify-install.json")["details"].get("origin.imports")
        result["service_action"] = service_action(root, data, version, old)
        result["checks"]["service_action"] = "pass"
        after = workspace(version, data, evidence / "after-update", "snapshot", keys)
        assert_preserved(before, after)
        result["schema"] = {"before": before["schema_version"], "after": after["schema_version"],
                            "target": after["target_schema_version"]}
        result["checks"]["workspace_content"] = result["checks"]["schema_migrated"] = "pass"
        journey.task(version, evidence / "candidate-task", "tst-c1-after")
        result["checks"]["task_settled"] = "pass"
        rerun(args, root, data, evidence / "rerun", app, candidate_path, manifest, version, keys)
        result["checks"]["same_release_rerun"] = "pass"
        journey.assert_one_runtime(data)
        result["checks"]["one_runtime"] = "pass"
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
    journey.task(version, evidence, "tst-c1-fresh-user-changes")
    task_id = journey.read_json(evidence / "task-settled.json")["data"]["task_id"]
    keys = workspace(version, data, evidence, "seed", {"project": "tst-c1-fresh-user-changes",
                                                      "generation": "fresh", "task_id": task_id})
    # Preferences are shared user content: capture both sets after the new edit.
    before_old = workspace(version, data, evidence / "before-old", "snapshot", old_keys)
    before_fresh = workspace(version, data, evidence / "before-fresh", "snapshot", keys)
    selected_path, _, _ = journey.installation(root, data)
    journey.bootstrap(app, manifest_path, args.shell, "install", evidence, root, data)
    journey.wait_ready(data)
    after_path, _, after = journey.installation(root, data)
    if after_path != selected_path or after.get("identity") != version.get("identity"):
        raise RuntimeError("Same-release rerun unexpectedly changed selected version")
    journey.selected_tuple(manifest, after)
    journey.verify("install", after_path, data, evidence)
    service_action(root, data, after)
    assert_preserved(before_old, workspace(after, data, evidence / "after-old", "snapshot", old_keys))
    assert_preserved(before_fresh, workspace(after, data, evidence / "after-fresh", "snapshot", keys))
    journey.task(after, evidence / "settlement", "tst-c1-rerun-after")


def busy_refusal(args, root, data, evidence, manifest_path, old,
                 old_pid, old_birth, sentinel, birth, result):
    keys = workspace(old, data, evidence / "busy", "busy", {})
    protected = {path: path.read_bytes() for path in (root / "current", Path(old["root"]) / "version.json",
                                                     data / "runtime/discovery.json")}
    try:
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
        journey.assert_one_runtime(data)
        result["checks"].update(busy_refusal="pass", one_runtime="pass", unrelated_sentinel="pass")
    finally:
        workspace(old, data, evidence / "busy-release", "release", keys)
