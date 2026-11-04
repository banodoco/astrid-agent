import argparse
import ctypes
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import ci_journey as journey
from scripts import windows_schema_acl_observer as observer


def test_collects_version_and_cache_ancestors_and_aliases_paths(tmp_path, monkeypatch):
    root, cache = tmp_path / 'program', tmp_path / 'private-cache'
    version = root / 'versions' / 'v1'
    staged = version / 'a' / observer.SCHEMA
    installed = version / 'py' / 'Lib' / 'site-packages' / observer.SCHEMA
    cached = cache / 'archive' / 'x' / observer.SCHEMA
    for path in (staged, installed, cached):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{}')
    monkeypatch.setattr(observer, 'native_observation', lambda path: {
        'owner_sid': 'S-1-5-18', 'dacl_sddl': 'D:(A;;FR;;;BU)',
        'file_identity': {'volume_serial': 1, 'file_index': 2}})
    monkeypatch.setattr(observer, 'native_token_observation', lambda: {
        'user_sid': 'S-1-5-21-1', 'owner_sid': 'S-1-5-32-544'})
    def run(argv, **kwargs):
        if argv[1:] == ['cache', 'dir']:
            return SimpleNamespace(returncode=0, stdout=str(cache), stderr='')
        return SimpleNamespace(returncode=0, stdout=f'{argv[1]} BUILTIN\\Users:(RX)', stderr='')
    monkeypatch.setattr(observer.subprocess, 'run', run)
    result = observer.collect(root, 'after-install')
    paths = result['paths']
    for kind, prefix in [('staged', '<program>'), ('installed', '<program>'), ('cache', '<uv-cache>')]:
        entries = [p for p in paths if p['kind'] == kind]
        assert entries[0]['path'].endswith('timeline-config.schema.json')
        assert entries[0]['owner_sid'] == 'S-1-5-18'
        assert entries[0]['observer_process_token'] == {
            'user_sid': 'S-1-5-21-1', 'owner_sid': 'S-1-5-32-544'}
        assert entries[0]['observation_started_utc'] <= entries[0]['native_observation_finished_utc']
        assert 'observer_process_token' not in entries[-1]
        assert entries[0]['icacls']['exit_code'] == 0
        assert entries[-1]['path'].replace('\\', '/') == (prefix + '/versions/v1' if kind != 'cache' else prefix)
    assert result['uv']['root_source'] == 'uv cache dir'
    assert str(tmp_path) not in json.dumps(result)


class FakeTokenApi:
    def __init__(self, failure=None):
        self.failure = failure
        self.error = 0
        self.closed = 0
        self.freed = 0
        self.strings = []

    def GetCurrentProcess(self):
        return 42

    def OpenProcessToken(self, process, access, token):
        assert process == 42 and access == 8
        if self.failure == 'open':
            self.error = 5
            return False
        ctypes.cast(token, ctypes.POINTER(ctypes.c_void_p))[0] = 123
        return True

    def GetTokenInformation(self, token, info, buffer, length, size):
        from ctypes import wintypes as w
        assert info in (1, 4)
        if not buffer:
            if self.failure == 'user_size' and info == 1:
                self.error = 5
                return False
            if self.failure == 'oversized_user' and info == 1:
                ctypes.cast(size, ctypes.POINTER(w.DWORD))[0] = 1024 * 1024 + 1
                self.error = 122
                return False
            ctypes.cast(size, ctypes.POINTER(w.DWORD))[0] = ctypes.sizeof(ctypes.c_void_p)
            self.error = 122
            return False
        if self.failure == 'owner_read' and info == 4:
            self.error = 6
            return False
        ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0] = info
        return True

    def IsValidSid(self, sid):
        return not (self.failure == 'invalid_user' and sid == 1)

    def ConvertSidToStringSidW(self, sid, target):
        if self.failure == 'convert_user' and sid == 1:
            self.error = 1337
            return False
        value = ctypes.create_unicode_buffer('S-1-5-' + str(sid))
        self.strings.append(value)
        ctypes.cast(target, ctypes.POINTER(ctypes.c_void_p))[0] = ctypes.addressof(value)
        return True

    def LocalFree(self, value):
        self.freed += 1

    def CloseHandle(self, value):
        self.closed += 1


@pytest.mark.parametrize(('failure', 'expected', 'freed', 'closed'), [
    (None, {'user_sid': 'S-1-5-1', 'owner_sid': 'S-1-5-4'}, 2, 1),
    ('open', {'error': 'OpenProcessToken: 5'}, 0, 0),
    ('user_size', {'user_error': 'GetTokenInformation size: 5; size=0', 'owner_sid': 'S-1-5-4'}, 1, 1),
    ('oversized_user', {'user_error': 'GetTokenInformation size: 122; size=1048577', 'owner_sid': 'S-1-5-4'}, 1, 1),
    ('owner_read', {'user_sid': 'S-1-5-1', 'owner_error': 'GetTokenInformation: 6'}, 1, 1),
    ('invalid_user', {'user_error': 'IsValidSid: invalid SID', 'owner_sid': 'S-1-5-4'}, 1, 1),
    ('convert_user', {'user_error': 'ConvertSidToStringSidW: 1337', 'owner_sid': 'S-1-5-4'}, 1, 1),
])
def test_token_sids_and_errors_are_independent(failure, expected, freed, closed):
    api = FakeTokenApi(failure)
    assert observer._read_token_sids(api, api, lambda: api.error) == expected
    assert api.freed == freed
    assert api.closed == closed


def test_token_exception_keeps_schema_observation(tmp_path, monkeypatch):
    root = tmp_path / 'program'
    leaf = root / 'versions' / 'v1' / 'a' / observer.SCHEMA
    leaf.parent.mkdir(parents=True)
    leaf.write_text('{}')
    monkeypatch.setattr(observer, 'native_token_observation', lambda: (_ for _ in ()).throw(OSError('private')))
    monkeypatch.setattr(observer, 'native_observation', lambda path: {'dacl_sddl': 'D:(A;;FR;;;BU)'})
    monkeypatch.setattr(observer.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=1, stdout='', stderr=''))
    monkeypatch.delenv('LOCALAPPDATA', raising=False)
    monkeypatch.delenv('UV_CACHE_DIR', raising=False)
    result = observer.collect(root, 'after-install')
    item = result['paths'][0]
    assert item['observer_process_token'] == {'error': 'OSError'}
    assert item['dacl_sddl'] == 'D:(A;;FR;;;BU)'
    assert 'private' not in json.dumps(result)


@pytest.mark.posix_native
def test_missing_cache_and_reparse_are_reported_without_following(tmp_path):
    cache = tmp_path / 'cache'
    cache.mkdir()
    external = tmp_path / 'outside'
    external.mkdir()
    (external / observer.SCHEMA.name).write_text('{}')
    (cache / 'link').symlink_to(external, target_is_directory=True)
    assert observer.cache_schemas(cache) == ([], [])
    assert observer.cache_schemas(tmp_path / 'absent')[1] == ['cache root not present']


def test_bootstrap_uv_and_unavailable_cache_are_explicit(tmp_path, monkeypatch):
    local = tmp_path / 'local'
    executable = local / 'Astrid Installer' / 'bootstrap' / 'uv.exe'
    executable.parent.mkdir(parents=True)
    executable.touch()
    monkeypatch.setenv('LOCALAPPDATA', str(local))
    monkeypatch.delenv('UV_CACHE_DIR', raising=False)
    def run(argv, **kwargs):
        assert argv == [str(executable), 'cache', 'dir']
        raise OSError('private path')
    monkeypatch.setattr(observer.subprocess, 'run', run)
    result = observer.collect(tmp_path / 'program', 'before-install')
    assert result['uv']['executable'] == 'installer-bootstrap'
    assert result['uv']['root_source'].startswith('environment/default')
    assert result['uv']['scan_errors'] == ['cache root not present']
    assert 'private path' not in json.dumps(result)


def test_observer_failure_is_evidence_and_does_not_raise(tmp_path, monkeypatch):
    monkeypatch.setattr(observer.sys, 'platform', 'win32')
    monkeypatch.setattr(observer, 'collect', lambda *args: (_ for _ in ()).throw(ValueError('sensitive')))
    observer.observe(tmp_path, tmp_path / 'evidence', 'after-install')
    data = json.loads((tmp_path / 'evidence' / 'windows-schema-acl-after-install.json').read_text())
    assert data == {'phase': 'after-install', 'observer_error': 'ValueError'}


def test_failed_install_still_observes_and_keeps_failure(tmp_path, monkeypatch):
    args = argparse.Namespace(leg='fresh-install', job_id='direct-windows', mode='direct',
                              source_override=False, app_checkout=tmp_path, agent_checkout=None,
                              runtime_checkout=None, shell='powershell.exe')
    monkeypatch.setattr(journey.sys, 'platform', 'win32')
    monkeypatch.setenv('ASTRID_CI_SCHEMA_ACL_DIAGNOSTIC', 'true')
    monkeypatch.setattr(journey, 'runner_identity', lambda: {})
    monkeypatch.setattr(journey, 'selected_manifest', lambda *a, **k: (tmp_path / 'manifest', {}))
    monkeypatch.setattr(journey, 'git_source', lambda *a: {'sha': 'a'})
    monkeypatch.setattr(journey, 'sentinel_observation', lambda *a: {'alive': True})
    calls = []
    monkeypatch.setattr(journey.windows_schema_acl_observer, 'observe', lambda root, evidence, phase: calls.append(phase))
    def fail(*args):
        calls.append('install')
        raise RuntimeError('original install failure')
    monkeypatch.setattr(journey, 'bootstrap', fail)
    evidence = tmp_path / 'run' / 'evidence' / 'fresh-install'
    with pytest.raises(RuntimeError, match='original install failure'):
        journey._execute_owned(args, tmp_path / 'program', tmp_path / 'data', evidence,
                               SimpleNamespace(pid=123), 'birth')
    assert calls == ['before-install', 'install', 'after-install']
    receipt = json.loads((evidence / 'journey-result.json').read_text())
    assert receipt['status'] == 'fail'
    assert receipt['evidence_class'] == 'diagnostic-only'


def test_named_evidence_staged_and_workflow_upload_is_always(tmp_path):
    evidence = tmp_path / 'run' / 'evidence' / 'fresh-install'
    evidence.mkdir(parents=True)
    for phase in ('before-install', 'after-install'):
        (evidence / f'windows-schema-acl-{phase}.json').write_text('{}')
    journey.retain_named_evidence(evidence, 'before-cleanup')
    staged = tmp_path / 'run' / 'evidence-upload' / 'before-cleanup'
    workflow = (Path(__file__).resolve().parents[1] / '.github/workflows/journey.yml').read_text()
    for phase in ('before-install', 'after-install'):
        name = f'windows-schema-acl-{phase}.json'
        assert (staged / name).read_text() == '{}'
        assert name in workflow
    assert "if: always() && steps.evidence_boundary.outcome == 'success'" in workflow
    assert "if: always() && matrix.job_id == 'direct-windows' && matrix.leg == 'fresh-install'" in workflow


@pytest.mark.windows_native
def test_native_probe_reads_owner_dacl_and_hard_link_identity(tmp_path):
    original, link = tmp_path / 'original', tmp_path / 'link'
    original.write_text('unchanged')
    os.link(original, link)
    first = observer.native_observation(original)
    second = observer.native_observation(link)
    assert first['owner_sid'].startswith('S-1-')
    assert first['dacl_sddl'].startswith('D:')
    assert first['file_identity'] == second['file_identity']
    assert first['file_identity']['link_count'] >= 2
    assert original.read_text() == 'unchanged'
    token = observer.native_token_observation()
    assert token['user_sid'].startswith('S-1-')
    assert token['owner_sid'].startswith('S-1-')
    from runtime_protocol import windows_security
    assert token['user_sid'] == windows_security._sid_text(windows_security._user_sid())
    assert token['owner_sid'] == windows_security._sid_text(windows_security._default_owner_sid())
