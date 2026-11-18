"""Validate actual JUnit cases against the collected IDs and flushed pytest reports.

Pytest can report both a passed call and a teardown error for one item, and
subtests share their parent's JUnit case. Reconcile those explicit reports;
never infer completeness from a suite header or a positive test count alone.
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import re
from pathlib import Path
from xml.etree import ElementTree as ET

ANSI = re.compile(r"\x1b\[[0-9;]*m")
SUMMARY = re.compile(r"\b(\d+) (subtests passed|subtests failed|subtests skipped|passed|failed|errors?|skipped|xfailed|xpassed|deselected)\b")


def summary_counts(text):
    return collections.Counter({("error" if key == "errors" else key): int(count)
                                for count, key in SUMMARY.findall(text)})


def validate(xml_path: Path) -> dict:
    events_path = xml_path.with_suffix(".jsonl")
    log_path = xml_path.with_suffix(".log")
    reference_path = xml_path.with_suffix(".reference.json")
    try:
        events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
        reference = json.loads(reference_path.read_text(encoding="utf-8"))
        log = ANSI.sub("", log_path.read_text(encoding="utf-8"))
        root = ET.parse(xml_path).getroot()
    except (OSError, ValueError, ET.ParseError) as exc:
        raise ValueError(f"{xml_path.name}: missing, partial or invalid evidence: {exc}") from exc
    if not events or events[0].get("type") != "start" or events[-1].get("type") != "finish":
        raise ValueError(f"{xml_path.name}: partial report stream (no session completion)")
    finish = events[-1]
    if finish["exitstatus"] not in (0, 1) or not finish["summary"] or finish["summary"] not in log:
        raise ValueError(f"{xml_path.name}: terminal summary or successful session completion missing")
    inventories = [event for event in events if event.get("type") == "collection"]
    if len(inventories) != 1 or {k: v for k, v in inventories[0].items() if k != "type"} != reference:
        raise ValueError(f"{xml_path.name}: run inventory differs from collect-only reference")
    reports = [event for event in events if event.get("type") == "report"]
    selected = collections.Counter(reference["selected"])
    completed = collections.Counter(r["nodeid"] for r in reports if r["when"] == "teardown" and not r["subtest"])
    if not selected or completed != selected:
        raise ValueError(f"{xml_path.name}: incomplete tests: selected={sum(selected.values())} completed={sum(completed.values())}")
    if any(r["nodeid"] not in selected for r in reports):
        raise ValueError(f"{xml_path.name}: report for an unselected test")
    counted = collections.Counter(r["category"] for r in reports if r["category"])
    if reference["deselected"]:
        counted["deselected"] = len(reference["deselected"])
    if counted != summary_counts(finish["summary"]):
        raise ValueError(f"{xml_path.name}: per-test reports disagree with the -rA summary: {dict(counted)}")
    suites = list(root.iter("testsuite"))
    # Each pytest invocation writes exactly one suite. Refuse unfamiliar nesting
    # instead of accidentally counting an aggregate and its children twice.
    if len(suites) != 1:
        raise ValueError(f"{xml_path.name}: expected one pytest suite, got {len(suites)}")
    suite = suites[0]
    cases = list(suite.findall("testcase"))
    if not cases or len(cases) != len(list(root.iter("testcase"))):
        raise ValueError(f"{xml_path.name}: empty or nested testcase list")
    by_item = collections.defaultdict(list)
    for report in reports:
        by_item[(report["classname"], report["name"])].append(report)
    xml_items = collections.defaultdict(list)
    for case in cases:
        xml_items[(case.get("classname"), case.get("name"))].append(case)
    if by_item.keys() != xml_items.keys():
        raise ValueError(f"{xml_path.name}: testcase identities differ from executed reports")
    totals = collections.Counter()
    expected_cases = 0
    double_failures = 0
    for key, item_reports in by_item.items():
        expected = collections.Counter()
        for report in item_reports:
            if report["outcome"] == "passed" and report["when"] == "call":
                expected["passed"] += 1
            elif report["outcome"] == "failed":
                expected["failures" if report["when"] == "call" else "errors"] += 1
            elif report["outcome"] == "skipped":
                expected["skipped"] += 1
        split = bool(expected["failures"] and any(r["when"] == "teardown" and r["outcome"] == "failed" for r in item_reports))
        double_failures += int(split)
        expected_cases += 1 + int(split)
        actual = collections.Counter()
        for case in xml_items[key]:
            actual["failures"] += len(case.findall("failure"))
            actual["errors"] += len(case.findall("error"))
            actual["skipped"] += len(case.findall("skipped"))
        if len(xml_items[key]) != 1 + int(split) or any(actual[k] != expected[k] for k in ("failures", "errors", "skipped")):
            raise ValueError(f"{xml_path.name}: incomplete or changed testcase {key}: XML={dict(actual)} reports={dict(expected)}")
        totals.update(expected)
    if len(cases) != expected_cases:
        raise ValueError(f"{xml_path.name}: actual testcase count does not match executed reports")
    totals["tests"] = sum(totals.values()) - double_failures
    for key in ("tests", "failures", "errors", "skipped"):
        if int(suite.get(key, "-1")) != totals[key]:
            raise ValueError(f"{xml_path.name}: forged/inconsistent {key} header: {suite.get(key)}; reports={totals[key]}")
    return {"file": xml_path.name, "testcases": len(cases), "items": sum(selected.values()),
            "summary": finish["summary"], "exitstatus": finish["exitstatus"]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--expected", type=int, required=True)
    args = parser.parse_args(argv)
    files = sorted(args.directory.glob("*.xml"))
    if not files:
        print("JUnit: no XML files; no test result available")
        return 1
    if len(files) != args.expected:
        print(f"JUnit: incomplete result set: expected={args.expected} found={len(files)}")
        return 1
    failed = False
    lines = []
    for path in files:
        try:
            result = validate(path)
            lines.append(f"{path.name}: verified {result['testcases']} testcase elements / {result['items']} items; {result['summary']}")
        except (ValueError, KeyError, TypeError) as exc:
            failed = True
            lines.append(f"ERROR: {exc}")
    for line in lines:
        print(line)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
