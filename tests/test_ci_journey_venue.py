"""Exercise result transport against a real local git remote."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

from scripts import ci_journey_venue as venue


def test_run_records_inner_failure_and_log(tmp_path, monkeypatch):
    monkeypatch.setenv('RUNNER_TEMP', str(tmp_path))
    assert venue.run_row([sys.executable, '-c', 'print("row output"); raise SystemExit(7)']) == 7
    assert (tmp_path / 'journey-venue/inner-exit.txt').read_text().strip() == '7'
    assert 'row output' in (tmp_path / 'journey-venue/journey.log').read_text()


def test_independent_rows_publish_and_hash_failures(tmp_path, monkeypatch):
    remote = tmp_path / 'remote.git'
    subprocess.run(['git', 'init', '--bare', '-q', str(remote)], check=True)
    real_git = venue.git

    def local_git(args, cwd, env, check=True):
        if args[:3] == ['remote', 'add', 'origin']:
            args = ['remote', 'add', 'origin', str(remote)]
        return real_git(args, cwd, env, check)

    monkeypatch.setattr(venue, 'git', local_git)
    monkeypatch.chdir(tmp_path)
    for key, value in {
        'GITHUB_TOKEN': 'dummy', 'GITHUB_REPOSITORY': 'example/repo', 'GITHUB_RUN_ID': '42',
        'GITHUB_RUN_ATTEMPT': '1', 'GITHUB_REF': 'refs/heads/venue/M-QUAL-R-05-1',
        'GITHUB_SHA': 'head', 'GITHUB_WORKFLOW_REF': 'workflow', 'GITHUB_WORKFLOW_SHA': 'head',
        'ROW_RUNNER': 'runner', 'RUNNER_OS': 'Linux', 'JOURNEY_OUTCOME': 'failure',
        'EVIDENCE_OUTCOME': 'failure', 'EVIDENCE_OVERSIZE': '',
    }.items():
        monkeypatch.setenv(key, value)
    for row in ('direct-linux', 'direct-macos', 'direct-windows'):
        root = tmp_path / row
        (root / 'journey-venue').mkdir(parents=True)
        (root / 'journey-venue/inner-exit.txt').write_text('7\n')
        (root / 'journey-venue/journey.log').write_text(row)
        monkeypatch.setenv('RUNNER_TEMP', str(root))
        monkeypatch.setenv('ROW_ID', row)
        venue.publish()
    listing = subprocess.check_output(['git', '--git-dir', str(remote), 'ls-tree', '-r', '--name-only', venue.RESULT_REF], text=True)
    for row in ('direct-linux', 'direct-macos', 'direct-windows'):
        prefix = f'runs/42-1/{row}/'
        assert prefix + 'receipt.json' in listing
        receipt = json.loads(subprocess.check_output(['git', '--git-dir', str(remote), 'show', venue.RESULT_REF + ':' + prefix + 'receipt.json']))
        assert receipt['inner_exit'] == 7 and receipt['row_id'] == row
        sums = subprocess.check_output(['git', '--git-dir', str(remote), 'show', venue.RESULT_REF + ':' + prefix + 'SHA256SUMS'], text=True)
        assert hashlib.sha256(row.encode()).hexdigest() + '  journey.log' in sums
