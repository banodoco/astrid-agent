#!/usr/bin/env python3
"""Compare a CI run's job logs with the Agent ceiling. Fails closed. Standard library only (Python 3.11).

  python3 -I scripts/ci/agent_ceiling_compare.py LOGDIR [LOGDIR ...] \
      --ceiling ci/agent-ceiling.tsv --baseline ci/agent-baseline-passed.tsv \
      [--base-ceiling BASE_CEILING.tsv] [--moving ci/agent-moving-groups.tsv] \
      [--expect-jobs "unit:8,integration:4,darwin native,opt-in,installed wheel"] [--out DIR]

Each LOGDIR is a run's log archive, unpacked (give the CI run's and the Lanes run's): one `<N>_<job name>.txt` per job at its top level (the per-step
sub-directories are ignored). Every line may carry GitHub's timestamp. Jobs are keyed by family, never by shard:
`unit shard 3 / 8` is the family `unit`, `integration shard 2 / 4` is `integration`, and any other job is its name
with the archive's leading `N_` index removed. A test that moves between shards is the same entry.

It exits 1 (and says why) on any of:
  new failure     a (family, id) failing that is not on the ceiling, and not a member of a moving group failing
                  with that group's message. A member of a moving group failing with another message is new.
  lost id         an id that passed in the baseline and did not run in the candidate (deleted, skipped,
                  deselected, uncollected).
  bad job         an expected job that is absent from the archive, whose log has no final pytest result line
                  (cancelled, timed out, truncated), whose FAILED/ERROR rows do not equal its result line, or
                  that ran zero tests.
  ceiling growth  with --base-ceiling: an entry in the ceiling that the base does not have.
  new xfail       an id that passed on the baseline and now reports XFAIL (a marker added to hide a failure).
Always pass the BASE branch's ceiling, baseline and moving-groups files (for example `git show <base>:ci/agent-ceiling.tsv`),
never the candidate's: a candidate that edits them would otherwise approve itself.
Ceiling entries that now pass are "ratchet candidates": reported, written to RATCHET.tsv, and not a failure.
Exit 2 is bad input.

Parsing follows evidence/cluster_failures.py of Plan 91 (AG-0): an id ends at the first " - " outside its
[parameters]; a failed subtest has no summary row, so it is read from its SUBFAILED(...) progress line and keyed
"<nodeid> SUBFAILED(<params>)"; the last "short test summary info" section before the final result
line of each of the job's own runs is read, so pytest output that a test prints from an inner run is not counted, and
a job with several sequential runs (several pytest steps, or a dispatch repeat loop) has every run read (see
top_level_runs).
"""
from __future__ import annotations

import argparse
import fnmatch
import re
import sys
from pathlib import Path

STAMP = re.compile(r"^\d{4}-\d\d-\d\dT[\d:.]+Z ?")
FINAL = re.compile(r"^=*\s*((?:\d+ (?:failed|passed|errors?|skipped|deselected|xfailed|xpassed)\b|no tests ran\b).*) in [\d.]+s\b")
SUBFAILED = re.compile(r"^(\S+::\S+) SUBFAILED(\(.*?\))\s+\[\s*\d+%\]$")
SUMMARY_START = re.compile(r"^=+ short test summary info =+$")
ROW = re.compile(r"^(PASSED|FAILED|ERROR|XFAIL|XPASS) (.+)$")
PASSING = {"PASSED", "XFAIL", "XPASS"}
DEFAULT_EXPECT = "unit:8,integration:4,darwin native,opt-in,installed wheel"
CEILING_HEADER = ("job", "id", "note")
PASSED_HEADER = ("job", "id")


def split_nodeid(rest: str):
    """Split "<nodeid> - <message>" where the nodeid may hold spaces inside its [parameters]."""
    depth = 0
    for index, char in enumerate(rest):
        if char == "[":
            depth += 1
        elif char == "]":
            depth = max(0, depth - 1)
        elif depth == 0 and rest.startswith(" - ", index):
            return rest[:index], rest[index + 3:]
    return rest, ""


def read_lines(path: Path):
    return [STAMP.sub("", raw.rstrip("\r\n")) for raw in path.read_text(errors="replace").splitlines()]


HEADER = re.compile(r"^=+ test session starts =+$")
STEP = re.compile(r"^##\[group\]Run ")
SELECTION = re.compile(r"^===== selection \d+ of \d+: ")
CANCELLED = re.compile(r"^##\[error\]The operation was cancell?ed\.")


SECTION = re.compile(r"^=+ (FAILURES|ERRORS|PASSES|warnings summary) =+$")


def plain_runs(lines, offset=0):
    """Top-level pytest runs of a plain log (no GitHub step lines) as [(start, summary_start or None, end)], plus
    problems. One scan keeps a frame per open session: a "test session starts" line opens a frame and a final result
    line closes it, and a frame is inside a report section (FAILURES, ERRORS, PASSES, warnings) from that heading to
    its own "short test summary info" heading. Output printed in a report section is a test's captured output, so a
    session or result line there is an inner run and is skipped. A result line outside every report section with no
    open session ends a header-less (pytest -q) run. So sequential runs, with headers or not, are each read, and an
    inner run never is. A session that is still open at the end is truncated."""
    runs, problems = [], []
    root = {"start": offset, "summary": None, "section": False}
    stack = []
    for i, line in enumerate(lines, offset):
        top = stack[-1] if stack else root
        if HEADER.match(line):
            stack.append({"start": i, "summary": None, "section": False, "inner": top["section"] or bool(stack)})
        elif SECTION.match(line):
            top["section"] = True
        elif SUMMARY_START.match(line):
            top["section"], top["summary"] = False, i
        elif FINAL.match(line):
            if top["section"]:
                continue  # a header-less inner run printed in a report section
            if stack:
                frame = stack.pop()
                if not frame["inner"]:
                    runs.append((frame["start"], frame["summary"], i))
                    root = {"start": i + 1, "summary": None, "section": False}
            else:
                runs.append((root["start"], root["summary"], i))
                root = {"start": i + 1, "summary": None, "section": False}
    if stack:
        problems.append(f"a pytest session from line {stack[0]['start'] + 1} has no final result line (truncated)")
    elif root["summary"] is not None:
        problems.append(f"a short test summary from line {root['summary'] + 1} has no final result line (truncated)")
    return runs, problems


def top_level_runs(lines):
    """The job's own pytest runs as [(start, summary_start or None, end)], plus problems found.

    A job log can hold several sequential runs (several pytest steps, or a dispatch `repeat` loop), and a test can
    print an inner run's output inside the outer one. A GitHub job log is first cut at each step's "##[group]Run"
    line and each repeat's "===== selection i of n" line; each piece, or a plain log as a whole, is then read by
    plain_runs. A GitHub "operation was canceled" line is a problem.
    """
    runs, problems = [], []
    if any(CANCELLED.match(line) for line in lines):
        problems.append("the job was cancelled or timed out (GitHub cancel line)")
    if any(STEP.match(line) for line in lines):
        bounds = [i for i, line in enumerate(lines) if STEP.match(line) or SELECTION.match(line)]
        pieces = list(zip([0] + bounds, bounds + [len(lines)]))
        for a, b in pieces:
            found, more = plain_runs(lines[a:b], offset=a)
            runs += found
            problems += more
        return runs, problems
    found, more = plain_runs(lines)
    return found, problems + more


def parse_job_log(lines):
    """Return (counts, rows, problems). counts is the (failed, errors, passed) sum of the job's own runs' result
    lines, or None when it has none. rows is [(status, id, message)] over those runs, including failed subtests."""
    runs, problems = top_level_runs(lines)
    if not runs:
        return None, [], problems
    total, rows = [0, 0, 0], []
    for begin, summary, end in runs:
        text = FINAL.match(lines[end]).group(1)

        def number(word):
            found = re.search(rf"(\d+) {word}", text)
            return int(found.group(1)) if found else 0

        counts = (number("failed"), number("errors?"), number("passed"))
        run_rows = []
        if summary is not None:
            for line in lines[summary + 1:end]:
                match = ROW.match(line)
                if match and "::" in match.group(2):
                    nodeid, message = split_nodeid(match.group(2))
                    run_rows.append((match.group(1), nodeid.strip(), message.strip()))
        seen = set()
        for line in lines[begin:end]:
            match = SUBFAILED.match(line)
            if match:
                nodeid = f"{match.group(1)} SUBFAILED{match.group(2)}"
                if nodeid not in seen:
                    seen.add(nodeid)
                    run_rows.append(("FAILED", nodeid, ""))
        got = (sum(1 for r in run_rows if r[0] == "FAILED"), sum(1 for r in run_rows if r[0] == "ERROR"))
        if got != counts[:2]:
            problems.append(f"a run ending at line {end + 1}: result line says {counts[0]} failed, {counts[1]} errors; "
                            f"summary rows are {got[0]} FAILED, {got[1]} ERROR (truncated or misread summary)")
        total = [x + y for x, y in zip(total, counts)]
        rows += run_rows
    return tuple(total), rows, problems


def job_family(name: str):
    """(family, shard) for a job name with its archive index already removed; shard is (n, total) or None."""
    match = re.match(r"^(unit|integration) shard (\d+) [_/] (\d+)\b", name)
    if match:
        return match.group(1), (int(match.group(2)), int(match.group(3)))
    if name == "unit":
        return "unit (aggregate)", None  # the job that needs the shards; it runs no tests
    return name, None


def parse_expect(spec: str):
    """{family: shard count or None}, in the order given."""
    out = {}
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        name, sep, count = part.rpartition(":")
        if sep and count.isdigit():
            out[name.strip()] = int(count)
        else:
            out[part] = None
    return out


def load_jobs(logdirs):
    """{family: [job]} for each job log at the top level of the archives. A job is a dict."""
    families = {}
    for path in sorted(p for d in logdirs for p in d.glob("*.txt")):
        name = re.sub(r"^\d+_", "", path.stem)
        family, shard = job_family(name)
        counts, rows, problems = parse_job_log(read_lines(path))
        families.setdefault(family, []).append({"name": name, "shard": shard, "counts": counts, "rows": rows,
                                                "problems": problems})
    return families


def check_jobs(families, expect):
    """Problems for each expected job, as strings."""
    problems = []
    for family, shards in expect.items():
        jobs = families.get(family, [])
        if shards is not None:
            by_shard = {}
            for job in jobs:
                by_shard.setdefault(job["shard"][0] if job["shard"] else None, []).append(job)
            for number in range(1, shards + 1):
                if number not in by_shard:
                    problems.append(f"{family} shard {number} of {shards}: absent from the archive")
                elif len(by_shard[number]) > 1:
                    problems.append(f"{family} shard {number} of {shards}: {len(by_shard[number])} logs for one job")
            for number in by_shard:
                if number is None or number > shards:
                    problems.append(f"{family}: a shard outside 1..{shards}: {[j['name'] for j in by_shard[number]]}")
            jobs = [j for j in jobs if j["shard"] and j["shard"][0] <= shards]
        elif not jobs:
            problems.append(f"{family}: absent from the archive")
        elif len(jobs) > 1:
            problems.append(f"{family}: {len(jobs)} logs for one job")
        for job in jobs:
            problems.extend(check_one(job))
    return problems


def check_one(job):
    name, counts = job["name"], job["counts"]
    out = [f"{name}: {p}" for p in job["problems"]]
    if counts is None:
        return out + [f"{name}: no final pytest result line (cancelled, timed out or truncated)"]
    if sum(counts) == 0:
        out.append(f"{name}: zero tests ran")
    return out


def read_tsv(path: Path, header):
    """Rows of a TSV as tuples of len(header) (missing trailing columns are ''); comments and the header skipped."""
    rows = []
    for number, raw in enumerate(path.read_text().splitlines(), 1):
        if not raw.strip() or raw.startswith("#"):
            continue
        cols = raw.split("\t")
        if tuple(cols[:len(header)]) == header or (cols[0], cols[1]) == header[:2]:
            continue
        if len(cols) < 2 or not cols[0] or not cols[1]:
            raise ValueError(f"{path}:{number}: expected job<TAB>id, got {raw!r}")
        rows.append(tuple((cols + [""] * len(header))[:len(header)]))
    return rows


def read_moving(path: Path):
    """[(group, pattern, message substring)]."""
    rows = []
    for number, raw in enumerate(path.read_text().splitlines(), 1):
        if not raw.strip() or raw.startswith("#"):
            continue
        cols = raw.split("\t")
        if cols[0] == "group":
            continue
        if len(cols) < 3 or not all(cols[:3]):
            raise ValueError(f"{path}:{number}: expected group<TAB>id pattern<TAB>message substring, got {raw!r}")
        rows.append((cols[0], cols[1], cols[2]))
    return rows


def moving_verdict(nodeid, message, moving):
    """None if nodeid is in no moving group; else (group, True) when it fails with that group's message, or
    (groups, False) when it is a member but the message differs."""
    members = [(g, sub) for g, pattern, sub in moving if fnmatch.fnmatchcase(nodeid, pattern)]
    if not members:
        return None
    for group, sub in members:
        if sub in message:
            return group, True
    return ",".join(sorted({g for g, _ in members})), False


def classify(families, expect, ceiling, baseline_passed, moving):
    """Compare candidate results, keyed (family, id). Returns a dict of result lists."""
    ran_failed, ran_passed, ran_xfail, messages = {}, set(), set(), {}
    for family, jobs in families.items():
        for job in jobs:
            for status, nodeid, message in job["rows"]:
                key = (family, nodeid)
                if status in PASSING:
                    ran_passed.add(key)
                    if status == "XFAIL":
                        ran_xfail.add(key)
                else:
                    ran_failed[key] = status
                    messages.setdefault(key, message)
    ran_passed -= set(ran_failed)  # a teardown error after a pass is a failure
    result = {"new": [], "moving": [], "lost": [], "ratchet": [], "known": 0}
    for key in sorted(ran_failed):
        verdict = moving_verdict(key[1], messages[key], moving)
        if verdict is not None:
            group, matched = verdict
            (result["moving"] if matched else result["new"]).append(
                key + ((f"moving group {group}",) if matched else (f"member of moving group {group} failed with another message: {messages[key][:120]!r}",)))
        elif key in ceiling:
            result["known"] += 1
        else:
            result["new"].append(key + (messages[key][:120],))
    for key in sorted((ran_xfail - set(ran_failed)) & baseline_passed):
        # It passed on the baseline and is now expected to fail: an xfail marker added to hide a failure.
        result["new"].append(key + ("passed on the baseline, now XFAIL (an xfail marker was added)",))
    for key in sorted(ceiling):
        if key in ran_passed and key not in ran_xfail:
            result["ratchet"].append(key)
    present = {family for family in expect if families.get(family)} | {f for f in families if f not in expect}
    for key in sorted(baseline_passed - ran_passed - set(ran_failed)):
        if key[0] in present:
            result["lost"].append(key)
    return result


def write_tsv(path: Path, header, rows):
    path.write_text("\n".join(["\t".join(header)] + ["\t".join(r) for r in rows]) + "\n")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("logdir", nargs="+", help="a run's log archive, unpacked (the CI run's and the Lanes run's)")
    ap.add_argument("--ceiling", required=True, help="ci/agent-ceiling.tsv: job<TAB>id[<TAB>note]")
    ap.add_argument("--baseline", help="ci/agent-baseline-passed.tsv: job<TAB>id of tests that passed on the baseline run")
    ap.add_argument("--base-ceiling", help="the base branch's ceiling; any entry the ceiling has and this lacks fails")
    ap.add_argument("--moving", help="ci/agent-moving-groups.tsv: group<TAB>id glob<TAB>required message substring")
    ap.add_argument("--expect-jobs", default=DEFAULT_EXPECT,
                    help=f"families that must be present and sound; NAME:N is N shards (default: {DEFAULT_EXPECT})")
    ap.add_argument("--out", help="directory for report.txt, new.tsv, lost.tsv and RATCHET.tsv")
    ap.add_argument("--write-passed", metavar="FILE", help="write the passed list of these logs (a baseline) and exit")
    a = ap.parse_args(argv)
    logdirs = [Path(d) for d in a.logdir]
    try:
        for logdir in logdirs:
            if not logdir.is_dir():
                raise OSError(f"{logdir} is not a directory")
        families = load_jobs(logdirs)
        expect = parse_expect(a.expect_jobs)
        if a.write_passed:
            gated = {f: jobs for f, jobs in families.items() if f in expect}  # other jobs run several pytest steps
            failing = {(f, n) for f, jobs in gated.items() for j in jobs for s, n, _ in j["rows"] if s not in PASSING}
            passed = sorted({(f, n) for f, jobs in gated.items() for j in jobs for s, n, _ in j["rows"]
                             if s == "PASSED" and (f, n) not in failing})
            write_tsv(Path(a.write_passed), PASSED_HEADER, passed)
            print(f"wrote {len(passed)} passed ids to {a.write_passed}")
            return 0
        ceiling_rows = read_tsv(Path(a.ceiling), CEILING_HEADER)
        ceiling = {(r[0], r[1]) for r in ceiling_rows}
        baseline = {(r[0], r[1]) for r in read_tsv(Path(a.baseline), PASSED_HEADER)} if a.baseline else set()
        base_ceiling = ({(r[0], r[1]) for r in read_tsv(Path(a.base_ceiling), CEILING_HEADER)}
                        if a.base_ceiling else None)
        moving = read_moving(Path(a.moving)) if a.moving else []
    except (OSError, ValueError) as e:
        print(f"agent-ceiling-compare: {e}", file=sys.stderr)
        return 2
    if not a.baseline:
        print("agent-ceiling-compare: no --baseline given; lost ids are NOT checked", file=sys.stderr)

    out = []
    out.append("expected jobs: " + ", ".join(f"{f} ({n} shards)" if n else f for f, n in expect.items()))
    for family, jobs in sorted(families.items()):
        gated = "gated" if family in expect else "not gated"
        for job in sorted(jobs, key=lambda j: j["name"]):
            c = job["counts"]
            line = "NO RESULT LINE" if c is None else f"{c[0]} failed, {c[1]} errors, {c[2]} passed; {len(job['rows'])} rows"
            out.append(f"  {family} | {job['name']} | {gated} | {line}")
    problems = check_jobs(families, expect)
    result = classify(families, expect, ceiling, baseline, moving)
    grown = sorted(ceiling - base_ceiling) if base_ceiling is not None else []

    out.append("")
    out.append(f"bad jobs: {len(problems)}")
    out += [f"  FAIL {p}" for p in problems]
    out.append(f"new failures: {len(result['new'])}")
    out += [f"  FAIL {f} | {n} | {why}" for f, n, why in result["new"][:200]]
    out.append(f"lost ids (passed on the baseline, did not run): {len(result['lost'])}")
    out += [f"  FAIL {f} | {n}" for f, n in result["lost"][:50]]
    if len(result["lost"]) > 50:
        out.append(f"  ... {len(result['lost']) - 50} more in lost.tsv")
    if a.base_ceiling:
        out.append(f"ceiling entries added over the base: {len(grown)}")
        out += [f"  FAIL {f} | {n}" for f, n in grown[:50]]
    else:
        out.append("ceiling growth: not checked (no --base-ceiling)")
    out.append(f"failing on the ceiling (known): {result['known']}")
    out.append(f"moving-group members failing with their group's message: {len(result['moving'])}")
    out += [f"  moving {f} | {n} | {why}" for f, n, why in result["moving"]]
    out.append(f"ratchet candidates (on the ceiling, now passing): {len(result['ratchet'])}")
    out += [f"  ratchet {f} | {n}" for f, n in result["ratchet"][:50]]
    failed = bool(problems or result["new"] or result["lost"] or grown)
    out.append("")
    out.append("RESULT: FAIL" if failed else "RESULT: PASS")
    text = "\n".join(out) + "\n"
    if a.out:
        d = Path(a.out)
        d.mkdir(parents=True, exist_ok=True)
        (d / "report.txt").write_text(text)
        write_tsv(d / "new.tsv", ("job", "id", "why"), [tuple(r) for r in result["new"]])
        write_tsv(d / "lost.tsv", PASSED_HEADER, result["lost"])
        write_tsv(d / "RATCHET.tsv", PASSED_HEADER, result["ratchet"])
    sys.stdout.write(text)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
