"""Base-only calibration of ambient work, with independent scoped guards.

The Release 14 Lead's decision 6965824641 requires five same-byte controls.
Candidate measurements never set a limit. A margin is one observed base range,
only for named ambient counters; stable counters receive no margin. Saved
references and the strict reference-replacement gate are not changed here.
"""

from __future__ import annotations

from collections import Counter

from tests._local_speed_baseline import CALLERS, compare_baseline, counter_values

SCHEMA = "astrid.local-speed-ambient-calibration.v1"
PARENT = "parent_runtime_host_sdk"
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


def calibration(reference):
    baseline = reference["baseline"]
    if not baseline.get("pair_id") or baseline.get("measurement", {}).get("gate_version") != 3:
        raise ValueError("calibration needs a current paired product-default measurement")
    result = {"schema": SCHEMA, "pair_id": baseline["pair_id"], "sources": baseline["sources"],
              "system": baseline["system"], "measurement": baseline["measurement"],
              "margin_rule": "one observed base range (maximum minus minimum); zero for stable or nonambient counters",
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
        result["callers"][caller] = {"samples": len(rows), "metrics": metrics}
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
