"""Bounded Linux host-observation diagnostic; never opens a platform session."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from scripts.native_service_adapter import LiveProcessProbe


def observe(pid):
    # Check PID birth around the other reads to reject reuse and concurrent exit.
    birth, exe = LiveProcessProbe._linux_identity(pid)
    status = Path(f'/proc/{pid}/status').read_text()
    uid_line = next(line for line in status.splitlines() if line.startswith('Uid:'))
    uids = [int(v) for v in uid_line.split()[1:]]
    executable_hash = hashlib.sha256(exe.read_bytes()).hexdigest()
    birth_after, exe_after = LiveProcessProbe._linux_identity(pid)
    if (birth, exe) != (birth_after, exe_after) or len(uids) != 4:
        raise RuntimeError('process identity changed or UID fields incomplete')
    return {'pid': pid, 'birth_ticks': birth, 'executable': str(exe),
            'executable_sha256': executable_hash, 'uids': uids}


def census(uid):
    rows = []
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit():
            continue
        try:
            status = (entry / 'status').read_text()
            line = next(line for line in status.splitlines() if line.startswith('Uid:'))
            uids = [int(v) for v in line.split()[1:]]
            if len(uids) != 4:
                raise RuntimeError('incomplete UID fields')
            if uid in uids:
                rows.append(observe(int(entry.name)))
        except FileNotFoundError:
            # Process exit makes a global point-in-time completeness claim impossible.
            continue
    return sorted(rows, key=lambda row: row['pid'])


def main():
    result = {'diagnostic_only': True, 'authority': False, 'complete_history': False,
              'head': os.environ.get('U1_HEAD'), 'inner_exit': None, 'child_exits': []}
    children = []
    exit_code = 2
    try:
        if sys.platform != 'linux' or os.geteuid() != 0:
            raise RuntimeError('requires Linux privileged observer, not a root product')
        uid = int(sys.argv[1])
        gid = int(sys.argv[2])
        if uid <= 0 or gid <= 0:
            raise RuntimeError('selected product UID must be nonzero')
        result.update(selected_uid=uid, selected_gid=gid, observer_uid=os.geteuid(),
                      boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
                      pid_namespace=os.readlink('/proc/self/ns/pid'),
                      observer=observe(os.getpid()))
        before = census(uid)
        result['before'] = before
        if before:
            raise RuntimeError('selected UID is not initially empty')
        for _ in range(2):
            children.append(subprocess.Popen(['/usr/bin/setpriv', '--reuid', str(uid),
                                             '--regid', str(gid), '--clear-groups',
                                             '--no-new-privs', '/usr/bin/sleep', '90']))
        deadline = time.monotonic() + 5
        while True:
            rows = census(uid)
            if {row['pid'] for row in rows} == {p.pid for p in children} and all(
                    row['uids'] == [uid] * 4 and row['executable'] == '/usr/bin/sleep'
                    for row in rows):
                break
            if time.monotonic() >= deadline:
                raise RuntimeError('selected principal census did not match both live children')
            time.sleep(.05)
        result['during'] = rows
        result['child_pid_namespace'] = [os.readlink(f'/proc/{p.pid}/ns/pid') for p in children]
        if any(ns != result['pid_namespace'] for ns in result['child_pid_namespace']):
            raise RuntimeError('selected children are in a different PID namespace')
        exit_code = 0
    except Exception as exc:
        result['error'] = f'{type(exc).__name__}: {exc}'
    finally:
        for child in children:
            if child.poll() is None:
                child.terminate()
            try:
                result['child_exits'].append(child.wait(timeout=5))
            except subprocess.TimeoutExpired:
                child.kill()
                result['child_exits'].append(child.wait())
                exit_code = 2
        try:
            if 'selected_uid' in result:
                result['after'] = census(result['selected_uid'])
                if result['after']:
                    exit_code = 1
        except Exception as exc:
            result['cleanup_observation_error'] = f'{type(exc).__name__}: {exc}'
            exit_code = 2
        result['inner_exit'] = exit_code
        print(json.dumps(result, sort_keys=True), flush=True)
    return exit_code


if __name__ == '__main__':
    sys.exit(main())
