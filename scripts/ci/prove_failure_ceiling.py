"""Execute native comparator fixtures; never qualify or retire product failures.

Requires Python 3.11+, Git and Node 20+. All fixture code, native streams, raw
exits and verdicts are retained under --output. No product suite is selected.
"""
from __future__ import annotations
import argparse
import copy
import datetime as dt
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    source = Path(__file__).resolve().parent
    product = source.parents[1].name
    repository = "fixture/" + product
    execution = output / "execution"
    code = execution / "scripts/ci"
    code.mkdir(parents=True)
    hashes = {}
    for name in ("failure_ceiling.py", "ceiling_adapters.py", "ceiling_capture.py", "ceiling_node.mjs"):
        raw = (source / name).read_bytes()
        (code / name).write_bytes(raw)
        hashes[name] = hashlib.sha256(raw).hexdigest()
    (execution / "custody.test.mjs").write_text("""import { test } from 'node:test';
import assert from 'node:assert/strict';
test('control passes', () => assert.equal(1, 1));
test('sensitive custody identity', () => {
  if (process.env.CEILING_FIXTURE_STATE === 'inherited') throw new Error('FixtureError: missing owner fixture');
  if (process.env.CEILING_FIXTURE_STATE === 'different-cause') throw new Error('CustodyError: publication after owner loss');
  assert.equal(process.env.CEILING_FIXTURE_STATE, 'ready');
});
""", encoding="utf-8")
    for command in (["git", "init", "-q", str(execution)],
                    ["git", "-C", str(execution), "add", "."],
                    ["git", "-C", str(execution), "-c", "user.name=Comparator fixture", "-c", "user.email=fixture@example.invalid",
                     "commit", "-q", "-m", "Local executed fixture; no product qualification"]):
        subprocess.run(command, check=True)
    head = subprocess.check_output(["git", "-C", str(execution), "rev-parse", "HEAD"], text=True).strip()
    core = module(code / "failure_ceiling.py", "core")
    adapter = module(code / "ceiling_adapters.py", "adapters")
    measurements = {}
    for state in ("g1", "g2", "passing", "recurrence", "different-cause"):
        jobs = []
        for workflow in ("CI", "Nightly"):
            run = f"local-fixture-{state}-{workflow.lower()}"
            directory = output / "runs" / run
            script = output / "commands" / (run + ".sh")
            script.parent.mkdir(exist_ok=True)
            script.write_text('node --test --test-reporter=spec --test-reporter-destination=stdout '
                              '--test-reporter=./scripts/ci/ceiling_node.mjs --test-reporter-destination=stdout custody.test.mjs\n')
            env = {**os.environ, "RUNNER_TEMP": str(directory), "GITHUB_WORKSPACE": str(execution),
                   "GITHUB_REPOSITORY": repository, "GITHUB_JOB": "native", "GITHUB_RUN_ID": run,
                   "GITHUB_RUN_ATTEMPT": "1", "CEILING_FIXTURE_STATE": "ready" if state == "passing" else "different-cause" if state == "different-cause" else "inherited"}
            env.pop("CEILING_REPORTER_ACTIVE", None)
            command = [sys.executable, "-I", str(code / "ceiling_capture.py"), "--step", "native", "--shell", "bash", str(script)]
            result = subprocess.run(command, cwd=execution, env=env, capture_output=True, text=True)
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "process.log").write_text(result.stdout + result.stderr)
            expected_exit = 0 if state == "passing" else 1
            assert result.returncode == expected_exit, (run, result.returncode, result.stdout, result.stderr)
            native = directory / "failure-ceiling/native/native"
            receipt = core.read_json(native / "command.json")
            assert receipt["raw_exit"] == result.returncode and receipt["complete"] and receipt["head"] == head
            streams = list(native.glob("node-*.jsonl"))
            assert len(streams) == 1
            lane = workflow + "/native"
            suite = adapter.native_stream(streams[0], lane, "native")
            core.verify_suite(suite, lane)
            started = dt.datetime.fromtimestamp(receipt["started_at"], tz=dt.timezone.utc).isoformat()
            jobs.append({"workflow": workflow, "job": "native", "status": "completed", "conclusion": "success" if not result.returncode else "failure",
                         "head": head, "tuple": {repository: head}, "run_id": run, "job_id": run + "/native", "attempt": 1,
                         "started_at": started, "unexplained_failed_steps": [], "suites": [suite]})
        measurements[state] = {"schema": 1, "repository": repository, "fixture_only": True,
                               "head": head, "tuple": {repository: head}, "tier": "release", "complete": True, "jobs": jobs}
        write(output / (state + "-observed.json"), measurements[state])
    checks = {"version": 1, "repository": repository, "merge": [], "release": []}
    inventory = {"schema": 1, "repository": repository, "tests": [], "tiers": {"merge": ["CI/native"], "release": ["CI/native", "Nightly/native"]}, "reconciliation": []}
    ledger = {"schema": 1, "repository": repository, "authority": {"fixture_only": True}, "entries": [], "retired": []}
    for index, job in enumerate(measurements["g2"]["jobs"]):
        spec = {"workflow": job["workflow"], "workflow_file": "fixture.yml", "job": job["job"], "kind": "tests", "commands": [{"lane": job["suites"][0]["lane"]}]}
        checks["release"].append(spec)
        if not index: checks["merge"].append(spec)
        for row in job["suites"][0]["tests"]:
            identity = {key: row.get(key) for key in ("lane", "native_id", "parameter", "subtest")}
            inventory["tests"].append(identity)
            if row["outcome"] != "failed": continue
            entry = {**identity, "owner": "fixture-owner", "release": 15, "admission": "explained-inherited", "stage": row["stage"], "diagnostic": row["diagnostic"]}
            for label in ("g1", "g2"):
                before = measurements[label]["jobs"][index]
                actual = next(t for t in before["suites"][0]["tests"] if core.identity(t) == core.identity(row))
                assert (actual["stage"], actual["diagnostic"]) == (entry["stage"], entry["diagnostic"])
                entry[label] = {**identity, "stage": actual["stage"], "diagnostic": actual["diagnostic"], "outcome": "failed",
                                "sha": head, "run_id": before["run_id"], "job_id": before["job_id"], "source": label + "-observed.json (executed local fixture)"}
            ledger["entries"].append(entry)
    trusted = (checks, ledger, inventory)
    results = {}
    def evaluate(name, proposed, report, tier="release", base=trusted):
        report = copy.deepcopy(report)
        report["tier"] = tier
        verdict = core.evaluate(base, proposed, report, repository=repository, head=head, tier=tier, expected_tuple={repository: head})
        write(output / (name + "-verdict.json"), verdict)
        results[name] = verdict["verdict"]
        return verdict
    for tier in ("merge", "release"):
        verdict = evaluate("complete-covered-" + tier, copy.deepcopy(trusted), measurements["g2"], tier)
        assert verdict["verdict"] == "PASS", verdict
    candidate = copy.deepcopy(trusted)
    for entry in list(candidate[1]["entries"]):
        workflow = entry["lane"].split("/")[0]
        candidate[1]["retired"].append({**{k: entry[k] for k in ("lane", "native_id", "parameter", "subtest")},
                                       "receipt": {"source": "qualifying-run", "workflow": workflow, "job": "native"}})
    candidate[1]["entries"].clear()
    verdict = evaluate("actual-fixture-retirement", candidate, measurements["passing"])
    assert verdict["verdict"] == "PASS" and len(verdict["retirement_receipts"]) == 2, verdict
    verdict = evaluate("retired-recurrence", copy.deepcopy(candidate), measurements["recurrence"], base=candidate)
    assert verdict["verdict"] == "FAIL" and any("retired failure" in s for s in verdict["errors"]), verdict
    verdict = evaluate("sensitive-different-cause", copy.deepcopy(trusted), measurements["different-cause"])
    assert verdict["verdict"] == "FAIL" and any("different-cause" in s for s in verdict["errors"]), verdict
    missing = copy.deepcopy(measurements["g2"])
    missing["jobs"].pop()
    verdict = evaluate("missing-nightly", copy.deepcopy(trusted), missing)
    assert verdict["verdict"] != "PASS" and any("Nightly / native" in s for s in verdict["unknown"]), verdict
    write(output / "proof.json", {"fixture_only": True, "product_qualification": False,
          "executed_local_fixture_commit": head, "implementation_sha256": hashes, "results": results,
          "actual_native_invocations": 10, "expected_raw_exits": {"g1": 1, "g2": 1, "passing": 0, "recurrence": 1, "different-cause": 1},
          "qualification_limit": "Executed fixture retirement is not retirement of any G1/G2 product entry."})
    print(json.dumps(results, sort_keys=True))


if __name__ == "__main__":
    main()
