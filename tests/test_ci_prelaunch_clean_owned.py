import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from scripts import ci_prelaunch_clean_owned as clean


def make_target(tmp_path, monkeypatch):
    monkeypatch.setattr(clean, 'UID', os.getuid())
    parent = tmp_path / 'task-parent'; parent.mkdir(mode=0o700)
    target = parent / 'run.test'; target.mkdir(mode=0o700)
    (target / 'sequence.env').write_text('agent_ref=a6b68d5b5478326a4db1971966d33a2cfe61d091\npinokiod_ref=add4a674ad1ba95bb62ec427fce519fe5bf0e9bf\npterm_ref=5be43718701c448b43daf0ce51d8935f170065bb\npinokio_product_ref=3a6a98ae251ca126505245286e2d3e760f4348e9\n')
    retained = tmp_path / 'retained'; retained.mkdir(mode=0o700)
    return parent, target, retained


def test_removes_owned_tree_without_following_symlinks_and_preserves_small_logs(tmp_path, monkeypatch):
    parent, target, retained = make_target(tmp_path, monkeypatch)
    outside = tmp_path / 'outside'; outside.mkdir(); (outside / 'important').write_text('keep')
    (target / 'setup.stderr.log').write_text('evidence')
    (target / 'outside-link').symlink_to(outside, target_is_directory=True)
    (target / 'sub').mkdir(); (target / 'sub/data').write_text('disposable')
    monkeypatch.setattr(clean, 'no_process_reference', lambda _: True)
    monkeypatch.setattr(clean, 'no_open_files', lambda _: True)
    record = {}; clean.guarded_remove(target, parent, 'r25-run-2TyWZKgP', False, retained, record, lambda: None)
    assert record['status'] == 'removed' and not target.exists()
    assert (outside / 'important').read_text() == 'keep'
    assert (retained / 'setup.stderr.log').read_text() == 'evidence'
    assert (retained / 'RETAINED-SHA256SUMS').exists()
    assert record['before']['symlinks'] == 1


def test_referenced_target_is_left_untouched(tmp_path, monkeypatch):
    parent, target, retained = make_target(tmp_path, monkeypatch)
    (target / 'data').write_text('keep')
    monkeypatch.setattr(clean, 'no_process_reference', lambda _: False)
    monkeypatch.setattr(clean, 'no_open_files', lambda _: True)
    record = {}; clean.guarded_remove(target, parent, 'r25-run-2TyWZKgP', False, retained, record, lambda: None)
    assert record['status'] == 'left-guard-refused' and (target / 'data').read_text() == 'keep'


def test_expected_empty_root_refuses_nonempty_contents(tmp_path, monkeypatch):
    parent, target, retained = make_target(tmp_path, monkeypatch)
    (target / 'data').write_text('keep')
    monkeypatch.setattr(clean, 'no_process_reference', lambda _: True)
    monkeypatch.setattr(clean, 'no_open_files', lambda _: True)
    record = {}; clean.guarded_remove(target, parent, 'r25-run-OslHRGip', True, retained, record, lambda: None)
    assert record['status'] == 'left-guard-refused' and (target / 'data').exists()


def test_root_symlink_is_refused(tmp_path, monkeypatch):
    parent, target, retained = make_target(tmp_path, monkeypatch)
    (target / 'sequence.env').unlink()
    target.rmdir()
    outside = tmp_path / 'outside'; outside.mkdir(); (outside / 'important').write_text('keep')
    target.symlink_to(outside, target_is_directory=True)
    record = {}; clean.guarded_remove(target, parent, 'r25-run-2TyWZKgP', False, retained, record, lambda: None)
    assert record['status'] == 'left-guard-refused' and (outside / 'important').read_text() == 'keep'


def test_changed_tree_before_removal_is_refused(tmp_path, monkeypatch):
    parent, target, retained = make_target(tmp_path, monkeypatch)
    (target / 'data').write_text('keep')
    monkeypatch.setattr(clean, 'no_process_reference', lambda _: True)
    monkeypatch.setattr(clean, 'no_open_files', lambda _: True)
    original = clean.scan_tree
    calls = []
    def scan(*args, **kwargs):
        result = original(*args, **kwargs); calls.append(1)
        if len(calls) == 2: result['tree_sha256'] = 'changed'
        return result
    monkeypatch.setattr(clean, 'scan_tree', scan)
    record = {}; clean.guarded_remove(target, parent, 'r25-run-2TyWZKgP', False, retained, record, lambda: None)
    assert record['status'] == 'left-guard-refused' and (target / 'data').read_text() == 'keep'


@pytest.mark.parametrize('code,stdout,stderr,expected', [
    (1, '', '', True), (0, 'open file', '', False), (1, '', 'warning', False), (2, '', 'error', False),
])
def test_lsof_guard_requires_unambiguous_absence(monkeypatch, code, stdout, stderr, expected, tmp_path):
    monkeypatch.setattr(clean.subprocess, 'run', lambda *a, **kw: SimpleNamespace(returncode=code, stdout=stdout, stderr=stderr))
    assert clean.no_open_files(tmp_path) is expected


def test_scan_refuses_mount_crossing(tmp_path, monkeypatch):
    parent, target, retained = make_target(tmp_path, monkeypatch)
    (target / 'mount').mkdir()
    original = clean.os.stat
    def fake(path, *args, **kwargs):
        st = original(path, *args, **kwargs)
        if str(path) == 'mount' and kwargs.get('dir_fd') is not None:
            values = list(st); values[2] = st.st_dev + 1
            return os.stat_result(values)
        return st
    monkeypatch.setattr(clean.os, 'stat', fake)
    with pytest.raises(RuntimeError, match='filesystem boundary'):
        clean.scan_tree(target, clean.target_identity(target, parent), 'r25-run-2TyWZKgP')


def test_retention_directory_symlink_refuses_without_deleting_source(tmp_path, monkeypatch):
    parent, target, retained = make_target(tmp_path, monkeypatch)
    outside = tmp_path / 'outside'; outside.mkdir(); (outside / 'important').write_text('keep')
    (target / 'results/pinokio-backend-macos').mkdir(parents=True)
    (target / 'results/pinokio-backend-macos/runner.exit').write_text('1')
    (retained / 'results').symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(clean, 'no_process_reference', lambda _: True)
    monkeypatch.setattr(clean, 'no_open_files', lambda _: True)
    record = {}; clean.guarded_remove(target, parent, 'r25-run-2TyWZKgP', False, retained, record, lambda: None)
    assert record['status'] == 'left-guard-refused' and target.exists()
    assert sorted(p.name for p in outside.iterdir()) == ['important']


def test_wrong_provenance_refuses_before_process_inspection_or_deletion(tmp_path, monkeypatch):
    parent, target, retained = make_target(tmp_path, monkeypatch)
    (target / 'sequence.env').write_text('agent_ref=not-our-task\n')
    monkeypatch.setattr(clean, 'no_process_reference', lambda _: pytest.fail('must refuse before process inspection'))
    record = {}; clean.guarded_remove(target, parent, 'r25-run-2TyWZKgP', False, retained, record, lambda: None)
    assert record['status'] == 'left-guard-refused' and target.exists()


def test_r18_provenance_requires_exact_candidate_tuple(tmp_path, monkeypatch):
    import json
    monkeypatch.setattr(clean, 'UID', os.getuid())
    target = tmp_path / 'r18'; target.mkdir()
    value = {'app': 'ee66de5d100e0a9b8cf5198961556a68b24cad3e',
             'agent': '80e19ea05d926172bbcf2588e46f6f9919776a81',
             'runtime': '255ecda21ba904520eb867936ef173cc5f7d3ae4',
             'pinokio': '3a6a98ae251ca126505245286e2d3e760f4348e9',
             'manifest_sha256': '9858d63ea5609d30e09f648df35aed3a8a0f519c98bca13bbd27830686f353e3'}
    (target / 'candidate.json').write_text(json.dumps(value))
    assert clean.provenance_matches(target, 'r18-41536262841-1')
    value['agent'] = 'not-our-task'
    (target / 'candidate.json').write_text(json.dumps(value))
    assert not clean.provenance_matches(target, 'r18-41536262841-1')


def test_workflow_embeds_tested_guard_and_uses_manual_runner():
    import base64
    import re
    root = Path(__file__).parents[1]
    workflow = (root / '.github/workflows/journey.yml').read_text()
    match = re.search(r'base64\.b64decode\("([A-Za-z0-9+/=]+)", validate=True\)', workflow)
    assert match and base64.b64decode(match.group(1)) == (root / 'scripts/ci_ws06_stop_owned.py').read_bytes()
    assert 'workflow_dispatch:' in workflow and 'actions/checkout' not in workflow
    assert 'prelaunch-runner-maintenance' in workflow
