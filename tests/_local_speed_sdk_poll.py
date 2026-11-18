"""Lead 6981129059: SDK sleep totals are observations; poll invariants gate."""

from tests._local_speed_process import SDK_PRODUCER

PARENT = "parent_runtime_host_sdk"
SITE = "sleep_attempt|" + SDK_PRODUCER
METRIC = "waitsite." + PARENT + "." + SITE
CADENCE = "cadence." + PARENT + "." + SDK_PRODUCER
TERMINAL = {"succeeded", "completed", "failed", "cancelled", "cancel_requested"}


def _period(requests, count):
    if not isinstance(requests, dict) or set(requests) != ({SITE} if count else set()):
        raise ValueError("SDK polling requested-period coverage is incomplete")
    if not count:
        return None
    item = requests[SITE]
    if (not isinstance(item, dict)
            or any(type(item.get(key)) is not int for key in
                   ("calls", "observed_calls", "unknown_calls", "minimum_ns", "maximum_ns"))
            or item["calls"] != count or item["observed_calls"] != count or item["unknown_calls"]
            or not 0 < item["minimum_ns"] <= item["maximum_ns"]):
        raise ValueError("SDK polling requested-period coverage is incomplete")
    return item["minimum_ns"], item["maximum_ns"]


def sdk_sample(row):
    """Reconcile every poll with a complete invocation and the raw sleep map."""
    records, periods, unbalanced, identities, poll_ids = [], [], [], set(), set()
    measured_task = row.get("proof", {}).get("measured_task_id", row.get("measured_task_id"))
    for process in row["process_observations"]:
        if process["role"] != PARENT:
            continue
        counts = process["counts"]
        observed = process.get("sdk_wait_invocations")
        raw = process["wait_sites"].get(SITE, 0)
        if not (observed or raw or counts.get("sdk_wait_start") or SDK_PRODUCER in process["poll_sites"]):
            continue
        if (not isinstance(observed, list) or not observed or process.get("coverage_gaps") != []
                or len(observed) != counts.get("sdk_wait_start", 0)
                or len(observed) != counts.get("sdk_wait_end", 0)):
            raise ValueError("SDK polling invocation coverage is incomplete")
        sleeps, polls, maxima = 0, 0, 0
        process_periods = []
        for item in observed:
            integers = ("invocation_id", "tid", "start_ns", "end_ns", "sleep_calls")
            if (any(type(item.get(key)) is not int or item[key] < 0 for key in integers)
                    or item.get("producer") != SDK_PRODUCER or not item["tid"]
                    or type(item.get("task_id")) is not str or not item["task_id"]
                    or (measured_task is not None and item["task_id"] != measured_task)
                    or item["end_ns"] <= item["start_ns"] or item.get("result_known") is not True
                    or item.get("carry_in") is not False or item.get("carry_out") is not False
                    or not isinstance(item.get("polls"), list) or not item["polls"]):
                raise ValueError("SDK polling invocation/result coverage is incomplete")
            identity = (process["pid"], item["tid"], item["invocation_id"])
            if identity in identities:
                raise ValueError("SDK polling invocation identity is ambiguous")
            identities.add(identity)
            interval = process["interval"]
            if ((interval["monotonic_start_ns"] is not None and item["start_ns"] < interval["monotonic_start_ns"])
                    or (interval["monotonic_reply_ns"] is not None and item["end_ns"] > interval["monotonic_reply_ns"])):
                raise ValueError("SDK polling invocation is clipped by the measured interval")
            previous_end, invocation_sleeps = item["start_ns"], 0
            for index, poll in enumerate(item["polls"]):
                if (any(type(poll.get(key)) is not int or poll[key] < 0 for key in
                        ("poll_id", "start_ns", "end_ns", "sleep_calls", "missing_sleep_results"))
                        or not previous_end <= poll["start_ns"] < poll["end_ns"] <= item["end_ns"]
                        or poll.get("result_known") is not True or poll["missing_sleep_results"]
                        or type(poll.get("state")) is not str or not poll["state"]
                        or (poll["state"] in TERMINAL) != (index == len(item["polls"]) - 1)):
                    raise ValueError("SDK polling cycle/result coverage is incomplete or ambiguous")
                key = (process["pid"], poll["poll_id"])
                if key in poll_ids:
                    raise ValueError("SDK polling cycle identity is ambiguous")
                poll_ids.add(key)
                previous_end = poll["end_ns"]
                expected = 0 if poll["state"] in TERMINAL else 1
                if poll["sleep_calls"] != expected:
                    unbalanced.append({"invocation_id": item["invocation_id"], "poll_id": poll["poll_id"],
                                       "actual": poll["sleep_calls"], "limit": expected})
                period = _period(poll.get("sleep_requests"), poll["sleep_calls"])
                if period is not None:
                    process_periods.append(period)
                invocation_sleeps += poll["sleep_calls"]
                maxima = max(maxima, poll["sleep_calls"])
            if invocation_sleeps != item["sleep_calls"]:
                raise ValueError("SDK polling cycles do not cover the observed sleeps")
            sleeps += invocation_sleeps
            polls += len(item["polls"])
        if (sleeps != raw or maxima != process["poll_wait_maxima"].get(SITE, 0)
                or any(polls != counts.get(key, 0) for key in
                       ("sdk_poll_start", "sdk_poll_end", "sdk_show_result"))):
            raise ValueError("SDK polling cycles do not cover the raw observations")
        raw_period = _period({SITE: process.get("sleep_requests", {}).get(SITE)} if raw else {}, raw)
        combined = ((min(p[0] for p in process_periods), max(p[1] for p in process_periods))
                    if process_periods else None)
        if raw_period != combined:
            raise ValueError("SDK polling requested periods differ from raw observations")
        periods.extend(process_periods)
        records.extend(observed)
    if not records:
        raise ValueError("SDK polling invocation coverage is missing")
    return {"sleep_calls": sum(item["sleep_calls"] for item in records), "unbalanced_polls": unbalanced,
            "minimum_requested_sleep_ns": min((period[0] for period in periods), default=None),
            "maximum_requested_sleep_ns": max((period[1] for period in periods), default=None)}


def sdk_rule(rows):
    samples = [sdk_sample(row) for row in rows]
    periods = {sample["minimum_requested_sleep_ns"] for sample in samples if sample["sleep_calls"]}
    if (len(periods) > 1 or any(sample["unbalanced_polls"]
            or (sample["sleep_calls"] and sample["maximum_requested_sleep_ns"] not in periods) for sample in samples)):
        raise ValueError("base SDK controls must establish one sleep per nonterminal poll at one observed period")
    return {"producer": SDK_PRODUCER, "decision": "6981129059", "sleeps_per_nonterminal_poll": 1,
            "sleeps_per_terminal_poll": 0, "minimum_requested_sleep_ns": next(iter(periods), None),
            "base_sleep_calls": [sample["sleep_calls"] for sample in samples],
            "total_policy": "observation_only"}


def compare_sdk(rows, rule, caller):
    failures = []
    for row in rows:
        sample = sdk_sample(row)
        for poll in sample["unbalanced_polls"]:
            failures.append({"caller": caller, "sample_index": row["sample_index"],
                             "metric": CADENCE + ".sleeps_per_poll", "comparison": "exact", **poll})
        period = sample["minimum_requested_sleep_ns"]
        if period is not None:
            if rule["minimum_requested_sleep_ns"] is None:
                raise ValueError("base SDK polling requested period was unobserved")
            if period < rule["minimum_requested_sleep_ns"]:
                failures.append({"caller": caller, "sample_index": row["sample_index"],
                                 "metric": CADENCE + ".minimum_requested_sleep_ns", "actual": period,
                                 "limit": rule["minimum_requested_sleep_ns"], "comparison": "at least"})
    return failures
