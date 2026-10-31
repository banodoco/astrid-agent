#!/usr/bin/env python3
"""Evaluate exact-tuple regression evidence. See .github/integration/README.md."""
import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path
import sys
import xml.etree.ElementTree as ET

import jsonschema

ROOT = Path(__file__).resolve().parents[2]
REPOS = ("runtime", "agent", "app", "pinokio")


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def validate_schema(value, filename):
    schema = read_json(ROOT / ".github/integration" / filename)
    jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker()).validate(value)


def nonempty(value):
    return isinstance(value, str) and bool(value.strip())


def evaluate(tuple_path, inventory_path, ledger_path, evidence_dir, freshness_path, today=None):
    errors, retirement = [], []
    today = today or dt.datetime.now(dt.timezone.utc).date()
    try:
        tup = read_json(tuple_path)
        validate_schema(tup, "tuple.schema.json")
        supplied = tup.get("digest")
        identity = digest({k: v for k, v in tup.items() if k != "digest"})
        require(supplied is None or supplied == identity, "tuple digest does not match contents")
        shas = {k: v["sha"] for k, v in tup["repos"].items()}
        require(all(k in shas for k in REPOS), "tuple missing required repos")
        inventory = read_json(inventory_path)
        require(isinstance(inventory, dict) and ("tuple_digest" not in inventory or inventory["tuple_digest"] == identity), "inventory tuple mismatch")
        lanes = inventory.get("lanes")
        require(isinstance(lanes, list) and lanes, "inventory has no lanes")
        rows = []
        for number, line in enumerate(Path(ledger_path).read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            row = json.loads(line)
            validate_schema(row, "ledger.schema.json")
            require(all(nonempty(row[k]) for k in ("id", "lane", "node_id", "fingerprint", "owner", "rationale")), f"ledger line {number}: empty identity")
            require(all(nonempty(row["baseline"][k]) for k in ("tuple_digest", "run")), f"ledger line {number}: missing baseline")
            require("semantics_hash" not in row or nonempty(row["semantics_hash"]), f"ledger line {number}: empty semantics hash")
            require("renamed_from" not in row or (nonempty(row["renamed_from"]) and nonempty(row.get("admitted_by"))), f"ledger line {number}: rename needs reviewed admission")
            rows.append(row)
        require(len({r["id"] for r in rows}) == len(rows), "duplicate ledger id")
        freshness = read_json(freshness_path)
        require(isinstance(freshness, dict) and freshness.get("tuple_digest") == identity and freshness.get("repo_shas") == shas and freshness.get("result") == "pass" and freshness.get("check") == "C-07", "missing, failing or wrong-tuple C-07 receipt")
        seen_lanes, observed_failures, observed_passes = set(), set(), set()
        for lane in lanes:
            require(isinstance(lane, dict), "invalid lane")
            repo, name, platform = (lane.get(k) for k in ("repo", "lane", "platform"))
            key = (repo, name, platform)
            require(repo in shas and nonempty(name) and platform in ("linux", "macos", "windows"), f"invalid lane {key}")
            require(key not in seen_lanes, f"duplicate lane {key}")
            seen_lanes.add(key)
            expected = lane.get("tests")
            require(isinstance(expected, list) and expected, f"empty test inventory for {key}")
            require(all(isinstance(x, dict) and nonempty(x.get("node_id")) and type(x.get("protected")) is bool for x in expected), f"invalid test inventory for {key}")
            expected_by_id = {x["node_id"]: x for x in expected}
            require(len(expected_by_id) == len(expected), f"duplicate test inventory for {key}")
            # Evidence file names derive from the identity, not from untrusted paths.
            lane_id = digest(list(key))
            base = Path(evidence_dir) / lane_id
            manifest = read_json(base / "manifest.json")
            require(manifest.get("tuple_digest") == identity and manifest.get("repo_shas") == shas and [manifest.get(k) for k in ("repo", "lane", "platform")] == list(key), f"wrong tuple/lane evidence for {key}")
            xml_bytes = (base / "junit.xml").read_bytes()
            require(manifest.get("junit_sha256") == hashlib.sha256(xml_bytes).hexdigest(), f"junit digest mismatch for {key}")
            root = ET.fromstring(xml_bytes)
            require(root.tag in ("testsuite", "testsuites"), f"invalid junit root for {key}")
            cases = root.findall(".//testcase")
            require(cases, f"empty junit for {key}")
            seen_tests = set()
            # A suite-level error indicates aborted collection, even when cases exist.
            require(not root.findall(".//testsuite/error") and not root.findall("./error"), f"collection error for {key}")
            for case in cases:
                props = {}
                for prop in case.findall("./properties/property"):
                    prop_name = prop.get("name")
                    require(nonempty(prop_name) and prop_name not in props, f"duplicate or invalid property in {key}")
                    props[prop_name] = prop.get("value") if prop.get("value") is not None else prop.text
                node = props.get("node_id")
                require(nonempty(node) and node not in seen_tests, f"missing or duplicate node_id in {key}")
                seen_tests.add(node)
                require(node in expected_by_id, f"unlisted test in {key}: {node}")
                failures = case.findall("./failure") + case.findall("./error")
                skipped = case.findall("./skipped")
                require(len(failures) <= 1 and len(skipped) <= 1 and not (failures and skipped), f"ambiguous outcome for {key} {node}")
                if skipped:
                    errors.append(f"skipped test: {key} {node}")
                    continue
                if failures:
                    phase, fingerprint, outcome = (props.get(k) for k in ("phase", "fingerprint", "outcome"))
                    require(phase in ("setup", "call", "teardown", "collection") and outcome in ("failed", "error", "timeout") and nonempty(fingerprint), f"missing failure identity for {key} {node}")
                    require((failures[0].tag == "failure" and outcome in ("failed", "timeout")) or (failures[0].tag == "error" and outcome in ("error", "timeout")), f"inconsistent outcome for {key} {node}")
                    observed_failures.add((*key, node))
                    if phase == "collection" or expected_by_id.get(node, {}).get("protected", False):
                        errors.append(f"unwaivable failure: {key} {node}")
                        continue
                    matches = [r for r in rows if r["status"] == "admitted" and dt.date.fromisoformat(r["expires"]) >= today and r["repo"] == repo and r["lane"] == name and r["platform"] in (platform, "any") and r["node_id"] == node and r["outcome"] == outcome and r["phase"] == phase and r["fingerprint"] == fingerprint and ("semantics_hash" not in r or r["semantics_hash"] == props.get("semantics_hash"))]
                    if not matches:
                        errors.append(f"unmatched failure: {key} {node}")
                else:
                    observed_passes.add((*key, node))
            missing = set(expected_by_id) - seen_tests
            if missing:
                errors.append(f"missing tests in {key}: {sorted(missing)}")
        for row in rows:
            if row["status"] != "admitted":
                continue
            for repo, lane, platform, node in observed_passes:
                if row["repo"] == repo and row["lane"] == lane and row["platform"] in (platform, "any") and row["node_id"] == node and (repo, lane, platform, node) not in observed_failures:
                    retirement.append(f"{row['id']}: passing on {repo}/{lane}/{platform}; review for retirement")
        return errors, retirement
    except (OSError, ValueError, KeyError, TypeError, ET.ParseError, jsonschema.exceptions.ValidationError) as exc:
        return errors + [f"invalid or missing evidence: {exc}"], retirement


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tuple", required=True)
    parser.add_argument("--inventory", required=True)
    parser.add_argument("--ledger", required=True)
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--freshness", required=True)
    args = parser.parse_args()
    errors, retirement = evaluate(args.tuple, args.inventory, args.ledger, args.evidence, args.freshness)
    for item in retirement:
        print("RETIREMENT:", item)
    for item in errors:
        print("ERROR:", item, file=sys.stderr)
    print("int/regression-green:", "FAIL" if errors else "PASS")
    return bool(errors)


if __name__ == "__main__":
    sys.exit(main())
