"""Run one LS-15 proof command with retained inner status and an isolated profile.

Windows Path.home() does not follow ASTRID_RUNTIME_DATA_ROOT. Give each pytest
invocation its own user profile so the unchanged personal-pack fixture cannot
leave a directory in the following base/head invocation's home.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def main() -> int:
    source_arg, destination_arg, label, *arguments = sys.argv[1:]
    source, destination = Path(source_arg).resolve(), Path(destination_arg).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    environment = dict(os.environ, PYTHONPATH=str(source))
    if sys.platform == "win32":
        profile = destination / (label + "-profile")
        profile.mkdir()
        environment["USERPROFILE"] = str(profile)
    command = [sys.executable, "-m", "pytest", "--import-mode=importlib", "-rA",
               "--junitxml=" + str(destination / (label + ".xml")), *arguments]
    (destination / (label + "-command.json")).write_text(json.dumps({
        "cwd": str(source), "argv": command, "PYTHONPATH": environment["PYTHONPATH"],
        "USERPROFILE": environment.get("USERPROFILE"),
    }, indent=2), encoding="utf-8")
    with (destination / (label + ".log")).open("w", encoding="utf-8") as log:
        with subprocess.Popen(command, cwd=source, env=environment, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, text=True, errors="replace") as process:
            assert process.stdout is not None
            for line in process.stdout:
                log.write(line)
                log.flush()
                print(line, end="", flush=True)
        status = process.returncode
    (destination / (label + ".exit")).write_text(str(status) + "\n", encoding="utf-8")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
