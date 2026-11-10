"""Keep hosted journey uploads inside the collector's named evidence boundary."""
from pathlib import Path
import re


REPO = Path(__file__).resolve().parents[1]
WORKFLOW = REPO / ".github" / "workflows" / "journey.yml"
COLLECTOR = REPO / "scripts" / "ci_journey.py"


def test_journey_concurrency_isolates_manual_dispatches_and_preserves_other_grouping():
    workflow_text = WORKFLOW.read_text(encoding="utf-8")
    group = (
        "group: reusable-journey-${{ github.event_name == 'workflow_dispatch' "
        "&& github.run_id || github.event.pull_request.number || github.ref }}"
    )
    assert group in workflow_text


def test_journey_upload_is_allowlisted_staged_evidence_under_size_limit():
    workflow_text = WORKFLOW.read_text(encoding="utf-8")
    guard_start = workflow_text.index("      - name: Validate the named lifecycle evidence artifact boundary")
    upload_start = workflow_text.index("      - name: Retain only the named lifecycle evidence", guard_start)
    release2_start = workflow_text.index("  release2:", upload_start)
    guard = workflow_text[guard_start:upload_start]
    upload = workflow_text[upload_start:release2_start]

    assert "id: evidence_boundary" in guard
    assert "if: always()" in guard
    assert "if: always() && steps.evidence_boundary.outcome == 'success'" in upload
    assert "uses: actions/upload-artifact@v4" in upload
    upload_path = re.search(r"^\s+path: (.+)$", upload, re.M).group(1)
    assert upload_path.endswith("/evidence-upload")
    assert upload_path != "${{ runner.temp }}/astrid-r1-${{ matrix.job_id }}"
    assert "compression-level: 0" in upload

    max_bytes = int(re.search(r"\$maxArtifactBytes\s*=\s*(\d+)", guard).group(1))
    assert max_bytes < 50_000_000
    assert re.search(r"\$estimatedArtifactBytes\s+-ge\s+\$maxArtifactBytes", guard)
    assert "[long]$file.Length + 1024" in guard

    names_block = guard.split("$allowedNames = @(", 1)[1].split("\n          )", 1)[0]
    staged_names = set(re.findall(r"'([^']+)'", names_block))
    assert "release0-bootstrap.log" in staged_names
    assert "live-seed-failure.json" in staged_names
    assert "live-snapshot-failure.json" in staged_names
    assert "live-release1-snapshot-failure.json" in staged_names
    assert "windows-schema-acl.json" in staged_names
    collector_block = COLLECTOR.read_text(encoding="utf-8").split("JOURNEY_EVIDENCE_NAMES = frozenset({", 1)[1].split("})", 1)[0]
    collector_names = set(re.findall(r'"([^\"]+)"', collector_block))
    assert {"runtime-owner-selection.json", "runtime-owner-selection-context.json"} <= collector_names
    bootstrap_captures = {
        "install-bootstrap-process.json", "install-bootstrap-stdout.log", "install-bootstrap-stderr.log",
        "update-bootstrap-process.json", "update-bootstrap-stdout.log", "update-bootstrap-stderr.log",
    }
    assert bootstrap_captures <= collector_names
    assert bootstrap_captures <= staged_names
    missing_guard_names = collector_names - staged_names
    assert not missing_guard_names, f"collector evidence rejected by workflow guard: {sorted(missing_guard_names)}"
    assert "^runtime-service-service-.+\\.log$" in guard
    assert "Unexpected staged evidence path" in guard
    assert "Unexpected staged evidence filename" in guard
    assert "No staged journey-result.json receipt" in guard
    assert "Get-ChildItem -LiteralPath $evidenceRoot -File -Recurse -Force" in guard
    assert "-ge $maxArtifactBytes" in guard


def test_sdk_capture_upload_requires_closed_receipts():
    text = WORKFLOW.read_text(encoding="utf-8")
    guard = text.split("      - name: Validate the named lifecycle evidence artifact boundary", 1)[1].split(
        "      - name: Retain only the named lifecycle evidence", 1)[0]
    assert "'project-create-capture-status.json', 'sdk-capture-status.json'" in guard
    for required in ("$file.Length -gt 1024", "ConvertFrom-Json", "$keys.Count -ne $expectedKeys.Count",
                     "$keys -cnotcontains $key", "Invalid SDK capture values", "-isnot [bool]"):
        assert required in guard


def test_live_snapshot_failure_upload_uses_producer_schema_and_size_limit():
    text = WORKFLOW.read_text(encoding="utf-8")
    guard = text.split("      - name: Validate the named lifecycle evidence artifact boundary", 1)[1].split(
        "      - name: Retain only the named lifecycle evidence", 1)[0]
    assert "'live-seed-failure.json', 'live-snapshot-failure.json', 'live-release1-snapshot-failure.json'" in guard
    assert "$name -in @('live-seed-failure.json', 'live-snapshot-failure.json', 'live-release1-snapshot-failure.json')" in guard
    assert "'project_id', 'timeline_id', 'cas_sha256', 'head_revision_id'" in guard
    for required in ("$file.Length -gt 4096", "astrid.live-workspace-failure.v1",
                     "astrid.live-workspace-failure.v2", "astrid.live-workspace-failure.v3",
                     "snapshot_lookup", "timeline_head_revision_id", "keys_revision",
                     "$allowedSnapshotFields -cnotcontains",
                     "Invalid live workspace failure fields", "Invalid live workspace failure frame",
                     "$allowedRuntimeCodes -cnotcontains $diagnostic.runtime_code",
                     "$allowedFunctions -cnotcontains $frame.function"):
        assert required in guard

    # ConvertFrom-Json can represent integral JSON values as Int64 (PowerShell
    # [long]); accept that alongside Int32 while still rejecting nonintegers.
    assert "($diagnostic.http_status -isnot [int] -and $diagnostic.http_status -isnot [long])" in guard
    assert "($frame.line -isnot [int] -and $frame.line -isnot [long])" in guard


def _linux_manager_script():
    import yaml
    workflow = yaml.safe_load((Path(__file__).resolve().parents[1] / ".github/workflows/journey.yml").read_text())
    step = next(s for s in workflow["jobs"]["journey"]["steps"] if s.get("name") == "Establish Linux user service manager")
    return step["run"]


def test_linux_manager_gets_the_installs_xdg_config_and_the_probe_checks_it():
    import subprocess
    script = _linux_manager_script()
    subprocess.run(["bash", "-n"], input=script, text=True, check=True)
    assert script.index('set-environment "HOME=$run_root/home" "XDG_CONFIG_HOME=$config" "XDG_DATA_HOME=$data"') < script.index("\nprobe_isolated_unit\n")
    assert 'if [[ "$manager_config/systemd/user" != "$probe_dir" ]]' in script
    assert 'probe_dir="$config/systemd/user"' in script


def _run_manager_assertion(tmp_path, environment):
    """Run the loud-failure block against a fake `systemctl` that reports `environment`."""
    import subprocess
    script = _linux_manager_script()
    start = script.index("activation=$(")
    end = script.index("probe_isolated_unit\nprintf 'XDG_RUNTIME_DIR")
    block = script[start:end]
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    fake = bin_dir / "systemctl"
    fake.write_text("#!/bin/bash\nif [[ \"$2\" == show-environment ]]; then cat \"$FAKE_ENV\"; fi\n")
    fake.chmod(0o755)
    env_file = tmp_path / "env"
    env_file.write_text(environment)
    program = f'run_root=/run; config=/run/home/.config; data=/run/home/.local/share\n{block}\necho ok\n'
    return subprocess.run(["bash", "-c", program], capture_output=True, text=True,
                          env={"PATH": f"{bin_dir}:/usr/bin:/bin", "FAKE_ENV": str(env_file)})


def test_manager_environment_assertion_fails_loudly_when_xdg_is_missing_or_different(tmp_path):
    good = "HOME=/run/home\nXDG_CONFIG_HOME=/run/home/.config\nXDG_DATA_HOME=/run/home/.local/share\nPATH=/usr/bin\n"
    ok = _run_manager_assertion(tmp_path, good)
    assert ok.returncode == 0 and ok.stdout.strip() == "ok"
    missing = _run_manager_assertion(tmp_path, "XDG_CONFIG_HOME=/run/home/.config\nXDG_DATA_HOME=/run/home/.local/share\n")
    assert missing.returncode == 1 and "has no HOME" in missing.stdout
    missing_xdg = _run_manager_assertion(tmp_path, "HOME=/run/home\nXDG_DATA_HOME=/run/home/.local/share\n")
    assert missing_xdg.returncode == 1 and "has no XDG_CONFIG_HOME" in missing_xdg.stdout
    different = _run_manager_assertion(tmp_path, good.replace("/run/home/.config", "/home/runner/.config"))
    assert different.returncode == 1 and "XDG_CONFIG_HOME is /home/runner/.config, not /run/home/.config" in different.stdout
