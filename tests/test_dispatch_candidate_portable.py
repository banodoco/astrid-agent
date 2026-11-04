import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/dispatch_candidate.py'
AGENT = 'a' * 40
RUNTIME = 'b' * 40


def git(*args, cwd):
    return subprocess.run(['git', *map(str, args)], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def test_dispatch_pins_manifest_and_emits_lane_input(tmp_path):
    remote = tmp_path / 'remote.git'
    git('init', '--bare', remote, cwd=tmp_path)
    app = tmp_path / 'repos/astrid-app'
    app.mkdir(parents=True)
    git('init', cwd=app)
    git('config', 'user.email', 'test@example.invalid', cwd=app)
    git('config', 'user.name', 'Test', cwd=app)
    public = app / 'public'
    public.mkdir()
    (public / 'install-manifest.json').write_text(json.dumps({'agent': {'sha': 'c'*40}, 'runtime': {'sha': 'd'*40}}))
    for name in ('install', 'install.sh', 'install.ps1'):
        (public / name).write_text(name)
    git('add', '.', cwd=app)
    git('commit', '-m', 'fixture', cwd=app)
    git('remote', 'add', 'origin', remote, cwd=app)
    git('push', 'origin', 'HEAD:refs/heads/int/r1', cwd=app)
    (tmp_path / 'outbox/inputs').mkdir(parents=True)
    cmd = [sys.executable, str(SCRIPT), AGENT, RUNTIME, 'direct-linux', 'test.json', '--workspace', str(tmp_path), '--lane', 'M-RT']
    result = subprocess.run(cmd, check=True, capture_output=True, text=True)
    inputs = json.loads((tmp_path / 'outbox/inputs/test.json').read_text())
    candidate = json.loads(inputs['candidate'])
    assert (candidate['agent'], candidate['runtime']) == (AGENT, RUNTIME)
    manifest = json.loads(git('show', f"{candidate['app']}:public/install-manifest.json", cwd=remote))
    assert manifest['agent']['sha'] == AGENT and manifest['runtime']['sha'] == RUNTIME
    assert inputs['row'] == 'direct-linux' and inputs['direct_only'] is True
    assert 'ACTIONS: astrid-agent journey.yml int/r1 @inputs/M-RT-test.json' in result.stdout
    assert subprocess.run(cmd, capture_output=True).returncode != 0
