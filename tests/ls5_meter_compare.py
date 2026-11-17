"""Acquire an auditable, same-pin LS-5 before/after Linux comparison.

The before checkout composes the original product with the accepted LS-2 test
merge and the same LS-5-only fixture. It never changes a historical product
file, old baseline, shared meter or test assertion. Each acquisition runs the
existing LS-2 collector, including its output, task and coverage checks.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections import Counter
from pathlib import Path

BEFORE = "2f085af3e8d5dfd15dc6ef8a064055335cbcf09a"
METER = "19103ea6acbc030b3379e840ccc03491e47f1a0e"
VIBE = "7fde33e2523281f840121b93a8a865a7f5d847de"
SCOPED_FILES = (
    "tests/helpers/ls5_comfy_peer.py",
    "tests/_ls5_meter_observer.py",
    "tests/integrations/test_ls5_meter.py",
    "tests/ls5_meter_run.py",
)
UNCHANGED_METER = (
    "tests/_local_speed*", "tests/local_speed*",
    "tests/integrations/test_local_speed_meter.py",
    "tests/integrations/local_speed_sources.py",
    "tests/integrations/_pinned_source.py",
    "tests/integrations/test_runpod_slot_path_cpu.py",
    "tests/integrations/runpod_slot_pod.py",
    "tests/conftest.py",
)


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def identity(root):
    return {"commit": git(root, "rev-parse", "HEAD"), "tree": git(root, "rev-parse", "HEAD^{tree}")}


def check_shared_meter(root):
    git(root, "diff", "--exit-code", METER, "HEAD", "--", *UNCHANGED_METER)


def compose_before(root, destination, candidate):
    git(root, "worktree", "add", "--detach", str(destination), BEFORE)
    git(destination, "-c", "user.name=ChatGPT packet proof", "-c", "user.email=packet-proof@users.noreply.github.com",
        "merge", "--no-ff", "--no-edit", METER)
    for relative in SCOPED_FILES:
        path = destination / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(subprocess.check_output(["git", "-C", str(root), "show", candidate + ":" + relative]))
    git(destination, "add", "--", *SCOPED_FILES)
    git(destination, "-c", "user.name=ChatGPT packet proof", "-c", "user.email=packet-proof@users.noreply.github.com",
        "commit", "-m", "Compose LS-5 before proof with accepted meter and D10 pin")
    git(destination, "diff", "--exit-code", BEFORE, "HEAD", "--", "astrid")
    check_shared_meter(destination)
    return identity(destination)


def compare(before, after):
    """Compare observed protocol counts, without changing the LS-2 comparator."""
    for side in (before, after):
        assert side["status"] == "PASS", side.get("error")
        assert side["baseline_status"] == "RECORDED; reference comparison not requested"
        assert side["baseline"]["sources"]["vibecomfy"] == VIBE
    for dependency in ("runtime", "vibecomfy", "lifecycle"):
        assert before["baseline"]["sources"][dependency] == after["baseline"]["sources"][dependency]
    rows = []
    for caller in sorted({row["caller"] for row in after["counters"]}):
        pair = {"caller": caller}
        for name, source, expected in (("before", before, "before"), ("after", after, "held")):
            observations = [row for row in source["counters"] if row["caller"] == caller]
            assert observations
            samples = []
            for row in observations:
                assert row["ls5_comparison"]["mode"] == expected
                paths = Counter()
                for path, count in row["attached_http_paths"].items():
                    paths["GET /history/<prompt>" if path.startswith("GET /history/") else path] += count
                workers = [item for item in row["process_observations"] if item["role"] == "pack_worker"]
                assert len(workers) == 1 and workers[0]["ls5_async_sleep_observed"] is True
                counts = workers[0]["counts"]
                polls = counts["ls5_comfy_poll_sleep_attempt"]
                assert paths["POST /prompt"] == 1
                if name == "after":
                    assert paths["GET /history/<prompt>"] == paths["GET /ws"] == 1
                    assert polls == counts.get("sleep_attempt", 0) == 0
                else:
                    assert paths["GET /history/<prompt>"] >= 2 and paths["GET /ws"] == 0
                    assert polls >= 1
                samples.append({"requests": dict(sorted(paths.items())),
                                "comfy_poll_sleep_attempts": polls,
                                "worker_sync_sleep_attempts": counts.get("sleep_attempt", 0),
                                "worker_async_sleep_attempts": counts["ls5_async_sleep_attempt"],
                                "proof": row["proof"]})
            pair[name] = samples
        rows.append(pair)
    assert {row["caller"] for row in rows} == {"resident_sdk", "fresh_python_cli"}
    return rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--samples", type=int, default=2)
    parser.add_argument("--counter-samples", type=int, default=1)
    parser.add_argument("--timeout", type=int, default=1800)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    before_root = output / "before-source"
    record = {"schema": "astrid.ls5-comparison.v1", "status": "NO-RESULT", "label": "LS-5 comparison",
              "before_product": BEFORE, "accepted_meter": METER, "approved_vibecomfy": VIBE,
              "claim": "request and Comfy poll counts; not the old LS-2 reference or the D1 latency budget"}
    try:
        git(root, "diff", "--exit-code")
        git(root, "diff", "--cached", "--exit-code")
        check_shared_meter(root)
        record["after"] = identity(root)
        record["before"] = compose_before(root, before_root, record["after"]["commit"])
        git(before_root, "archive", "--format=zip", "--output=" + str(output / "before-source.zip"), "HEAD")
        (output / "before-composition.patch").write_text(git(before_root, "diff", BEFORE, "HEAD") + "\n")
        (output / "sources.json").write_text(json.dumps(record, indent=2) + "\n")
        results = {}
        for name, source, mode in (("before", before_root, "before"), ("after", root, "held")):
            command = [sys.executable, "-m", "tests.ls5_meter_run", "--output", str(output / name),
                       "--samples", str(args.samples), "--counter-samples", str(args.counter_samples),
                       "--timeout", str(args.timeout)]
            (output / (name + "-command.json")).write_text(json.dumps(command, indent=2) + "\n")
            with (output / (name + "-runner.log")).open("w") as log:
                result = subprocess.run(command, cwd=source, env=dict(os.environ, ASTRID_LS5_MODE=mode),
                                        stdout=log, stderr=subprocess.STDOUT, check=False,
                                        timeout=args.timeout * 2 + 120)
            (output / (name + ".exit")).write_text(str(result.returncode) + "\n")
            summary = output / name / "summary.json"
            results[name] = json.loads(summary.read_text()) if summary.exists() else {"status": "MISSING"}
        record["callers"] = compare(results["before"], results["after"])
        for name in ("before", "after"):
            assert results[name]["baseline"]["sources"]["agent"] == record[name]["commit"]
        record["status"] = "PASS"
    except (AssertionError, OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        record["error"] = f"{type(error).__name__}: {error}"
    finally:
        (output / "comparison.json").write_text(json.dumps(record, indent=2) + "\n")
        if before_root.exists():
            subprocess.run(["git", "-C", str(root), "worktree", "remove", str(before_root)], check=False)
    print(json.dumps(record, indent=2))
    return 0 if record["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
