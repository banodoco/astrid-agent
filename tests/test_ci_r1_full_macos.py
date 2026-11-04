import base64
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from scripts import ci_r1_full_macos as full


def test_manual_workflow_embeds_exact_harness_and_two_choices():
    root = Path(__file__).parents[1]
    workflow = (root / '.github/workflows/journey.yml').read_text()
    match = re.search(r'base64\.b64decode\("([A-Za-z0-9+/=]+)", validate=True\)', workflow)
    assert match and base64.b64decode(match.group(1)) == (root / 'scripts/ci_r1_full_macos.py').read_bytes()
    assert 'options: [runtime]' in workflow and 'workflow_dispatch:' in workflow
    assert 'actions/checkout' not in workflow


def test_real_task_child_gets_exclusive_session_before_execution(tmp_path):
    command = [sys.executable, '-c', 'import os; print(os.getpid(),os.getpgrp(),os.getsid(0))']
    proc, log = full.isolated_group(command, tmp_path, {'PATH': '/usr/bin:/bin'}, tmp_path / 'out.log')
    try:
        assert proc.wait(timeout=10) == 0
    finally:
        log.close()
    pid, pgid, sid = map(int, (tmp_path / 'out.log').read_text().split())
    assert pid == pgid == sid and pgid != os.getpgrp()


def test_port_guard_requires_definite_absence(monkeypatch):
    monkeypatch.setattr(full.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=1, stdout='', stderr=''))
    assert full.idle_port(17340)
    monkeypatch.setattr(full.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=1, stdout='', stderr='warning'))
    assert not full.idle_port(17340)


def test_artifact_log_redaction_and_source_hash(tmp_path):
    home = tmp_path / 'home'; task = home / 'task'; logs = task / 'logs'; logs.mkdir(parents=True)
    output = tmp_path / 'output'; output.mkdir()
    data = (str(task) + '/file TOKEN=secret ghp_abcd1234\n').encode()
    (logs / 'pytest.log').write_bytes(data)
    status = {}
    full.stage_logs(logs, output, home, task, status)
    text = (output / 'pytest.log').read_text()
    assert str(home) not in text and 'secret' not in text and 'ghp_' not in text
    assert status['log_source_sha256']['pytest.log'] == __import__('hashlib').sha256(data).hexdigest()


def test_requested_install_commands_are_exact_and_test_scope_unfiltered():
    source = Path(full.__file__).read_text()
    assert "'install', '-e', '.[dev]'" in source
    assert "'install', '-e', '../astrid-agent', '-e', '.[test]'" in source
    assert "[str(py), '-m', 'pytest', '-q', 'tests/test_backup_pinned_sqlite.py', 'tests/test_db_final_r1_rework.py', '-k', 'backup']" in source
    assert '--basetemp=' in source and 'PYTEST_ADDOPTS' in source
