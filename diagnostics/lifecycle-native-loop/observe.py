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
    dump(name + '.json', stamp)


def main():
    result = {'kind': 'diagnostic-only', 'status': 'not-run', 'stage': 'preflight', 'uid': os.getuid(),
              'r0_runtime': '6d53a19070faf63fc15ae1f81417722edc2c8a5f',
              'r1_agent': 'b54117c9312165e2799554ec27480665d5cad2b1',
              'r1_runtime': '2776ba973b6c8f3555b5629b0840d9691a98d3c2',
              'checks': {'r0_install': 'not-run', 'stop': 'not-observed', 'fenced_transfer': 'not-observed', 'r1_start_registration': 'not-observed'}}
    try:
        if os.getuid() == 0:
            raise RuntimeError('root executor refused')
        fixture, manifest, provenance = jumps.verified_release0_fixture(RUN / 'agent' / 'tests' / 'fixtures' / 'release0')
        dump('r0-fixture.json', provenance)
        result['stage'] = 'r0-install'
        snap('before-install')
        env_boot = [fixture / 'served' / 'install', '--manifest', manifest, '--root', RUN / 'program', '--data-root', RUN / 'data']
        rc = command('r0-install', ['sh', *env_boot], 900)
        result['r0_install_exit'] = rc
        if rc:
            raise RuntimeError('authentic R0 bootstrap failed')
        result['stage'] = 'r0-readiness'
        journey.wait_ready(RUN / 'data', timeout=120)
        version_path, _, version = journey.installation(RUN / 'program', RUN / 'data')
        service = journey.assert_direct_service_ownership(RUN / 'program', RUN / 'data')
        journey.assert_one_runtime(RUN / 'data')
        dump('r0-selection.json', {'version_path': str(version_path), 'version': version, 'service': service,
                                   'discovery': journey.healthy(RUN / 'data')})
        result['checks']['r0_install'] = 'observed'
        snap('r0-ready')
        result['stage'] = 'r1-update'
        # The bootstrap itself drives the selected stop/transfer/start. A trace is
        # optional and only follows the updater's children; missing events are unknown.
        args = ['sh', RUN / 'app' / 'public' / 'install', '--manifest', RUN / 'target-manifest.json',
                '--root', RUN / 'program', '--data-root', RUN / 'data', '--update']
        if subprocess.run(['sh','-c','command -v strace >/dev/null && strace -o /dev/null true'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False).returncode == 0:
            args = ['strace','-f','-ttt','-e','trace=flock,fcntl','-o',str(OUT / 'updater-lock-syscalls.log'), *args]
            result['strace'] = 'requested; updater descendants only'
        else:
            result['strace'] = 'unavailable'
        # Capture non-atomic snapshots while updater executes, without acquiring any lock.
        import threading
        stop = threading.Event()
        def sample():
            index = 0
            while not stop.wait(1):
                snap('during-%04d' % index)
                index += 1
        observer = threading.Thread(target=sample, daemon=True)
        observer.start()
        try:
            rc = command('r1-update', args, 900)
        finally:
            stop.set()
            observer.join(timeout=10)
        result['r1_update_exit'] = rc
        snap('after-update')
        if rc:
            raise RuntimeError('R1 update refused or failed; inspect update and installer logs')
        result['stage'] = 'r1-readiness'
        journey.wait_ready(RUN / 'data', timeout=120)
        selected, _, version = journey.installation(RUN / 'program', RUN / 'data')
        service = journey.assert_direct_service_ownership(RUN / 'program', RUN / 'data')
        journey.assert_one_runtime(RUN / 'data')
        dump('r1-selection.json', {'version_path': str(selected), 'version': version, 'service': service,
                                   'discovery': journey.healthy(RUN / 'data')})
        result['checks']['r1_start_registration'] = 'observed'
        result['status'] = 'completed-observation'
        result['stage'] = 'complete'
        # An external observation cannot establish that the product held the
        # lock continuously through handover. Leave that predicate unobserved.
        return 0
    except Exception as exc:
        result['status'] = 'failed'
        result['error'] = f'{type(exc).__name__}: {exc}'
        (OUT / 'exception.txt').write_text(traceback.format_exc())
        return 1
    finally:
        try:
            snap('final')
        except Exception:
            pass
        dump('result.json', result)

if __name__ == '__main__':
    sys.exit(main())
