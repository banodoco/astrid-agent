"""Unadmitted Linux principal boundary discriminator; never opens hosted admission.

Run only as root on a disposable host with an already provisioned user manager.
The helper is synthetic: it does not invoke a product or establish lifecycle authority.
"""
from __future__ import annotations
import ctypes
import hashlib
import json
import os
from pathlib import Path
import pwd
import select
import socket
import subprocess
import sys
import tempfile
import time


class Refusal(RuntimeError):
    pass


def birth(pid):
    # comm may contain spaces and ')'; starttime is field 22, index 19 after comm.
    stat = Path(f'/proc/{pid}/stat').read_text()
    return stat[stat.rfind(')') + 2:].split()[19]


def identity(pid):
    p = Path(f'/proc/{pid}')
    status = dict(line.split(':', 1) for line in (p / 'status').read_text().splitlines() if ':' in line)
    return {'pid': pid, 'birth': birth(pid), 'uids': list(map(int, status['Uid'].split())),
            'exe': os.readlink(p / 'exe'), 'argv': (p / 'cmdline').read_bytes().split(b'\0')[:-1],
            'cwd': os.readlink(p / 'cwd')}


def manager(uid):
    def mainpid():
        p = subprocess.run(['/usr/bin/systemctl', 'show', f'user@{uid}.service', '-p', 'MainPID', '--value'],
                           capture_output=True, text=True, check=True, timeout=15)
        return int(p.stdout.strip())
    pid = mainpid()
    if pid <= 1:
        raise Refusal('PID 1 reports no user manager')
    fd = os.pidfd_open(pid)
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    peerfd = None
    try:
        before = identity(pid)
        s.connect(f'/run/user/{uid}/systemd/private')
        # Linux SO_PEERPIDFD returns a new fd, held through the root-anchor recheck.
        peerfd = int.from_bytes(s.getsockopt(socket.SOL_SOCKET, 77, 4), sys.byteorder, signed=True)
        info = Path(f'/proc/self/fdinfo/{peerfd}').read_text()
        peer = int(next(x.split(':', 1)[1] for x in info.splitlines() if x.startswith('Pid:')))
        after = identity(pid)
        if (peer != pid or mainpid() != pid or before != after or
            before['uids'] != [uid] * 4 or Path(before['exe']).name != 'systemd' or
            select.select([fd, peerfd], [], [], 0)[0]):
            raise Refusal('manager identity or root anchor changed')
        return {'pid': pid, 'birth': before['birth'], 'uid': uid, 'peer_pid': peer,
                'executable': before['exe']}
    finally:
        if peerfd is not None: os.close(peerfd)
        s.close()
        os.close(fd)


def verify_child(pid, uid, executable, argv, cwd, expected_birth):
    observed = identity(pid)
    if (observed['birth'] != expected_birth or observed['uids'] != [uid] * 4 or
        observed['exe'] != executable or observed['argv'] != [os.fsencode(x) for x in argv] or
        observed['cwd'] != cwd):
        raise Refusal('child identity drift')
    return observed


def drop(uid, gid):
    os.setgroups([])
    os.setgid(gid)
    os.setuid(uid)
    if ctypes.CDLL(None, use_errno=True).prctl(38, 1, 0, 0, 0) != 0:
        os._exit(126)


def helper(collector, export):
    results = {}
    for name, path in [('collector', collector), ('export', export)]:
        for operation in ('write', 'replace'):
            try:
                if operation == 'write':
                    with open(path, 'ab') as f: f.write(b'candidate')
                else:
                    fd, source = tempfile.mkstemp(prefix='m46-candidate-')
                    os.close(fd)
                    try:
                        os.replace(source, path)
                    finally:
                        if os.path.exists(source): os.unlink(source)
                results[name + '_' + operation] = 'unexpected-success'
            except OSError as exc:
                results[name + '_' + operation] = f'denied:{exc.errno}'
    print(json.dumps(results), flush=True)
    request = sys.stdin.readline().strip()
    if request == 'drift':
        os.execv('/usr/bin/sleep', ['/usr/bin/sleep', '30'])
    print(json.dumps({'request': request, 'settled': True}), flush=True)


class OneUseRequest:
    def __init__(self, request):
        self._request = request
        self._used = False

    def consume(self, request):
        if self._used or request != self._request:
            raise Refusal('foreign or replayed request')
        self._used = True
        return request


def run(uid, directory):
    if os.geteuid() != 0 or uid <= 0:
        raise Refusal('root collector and non-root candidate are required')
    account = pwd.getpwuid(uid)
    directory = Path(directory).resolve(strict=True)
    collector = directory / 'collector.py'
    export = directory / 'export.json'
    if directory.stat().st_uid != 0 or directory.stat().st_mode & 0o022:
        raise Refusal('collector directory is not protected')
    for p in (collector, export):
        if p.is_symlink() or p.stat().st_uid != 0 or p.stat().st_mode & 0o022:
            raise Refusal('collector/export is not protected')
    anchored = manager(uid)
    baseline = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (collector, export)}
    argv = ['/usr/bin/python3', '-I', str(collector), '--helper', str(collector), str(export)]
    exe = str(Path('/usr/bin/python3').resolve())
    records = []
    ledger = OneUseRequest('request-1')
    for action in ('request-1', 'drift'):
        child = subprocess.Popen(argv, cwd=str(directory), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True, preexec_fn=lambda: drop(uid, account.pw_gid))
        try:
            fd = os.pidfd_open(child.pid)
            controls = json.loads(child.stdout.readline())
            if any(not value.startswith('denied:') for value in controls.values()) or len(controls) != 4:
                raise Refusal('candidate changed protected files')
            first = identity(child.pid)
            verify_child(child.pid, uid, exe, argv, str(directory), first['birth'])
            if manager(uid) != anchored:
                raise Refusal('manager changed')
            if action != 'drift': ledger.consume(action)
            child.stdin.write(action + '\n'); child.stdin.flush()
            if action == 'drift':
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and identity(child.pid)['exe'] == exe:
                    time.sleep(.01)
                try:
                    verify_child(child.pid, uid, exe, argv, str(directory), first['birth'])
                except Refusal:
                    drift_rejected = True
                else:
                    raise Refusal('real exec drift accepted')
                child.terminate()
                out, err = child.communicate(timeout=5)
                records.append({'actual_exec_drift_rejected': drift_rejected, 'outer_exit': child.returncode})
            else:
                out, err = child.communicate(timeout=5)
                readback = json.loads(out)
                if child.returncode != 0 or readback != {'request': action, 'settled': True}:
                    raise Refusal('unknown child exit or readback')
                records.append({'pid': child.pid, 'birth': first['birth'], 'argv': argv, 'cwd': str(directory),
                                'controls': controls, 'outer_exit': child.returncode, 'readback': readback})
            os.close(fd)
        finally:
            if child.poll() is None:
                child.kill(); child.wait()
    if manager(uid) != anchored or baseline != {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (collector, export)}:
        raise Refusal('manager or protected bytes changed')
    return {'admitted': False, 'synthetic_only': True, 'manager': anchored, 'candidate_uid': uid,
            'protected_sha256': baseline, 'records': records}


if __name__ == '__main__':
    if len(sys.argv) == 4 and sys.argv[1] == '--helper':
        helper(sys.argv[2], sys.argv[3])
    elif len(sys.argv) == 3:
        print(json.dumps(run(int(sys.argv[1]), sys.argv[2]), indent=2))
    else:
        raise SystemExit('usage: python3 SCRIPT UID PROTECTED_DIRECTORY')
