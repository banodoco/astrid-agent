#!/usr/bin/env python3
"""Run one diagnostic stage with the existing installer; never a release verdict."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import traceback

HERE = Path(__file__).resolve().parent
RUN = Path(os.environ['LOOP_ROOT'])
OUT = RUN / 'evidence'
OUT.mkdir(mode=0o700, exist_ok=True)
sys.path.insert(0, str(RUN / 'agent' / 'scripts'))
import ci_journey as journey
import journey_version_jumps as jumps

# Keep the existing identity-checked readiness and ownership checks on the
# selected case port; frozen R0's installer also consumes this environment.
journey.HEALTH_URL = f"http://127.0.0.1:{int(os.environ['ASTRID_RUNTIME_DOOR_PORT'])}/api/runtime/v1/health"


def dump(name, value):
    (OUT / name).write_text(json.dumps(value, indent=2, sort_keys=True) + '\n')


def command(name, args, timeout):
    started = time.time()
    try:
        with (OUT / (name + '.log')).open('wb') as log:
            p = subprocess.run([str(a) for a in args], stdout=log, stderr=subprocess.STDOUT, timeout=timeout)
        rc = p.returncode
    except subprocess.TimeoutExpired:
        rc = 124
    dump(name + '.json', {'argv': [str(a) for a in args], 'start': started, 'end': time.time(), 'exit': rc})
    return rc


def snap(name):
    stamp = {'wall_time': time.time(), 'monotonic': time.monotonic(), 'uid': os.getuid()}
    for label, args in [('unit', ['systemctl','--user','show','astrid.service','-p','LoadState,ActiveState,SubState,FragmentPath,MainPID,ControlGroup,ExecStart,Restart,UnitFileState']),
                        ('processes', ['ps','-ww','-u',str(os.getuid()),'-o','pid=,ppid=,sid=,etimes=,stat=,args=']),
                        ('locks', ['cat','/proc/locks'])]:
        try:
            p = subprocess.run(args, capture_output=True, text=True, timeout=5)
            stamp[label] = {'exit': p.returncode, 'stdout': p.stdout, 'stderr': p.stderr}
        except Exception as exc:
            stamp[label] = {'error': repr(exc)}
    files = {}
    for relative in ('runtime/discovery.json', 'runtime/instance.lock', 'runtime/bootstrap.lock'):
        path = RUN / 'data' / relative
        try:
            st = path.lstat()
            row = {'device': st.st_dev, 'inode': st.st_ino, 'mode': oct(st.st_mode), 'uid': st.st_uid, 'size': st.st_size}
            if path.is_file() and not path.is_symlink():
                contents = path.read_bytes()
                row['sha256'] = hashlib.sha256(contents).hexdigest()
                if relative.endswith(('discovery.json', 'instance.lock')):
                    row['contents'] = contents.decode('utf-8', errors='replace')
            files[relative] = row
        except OSError as exc:
            files[relative] = {'error': repr(exc)}
    stamp['files'] = files
    # Read only the run-owned unit and the selected same-user process. These
    # are separately timed samples, not the operands of the installer's call.
    unit_path = RUN / 'home/.config/systemd/user/astrid.service'
    try:
        if unit_path.is_symlink():
            raise ValueError('unit path is a symlink')
        unit_bytes = unit_path.read_bytes()
        stamp['unit_definition'] = {'sha256': hashlib.sha256(unit_bytes).hexdigest(),
                                    'contents': unit_bytes.decode('utf-8', errors='replace')}
    except (OSError, ValueError) as exc:
        stamp['unit_definition'] = {'error': repr(exc)}
    lock_path = RUN / 'home/.local/share/astrid-install.lock'
    try:
        st = lock_path.lstat()
        stamp['installer_lock'] = {'device': st.st_dev, 'inode': st.st_ino, 'uid': st.st_uid,
                                   'mode': oct(st.st_mode)}
    except OSError as exc:
        stamp['installer_lock'] = {'error': repr(exc)}
    try:
        discovery = json.loads((RUN / 'data/runtime/discovery.json').read_text())
        pid = discovery['pid']
        if type(pid) is not int or pid <= 0:
            raise ValueError('invalid selected discovery pid')
        proc = Path('/proc') / str(pid)
        if proc.stat().st_uid != os.getuid():
            raise ValueError('selected pid is not owned by disposable user')
        stat = (proc / 'stat').read_text().rsplit(')', 1)[1].split()
        observed = {'pid': pid, 'uid': proc.stat().st_uid, 'state': stat[0],
                    'ppid': int(stat[1]), 'start_ticks': stat[19],
                    'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
                    'pidns_inode': (proc / 'ns/pid').stat().st_ino,
                    'cgroup': (proc / 'cgroup').read_text(), 'fds': {}}
        for fd in (proc / 'fd').iterdir():
            try:
                st = fd.stat()
                observed['fds'][fd.name] = {'target': os.readlink(fd), 'device': st.st_dev, 'inode': st.st_ino}
            except OSError as exc:
                observed['fds'][fd.name] = {'error': repr(exc)}
        stamp['selected_process_sample'] = observed
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        stamp['selected_process_sample'] = {'error': repr(exc)}
    dump(name + '.json', stamp)


def main():
    mode = os.environ['PROBE_MODE']
    result = {'kind': 'diagnostic-only', 'mode': mode, 'authority': False,
              'r0_install_exit': None, 'probe_exit': None, 'stop_exit': None}
    try:
        assert os.getuid() != 0
        dump('identity.json', {'uid': os.getuid(), 'euid': os.geteuid(), 'home': str(Path.home()),
             'bus': os.environ.get('DBUS_SESSION_BUS_ADDRESS'), 'runtime_dir': os.environ.get('XDG_RUNTIME_DIR'),
             'mode': mode, 'health_url': journey.HEALTH_URL, 'candidate': '9482dda551130fa1d03d6c114c6f8199572e1422',
             'runtime': '2776ba973b6c8f3555b5629b0840d9691a98d3c2',
             'r0_agent': '3204bb00135bec8ee2d2d3fb21eed5abfceca649',
             'r0_runtime': '6d53a19070faf63fc15ae1f81417722edc2c8a5f'})
        fixture, manifest, provenance = jumps.verified_release0_fixture(RUN / 'agent/tests/fixtures/release0')
        dump('r0-fixture.json', provenance)
        rc = command('r0-install', ['sh', fixture / 'served/install', '--manifest', manifest,
                     '--root', RUN / 'program', '--data-root', RUN / 'data'], 900)
        result['r0_install_exit'] = rc
        if rc: raise RuntimeError('authentic R0 installation failed')
        journey.wait_ready(RUN / 'data', timeout=120)
        selected, _, version = journey.installation(RUN / 'program', RUN / 'data')
        service = journey.assert_direct_service_ownership(RUN / 'program', RUN / 'data')
        journey.assert_one_runtime(RUN / 'data')
        dump('r0-selection.json', {'version_path': str(selected), 'version': version, 'service': service,
                                   'discovery': journey.healthy(RUN / 'data')})
        snap('r0-ready')
        if command('venv', ['python3', '-m', 'venv', RUN / 'venv'], 120): raise RuntimeError('venv failed')
        py = RUN / 'venv/bin/python'
        if command('dependencies', [py, '-m', 'pip', 'install', 'PyYAML', RUN / 'runtime'], 600):
            raise RuntimeError('dependency installation failed')
        if mode == 'stopped':
            # Explicitly authorized disposable fixture only; probe itself is read-only.
            rc = command('stop-disposable-r0', ['systemctl', '--user', 'stop', 'astrid.service'], 120)
            result['stop_exit'] = rc
            if rc: raise RuntimeError('disposable stop failed')
            snap('already-stopped')
        assert not subprocess.check_output(['git', '-C', str(RUN / 'candidate'), 'status', '--porcelain'], text=True).strip()
        # Separate, adjacent sample: retain the candidate's exact query and
        # parser outcome even when the candidate maps an exception to unknown.
        command('systemd-capture', [py, RUN / 'capture-systemd.py', RUN / 'candidate', OUT], 30)
        rc = command('probe', [py, RUN / 'native-probe.py', '--agent', RUN / 'candidate',
                     '--head', '9482dda551130fa1d03d6c114c6f8199572e1422', '--root', RUN / 'program',
                     '--data', RUN / 'data', '--home', RUN / 'home', '--expect', mode], 120)
        result['probe_exit'] = rc
        snap('after-probe')
        if rc: raise RuntimeError('probe failed; inspect inner log')
        result['status'] = 'checkpoint-observed-non-authoritative'
        return 0
    except Exception as exc:
        result['status'] = 'failed'
        result['error'] = f'{type(exc).__name__}: {exc}'
        (OUT / 'exception.txt').write_text(traceback.format_exc())
        return 1
    finally:
        dump('result.json', result)

if __name__ == '__main__':
    sys.exit(main())
