"""Replay archived R14-P39-30 evidence without dispatching or changing policy."""
from __future__ import annotations
import argparse
import copy
import datetime as dt
import gzip
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
REPOSITORY = "banodoco/astrid-agent"
HEAD = "b1e53baf5facbb340dfc42b1688f7152c255e351"


def module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    hosted = module(ROOT / "scripts/ci/ceiling_hosted.py", "proposed_hosted")
    policy = hosted.core.load_policy(ROOT, REPOSITORY)
    job_spec = next(row for row in policy[0]["merge"] if row["job"] == "windows-launchers")
    ledger = json.loads((HERE / "per-id-ledger.json").read_text())
    result = {"scope": "archived-native replay; not a new Windows execution or hosted qualification",
              "native_head": HEAD, "implementation": {}, "steps": {}, "policy_unchanged": True}
    for name in ("scripts/ci/ceiling_adapters.py", "scripts/ci/ceiling_hosted.py", "scripts/ci/test_bun_selection.py",
                 ".github/workflows/failure-ceiling.yml"):
        result["implementation"][name] = {"sha256": hashlib.sha256((ROOT / name).read_bytes()).hexdigest(),
                                          "git_blob": subprocess.check_output(["git", "hash-object", name], cwd=ROOT, text=True).strip()}
    with tempfile.TemporaryDirectory() as temporary:
        temporary = Path(temporary)
        native = temporary / "native"
        earlier = temporary / "earlier"
        baseline = temporary / "baseline"
        with zipfile.ZipFile(HERE / "original-launchers-14581372377.zip") as archive:
            archive.extractall(native)
        with zipfile.ZipFile(HERE / "original-trusted-refusal-14566167356.zip") as archive:
            archive.extractall(earlier)
        with zipfile.ZipFile(HERE / "original-source-and-metadata.zip") as archive:
            for name in ("ceiling_adapters.py", "failure_ceiling.py"):
                baseline.mkdir(exist_ok=True)
                (baseline / name).write_bytes(archive.read("source-b1e53baf/scripts/ci/" + name))
        before = module(baseline / "ceiling_adapters.py", "original_adapters")
        after_suites = []
        for step in ("step-03", "step-04"):
            directory = native / step
            expected = next(row for row in job_spec["commands"] if row["id"] == step)
            raw = json.loads((directory / "command.json").read_text())
            arguments = (directory / "bun-results.xml", directory / "command.log", expected["lane"], step, raw["raw_exit"])
            try:
                old = before.bun_junit(*arguments)
                old_result = {"normalization": "ACCEPTED", "suite": old}
            except ValueError as error:
                old_result = {"normalization": "REFUSED", "reason": str(error)}
            command = hosted.trusted_bun_command(job_spec, expected, directory)
            new = hosted.adapters.bun_junit(*arguments, command)
            hosted.core.verify_suite(new, expected["lane"])
            assert new["collected"] == [row["native_id"] for row in ledger["steps"][step]["cases"]]
            after_suites.append(new)
            result["steps"][step] = {"before": old_result, "after": {"normalization": "ACCEPTED", "suite": new},
                                     "original_receipt": raw, "command": command}
        raw_job = json.loads((HERE / "original-launchers-job.json").read_text())
        job = hosted.normalized_job(job_spec, raw_job, {"id": 44626225586, "run_attempt": 1}, [native],
                                    REPOSITORY, HEAD, ROOT, {REPOSITORY: HEAD})
        assert job["suites"] == after_suites
        result["original_native_job_after"] = job
        # Use the full, unchanged production job/inventory policy. This bounded
        # input deliberately lacks other jobs; the real comparator names them.
        result["windows_only_policy_replay"] = {}
        for tier in ("merge", "release"):
            report = {"schema": 1, "repository": REPOSITORY, "head": HEAD, "tier": tier,
                      "tuple": {REPOSITORY: HEAD}, "complete": True, "jobs": [job]}
            verdict = hosted.core.evaluate(policy, copy.deepcopy(policy), report, repository=REPOSITORY, head=HEAD,
                                            tier=tier, expected_tuple={REPOSITORY: HEAD},
                                            now=dt.datetime.fromisoformat(job["started_at"].replace("Z", "+00:00")))
            # Keep the exhaustive controlled-input verdict reproducible without
            # burying this packet's 35 rows under intentionally absent unit and
            # integration lanes. These are not observed product failures.
            raw_verdict = (json.dumps(verdict, indent=2) + "\n").encode()
            (args.output / ("windows-only-" + tier + ".json.gz")).write_bytes(gzip.compress(raw_verdict, mtime=0))
            result["windows_only_policy_replay"][tier] = {
                "scope": "Windows-only diagnostic input; other required jobs deliberately absent; not qualification",
                "verdict": verdict["verdict"], "total_error_count": len(verdict["errors"]),
                "total_unknown_count": len(verdict["unknown"]),
                "packet_errors": [message for message in verdict["errors"] if "Chat service/windows-launchers/" in message],
                "full_verdict_sha256": hashlib.sha256(raw_verdict).hexdigest(),
                "reproduction_output": "windows-only-" + tier + ".json.gz"}
            assert verdict["verdict"] == "FAIL"
        official = json.loads((earlier / "verdict.json").read_text())
        report = json.loads((earlier / "observed.json").read_text())
        def evaluate_frozen(report):
            verdict = hosted.core.evaluate(policy, copy.deepcopy(policy), report, repository=REPOSITORY,
                                            head=report["head"], tier=report["tier"], expected_tuple=report["tuple"])
            verdict["unknown"].extend(report["collection_errors"])
            return verdict
        reproduced = evaluate_frozen(report)
        assert reproduced == official, "unchanged official verdict did not reproduce"
        run = json.loads((earlier / "run-44580348392.json").read_text())
        raw_job = json.loads((HERE / "earlier-launchers-job.json").read_text())
        earlier_root = earlier / "artifact-14559687499/files"
        tree_metadata = json.loads((HERE / "verified-execution-trees.json").read_text())
        archived_trees = json.loads((earlier / "execution-trees.json").read_text())
        assert {sha: row["tree"] for sha, row in tree_metadata.items()} == archived_trees
        class RetainedGitHubTrees:
            def tree(self, sha):
                assert tree_metadata[sha]["sha"] == sha
                return tree_metadata[sha]["tree"]
        restored = hosted.normalized_job(job_spec, raw_job, run, [earlier_root], REPOSITORY,
                                         report["head"], ROOT, report["tuple"], RetainedGitHubTrees())
        after_report = copy.deepcopy(report)
        after_report["jobs"].append(restored)
        refusal = "('Chat service', 'windows-launchers'): Bun terminal summary missing/truncated"
        assert after_report["collection_errors"].count(refusal) == 1
        after_report["collection_errors"].remove(refusal)
        # The other original collection gaps remain. Never turn this into a
        # complete aggregate or borrow a later CI result for the frozen report.
        after_report["complete"] = not after_report["collection_errors"]
        proposed = evaluate_frozen(after_report)
        assert proposed["verdict"] == "FAIL" and proposed["errors"] == official["errors"]
        result["earlier_official_replay"] = {"before": official, "after": proposed,
                                             "remaining_collection_errors": after_report["collection_errors"],
                                             "restored_windows_job": restored}
        linux = earlier / "artifact-14560192158/files/step-06"
        linux_suite = hosted.adapters.bun_junit(linux / "bun-results.xml", linux / "command.log", "Chat service/chat",
                                                "step-06", 0, (linux / "command.txt").read_text())
        result["linux_control"] = {"suite": linux_suite, "receipt": json.loads((linux / "command.json").read_text()),
                                   "models_declarations": [row for row in linux_suite["tests"]
                                                           if json.loads(row["native_id"])[0] == "astrid/chat/astrid-agent-models.test.ts"]}
        with tarfile.open(ROOT / "ci/evidence/p39-k2-k3/native-adapter-probes.tar.gz") as archive:
            fixture = temporary / "native-failure"
            fixture.mkdir()
            for name in ("bun-results.xml", "command.log", "exit.json"):
                (fixture / name).write_bytes(archive.extractfile("native-current/bun/" + name).read())
        result["real_failure_fixture"] = hosted.adapters.bun_junit(fixture / "bun-results.xml", fixture / "command.log",
                                                                   "fixture/bun", "native-fixture", 1)
        (args.output / "earlier-proposed-observed.json.gz").write_bytes(gzip.compress((json.dumps(after_report, indent=2) + "\n").encode(), mtime=0))
        (args.output / "proposed-windows-job.json").write_text(json.dumps(job, indent=2) + "\n")
    (args.output / "replay.json.gz").write_bytes(gzip.compress((json.dumps(result, indent=2) + "\n").encode(), mtime=0))
    summary = {"steps": {name: {"before": row["before"]["normalization"],
                                "after": row["after"]["normalization"],
                                "native_exit": row["original_receipt"]["raw_exit"],
                                "identities": len(row["after"]["suite"]["tests"])}
                         for name, row in result["steps"].items()},
               "earlier_official": {"before": official["verdict"], "after": proposed["verdict"],
                                    "unknown_before": len(official["unknown"]), "unknown_after": len(proposed["unknown"])},
               "windows_only_policy": {tier: {"verdict": v["verdict"], "packet_errors": len(v["packet_errors"]),
                                               "scope": v["scope"]}
                                       for tier, v in result["windows_only_policy_replay"].items()}}
    print(json.dumps(summary, indent=2))
    (args.output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")


if __name__ == "__main__":
    main()
