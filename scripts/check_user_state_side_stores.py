#!/usr/bin/env python3
"""Shrink-only ratchet for user-state side stores in Astrid Python sources."""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "scripts" / "reshape" / "baselines" / "user_state_side_stores.json"
PATTERN = re.compile(r"Path\.home\(\)|XDG_(?:STATE|DATA|CONFIG)_HOME|os\.path\.expanduser\(\s*[\"']~")


def scan_source(text: str) -> int:
    return sum(
        len(PATTERN.findall(line))
        for line in text.splitlines()
        if not line.strip().startswith("#")
    )


def scan_tree(root: Path) -> dict[str, int]:
    found = {}
    for path in sorted((root / "astrid").rglob("*.py")):
        try:
            count = scan_source(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError):
            continue
        if count:
            found[path.relative_to(root).as_posix()] = count
    return found


def load_baseline(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def findings(found: dict[str, int], baseline: dict) -> list[str]:
    violations = []
    files = baseline["files"]
    for path, count in sorted(found.items()):
        if path not in files:
            violations.append(
                f"new side store: {path} uses {count} home/XDG paths; classify it in the "
                "baseline with a category and a reason, or use Runtime app-state"
            )
        elif count > files[path]["count"]:
            violations.append(f"{path} rose from {files[path]['count']} to {count}")
    for path, entry in sorted(files.items()):
        if entry["category"] not in baseline["categories"]:
            violations.append(f"{path}: unknown category {entry['category']!r}")
    return violations


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="check for new or increased uses")
    mode.add_argument("--write", action="store_true", help="tighten existing reviewed counts")
    args = parser.parse_args(argv)
    baseline = load_baseline(BASELINE)
    found = scan_tree(ROOT)
    violations = findings(found, baseline)
    if violations:
        print("user-state side-store ratchet grew:", file=sys.stderr)
        print("\n".join(f"  {item}" for item in violations), file=sys.stderr)
        return 1
    if args.write:
        # Do not erase classifications for missing files in a partial checkout.
        # Remove an entry only when its readable source has no remaining matches.
        for path, entry in list(baseline["files"].items()):
            if path in found:
                entry["count"] = found[path]
            else:
                try:
                    count = scan_source((ROOT / path).read_text(encoding="utf-8"))
                except (OSError, UnicodeError):
                    continue
                if count == 0:
                    del baseline["files"][path]
        BASELINE.write_text(json.dumps(baseline, indent=2) + "\n", encoding="utf-8")
        print("wrote tightened user-state side-store baseline")
    else:
        print("user-state side-store ratchet passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
