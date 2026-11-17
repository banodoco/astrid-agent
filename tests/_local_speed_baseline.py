"""Standing-meter baseline and explicit tolerances, not the D1 speed budget.

Latency is uninstrumented. Counter caps apply to instrumented jobs only.
OS flush completions are a separate observation; an absent trace is never zero.
"""

from __future__ import annotations

import math
import re
import statistics
from collections import Counter

SCHEMA = "astrid.local-speed-baseline.v2"
# LS-2.2: fix the policy before measuring. A faster candidate is welcome; a
# slower one does not receive an absolute allowance as the product improves.
TOLERANCE = {
    "median_relative": 0.15,
    "median_seconds": 0.0,
    "slowest_relative": 0.15,
    "slowest_seconds": 0.0,
    "background_relative": 0.0,
    "background_count": 0,
}
# The original references keep their measured bytes and acquisition policy.
# Recognizing that provenance never selects it as the comparison policy.
LEGACY_TOLERANCE = {
    "median_relative": 0.50, "median_seconds": 3.0,
    "slowest_relative": 1.00, "slowest_seconds": 5.0,
    "background_relative": 0.50, "background_count": 10,
}
CALLERS = ("resident_sdk", "fresh_python_cli")


def distribution(values):
    if not values or any(
        not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0 for v in values
    ):
        raise ValueError("a complete finite nonnegative sample distribution is required")
    return {"samples": len(values), "median": statistics.median(values), "slowest": max(values)}


def counter_values(row):
    counts = row["python_counts"]
    if any(counts.get(name, 0) for name in ("fsync_error", "fdatasync_error")):
        raise ValueError("a Python forced-write request failed")
    values = {"process_start." + key: value for key, value in row["process_start_counts"].items()}
    for name in (
        "workflow_compile",
        "bundle_compile",
        "bundle_emission",
        "sleep_attempt",
        "fsync_attempt",
        "fsync_success",
        "fdatasync_attempt",
        "fdatasync_success",
    ):
        values["python." + name] = counts.get(name, 0)
    paths = Counter()
    for path, count in row["attached_http_paths"].items():
        # Prompt IDs identify attempts, not different request operations.
        paths[re.sub(r"(/history/)[^/?]+", r"\1<prompt>", path)] += count
    values.update({"comfy_http." + key: value for key, value in paths.items()})
    values["output_hash_streams"] = row["output_hash_streams"]
    values["python_forced_write_requests"] = counts.get("fsync_success", 0) + counts.get(
        "fdatasync_success", 0
    )
    values["runtime_commit_statements"] = row["runtime_main_connection_commit_statements"]
    if row.get("native_flushes") is not None:
        values["native.completed_flushes"] = row["native_flushes"]
    if any(type(v) is not int or v < 0 for v in values.values()):
        raise ValueError("counter values must be observed nonnegative integers")
    return dict(sorted(values.items()))


def make_baseline(latency, counters, *, system, sources):
    baseline = {
        "schema": SCHEMA,
        "system": system,
        "sources": sources,
        "tolerance": dict(TOLERANCE),
        "callers": {},
        "fixture": "attached stand-in, 16x16 PNG; no inference, not a D1 workload",
        "native_windows_flushes": "unqualified; LS-13 owns the native trace",
        "budget_certified": False,
    }
    for caller in CALLERS:
        timings = [row for row in latency if row["caller"] == caller]
        counter_rows = [row for row in counters if row["caller"] == caller]
        observed = [counter_values(row) for row in counter_rows]
        if not observed:
            raise ValueError("each caller needs an instrumented counter sample")
        native_coverage = {row.get("native_flushes") is not None for row in counter_rows}
        if len(native_coverage) != 1:
            raise ValueError("native coverage differs between counter samples")
        names = set().union(*(row.keys() for row in observed))
        baseline["callers"][caller] = {
            "caller_seconds": distribution([row["caller_seconds"] for row in timings]),
            "overhead_seconds": distribution([row["overhead_seconds"] for row in timings]),
            "queue_seconds": distribution([row["standin_queue_seconds"][0] for row in timings]),
            "execution_seconds": distribution(
                [row["standin_execution_seconds"][0] for row in timings]
            ),
            "counter_samples": len(observed),
            "native_flush_coverage": "observed" if True in native_coverage else "unobserved",
            "counter_maxima": {
                key: max(row.get(key, 0) for row in observed) for key in sorted(names)
            },
        }
    return baseline


def compare_baseline(actual, reference):
    """Return every regression. Extra starts/compiles/emissions/hashes fail.

    Every observed counter is an upper bound. Background work is reported
    separately by the observer; it is not a reason to forgive extra work.
    """
    if reference.get("schema") != SCHEMA or actual["system"] != reference.get("system"):
        raise ValueError("baseline schema/system mismatch")
    if reference.get("tolerance") not in (TOLERANCE, LEGACY_TOLERANCE):
        raise ValueError("baseline tolerance differs from the checked policy")
    if actual["fixture"] != reference.get("fixture"):
        raise ValueError("baseline fixture mismatch")
    if set(reference.get("callers", {})) != set(CALLERS):
        raise ValueError("baseline must name both callers")
    for dependency in ("runtime", "vibecomfy", "lifecycle"):
        if actual["sources"].get(dependency) != reference.get("sources", {}).get(dependency):
            raise ValueError("baseline dependency mismatch: " + dependency)
    failures = []
    for caller in CALLERS:
        current, prior = actual["callers"][caller], reference["callers"][caller]
        if current["overhead_seconds"]["samples"] < prior["overhead_seconds"]["samples"]:
            raise ValueError("fewer latency samples than the recorded baseline")
        if current["counter_samples"] < prior["counter_samples"]:
            raise ValueError("fewer counter samples than the recorded baseline")
        if current["native_flush_coverage"] != prior["native_flush_coverage"]:
            raise ValueError("baseline native coverage mismatch")
        for statistic in ("median", "slowest"):
            expected = prior["overhead_seconds"][statistic]
            limit = (
                expected * (1 + TOLERANCE[statistic + "_relative"])
                + TOLERANCE[statistic + "_seconds"]
            )
            value = current["overhead_seconds"][statistic]
            if value > limit:
                failures.append(
                    {
                        "caller": caller,
                        "metric": "overhead_seconds." + statistic,
                        "actual": value,
                        "limit": limit,
                        "reference": expected,
                    }
                )
        old, new = prior["counter_maxima"], current["counter_maxima"]
        for name in sorted(set(old) | set(new)):
            if name not in new:
                failures.append({
                    "caller": caller, "metric": name, "actual": None,
                    "limit": old[name], "reference": old[name],
                    "reason": "previously observed counter disappeared",
                })
                continue
            expected, value = old.get(name, 0), new.get(name, 0)
            limit = expected
            if value > limit:
                failures.append(
                    {
                        "caller": caller,
                        "metric": name,
                        "actual": value,
                        "limit": limit,
                        "reference": expected,
                    }
                )
    return failures
