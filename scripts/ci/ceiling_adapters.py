"""Read native reporter streams without dropping parameters, subtests or retries."""
from __future__ import annotations
from collections import Counter, defaultdict
import importlib.util
import json
from pathlib import Path
import re
from xml.etree import ElementTree as ET

spec = importlib.util.spec_from_file_location("ceiling_core", Path(__file__).with_name("failure_ceiling.py"))
core = importlib.util.module_from_spec(spec)
spec.loader.exec_module(core)
require = core.require


def stream(path):
    text = Path(path).read_text(encoding="utf-8")
    require(text.endswith("\n"), f"truncated stream: {path}")
    events = [json.loads(line) for line in text.split("\n") if line]
    require(events and events[0].get("event") == "start" and events[-1].get("event") == "finish",
            f"missing start/finish: {path}")
    require(sum(e.get("event") == "start" for e in events) == 1 and sum(e.get("event") == "finish" for e in events) == 1,
            f"duplicate start/finish: {path}")
    return events


def parameter(native_id):
    return native_id[native_id.index("["):] if "[" in native_id else None


def pytest_result(path, lane, name):
    events = stream(path)
    require(not events[0].get("collect_only"), "collection-only is not execution proof")
    require(not any(e["event"] == "collection_error" for e in events), "collection error/skip is incomplete evidence")
    collections = [e["nodeids"] for e in events if e["event"] == "collection"]
    require(collections and all(Counter(ids) == Counter(collections[0]) for ids in collections), "xdist workers disagree on collected IDs")
    expected = collections[0]
    require(expected and len(expected) == len(set(expected)), "empty/duplicate pytest collection")
    reports = [e for e in events if e["event"] == "report"]
    primary = defaultdict(list)
    subtests = []
    for row in reports:
        require(row["native_id"] in expected, "uncollected pytest report")
        if row.get("subtest") is not None:
            subtests.append(row)
        else:
            primary[row["native_id"]].append(row)
    require(set(primary) == set(expected), "lost pytest ID/report")
    tests = []
    for nodeid, phases in primary.items():
        require(sum(p["stage"] == "teardown" for p in phases) == 1, f"unfinished/duplicate teardown: {nodeid}")
        require(sum(p["stage"] == "setup" for p in phases) == 1, f"missing/duplicate setup: {nodeid}")
        failures = [p for p in phases if p["outcome"] == "failed"]
        skipped = [p for p in phases if p["outcome"] == "skipped"]
        if not failures and not skipped:
            require(sum(p["stage"] == "call" and p["outcome"] == "passed" for p in phases) == 1, f"unexecuted pytest call: {nodeid}")
        selected = (failures or skipped or [p for p in phases if p["stage"] == "call"])[0]
        if len(failures) > 1:
            selected = {**selected, "stage": "+".join(p["stage"] for p in failures),
                        "diagnostic": core.canonical([{k: p[k] for k in ("stage", "diagnostic")} for p in failures])}
        outcome = selected["outcome"]
        if selected.get("wasxfail"):
            outcome = "xfailed" if outcome == "skipped" else "failed"
            if outcome == "failed":
                selected = {**selected, "diagnostic": "XPASS: " + str(selected["wasxfail"])}
        tests.append({"lane": lane, "native_id": nodeid, "parameter": parameter(nodeid), "subtest": None,
                      "stage": selected["stage"], "outcome": outcome,
                      "diagnostic": core.diagnostic(selected.get("diagnostic", "")) if outcome != "passed" else "",
                      "subtests_failed": sum(s["native_id"] == nodeid and s["outcome"] == "failed" for s in subtests)})
    for row in subtests:
        tests.append({"lane": lane, "native_id": row["native_id"], "parameter": parameter(row["native_id"]),
                      "subtest": row["subtest"], "stage": row["stage"], "outcome": row["outcome"],
                      "diagnostic": core.diagnostic(row.get("diagnostic", "")) if row["outcome"] != "passed" else ""})
    return {"name": name, "lane": lane, "format": "pytest", "complete": True,
            "raw_exit": events[-1]["exitstatus"], "summary_count": events[-1]["collected"],
            "collected": expected, "tests": tests}


def native_stream(path, lane, name):
    """Playwright/Vitest/Node observers preserve both native IDs and source keys."""
    events = stream(path)
    collected = [test for e in events if e["event"] == "collection" for test in e["tests"]]
    results = [e for e in events if e["event"] == "result"]
    ids = [r["native_id"] for r in collected]
    require(ids and len(ids) == len(set(ids)), "empty/ambiguous native collection")
    require(Counter(ids) == Counter(r["native_id"] for r in results), "native collected/executed ID mismatch; retries are not extra tests")
    tests = []
    for row in results:
        require(row.get("finished") is True, f"unfinished native test: {row['native_id']}")
        tests.append({"lane": lane, "native_id": row["native_id"], "parameter": row.get("parameter"),
                      "subtest": None, "outcome": row["outcome"], "stage": row.get("stage", "call"),
                      "diagnostic": core.diagnostic(row.get("diagnostic", "")),
                      **({"source": row["source"]} if "source" in row else {}),
                      "attempts": row.get("attempts", [])})
    require(events[-1].get("pending") == 0 and events[-1].get("count") == len(ids), "native completion summary mismatch")
    return {"name": name, "lane": lane, "format": events[0].get("runner"), "complete": True,
            "raw_exit": events[-1]["exitstatus"], "summary_count": len(ids), "collected": ids, "tests": tests}


def bun_junit(path, log_path, lane, name, raw_exit):
    """Bun native file/title identities; duplicate identities stay unqualified."""
    root = ET.parse(path).getroot()
    suites = list(root.iter("testsuite"))
    log = core.ANSI.sub("", Path(log_path).read_text(encoding="utf-8", errors="replace"))
    # Bun 1.3 emits empty <failure type=.../> elements. Bind the full preceding
    # terminal error to the exact file + nested title, never to a failure count.
    error_blocks = defaultdict(list)
    current_file, pending = None, []
    for line in log.splitlines():
        if re.fullmatch(r".+\.[cm]?[jt]sx?:", line):
            current_file, pending = line[:-1], []
            continue
        terminal = re.fullmatch(r"\((pass|fail|skip|todo)\) (.*?)(?: \[[^\]]+\])?", line)
        if terminal:
            if terminal[1] == "fail":
                error_blocks[(current_file, terminal[2])].append("\n".join(pending).strip())
            pending = []
        else:
            pending.append(line)
    tests = []
    for suite in suites:
        cases = list(suite.findall("testcase"))
        require(int(suite.attrib["tests"]) == len(list(suite.iter("testcase"))), "Bun JUnit truncated count")
        for case in cases:
            file = case.get("file") or suite.get("file")
            title = " > ".join(filter(None, (case.get("classname"), case.get("name"))))
            native = core.canonical([file, case.get("line"), case.get("classname"), case.get("name")])
            failures = list(case.findall("failure")) + list(case.findall("error"))
            skips = list(case.findall("skipped"))
            message = "\n".join((e.get("type") or "") + ": " + (e.get("message") or "") + (e.text or "") for e in failures + skips)
            if failures:
                blocks = error_blocks[(file, title)]
                require(len(blocks) == 1 and blocks[0], "Bun failure diagnostic cannot be uniquely bound to native identity")
                message += "\n" + blocks[0]
            tests.append({"lane": lane, "native_id": native, "parameter": None, "subtest": None,
                          "stage": "call", "outcome": "failed" if failures else "skipped" if skips else "passed",
                          "diagnostic": core.diagnostic(message)})
    match = re.search(r"Ran (\d+) tests? across .*files?", log)
    require(match and int(match[1]) == len(tests), "Bun terminal summary missing/truncated")
    require(tests, "Bun ran no tests")
    return {"name": name, "lane": lane, "format": "bun", "complete": True, "raw_exit": raw_exit,
            "summary_count": len(tests), "collected": [r["native_id"] for r in tests], "tests": tests}
