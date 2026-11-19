"""Publish only immutable source IDs, sanitized observation, and original outcome metadata."""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


def integer_file(path):
    if not path.is_file():
        return None
    value = path.read_text().strip()
    return int(value) if re.fullmatch(r"-?\d{1,6}", value) else None


def git_identity(path):
    if not (path / ".git").exists():
        return None
    result = subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"], capture_output=True)
    value = result.stdout.decode("ascii", errors="ignore").strip()
    return value if result.returncode == 0 and re.fullmatch(r"[a-f0-9]{40}", value) else None


def main():
    raw, public = map(Path, sys.argv[1:])
    public.mkdir(exist_ok=False)
    side = os.environ["LS15_SIDE"]
    expected = os.environ["LS15_AGENT_SHA"]
    full_id = ("tests/integrations/test_runpod_slot_path_cpu.py::"
               "test_capability_reaches_generation_on_each_target[audio/music-local-managed]")
    locations = {
        "agent": ".", "runtime": "astrid-runtime", "launcher": "astrid-pinokio",
        "vibecomfy": "vibecomfy-checkout", "lifecycle": "runpod-lifecycle-checkout",
        "comfyui": "comfyui-checkout",
    }
    sources = {key: git_identity(Path(path)) for key, path in locations.items()}
    pytest_exit = integer_file(raw / "pytest.exit")
    launcher_exit = integer_file(raw / "launcher.exit")
    summary = {
        "side": side, "expected_agent": expected, "proof_workflow_sha": os.environ["GITHUB_SHA"],
        "source_checkouts": sources, "selector": full_id, "pytest_exit": pytest_exit,
        "launcher_exit": launcher_exit, "phases": [], "valid_observation": False,
        "required_boundaries_observed": False, "first_queue_failure_call": None,
        "guarded_launcher_inputs_recorded": (raw / "inputs.json").is_file(),
    }
    observation = raw / "observation.json"
    if observation.is_file() and observation.stat().st_size <= 1024 * 1024:
        try:
            record = json.loads(observation.read_bytes())
        except (ValueError, OSError):
            record = {}
        if (isinstance(record, dict) and record.get("head") == expected and record.get("side") == side
                and record.get("selector") == full_id):
            # Entire file is produced by the isolated proof module's fixed-schema encoder.
            shutil.copyfile(observation, public / "observation.json")
            summary.update(phases=record["pytest_phases"], valid_observation=record["observation_valid"],
                           required_boundaries_observed=record["required_boundaries_observed"],
                           first_queue_failure_call=record["first_queue_failure_call"])
    junit = raw / "junit.xml"
    summary["junit_result"] = None
    if junit.is_file():
        try:
            cases = ET.parse(junit).findall(".//testcase")
        except (ET.ParseError, OSError):
            cases = []
        expected_class = "tests.integrations.test_runpod_slot_path_cpu"
        if (len(cases) == 1 and cases[0].get("classname") == expected_class
                and cases[0].get("name") == full_id.split("::")[1]):
            states = [node.tag for node in cases[0] if node.tag in {"error", "failure", "skipped"}]
            summary["junit_result"] = states or ["passed"]
    # Raw JUnit, basetemp, endpoint state, and console are never published.
    for name in ("junit.xml", "pytest.log"):
        path = raw / name
        summary[name + "_sha256"] = None
        if path.is_file():
            with path.open("rb") as stream:
                summary[name + "_sha256"] = hashlib.file_digest(stream, "sha256").hexdigest()
    (public / "receipt.json").write_text(json.dumps(summary, indent=2) + "\n")
    return 0 if (summary["valid_observation"] and summary["required_boundaries_observed"]
                 and summary["junit_result"] is not None and sources["agent"] == expected and pytest_exit is not None
                 and launcher_exit == (pytest_exit if pytest_exit >= 0 else 128 - pytest_exit)) else 1


if __name__ == "__main__":
    sys.exit(main())
