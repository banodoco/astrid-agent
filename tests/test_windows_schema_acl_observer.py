import argparse
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
        assert entries[0]['icacls']['exit_code'] == 0
        assert entries[-1]['path'].replace('\\', '/') == (prefix + '/versions/v1' if kind != 'cache' else prefix)
    assert result['uv']['root_source'] == 'uv cache dir'
    assert str(tmp_path) not in json.dumps(result)


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
    assert json.loads((evidence / 'journey-result.json').read_text())['status'] == 'fail'


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
