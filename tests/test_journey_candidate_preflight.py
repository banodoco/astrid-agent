"""A mistyped candidate must fail at the manifest before checkout."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


def test_manifest_preflight_rejects_the_observed_one_digit_error(tmp_path):
    app = tmp_path / "app"
    public = app / "public"
    public.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(app)], check=True)
    subprocess.run(["git", "-C", str(app), "-c", "user.name=Test", "-c", "user.email=test@example.com",
                    "commit", "--allow-empty", "-qm", "test app"], check=True)
    app_sha = subprocess.check_output(["git", "-C", str(app), "rev-parse", "HEAD"], text=True).strip()
    agent = "dd6b48192c720c5346e6f140184e313b2cf84f31"
    runtime = "1e6d899efa77a2d6ec247c38250f9748330bb587"
    manifest = public / "install-manifest.json"
    manifest.write_text(json.dumps({"agent": {"sha": agent}, "runtime": {"sha": runtime}}))
    artifacts = {}
    for name in ("install", "install.sh", "install.ps1"):
        path = public / name
        path.write_text(name)
        artifacts[f"public/{name}"] = hashlib.sha256(path.read_bytes()).hexdigest()
    candidate = {"app": app_sha, "agent": agent, "runtime": runtime,
                 "pinokio": "2f085c5fc0cde192ac4a022065883c85dcccd0de",
                 "manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(), "artifacts": artifacts}
    path = tmp_path / "candidate.json"

    def run(value):
        path.write_text(json.dumps(value))
        return subprocess.run([sys.executable, str(ROOT / "scripts/check_journey_evidence.py"),
                               "--candidate", str(path), "--app-checkout", str(app), "--prepare-only"],
                              capture_output=True, text=True)

    valid = run(candidate)
    assert valid.returncode == 0, valid.stdout + valid.stderr
    candidate["agent"] = "dd6b48192c720c5346e6e140184e313b2cf84f31"
    invalid = run(candidate)
    assert invalid.returncode != 0
    assert "manifest agent differs from frozen candidate" in invalid.stdout + invalid.stderr


def test_matrix_checks_manifest_before_fetching_candidate_and_keeps_harness_separate():
    workflow = (ROOT / ".github/workflows/journey.yml").read_text()
    journey = workflow.split("  journey:\n", 1)[1].split("  release2:\n", 1)[0]
    assert journey.index("Check out reviewed Agent receipt harness") < journey.index("Check out frozen App candidate")
    assert journey.index("Check out frozen App candidate") < journey.index("Freeze and validate candidate inputs")
    assert journey.index("Freeze and validate candidate inputs") < journey.index("Check out frozen Agent candidate")
    assert journey.index("actions/setup-python@v5") < journey.index("Freeze and validate candidate inputs")
    harness = journey.split("- name: Check out reviewed Agent receipt harness", 1)[1].split("- name:", 1)[0]
    agent = journey.split("- name: Check out frozen Agent candidate", 1)[1].split("- name:", 1)[0]
    assert "ref: ${{ needs.config.outputs.driver_agent_ref }}" in harness
    assert "ref: ${{ fromJSON(needs.config.outputs.candidate).agent }}" in agent
    assert "fetch-depth: 0" in agent
