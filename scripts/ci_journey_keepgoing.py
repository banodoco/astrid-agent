"""Disposable direct lifecycle diagnostic. Its receipts are never proof."""
from pathlib import Path
import json

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


def initial(evidence, args):
    return {"schema_version": 1, "proof_mode": PROOF_MODE, "classification": "NON-PROOF",
            "qualification": {"eligible": False}, "job_id": args.job_id,
            "scenario": args.leg, "status": "fail", "stages": [],
            "evidence_root": str(evidence)}


def write(evidence, report):
    evidence.mkdir(parents=True, exist_ok=True)
    journey.write_json(evidence / "keepgoing-report.json", report)
    # A diagnostic receipt must never look like an eligible successful row.
    journey.write_json(evidence / "journey-result.json", {**report, "receipt_kind": "diagnostic-only", "status": "fail"})
    lines = ["NON-PROOF: keep-going diagnostic", "", "| stage | result | forced-continuation-used | first error |", "| --- | --- | --- | --- |"]
    for row in report["stages"]:
        error = (row.get("first_error") or "").replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {row['stage']} | {row['result']} | {row['forced_continuation_used']} | {error} |")
    (evidence / "keepgoing-report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def execute(args, root: Path, data: Path, evidence: Path, sentinel, birth: str):
    report = initial(evidence, args)
    state = {}
    project = "tst-m4-3b-release0"
    def stage(name, dependencies, action, path):
        missing = [item for item in dependencies if item not in state]
        row = {"stage": name, "result": "not-run", "forced_continuation_used": False,
               "first_error": None, "evidence_path": str(path)}
        if missing:
            row.update(result="not-run", first_error="Unsafe or unavailable prerequisites: " + ", ".join(missing))
        else:
            try:
                action()
                row["result"] = "pass"
            except Exception as exc:
                row.update(result="fail", first_error=f"{type(exc).__name__}: {exc}")
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
    def update():
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
        if selected_data != data or path == state["old_path"] or jumps.installed_release(version) != 1:
            raise RuntimeError("Release-1 update did not retain data and activate release 1")
        jumps.selected_candidate(state["manifest"], version, state["old_tuple"])
        journey.assert_direct_service_ownership(root, data)
        health = journey.healthy(data)
        if health["runtime_instance_id"] == state["health0"]["runtime_instance_id"] or journey.process_birth(state["pid0"]) == state["birth0"]:
            raise RuntimeError("Release-0 Runtime survived the direct release-1 update")
        state.update(version=version, version_path=path)
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
    stage("release0-verify", ["old"], lambda: journey.verify("observe", state["old_path"], data, evidence / "release0-api-seed"), evidence / "release0-api-seed" / "verify-observe.log")
    stage("release0-seed", ["old"], seed, evidence / "release0-api-seed")
    stage("release0-task", ["old", "keys"], task0, evidence / "release0-api-seed" / "release0-task")
    stage("release0-snapshot", ["old", "task0"], snap0, evidence / "release0-api-seed")
    stage("release0-owner", ["old"], owner0, evidence / "release0")
    stage("release1-update", ["old", "health0", "app"], update, evidence / "release1" / "update-bootstrap.log")
    stage("release1-start", ["updated"], start1, evidence / "release1")
    stage("release1-selection", ["started1", "health0"], select1, evidence / "release1")
    stage("release1-verify-update", ["version"], verify_update, evidence / "verify-update.log")
    stage("release1-verify-install", ["version"], verify_install, evidence / "verify-install.log")
    stage("release1-snapshot", ["version", "task0"], snap1, evidence / "release1-api-observe")
    stage("semantic-preservation", ["before", "after"], preserve, evidence / "release1-api-observe")
    stage("release1-task", ["version"], lambda: jumps.task_after_update(state["version"], evidence / "release1-working-task", project), evidence / "release1-working-task")
    stage("one-runtime", ["version"], lambda: journey.assert_one_runtime(data), evidence)
    stage("sentinel", [], check_sentinel, evidence)
    return report
