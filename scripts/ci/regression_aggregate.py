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


def _validate_features(path: str | Path, predicate_path: str | Path, predicate_sha256: str, tuple_value: dict[str, object]) -> list[str]:
    """Run optional reviewed feature predicates only after input provenance passed."""
    if predicate_sha256 != QUAL_PREDICATE_SHA256:
        return ["unapproved QUAL predicate SHA-256"]
    if _sha256(predicate_path) != predicate_sha256:
        return ["predicate SHA-256 mismatch"]
    features = ledger.read_json(path)
    if features is None:
        return []
    if not isinstance(features, list):
        return ["feature bindings are malformed"]
    try:
        spec = importlib.util.spec_from_file_location("c06_qual_predicates", predicate_path)
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
            if not all(isinstance(value, str) and value for value in (scenario, slot_id, candidate)) or not isinstance(files, list):
                raise ValueError("invalid feature binding")
            if scenario == "m4_6":
                generated = tuple_value.get("generated")
                if not isinstance(generated, dict) or candidate != generated.get("manifest_sha256"):
                    raise ValueError("m4_6 candidate is not derived from frozen tuple manifest_sha256")
            predicate = getattr(module, "check_" + scenario, None)
            if not callable(predicate):
                raise ValueError("missing predicate: " + scenario)
            result = predicate({"slot_id": slot_id, "candidate": candidate, "files": files})
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

    The finalizer invokes this function unconditionally.  A deployment may
    inject its reviewed P-04/QUAL evaluator as ``evaluator``; without that
    authority aggregation remains explicitly red.  No producer command or
    source-selection input exists at this boundary.
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
    if evaluator is None:
        return {"result": "fail", "reasons": ["reviewed aggregate evaluator is unavailable"]}
    outcome = evaluator(sealed_input)
    if not (isinstance(outcome, dict) and set(outcome) == {"result", "reasons"} and
            outcome["result"] in ("pass", "fail") and isinstance(outcome["reasons"], list) and
            all(isinstance(item, str) and item for item in outcome["reasons"])):
        return {"result": "fail", "reasons": ["reviewed aggregate evaluator returned malformed outcome"]}
    if outcome["result"] == "pass" and outcome["reasons"]:
        return {"result": "fail", "reasons": ["passing aggregate outcome contains reasons"]}
    return outcome


def evaluate(args: argparse.Namespace) -> tuple[list[str], list[str]]:
    """Evaluate all sealed terminal results.  Any non-completed result is red."""
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
    p04_errors, retirement = ledger.evaluate(args.tuple, args.inventory, args.ledger, args.evidence_root,
                                              args.freshness, args.tuple_schema, args.ledger_schema)
    errors.extend(p04_errors)
    # A predicate hash is always verified, even when an empty reviewed feature
    # binding list means no predicate function is selected for this batch.
    errors.extend(_validate_features(args.features, args.predicate_path, args.predicate_sha256, tup))
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
