"""Independent review probes; run against an exact worktree via PYTHONPATH."""
import hashlib
import json
from pathlib import Path
import pytest
from scripts import journey_live_workspace as live
from scripts import journey_version_jumps as jumps
from scripts import verify_installed as verify


def inventory(root):
    return {str(p.relative_to(root)): (p.lstat().st_ino, p.lstat().st_mode,
            p.lstat().st_mtime_ns, hashlib.sha256(p.read_bytes()).hexdigest())
            for p in root.rglob('*') if p.is_file()}


@pytest.fixture
def case(tmp_path):
    root, r0, r1 = (tmp_path / n for n in ('program', 'data', 'data-r1'))
    version = root / 'versions' / 'v1' / 'version.json'
    version.parent.mkdir(parents=True)
    version.write_text('{}')
    for data in (r0, r1):
        (data / 'runtime' / 'realms' / 'realm').mkdir(parents=True)
        (data / 'user.txt').write_text('retained edits in ' + data.name)
        (data / 'token').write_text('test-only credential')
    marker = root / 'install.json'
    marker.write_text(json.dumps({'data_root': str(r0), 'r1_data_root': str(r1)}))
    (tmp_path / 'tst-b1-owner.json').write_text(json.dumps({
        'schema_version': 1, 'run_id': 'a' * 32, 'root': str(root), 'data_root': str(r0)}))
    before = inventory(r0), inventory(r1)
    yield root, r0, r1, version, marker
    assert (inventory(r0), inventory(r1)) == before, 'R0 or R1 file content/identity changed'


def discovery(data):
    return {'endpoint': 'http://127.0.0.1:12345',
            'realm_root': str(data / 'runtime/realms/realm'), 'active_realm': 'realm',
            'runtime_instance_id': 'instance', 'pid': 123, 'process_birth_id': 'birth',
            'credential_file': str(data / 'token')}








@pytest.mark.parametrize('damage', ['realm-outside', 'credential-outside', 'root-symlink', 'owner-mismatch'])
def test_live_selection_fails_closed(case, damage):
    root, r0, r1, version, marker = case
    d = discovery(r1)
    if damage == 'realm-outside':
        d['realm_root'] = str(r0 / 'runtime/realms/realm')
    elif damage == 'credential-outside':
        d['credential_file'] = str(r0 / 'token')
    elif damage == 'root-symlink':
        alias = r0.parent / 'alias'
        alias.symlink_to(r1, target_is_directory=True)
        marker.write_text(json.dumps({'data_root': str(r0), 'r1_data_root': str(alias)}))
        r1 = alias
    else:
        (r0.parent / 'tst-b1-owner.json').write_text('{}')
    with pytest.raises(ValueError):
        live.selection_for(r1, root, d)


def test_corrupt_r1_marker_cannot_authorize_unrelated_workspace(case, tmp_path):
    root, r0, r1, version, marker = case
    other = tmp_path / 'unrelated-user-data'
    (other / 'runtime/realms/realm').mkdir(parents=True)
    (other / 'token').write_text('unrelated credential')
    (other / 'user.txt').write_text('unrelated user edits')
    before = inventory(other)
    marker.write_text(json.dumps({'data_root': str(r0), 'r1_data_root': str(other)}))
    try:
        # Refuse at every entry point, including before live selection.
        with pytest.raises(RuntimeError):
            jumps.selected_data_root(root, r0)
        with pytest.raises(ValueError):
            verify.observation_data_root(version, r0)
        with pytest.raises(ValueError):
            live.selection_for(other, root, discovery(other))
    finally:
        assert inventory(other) == before


def test_direct_workspace_rejects_bad_selection_before_service_action(case, monkeypatch):
    root, r0, r1, version, marker = case
    from scripts import ci_journey as journey
    marker.write_text(json.dumps({'data_root': str(r0), 'r1_data_root': 'relative'}))
    actions = []
    monkeypatch.setattr(journey, 'assert_direct_service_ownership', lambda *a: actions.append('ownership'))
    monkeypatch.setattr(journey, 'run', lambda *a, **kw: actions.append('service-mutation'))
    with pytest.raises(RuntimeError):
        jumps.direct_workspace(root, {}, r0, root.parent / 'evidence', 'seed', {})
    assert actions == []


def test_live_selection_requires_original_owned_data_directory(case):
    root, r0, r1, version, marker = case
    other = r0.parent / 'other'
    (other / 'runtime/realms/realm').mkdir(parents=True)
    (other / 'token').write_text('credential')
    marker.write_text(json.dumps({'data_root': str(other)}))
    (r0.parent / 'tst-b1-owner.json').write_text(json.dumps({
        'schema_version': 1, 'run_id': 'a' * 32, 'root': str(root), 'data_root': str(other)}))
    with pytest.raises(ValueError, match='owned program and data roots'):
        live.selection_for(other, root, discovery(other))
