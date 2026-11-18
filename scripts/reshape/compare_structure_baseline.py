"""Gate for `astrid.core.structure.validate_repo_structure`.

The baseline is the set of known violations, each normalised so it survives unrelated edits
(no line numbers, no absolute paths, first line of a traceback only). The gate fails on any
violation not in the baseline. Baseline entries that no longer occur are reported as improvements
and pass; the manager can remove them when updating the baseline for a merged batch.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
# GL.2 freezes the owner's exact 102 records separately from the original
# 23-entry baseline, which remains byte-identical as provenance. CI also runs
# check_structure_ceiling.py from the merge base: this report cannot authorize
# additions to the ceiling or changes to its own scanner.
BASELINE_PATH = REPO_ROOT / "scripts" / "reshape" / "structure_ceiling.json"

# Make direct execution (as used by ``make structure``) able to import the
# checkout's ``astrid`` package even though Python puts this script's folder on
# sys.path, not the repository root.
if __package__ in {None, ""}:
    sys.path.insert(0, str(REPO_ROOT))

# The safe module launch exposes only the protected scripts directory. Keep
# structure.py's existing scripts.reshape import working without adding the
# repository root (and its potential stdlib shadows) to the import path.
if __package__ == "reshape" and "scripts" not in sys.modules:
    spec = importlib.util.spec_from_file_location("scripts", REPO_ROOT / "scripts" / "__init__.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load the protected scripts package")
    scripts_package = importlib.util.module_from_spec(spec)
    sys.modules["scripts"] = scripts_package
    spec.loader.exec_module(scripts_package)


def normalise(error: str, root: Path = REPO_ROOT) -> str:
    first = error.splitlines()[0].replace(f"{root}/", "")
    first = re.sub(r"(\.py):\d+ ", r"\1 ", first)
    return re.sub(r": failed to extract folder executor metadata from .*", ": failed to extract folder executor metadata", first)


def current(root: Path = REPO_ROOT) -> list[str]:
    from astrid.core.structure import validate_repo_structure

    return sorted({normalise(error, root) for error in validate_repo_structure(root).errors})


def compare(found: list[str], baseline: list[str]) -> tuple[list[str], list[str]]:
    return sorted(set(found) - set(baseline)), sorted(set(baseline) - set(found))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", type=Path, default=BASELINE_PATH)
    parser.add_argument("--write-baseline", action="store_true")
    args = parser.parse_args()

    if args.write_baseline and args.baseline.resolve() == BASELINE_PATH.resolve():
        parser.error("the approved structure ceiling cannot be regenerated; remove only fixed findings")

    found = current()
    if args.write_baseline:
        args.baseline.write_text(json.dumps({"tool": "structure", "violations": found}, indent=2) + "\n")
        print(f"Wrote structure baseline to {args.baseline} ({len(found)} violations)")
        return 0

    new, fixed = compare(found, json.loads(args.baseline.read_text())["violations"])
    for line in new:
        print(f"STRUCTURE ERROR (new): {line}", file=sys.stderr)
    for line in fixed:
        print(f"STRUCTURE improvement (resolved): {line}")
    print(f"Structure violations: current={len(found)} new={len(new)} fixed={len(fixed)}")
    return 1 if new else 0


if __name__ == "__main__":
    raise SystemExit(main())
