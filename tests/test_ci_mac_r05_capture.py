import base64
import hashlib
import json
import os
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from scripts import ci_mac_r05_capture as capture


def test_manual_workflow_embeds_script_and_never_uploads_logs():
    root = Path(__file__).parents[1]
    workflow = (root / '.github/workflows/journey.yml').read_text()
    match = re.search(r'base64\.b64decode\("([A-Za-z0-9+/=]+)", validate=True\)', workflow)
    assert match and base64.b64decode(match.group(1)) == (root / 'scripts/ci_mac_r05_capture.py').read_bytes()
    assert 'workflow_dispatch:' in workflow and 'actions/checkout' not in workflow
    assert 'Upload sanitized capture and checksums' in workflow


def test_capture_environment_is_truthful_and_excludes_credentials(tmp_path, monkeypatch):
    for key in ('ASTRID_CI_EXPECTED_RUNNER', 'ImageOS', 'ImageVersion', 'GITHUB_TOKEN'):
        monkeypatch.setenv(key, 'do-not-copy')
    monkeypatch.setenv('RUNNER_OS', 'macOS')
    env = capture.environment(tmp_path / 'home', tmp_path / 'fresh', tmp_path / 'python/bin/python3.11')
    assert env['RUNNER_OS'] == 'macOS' and env['RUNNER_TEMP'] == str(tmp_path / 'fresh')
    assert all(key not in env for key in ('ASTRID_CI_EXPECTED_RUNNER', 'ImageOS', 'ImageVersion', 'GITHUB_TOKEN'))


def test_host_census_rejects_exact_runtime_match_but_not_unrelated():
    assert capture.owner_match("python -I -c from astrid_runtime.cli import main; main(['run'])")
    assert capture.owner_match('python -m runtime_protocol start')
    assert not capture.owner_match('python -m unrelated start')


def test_export_copies_only_capture_and_recomputes_recursive_hashes(tmp_path):
    home = tmp_path / 'home'
    root = tmp_path / 'fresh'
    data = root / 'evidence/nested'
    data.mkdir(parents=True)
    (data / 'receipt.json').write_text(json.dumps({'path': str(home / 'secret'), 'value': 'ok'}))
    (root / 'private-logs').mkdir()
    (root / 'private-logs' / 'do-not-send.log').write_text('secret')
    outside = tmp_path / 'outside'
    outside.write_text('sensitive')
    (data / 'escape.txt').symlink_to(outside)
    output = tmp_path / 'artifact'
    output.mkdir()
    capture.export_capture(root, home, output, {'status': 'captured'})
    text = (output / 'capture-upload/nested/receipt.json').read_text()
    assert str(home) not in text and '<RUNNER_HOME>' in text
    assert not (output / 'private-logs').exists() and not (output / 'capture-upload/nested/escape.txt').exists()
    for row in (output / 'SHA256SUMS').read_text().splitlines():
        sha, path = row.split('  ', 1)
        assert hashlib.sha256((output / path).read_bytes()).hexdigest() == sha
    assert 'SHA256SUMS  SHA256SUMS' not in (output / 'SHA256SUMS').read_text()


@pytest.mark.parametrize('code,text,expected', [
    (113, 'Could not find service "haus.astrid.runtime" in domain for user gui: 503', True),
    (113, 'Bad request.\nCould not find service "haus.astrid.runtime" in domain for user gui: 503', True),
    (1, 'Could not find service "haus.astrid.runtime" in domain for user gui: 503', False),
    (113, 'Could not find service "other" in domain for user gui: 503', False),
])
def test_service_absence_is_exact(monkeypatch, code, text, expected):
    monkeypatch.setattr(capture.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=code, stderr=text))
    assert capture.service_absent() is expected


@pytest.mark.parametrize("code,stdout,stderr,expected", [
    (1, "", "", True),
    (0, "COMMAND PID USER", "", False),
    (1, "", "lsof warning", False),
    (2, "", "error", False),
])
def test_port_guard_requires_exact_lsof_absence(monkeypatch, code, stdout, stderr, expected):
    calls = []
    def run(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=code, stdout=stdout, stderr=stderr)
    monkeypatch.setattr(capture.subprocess, 'run', run)
    assert capture.port_free() is expected
    assert calls == [["/usr/sbin/lsof", "-nP", "-iTCP:17340", "-sTCP:LISTEN"]]



def test_refused_host_does_not_create_private_temp_root(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    runner_temp = home / 'actions-runner/_work/_temp'
    runner_temp.mkdir(parents=True)
    github_env = tmp_path / 'github-env'
    github_env.write_text('')
    monkeypatch.setattr(capture, 'UID', os.getuid())
    monkeypatch.setattr(capture.sys, 'platform', 'darwin')
    monkeypatch.setattr(capture.pwd, 'getpwuid', lambda _: SimpleNamespace(pw_dir=str(home)))
    monkeypatch.setenv('RUNNER_NAME', 'hannah-mac')
    monkeypatch.setenv('RUNNER_TEMP', str(runner_temp))
    monkeypatch.setenv('GITHUB_ENV', str(github_env))
    monkeypatch.setattr(capture.shutil, 'disk_usage', lambda _: SimpleNamespace(free=20_000_000_000))
    monkeypatch.setattr(capture, 'port_free', lambda: True)
    monkeypatch.setattr(capture, 'service_absent', lambda: False)
    monkeypatch.setattr(capture.tempfile, 'mkdtemp', lambda **kw: pytest.fail('capture root must not be created'))
    with pytest.raises(SystemExit):
        capture.main()
    custody = json.loads((runner_temp / 'r05-artifact/custody.json').read_text())
    assert custody['error'] == 'Runner Runtime service present or absence unverified'


def test_error_reporting_does_not_leak_unknown_exception_text():
    assert capture.safe_error(OSError('password=abc /Users/person/secret')) == 'Unexpected operation failure; inspect runner-owned private evidence'
    assert capture.safe_error(RuntimeError('Source operation failed: driver/clone')) == 'Source operation failed: driver/clone'


def test_global_guard_is_rechecked_just_before_driver():
    import inspect
    source = inspect.getsource(capture.main)
    assert source.count('guard_host(home)') == 2
    assert source.rindex('guard_host(home)') < source.index('inner.write_text(')
    assert 'host_owners_exist()' in inspect.getsource(capture.guard_host)
    assert 'port_free()' in inspect.getsource(capture.guard_host)


def test_guard_checks_lsof_before_launchd_and_refuses_unknown(monkeypatch, tmp_path):
    events = []
    monkeypatch.setattr(capture, 'port_free', lambda: events.append('lsof') or False)
    monkeypatch.setattr(capture, 'service_absent', lambda: events.append('launchctl') or True)
    with pytest.raises(RuntimeError, match='port'):
        capture.guard_host(tmp_path)
    assert events == ['lsof']


def test_adopt_diagnostic_binds_sources_and_guards_before_mutation():
    import inspect
    source = inspect.getsource(capture.main)
    inner = (Path(__file__).parents[1] / 'scripts/ci_mac_r1fix_inner.py').read_text()
    assert repr(inner) in source
    assert inner.index("done('r0-fixture-verification')") < inner.index('v.bootstrap_release0(')
    assert inner.index("done('r0-stopped-and-lock-free')") < inner.index("command('adopt'")
    assert 'fcntl.LOCK_EX|fcntl.LOCK_NB' in inner
    assert 'loaded_path.group(1)!=str(plist)' in inner
    assert "'workspace','adopt','--data-root'" in inner
    assert capture.REFS['agent'][1] == 'ce78f3586b02ca2d5526760a062c6240d28da7db'
    assert capture.REFS['runtime'][1] == 'f27ff70126958d1736a83b606df5126acf268bf5'
