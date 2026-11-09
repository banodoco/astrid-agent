"""Disposable direct lifecycle diagnostic. Its receipts are never proof."""
from pathlib import Path
import json
import os
import re
import sys

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
          "semantic-preservation", "release1-task", "sentinel")


FRESH_STAGES = ("admission", "release1-install", "release1-start", "release1-selection",
                "release1-verify-install", "release1-task", "sentinel")


FAILED_CAPABILITY_ID_LIMIT = 32
_CAPABILITY_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}\Z")


def failed_capability_details(receipt: dict, failures: list) -> list[dict]:
    """Project bounded capability IDs from failed verifier checks only.

    The full verifier receipt may include paths, environment, identities, and
    live service data. Keep only the two fixed capability check projections.
    """
    if not isinstance(receipt, dict) or not isinstance(failures, list):
        return []
    details = receipt.get("details")
    if not isinstance(details, dict):
        return []
    allowed = {
        "capabilities.required": "unready",
        "capabilities.approval": "unapproved",
    }
    projected = []
    for check, field in allowed.items():
        if check not in failures:
            continue
        value = details.get(check)
        values = value.get(field) if isinstance(value, dict) else None
        if not isinstance(values, list):
            continue
        ids = sorted({item for item in values
                      if isinstance(item, str) and _CAPABILITY_ID.fullmatch(item)})
        projected.append({"check": check, "ids": ids[:FAILED_CAPABILITY_ID_LIMIT],
                          "omitted_count": max(0, len(ids) - FAILED_CAPABILITY_ID_LIMIT)})
    return projected


def release0_owner(root: Path, data: Path) -> dict:
    """Capture the selected R0 service and its live Runtime identity."""
    service = journey.assert_direct_service_ownership(root, data)
    health = journey.healthy(data)
    pid = int(health["pid"])
    birth = journey.process_birth(pid)
    if not birth:
        raise RuntimeError("Cannot establish the release-0 Runtime process identity")
    return {"service": service, "health": health, "pid": pid, "birth": birth}


def assert_release0_owner(root: Path, data: Path, owner: dict) -> dict:
    """Require the same healthy R0 service owner immediately before update."""
    journey.assert_direct_service_ownership(root, data)
    health = journey.healthy(data)
    if (health.get("runtime_instance_id") != owner["health"].get("runtime_instance_id")
            or int(health["pid"]) != owner["pid"]
            or journey.process_birth(owner["pid"]) != owner["birth"]):
        raise RuntimeError("Release-0 Runtime owner changed before the direct release-1 update")
    return health


def release0_workspace(root: Path, data: Path, evidence: Path,
                       action: str, request: dict) -> dict:
    """Use the live snapshot contract for both sides of the R0-to-R1 journey."""
    return jumps.live_workspace(root, data, evidence, action, request, release=0)


def initial(evidence, args):
    return {"schema_version": 1, "proof_mode": PROOF_MODE, "classification": "NON-PROOF",
            "qualification": {"eligible": False}, "job_id": args.job_id,
            "scenario": args.leg, "status": "fail", "stages": [],
            "evidence_root": str(evidence)}


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
             ""]
    driver_exit = report.get("driver_exit")
    if isinstance(driver_exit, dict):
        lines.append("Driver exit: {exit_code} during {phase}; diagnostic-only receipts are intentionally qualification-ineligible.".format(
            exit_code=driver_exit["exit_code"], phase=driver_exit["phase"]))
        lines.append("Driver command: `" + " ".join(driver_exit["command"]) + "`")
        lines.append("")
    lines.extend(["| stage | result | observation | forced-continuation-used | first error |",
             "| --- | --- | --- | --- | --- |"]
    )
    for row in report["stages"]:
        error = (row.get("first_error") or "").replace("|", "\\|").replace("\n", " ")
        observation = json.dumps(row["observation"], sort_keys=True) if "observation" in row else ""
        observation = observation.replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {row['stage']} | {row['result']} | {observation} | {row['forced_continuation_used']} | {error} |")
    lines.extend(["", "| stage | needs | source | result | class |", "| --- | --- | --- | --- | --- |"])
    for row in report["stages"]:
        needs = row.get("preconditions", [])
        names = ", ".join(item["name"] for item in needs) or "none"
        sources = ", ".join(item["source"] for item in needs) or "none"
        lines.append(f"| {row['stage']} | {names} | {sources} | {row['result']} | {row.get('failure_class', '-')} |")
    lines.extend(["", "```mermaid", "graph TD"])
    for index, row in enumerate(report["stages"]):
        lines.append(f'  s{index}["{row["stage"]}"]')
        if row["result"] in {"fail", "observed-failed"}:
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


def record_expected_nonzero_exit(evidence: Path, command: list[str]) -> bool:
    """Attribute the intentional exit only after all diagnostic work passed."""
    report_path = evidence / "keepgoing-report.json"
    receipt_path = evidence / "journey-result.json"
    if not report_path.is_file() or not receipt_path.is_file():
        return False
    report = journey.read_json(report_path)
    receipt = journey.read_json(receipt_path)
    stages = report.get("stages")
    if (not isinstance(stages, list) or not stages or
            any(not isinstance(row, dict) or row.get("result") != "pass" for row in stages) or
            receipt.get("final_cleanup") != "pass"):
        return False
    report["driver_exit"] = {
        "phase": "post-journey diagnostic qualification gate",
        "command": command,
        "exit_code": 1,
        "reason": "diagnostic-only receipts are intentionally qualification-ineligible",
    }
    write(evidence, report)
    journey.retain_named_evidence(evidence, "before-cleanup")
    journey.retain_named_evidence(evidence, "after-cleanup")
    return True


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
                                 if any(previous["stage"] == producers[item]
                                        and previous["result"] in {"fail", "observed-failed"}
                                        for previous in report["stages"])})
            if tainted_by:
                row["failure_class"] = "diagnostic-tainted:" + ",".join(tainted_by)
        else:
            try:
                outcome = action()
                if isinstance(outcome, dict) and outcome.get("result") in {"observed-passed", "observed-failed"}:
                    row["result"] = outcome["result"]
                    row["observation"] = outcome["observation"]
                    row["continuation"] = "Observation recorded; subsequent diagnostic stages continue"
                    if outcome["result"] == "observed-failed":
                        row["failure_class"] = "real"
                else:
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
            if gate.validate_tuple(journey.read_json(args.candidate), app, path) != manifest:
                raise RuntimeError("fresh-install requires the frozen candidate manifest")
            release = manifest.get("release")
            if type(release) is not int or release < 0:
                raise RuntimeError("fresh-install requires a frozen manifest with an integer release")
            if (root / "current").exists():
                raise RuntimeError("fresh-install requires an uninstalled machine")
            if not journey.sentinel_observation(sentinel, birth)["alive"]:
                raise RuntimeError("Unrelated sentinel exited before installation")
            state.update(app=app, candidate_path=path, manifest=manifest)
        def fresh_install():
            try:
                journey.bootstrap(state["app"], state["candidate_path"], args.shell, "install", evidence / "release1", root, data,
                                  agent=getattr(args, "candidate_agent_checkout", None))
            except Exception:
                if getattr(args, "mode", "direct") == "direct":
                    journey.capture_failed_windows_fresh_install(root, evidence)
                raise
            state["updated"] = True
        def fresh_start():
            jumps.start_direct_runtime(evidence / "release1")
            journey.wait_ready(data, timeout=int(os.environ.get("ASTRID_CI_WAIT_READY_TIMEOUT", "900")))
            state["started1"] = True
        def fresh_select():
            path, selected_data, version = journey.installation(root, data)
            if selected_data != data or jumps.installed_release(version) != state["manifest"]["release"]:
                raise RuntimeError("Fresh install did not select the candidate release and the requested data root")
            journey.selected_tuple(state["manifest"], version)
            journey.assert_direct_service_ownership(root, data)
            state.update(version=version, version_path=path)
        def fresh_sentinel():
            if not journey.sentinel_observation(sentinel, birth)["alive"]:
                raise RuntimeError("Unrelated sentinel did not survive fresh installation")
        def fresh_task():
            try:
                task_evidence = evidence / "release1-working-task"
                if sys.platform == "win32":
                    journey.windows.EVIDENCE = task_evidence
                    journey.windows.wait_for("pack-host is running with its current ready file",
                                             journey.windows.host_ready)
                    journey.windows.snapshot("processes-host-ready.json")
                    return journey.windows.task(project, create_project=True)
                return journey.task(state["version"], task_evidence, project)
            except Exception:
                journey.capture_failed_release1_task(evidence, state["version"])
                raise
        stage("admission", [], fresh_admission, evidence)
        stage("release1-install", ["app"], fresh_install, evidence / "release1" / "install-bootstrap.log")
        stage("release1-start", ["updated"], fresh_start, evidence / "release1")
        stage("release1-selection", ["started1"], fresh_select, evidence / "release1")
        stage("release1-verify-install", ["version"], lambda: journey.verify("install", state["version_path"], data, evidence), evidence / "verify-install.log")
        stage("release1-task", ["version"], fresh_task, evidence / "release1-working-task")
        stage("sentinel", [], fresh_sentinel, evidence)
        return report
    def admission():
        fixture, manifest0, receipt = jumps.release9_admission(args, root.parent / "release9-source")
        app = args.app_checkout.resolve(strict=True)
        path, manifest = journey.selected_manifest(app, source_override=False, agent=None, runtime=None, evidence=evidence)
        candidate = journey.read_json(args.candidate)
        if gate.validate_tuple(candidate, app, path) != manifest:
            raise RuntimeError("release9-then-install requires a frozen candidate manifest")
        jumps.candidate_release(manifest)
        if not journey.sentinel_observation(sentinel, birth)["alive"]:
            raise RuntimeError("Unrelated sentinel exited before release-0 bootstrap")
        state.update(fixture=fixture, manifest0=manifest0, manifest=manifest, candidate_path=path, app=app)
        report["release9_source"] = receipt
    def bootstrap0():
        jumps.bootstrap_release9(state["fixture"], state["manifest0"], args.shell, evidence / "release0", root, data)
        state["bootstrap0"] = True
    def start0():
        jumps.start_direct_runtime(evidence / "release0")
        journey.wait_ready(data)
        state["started0"] = True
    def select0():
        path, selected_data, old = journey.installation(root, data)
        if selected_data != data or jumps.installed_release(old) != jumps.RELEASE9_RELEASE:
            raise RuntimeError("Pinned Release-9 bootstrap did not select release 9")
        release0_owner(root, data)
        old_tuple = journey.selected_tuple(journey.read_json(state["manifest0"]), old)
        jumps.write_diagnostic_release0_preconditions(
            args, root, data, fixture_receipt=report["release9_source"],
            release0_manifest=state["manifest0"], target_manifest=state["manifest"],
            candidate=journey.read_json(args.candidate), version_path=path, version=old)
        state.update(old=old, old_path=path, old_tuple=old_tuple)
    def seed():
        state["keys"] = release0_workspace(root, data, evidence / "release0-api-seed", "seed", {"project": project, "generation": "release0", "task_id": None, "create_project": True})
    def task0():
        state["keys"]["task_id"] = jumps.task_after_update(state["old"], evidence / "release0-api-seed" / "release0-task", project)
        state["task0"] = True
    def snap0():
        state["before"] = release0_workspace(root, data, evidence / "release0-api-seed", "snapshot", state["keys"])
    def assert_selection0():
        path, selected_data, version = journey.installation(root, data)
        if (selected_data != data or path != state["old_path"] or version != state["old"]
                or jumps.installed_release(version) != jumps.RELEASE9_RELEASE):
            raise RuntimeError("Selected installation changed from release 9 before the candidate upgrade")
    def owner0():
        assert_selection0()
        owner = release0_owner(root, data)
        state.update(health0=owner["health"], pid0=owner["pid"], birth0=owner["birth"])
    def update():
        assert_selection0()
        assert_release0_owner(root, data, {"health": state["health0"],
                                           "pid": state["pid0"], "birth": state["birth0"]})
        journey.bootstrap(state["app"], state["candidate_path"], args.shell, "install", evidence / "release1", root, data)
        state["updated"] = True
    def start1():
        jumps.start_direct_runtime(evidence / "release1")
        journey.wait_ready(jumps.selected_data_root(root, data))
        state["started1"] = True
    def select1():
        path, selected_data, version = journey.installation(root, data)
        if selected_data != data or path == state["old_path"] or jumps.installed_release(version) != state["manifest"]["release"]:
            raise RuntimeError("Candidate upgrade did not retain data and activate the candidate release")
        jumps.selected_candidate(state["manifest"], version, state["old_tuple"])
        journey.assert_direct_service_ownership(root, data)
        health = journey.healthy(jumps.selected_data_root(root, data))
        if health["runtime_instance_id"] == state["health0"]["runtime_instance_id"] or journey.process_birth(state["pid0"]) == state["birth0"]:
            raise RuntimeError("Release-9 Runtime survived the candidate upgrade")
        state.update(version=version, version_path=path)
    def verify0():
        receipt_path = journey.verify("observe", state["old_path"], data, evidence / "release0-api-seed")
        receipt = journey.read_json(receipt_path)
        passed = receipt.get("passed")
        failures = receipt.get("failures")
        if type(passed) is not bool or not isinstance(failures, list):
            raise RuntimeError("Release-0 observation receipt has invalid passed or failures fields")
        if passed:
            state["verified0"] = True
        observation = {"passed": passed, "failures": failures}
        failed_details = failed_capability_details(receipt, failures)
        if failed_details:
            observation["failed_check_details"] = failed_details
        return {"result": "observed-passed" if passed else "observed-failed",
                "observation": observation}
    def verify_update():
        journey.verify("update", state["version_path"], jumps.selected_data_root(root, data), evidence, before=evidence / "release0-api-seed" / "verify-observe.json")
    def verify_install():
        journey.verify("install", state["version_path"], jumps.selected_data_root(root, data), evidence)
    def snap1():
        state["after"] = jumps.live_workspace(root, jumps.selected_data_root(root, data),
                                              evidence / "release1-api-observe", "snapshot", state["keys"])
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
    stage("release1-update", ["old", "health0", "app"], update, evidence / "release1" / "update-bootstrap.log")
    stage("release1-start", ["updated"], start1, evidence / "release1")
    stage("release1-selection", ["started1", "health0"], select1, evidence / "release1")
    stage("release1-verify-update", ["version", "verified0"], verify_update, evidence / "verify-update.log")
    stage("release1-verify-install", ["version"], verify_install, evidence / "verify-install.log")
    stage("release1-snapshot", ["version", "task0"], snap1, evidence / "release1-api-observe")
    stage("semantic-preservation", ["before", "after"], preserve, evidence / "release1-api-observe")
    stage("release1-task", ["version"], lambda: jumps.task_after_update(state["version"], evidence / "release1-working-task", project), evidence / "release1-working-task")
    stage("sentinel", [], check_sentinel, evidence)
    return report
