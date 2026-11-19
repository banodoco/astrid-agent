"""Proposed Lead-owned diagnostic: one unchanged audio test per immutable head.

Run only on an isolated hosted macOS checkout after the existing CI Darwin
provisioning recipe. The checkout must already be detached. This script does
not dispatch, cancel, push, change a branch ref, alter tests, or set a timeout.
It is correctness evidence, never D1 or owned-phase qualification.
"""
from pathlib import Path
import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import shutil
import subprocess
import sys
import time


REFERENCE = "e379565641414bc183cd02edebab1cbf77617931"
CANDIDATE = "aacdcd86bfe09bcddac0f4d5941498c5126f74d8"
RUNTIME = "62333bd797e84b21cb3995e4bcf580ed0bdcf9f0"
VIBE = "1ba0ac3ad4fe69fa0b4966cc41e6852224013f97"
TEST = "tests/packs/test_audio_render.py::AudioRenderTest::test_rendered_mp4_contains_audio_stream"
SELECTOR = "darwin_native or remotion or chrome or codex_cli"


def capture(command, *, cwd=None, env=None):
    result = subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True)
    return {"command": list(map(str, command)), "exit_code": result.returncode,
            "stdout": result.stdout, "stderr": result.stderr}


def checked(command, *, cwd=None):
    result = capture(command, cwd=cwd)
    if result["exit_code"]:
        raise RuntimeError(json.dumps(result))
    return result["stdout"].strip()


def packages():
    return sorted((dist.metadata["Name"], dist.version) for dist in importlib.metadata.distributions())


def write(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkout", type=Path, required=True)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--vibe", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    repo, output = args.checkout.resolve(), args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    receipt = {"diagnostic_only": True, "executed": False, "test": TEST,
               "reference": REFERENCE, "candidate": CANDIDATE, "cells": []}
    write(output / "pair.json", receipt)
    if platform.system() != "Darwin":
        raise SystemExit("NO-RESULT: actual macOS is required; no test was executed")
    if capture(["git", "symbolic-ref", "--quiet", "HEAD"], cwd=repo)["exit_code"] == 0:
        raise SystemExit("NO-RESULT: use a disposable detached checkout, never a packet branch")
    checked(["git", "diff", "--exit-code"], cwd=repo)
    checked(["git", "diff", "--cached", "--exit-code"], cwd=repo)
    for path, expected in ((args.runtime, RUNTIME), (args.vibe, VIBE)):
        if checked(["git", "rev-parse", "HEAD"], cwd=path) != expected:
            raise SystemExit("NO-RESULT: dependency checkout differs from the frozen pair")
        checked(["git", "diff", "--exit-code"], cwd=path)
    if checked(["node", "--version"]) != "v20.19.4":
        raise SystemExit("NO-RESULT: Node differs from both heads' existing .node-version")
    common_paths = [".github/workflows/ci.yml", "pyproject.toml", "runtime.lock.json", ".node-version",
                    "remotion/package.json", "remotion/package-lock.json", "tests/packs/test_audio_render.py",
                    "tests/packs/rendering/_helpers.py", "tests/conftest.py", "tests/lanes.py"]
    receipt["equal_source_blobs"] = {}
    for path in common_paths:
        blobs = [checked(["git", "rev-parse", head + ":" + path], cwd=repo)
                 for head in (REFERENCE, CANDIDATE)]
        if blobs[0] != blobs[1]:
            raise SystemExit("NO-RESULT: source setup differs for " + path)
        receipt["equal_source_blobs"][path] = blobs[0]
    receipt["platform"] = {"python": sys.version, "executable": sys.executable,
                            "machine": platform.machine(), "sw_vers": capture(["sw_vers"]),
                            "hardware": capture(["sysctl", "hw.model", "hw.ncpu", "hw.memsize"]),
                            "runner_image": os.environ.get("ImageVersion"),
                            "runner_os": os.environ.get("RUNNER_OS")}
    receipt["binaries"] = {}
    for name in ("node", "ffmpeg", "ffprobe"):
        path = Path(shutil.which(name) or "").resolve(strict=True)
        receipt["binaries"][name] = {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                                      "version": capture([name, "--version" if name == "node" else "-version"])}
    initial_packages = None
    for label, head in (("reference", REFERENCE), ("candidate", CANDIDATE)):
        folder = output / label
        folder.mkdir()
        checkout = capture(["git", "checkout", "--detach", head], cwd=repo)
        write(folder / "checkout.json", checkout)
        if checkout["exit_code"] or checked(["git", "rev-parse", "HEAD"], cwd=repo) != head:
            raise SystemExit("NO-RESULT: immutable checkout failed")
        # Same path and same environment; no dependency resolution between heads.
        install = capture([sys.executable, "-m", "pip", "install", "--no-deps", "--no-build-isolation", "-e", "."], cwd=repo)
        write(folder / "editable-install.json", install)
        if install["exit_code"]:
            raise SystemExit("NO-RESULT: editable installation failed")
        observed_packages = packages()
        write(folder / "packages.json", observed_packages)
        if initial_packages is not None and observed_packages != initial_packages:
            raise SystemExit("NO-RESULT: resolved package versions changed between heads")
        initial_packages = observed_packages
        env = dict(os.environ)
        env.update(ASTRID_CI_JOB="darwin-native", ASTRID_RUNTIME_CHECKOUT=str(args.runtime.resolve()),
                   ASTRID_VIBECOMFY_CHECKOUT=str(args.vibe.resolve()), PYTEST_ADDOPTS="-rA",
                   PYTHONDONTWRITEBYTECODE="1", PYTHONPYCACHEPREFIX=str(folder / "fresh-pycache"))
        origins = capture([sys.executable, "-c",
            "import importlib.util,json; print(json.dumps({name:importlib.util.find_spec(name).origin "
            "for name in ('astrid','runtime_protocol','vibecomfy')}))"], cwd=repo, env=env)
        write(folder / "import-origins.json", origins)
        if origins["exit_code"]:
            raise SystemExit("NO-RESULT: dependency origins could not be established")
        resolved = json.loads(origins["stdout"])
        for name, owner in (("astrid", repo), ("runtime_protocol", args.runtime.resolve()),
                            ("vibecomfy", args.vibe.resolve())):
            if not Path(resolved[name]).resolve().is_relative_to(owner):
                raise SystemExit("NO-RESULT: wrong imported source for " + name)
        command = [sys.executable, "-m", "pytest", "-q", "-rA", "-m", SELECTOR, TEST,
                   "--junitxml=" + str(folder / "result.xml"), "--basetemp=" + str(folder / "pytest")]
        row = {"label": label, "head": head, "command": command,
               "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
               "processes_before": capture(["ps", "-axo", "pid,ppid,lstart,comm"])}
        with (folder / "console.log").open("wb") as log:
            result = subprocess.run(command, cwd=repo, env=env, stdout=log, stderr=subprocess.STDOUT)
        row.update(exit_code=result.returncode,
                   finished_utc=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                   processes_after=capture(["ps", "-axo", "pid,ppid,lstart,comm"]),
                   tracked_diff=checked(["git", "diff", "--exit-code"], cwd=repo))
        write(folder / "receipt.json", row)
        receipt["cells"].append(row)
        receipt["executed"] = True
        write(output / "pair.json", receipt)
    receipt["complete"] = True
    receipt["matched_packages"] = True
    write(output / "pair.json", receipt)
    checksums = {str(path.relative_to(output)): hashlib.sha256(path.read_bytes()).hexdigest()
                 for path in sorted(output.rglob("*")) if path.is_file()}
    write(output / "sha256.json", checksums)
    return int(any(row["exit_code"] for row in receipt["cells"]))


if __name__ == "__main__":
    raise SystemExit(main())
