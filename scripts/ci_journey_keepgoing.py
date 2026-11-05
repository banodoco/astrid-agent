"""Disposable direct lifecycle diagnostic. Its receipts are never proof."""
from pathlib import Path
import json
import os
import stat
import sys
import time

if __package__:
    from . import ci_journey as journey, journey_version_jumps as jumps
    from . import check_journey_evidence as gate
else:
    import ci_journey as journey
    import journey_version_jumps as jumps
    import check_journey_evidence as gate

PROOF_MODE = "keep-going-diagnostic"
STAGES = ("admission", "release0-bootstrap", "release0-start", "release0-selection",
          "release0-verify", "release0-seed", "release0-task", "release0-snapshot",
          "release0-owner", "release1-update", "release1-start", "release1-selection",
          "release1-verify-update", "release1-verify-install", "release1-snapshot",
          "semantic-preservation", "release1-task", "one-runtime", "sentinel")


def stages_for(args):
    stages = list(FRESH_STAGES if args.leg == "fresh-install" else STAGES)
    if getattr(args, "diagnostic_prestop_r0", False):
        stages.insert(stages.index("release1-update"), "release0-prestop")
        index = stages.index("release1-selection") + 1
        stages[index:index] = ["release1-release", "release1-service", "release1-health"]
    return stages


FRESH_STAGES = ("admission", "release1-install", "release1-start", "release1-selection",
                "release1-verify-install", "release1-task", "one-runtime", "sentinel")


def initial(evidence, args):
    return {"schema_version": 1, "proof_mode": PROOF_MODE, "classification": "NON-PROOF",
            "qualification": {"eligible": False}, "job_id": args.job_id,
            "scenario": args.leg, "status": "fail", "stages": [],
            "evidence_root": str(evidence),
            "diagnostic_prestop_r0": getattr(args, "diagnostic_prestop_r0", False)}


def write(evidence, report):
    evidence.mkdir(parents=True, exist_ok=True)
    journey.write_json(evidence / "keepgoing-report.json", report)
    # A diagnostic receipt must never look like an eligible successful row.
    result_path = evidence / "journey-result.json"
    previous = journey.read_json(result_path) if result_path.is_file() else {}
    journey.write_json(result_path, {**previous, **report, "receipt_kind": "diagnostic-only", "status": "fail"})
    real = [r for r in report["stages"] if r.get("failure_class") == "real"]
    suspect = [r for r in report["stages"] if str(r.get("failure_class", "")).startswith("suspect:")]
    lines = ["NON-PROOF: keep-going diagnostic", "", "REAL failures: " + (", ".join(r["stage"] for r in real) or "none"),
             "SUSPECT failures: " + (", ".join(r["stage"] + " (" + r["failure_class"] + ")" for r in suspect) or "none"),
             "", "| stage | result | forced-continuation-used | first error |", "| --- | --- | --- | --- |"]
    for row in report["stages"]:
        error = (row.get("first_error") or "").replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {row['stage']} | {row['result']} | {row['forced_continuation_used']} | {error} |")
    lines.extend(["", "| stage | needs | source | result | class |", "| --- | --- | --- | --- | --- |"])
    for row in report["stages"]:
        needs = row.get("preconditions", [])
        names = ", ".join(item["name"] for item in needs) or "none"
        sources = ", ".join(item["source"] for item in needs) or "none"
        lines.append(f"| {row['stage']} | {names} | {sources} | {row['result']} | {row.get('failure_class', '-')} |")
    lines.extend(["", "```mermaid", "graph TD"])
    for index, row in enumerate(report["stages"]):
        lines.append(f'  s{index}["{row["stage"]}"]')
        if row["result"] == "fail":
            lines.append(f"  style s{index} fill:#f99")
    indexes = {row["stage"]: index for index, row in enumerate(report["stages"])}
    for index, row in enumerate(report["stages"]):
        for item in row.get("preconditions", []):
            producer = item.get("producer")
            if producer in indexes:
                edge = "-.->" if item["source"].startswith("forced-by:") else "-->"
                lines.append(f"  s{indexes[producer]} {edge} s{index}")
    lines.append("```")
    (evidence / "keepgoing-report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")



def owner_lock_free(realm: Path, runtime_checkout: Path) -> bool:
    """Probe the existing lifetime lock with Runtime's own OS lock primitive."""
    path = realm / "owner.lock"
    if path.is_symlink() or not path.is_file():
        raise RuntimeError("Selected realm owner.lock is missing or unsafe")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise RuntimeError("Selected realm owner.lock is not a regular file")
        # The candidate Runtime checkout is already verified against the frozen tuple.
        sys.path.insert(0, str(runtime_checkout))
        try:
            from runtime_protocol import platform as native
            if not Path(native.__file__).resolve().is_relative_to(runtime_checkout.resolve(strict=True)):
                raise RuntimeError("Runtime lock primitive did not come from the verified checkout")
        finally:
            sys.path.pop(0)
        try:
            native.lock_file(fd, blocking=False)
        except BlockingIOError:
            return False
        native.unlock_file(fd)
        return True
    finally:
        os.close(fd)


def prestop_r0(args, root, data, evidence, health, pid, birth):
    """Stop only the validated installed service; uncertainty blocks the update."""
    service = journey.assert_direct_service_ownership(root, data)
    journey.assert_one_runtime(data)
    if journey.healthy(data) != health or journey.process_birth(pid) != birth:
        raise RuntimeError("Release-0 owner changed before diagnostic stop")
    realm = journey.workspace_root(data)  # discovery can disappear during stop
    evidence.mkdir(parents=True, exist_ok=True)
    manager = service["manager"]
    if manager == "systemd-user":
        journey.run(["systemctl", "--user", "stop", "astrid.service"], evidence / "prestop-stop.log", timeout=120)
        journey.run(["systemctl", "--user", "disable", "astrid.service"], evidence / "prestop-disable.log", timeout=120)
    elif manager == "launchd":
        journey.run(["launchctl", "bootout", f"gui/{os.getuid()}/haus.astrid.runtime"], evidence / "prestop-stop.log", timeout=120)
    elif manager == "task-scheduler":
        journey.windows.EVIDENCE = evidence
        journey.windows.powershell("$ErrorActionPreference='Stop'; schtasks.exe /End /TN '\\Astrid Runtime'; "
                                   "if ($LASTEXITCODE -ne 0) { throw 'Scheduled task end failed' }; "
                                   "Disable-ScheduledTask -TaskName 'Astrid Runtime' -TaskPath '\\' | Out-Null",
                                   "prestop-task.log", show=False, timeout=120)
    else:
        raise RuntimeError(f"Unsupported direct service manager: {manager}")
    deadline = time.monotonic() + 120
    while True:
        owners = journey.runtime_processes(data)
        if (journey.process_birth(pid) != birth and not owners
                and owner_lock_free(realm, args.candidate_runtime_checkout)):
            return {"manager": manager, "pid": pid, "birth": birth, "realm": str(realm), "owner_lock_free": True}
        if time.monotonic() >= deadline:
            raise RuntimeError("Release-0 owner or realm lock did not stop; update refused")
        time.sleep(1)


def execute(args, root: Path, data: Path, evidence: Path, sentinel, birth: str):
    report = initial(evidence, args)
    state = {}
    project = "tst-m4-3b-release0"
    producers = {"fixture": "admission", "app": "admission", "bootstrap0": "release0-bootstrap",
                 "started0": "release0-start", "old": "release0-selection", "keys": "release0-seed",
                 "task0": "release0-task", "before": "release0-snapshot", "health0": "release0-owner",
                 "updated": "release1-update", "started1": "release1-start", "version": "release1-selection",
                 "after": "release1-snapshot", "verified0": "release0-verify"}
    def stage(name, dependencies, action, path, forced_dependencies=()):
        missing = [item for item in dependencies if item not in state]
        row = {"stage": name, "result": "not-run", "forced_continuation_used": False,
               "first_error": None, "evidence_path": str(path),
               "preconditions": [{"name": key, "producer": producers[key],
                                  "source": "genuine" if key in state else "unavailable"}
                                 for key in dependencies], "postconditions": []}
        for key, producer in forced_dependencies:
            row["preconditions"].append({"name": key, "producer": producer,
                                         "source": "genuine" if key in state else "forced-by:" + producer})
        if missing:
            row.update(result="not-run", first_error="Unsafe or unavailable prerequisites: " + ", ".join(missing))
            tainted_by = sorted({producers[item] for item in missing
                                 if any(previous["stage"] == producers[item] and previous["result"] == "fail"
                                        for previous in report["stages"])})
            if tainted_by:
                row["failure_class"] = "diagnostic-tainted:" + ",".join(tainted_by)
        else:
            try:
                action()
                row["result"] = "pass"
                row["postconditions"] = [key for key, producer in producers.items() if producer == name and key in state]
            except Exception as exc:
                row.update(result="fail", first_error=f"{type(exc).__name__}: {exc}")
                forced = sorted({item["source"].split(":", 1)[1] for item in row["preconditions"]
                                 if item["source"].startswith("forced-by:")})
                row["failure_class"] = "suspect:" + ",".join(forced) if forced else "real"
                # Continue to independent checks only; never force a service or delete a lock marker.
                if "verify" in name:
                    row["forced_continuation_used"] = True
                    row["continuation"] = "FORCED: skip failed verification and attempt independently safe stages"
                else:
                    row["continuation"] = "unsafe service/lock forcing unsupported; dependent stages are not run"
        if path.is_file() and not path.is_symlink() and path.suffix == ".log":
            try:
                artifact_name = f"keepgoing-stage-{len(report['stages']) + 1:02d}.log"
                with path.open("rb") as stream:
                    stream.seek(max(0, path.stat().st_size - journey.EVIDENCE_LOG_LIMIT))
                    (evidence / artifact_name).write_bytes(stream.read(journey.EVIDENCE_LOG_LIMIT))
                row["retained_evidence"] = f"before-cleanup/{artifact_name}"
                row["retained_evidence_note"] = "log tail, at most 1 MiB"
            except OSError as exc:
                row["retained_evidence_error"] = f"{type(exc).__name__}: {exc}"
        report["stages"].append(row)
        write(evidence, report)
    if args.leg == "fresh-install":
        producers.update(app="admission", updated="release1-install", started1="release1-start",
                         version="release1-selection")
        def fresh_admission():
            app = args.app_checkout.resolve(strict=True)
            path, manifest = journey.selected_manifest(app, source_override=False, agent=None, runtime=None, evidence=evidence)
            if gate.validate_tuple(journey.read_json(args.candidate), app, path) != manifest or manifest.get("release") != 1:
                raise RuntimeError("fresh-install requires a frozen release-1 manifest")
            if (root / "current").exists():
                raise RuntimeError("fresh-install requires an uninstalled machine")
            if not journey.sentinel_observation(sentinel, birth)["alive"]:
                raise RuntimeError("Unrelated sentinel exited before installation")
            state.update(app=app, candidate_path=path, manifest=manifest)
        def fresh_install():
            journey.bootstrap(state["app"], state["candidate_path"], args.shell, "install", evidence / "release1", root, data)
            state["updated"] = True
        def fresh_start():
            jumps.start_direct_runtime(evidence / "release1")
            journey.wait_ready(data, timeout=int(os.environ.get("ASTRID_CI_WAIT_READY_TIMEOUT", "900")))
            state["started1"] = True
        def fresh_select():
            path, selected_data, version = journey.installation(root, data)
            if selected_data != data or jumps.installed_release(version) != 1:
                raise RuntimeError("Fresh install did not select release 1 and the requested data root")
            journey.selected_tuple(state["manifest"], version)
            journey.assert_direct_service_ownership(root, data)
            state.update(version=version, version_path=path)
        def fresh_sentinel():
            if not journey.sentinel_observation(sentinel, birth)["alive"]:
                raise RuntimeError("Unrelated sentinel did not survive fresh installation")
        stage("admission", [], fresh_admission, evidence)
        stage("release1-install", ["app"], fresh_install, evidence / "release1" / "install-bootstrap.log")
        stage("release1-start", ["updated"], fresh_start, evidence / "release1")
        stage("release1-selection", ["started1"], fresh_select, evidence / "release1")
        stage("release1-verify-install", ["version"], lambda: journey.verify("install", state["version_path"], data, evidence), evidence / "verify-install.log")
        stage("release1-task", ["version"], lambda: jumps.task_after_update(state["version"], evidence / "release1-working-task", project), evidence / "release1-working-task")
        stage("one-runtime", ["version"], lambda: journey.assert_one_runtime(data), evidence)
        stage("sentinel", [], fresh_sentinel, evidence)
        return report
    def admission():
        fixture, manifest0, receipt = jumps.release0_admission(args)
        app = args.app_checkout.resolve(strict=True)
        path, manifest = journey.selected_manifest(app, source_override=False, agent=None, runtime=None, evidence=evidence)
        candidate = journey.read_json(args.candidate)
        if gate.validate_tuple(candidate, app, path) != manifest or manifest.get("release") != 1:
            raise RuntimeError("release0-to-release1 requires a frozen release-1 manifest")
        if not journey.sentinel_observation(sentinel, birth)["alive"]:
            raise RuntimeError("Unrelated sentinel exited before release-0 bootstrap")
        state.update(fixture=fixture, manifest0=manifest0, manifest=manifest, candidate_path=path, app=app)
        report["release0_fixture"] = receipt
    def bootstrap0():
        jumps.bootstrap_release0(state["fixture"], state["manifest0"], args.shell, evidence / "release0", root, data)
        state["bootstrap0"] = True
    def start0():
        jumps.start_direct_runtime(evidence / "release0")
        journey.wait_ready(data)
        state["started0"] = True
    def select0():
        path, selected_data, old = journey.installation(root, data)
        if selected_data != data or jumps.installed_release(old) != 0:
            raise RuntimeError("Frozen release-0 bootstrap did not select release 0")
        journey.assert_direct_service_ownership(root, data)
        journey.assert_one_runtime(data)
        state.update(old=old, old_path=path, old_tuple=journey.selected_tuple(journey.read_json(state["manifest0"]), old))
    def seed():
        state["keys"] = jumps.direct_workspace(root, state["old"], data, evidence / "release0-api-seed", "seed", {"project": project, "generation": "release0", "task_id": None, "create_project": True})
    def task0():
        state["keys"]["task_id"] = jumps.task_after_update(state["old"], evidence / "release0-api-seed" / "release0-task", project)
        state["task0"] = True
    def snap0():
        state["before"] = jumps.direct_workspace(root, state["old"], data, evidence / "release0-api-seed", "snapshot", state["keys"])
    def owner0():
        health = journey.healthy(data)
        pid = int(health["pid"])
        birth0 = journey.process_birth(pid)
        if not birth0:
            raise RuntimeError("Cannot establish the release-0 Runtime process identity")
        state.update(health0=health, pid0=pid, birth0=birth0)
    def stop0():
        report["prestop_r0"] = prestop_r0(args, root, data, evidence / "release0", state["health0"], state["pid0"], state["birth0"])
        state["stopped0"] = True
    def update():
        if not getattr(args, "diagnostic_prestop_r0", False):
            journey.assert_direct_service_ownership(root, data)
            journey.assert_one_runtime(data)
        journey.bootstrap(state["app"], state["candidate_path"], args.shell, "update", evidence / "release1", root, data)
        state["updated"] = True
    def start1():
        jumps.start_direct_runtime(evidence / "release1")
        journey.wait_ready(data)
        state["started1"] = True
    def select1():
        path, selected_data, version = journey.installation(root, data)
        if selected_data != data or path == state["old_path"]:
            raise RuntimeError("Release-1 update did not retain data and activate a new version")
        if not getattr(args, "diagnostic_prestop_r0", False) and jumps.installed_release(version) != 1:
            raise RuntimeError("Release-1 update did not select release 1")
        if getattr(args, "diagnostic_prestop_r0", False):
            state.update(version=version, version_path=path)
        else:
            jumps.selected_candidate(state["manifest"], version, state["old_tuple"])
            journey.assert_direct_service_ownership(root, data)
            health = journey.healthy(data)
            if health["runtime_instance_id"] == state["health0"]["runtime_instance_id"] or journey.process_birth(state["pid0"]) == state["birth0"]:
                raise RuntimeError("Release-0 Runtime survived the direct release-1 update")
            state.update(version=version, version_path=path)
    def check_release1():
        if jumps.installed_release(state["version"]) != 1:
            raise RuntimeError("Release-1 update did not select release 1")
        jumps.selected_candidate(state["manifest"], state["version"], state["old_tuple"])
    def check_health1():
        health = journey.healthy(data)
        if health["runtime_instance_id"] == state["health0"]["runtime_instance_id"] or journey.process_birth(state["pid0"]) == state["birth0"]:
            raise RuntimeError("Release-0 Runtime survived the direct release-1 update")
    def verify0():
        receipt_path = journey.verify("observe", state["old_path"], data, evidence / "release0-api-seed")
        receipt = journey.read_json(receipt_path)
        if receipt.get("passed") is not True:
            failures = receipt.get("failures", [])
            raise RuntimeError("release-0 verification failed: " + ", ".join(map(str, failures)))
        state["verified0"] = True
    def verify_update():
        journey.verify("update", state["version_path"], data, evidence, before=evidence / "release0-api-seed" / "verify-observe.json")
    def verify_install():
        journey.verify("install", state["version_path"], data, evidence)
    def snap1():
        state["after"] = jumps.direct_workspace(root, state["version"], data, evidence / "release1-api-observe", "snapshot", state["keys"])
    def preserve():
        jumps.assert_preserved(state["before"], state["after"])
        if jumps.semantic_digest(state["before"]) != jumps.semantic_digest(state["after"]):
            raise RuntimeError("Release-1 semantic snapshot digest differs after exact preservation")
    def check_sentinel():
        if not journey.sentinel_observation(sentinel, birth)["alive"]:
            raise RuntimeError("Unrelated sentinel did not survive the direct release-1 update")
    stage("admission", [], admission, evidence)
    stage("release0-bootstrap", ["fixture"], bootstrap0, evidence / "release0" / "release0-bootstrap.log")
    stage("release0-start", ["bootstrap0"], start0, evidence / "release0")
    stage("release0-selection", ["started0"], select0, evidence / "release0")
    stage("release0-verify", ["old"], verify0, evidence / "release0-api-seed" / "verify-observe.log")
    stage("release0-seed", ["old"], seed, evidence / "release0-api-seed")
    stage("release0-task", ["old", "keys"], task0, evidence / "release0-api-seed" / "release0-task")
    stage("release0-snapshot", ["old", "task0"], snap0, evidence / "release0-api-seed")
    stage("release0-owner", ["old"], owner0, evidence / "release0")
    update_dependencies = ["old", "health0", "app"]
    if getattr(args, "diagnostic_prestop_r0", False):
        producers["stopped0"] = "release0-prestop"
        stage("release0-prestop", ["old", "health0", "before"], stop0, evidence / "release0")
        update_dependencies.append("stopped0")
    stage("release1-update", update_dependencies, update, evidence / "release1" / "update-bootstrap.log")
    stage("release1-start", ["updated"], start1, evidence / "release1")
    stage("release1-selection", ["started1", "health0"], select1, evidence / "release1")
    if getattr(args, "diagnostic_prestop_r0", False):
        stage("release1-release", ["version"], check_release1, evidence / "release1")
        stage("release1-service", ["version"], lambda: journey.assert_direct_service_ownership(root, data), evidence / "release1")
        stage("release1-health", ["version", "health0"], check_health1, evidence / "release1")
    stage("release1-verify-update", ["version", "verified0"], verify_update, evidence / "verify-update.log")
    stage("release1-verify-install", ["version"], verify_install, evidence / "verify-install.log")
    stage("release1-snapshot", ["version", "task0"], snap1, evidence / "release1-api-observe")
    stage("semantic-preservation", ["before", "after"], preserve, evidence / "release1-api-observe")
    stage("release1-task", ["version"], lambda: jumps.task_after_update(state["version"], evidence / "release1-working-task", project), evidence / "release1-working-task")
    stage("one-runtime", ["version"], lambda: journey.assert_one_runtime(data), evidence)
    stage("sentinel", [], check_sentinel, evidence)
    return report
