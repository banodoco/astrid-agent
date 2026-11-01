#!/usr/bin/env python3
"""Evaluate one sealed C-06 result set without starting or replaying producers.

The finalizer's only executable work is trusted evaluation supplied by its
caller as an in-process function.  It intentionally has no command-plan or
child-process interface: producer execution belongs to the dispatcher, before the
batch is sealed.
"""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import tempfile

import regression_aggregate as aggregate


INPUT_VERSION = "c06-sealed-finalizer-input/v1"
STATUS_VERSION = "c06-finalizer-status/v1"
CHECK_NAME = "int/regression-green"


class FinalizerError(ValueError):
    pass


class AlreadyStarted(FinalizerError):
    """The accepted invocation has a durable start marker and may not rerun."""


def canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(value):
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _require(condition, message):
    if not condition:
        raise FinalizerError(message)


def _now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def validate_sealed_input(value):
    fields = {
        "schema_version", "check", "batch_id", "generation", "invocation_id", "tuple_digest",
        "target_sha", "sealed_results_digest", "tuple", "producer_results", "sealed_input_digest",
    }
    _require(isinstance(value, dict) and set(value) == fields, "invalid sealed finalizer input")
    _require(value["schema_version"] == INPUT_VERSION and value["check"] == CHECK_NAME,
             "wrong finalizer input version or check")
    _require(isinstance(value["batch_id"], str) and isinstance(value["invocation_id"], str) and
             type(value["generation"]) is int and value["generation"] > 0,
             "invalid finalizer invocation identity")
    for field in ("tuple_digest", "target_sha", "sealed_results_digest", "sealed_input_digest"):
        expected = 40 if field == "target_sha" else 64
        _require(isinstance(value[field], str) and len(value[field]) == expected and
                 all(character in "0123456789abcdef" for character in value[field]),
                 "invalid " + field)
    _require(isinstance(value["tuple"], dict) and isinstance(value["producer_results"], list) and
             value["producer_results"], "invalid sealed finalizer payload")
    body = {key: item for key, item in value.items() if key != "sealed_input_digest"}
    _require(digest(body) == value["sealed_input_digest"], "sealed finalizer input digest mismatch")
    _require(digest(value["producer_results"]) == value["sealed_results_digest"],
             "sealed producer result digest mismatch")
    return value


def _atomic_json(path, value):
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".c06-finalizer-", dir=destination.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(canonical_json(value))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        directory = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _start_marker(path, sealed):
    """Create and fsync an O_EXCL marker before a body can evaluate anything."""
    marker = Path(path)
    marker.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise AlreadyStarted("finalizer start marker already exists") from exc
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(canonical_json({
                "batch_id": sealed["batch_id"], "generation": sealed["generation"],
                "invocation_id": sealed["invocation_id"],
                "sealed_input_digest": sealed["sealed_input_digest"], "started_at": _now(),
            }))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        directory = os.open(marker.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except BaseException:
        # The marker represents an accepted start even if a process dies while
        # writing its body.  Never remove it here.
        raise


def status_for(sealed, result, reasons):
    _require(result in ("pass", "fail"), "evaluator returned an invalid result")
    _require(isinstance(reasons, list) and all(isinstance(item, str) and item for item in reasons),
             "evaluator returned invalid reasons")
    if result == "pass":
        _require(not reasons, "a passing finalizer status cannot contain reasons")
    return {
        "schema_version": STATUS_VERSION, "check": CHECK_NAME,
        "batch_id": sealed["batch_id"], "generation": sealed["generation"],
        "invocation_id": sealed["invocation_id"], "tuple_digest": sealed["tuple_digest"],
        "target_sha": sealed["target_sha"], "sealed_input_digest": sealed["sealed_input_digest"],
        "result": result, "reasons": reasons, "evaluated_at": _now(),
    }


def run(sealed_input, invocation_directory, evaluator=None, status_path=None):
    """Run one finalizer body and atomically write its tuple-bound status.

    ``evaluator`` receives only the sealed structure.  It cannot receive a
    producer command plan, broker credentials, or a Checks token.  The
    aggregate is invoked unconditionally; absent reviewed authority is red.
    """
    try:
        sealed = validate_sealed_input(sealed_input)
    except Exception:
        # Even a malformed sealed handoff crosses the aggregate failure path.
        # There is no safe invocation identity for a local status here; the
        # deployment adapter persists the coordinator-generated red verdict.
        aggregate.evaluate_sealed(sealed_input, evaluator)
        raise
    directory = Path(invocation_directory)
    output = Path(status_path) if status_path is not None else directory / "status.json"
    lock_path, marker = directory / "invocation.lock", directory / "started.json"
    directory.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        _start_marker(marker, sealed)
        try:
            outcome = aggregate.evaluate_sealed(sealed, evaluator)
            _require(isinstance(outcome, dict) and set(outcome) == {"result", "reasons"},
                     "evaluator returned malformed outcome")
            status = status_for(sealed, outcome["result"], outcome["reasons"])
        except Exception as exc:
            status = status_for(sealed, "fail", ["finalizer evaluation failed: %s: %s" %
                                                    (type(exc).__name__, exc)])
        _atomic_json(output, status)
        return status


def read_complete_status(path, sealed_input):
    """Read a complete status only if it exactly binds the accepted invocation."""
    try:
        status = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise FinalizerError("finalizer status is missing or malformed") from exc
    sealed = validate_sealed_input(sealed_input)
    expected = status_for(sealed, status.get("result"), status.get("reasons"))
    # evaluated_at is intentionally not predetermined; every other field must match.
    _require(isinstance(status.get("evaluated_at"), str) and
             all(status.get(key) == value for key, value in expected.items() if key != "evaluated_at"),
             "finalizer status does not bind the sealed invocation")
    return status


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sealed-input", required=True)
    parser.add_argument("--invocation-dir", required=True)
    parser.add_argument("--status")
    args = parser.parse_args(argv)
    try:
        sealed = json.loads(Path(args.sealed_input).read_text(encoding="utf-8"))
        status = run(sealed, args.invocation_dir, status_path=args.status)
    except Exception as exc:
        print("ERROR: finalizer failed: %s: %s" % (type(exc).__name__, exc))
        return 1
    print(canonical_json(status))
    return 0 if status["result"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
