import os
from pathlib import Path
from types import SimpleNamespace
from scripts import ci_prelaunch_clean_inventory as inv


def test_measure_counts_without_following_symlinks(tmp_path, monkeypatch):
    monkeypatch.setattr(inv, 'UID', os.getuid())
    root = tmp_path / 'owned'; root.mkdir()
    (root / 'first').write_bytes(b'1234')
    (root / 'hardlink').hardlink_to(root / 'first')
    (root / 'nested').mkdir()
    (root / 'nested/second').write_bytes(b'123')
    outside = tmp_path / 'outside'; outside.mkdir()
    (outside / 'private').write_bytes(b'x' * 100)
    (root / 'link').symlink_to(outside, target_is_directory=True)
    result = inv.measure(root)
    assert result['bytes'] == 7 and result['files'] == 3
    assert result['symlinks'] == 1 and not result['truncated']
    assert result['owner_uid'] == os.getuid()


def test_measure_marks_entry_limit_incomplete(tmp_path, monkeypatch):
    monkeypatch.setattr(inv, 'UID', os.getuid())
    for i in range(5): (tmp_path / str(i)).write_text('x')
    assert inv.measure(tmp_path, max_entries=2)['truncated']


def test_processes_report_only_uid_and_category_without_commands(monkeypatch):
    sample = (' 503 12 5 00:12 node /Users/runner/r25-option-b-runs/run.a/secret token=abc\n'
              ' 501 14 1 00:15 python /Users/peter/private\n'
              ' 503 15 1 00:16 unrelated --password=secret\n')
    monkeypatch.setattr(inv.subprocess, 'run', lambda args, **kw: SimpleNamespace(stdout=sample if '-U' in args else '503 Mon Nov  2 12:00:00 2026'))
    result = inv.task_processes()
    assert result == [{'uid': 503, 'pid': 12, 'ppid': 5, 'elapsed': '00:12', 'category': 'r25-test', 'observed_start': 'Mon Nov  2 12:00:00 2026'}]
    assert 'secret' not in str(result) and 'token' not in str(result)


def test_workflow_embeds_tested_inventory_script():
    import base64
    import re
    root = Path(__file__).parents[1]
    workflow = (root / '.github/workflows/journey.yml').read_text()
    match = re.search(r'base64\.b64decode\("([A-Za-z0-9+/=]+)", validate=True\)', workflow)
    assert match and base64.b64decode(match.group(1)) == (root / 'scripts/ci_prelaunch_clean_inventory.py').read_bytes()
    assert 'workflow_dispatch:' in workflow and 'actions/checkout' not in workflow
