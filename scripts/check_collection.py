"""Fail on unexpected pytest collection errors, including new causes in known files."""

import argparse
import re
import subprocess
import sys
from pathlib import Path

KNOWN = Path(__file__).with_name("known_collection_errors.txt")
HEADING = re.compile(r"^(?:_+\s+)?ERROR collecting (.+?)(?:\s+_+)?$")
SUMMARY = re.compile(r"^ERROR\s+(\S+)(?:\s+-\s+(.*))?$")
COUNT = re.compile(r"\b(\d+)\s+tests? collected\b")


def parse_errors(output):
    """Return separate diagnostics per error id, merging duplicate summary lines."""
    errors = {}
    current = None
    headings = {}
    summaries = {}
    for line in output.splitlines():
        heading = HEADING.match(line)
        summary = SUMMARY.match(line)
        if heading:
            current = heading.group(1)
            headings[current] = headings.get(current, 0) + 1
            errors.setdefault(current, []).append([])
        elif summary:
            current = None
            name, detail = summary.group(1), summary.group(2) or ""
            summaries[name] = summaries.get(name, 0) + 1
            if summaries[name] > headings.get(name, 0):
                errors.setdefault(name, []).append([detail])
        elif current is not None:
            if re.match(r"^=+", line):
                current = None
            else:
                errors[current][-1].append(line)
    return {name: ["\n".join(lines) for lines in diagnostics]
            for name, diagnostics in errors.items()}


def read_known(path):
    entries = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, sep, reason = line.partition("::")
        if not sep or not name or not reason:
            raise ValueError(f"{path}:{number}: expected path::reason-key")
        entries.append((name, reason))
    return entries


def evaluate(output, returncode, known, strict=False):
    errors = parse_errors(output)
    counts = COUNT.findall(output)
    failures = []
    if returncode not in (0, 1) or not counts or int(counts[-1]) == 0:
        failures.append(f"pytest failed or collected no tests (exit {returncode})")
    if (returncode == 0) != (not errors):
        failures.append(f"pytest exit {returncode} is inconsistent with parsed collection errors")
    matched = {entry for entry in known if any(
        entry[1] in diagnostic for diagnostic in errors.get(entry[0], []))}
    new = [(name, diagnostic) for name, diagnostics in errors.items() for diagnostic in diagnostics
           if not any(name == path and reason in diagnostic for path, reason in known)]
    stale = sorted(set(known) - matched)
    messages = failures + [f"NEW collection error: {name}\n{diagnostic}"
                           for name, diagnostic in sorted(new)]
    messages += [f"{'STALE' if strict else 'WARNING: stale'} entry: {path}::{reason}"
                 for path, reason in stale]
    return int(bool(failures or new or (strict and stale))), messages


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strict", action="store_true", help="fail on stale known errors")
    args = parser.parse_args(argv)
    try:
        known = read_known(KNOWN)
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider",
             "-o", "addopts=", "--continue-on-collection-errors", "tests"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, check=False,
        )
        status, messages = evaluate(result.stdout, result.returncode, known, args.strict)
    except (OSError, ValueError) as exc:
        print(f"Collection gate failed: {exc}", file=sys.stderr)
        return 1
    if status and not parse_errors(result.stdout):
        print(result.stdout, file=sys.stderr)
    for message in messages:
        print(message)
    if not status:
        print("Collection gate passed")
    return status


if __name__ == "__main__":
    sys.exit(main())
