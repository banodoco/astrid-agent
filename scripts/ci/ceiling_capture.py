"""A transparent GitHub Actions shell: retain the script, output and real exit.

Usage as a custom step shell: python <this file> --step <id> --shell bash {0}
No result is accepted from this journal alone. Native reporters supply test
identities, and the trusted collector validates both representations.
"""
from __future__ import annotations
import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import subprocess
import shutil
import re
import sys
import time


def git_sha(directory):
    if not Path(directory).is_dir():
        return None
    top = subprocess.run(["git", "-C", str(directory), "rev-parse", "--show-toplevel"],
                         text=True, capture_output=True, check=False)
    if top.returncode or Path(top.stdout.strip()).resolve() != Path(directory).resolve():
        return None
    result = subprocess.run(["git", "-C", str(directory), "rev-parse", "HEAD"],
                            text=True, capture_output=True, check=False)
    return result.stdout.strip() if result.returncode == 0 else None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--step", required=True)
    parser.add_argument("--matrix", default="0")
    parser.add_argument("--shell", choices=("bash", "pwsh", "powershell", "sh", "cmd", "python"), required=True)
    parser.add_argument("script", type=Path)
    args = parser.parse_args(argv)
    root = Path(os.environ["RUNNER_TEMP"]) / "failure-ceiling" / os.environ["GITHUB_JOB"]
    directory = root / args.step
    # Every step and job attempt has its own retained evidence, never overwritten.
    directory.mkdir(parents=True, exist_ok=False)
    script = args.script.read_bytes()
    (directory / "command.txt").write_bytes(script)
    executed_script = args.script
    if args.shell in {"pwsh", "powershell"}:
        # Match Actions' built-in PowerShell shell's failure semantics. A custom
        # shell does not receive these prologue/epilogue lines automatically.
        executed_script = directory / "execute.ps1"
        executed_script.write_text("$ErrorActionPreference = 'stop'\n" + script.decode("utf-8-sig")
                                  + "\nif (Test-Path -LiteralPath variable:\\LASTEXITCODE) { exit $LASTEXITCODE }\n", encoding="utf-8")
    repository = os.environ["GITHUB_REPOSITORY"]
    workspace = Path(os.environ["GITHUB_WORKSPACE"])
    product_root = Path(__file__).resolve().parents[2]
    inputs = {repository: git_sha(product_root)}
    aliases = {"astrid-agent": ("astrid-agent", "astrid-producer"),
               "astrid-runtime": ("astrid-runtime", "runtime-producer", "journey-runtime"),
               "astrid-app": ("astrid-app", "journey-app")}
    for name in aliases:
        if "banodoco/" + name == repository:
            continue
        paths = [base / alias for base in (workspace, workspace.parent, product_root) for alias in aliases[name]]
        if name == "astrid-agent" and os.environ.get("ASTRID_CHECKOUT"):
            paths.insert(0, Path(os.environ["ASTRID_CHECKOUT"]))
        for path in paths:
            if sha := git_sha(path):
                inputs["banodoco/" + name] = sha
                break
    installed_sources, conflicts = {}, []
    for distribution, product in (("astrid", "banodoco/astrid-agent"), ("astrid-runtime", "banodoco/astrid-runtime"),
                                  ("banodoco-timeline-schema", None)):
        try:
            source = json.loads(importlib.metadata.distribution(distribution).read_text("direct_url.json") or "null")
            if not source:
                continue
            installed_sources[distribution] = source
            url = source.get("url", "")
            match = re.fullmatch(r"https://github.com/(banodoco/astrid-(?:agent|runtime))/archive/([0-9a-f]{40})\.tar\.gz", url)
            sha = source.get("vcs_info", {}).get("commit_id")
            if match:
                sha = match[2]
            if product and product != repository and sha and re.fullmatch(r"[0-9a-f]{40}", sha):
                if product in inputs and inputs[product] != sha:
                    conflicts.append(product)
                else:
                    inputs[product] = sha
        except (importlib.metadata.PackageNotFoundError, ValueError, OSError):
            continue
    commands = {"bash": ["bash", "--noprofile", "--norc", "-e", "-o", "pipefail"],
                "sh": ["sh", "-e"], "pwsh": ["pwsh", "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File"],
                "powershell": ["powershell", "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File"],
                "cmd": ["cmd", "/D", "/E:ON", "/V:OFF", "/S", "/C"], "python": [sys.executable]}
    metadata = {"schema": 1, "repository": repository, "head": inputs[repository],
                "tuple": inputs, "step": args.step, "job": os.environ["GITHUB_JOB"], "matrix_index": args.matrix,
                "run_id": os.environ["GITHUB_RUN_ID"], "attempt": os.environ["GITHUB_RUN_ATTEMPT"],
                "shell": args.shell, "command_sha256": hashlib.sha256(script).hexdigest(),
                "installed_sources": installed_sources, "tuple_conflicts": conflicts,
                "started_at": time.time(), "complete": False}
    receipt = directory / "command.json"
    receipt.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    env = dict(os.environ, CEILING_STEP_DIR=str(directory))
    env["PYTHONPATH"] = str(Path(__file__).resolve().parent) + os.pathsep + env.get("PYTHONPATH", "")
    with (directory / "command.log").open("wb", buffering=0) as log:
        process = subprocess.Popen([*commands[args.shell], str(executed_script)], env=env,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        assert process.stdout is not None
        try:
            while chunk := process.stdout.read1(65536):
                log.write(chunk)
                sys.stdout.buffer.write(chunk)
                sys.stdout.buffer.flush()
            status = process.wait()
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
    metadata.update(complete=True, raw_exit=status, finished_at=time.time())
    # Container native output is mounted through the pre-existing results
    # directory. Copy it after the real process exits, including on failure.
    if results := os.environ.get("TEST_RESULTS_DIR"):
        for source in (Path(results) / "ceiling").glob("pytest-*.jsonl"):
            target = directory / source.name
            if not target.exists():
                shutil.copyfile(source, target)
    receipt.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    # Existing XML/JSON/reporters stay where their owners put them. They are
    # uploaded by their existing steps; this journal adds no success override.
    return status if status >= 0 else 128 - status


if __name__ == "__main__":
    raise SystemExit(main())
