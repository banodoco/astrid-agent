"""Run the declared pytest command with a census and crash-surviving evidence."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from .check_results import validate


def read_manifest(path, declaration):
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("invocation") != declaration:
        raise ValueError("collection manifest differs from the parent's declared invocation")
    return manifest


def junit_argument(args):
    result = []
    remaining = []
    arguments = iter(args)
    for argument in arguments:
        if argument in ("--junitxml", "--junit-xml"):
            result.append(next(arguments))
        elif argument.startswith(("--junitxml=", "--junit-xml=")):
            result.append(argument.split("=", 1)[1])
        else:
            remaining.append(argument)
    if len(result) != 1:
        raise ValueError("one --junitxml destination is required")
    return Path(result[0]).resolve(), remaining


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    xml, selection_args = junit_argument(args)
    xml.parent.mkdir(parents=True, exist_ok=True)
    reference = xml.with_suffix(".reference.json")
    manifest = xml.with_suffix(".collection.json")
    events = xml.with_suffix(".jsonl")
    log = xml.with_suffix(".log")
    reference_log = xml.with_suffix(".reference.log")
    # Results from an earlier invocation must never satisfy this invocation.
    for path in (xml, reference, manifest, events, log, reference_log):
        if path.exists():
            raise ValueError(f"refusing to overwrite existing test evidence: {path}")
    environment = dict(os.environ)
    declaration = {"args": selection_args, "cwd": str(Path.cwd()),
                   "addopts": environment.get("PYTEST_ADDOPTS", "")}
    entrypoint = Path(__file__).with_name("pytest_entrypoint.py").resolve()
    command = [sys.executable, "-I", "-u", str(entrypoint)]
    plugin = ["-p", "scripts.ci.collection_guard"]
    census_args = [arg for arg in selection_args if arg not in ("--store-durations", "--clean-durations")]
    census_args = [*plugin, *census_args, "--collect-only", "--collection-manifest", str(reference)]
    run_args = [*plugin, *args, "-rA", "--collection-manifest", str(manifest),
                "--collection-reference", str(reference), "--test-event-log", str(events)]

    def child_environment(arguments):
        return {**environment, "ASTRID_PYTEST_INVOCATION": json.dumps(
            {"declared": declaration, "child_args": arguments})}

    with reference_log.open("w", encoding="utf-8") as output:
        census = subprocess.run(
            [*command, *census_args], env=child_environment(census_args),
            stdout=output, stderr=subprocess.STDOUT, text=True, check=False,
        )
    if census.returncode != 0 or not reference.is_file():
        print(reference_log.read_text(encoding="utf-8"))
        print(f"Collection reference failed (exit {census.returncode}); no tests were executed.")
        return census.returncode or 1
    try:
        reference_manifest = read_manifest(reference, declaration)
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        print(f"ERROR: invalid declared collection reference: {exc}")
        return 1
    with log.open("w", encoding="utf-8", buffering=1) as output:
        with subprocess.Popen(
            [*command, *run_args], env=child_environment(run_args),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            encoding="utf-8", errors="replace", bufsize=1,
        ) as process:
            assert process.stdout is not None
            for line in process.stdout:
                output.write(line)
                output.flush()
                print(line, end="", flush=True)
            status = process.wait()
    try:
        if read_manifest(manifest, declaration) != reference_manifest:
            raise ValueError("collection manifest differs from the parent's retained reference")
        result = validate(xml)
        print(f"Verified test evidence: {result}")
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        print(f"ERROR: {exc}")
        return status if status > 0 else 1
    return status if status >= 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
