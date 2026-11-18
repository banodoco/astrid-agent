"""Base-only calibration of ambient work, with independent scoped guards.

The Release 14 Lead's decisions 6965824641 and 6968415737 require five
same-byte controls. Candidate measurements never set a limit. Named ambient
counters use one observed base range. The named host poll loop additionally
uses its base-only window/period budget, with exact cadence guards. Saved
references and the strict reference-replacement gate are not changed here.
"""

from __future__ import annotations

from collections import Counter

from tests._local_speed_baseline import CALLERS, compare_baseline, counter_values
from tests._local_speed_census import CONTAINERS, census_remainder, census_rule, compare_census

SCHEMA = "astrid.local-speed-ambient-calibration.v2"
PARENT = "parent_runtime_host_sdk"
HOST_PRODUCER = "astrid.core.execution.generic_host:GenericPackHost._run_command_definition"
HOST_SLEEP_SITE = "sleep_attempt|" + HOST_PRODUCER
HOST_SLEEP_METRIC = f"waitsite.{PARENT}.{HOST_SLEEP_SITE}"
HOST_CADENCE_METRIC = f"cadence.{PARENT}.{HOST_PRODUCER}"
OPERATIONS = ("sleep_attempt", "event_wait_attempt", "condition_wait_attempt",
              "asyncio_sleep_attempt", "selector_wait_attempt", "fsync_attempt",
              "fsync_success", "fdatasync_attempt", "fdatasync_success",
              "runtime_outer_commit_attempt", "runtime_outer_commit_success",
              "runtime_commit_call_attempt", "runtime_commit_without_transaction",
              "runtime_commit_statement", "runtime_commit_statement_unattributed",
              "platform_write_through_success", "workflow_compile", "bundle_compile",
              "bundle_emission")
AMBIENT_ROUTES = {"GET /v1/health", "GET /v1/tasks/<task>", "POST /v1/tasks/claim",
                  "POST /v1/attempts/<attempt>/heartbeat"}


def scoped_values(row, *, known_counter_names=()):
    """Keep raw totals, process roles, wait producers and request cardinality."""
    values = counter_values(row, known_counter_names=known_counter_names)
    scopes = row.get("runtime_operation_scopes")
    if scopes is None or not row.get("process_observations"):
        raise ValueError("ambient calibration needs scoped observer coverage")
    roles = {PARENT, "pack_worker", "cli"}
    observed_roles = {process["role"] for process in row["process_observations"]}
    if not observed_roles <= roles or PARENT not in observed_roles or "pack_worker" not in observed_roles:
        raise ValueError("unexpected or missing process role coverage")
    request_counts = Counter(scope["route"] for scope in scopes
                             if scope["kind"] == "runtime_request_span" and not scope.get("carry_in"))
    if request_counts != Counter(row["runtime_http_paths"]):
        raise ValueError("Runtime request scope coverage is incomplete")
    if row["python_counts"].get("runtime_commit_statement_unattributed", 0):
        raise ValueError("an SQL COMMIT statement lacks Runtime scope")
    if row["python_counts"].get("runtime_commit_statement", 0) != row["runtime_main_connection_commit_statements"]:
        raise ValueError("SQL COMMIT statement coverage is incomplete")
    for operation in ("runtime_outer_commit_attempt", "runtime_outer_commit_success", "runtime_commit_call_attempt",
                      "runtime_commit_without_transaction", "runtime_commit_statement"):
        if sum(scope["counts"].get(operation, 0) for scope in scopes) != row["python_counts"].get(operation, 0):
            raise ValueError("Runtime commit scope coverage is incomplete: " + operation)
    poll_metrics = set()
    for role in sorted(roles):
        processes = [process for process in row["process_observations"] if process["role"] == role]
        for operation in OPERATIONS:
            values[f"process.{role}.{operation}"] = sum(process["counts"].get(operation, 0) for process in processes)
        values[f"process.{role}.output_hash_streams"] = sum(process["output_hash_streams"] for process in processes)
        for process in processes:
            for field in ("wait_sites", "wait_invocation_maxima", "poll_sites", "poll_wait_maxima"):
                if field not in process:
                    raise ValueError("wait producer/cycle coverage is incomplete")
            for kind in ("sleep", "event_wait", "condition_wait", "asyncio_sleep", "selector_wait"):
                operation = kind + "_attempt"
                if sum(count for site, count in process["wait_sites"].items() if site.startswith(operation + "|")) != process["counts"].get(operation, 0):
                    raise ValueError("wait producer counts do not cover the observed waits")
            if set(process["wait_invocation_maxima"]) != set(process["wait_sites"]):
                raise ValueError("wait invocation coverage is incomplete")
            for site, count in process["wait_sites"].items():
                key = f"waitsite.{role}.{site}"
                values[key] = values.get(key, 0) + count
                producer = site.split("|", 1)[1]
                if producer in process["poll_sites"]:
                    if site not in process["poll_wait_maxima"]:
                        raise ValueError("observed polling wait lacks cycle evidence")
                    poll_metrics.add(key)
                    bound_key = f"per_poll.{role}.{site}"
                    values[bound_key] = max(values.get(bound_key, 0), process["poll_wait_maxima"][site])
                else:
                    bound_key = f"per_invocation.{role}.{site}"
                    values[bound_key] = max(values.get(bound_key, 0), process["wait_invocation_maxima"][site])
    for scope in scopes:
        route = scope["route"] or scope["write_context"]
        prefix = "runtime_scope_count." + route
        values[prefix] = values.get(prefix, 0) + (0 if scope.get("carry_in") else 1)
        for operation in OPERATIONS:
            key = f"per_request.{route}.{operation}"
            values[key] = max(values.get(key, 0), scope["counts"].get(operation, 0))
        if row.get("native_flushes") is not None:
            native = [event for event in row["native_attribution"]["events"]
                      if event["pid"] == scope["pid"] and event["tid"] == scope["tid"]
                      and scope["epoch_start_ns"] - 1000 <= event["start_ns"]
                      and scope["epoch_end_ns"] is not None
                      and event["end_ns"] <= scope["epoch_end_ns"] + 1000]
            key = f"per_request.{route}.native_flushes"
            values[key] = max(values.get(key, 0), len(native))
    # These are complete enumerations, as distinct from an absent observer.
    # An eliminated wait/route is an observed zero; a missing map failed above.
    enumerated = ("waitsite.", "per_poll.", "per_invocation.", "per_request.", "runtime_scope_count.")
    for name in known_counter_names:
        if name.startswith(enumerated) and name not in values:
            values[name] = 0
    if any(type(value) is not int or value < 0 for value in values.values()):
        raise ValueError("scoped counters must be observed nonnegative integers")
    return dict(sorted(values.items())), poll_metrics


def ambient_group(name, polling_metrics):
    """The allowlist names scheduling volume, never per-request operation cost."""
    waits = {kind + "_attempt" for kind in ("sleep", "event_wait", "condition_wait", "asyncio_sleep", "selector_wait")}
    if name in {"python." + kind for kind in waits}:
        return "whole_workload_wait_volume"
    if name in {f"process.{PARENT}." + kind for kind in waits}:
        return "parent_wait_volume"
    if name == "process.cli.sleep_attempt" and any(site.startswith("waitsite.cli.sleep_attempt|") for site in polling_metrics):
        return "cli_sdk_poll_volume"
    if name.startswith("waitsite.cli.sleep_attempt|") and name in polling_metrics:
        return "cli_sdk_poll_volume"
    if name.startswith(f"waitsite.{PARENT}."):
        # A parent sleep is not automatically ambient. Only an observed poll
        # cycle permits a margin, and its per-cycle maximum stays independent.
        if ".sleep_attempt|" in name and name not in polling_metrics:
            return None
        return "parent_poll_wait_volume" if name in polling_metrics else "parent_wait_producer_volume"
    if name == f"per_invocation.{PARENT}.event_wait_attempt|astrid.core.execution.generic_host:GenericPackHost.run_task.<locals>.pump_lease":
        return "lease_pump_wait_volume"
    if name == f"per_invocation.{PARENT}.selector_wait_attempt|subprocess:Popen._communicate":
        return "parent_pipe_readiness_volume"
    if name in {"runtime_commit_statements", "python.runtime_outer_commit_attempt", "python.runtime_outer_commit_success",
                f"process.{PARENT}.runtime_outer_commit_attempt", f"process.{PARENT}.runtime_outer_commit_success",
                f"process.{PARENT}.runtime_commit_statement", f"process.{PARENT}.runtime_commit_call_attempt"}:
        return "whole_workload_commit_volume"
    if name.startswith("runtime_committed."):
        return "runtime_context_commit_volume"
    if name in {"runtime_http." + route for route in AMBIENT_ROUTES}:
        return "runtime_poll_request_volume"
    if name in {"runtime_scope_count." + route for route in (*AMBIENT_ROUTES, "background_lifetime_reaper")}:
        return "runtime_poll_or_reaper_invocation_volume"
    if name in {"native.completed_flushes", "native.sqlite_wal_during_commit", "native.sqlite_wal_outside_commit",
                "native.sqlite_checkpoint_database", "native_context.http_heartbeat",
                "native_context.http_request", "native_context.background_lifetime_reaper"}:
        return "whole_workload_runtime_native_volume"
    return None


def _requested_period(summary, count):
    if count == 0 and summary is None:
        return None
    if (not isinstance(summary, dict)
            or any(type(summary.get(key)) is not int for key in
                   ("calls", "observed_calls", "unknown_calls", "minimum_ns", "maximum_ns"))
            or summary["calls"] != count or summary["observed_calls"] != count
            or summary["unknown_calls"] != 0
            or not 0 < summary["minimum_ns"] <= summary["maximum_ns"]):
        raise ValueError("host polling requested-period coverage is incomplete")
    return summary["minimum_ns"], summary["maximum_ns"]


def _host_poll_sample(row, *, required=False):
    """Reconcile raw host sleeps with complete, actually observed poll windows."""
    periods, windows, seen = [], [], set()
    maximum_per_poll = 0
    for process in row["process_observations"]:
        if process["role"] != PARENT:
            continue
        count = process["wait_sites"].get(HOST_SLEEP_SITE, 0)
        if not required and not count and "poll_windows" not in process:
            continue
        if not isinstance(process.get("poll_windows"), list) or not isinstance(process.get("sleep_requests"), dict):
            raise ValueError("host polling window/period coverage is incomplete")
        requested = _requested_period(process["sleep_requests"].get(HOST_SLEEP_SITE), count)
        if requested is not None:
            periods.append(requested)
        selected = [item for item in process["poll_windows"] if item["wait_site"] == HOST_PRODUCER]
        covered = 0
        for window in selected:
            integer_fields = ("pid", "tid", "wait_invocation", "poll_start_ns", "poll_end_ns",
                              "interval_start_ns", "interval_end_ns", "duration_ns", "poll_calls",
                              "missing_results", "nonterminal_polls", "terminal_polls", "sleep_calls")
            if (any(type(window.get(key)) is not int or window[key] < 0 for key in integer_fields)
                    or window["pid"] == 0 or window["tid"] == 0
                    or window.get("carry_in") is not False or window.get("carry_out") is not False
                    or window.get("ambiguous_children") is not False or window["missing_results"]
                    or window["terminal_polls"] != 1
                    or window["poll_calls"] != window["nonterminal_polls"] + 1
                    or window["poll_start_ns"] != window["interval_start_ns"]
                    or window["poll_end_ns"] != window["interval_end_ns"]
                    or window["duration_ns"] != window["poll_end_ns"] - window["poll_start_ns"]
                    or window["duration_ns"] <= 0):
                raise ValueError("host polling completed-window coverage is incomplete")
            interval = process["interval"]
            if (interval["monotonic_start_ns"] is not None and window["poll_start_ns"] < interval["monotonic_start_ns"]
                    or interval["monotonic_reply_ns"] is not None and window["poll_end_ns"] > interval["monotonic_reply_ns"]):
                raise ValueError("host polling interval coverage is incomplete")
            identity = (window["pid"], window["tid"], window["wait_invocation"])
            if identity in seen:
                raise ValueError("host polling window coverage contains duplicates")
            seen.add(identity)
            requests = window.get("sleep_requests")
            if not isinstance(requests, dict) or set(requests) != ({HOST_SLEEP_SITE} if window["sleep_calls"] else set()):
                raise ValueError("host polling window period coverage is incomplete")
            period = _requested_period(requests.get(HOST_SLEEP_SITE), window["sleep_calls"])
            if period is not None:
                if requested is None or not requested[0] <= period[0] <= period[1] <= requested[1]:
                    raise ValueError("host polling window period coverage differs from raw sleeps")
            covered += window["sleep_calls"]
        if covered != count:
            raise ValueError("host polling windows do not cover the raw producer sleeps")
        maximum_per_poll = max(maximum_per_poll, process["poll_wait_maxima"].get(HOST_SLEEP_SITE, 0))
        windows.extend(selected)
    return {"duration_ns": sum(window["duration_ns"] for window in windows),
            "nonterminal_polls": sum(window["nonterminal_polls"] for window in windows),
            "sleep_calls": sum(window["sleep_calls"] for window in windows),
            "minimum_requested_sleep_ns": min((period[0] for period in periods), default=None),
            "maximum_requested_sleep_ns": max((period[1] for period in periods), default=None),
            "maximum_sleep_per_poll": maximum_per_poll,
            "unbalanced_windows": sum(window["sleep_calls"] != window["nonterminal_polls"] for window in windows)}


def _calibrate_host_poll(rows, metrics):
    samples = [_host_poll_sample(row) for row in rows]
    if not any(sample["sleep_calls"] for sample in samples):
        return None
    # A zero-work control still needs a complete observer enumeration.
    samples = [_host_poll_sample(row, required=True) for row in rows]
    periods = {sample["minimum_requested_sleep_ns"] for sample in samples if sample["sleep_calls"]}
    if (len(periods) != 1 or any(sample["unbalanced_windows"] or
            sample["maximum_sleep_per_poll"] != (1 if sample["sleep_calls"] else 0) or
            (sample["sleep_calls"] and sample["maximum_requested_sleep_ns"] not in periods) for sample in samples)):
        raise ValueError("base host polling controls do not establish one sleep per poll at one constant period")
    period = periods.pop()
    durations = [sample["duration_ns"] for sample in samples]
    nominal = max(durations) // period
    original_limit = metrics[HOST_SLEEP_METRIC]["limit"]
    increment = max(0, nominal - original_limit)
    # Only the named producer earns this allowance. Propagate its incremental
    # budget once to the two totals that contain it; every other cap is intact.
    for name in (HOST_SLEEP_METRIC, f"process.{PARENT}.sleep_attempt", "python.sleep_attempt"):
        item = metrics[name]
        item["range_limit"] = item["limit"]
        item["cadence_increment"] = increment
        item["limit"] += increment
        item["margin"] = item["limit"] - item["maximum"]
        item["policy"] = ("base_control_host_poll_window_cadence" if name == HOST_SLEEP_METRIC
                          else "host_poll_cadence_increment_in_containing_total")
    return {"model": "base_control_host_poll_window_cadence.v1", "decision": "6968415737",
            "producer": HOST_PRODUCER, "requested_period_ns": period,
            "base_window_duration_ns": durations,
            "base_nonterminal_polls": [sample["nonterminal_polls"] for sample in samples],
            "base_sleep_calls": [sample["sleep_calls"] for sample in samples],
            "nominal_poll_limit": nominal, "range_limit": original_limit,
            "limit": metrics[HOST_SLEEP_METRIC]["limit"], "propagated_increment": increment}


def _compare_host_poll(rows, cadence, caller):
    failures = []
    for row in rows:
        sample = _host_poll_sample(row, required=True)
        if sample["unbalanced_windows"]:
            failures.append({"caller": caller, "sample_index": row["sample_index"],
                             "metric": HOST_CADENCE_METRIC + ".unbalanced_windows",
                             "actual": sample["unbalanced_windows"], "limit": 0,
                             "reason": "each nonterminal host poll must have exactly one sleep"})
        minimum = sample["minimum_requested_sleep_ns"]
        if minimum is not None and minimum < cadence["requested_period_ns"]:
            failures.append({"caller": caller, "sample_index": row["sample_index"],
                             "metric": HOST_CADENCE_METRIC + ".minimum_requested_sleep_ns",
                             "actual": minimum, "limit": cadence["requested_period_ns"],
                             "comparison": "at least", "reason": "host polling requested sleep period was shortened"})
    return failures


def calibration(reference):
    baseline = reference["baseline"]
    if not baseline.get("pair_id") or baseline.get("measurement", {}).get("gate_version") != 3:
        raise ValueError("calibration needs a current paired product-default measurement")
    result = {"schema": SCHEMA, "pair_id": baseline["pair_id"], "sources": baseline["sources"],
              "system": baseline["system"], "measurement": baseline["measurement"],
              "margin_rule": "one observed base range for named ambient counters; named host polling additionally uses the base-only window/period budget with exact cadence guards",
              "minimum_control_samples": 5, "callers": {}, "reference_eligible": False}
    for caller in CALLERS:
        rows = [row for row in reference["counters"] if row["caller"] == caller]
        if len(rows) < 5 or len({row["sample_index"] for row in rows}) != len(rows):
            raise ValueError("at least five distinct same-byte base samples per caller are required")
        initial = [scoped_values(row) for row in rows]
        vocabulary = set().union(*(set(values) for values, _ in initial))
        observed = [scoped_values(row, known_counter_names=vocabulary)[0] for row in rows]
        if any(set(values) != vocabulary for values in observed):
            raise ValueError("base counter coverage differs across samples")
        census, census_samples = census_rule(rows)
        raw_observed = observed
        observed = [census_remainder(values, sample) for values, sample in zip(observed, census_samples)]
        # Four observed already-exited children establish zero census waits,
        # even when the containing Popen sleep site never occurred at all.
        vocabulary.update(name for values in observed for name in values)
        polling = set().union(*(sites for _, sites in initial))
        metrics = {}
        for name in sorted(vocabulary):
            samples = [values[name] for values in observed]
            minimum, maximum = min(samples), max(samples)
            group = ambient_group(name, polling)
            margin = maximum - minimum if group is not None else 0
            metrics[name] = {"samples": samples, "minimum": minimum, "maximum": maximum,
                             "spread": maximum - minimum, "margin": margin,
                             "limit": maximum + margin, "group": group or "scoped_or_deterministic",
                             "policy": "observed_maximum_plus_range" if margin else "observed_maximum_exact_cap"}
        cadence = _calibrate_host_poll(rows, metrics)
        if census is not None:
            for name in CONTAINERS:
                item = metrics[name]
                item["raw_samples"] = [values.get(name, 0) for values in raw_observed]
                item["census_sleep_samples"] = [sample["sleep_calls"] for sample in census_samples]
                item["remainder_policy"] = item["policy"]
                item["policy"] = "remainder_after_individually_gated_census_sleeps"
        result["callers"][caller] = {"samples": len(rows), "metrics": metrics,
                                     "host_poll_cadence": cadence, "census_invariants": census}
    return result


def compare_calibrated(actual, reference, policy):
    """Validate the policy from base-only bytes before looking at candidates."""
    if policy != calibration(reference):
        raise ValueError("counter limits must be derived only from the recorded base controls")
    prior, current = reference["baseline"], actual["baseline"]
    if current.get("pair_id") != prior["pair_id"]:
        raise ValueError("calibrated comparison requires the same runner pair")
    # Keep the existing source, mode, coverage, sample and timing checks. Only
    # counter policy changes, and every old counter is still compared below.
    failures = [failure for failure in compare_baseline(current, prior)
                if failure["metric"].startswith("overhead_seconds.")]
    for caller in CALLERS:
        metrics = policy["callers"][caller]["metrics"]
        rows = [row for row in actual["counters"] if row["caller"] == caller]
        if len(rows) < policy["callers"][caller]["samples"]:
            raise ValueError("candidate/control validation has fewer counter samples")
        initial = [scoped_values(row, known_counter_names=metrics)[0] for row in rows]
        names = set(metrics).union(*(set(values) for values in initial))
        observed = [scoped_values(row, known_counter_names=names)[0] for row in rows]
        census_failures, census_samples = compare_census(rows, policy["callers"][caller]["census_invariants"], caller)
        failures.extend(census_failures)
        observed = [census_remainder(values, sample) for values, sample in zip(observed, census_samples)]
        cadence = policy["callers"][caller]["host_poll_cadence"]
        if cadence is not None:
            failures.extend(_compare_host_poll(rows, cadence, caller))
        for name in sorted(names):
            limits = metrics.get(name, {"maximum": 0, "limit": 0, "margin": 0, "spread": 0})
            samples = [values.get(name) for values in observed]
            if any(value is None for value in samples):
                failures.append({"caller": caller, "metric": name, "actual": None,
                                 "limit": limits["limit"], "reason": "previously observed counter disappeared"})
            elif max(samples) > limits["limit"]:
                failures.append({"caller": caller, "metric": name, "actual": max(samples),
                                 "samples": samples, "limit": limits["limit"], "reference": limits["maximum"],
                                 "margin": limits["margin"], "base_spread": limits["spread"]})
    return failures
