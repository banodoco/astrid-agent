"""Run the Plan 55 D9 affected-file proof on each exact checkout, retaining failures."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET

BASE_FILES = [
    "tests/core/generation/backends/test_comfy_target.py",
    "tests/core/generation/test_output_delivery.py",
]
ADDED_FILES = [
    "tests/core/generation/backends/test_comfy_cancel_safety.py",
    "tests/core/generation/test_local_speed_hash_observer.py",
]


def run_side(side: str, source: Path, runtime: Path, output: Path) -> int:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(map(str, (source, runtime, runtime / "packages/python")))
    env["ASTRID_RUNTIME_CHECKOUT"] = str(runtime)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTEST_ADDOPTS"] = "-rA"
    check = [sys.executable, "-c", "import astrid, pathlib, sys; "
             "assert pathlib.Path(astrid.__file__).resolve().is_relative_to(pathlib.Path(sys.argv[1]).resolve()), astrid.__file__; "
             "print(astrid.__file__)", str(source)]
    with (output / f"{side}-source.log").open("wb") as log:
        source_rc = subprocess.run(check, cwd=source, env=env, stdout=log,
                                   stderr=subprocess.STDOUT, timeout=60).returncode
    if source_rc:
        (output / f"{side}.exit").write_text(str(source_rc) + "\n")
        return source_rc
    files = BASE_FILES + (ADDED_FILES if side == "candidate" else [])
    command = [sys.executable, "-m", "pytest", "-q", "-rA", *files,
               f"--junitxml={output / (side + '.xml')}"]
    (output / f"{side}-command.json").write_text(json.dumps({
        "command": command, "cwd": str(source), "runtime": str(runtime),
        "selected_files": files, "environment_keys_set": [
            "PYTHONPATH", "ASTRID_RUNTIME_CHECKOUT", "PYTHONDONTWRITEBYTECODE", "PYTEST_ADDOPTS"],
    }, indent=2) + "\n")
    with (output / f"{side}.log").open("wb") as log:
        try:
            result = subprocess.run(command, cwd=source, env=env, stdout=log,
                                    stderr=subprocess.STDOUT, timeout=1200)
            code = result.returncode
        except subprocess.TimeoutExpired:
            log.write(b"\nPLAN55_SCOPE_TIMEOUT: no completed proof\n")
            code = 124
    (output / f"{side}.exit").write_text(str(code) + "\n")
    return code


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, required=True)
    args = parser.parse_args()
    workspace = args.workspace.resolve()
    output = workspace / "plan55-scoped-proof"
    output.mkdir(exist_ok=True)
    statuses = {}
    for side in ("baseline", "candidate"):
        statuses[side] = run_side(side, workspace / side, workspace / "runtime", output)
    counts = {}
    for side in statuses:
        path = output / f"{side}.xml"
        if path.is_file():
            root = ET.parse(path).getroot()
            suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
            counts[side] = {name: sum(int(s.get(name, "0")) for s in suites)
                            for name in ("tests", "failures", "errors", "skipped")}
    (output / "summary.json").write_text(json.dumps({
        "exit_codes": statuses, "junit_counts": counts,
        "scope": "affected files, not the Agent meter, GPU proof, or independent review",
    }, indent=2) + "\n")
    print(json.dumps({"exit_codes": statuses, "junit_counts": counts}, indent=2))
    return 0 if all(code == 0 for code in statuses.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
