"""Lead 6970432614: guard the ps census per invocation, retaining raw totals."""

from tests._local_speed_process import CENSUS_PRODUCER

PARENT = "parent_runtime_host_sdk"
SLEEP_SITE = "sleep_attempt|subprocess:Popen._wait"
METRIC = "census." + CENSUS_PRODUCER
CONTAINERS = ("python.sleep_attempt", "process." + PARENT + ".sleep_attempt",
              "waitsite." + PARENT + "." + SLEEP_SITE)


def _period(requests, count):
    if not isinstance(requests, dict) or set(requests) != ({SLEEP_SITE} if count else set()):
        raise ValueError("census sleep period coverage is incomplete")
    if not count:
        return None
    item = requests[SLEEP_SITE]
    if (not isinstance(item, dict)
            or any(type(item.get(key)) is not int for key in
                   ("calls", "observed_calls", "unknown_calls", "minimum_ns", "maximum_ns"))
            or item["calls"] != count or item["observed_calls"] != count or item["unknown_calls"]
            or not 0 < item["minimum_ns"] <= item["maximum_ns"]):
        raise ValueError("census sleep period coverage is incomplete")
    return item["minimum_ns"], item["maximum_ns"]


def census_sample(row, *, required=False):
    records, periods, identities = [], [], set()
    for process in row["process_observations"]:
        if process["role"] != PARENT:
            continue
        counts = process["counts"]
        if not required and not counts.get("census_start") and "census_invocations" not in process:
            continue
        observed = process.get("census_invocations")
        if (not isinstance(observed, list) or len(observed) != counts.get("census_start", 0)
                or len(observed) != counts.get("census_end", 0)):
            raise ValueError("census invocation coverage is incomplete")
        starts = process.get("process_starts", [])
        if not isinstance(starts, list):
            raise ValueError("census child coverage is incomplete")
        covered = 0
        for item in observed:
            integers = ("invocation_id", "tid", "child_pid", "spawned_children", "start_ns", "end_ns",
                        "wait_calls", "missing_wait_results", "nonterminal_waits", "terminal_waits", "sleep_calls")
            if (any(type(item.get(key)) is not int or item[key] < 0 for key in integers)
                    or item["producer"] != CENSUS_PRODUCER or not item["tid"] or not item["child_pid"]
                    or item["spawned_children"] != 1 or item["end_ns"] <= item["start_ns"]
                    or item.get("carry_in") is not False or item.get("carry_out") is not False
                    or item.get("command_known") is not True or item.get("ambiguous_children") is not False
                    or type(item.get("returncode")) is not int or item["returncode"] != 0
                    or item["missing_wait_results"] or item["terminal_waits"] > 1
                    or item["wait_calls"] != item["nonterminal_waits"] + item["terminal_waits"]):
                raise ValueError("census invocation/result coverage is incomplete")
            identity = (process["pid"], item["tid"], item["invocation_id"])
            if identity in identities:
                raise ValueError("census invocation identity is duplicated")
            identities.add(identity)
            children = [start for start in starts if start.get("census_invocation") == item["invocation_id"]]
            if (len(children) != 1 or children[0]["child_pid"] != item["child_pid"]
                    or children[0]["tid"] != item["tid"]):
                raise ValueError("census child identity coverage is incomplete")
            interval = process["interval"]
            if ((interval["monotonic_start_ns"] is not None and item["start_ns"] < interval["monotonic_start_ns"])
                    or (interval["monotonic_reply_ns"] is not None and item["end_ns"] > interval["monotonic_reply_ns"])):
                raise ValueError("census interval coverage is incomplete")
            period = _period(item.get("sleep_requests"), item["sleep_calls"])
            if period is not None:
                periods.append(period)
            covered += item["sleep_calls"]
        raw = process["wait_sites"].get(SLEEP_SITE, 0)
        if covered > raw or covered > counts.get("sleep_attempt", 0):
            raise ValueError("census sleeps exceed their raw observations")
        if observed:
            raw_period = _period({SLEEP_SITE: process["sleep_requests"][SLEEP_SITE]} if raw else {}, raw)
            for item in observed:
                period = _period(item["sleep_requests"], item["sleep_calls"])
                if period is not None and (raw_period is None or not raw_period[0] <= period[0] <= period[1] <= raw_period[1]):
                    raise ValueError("census requested periods differ from raw observations")
        records.extend(observed)
    return {"invocations": len(records), "sleep_calls": sum(item["sleep_calls"] for item in records),
            "maximum_nonterminal_waits": max((item["nonterminal_waits"] for item in records), default=0),
            "unbalanced_waits": sum(item["sleep_calls"] != item["nonterminal_waits"] for item in records),
            "minimum_requested_sleep_ns": min((period[0] for period in periods), default=None),
            "maximum_requested_sleep_ns": max((period[1] for period in periods), default=None)}


def census_rule(rows):
    samples = [census_sample(row) for row in rows]
    if not any(sample["invocations"] for sample in samples):
        return None, samples
    samples = [census_sample(row, required=True) for row in rows]
    periods = {sample["minimum_requested_sleep_ns"] for sample in samples if sample["sleep_calls"]}
    if (len(periods) != 1 or any(sample["invocations"] != 4 or sample["maximum_nonterminal_waits"] > 1
            or sample["unbalanced_waits"] or (sample["sleep_calls"] and sample["maximum_requested_sleep_ns"] not in periods)
            for sample in samples)):
        raise ValueError("base census controls must establish four calls and at most one wait at one observed period")
    return {"producer": CENSUS_PRODUCER, "decision": "6970432614", "invocations": 4,
            "maximum_nonterminal_waits": 1, "minimum_requested_sleep_ns": periods.pop(),
            "base_sleep_calls": [sample["sleep_calls"] for sample in samples],
            "total_policy": "derived_from_individually_gated_invocations"}, samples


def census_remainder(values, sample):
    result = dict(values)
    if sample["invocations"]:
        for name in CONTAINERS:
            raw = result.get(name, 0)
            if raw < sample["sleep_calls"]:
                raise ValueError("census sleep attribution exceeds a containing counter")
            result[name] = raw - sample["sleep_calls"]
    return result


def compare_census(rows, rule, caller):
    samples = [census_sample(row, required=rule is not None) for row in rows]
    failures = []
    for row, sample in zip(rows, samples):
        limits = {"invocations": rule["invocations"] if rule is not None else 0,
                  "maximum_nonterminal_waits": 1, "unbalanced_waits": 0}
        for name, limit in limits.items():
            invalid = sample[name] != limit if name == "invocations" else sample[name] > limit
            if invalid:
                failures.append({"caller": caller, "sample_index": row["sample_index"],
                                 "metric": METRIC + "." + name, "actual": sample[name], "limit": limit,
                                 "comparison": "exact" if name == "invocations" else "at most"})
        period = sample["minimum_requested_sleep_ns"]
        if rule is not None and period is not None and period < rule["minimum_requested_sleep_ns"]:
            failures.append({"caller": caller, "sample_index": row["sample_index"],
                             "metric": METRIC + ".minimum_requested_sleep_ns", "actual": period,
                             "limit": rule["minimum_requested_sleep_ns"], "comparison": "at least"})
    return failures, samples
