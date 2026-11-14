from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.packs.runpod import remote_placement as rp


def old_digest(root):
    lines = []
    for directory in rp.SOURCE_DIRS:
        base = root / directory
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in ('.git', '__pycache__')]
            if any((Path(dirpath) / name).is_symlink() for name in (*dirnames, *filenames)):
                raise rp.RemotePlacementError('bootstrap source closure contains a symlink')
            for name in filenames:
                if not name.endswith('.pyc'):
                    full = Path(dirpath) / name
                    lines.append((full.relative_to(root).as_posix().encode(), rp.sha256_file(full)))
    lines.sort(key=lambda item: item[0])
    listing = ''.join(f'{digest}  {rel.decode()}\n' for rel, digest in lines)
    return hashlib.sha256(listing.encode()).hexdigest()[:16]


@pytest.fixture
def tree(tmp_path, monkeypatch):
    monkeypatch.setenv('XDG_CACHE_HOME', str(tmp_path / 'cache'))
    rp._SOURCE_DIGEST_MEMO.clear()
    root = tmp_path / 'sources'
    for directory in rp.SOURCE_DIRS:
        base = root / directory
        base.mkdir(parents=True)
        (base / 'file').write_text(directory)
    return root


def test_memo_and_old_digest(tree, monkeypatch):
    expected = old_digest(tree)
    original = rp.sha256_file
    calls = []
    def counted(path):
        calls.append(path)
        return original(path)
    monkeypatch.setattr(rp, 'sha256_file', counted)
    assert rp.source_digest(tree) == expected
    assert len(calls) == 4
    calls.clear()
    assert rp.source_digest(tree) == expected
    assert not calls
    rp._SOURCE_DIGEST_MEMO.clear()
    assert rp.source_digest(tree) == expected
    assert not calls
    target = tree / rp.SOURCE_DIRS[0] / 'file'
    before = target.stat().st_mtime_ns
    target.write_text('changed')
    os.utime(target, ns=(before + 1000000, before + 1000000))
    assert rp.source_digest(tree) != expected
    assert len(calls) == 4


def test_untracked_ignored_corrupt_and_symlink(tree, monkeypatch, tmp_path):
    repo = tree / rp.SOURCE_DIRS[0]
    subprocess.run(['git', 'init', '-q', str(repo)], check=True)
    (repo / '.gitignore').write_text('ignored\n')
    initial = rp.source_digest(tree)
    ignored = repo / 'ignored'
    ignored.write_text('ignored content')
    subprocess.run(['git', '-C', str(repo), 'check-ignore', '-q', 'ignored'], check=True)
    assert rp.source_digest(tree) != initial
    untracked = tree / rp.SOURCE_DIRS[0] / 'untracked'
    untracked.write_text('untracked content')
    expected = rp.source_digest(tree)
    assert expected != initial
    rp._SOURCE_DIGEST_MEMO.clear()
    (tmp_path / 'cache/astrid/source-digest-memo.json').write_text('{bad')
    calls = []
    original = rp.sha256_file
    def counted(path):
        calls.append(path)
        return original(path)
    monkeypatch.setattr(rp, 'sha256_file', counted)
    assert rp.source_digest(tree) == expected
    assert len(calls) == 7
    (tree / rp.SOURCE_DIRS[0] / 'link').symlink_to(ignored)
    with pytest.raises(rp.RemotePlacementError, match='symlink'):
        rp.source_digest(tree)


class PollOps:
    def __init__(self, running):
        self.running = running
        self.polls = 0
    def job_start(self, command):
        return {'id': 'job'}
    def job_poll(self, job_id):
        self.polls += 1
        return {'state': 'running'} if self.polls <= self.running else {'state': 'exited', 'exit_code': 0}


@pytest.mark.parametrize(('cap', 'running', 'expected'), [
    (5.0, 6, [1.0, 1.5, 2.25, 3.375, 5.0, 5.0]),
    (0.25, 2, [0.25, 0.25]),
    (0, 2, [0, 0]),
])
def test_poll_backoff(tmp_path, cap, running, expected):
    ops = PollOps(running)
    sleeps = []
    runner = rp.RemotePlacement(None, limits=rp.Limits(poll_seconds=cap), sleep=sleeps.append)
    state = rp.RemoteRun('run', '/run', tmp_path, claim=SimpleNamespace(ops=ops))
    runner._run_job(state, 'command', 'job')
    assert sleeps == expected
    assert ops.polls == running + 1


def test_poll_sleep_clamped_to_deadline(tmp_path):
    ops = PollOps(99)
    now = [0.0]
    sleeps = []
    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds
    runner = rp.RemotePlacement(None, clock=lambda: now[0], sleep=sleep)
    runner._deadline_at = 2.0
    runner._execution_seconds = 2.0
    state = rp.RemoteRun('run', '/run', tmp_path, claim=SimpleNamespace(ops=ops))
    with pytest.raises(rp.RemotePlacementError, match='deadline'):
        runner._run_job(state, 'command', 'job')
    assert sleeps == [1.0, 1.0]
    assert ops.polls == 2
