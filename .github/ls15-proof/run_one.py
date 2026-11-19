"""Prepared once-only diagnostic launcher; no dispatch, retry, or source edit.

Run from an unchanged Agent checkout after the existing hosted integration setup:
    python /external/observation/run_one.py base /external/fresh-receipt-dir
or candidate with a different fresh receipt directory. No execution on import.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

PINS = {
    "ASTRID_RUNTIME_CHECKOUT": "62333bd797e84b21cb3995e4bcf580ed0bdcf9f0",
    "ASTRID_VIBECOMFY_CHECKOUT": "1ba0ac3ad4fe69fa0b4966cc41e6852224013f97",
    "ASTRID_TEST_LIFECYCLE_CHECKOUT": "7f0b025e9391baac9f5c2fb7393617ea6fef0704",
    "ASTRID_TEST_COMFY_SOURCE": "6bfaacc67c2103481e5f0c84d75257cd0581d86a",
    "ASTRID_PINOKIO_CHECKOUT": "d15c58b5cd1f7e24c1e699340b33961027f7c573",
}


def identity(root):
    return {
        "head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip(),
        "tracked_clean": subprocess.run(["git", "diff", "--quiet", "HEAD", "--"],
                                        cwd=root, check=False).returncode == 0,
        "status_clean": not subprocess.check_output(["git", "status", "--porcelain"],
                                                    cwd=root, text=True).strip(),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("side", choices=("base", "candidate"))
    parser.add_argument("receipt_directory", type=Path)
    args = parser.parse_args()
    root = Path.cwd().resolve()
    folder = Path(__file__).resolve().parent
    destination = args.receipt_directory.resolve()
    if not sys.platform.startswith("linux") or sys.version_info[:2] != (3, 11):
        parser.error("requires the original Linux / Python 3.11 hosted integration setup")
    if destination.is_relative_to(root) or destination.exists() or not destination.parent.is_dir():
        parser.error("receipt directory must be a new external directory under an existing parent")
    if os.environ.get("ASTRID_CI_JOB") != "lanes-integration":
        parser.error("requires the original lanes-integration environment")
    module_spec = importlib.util.spec_from_file_location("ls15_observer_inputs", folder / "ls15_queue_observer.py")
    observer = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(observer)
    agent = identity(root)
    if agent["head"] != observer.HEADS[args.side] or not agent["tracked_clean"]:
        parser.error("Agent head or tracked tree differs from the frozen pair")
    sources = {}
    for variable, expected in PINS.items():
        location = os.environ.get(variable)
        if not location:
            parser.error(f"missing original setup variable: {variable}")
        sources[variable] = identity(Path(location))
        if sources[variable]["head"] != expected or not sources[variable]["status_clean"]:
            parser.error(f"source identity/cleanliness mismatch: {variable}")
    destination.mkdir()
    pytest_args = [
        "-v", "-rA", "--durations=40",
        "-m", "integration and not opt_in and not app_checkout", observer.SELECTOR,
        "--basetemp", str(destination / "basetemp"),
        "--junitxml", str(destination / "junit.xml"),
        "--ls15-observe-side", args.side,
        "--ls15-observe-output", str(destination / "observation.json"),
    ]
    # A fresh -c interpreter retains the original cwd-based import/child path.
    # The external script directory is never added to its sys.path or PYTHONPATH.
    code = "import runpy; runpy.run_path(" + repr(str(folder / "pytest_entrypoint.py")) + ", run_name='__main__')"
    command = [sys.executable, "-c", code, *pytest_args]
    (destination / "inputs.json").write_text(json.dumps({
        "kind": "one original test plus external observation; not full qualification",
        "side": args.side, "agent": agent, "sources": sources,
        "python": list(sys.version_info[:3]), "pytest_args": pytest_args,
        "no_environment_override": True, "no_retry": True,
    }, indent=2) + "\n")
    with (destination / "pytest.log").open("xb") as log:
        result = subprocess.run(command, cwd=root, stdout=log, stderr=subprocess.STDOUT, check=False)
    (destination / "pytest.exit").write_text(str(result.returncode) + "\n")
    # Retain a negative subprocess signal return verbatim; expose its conventional
    # 128+signal shell status instead of Python's unrelated modulo-256 exit.
    shell_status = result.returncode if result.returncode >= 0 else 128 - result.returncode
    (destination / "launcher.exit").write_text(str(shell_status) + "\n")
    return shell_status


if __name__ == "__main__":
    raise SystemExit(main())
