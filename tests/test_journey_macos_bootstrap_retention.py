"""Exercise the actual workflow staging script against disposable evidence trees."""

import os
from pathlib import Path
import subprocess
import textwrap

import pytest


WORKFLOW = Path(__file__).resolve().parents[1] / '.github/workflows/journey.yml'
STEP = '      - name: Stage macOS direct update bootstrap log\n'
NEXT = '      - name: Validate the named lifecycle evidence artifact boundary\n'


def workflow_step():
    workflow = WORKFLOW.read_text(encoding='utf-8')
    step = workflow.split(STEP, 1)[1].split(NEXT, 1)[0]
    return step, textwrap.dedent(step.split('        run: |\n', 1)[1])


def test_staging_is_scoped_and_precedes_existing_boundary_and_upload():
    workflow = WORKFLOW.read_text(encoding='utf-8')
    step, _ = workflow_step()
    assert "if: always() && matrix.job_id == 'direct-macos' && matrix.os == 'macos' && matrix.mode == 'direct' && matrix.leg == 'release0-to-release1'" in step
    assert workflow.index(STEP) < workflow.index(NEXT) < workflow.index('      - name: Retain only the named lifecycle evidence')
    assert "'update-bootstrap.log'" in workflow.split(NEXT, 1)[1].split('      - name: Retain only', 1)[0]


@pytest.mark.parametrize('case', ['one', 'limit', 'missing', 'duplicate', 'symlink', 'oversized'])
def test_actual_staging_script(case, tmp_path):
    evidence = tmp_path / 'astrid-r1-direct-macos' / 'evidence'
    source = evidence / 'release0-to-release1' / 'release1' / 'update-bootstrap.log'
    source.parent.mkdir(parents=True)
    content = b'bootstrap failure: exit 1\n'
    if case == 'limit':
        content = b'x' * (1024 * 1024)
    if case == 'oversized':
        content = b'x' * (1024 * 1024 + 1)
    if case == 'symlink':
        target = source.parent / 'target.log'
        target.write_bytes(content)
        source.symlink_to(target)
    elif case != 'missing':
        source.write_bytes(content)
    if case == 'duplicate':
        second = evidence / 'other' / 'update-bootstrap.log'
        second.parent.mkdir()
        second.write_bytes(content)

    _, script = workflow_step()
    result = subprocess.run(['bash', '-e', '-o', 'pipefail', '-c', script],
                            env={**os.environ, 'RUNNER_TEMP': str(tmp_path)},
                            capture_output=True, text=True, check=False)
    staged = tmp_path / 'astrid-r1-direct-macos' / 'evidence-upload' / 'before-cleanup' / 'update-bootstrap.log'
    if case in ('one', 'limit'):
        assert result.returncode == 0, result.stderr
        assert staged.read_bytes() == content
    else:
        assert result.returncode != 0
        assert not staged.exists()
