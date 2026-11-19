"""Plan 39 K2/K3: compare complete native results against an immutable ceiling.

This file is executed from the trusted Git snapshot, never the candidate. It
uses only the standard library and reads candidate JSON as data. The same
implementation is vendored in the three products so their verdicts agree.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import datetime as dt
import hashlib
import gzip
import json
import os
from pathlib import Path
import re
import sys

SCHEMA = 1
SHA = re.compile(r"[0-9a-f]{40}\Z")
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
BAD_JOB = {"cancelled", "timed_out", "skipped", "action_required", "neutral", "stale", None}


class InvalidEvidence(ValueError):
    """Evidence cannot establish a complete, correctly identified run."""


def require(condition, message):
    if not condition:
        raise InvalidEvidence(message)


def read_json(path):
    try:
        path = Path(path)
        raw = gzip.decompress(path.read_bytes()) if path.suffix == ".gz" else path.read_bytes()
        return json.loads(raw)
    except (OSError, ValueError) as exc:
        raise InvalidEvidence(f"missing or invalid {path}: {exc}") from exc


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def job_key(row):
    return (row["workflow"], row["job"])


def identity(row):
    """No title-only matching, parameter masking, or subtest deduplication."""
    return (row["lane"], row["native_id"], canonical(row.get("parameter")),
            canonical(row.get("subtest")))


def key_text(key):
    return canonical(list(key))


def diagnostic(text):
    """A narrow error-message normalization, not a causal equivalence claim.

    Keep values, hashes, error codes and parameters. Only known runner checkout
    prefixes, pytest temporary directory roots and memory addresses vary. A
    different failing stage or error remains a different failure.
    """
    value = ANSI.sub("", str(text)).replace("\r\n", "\n").strip()
    value = re.sub(r"(?:/home/runner/work|/Users/runner/work|D:[/\\]a)[/\\](astrid-(?:agent|runtime|app))[/\\]\1[/\\]", "<checkout>/", value)
    value = re.sub(r"(?:/tmp|/private/tmp|/private/var/folders/[^/]+/[^/]+/T)/pytest-of-[^/\s]+/pytest-\d+/", "<pytest>/", value)
    value = re.sub(r" at 0x[0-9a-fA-F]+(?=>)", " at <address>", value)
    return value


def error_message(detail):
    """Retain pytest's exception text instead of treating its traceback as ID."""
    text = ANSI.sub("", str(detail)).strip()
    errors = re.findall(r"^E\s+(.*)$", text, re.MULTILINE)
    # Error lines preserve assertion values and multiline exception details.
    if errors:
        return diagnostic("\n".join(errors))
    return diagnostic(text)


def index_unique(rows, key, label):
    result = {}
    require(isinstance(rows, list), f"{label} is not a list")
    for row in rows:
        require(isinstance(row, dict), f"invalid {label} record")
        ident = key(row)
        require(ident not in result, f"duplicate {label}: {ident}")
        result[ident] = row
    return result


def validate_ledger(ledger, repository):
    require(ledger.get("schema") == SCHEMA and ledger.get("repository") == repository,
            "ledger schema/repository mismatch")
    entries = index_unique(ledger.get("entries"), identity, "ceiling entry")
    retired = index_unique(ledger.get("retired"), identity, "retired entry")
    require(not (entries.keys() & retired.keys()), "retired failure reintroduced in ledger")
    for key, row in entries.items():
        require(all(isinstance(row.get(k), str) and row[k] for k in ("lane", "native_id", "owner", "stage", "diagnostic")),
                f"incomplete ceiling identity/provenance: {key}")
        require(row.get("admission") == "explained-inherited", f"unexplained ceiling entry: {key}")
        require(row.get("release") == 15, f"entry has no Release 15 owner: {key}")
        g1, g2 = row.get("g1", {}), row.get("g2", {})
        for label, witness in (("G1", g1), ("G2", g2)):
            require(witness.get("outcome") == "failed" and witness.get("stage") == row["stage"]
                    and witness.get("diagnostic") == row["diagnostic"]
                    and witness.get("lane") == row["lane"]
                    and witness.get("native_id") == row["native_id"]
                    and witness.get("parameter") == row.get("parameter")
                    and witness.get("subtest") == row.get("subtest")
                    and SHA.fullmatch(witness.get("sha", ""))
                    and witness.get("run_id") and witness.get("job_id")
                    and witness.get("source"), f"{label} admission does not match lane/parameter/stage/diagnostic: {key}")
    return entries, retired


def load_policy(root, repository):
    root = Path(root)
    checks = read_json(root / "ci/required-checks.json")
    ledger = read_json(root / "ci/failure-ceiling.json")
    inventory = read_json(root / "ci/test-inventory.json.gz")
    require(checks.get("version") == SCHEMA and checks.get("repository") == repository,
            "required job inventory schema/repository mismatch")
    for tier in ("merge", "release"):
        require(checks.get(tier), f"empty {tier} job inventory")
        index_unique(checks[tier], job_key, f"{tier} required job")
    require(set(map(job_key, checks["merge"])) <= set(map(job_key, checks["release"])),
            "release tier omits merge jobs")
    validate_ledger(ledger, repository)
    require(inventory.get("schema") == SCHEMA and inventory.get("repository") == repository,
            "test inventory schema/repository mismatch")
    index_unique(inventory.get("tests"), identity, "expected test")
    return checks, ledger, inventory


def required_jobs(checks, tier, retirement_lanes=()):
    result = index_unique(checks[tier], job_key, "required job")
    for lane in retirement_lanes:
        matching = [spec for spec in checks["release"] if any(c.get("lane") == lane for c in spec.get("commands", []))]
        require(matching, f"retirement lane has no trusted job: {lane}")
        for spec in matching:
            result[job_key(spec)] = spec
    return result


def verify_suite(suite, lane):
    """Validate the append-only reporter's selected, executed and end records."""
    require(suite.get("complete") is True, f"incomplete results: {lane}/{suite.get('name')}")
    require(type(suite.get("raw_exit")) is int and suite["raw_exit"] in (0, 1),
            f"interrupted or non-test exit: {lane}/{suite.get('name')}")
    expected = suite.get("collected")
    require(isinstance(expected, list) and expected and len(expected) == len(set(expected)),
            f"empty/duplicate collected IDs: {lane}/{suite.get('name')}")
    tests = index_unique(suite.get("tests"), identity, "executed test")
    primary = [r for r in tests.values() if r.get("subtest") is None]
    require(Counter(r["native_id"] for r in primary) == Counter(expected),
            f"selected/executed ID mismatch: {lane}/{suite.get('name')}")
    require(all(r["lane"] == lane for r in tests.values()), "suite contains another lane")
    require(suite.get("summary_count") == len(expected), f"truncated result count: {lane}/{suite.get('name')}")
    failed = False
    for row in tests.values():
        require(row.get("outcome") in {"passed", "failed", "skipped", "xfailed"},
                f"unfinished result: {identity(row)}")
        if row["outcome"] == "failed":
            failed = True
            require(row.get("stage") and row.get("diagnostic"), f"failure lacks stage/diagnostic: {identity(row)}")
        if row.get("subtest") is not None:
            require(row["native_id"] in expected, "subtest has no collected parent")
    require(not failed or suite["raw_exit"] == 1, "test failure reported with a successful raw exit")
    require(failed or suite["raw_exit"] == 0, "nonzero test exit has no explained failure")
    return tests


def evaluate(trusted, proposed, report, *, repository, head, tier, expected_tuple, now=None):
    """Return a complete verdict, never reinterpret missing evidence as zero."""
    errors, unknown, covered, retiring, warnings, passing_receipts = [], [], [], [], [], []
    now = now or dt.datetime.now(dt.timezone.utc)
    try:
        require(tier in {"merge", "release"}, "invalid tier")
        require(SHA.fullmatch(head), "head must be an immutable commit")
        require(isinstance(expected_tuple, dict) and expected_tuple.get(repository) == head
                and all(isinstance(v, str) and SHA.fullmatch(v) for v in expected_tuple.values()),
                "expected tuple lacks immutable product/dependency SHAs")
        checks, ledger, inventory = trusted
        candidate_checks, candidate_ledger, candidate_inventory = proposed
        require(candidate_checks == checks, "candidate changed its required job inventory")
        # Historical admissions and reviewed reconciliation cannot approve themselves.
        require({k: v for k, v in candidate_ledger.items() if k not in {"entries", "retired"}}
                == {k: v for k, v in ledger.items() if k not in {"entries", "retired"}},
                "candidate changed sealed ledger authority/provenance")
        old, retired = validate_ledger(ledger, repository)
        new, new_retired = validate_ledger(candidate_ledger, repository)
        require(new.keys() <= old.keys(), "ceiling entry added or substituted")
        for key in new:
            require(new[key] == old[key], f"ceiling entry edited/swapped: {key}")
        require(retired.keys() <= new_retired.keys(), "retirement history removed")
        for key in retired:
            require(new_retired[key] == retired[key], f"retirement receipt edited: {key}")
        require(new_retired.keys() - retired.keys() == old.keys() - new.keys(),
                "removed entries and appended retirement receipts do not agree")
        require(report.get("schema") == SCHEMA and report.get("repository") == repository
                and report.get("head") == head and report.get("tier") == tier
                and report.get("tuple") == expected_tuple, "wrong repository/head/tier/tuple evidence")
        require(report.get("complete") is True, "aggregate collection did not complete")
        # A PR retiring a release-only failure must bring that lane's actual
        # passing job too. Ordinary merge checks cannot manufacture its pass.
        removed_lanes = {old[key]["lane"] for key in old.keys() - new.keys()}
        tier_lanes = set(inventory.get("tiers", {}).get(tier, []))
        extra_lanes = removed_lanes - tier_lanes
        required = required_jobs(checks, tier, extra_lanes)
        observed = index_unique(report.get("jobs"), job_key, "observed job")
        for key in sorted(required.keys() - observed.keys()):
            unknown.append(f"missing required job/nightly: {key[0]} / {key[1]}")
        tests, receipt_by_key, location_by_key = {}, {}, {}
        for key, spec in required.items():
            job = observed.get(key)
            if job is None:
                continue
            try:
                require(job.get("status") == "completed" and job.get("conclusion") not in BAD_JOB,
                        f"incomplete job: {key}: {job.get('status')}/{job.get('conclusion')}")
                job_tuple = {**spec.get("dependency_tuple", {}), **expected_tuple}
                require(job.get("head") == head and job.get("tuple") == job_tuple,
                        f"wrong job commit/dependency tuple: {key}")
                require(job.get("run_id") and job.get("job_id") and type(job.get("attempt")) is int,
                        f"missing run/job/attempt custody: {key}")
                if tier == "release":
                    when = dt.datetime.fromisoformat(job["started_at"].replace("Z", "+00:00"))
                    require(when.date() == now.date() and dt.timedelta(0) <= now - when <= dt.timedelta(hours=24),
                            f"stale/missing nightly: {key}")
                suites = job.get("suites", [])
                require(isinstance(suites, list), f"invalid suites: {key}")
                if spec["kind"] in {"tests", "mixed"}:
                    require(suites, f"no per-ID results: {key}")
                declared_suites = spec.get("suites")
                if declared_suites is not None:
                    require(Counter(s.get("name") for s in suites) == Counter(declared_suites),
                            f"missing/added command result: {key}")
                job_tests = {}
                for suite in suites:
                    items = verify_suite(suite, suite["lane"])
                    require(not (job_tests.keys() & items.keys()), f"duplicate invocation identity: {key}")
                    job_tests.update(items)
                require(not (tests.keys() & job_tests.keys()), f"duplicate ID across shards/jobs: {key}")
                tests.update(job_tests)
                for ident in job_tests:
                    location_by_key[ident] = {"source": "qualifying-run", "workflow": key[0], "job": key[1]}
                    receipt_by_key[ident] = {"head": head, "tuple": job_tuple,
                                            "run_id": job["run_id"], "job_id": job["job_id"],
                                            "attempt": job["attempt"], "outcome": "passed"}
                failed_steps = job.get("unexplained_failed_steps", [])
                require(isinstance(failed_steps, list) and not failed_steps,
                        f"uncovered non-test/earlier-stage failure: {key}: {failed_steps}")
                if spec["kind"] in {"check", "journey"}:
                    require(job["conclusion"] == "success", f"failed non-test/journey: {key}")
                    require(job.get("commands_complete") is True, f"missing command/stage receipt: {key}")
                if spec["kind"] == "aggregate":
                    # The original aggregate may be red because covered children are red.
                    require(job.get("dependencies") == spec.get("dependencies") and spec.get("dependencies"),
                            f"aggregate dependency inventory unavailable: {key}")
                elif job["conclusion"] == "failure":
                    require(any(r["outcome"] == "failed" for r in job_tests.values()),
                            f"failed job has no attributable native failure: {key}")
            except (InvalidEvidence, KeyError, TypeError, ValueError) as exc:
                unknown.append(str(exc))
        wanted = index_unique(inventory.get("tests"), identity, "trusted expected test")
        for key, spec in required.items():
            job = observed.get(key, {})
            if spec["kind"] == "aggregate" and job.get("conclusion") == "failure":
                children = [observed.get(job_key(child), {}) for child in spec["dependencies"]]
                if not any(t["outcome"] == "failed" for child in children for suite in child.get("suites", []) for t in suite.get("tests", [])):
                    errors.append(f"aggregate failed without a failing child: {key}")
        changed = index_unique(candidate_inventory.get("tests"), identity, "proposed expected test")
        require({k: v for k, v in candidate_inventory.items() if k != "tests"}
                == {k: v for k, v in inventory.items() if k != "tests"}, "candidate changed inventory authority/reconciliation")
        require(wanted.keys() <= changed.keys(), "expected test removed or renamed without trusted reconciliation")
        for key in wanted:
            require(changed[key] == wanted[key], f"expected test/skip allowance edited: {key}")
        active_lanes = {s["lane"] for j in observed.values() for s in j.get("suites", [])}
        required_lanes = set(inventory.get("tiers", {}).get(tier, []))
        for lane in sorted(required_lanes - active_lanes):
            unknown.append(f"missing lane evidence: {lane}")
        for key, row in wanted.items():
            if row["lane"] in required_lanes and key not in tests:
                errors.append(f"lost ID: {key_text(key)}")
        # Each future base retains new collected IDs. An addition needs an actual
        # passing receipt, preventing a speculative ID from hiding a future loss.
        for key in changed.keys() - wanted.keys():
            require(key in tests and tests[key]["outcome"] == "passed",
                    f"new inventory ID lacks a passing receipt: {key}")
            actual = tests[key]
            expected = {name: actual.get(name) for name in ("lane", "native_id", "parameter", "subtest")}
            if "source" in actual:
                expected["source"] = actual["source"]
            require(changed[key] == expected, f"new inventory ID cannot grant an omission or change its source: {key}")
        for key in tests.keys() - changed.keys():
            errors.append(f"observed ID must be retained in the inventory: {key_text(key)}")
        for key, actual in tests.items():
            if "source" in wanted.get(key, {}):
                require(actual.get("source") == wanted[key]["source"], f"native ID source binding changed: {key}")
            if actual["outcome"] == "failed":
                if key in retired or key in new_retired:
                    errors.append(f"retired failure reintroduced: {key_text(key)}")
                elif key not in new:
                    errors.append(f"new red: {key_text(key)}: {actual['diagnostic']}")
                elif (actual["stage"], diagnostic(actual["diagnostic"])) != (new[key]["stage"], new[key]["diagnostic"]):
                    errors.append(f"same-ID different-cause/stage: {key_text(key)}: {actual['stage']}: {actual['diagnostic']}")
                else:
                    covered.append(key_text(key))
            elif actual["outcome"] in {"skipped", "xfailed"}:
                allowance = wanted.get(key, {}).get("allowed_omission")
                require(allowance is not None and allowance.get("outcome") == actual["outcome"]
                        and allowance.get("diagnostic") and allowance.get("source")
                        and allowance.get("diagnostic") == diagnostic(actual.get("diagnostic", "")),
                        f"new skip/xfail/deselection: {key}")
                if expiry := allowance.get("expires"):
                    expires = dt.date.fromisoformat(expiry)
                    require(expires >= now.date(), f"expired quarantine: {key}")
                    if expires <= now.date() + dt.timedelta(days=3):
                        warnings.append(f"quarantine expires {expiry}: {key_text(key)}")
            elif key in old:
                # A primary parent pass does not retire any subtest entry.
                require(not actual.get("subtests_failed"), f"parent pass has failing subtests: {key}")
                if key in new:
                    errors.append(f"passing entry must be retired: {key_text(key)}")
                else:
                    receipt = new_retired[key].get("receipt")
                    # A commit cannot contain its own future Actions run/job ID.
                    # A selector requests proof from this complete qualifying
                    # run; the trusted verdict emits the exact immutable receipt.
                    require(receipt == receipt_by_key[key] or receipt == location_by_key[key],
                            f"retirement lacks exact passing receipt: {key}")
                    retiring.append(key_text(key))
                    passing_receipts.append({"identity": list(key), **location_by_key[key], **receipt_by_key[key]})
        for key in old.keys() - new.keys():
            require(key in tests and tests[key]["outcome"] == "passed", f"retirement without execution/pass: {key}")
        for unresolved in inventory.get("reconciliation", []):
            if unresolved.get("status") != "resolved":
                unknown.append(f"unreconciled historical evidence: {unresolved.get('id')}: {unresolved.get('reason')}")
    except (InvalidEvidence, KeyError, TypeError, ValueError) as exc:
        errors.append(str(exc))
    return {"schema": SCHEMA, "repository": repository, "head": head, "tier": tier,
            "tuple": expected_tuple, "verdict": "FAIL" if errors else "NO-RESULT" if unknown else "PASS",
            "errors": errors, "unknown": unknown, "covered": covered, "retired": retiring,
            "retirement_receipts": passing_receipts, "warnings": warnings, "raw_outcomes_preserved": True}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--tier", choices=("merge", "release"), required=True)
    parser.add_argument("--tuple", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    trusted_root = Path(__file__).resolve().parents[2]
    try:
        verdict = evaluate(load_policy(trusted_root, args.repository), load_policy(args.candidate, args.repository),
                           read_json(args.report), repository=args.repository, head=args.head, tier=args.tier,
                           expected_tuple=read_json(args.tuple))
    except (InvalidEvidence, KeyError, TypeError, ValueError) as exc:
        verdict = {"verdict": "NO-RESULT", "errors": [], "unknown": [str(exc)]}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(verdict, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(verdict, indent=2))
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(summary, "a", encoding="utf-8") as stream:
            stream.write(f"## Plan 39 {args.tier} failure ceiling: {verdict['verdict']}\n\n")
            for label in ("errors", "unknown", "warnings"):
                for message in verdict.get(label, []):
                    stream.write(f"- {label}: {message}\n")
            stream.write(f"\nCovered failures: {len(verdict.get('covered', []))}; retirements: {len(verdict.get('retired', []))}. Raw test exits are unchanged.\n")
    return 0 if verdict["verdict"] == "PASS" else 2 if verdict["verdict"] == "NO-RESULT" else 1


if __name__ == "__main__":
    raise SystemExit(main())
