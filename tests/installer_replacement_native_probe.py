#!/usr/bin/env python3
"""Disposable Linux file-descriptor replacement discriminator, never authority.

The synchronized editor is a distinct process using real syscalls. This probes
only synthetic same-length systemd unit files; it does not implement a guard.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import traceback


def digest(data):
    return hashlib.sha256(data).hexdigest()


def content(label):
    assert len(label) == 8
    return ('[Unit]\nDescription=' + label + '\n[Service]\nExecStart=/usr/bin/true\n').encode()


def identity(stat):
    return {'dev': stat.st_dev, 'ino': stat.st_ino, 'nlink': stat.st_nlink, 'size': stat.st_size}


def editor(target, kind):
    # Block until the guard has completed its comparison, or until the caller
    # explicitly signals the before-open case. No advisory lock is shared.
    if sys.stdin.readline().strip() != 'GO':
        raise RuntimeError('missing editor release')
    replacement = content('FOREIGN0')
    if kind == 'rename':
        pending = target.with_name('.editor-' + target.name)
        pending.write_bytes(replacement)
        os.replace(pending, target)
    elif kind == 'inplace':
        with target.open('r+b', buffering=0) as f:
            written = os.pwrite(f.fileno(), replacement, 0)
            if written != len(replacement):
                raise RuntimeError('short editor write')
            os.fsync(f.fileno())
    else:
        raise ValueError(kind)
    print(json.dumps({'identity': identity(target.stat()), 'sha256': digest(target.read_bytes())}), flush=True)


def command(*argv):
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=20, check=False)
        return {'argv': list(argv), 'exit': proc.returncode, 'stdout': proc.stdout, 'stderr': proc.stderr}
    except Exception as exc:
        return {'argv': list(argv), 'exit': None, 'error': repr(exc)}


def manager_read(name, enabled):
    if not enabled:
        return {'skipped': 'filesystem-only local plumbing run'}
    reload_result = command('systemctl', '--user', 'daemon-reload')
    show_result = command('systemctl', '--user', 'show', name, '-p', 'Description', '--value')
    return {'reload': reload_result, 'show': show_result}


def require_loaded(observation, expected, enabled):
    if enabled:
        assert observation['reload']['exit'] == 0, observation
        assert observation['show']['exit'] == 0, observation
        assert observation['show']['stdout'].strip() == expected, observation


def run_case(name, kind, before_open, original, ours, manager, directory):
    unit = 'r18-replacement-' + name.replace('_', '-') + '.service'
    target = directory / unit
    target.write_bytes(content(original))
    before = {'identity': identity(target.stat()), 'sha256': digest(target.read_bytes())}
    before['manager'] = manager_read(unit, manager)
    fd = None
    process = None
    observations = {'before': before, 'editor_command': [sys.executable, __file__, '--editor', kind, '--target', str(target)]}
    try:
        require_loaded(before['manager'], original, manager)
        process = subprocess.Popen(observations['editor_command'], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if not before_open:
            fd = os.open(target, os.O_RDWR | os.O_NOFOLLOW)
            observations['checked_fd'] = {'identity': identity(os.fstat(fd)), 'sha256': digest(os.pread(fd, 4096, 0))}
            assert observations['checked_fd']['identity']['ino'] == before['identity']['ino']
            assert observations['checked_fd']['sha256'] == before['sha256']
        stdout, stderr = process.communicate('GO\n', timeout=20)
        observations['editor'] = {'exit': process.returncode, 'stdout': stdout, 'stderr': stderr}
        assert process.returncode == 0, observations['editor']
        observations['editor']['observation'] = json.loads(stdout)
        observations['replacement_before_write'] = {'identity': identity(target.stat()), 'sha256': digest(target.read_bytes()), 'manager': manager_read(unit, manager)}
        assert observations['replacement_before_write']['sha256'] == digest(content('FOREIGN0'))
        require_loaded(observations['replacement_before_write']['manager'], 'FOREIGN0', manager)
        if before_open:
            fd = os.open(target, os.O_RDWR | os.O_NOFOLLOW)
            observations['checked_fd'] = {'identity': identity(os.fstat(fd)), 'sha256': digest(os.pread(fd, 4096, 0))}
        checked = observations['checked_fd']
        comparison_matches = (checked['identity']['dev'], checked['identity']['ino'], checked['sha256']) == (before['identity']['dev'], before['identity']['ino'], before['sha256'])
        observations['comparison_matches'] = comparison_matches
        if comparison_matches:
            written = os.pwrite(fd, content(ours), 0)
            os.fsync(fd)
            observations['write'] = {'attempted': True, 'bytes': written, 'fd_identity_after': identity(os.fstat(fd)), 'fd_sha256_after': digest(os.pread(fd, 4096, 0))}
            assert written == len(content(ours))
        else:
            observations['write'] = {'attempted': False, 'result': 'binding_changed'}
        observations['after'] = {'identity': identity(target.stat()), 'sha256': digest(target.read_bytes()), 'manager': manager_read(unit, manager)}
        expect_preserved = kind == 'rename'
        expected = 'FOREIGN0' if expect_preserved else ours
        assert observations['after']['sha256'] == digest(content(expected))
        require_loaded(observations['after']['manager'], expected, manager)
        assert comparison_matches != before_open
        observations['outcome'] = 'replacement_preserved' if expect_preserved else 'same_inode_edit_overwritten'
        return observations
    except Exception as exc:
        exc.observations = observations
        raise
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.communicate()
        if fd is not None:
            os.close(fd)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--editor', choices=['rename', 'inplace'])
    parser.add_argument('--target', type=Path)
    parser.add_argument('--filesystem-only', action='store_true')
    args = parser.parse_args()
    if args.editor:
        editor(args.target, args.editor)
        return 0
    root = Path(os.environ['PROOF_ROOT'])
    result = {'uid': os.getuid(), 'euid': os.geteuid(), 'filesystem_only': args.filesystem_only,
              'authority': False, 'cases': {}, 'setup_error': None,
              'assumptions_and_limits': ['synthetic same-size files; no full AG2 mutation implementation',
                'fd writes do not exclude same-inode edits or hard links; no atomic compare-and-write',
                'a pinned fd does not protect manager reload/restart, trigger set, or loaded/disk equivalence',
                'no gate, no safe conditional unlink, no issuer barrier or lifecycle authority',
                'filesystem-only mode is local plumbing, not qualifying native evidence']}
    directory = Path(os.environ['XDG_CONFIG_HOME']) / 'systemd' / 'user'
    try:
        if not args.filesystem_only and (os.getuid() == 0 or os.getuid() != os.geteuid()):
            raise RuntimeError('requires real non-root user and matching euid')
        directory.mkdir(parents=True, exist_ok=True)
        cases = [('rename_after_check_install', 'rename', False, 'ORIGINAL', 'OWNED000'),
                 ('rename_after_check_restore', 'rename', False, 'OWNED000', 'ORIGINAL'),
                 ('rename_before_open_refuse', 'rename', True, 'ORIGINAL', 'OWNED000'),
                 ('inplace_after_check_counterexample', 'inplace', False, 'ORIGINAL', 'OWNED000')]
        for name, kind, before_open, original, ours in cases:
            started = time.monotonic_ns()
            try:
                result['cases'][name] = {'exit': 0, 'observations': run_case(name, kind, before_open, original, ours, not args.filesystem_only, directory)}
            except Exception as exc:
                result['cases'][name] = {'exit': 1, 'error': repr(exc), 'traceback': traceback.format_exc(), 'observations': getattr(exc, 'observations', {})}
            result['cases'][name]['elapsed_ns'] = time.monotonic_ns() - started
    except Exception as exc:
        result['setup_error'] = repr(exc)
        result['setup_traceback'] = traceback.format_exc()
    result['inner_exit'] = 0 if result['setup_error'] is None and len(result['cases']) == 4 and all(c['exit'] == 0 for c in result['cases'].values()) else 1
    root.mkdir(parents=True, exist_ok=True)
    (root / 'result.json').write_text(json.dumps(result, indent=2, sort_keys=True) + '\n')
    return result['inner_exit']


if __name__ == '__main__':
    sys.exit(main())
