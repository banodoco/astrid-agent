"""Read-only service response probes: no real service-manager calls are allowed."""
import hashlib
import json
import shlex
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import pytest
from scripts import ci_journey as journey


# Module scope: the sentinel guards this module's service probes. At session
# scope its teardown ran after the whole shard, past the sentinel's 300 s
# lifetime, and pytest reported the error on whichever test ran last.
@pytest.fixture(scope='module', autouse=True)
def unrelated_process():
    process = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300)'])
    try:
        yield process
        assert process.poll() is None, 'Unrelated sentinel died during service probes'
    finally:
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=10)


def inventory(root):
    return {str(p.relative_to(root)): (p.lstat().st_ino, p.lstat().st_mode,
            p.lstat().st_mtime_ns, hashlib.sha256(p.read_bytes()).hexdigest())
            for p in root.rglob('*') if p.is_file()}


@pytest.fixture
def setup_case(tmp_path, monkeypatch):
    root, r0, r1 = (tmp_path / n for n in ('program', 'data', 'data-r1'))
    for p in (root, r0, r1):
        p.mkdir()
    for p in (r0, r1):
        (p / 'user.txt').write_text('retained edits ' + p.name)
    before = inventory(r0), inventory(r1)
    monkeypatch.setattr(journey, 'program_root', lambda: tmp_path / 'default/program')
    monkeypatch.setattr(journey.os, 'getuid', lambda: 501)
    calls = []

    def build(platform, damage='none'):
        monkeypatch.setattr(journey.sys, 'platform', platform)
        if platform == 'linux':
            directory = tmp_path / 'home/.config/systemd/user'
            old, new = 'astrid.service', 'astrid-r1.service'
        else:
            directory = tmp_path / 'home/Library/LaunchAgents'
            old, new = 'haus.astrid.runtime.plist', 'haus.astrid.runtime.r1.plist'
        directory.mkdir(parents=True, exist_ok=True)
        paths = {'r0': directory / old, 'r1': directory / new}
        for p in paths.values():
            p.write_text('synthetic owned definition')
        if damage == 'symlink':
            paths['r1'].unlink()
            paths['r1'].symlink_to(paths['r0'])
        if damage == 'missing-r0':
            paths['r0'].unlink()

        def run(argv, **kwargs):
            calls.append(argv)
            assert (argv[0] == 'busctl' or argv[:3] == ['systemctl', '--user', 'show'] or
                    argv[:2] in (['launchctl', 'print'], ['launchctl', 'print-disabled'])), argv
            code, stderr = 0, ''
            if argv[0] == 'busctl':
                args = [str(root / 'bin/runtime'), 'run', '--profile', 'astrid', '--data-root', str(r1)]
                if damage == 'foreign-action': args[0] += '.foreign'
                stdout = 'a(sasbttttuii) 1 ' + shlex.quote(args[0]) + ' 6 ' + shlex.join(args) + ' false 0 0 0 0 0 0 0'
            elif platform == 'linux':
                is_r1 = argv[3] == 'astrid-r1.service'
                fields = dict(LoadState='loaded', FragmentPath=str(paths['r1' if is_r1 else 'r0']),
                              ActiveState='active' if is_r1 else 'inactive',
                              UnitFileState='enabled' if is_r1 else 'disabled', DropInPaths='')
                if is_r1:
                    if damage == 'foreign-path': fields['FragmentPath'] += '.foreign'
                    if damage == 'inactive-r1': fields['ActiveState'] = 'inactive'
                    if damage == 'query-error': code = 1
                    if damage == 'missing-load': fields.pop('LoadState')
                    if damage == 'foreign-dropin': fields['DropInPaths'] = '/foreign/override.conf'
                else:
                    if damage == 'active-r0': fields['ActiveState'] = 'active'
                    if damage == 'enabled-r0': fields['UnitFileState'] = 'enabled'
                stdout = ''.join(f'{k}={v}\n' for k, v in fields.items())
            elif argv[1] == 'print-disabled':
                stdout = '"haus.astrid.runtime" => ' + ('false' if damage == 'enabled-r0' else 'true') + '\n'
                if damage == 'disabled-query-error': code = 1
            elif argv[-1].endswith('runtime.r1'):
                path = str(paths['r1']) + ('.foreign' if damage == 'foreign-path' else '')
                state = 'waiting' if damage == 'inactive-r1' else 'running'
                args = [str(root / 'bin/runtime'), 'run', '--profile', 'astrid', '--data-root', str(r1)]
                if damage == 'foreign-action': args[0] += '.foreign'
                stdout = f'{argv[-1]} = {{\n  path = {path}\n  state = {state}\n  program = {root / "bin/runtime"}\n  arguments = {{\n' + ''.join(f'    {arg}\n' for arg in args) + '  }\n}\n'
                if damage == 'query-error': code = 1
            else:
                stdout = ''
                code = 0 if damage == 'active-r0' else 113
                stderr = 'Permission denied' if damage == 'ambiguous-absence' else 'Could not find service "haus.astrid.runtime"'
            return SimpleNamespace(returncode=code, stdout=stdout, stderr=stderr)

        monkeypatch.setattr(journey.subprocess, 'run', run)
        return root, r0, paths, calls

    yield build
    assert (inventory(r0), inventory(r1)) == before, 'Service observation changed R0 or R1'










def test_macos_rejects_foreign_plist_with_expected_path_prefix(setup_case):
    root, data, *_ = setup_case('darwin', 'foreign-path')
    with pytest.raises(RuntimeError):
        journey.assert_direct_service_ownership(root, data, release=1)


def test_linux_rejects_foreign_service_dropin(setup_case):
    root, data, *_ = setup_case('linux', 'foreign-dropin')
    with pytest.raises(RuntimeError):
        journey.assert_direct_service_ownership(root, data, release=1)






@pytest.mark.parametrize('platform', ['linux', 'darwin'])
def test_rejects_foreign_effective_action(setup_case, platform):
    root, data, *_ = setup_case(platform, 'foreign-action')
    with pytest.raises(RuntimeError):
        journey.assert_direct_service_ownership(root, data, release=1)
