"""Reproduce the old gate's blind spots without altering a product test."""

import argparse
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import types

from tests import _local_speed_baseline as current
from tests.core.generation.test_local_speed_baseline import baseline
from tests.core.generation.test_local_speed_meter_collector import rows, write
from tests.core.generation.test_local_speed_process_observer import TARGETS, observer
from tests.local_speed_meter_run import parse_reports

OLD_SHA = "19103ea6acbc030b3379e840ccc03491e47f1a0e"


def old_module(path, name):
    source = subprocess.check_output(["git", "show", OLD_SHA + ":" + path], text=True)
    module = types.ModuleType(name)
    sys.modules[name] = module
    exec(compile(source, OLD_SHA + "/" + path, "exec"), module.__dict__)
    return module


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    old = old_module("tests/_local_speed_baseline.py", "old_ls2_baseline")
    old_process = old_module("tests/_local_speed_process.py", "old_ls2_process")
    old_collector = old_module("tests/local_speed_meter_run.py", "old_ls2_collector")
    reference = baseline()
    reference["tolerance"] = old.TOLERANCE
    probes = []
    for name, metric in (("20_percent_slowdown", "latency"), ("extra_sleep", "python.sleep_attempt"),
                         ("extra_commit", "runtime_commit_statements"), ("extra_flush", "native.completed_flushes"),
                         ("vanished_counter", "missing")):
        candidate = copy.deepcopy(reference)
        cell = candidate["callers"]["resident_sdk"]
        if metric == "latency":
            cell["overhead_seconds"]["median"] *= 1.2
        elif metric == "missing":
            del cell["counter_maxima"]["python.bundle_emission"]
        else:
            cell["counter_maxima"][metric] = cell["counter_maxima"].get(metric, 0) + 1
        prior, new = old.compare_baseline(candidate, reference), current.compare_baseline(candidate, reference)
        assert prior == [] and new
        probes.append({"probe": name, "old": "PASS", "new": "FAIL", "rejections": new})
    measured = []
    for meter in (old_process.ProcessObserver(TARGETS, "probe", hash_source="", observer_source=""), observer()):
        meter.start()
        try:
            for _ in range(8):
                threading.Event().wait(0)
            measured.append(meter.snapshot()["counts"])
        finally:
            meter.stop()
    assert measured[0].get("event_wait_attempt", 0) == 0
    assert measured[1]["event_wait_attempt"] == 8
    probes.append({"probe": "hidden_Event_wait_loop", "old": "PASS (unseen)", "new": "FAIL (8 waits over zero)", "counts": measured})
    offered = copy.deepcopy(reference)
    offered["callers"]["resident_sdk"]["overhead_seconds"]["median"] *= 1.2
    assert old.compare_baseline(offered, offered) == []
    try:
        current.approve_reference(offered, reference)
    except ValueError:
        probes.append({"probe": "regressed_replacement_reference", "old": "PASS", "new": "FAIL"})
    else:
        raise AssertionError("regressed replacement was accepted")
    with tempfile.TemporaryDirectory() as temporary:
        path = Path(temporary)
        value = rows(True)
        for row in value:
            row["gate_version"] = 3
            row["harness_settings"] = {"host_poll_seconds": 0.02}
        log = write(path, value)
        assert len(old_collector.parse_reports(log, instrumented=True)) == 2
        try:
            parse_reports(log, instrumented=True, require_gate=True)
        except ValueError as error:
            assert "product-default" in str(error)
            probes.append({"probe": "accelerated_claim_poll_cell", "old": "PASS", "new": "FAIL"})
        else:
            raise AssertionError("nondefault meter cell was accepted")
    result = {"schema": "astrid.ls2-adversarial-probes.v1", "old_sha": OLD_SHA,
              "candidate_sha": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
              "probes": probes, "status": "PASS"}
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
