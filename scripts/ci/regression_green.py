#!/usr/bin/env python3
"""Fail-closed P-04 ledger evaluation for a sealed tuple batch.

This module deliberately has no network, broker, or producer-execution API.
The coordinator supplies a private evidence directory only after source
identity and artifact hashes have been checked.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path
import xml.etree.ElementTree as ET

import jsonschema


REPOS = ("runtime", "agent", "app", "pinokio")


def canonical_digest(value: object) -> str:
    """Return the P-04 canonical JSON SHA-256 for *value*."""
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def strict_json_bytes(raw: bytes) -> object:
    def reject_duplicates(items: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    def reject_constant(value: str) -> None:
        raise ValueError(f"invalid JSON constant: {value}")

    return json.loads(raw.decode("utf-8"), object_pairs_hook=reject_duplicates, parse_constant=reject_constant)


def read_json(path: str | Path) -> object:
    return strict_json_bytes(Path(path).read_bytes())


def require(condition: object, message: str) -> None:
    if not condition:
        raise ValueError(message)


def sha256(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def validate_schema(value: object, schema_path: str | Path) -> None:
    schema = read_json(schema_path)
    require(isinstance(schema, dict), "schema is not an object")
    jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker()).validate(value)


def tuple_identity(tup: object, schema_path: str | Path) -> tuple[str, dict[str, str]]:
    require(isinstance(tup, dict), "tuple is not an object")
    validate_schema(tup, schema_path)
    identity = canonical_digest({key: value for key, value in tup.items() if key != "digest"})
    require(tup.get("digest", identity) == identity, "tuple digest does not match contents")
    repos = tup.get("repos")
    require(isinstance(repos, dict) and all(repo in repos for repo in REPOS), "tuple missing required repos")
    shas = {repo: repos[repo].get("sha") for repo in repos}
    require(all(isinstance(sha, str) and len(sha) == 40 for sha in shas.values()), "invalid tuple repository SHA")
    return identity, shas


def _nonempty(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _read_ledger(path: str | Path, schema_path: str | Path) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        row = strict_json_bytes(line.encode("utf-8"))
        validate_schema(row, schema_path)
        require(isinstance(row, dict), f"ledger line {number} is not an object")
        for key in ("id", "lane", "node_id", "fingerprint", "owner", "rationale"):
            require(_nonempty(row.get(key)), f"ledger line {number}: empty {key}")
        baseline = row.get("baseline")
        require(isinstance(baseline, dict) and _nonempty(baseline.get("tuple_digest")) and _nonempty(baseline.get("run")),
                f"ledger line {number}: incomplete baseline")
        if "renamed_from" in row:
            require(_nonempty(row["renamed_from"]) and _nonempty(row.get("admitted_by")),
                    f"ledger line {number}: renamed row lacks reviewed mapping")
        rows.append(row)
    require(len({row["id"] for row in rows}) == len(rows), "duplicate ledger id")
    return rows


def _case_properties(case: ET.Element, label: str) -> dict[str, str | None]:
    values: dict[str, str | None] = {}
    for prop in case.findall("./properties/property"):
        key = prop.get("name")
        require(_nonempty(key) and key not in values, f"duplicate or invalid property in {label}")
        values[str(key)] = prop.get("value") if prop.get("value") is not None else prop.text
    return values


def evaluate(
    tuple_path: str | Path,
    inventory_path: str | Path,
    ledger_path: str | Path,
    evidence_dir: str | Path,
    freshness_path: str | Path,
    tuple_schema: str | Path,
    ledger_schema: str | Path,
    today: dt.date | None = None,
) -> tuple[list[str], list[str]]:
    """Evaluate complete P-04 evidence; malformed input is always red."""
    errors: list[str] = []
    retirement: list[str] = []
    today = today or dt.datetime.now(dt.timezone.utc).date()
    try:
        tup = read_json(tuple_path)
        identity, shas = tuple_identity(tup, tuple_schema)
        inventory = read_json(inventory_path)
        require(isinstance(inventory, dict) and inventory.get("tuple_digest") == identity,
                "inventory tuple mismatch")
        lanes = inventory.get("lanes")
        require(isinstance(lanes, list) and lanes, "inventory has no lanes")
        rows = _read_ledger(ledger_path, ledger_schema)
        freshness = read_json(freshness_path)
        require(isinstance(freshness, dict) and freshness.get("check") == "C-07" and
                freshness.get("result") == "pass" and freshness.get("tuple_digest") == identity and
                freshness.get("repo_shas") == shas, "missing, failing or wrong-tuple C-07 receipt")

        seen_lanes: set[tuple[str, str, str]] = set()
        observed_failures: set[tuple[str, str, str, str]] = set()
        observed_passes: set[tuple[str, str, str, str]] = set()
        for lane in lanes:
            require(isinstance(lane, dict), "invalid lane")
            key = (lane.get("repo"), lane.get("lane"), lane.get("platform"))
            require(key[0] in shas and _nonempty(key[1]) and key[2] in ("linux", "macos", "windows"),
                    f"invalid lane {key}")
            typed_key = (str(key[0]), str(key[1]), str(key[2]))
            require(typed_key not in seen_lanes, f"duplicate lane {typed_key}")
            seen_lanes.add(typed_key)
            expected = lane.get("tests")
            require(isinstance(expected, list) and expected and all(
                isinstance(item, dict) and _nonempty(item.get("node_id")) and type(item.get("protected")) is bool
                for item in expected), f"invalid test inventory for {typed_key}")
            expected_by_id = {str(item["node_id"]): item for item in expected}
            require(len(expected_by_id) == len(expected), f"duplicate test inventory for {typed_key}")
            base = Path(evidence_dir) / canonical_digest(list(typed_key))
            manifest = read_json(base / "manifest.json")
            require(isinstance(manifest, dict) and manifest.get("tuple_digest") == identity and
                    manifest.get("repo_shas") == shas and
                    [manifest.get(name) for name in ("repo", "lane", "platform")] == list(typed_key),
                    f"wrong tuple/lane evidence for {typed_key}")
            junit = (base / "junit.xml").read_bytes()
            require(manifest.get("junit_sha256") == hashlib.sha256(junit).hexdigest(),
                    f"junit digest mismatch for {typed_key}")
            root = ET.fromstring(junit)
            require(root.tag in ("testsuite", "testsuites"), f"invalid junit root for {typed_key}")
            require(not root.findall(".//testsuite/error") and not root.findall("./error"),
                    f"collection error for {typed_key}")
            cases = root.findall(".//testcase")
            require(cases, f"empty junit for {typed_key}")
            seen_cases: set[str] = set()
            for case in cases:
                properties = _case_properties(case, str(typed_key))
                node = properties.get("node_id")
                require(_nonempty(node) and node not in seen_cases, f"missing or duplicate node_id in {typed_key}")
                node = str(node)
                seen_cases.add(node)
                require(node in expected_by_id, f"unlisted test in {typed_key}: {node}")
                failures = case.findall("./failure") + case.findall("./error")
                skipped = case.findall("./skipped")
                require(len(failures) <= 1 and len(skipped) <= 1 and not (failures and skipped),
                        f"ambiguous outcome for {typed_key} {node}")
                if skipped:
                    errors.append(f"skipped test: {typed_key} {node}")
                    continue
                if not failures:
                    observed_passes.add((*typed_key, node))
                    continue
                phase, fingerprint, outcome = (properties.get(name) for name in ("phase", "fingerprint", "outcome"))
                require(phase in ("setup", "call", "teardown", "collection") and
                        outcome in ("failed", "error", "timeout") and _nonempty(fingerprint),
                        f"missing failure identity for {typed_key} {node}")
                require((failures[0].tag == "failure" and outcome in ("failed", "timeout")) or
                        (failures[0].tag == "error" and outcome in ("error", "timeout")),
                        f"inconsistent outcome for {typed_key} {node}")
                observed_failures.add((*typed_key, node))
                if phase == "collection" or expected_by_id[node]["protected"]:
                    errors.append(f"unwaivable failure: {typed_key} {node}")
                    continue
                matches = [
                    row for row in rows
                    if row["status"] == "admitted" and dt.date.fromisoformat(str(row["expires"])) >= today and
                    row["repo"] == typed_key[0] and row["lane"] == typed_key[1] and
                    row["platform"] in (typed_key[2], "any") and row["node_id"] == node and
                    row["outcome"] == outcome and row["phase"] == phase and row["fingerprint"] == fingerprint and
                    ("semantics_hash" not in row or row["semantics_hash"] == properties.get("semantics_hash"))
                ]
                if not matches:
                    errors.append(f"unmatched failure: {typed_key} {node}")
            missing = set(expected_by_id) - seen_cases
            if missing:
                errors.append(f"missing tests in {typed_key}: {sorted(missing)}")
        for row in rows:
            if row["status"] != "admitted":
                continue
            for repo, lane, platform, node in observed_passes:
                if row["repo"] == repo and row["lane"] == lane and row["platform"] in (platform, "any") and row["node_id"] == node and (repo, lane, platform, node) not in observed_failures:
                    retirement.append(f"{row['id']}: passing on {repo}/{lane}/{platform}; review for retirement")
    except (OSError, ValueError, KeyError, TypeError, ET.ParseError, json.JSONDecodeError, jsonschema.exceptions.ValidationError) as exc:
        errors.append(f"invalid or missing evidence: {type(exc).__name__}: {exc}")
    return errors, retirement


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("tuple", "inventory", "ledger", "evidence", "freshness", "tuple-schema", "ledger-schema"):
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args(argv)
    errors, retirement = evaluate(args.tuple, args.inventory, args.ledger, args.evidence, args.freshness,
                                  args.tuple_schema, args.ledger_schema)
    for item in retirement:
        print("RETIREMENT:", item)
    for item in errors:
        print("ERROR:", item)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
