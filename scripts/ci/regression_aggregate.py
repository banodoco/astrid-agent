#!/usr/bin/env python3
"""Unconditional, host-authoritative aggregation for a sealed C-06 batch."""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
from typing import Any

import regression_green as ledger


STATUS_VERSION = "c06-host-verdict/v1"
CHECK = "int/regression-green"
QUAL_PREDICATE_SHA256 = "6ef495007d3f815eaad52c61d2ec7e86f8abca9e4287a48321c64f119e7021f4"
TERMINAL = frozenset(("completed", "infrastructure_failed", "unstartable", "skipped", "cancelled", "timed_out", "missing_at_deadline", "collection_failed"))



# Compatibility for the published CP1 diagnostic interface.
ROOT = Path(__file__).resolve().parents[2]
COVERAGE = ROOT / '.github/integration/cp1-coverage.json'
# This is a reviewed, version-controlled authority, never an evidence-provider input.
REVIEW = ROOT / '.github/integration/cp1-reviewed-inventory.json'
CP1 = {
    'runtime': '3b12fface79a3be029f3b6623765a7f3e1e5e24f',
    'agent': '0a47bf37e309741ef2a90d18739fa94758cf8f3f',
    'app': '8a05d31ae702ad83d8e61867d82f0fc2feb52ce3',
    'pinokio': '8d8e1b46948f6a0cdf8d3f81f4db3f14c21f11b2',
}
CUTOFF = '2026-10-31T23:36:50Z'


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def _evaluate_published(args):
    # Imports are deliberately deferred: missing evaluator dependencies must still emit red.
    import local_regression_green as local
    import regression_green as core
    tup = read(args.tuple)
    core.validate_schema(tup, 'tuple.schema.json')
    if set(tup['repos']) != set(CP1):
        raise ValueError('CP1 requires exactly four repositories')
    for repo, sha in CP1.items():
        if tup['repos'][repo]['sha'] != sha or tup['repos'][repo]['url'] != 'https://github.com/banodoco/astrid-' + repo:
            raise ValueError('wrong CP1 repository or SHA: ' + repo)
    if local.timestamp(args.not_before) < local.timestamp(CUTOFF):
        raise ValueError('cutoff predates CP1 publication')
    receipt = read(args.collection)
    if receipt != {'collection': 'success', 'prerequisites': 'success'}:
        raise ValueError('collection or prerequisite did not succeed')
    coverage = read(COVERAGE)
    reviewed = read(REVIEW)
    if reviewed.get('complete') is not True:
        raise ValueError('CP1 test-node and source inventory is not reviewed and complete')
    identity = core.digest({k: v for k, v in tup.items() if k != 'digest'})
    if reviewed.get('tuple_digest') != identity:
        raise ValueError('reviewed inventory tuple mismatch')
    features = reviewed.get('features')
    required_features = coverage.get('features')
    if reviewed.get('features_complete') is not True or not isinstance(features, list) or not features or not isinstance(required_features, list) or not required_features:
        raise ValueError('feature coverage and reviewed feature inventory are missing or incomplete')
    feature_key = lambda x: tuple(x[k] for k in ('repo', 'lane', 'platform', 'scenario', 'slot_id'))
    if (len({feature_key(x) for x in features}) != len(features) or
            len({feature_key(x) for x in required_features}) != len(required_features) or
            {feature_key(x) for x in features} != {feature_key(x) for x in required_features}):
        raise ValueError('reviewed feature inventory does not match required coverage')
    lanes = reviewed['lanes']
    key = lambda x: (x['repo'], x['lane'], x['platform'])
    required = coverage['lanes']
    if (len({key(x) for x in required}) != len(required) or
            len({key(x) for x in lanes}) != len(lanes) or
            {key(x) for x in required} != {key(x) for x in lanes}):
        raise ValueError('reviewed inventory does not match fixed G3 coverage')
    by_key = {key(x): x for x in lanes}
    for row in required:
        lane = by_key[key(row)]
        if lane['source']['kind'] != row['kind']:
            raise ValueError('wrong evidence transport: ' + str(key(row)))
        if row['protected'] and not all(t.get('protected') is True for t in lane['tests']):
            raise ValueError('protected lane contains waivable tests: ' + str(key(row)))
    with tempfile.TemporaryDirectory(prefix='regression-aggregate-') as tmp:
        inventory = Path(tmp) / 'inventory.json'
        inventory.write_text(json.dumps({'tuple_digest': identity, 'lanes': lanes}), encoding='utf-8')
        return local.local_evaluate(args.tuple, inventory, COVERAGE, args.ledger,
                                    args.broker_root, args.hosted_root, args.freshness,
                                    args.not_before, features=features)



def _sha256(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _canonical(value: object) -> str:
    return ledger.canonical_digest(value)


def _atomic_json(path: str | Path, value: dict[str, Any]) -> None:
    output = Path(path)
    output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".c06-status-", dir=output.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, output)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _validate_features(path: str | Path, predicate_path: str | Path, predicate_sha256: str,
                       tuple_value: dict[str, object], snapshots=None) -> list[str]:
    """Check selected byte snapshots; caller must first authenticate their provenance.

    This helper does not authenticate the index or source. No production path
    may call it until the missing source-to-index bridge has been established.
    """
    if predicate_sha256 != QUAL_PREDICATE_SHA256:
        return ["unapproved QUAL predicate SHA-256"]
    if _sha256(predicate_path) != predicate_sha256:
        return ["predicate SHA-256 mismatch"]
    features = ledger.read_json(path)
    if not isinstance(features, list) or not features:
        return ["required feature bindings are missing or malformed"]
    if not isinstance(snapshots, dict):
        return ["authenticated feature snapshots are unavailable"]
    try:
        import local_regression_green as local
        # The approved file was published in the scripts package; its relative
        # imports must resolve there, not under an invented top-level name.
        spec = importlib.util.spec_from_file_location("scripts.journey_predicates", predicate_path)
        if spec is None or spec.loader is None:
            raise ValueError("predicate module is unavailable")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    except Exception as exc:
        return [f"predicate import failed: {type(exc).__name__}: {exc}"]
    errors: list[str] = []
    for feature in features:
        try:
            if not isinstance(feature, dict):
                raise ValueError("feature is not an object")
            scenario, slot_id, candidate, files = (feature.get(name) for name in ("scenario", "slot_id", "candidate", "files"))
            if not all(isinstance(value, str) and value for value in (scenario, slot_id)):
                raise ValueError("invalid feature binding")
            repos = tuple_value["repos"]
            expected = {name: repos[name]["sha"] for name in ("runtime", "agent", "app", "pinokio")}
            if isinstance(candidate, dict) and "manifest_sha256" in candidate:
                manifest = tuple_value["generated"]["manifest_sha256"]
                if not isinstance(manifest, str) or len(manifest) != 64 or any(c not in "0123456789abcdef" for c in manifest):
                    raise ValueError("invalid frozen manifest SHA-256")
                expected["manifest_sha256"] = manifest
            if candidate != expected or (scenario == "m4_6" and "manifest_sha256" not in expected):
                raise ValueError("feature candidate tuple mismatch")
            if type(feature.get("attempt")) is not int or feature["attempt"] <= 0:
                raise ValueError("invalid feature attempt")
            if not isinstance(files, list) or not files or len(set(map(local.safe_path, files))) != len(files):
                raise ValueError("missing or duplicate feature files")
            key = tuple(feature[name] for name in ("repo", "lane", "platform", "scenario", "slot_id", "attempt"))
            selected = snapshots[key]
            entries = []
            for name in files:
                digest, raw = selected[name]
                if not isinstance(raw, bytes) or hashlib.sha256(raw).hexdigest() != digest:
                    raise ValueError("feature snapshot SHA-256 mismatch")
                entries.append({"path": name, "sha256": digest, "json": local.strict_json(raw)})
            predicate = getattr(module, "check_" + scenario, None)
            if not callable(predicate):
                raise ValueError("missing predicate: " + scenario)
            result = predicate({"slot_id": slot_id, "candidate": candidate, "files": entries})
            if not (isinstance(result, tuple) and len(result) == 2 and type(result[0]) is bool and
                    isinstance(result[1], list) and all(isinstance(item, str) for item in result[1])):
                raise ValueError("malformed predicate result")
            if not result[0] or result[1]:
                errors.append(f"feature {scenario}/{slot_id}: " + ("; ".join(result[1]) or "predicate returned false"))
        except Exception as exc:
            errors.append(f"feature predicate failed: {type(exc).__name__}: {exc}")
    return errors


def _metadata(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "schema_version": STATUS_VERSION,
        "check": CHECK,
        "host_verdict": True,
        "batch_id": args.batch_id,
        "generation": args.generation,
        "invocation_id": args.invocation_id,
        "tuple_digest": args.tuple_digest,
        "target_sha": args.target_sha,
        "sealed_input_digest": args.sealed_input_digest,
        "predicate_sha256": args.predicate_sha256,
        "artifact_index_digest": args.artifact_index_digest,
        "evaluated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }


def evaluate_sealed(sealed_input: object, evaluator=None) -> dict[str, object]:
    """Aggregate a coordinator-sealed result set exactly once.

    The finalizer invokes this function unconditionally. The legacy callback
    parameter is ignored: this payload lacks authenticated evaluator inputs.
    """
    if not isinstance(sealed_input, dict):
        return {"result": "fail", "reasons": ["aggregate sealed input is malformed"]}
    results = sealed_input.get("producer_results")
    if not isinstance(results, list) or not results:
        return {"result": "fail", "reasons": ["aggregate sealed result set is missing"]}
    errors: list[str] = []
    seen: set[str] = set()
    for result in results:
        if not isinstance(result, dict) or not isinstance(result.get("producer_id"), str) or result["producer_id"] in seen:
            errors.append("aggregate sealed producer result is malformed or duplicate")
            continue
        seen.add(result["producer_id"])
        outcome = result.get("outcome")
        if outcome not in TERMINAL:
            errors.append("aggregate producer %s has a nonterminal outcome" % result["producer_id"])
        elif outcome != "completed":
            errors.append("producer %s: %s" % (result["producer_id"], outcome))
    if errors:
        return {"result": "fail", "reasons": errors}
    # The coordinator payload has no authenticated index, feature bytes,
    # ledger or freshness bindings. A callback cannot supply that authority.
    return {"result": "fail", "reasons": ["reviewed aggregate evaluator is unavailable: authenticated source, index and feature bindings are missing"]}



def _reviewed_requirements(inventory_path: str | Path, features_path: str | Path, identity: str) -> None:
    """Require the fixed coverage and the independent reviewed authority, not caller assertions."""
    coverage, review = ledger.read_json(COVERAGE), ledger.read_json(REVIEW)
    inventory, features = ledger.read_json(inventory_path), ledger.read_json(features_path)
    ledger.require(isinstance(coverage, dict) and isinstance(review, dict), "missing reviewed authority")
    ledger.require(review.get("complete") is True and review.get("features_complete") is True and
                   review.get("tuple_digest") == identity, "reviewed authority incomplete or wrong tuple")
    lane_key = lambda row: tuple(row[k] for k in ("repo", "lane", "platform"))
    feature_key = lambda row: tuple(row[k] for k in ("repo", "lane", "platform", "scenario", "slot_id"))
    def unique_rows(rows, key, label):
        ledger.require(isinstance(rows, list) and rows and all(isinstance(row, dict) for row in rows),
                       "missing " + label)
        mapped = {key(row): row for row in rows}
        ledger.require(len(mapped) == len(rows) and all(all(isinstance(v, str) and v.strip() for v in k) for k in mapped),
                       "duplicate or malformed " + label)
        return mapped
    required = unique_rows(coverage.get("lanes"), lane_key, "coverage lanes")
    reviewed = unique_rows(review.get("lanes"), lane_key, "reviewed lanes")
    ledger.require(len(required) == 71 and set(required) == set(reviewed), "reviewed inventory differs from 71-lane coverage")
    ledger.require(isinstance(inventory, dict) and inventory.get("tuple_digest") == identity, "inventory tuple mismatch")
    supplied = unique_rows(inventory.get("lanes"), lane_key, "inventory lanes")
    ledger.require(supplied == reviewed, "inventory differs from reviewed lanes")
    for key, row in required.items():
        lane = reviewed[key]
        ledger.require(isinstance(lane.get("source"), dict) and lane["source"].get("kind") == row.get("kind"), "reviewed source mismatch")
        tests = lane.get("tests")
        ledger.require(isinstance(tests, list) and tests and all(isinstance(t, dict) and isinstance(t.get("node_id"), str) and t["node_id"].strip() and type(t.get("protected")) is bool for t in tests), "invalid reviewed tests")
        ledger.require(len({t["node_id"] for t in tests}) == len(tests), "duplicate reviewed test")
        ledger.require(not row.get("protected") or all(t["protected"] for t in tests), "unprotected reviewed test")
    required_features = unique_rows(coverage.get("features"), feature_key, "feature coverage")
    reviewed_features = unique_rows(review.get("features"), feature_key, "reviewed features")
    supplied_features = unique_rows(features, feature_key, "feature bindings")
    ledger.require(set(required_features) == set(reviewed_features) and supplied_features == reviewed_features,
                   "feature bindings differ from reviewed coverage")
    ledger.require(all(k[:3] in reviewed for k in reviewed_features), "feature references unknown lane")


def evaluate(args: argparse.Namespace) -> tuple[list[str], list[str]]:
    """Evaluate sealed results, or the published CP1 diagnostic arguments."""
    if not hasattr(args, "sealed_results"):
        return _evaluate_published(args)
    sealed = ledger.read_json(args.sealed_results)
    if not isinstance(sealed, dict):
        raise ValueError("sealed result set is not an object")
    expected_digest = _canonical(sealed)
    if expected_digest != args.sealed_input_digest:
        raise ValueError("sealed result digest mismatch")
    tup = ledger.read_json(args.tuple)
    actual_digest, repo_shas = ledger.tuple_identity(tup, args.tuple_schema)
    if actual_digest != args.tuple_digest or tup["repos"]["agent"]["sha"] != args.target_sha:
        raise ValueError("wrong tuple or target SHA")
    required = {"batch_id", "generation", "tuple_digest", "results", "artifact_index"}
    if set(sealed) != required or sealed["batch_id"] != args.batch_id or sealed["generation"] != args.generation or sealed["tuple_digest"] != actual_digest:
        raise ValueError("sealed result identity mismatch")
    results = sealed["results"]
    if not isinstance(results, list) or not results:
        raise ValueError("sealed result set is incomplete")
    if not isinstance(sealed["artifact_index"], (list, dict)) or not sealed["artifact_index"]:
        raise ValueError("required artifact index is missing or empty")
    if _canonical(sealed["artifact_index"]) != args.artifact_index_digest:
        raise ValueError("artifact index digest mismatch")
    errors: list[str] = []
    seen: set[str] = set()
    for result in results:
        if not isinstance(result, dict) or not isinstance(result.get("producer_id"), str) or result["producer_id"] in seen:
            raise ValueError("duplicate or malformed sealed producer result")
        seen.add(result["producer_id"])
        state = result.get("state")
        if state not in TERMINAL:
            raise ValueError("nonterminal producer result")
        if state != "completed":
            errors.append(f"producer {result['producer_id']}: {state}")
    if errors:
        return errors, []
    _reviewed_requirements(args.inventory, args.features, actual_digest)
    p04_errors, retirement = ledger.evaluate(args.tuple, args.inventory, args.ledger, args.evidence_root,
                                              args.freshness, args.tuple_schema, args.ledger_schema)
    errors.extend(p04_errors)
    # Do not interpret feature files until their source, index and bytes are
    # authenticated together. A digest supplied with an index is not authority.
    errors.append("sealed artifact index is not authenticated to reviewed sources and feature bytes")
    return errors, retirement



def _output_path(raw: list[str]) -> str:
    for index, item in enumerate(raw):
        if item.startswith("--status=") and item != "--status=":
            return item.split("=", 1)[1]
        if item == "--status" and index + 1 < len(raw) and not raw[index + 1].startswith("--"):
            return raw[index + 1]
    return "host-verdict.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--status", required=True)
    for name in ("sealed-results", "tuple", "inventory", "ledger", "evidence-root", "freshness", "tuple-schema", "ledger-schema",
                 "features", "predicate-path", "predicate-sha256", "batch-id", "generation", "invocation-id", "tuple-digest",
                 "target-sha", "sealed-input-digest", "artifact-index-digest"):
        parser.add_argument("--" + name, required=True, type=int if name == "generation" else None)
    raw = list(argv if argv is not None else __import__("sys").argv[1:])
    status_path = _output_path(raw)
    status: dict[str, Any] = {
        "schema_version": STATUS_VERSION, "check": CHECK, "host_verdict": True, "result": "fail", "reasons": [],
        "evaluated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    try:
        args = parser.parse_args(raw)
        status.update(_metadata(args))
        errors, retirement = evaluate(args)
        status["reasons"] = errors
        status["retirement"] = retirement
        status["result"] = "pass" if not errors else "fail"
    except SystemExit as exc:
        if exc.code == 0:
            return 0
        status["reasons"] = ["invalid or missing aggregate arguments"]
    except Exception as exc:
        status["reasons"] = [f"aggregate failed: {type(exc).__name__}: {exc}"]
    try:
        _atomic_json(status_path, status)
    except Exception:
        # An unwritable status has no authoritative host verdict.  The caller
        # must treat this as red; there is deliberately no fallback file.
        return 1
    print(json.dumps(status, sort_keys=True))
    return 0 if status["result"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
