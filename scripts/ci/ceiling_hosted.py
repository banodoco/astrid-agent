"""Collect exact-head Actions evidence, then run the trusted K2/K3 comparator.

Run from the exported merge-base snapshot. Only GitHub's read-only Actions API
and archive bytes are used. No candidate package, reporter or policy is imported.
"""
from __future__ import annotations
import argparse
import datetime as dt
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
import urllib.parse
import zipfile


def module(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(name + ".py"))
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


core = module("failure_ceiling")
adapters = module("ceiling_adapters")


class ArtifactRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        core.require(urllib.parse.urlsplit(newurl).scheme == "https", "insecure artifact redirect")
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if urllib.parse.urlsplit(req.full_url).netloc != urllib.parse.urlsplit(newurl).netloc:
            redirected.remove_header("Authorization")
        return redirected


class GitHub:
    def __init__(self, repository):
        core.require(re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository), "invalid repository")
        self.root = f"https://api.github.com/repos/{repository}/"
        self.headers = {"Accept": "application/vnd.github+json", "Authorization": "Bearer " + os.environ["GITHUB_TOKEN"],
                        "X-GitHub-Api-Version": "2022-11-28"}
        self.trees = {}

    def get(self, path):
        request = urllib.request.Request(self.root + path, headers=self.headers)
        with urllib.request.build_opener(ArtifactRedirect()).open(request, timeout=30) as response:
            return response.read()

    def json(self, path):
        return json.loads(self.get(path))

    def pages(self, path, key):
        values = []
        for page in range(1, 101):
            result = self.json(path + ("&" if "?" in path else "?") + f"per_page=100&page={page}")[key]
            values.extend(result)
            if len(result) < 100:
                return values
        raise core.InvalidEvidence("Actions pagination exceeded bound; inventory incomplete")

    def tree(self, sha):
        core.require(isinstance(sha, str) and core.SHA.fullmatch(sha), "invalid executed commit")
        if sha not in self.trees:
            self.trees[sha] = self.json("git/commits/" + sha)["tree"]["sha"]
        return self.trees[sha]


def same_tree(repository, executed, requested, api=None):
    if executed == requested:
        return True
    if not isinstance(executed, str) or not core.SHA.fullmatch(executed):
        return False
    def tree(sha):
        result = subprocess.run(["git", "-C", str(repository), "rev-parse", "--verify", sha + "^{tree}"],
                                text=True, capture_output=True, check=False)
        return result.stdout.strip() if result.returncode == 0 else None
    # PR checks may run the synthetic merge; qualify only exact equal trees.
    # Its executed SHA remains in the raw command receipt.
    a, b = tree(executed), tree(requested)
    if api is not None and (a is None or b is None):
        a, b = api.tree(executed), api.tree(requested)
    return a is not None and b is not None and a == b


def extract_archive(api, artifact, directory):
    raw = api.get(f"actions/artifacts/{artifact['id']}/zip")
    actual = "sha256:" + hashlib.sha256(raw).hexdigest()
    core.require(artifact.get("digest") == actual, f"artifact digest unavailable/mismatched: {artifact['id']}")
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "provenance.json").write_text(json.dumps(artifact, indent=2) + "\n", encoding="utf-8")
    (directory / "artifact.zip").write_bytes(raw)
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        core.require(sum(item.file_size for item in archive.infolist()) < 1_500_000_000, "oversized result archive")
        for item in archive.infolist():
            target = directory / "files" / item.filename
            core.require(target.resolve().is_relative_to((directory / "files").resolve()), "archive path escaped result directory")
            core.require(not item.is_dir() or not item.file_size, "invalid archive directory")
            if item.is_dir():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.read(item))
    return directory / "files"


def read_command(directory, spec, job, repository, head, candidate, api=None):
    receipts = {}
    for path in directory.rglob("command.json"):
        value = core.read_json(path)
        if (str(value.get("run_id")) != str(job["run_id"]) or value.get("job") != spec["job_id"]
                or value.get("matrix_index") != spec["matrix_index"]):
            continue
        core.require(str(value.get("attempt")) == str(job["attempt"]), "artifact belongs to another attempt")
        core.require(value.get("repository") == repository and same_tree(candidate, value.get("head"), head, api),
                     "command executed different product bytes")
        step = value.get("step")
        core.require(step not in receipts, f"duplicate command receipt: {spec['job']}/{step}")
        core.require(value.get("complete") is True and type(value.get("raw_exit")) is int, f"incomplete command: {step}")
        command = (path.parent / "command.txt").read_bytes()
        core.require(hashlib.sha256(command).hexdigest() == value.get("command_sha256"), "command receipt digest mismatch")
        receipts[step] = (path.parent, value)
    return receipts


def normalized_job(spec, raw, run, roots, repository, head, candidate, tup, api=None):
    row = {"workflow": spec["workflow"], "job": spec["job"], "status": raw["status"], "conclusion": raw["conclusion"],
           "job_id": raw["id"], "run_id": run["id"], "attempt": raw.get("run_attempt", run["run_attempt"]), "started_at": raw["started_at"],
           "head": head, "tuple": {**spec.get("dependency_tuple", {}), **tup}, "suites": [], "unexplained_failed_steps": [], "commands_complete": False}
    if spec["kind"] == "aggregate":
        row["dependencies"] = spec.get("dependencies")
        return row
    receipts = {}
    for root in roots:
        found = read_command(root, spec, row, repository, head, candidate, api)
        core.require(not (receipts.keys() & found.keys()), "same command appears in multiple artifacts")
        receipts.update(found)
    required_steps = spec.get("commands", [])
    core.require(required_steps, f"trusted command inventory absent: {spec['job']}")
    actual_steps = {step["name"]: step for step in raw["steps"]}
    for expected in required_steps:
        step = expected["id"]
        observed = actual_steps.get(expected["name"])
        core.require(observed is not None, f"missing workflow step: {expected['name']}")
        if observed.get("conclusion") == "skipped" and expected.get("optional"):
            continue
        core.require(step in receipts, f"missing command receipt: {step}/{expected['name']}")
        directory, receipt = receipts[step]
        core.require((receipt["raw_exit"] == 0) == (observed.get("conclusion") == "success"), "raw exit and Actions step conclusion differ")
        if expected["kind"] == "tests":
            actual_tuple = {**receipt.get("tuple", {}), repository: head}
            expected_tuple = {**expected.get("dependency_tuple", spec.get("dependency_tuple", {})), repository: head}
            core.require(not receipt.get("tuple_conflicts") and actual_tuple == expected_tuple,
                         f"native invocation has wrong/unrecorded dependency tuple: {expected['name']}")
            suites = []
            for stream in sorted(directory.glob("*.jsonl")):
                if stream.name.startswith("pytest-"):
                    events = adapters.stream(stream)
                    if events[0].get("collect_only"):
                        continue
                    suite = adapters.pytest_result(stream, expected["lane"], expected["id"])
                else:
                    suite = adapters.native_stream(stream, expected["lane"], expected["id"])
                suites.append(suite)
            for xml in sorted(directory.glob("bun-*.xml")):
                suites.append(adapters.bun_junit(xml, directory / "command.log", expected["lane"], expected["id"], receipt["raw_exit"]))
            core.require(suites, f"test step has no native results: {expected['name']}")
            core.require(len(suites) == expected.get("invocations", 1), f"missing/extra native invocation: {expected['name']}")
            if receipt["raw_exit"] and not any(s["raw_exit"] for s in suites):
                row["unexplained_failed_steps"].append(expected["name"])
            row["suites"].extend(suites)
        elif receipt["raw_exit"]:
            row["unexplained_failed_steps"].append(expected["name"])
    covered_names = {c["name"] for c in required_steps}
    for step in raw["steps"]:
        if step.get("conclusion") not in ("success", "skipped") and step["name"] not in covered_names:
            row["unexplained_failed_steps"].append(step["name"])
    row["commands_complete"] = True
    if spec.get("unresolved_dependencies"):
        raise core.InvalidEvidence("unresolved installed/dependency custody: " + "; ".join(spec["unresolved_dependencies"]))
    if spec["kind"] in {"check", "journey"}:
        last = next((receipts[c["id"]][1] for c in reversed(required_steps) if c["id"] in receipts), {})
        core.require({**last.get("tuple", {}), repository: head} == row["tuple"] and not last.get("tuple_conflicts"),
                     "non-test/journey dependency tuple unavailable or different")
    return row


def collect(api, checks, repository, head, tier, candidate, directory, wait_seconds, retirement_lanes=()):
    required = core.required_jobs(checks, tier, retirement_lanes)
    workflows = {spec["workflow_file"]: spec["workflow"] for spec in required.values()}
    end = time.monotonic() + wait_seconds
    runs = {}
    while True:
        all_runs = api.pages(f"actions/runs?head_sha={head}", "workflow_runs")
        runs = {}
        for filename, name in workflows.items():
            matches = [r for r in all_runs if r.get("path", "").split("@")[0] == ".github/workflows/" + filename
                       and r["name"] == name and r["head_sha"] == head]
            if tier == "release":
                matches = [r for r in matches if r["event"] in {"schedule", "workflow_dispatch", "push"}]
            else:
                matches = [r for r in matches if r["event"] in {"pull_request", "workflow_dispatch", "push"}]
            if matches:
                # Do not choose an older green attempt/run over a newer red one.
                runs[filename] = max(matches, key=lambda r: (r["created_at"], r["id"]))
        if len(runs) == len(workflows) and all(r["status"] == "completed" for r in runs.values()):
            break
        if time.monotonic() >= end:
            break
        print("Waiting for required workflows: " + ", ".join(name for file, name in workflows.items()
              if file not in runs or runs[file]["status"] != "completed"), flush=True)
        time.sleep(min(20, max(0, end - time.monotonic())))
    tup = {repository: head}
    report = {"schema": 1, "repository": repository, "head": head, "tier": tier, "tuple": tup,
              "complete": True, "jobs": [], "collection_errors": []}
    for filename, run in runs.items():
        (directory / f"run-{run['id']}.json").write_text(json.dumps(run, indent=2) + "\n", encoding="utf-8")
        # Uploaded jobs cannot qualify a cancelled workflow or a run that is
        # still nonterminal when the bounded wait ends. A completed failure
        # remains eligible: its real red tests need per-ID accounting below.
        if run.get("status") != "completed" or run.get("conclusion") not in {"success", "failure"}:
            report["complete"] = False
            report["collection_errors"].append(
                f"unqualified workflow {run['name']} / run {run['id']}: "
                f"status={run.get('status')}, conclusion={run.get('conclusion')}")
            continue
        jobs = api.pages(f"actions/runs/{run['id']}/attempts/{run['run_attempt']}/jobs", "jobs")
        artifacts = api.pages(f"actions/runs/{run['id']}/artifacts", "artifacts")
        roots = []
        for artifact in artifacts:
            if artifact["name"].startswith("failure-ceiling-") and not artifact["expired"]:
                roots.append(extract_archive(api, artifact, directory / f"artifact-{artifact['id']}"))
        for key, spec in required.items():
            if spec["workflow_file"] != filename:
                continue
            matching = [j for j in jobs if j["name"] == spec["job"]]
            if not matching:
                continue
            try:
                core.require(len(matching) == 1, f"ambiguous job: {key}")
                raw = matching[0]
                report["jobs"].append(normalized_job(spec, raw, run, roots, repository, head, candidate, tup, api))
            except (core.InvalidEvidence, KeyError, TypeError, ValueError, OSError) as exc:
                report["collection_errors"].append(f"{key}: {exc}")
    (directory / "execution-trees.json").write_text(json.dumps(api.trees, indent=2) + "\n", encoding="utf-8")
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--tier", choices=("merge", "release"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--wait-seconds", type=int, default=5400)
    args = parser.parse_args(argv)
    args.output.mkdir(parents=True, exist_ok=True)
    try:
        root = Path(__file__).resolve().parents[2]
        trusted = core.load_policy(root, args.repository)
        proposed = core.load_policy(args.candidate, args.repository)
        core.require(0 <= args.wait_seconds <= 7200, "invalid bounded wait")
        old = core.index_unique(trusted[1]["entries"], core.identity, "trusted entry")
        new = core.index_unique(proposed[1]["entries"], core.identity, "proposed entry")
        lanes = {old[key]["lane"] for key in old.keys() - new.keys()}
        report = collect(GitHub(args.repository), trusted[0], args.repository, args.head, args.tier,
                         args.candidate, args.output, args.wait_seconds, lanes)
        verdict = core.evaluate(trusted, proposed, report, repository=args.repository, head=args.head, tier=args.tier,
                                expected_tuple={args.repository: args.head})
        if report["collection_errors"]:
            verdict["unknown"].extend(report["collection_errors"])
            if verdict["verdict"] == "PASS":
                verdict["verdict"] = "NO-RESULT"
        (args.output / "observed.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    except (core.InvalidEvidence, KeyError, ValueError, OSError, urllib.error.URLError) as exc:
        verdict = {"verdict": "NO-RESULT", "errors": [], "unknown": [str(exc)]}
    (args.output / "verdict.json").write_text(json.dumps(verdict, indent=2) + "\n", encoding="utf-8")
    lines = [f"## Plan 39 {args.tier} failure ceiling: {verdict['verdict']}", "", f"Candidate: `{args.repository}@{args.head}`", "", "D4 consumer: Lead posts new red to banodoco/astrid-dev#971 (decision 6998123631).", ""]
    for label in ("errors", "unknown", "warnings"):
        values = verdict.get(label, [])
        lines.extend(f"- {label}: {s[:1200]}" for s in values[:20])
        if len(values) > 20:
            lines.append(f"- {len(values) - 20} further {label} are retained in verdict.json.")
    lines.append(f"Covered failures: {len(verdict.get('covered', []))}; retirements: {len(verdict.get('retired', []))}.")
    body = "\n".join(lines) + "\n"
    print(body)
    (args.output / "nightly-report.md").write_text(body, encoding="utf-8")
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(summary, "a", encoding="utf-8") as stream:
            stream.write(body)
    return 0 if verdict["verdict"] == "PASS" else 2 if verdict["verdict"] == "NO-RESULT" else 1


if __name__ == "__main__":
    raise SystemExit(main())
