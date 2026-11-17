"""Standing-meter baseline and explicit tolerances, not the D1 speed budget.

Latency is uninstrumented. Counter caps apply to instrumented jobs only.
OS flush completions are a separate observation; an absent trace is never zero.
"""

from __future__ import annotations

import math
import os
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


def counter_values(row, *, known_counter_names=()):
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
    if row.get("gate_version") == 3:
        required_waits = {"event_wait", "condition_wait", "asyncio_sleep", "selector_wait"}
        if set(row.get("wait_coverage", [])) != required_waits:
            raise ValueError("wait counter coverage is incomplete")
        for name in sorted(required_waits):
            values["python." + name + "_attempt"] = counts.get(name + "_attempt", 0)
        for name in ("runtime_outer_commit_attempt", "runtime_outer_commit_success", "platform_write_through_success"):
            values["python." + name] = counts.get(name, 0)
        if counts.get("runtime_outer_commit_error", 0) or counts.get("platform_write_through_error", 0):
            raise ValueError("a commit or platform forced write failed")
        values.update({"runtime_http." + path: count for path, count in row["runtime_http_paths"].items()})
        for origin in ("runtime_publication", "vibecomfy_evidence", "caller_delivery", "runtime_background",
                       "runtime_other", "other_application"):
            values["application_write." + origin] = 0
        for span in row["write_spans"]:
            if span["kind"] == "application_sync":
                if not span["success"]:
                    raise ValueError("an attributed application write failed")
                key = "application_write." + span["origin"]
                values[key] = values.get(key, 0) + 1
        modes = row["sqlite"]
        if modes["before"] != modes["after"]:
            raise ValueError("effective SQLite mode changed during the job")
        if row.get("native_flushes") is not None:
            attribution = row.get("native_attribution", {})
            if attribution.get("status") != "PASS":
                raise ValueError("native forced-write attribution is incomplete")
            values.update({"native." + name: value for name, value in attribution["counts"].items()})
    # These maps are complete enumerations from a live observer, so an operation
    # that did not occur is an observed zero. Preserve the reference's vocabulary
    # explicitly instead of deleting its counter. Missing coverage never enters
    # this path, and compare_baseline itself never invents zero for a lost key.
    for name in known_counter_names:
        if name in values:
            continue
        if name.startswith("comfy_http.") and "attached_http_paths" in row:
            values[name] = 0
        elif name.startswith("runtime_http.") and row.get("gate_version") == 3 and "runtime_http_paths" in row:
            values[name] = 0
    if any(type(v) is not int or v < 0 for v in values.values()):
        raise ValueError("counter values must be observed nonnegative integers")
    return dict(sorted(values.items()))


def make_baseline(latency, counters, *, system, sources, counter_vocabulary=None):
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
    if any(row.get("gate_version") == 3 for row in latency + counters):
        if any(row.get("gate_version") != 3 for row in latency + counters):
            raise ValueError("mixed meter versions cannot produce a reference")
        settings = {tuple(sorted(row["harness_settings"].items())) for row in latency + counters}
        if len(settings) != 1:
            raise ValueError("harness settings differ between samples")
        baseline["measurement"] = {"gate_version": 3, "harness_settings": dict(settings.pop())}
        if os.environ.get("ASTRID_LS2_PAIR_ID"):
            baseline["pair_id"] = os.environ["ASTRID_LS2_PAIR_ID"]
    for caller in CALLERS:
        timings = [row for row in latency if row["caller"] == caller]
        counter_rows = [row for row in counters if row["caller"] == caller]
        names_known = (counter_vocabulary or {}).get("callers", {}).get(caller, {}).get("counter_maxima", {})
        observed = [counter_values(row, known_counter_names=names_known) for row in counter_rows]
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
        if "measurement" in baseline:
            modes = [row["sqlite"]["before"] for row in counter_rows]
            if any(mode != modes[0] for mode in modes):
                raise ValueError("effective SQLite modes differ between counter samples")
            baseline["callers"][caller]["sqlite_mode"] = modes[0]
    return baseline


def compare_baseline(actual, reference, *, coverage_reference=None):
    """Return every regression. Extra starts/compiles/emissions/hashes fail.

    Every observed counter is an upper bound. Background work is reported
    separately by the observer; it is not a reason to forgive extra work.
    """
    if reference.get("schema") != SCHEMA or actual.get("schema") != SCHEMA or actual["system"] != reference.get("system"):
        raise ValueError("baseline schema/system mismatch")
    if reference.get("tolerance") not in (TOLERANCE, LEGACY_TOLERANCE):
        raise ValueError("baseline tolerance differs from the checked policy")
    if actual["fixture"] != reference.get("fixture"):
        raise ValueError("baseline fixture mismatch")
    if "measurement" in reference and actual.get("measurement") != reference["measurement"]:
        raise ValueError("baseline measurement coverage/settings mismatch")
    if set(reference.get("callers", {})) != set(CALLERS) or set(actual.get("callers", {})) != set(CALLERS):
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
        if "sqlite_mode" in prior and current.get("sqlite_mode") != prior["sqlite_mode"]:
            raise ValueError("baseline effective SQLite mode mismatch")
        for statistic in ("median", "slowest"):
            expected = prior["overhead_seconds"][statistic]
            value = current["overhead_seconds"][statistic]
            if any(type(item) not in (float, int) or not math.isfinite(item) or item < 0 for item in (expected, value)):
                raise ValueError("timing statistics must be finite nonnegative observations")
            limit = (
                expected * (1 + TOLERANCE[statistic + "_relative"])
                + TOLERANCE[statistic + "_seconds"]
            )
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
            if name not in old and coverage_reference is not None:
                expected = coverage_reference["callers"][caller]["counter_maxima"].get(name, 0)
            if type(value) is not int or value < 0:
                raise ValueError("candidate counter is not an observed nonnegative integer")
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


def approve_reference(actual, old_reference, *, coverage_reference=None):
    """The caller must supply the prior reference, never the proposed replacement."""
    if actual.get("measurement", {}).get("gate_version") == 3 and actual["system"] == "Windows":
        if (coverage_reference is None or not actual.get("pair_id")
                or actual["pair_id"] != coverage_reference.get("pair_id")):
            raise ValueError("Windows reference must be measured in the same runner pair")
    failures = compare_baseline(actual, old_reference, coverage_reference=coverage_reference)
    if failures:
        raise ValueError("regressed candidate cannot replace the prior reference")
    if coverage_reference is not None and compare_baseline(actual, coverage_reference):
        raise ValueError("candidate failed its contemporaneous reference")
    return actual
