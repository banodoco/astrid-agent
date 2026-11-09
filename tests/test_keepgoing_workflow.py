"""Exercise the dispatched diagnostic configuration and non-proof driver boundary."""
import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap
from unittest.mock import patch

import pytest

from scripts import ci_journey as journey
from scripts import ci_journey_keepgoing as keepgoing

REPO = Path(__file__).resolve().parents[1]
WORKFLOW = (REPO / '.github/workflows/journey.yml').read_text()


def config(tmp_path, **overrides):
    code = WORKFLOW.split('      - id: config\n', 1)[1].split("          python3 - <<'PY'\n", 1)[1].split('\n          PY\n', 1)[0]
    (tmp_path / 'driver').symlink_to(REPO, target_is_directory=True)
    output = tmp_path / 'output'
    env = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1', 'GITHUB_OUTPUT': str(output),
           'CANDIDATE': json.dumps(dict.fromkeys(('app', 'agent', 'runtime', 'pinokio'), 'a' * 40)),
           'DRIVER_AGENT_REF': 'b' * 40, 'WORKFLOW_REPOSITORY': 'banodoco/astrid-agent',
           'OLD_LAUNCHER_REF': 'bbba620e3d89eeff8021417f365e6ea584203105',
           'STOPPED_PATH_AGENT_REF': 'a' * 40, 'M4_4A_EXECUTOR_CONFIGS': '{"linux":{},"macos":{},"windows":{}}',
           'DIAGNOSTIC_KEEP_GOING': 'true', 'DIAGNOSTIC_LEG': 'both', 'DIRECT_ONLY': 'true',
           'JOURNEY_ROW': 'direct', 'M4_4A_BACKEND_ONLY': 'false', 'M4_5_ENABLED': 'false',
           'M4_5_ONLY': 'false', 'M4_5_BOUNDARY': '', 'M4_5_BOUNDARY_ARGS': '[]',
           'M4_4B_ENABLED': 'false', 'M4_6_ENABLED': 'false', 'M4_6_CANDIDATE': '', **overrides}
    result = subprocess.run([sys.executable, '-c', textwrap.dedent(code)], cwd=tmp_path, env=env,
                            capture_output=True, text=True)
    outputs = dict(line.split('=', 1) for line in output.read_text().splitlines()) if output.exists() else {}
    return result, outputs


def test_both_legs_cover_platforms_without_duplicate_windows_fresh_and_retain_refusal(tmp_path):
    result, outputs = config(tmp_path)
    assert result.returncode == 0, result.stderr
    rows = json.loads(outputs['matrix'])['include']
    assert {(row['job_id'], row['leg']) for row in rows} == {
        ('direct-linux', 'fresh-install'), ('direct-linux', 'release9-then-install'),
        ('direct-macos', 'fresh-install'), ('direct-macos', 'release9-then-install'),
        ('direct-windows', 'fresh-install'),
    }
    assert len(rows) == len({(row['job_id'], row['leg']) for row in rows}) == 5
    assert 'fail-fast: false' in WORKFLOW
    suffix = "${{ inputs.diagnostic_keep_going && format('-{0}', matrix.leg) || '' }}"
    assert 'name: journey-receipt-${{ matrix.job_id }}' + suffix in WORKFLOW
    assert 'name: journey-manager-${{ matrix.job_id }}' + suffix in WORKFLOW
    assert "$runWindowsRefusal = '${{ matrix.job_id }}' -eq 'direct-windows'" in WORKFLOW
    assert "'${{ inputs.diagnostic_keep_going }}' -eq 'true' -and '${{ matrix.leg }}' -eq 'fresh-install'" in WORKFLOW
    assert "'${{ inputs.diagnostic_keep_going }}' -ne 'true'" in WORKFLOW
    assert '$freshExit -eq 0' not in WORKFLOW
    assert '--leg windows-upgrade-refusal' in WORKFLOW
    assert 'test "$DIAGNOSTIC_KEEP_GOING" != true' in WORKFLOW


def test_strict_direct_matrix_preserves_original_leg_and_artifact_names(tmp_path):
    result, outputs = config(tmp_path, DIAGNOSTIC_KEEP_GOING='false',
                             DIAGNOSTIC_LEG='release9-then-install')
    assert result.returncode == 0, result.stderr
    rows = json.loads(outputs['matrix'])['include']
    assert {(row['job_id'], row['leg']) for row in rows} == {
        ('direct-linux', 'r0-handoff-then-update'), ('direct-macos', 'r0-handoff-then-update'),
        ('direct-windows', 'fresh-install'),
        ('direct-linux-fresh', 'fresh-install'), ('direct-macos-fresh', 'fresh-install')}
    assert len(rows) == 5
    suffix = "${{ inputs.diagnostic_keep_going && format('-{0}', matrix.leg) || '' }}"
    assert 'name: journey-receipt-${{ matrix.job_id }}' + suffix in WORKFLOW
    assert 'name: journey-manager-${{ matrix.job_id }}' + suffix in WORKFLOW


def test_windows_diagnostic_does_not_run_deferred_release0_task(tmp_path):
    result, outputs = config(tmp_path, DIAGNOSTIC_LEG='release9-then-install')
    assert result.returncode == 0, result.stderr
    rows = json.loads(outputs['matrix'])['include']
    assert {(row['job_id'], row['leg']) for row in rows} == {
        ('direct-linux', 'release9-then-install'),
        ('direct-macos', 'release9-then-install'),
        ('direct-windows', 'fresh-install'),
    }


@pytest.mark.parametrize('overrides,message', [
    ({'DIAGNOSTIC_KEEP_GOING': 'false'}, 'requires diagnostic_keep_going'),
    ({'DIRECT_ONLY': 'false', 'JOURNEY_ROW': ''}, 'requires direct_only'),
    ({'DIAGNOSTIC_LEG': 'bogus'}, 'invalid diagnostic_leg'),
])
def test_invalid_diagnostic_scope_is_rejected(tmp_path, overrides, message):
    result, _ = config(tmp_path, **overrides)
    assert result.returncode != 0
    assert message in result.stderr


@pytest.mark.parametrize('leg', ['fresh-install', 'release9-then-install'])
def test_driver_routes_both_legs_and_never_reports_success(monkeypatch, tmp_path, leg):
    seen = []
    monkeypatch.setattr(journey, 'execute', lambda args: seen.append((args.leg, args.diagnostic_keep_going)))
    args = ['--leg', leg, '--app-checkout', '.', '--run-root', str(tmp_path), '--root', str(tmp_path / 'program'),
            '--data-root', str(tmp_path / 'data'), '--job-id', 'direct-linux', '--candidate', 'candidate.json',
            '--candidate-agent-checkout', '.', '--candidate-runtime-checkout', '.', '--pinokio-checkout', '.',
            '--diagnostic-keep-going']
    if leg == 'release9-then-install':
        args += ['--release9-app-checkout', '.', '--allow-release0-to-release1']
    with patch.object(journey.sys, 'platform', 'linux'):
        assert journey.main(args) == 1
    assert seen == [(leg, True)]


def test_diagnostic_report_is_staged_and_explicitly_ineligible(tmp_path):
    args = type('Args', (), {'job_id': 'direct-linux', 'leg': 'fresh-install'})()
    evidence = tmp_path / 'run' / 'evidence'
    keepgoing.write(evidence, keepgoing.initial(evidence, args))
    journey.retain_named_evidence(evidence, 'before-cleanup')
    staged = tmp_path / 'evidence-upload' / 'before-cleanup'
    receipt = json.loads((staged / 'journey-result.json').read_text())
    assert receipt['receipt_kind'] == 'diagnostic-only'
    assert receipt['status'] == 'fail' and receipt['qualification']['eligible'] is False
    assert (staged / 'keepgoing-report.json').is_file()
    assert 'NON-PROOF' in (staged / 'keepgoing-report.md').read_text()
