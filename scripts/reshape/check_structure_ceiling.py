"""Check the candidate's structure ceiling using only merge-base gate code.

CI exports the merge base and invokes this file there with isolated Python.
The candidate supplies source to inspect and a proposed ceiling, never Python
to import. No result file or log is accepted as evidence of a completed scan.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

TRUSTED_ROOT = Path(__file__).resolve().parents[2]
CEILING = Path("scripts/reshape/structure_ceiling.json")


@dataclass(frozen=True)
class Ceiling:
    origin: dict[str, str]
    violations: tuple[str, ...]


def load_ceiling(path: Path) -> Ceiling:
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or set(document) != {"tool", "origin", "violations"}:
        raise ValueError(f"invalid structure ceiling document: {path}")
    if document["tool"] != "structure":
        raise ValueError(f"wrong ceiling tool: {path}")
    origin = document["origin"]
    if not isinstance(origin, dict) or not origin or any(
        not isinstance(key, str) or not isinstance(value, str) or not value
        for key, value in origin.items()
    ):
        raise ValueError(f"missing ceiling provenance: {path}")
    violations = document["violations"]
    if not isinstance(violations, list) or any(
        not isinstance(value, str) or not value.strip() or "\n" in value
        for value in violations
    ):
        raise ValueError(f"invalid ceiling records: {path}")
    if violations != sorted(set(violations)):
        raise ValueError(f"ceiling records must be sorted and unique: {path}")
    return Ceiling(origin=origin, violations=tuple(violations))


def compare_ceiling(before: Ceiling, proposed: Ceiling, found: list[str]) -> list[str]:
    errors = []
    accepted, candidate, observed = set(before.violations), set(proposed.violations), set(found)
    if before.origin != proposed.origin:
        errors.append("the frozen ceiling provenance changed")
    errors.extend(f"ceiling entry added or edited: {value}" for value in sorted(candidate - accepted))
    errors.extend(f"new structure finding: {value}" for value in sorted(observed - candidate))
    errors.extend(
        f"ceiling entry removed while its finding remains: {value}"
        for value in sorted((accepted - candidate) & observed)
    )
    return errors


def scan(root: Path) -> list[str]:
    for relative in ("astrid/__init__.py", "astrid/core/__init__.py", "astrid/packs/__init__.py"):
        if not (root / relative).is_file():
            raise ValueError(f"incomplete source tree: missing {relative}")

    # Isolated Python has not imported the candidate's editable installation.
    # Put the exported merge base first for both the gate and product scanner.
    sys.path.insert(0, str(TRUSTED_ROOT))
    from scripts.reshape.compare_structure_baseline import current

    found = current(root)
    if not isinstance(found, list) or any(not isinstance(value, str) or not value for value in found):
        raise ValueError("structure scanner did not return complete finding records")
    if found != sorted(set(found)):
        raise ValueError("structure scanner returned invalid finding records")
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True, help="candidate checkout to inspect")
    args = parser.parse_args(argv)
    try:
        root = args.root.resolve(strict=True)
        if root == TRUSTED_ROOT:
            raise ValueError("run the checker exported from the merge base, not the candidate's own copy")
        before = load_ceiling(TRUSTED_ROOT / CEILING)
        proposed = load_ceiling(root / CEILING)
        found = scan(root)
        errors = compare_ceiling(before, proposed, found)
        for error in errors:
            print(f"STRUCTURE CEILING ERROR: {error}", file=sys.stderr)
        for value in sorted(set(proposed.violations) - set(found)):
            print(f"STRUCTURE improvement (resolved): {value}")
        print(
            f"Structure ceiling: trusted={len(before.violations)} candidate={len(proposed.violations)} "
            f"current={len(found)} errors={len(errors)}"
        )
        print(f"Structure scanner source: {TRUSTED_ROOT}")
        return int(bool(errors))
    except (OSError, ValueError, RuntimeError, SystemExit) as exc:
        # A scanner/import which exits early, including SystemExit(0), is not
        # a completed scan. Missing/empty/truncated ceiling data also fails.
        print(f"Structure ceiling failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
