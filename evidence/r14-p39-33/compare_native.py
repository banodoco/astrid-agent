"""Diagnostic native evidence comparison; does not change or replace gate policy.

Usage: python -I compare_native.py BEFORE_STEP AFTER_STEP OUTPUT_JSON
Each step contains bun-results.xml, command.log and command.json. Historical
duplicate observations remain separate; no native Boolean attribution is invented.
"""
from __future__ import annotations

import collections
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import sys
import xml.etree.ElementTree as ET

sys.dont_write_bytecode = True
REPOSITORY = Path(__file__).resolve().parents[2]
FILE = "astrid/chat/sessionConfigOptions.test.ts"
PREFIX = "reads Claude effort with recommendation capability "
OLD = [FILE, "20", "ACP session config options", PREFIX + "%s"]
NEW = [[FILE, "20", "ACP session config options", PREFIX + str(flag).lower()]
       for flag in (False, True)]


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def read_step(directory):
    receipt = json.loads((directory / "command.json").read_text())
    xml = ET.parse(directory / "bun-results.xml").getroot()
    observations, occurrences = [], collections.Counter()
    for suite in xml.iter("testsuite"):
        assert int(suite.attrib["tests"]) == len(list(suite.iter("testcase")))
        for case in suite.findall("testcase"):
            native = [case.get("file") or suite.get("file"), case.get("line"),
                      case.get("classname"), case.get("name")]
            occurrences[canonical(native)] += 1
            children = [{"tag": item.tag, "attributes": item.attrib, "text": item.text}
                        for item in case]
            outcome = ("failed" if any(c["tag"] in ("failure", "error") for c in children)
                       else "skipped" if any(c["tag"] == "skipped" for c in children)
                       else "passed")
            observations.append({"index": len(observations), "native_id": native,
                                 "occurrence": occurrences[canonical(native)],
                                 "assertions": int(case.get("assertions", "0")),
                                 "outcome": outcome, "diagnostics": children})
    terminal, current_file = [], None
    log = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", (directory / "command.log").read_text())
    for line in log.splitlines():
        if line == "::endgroup::" or re.fullmatch(r"\d+ tests? skipped:", line.strip()):
            current_file = None
            continue
        line = line.removeprefix("::group::")
        if re.fullmatch(r".+\.[cm]?[jt]sx?:", line):
            current_file = line[:-1]
            continue
        match = re.fullmatch(r"\((pass|fail|skip|todo)\) (.*?)(?: \[[^\]]+\])?", line)
        if match and current_file is not None:
            terminal.append({"file": current_file, "title": match[2], "outcome": match[1]})
    outcome_names = {"passed": "pass", "failed": "fail", "skipped": "skip"}
    expected_terminal = [{"file": row["native_id"][0],
                          "title": " > ".join(filter(None, row["native_id"][2:])),
                          "outcome": outcome_names[row["outcome"]]} for row in observations]
    terminal_matches = collections.Counter(map(canonical, terminal)) == collections.Counter(map(canonical, expected_terminal))
    summary = re.search(r"Ran (\d+) tests? across .*files?", log)
    assertion_summary = re.search(r"(\d+) expect\(\) calls", log)
    assert summary and int(summary[1]) == len(observations)
    assert assertion_summary and int(assertion_summary[1]) == sum(row["assertions"] for row in observations)
    assert int(xml.get("tests")) == len(observations)
    assert int(xml.get("assertions")) == sum(row["assertions"] for row in observations)
    assert terminal_matches
    spec = importlib.util.spec_from_file_location("base_adapters", REPOSITORY / "scripts/ci/ceiling_adapters.py")
    adapter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(adapter)
    suite = adapter.bun_junit(directory / "bun-results.xml", directory / "command.log",
                              "Chat service/chat", "step-06", receipt["raw_exit"])
    try:
        adapter.core.verify_suite(suite, "Chat service/chat")
        collector = {"accepted": True}
    except ValueError as error:
        collector = {"accepted": False, "reason": str(error)}
    return {"observations": observations, "terminal": terminal, "raw_exit": receipt["raw_exit"],
            "count": len(observations), "distinct_ids": len(occurrences),
            "assertions": sum(row["assertions"] for row in observations),
            "outcomes": dict(collections.Counter(row["outcome"] for row in observations)),
            "skips": [row for row in observations if row["outcome"] == "skipped"],
            "terminal_junit_multiset_equal": terminal_matches, "trusted_suite": collector,
            "native_files": {name: hashlib.sha256((directory / name).read_bytes()).hexdigest()
                             for name in ("bun-results.xml", "command.log", "command.json")}}


def comparable(row):
    return {key: row[key] for key in ("native_id", "assertions", "outcome", "diagnostics")}


def compare(before, after):
    old_pair = [row for row in before["observations"] if row["native_id"] == OLD]
    new_pair = [row for row in after["observations"] if row["native_id"] in NEW]
    assert len(old_pair) == len(new_pair) == 2
    assert [row["native_id"] for row in new_pair] == NEW
    assert all(row["outcome"] == "passed" and row["assertions"] == 2 for row in old_pair + new_pair)
    unchanged_before = [comparable(row) for row in before["observations"] if row["native_id"] != OLD]
    unchanged_after = [comparable(row) for row in after["observations"] if row["native_id"] not in NEW]
    original = collections.Counter(map(canonical, unchanged_before))
    proposal = collections.Counter(map(canonical, unchanged_after))
    lost, added = list((original - proposal).elements()), list((proposal - original).elements())
    def normalized_terminal(rows):
        result = []
        for row in rows:
            row = dict(row)
            if row["file"] == FILE and row["title"] in ["ACP session config options > " + name[3] for name in NEW]:
                row["title"] = "ACP session config options > " + OLD[3]
            result.append(row)
        return result
    def per_file(rows):
        result = collections.defaultdict(list)
        for row in rows:
            result[row["file"]].append(row)
        return dict(result)
    old_terminal, new_terminal = normalized_terminal(before["terminal"]), normalized_terminal(after["terminal"])
    return {"historical_duplicate_observations": old_pair, "proposed_distinct_observations": new_pair,
            "historical_boolean_attribution": "unknown; no per-observation parameter in original native record",
            "unchanged_observations": len(unchanged_before), "unchanged_native_roster": not lost and not added,
            "lost_or_changed_unaffected_rows": [json.loads(row) for row in lost],
            "added_or_changed_unaffected_rows": [json.loads(row) for row in added],
            "terminal_roster_equal_after_only_title_substitution": collections.Counter(map(canonical, old_terminal)) == collections.Counter(map(canonical, new_terminal)),
            "terminal_order_within_each_file_equal_after_only_title_substitution": per_file(old_terminal) == per_file(new_terminal),
            "raw_exit_equal": before["raw_exit"] == after["raw_exit"],
            "assertions_equal": before["assertions"] == after["assertions"],
            "outcomes_equal": before["outcomes"] == after["outcomes"]}


if __name__ == "__main__":
    before_dir, after_dir, output = map(Path, sys.argv[1:])
    before, after = read_step(before_dir), read_step(after_dir)
    comparison = compare(before, after)
    output.write_text(json.dumps({"before": before, "after": after, "comparison": comparison}, indent=2) + "\n")
    print(json.dumps({"before": {k: before[k] for k in ("count", "distinct_ids", "assertions", "outcomes", "raw_exit", "trusted_suite")},
                      "after": {k: after[k] for k in ("count", "distinct_ids", "assertions", "outcomes", "raw_exit", "trusted_suite")},
                      "comparison": comparison}, indent=2))
    assert comparison["unchanged_native_roster"]
    assert comparison["terminal_roster_equal_after_only_title_substitution"]
    assert comparison["raw_exit_equal"] and comparison["assertions_equal"] and comparison["outcomes_equal"]
