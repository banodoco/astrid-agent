#!/usr/bin/env python3
"""Diagnostic only: direct private D-Bus socket, peer credentials and loaded properties.

Never emits authority. All native calls go through a local byte-forwarding proxy;
SO_PEERCRED is taken on the actual upstream socket, not a separate sample.
"""
import hashlib
import json
import os
import pathlib
import select
import socket
import struct
import subprocess
import threading
import time
import traceback

ROOT = pathlib.Path(os.environ['PROOF_ROOT'])
HOME = pathlib.Path(os.environ['HOME'])
OUT = ROOT / 'result.json'
UNIT = 'astrid-identity-discriminator.service'
PRIVATE = pathlib.Path(os.environ['XDG_RUNTIME_DIR']) / 'systemd/private'
result = {'authority': False, 'uid': os.getuid(), 'euid': os.geteuid(),
          'predicates': {}, 'calls': [], 'errors': [], 'cleanup': {}}

def record(name, value, detail=None):
    result['predicates'][name] = {'state': value, 'detail': detail}

def proc(pid):
    p = pathlib.Path('/proc') / str(pid)
    # Field 22 follows the last ')' of comm, even if comm contains spaces or ')'.
    stat = (p / 'stat').read_text()
    fields = stat[stat.rfind(')') + 2:].split()
    return {'pid': pid, 'start_ticks': int(fields[19]),
            'boot_id': pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
            'pidns_inode': (p / 'ns/pid').stat().st_ino,
            'userns_inode': (p / 'ns/user').stat().st_ino,
            'exe': os.readlink(p / 'exe'), 'uid': p.stat().st_uid}

class Proxy:
    def __init__(self, path):
        self.path = str(path)
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(self.path)
        self.listener.listen(1)
        self.listener.settimeout(12)
        self.peer = None
        self.error = None
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()
    def serve(self):
        try:
            client, _ = self.listener.accept()
            with client, socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as upstream:
                upstream.connect(str(PRIVATE))
                self.peer = struct.unpack('3i', upstream.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize('3i')))
                sockets = [client, upstream]
                while True:
                    ready, _, _ = select.select(sockets, [], [], 15)
                    if not ready:
                        raise TimeoutError('proxy forwarding timeout')
                    for source in ready:
                        data = source.recv(65536)
                        if not data:
                            return
                        (upstream if source is client else client).sendall(data)
        except Exception as exc:
            self.error = type(exc).__name__ + ': ' + str(exc)
    def close(self):
        self.thread.join(20)
        self.listener.close()
        os.unlink(self.path)
        if self.thread.is_alive():
            raise TimeoutError('proxy thread still running')
        if self.error:
            raise RuntimeError(self.error)

def call(label, *args):
    proxy = Proxy(ROOT / ('proxy-' + str(len(result['calls']))))
    command = ['busctl', '--address=unix:path=' + proxy.path, '--json=short', '--no-pager', 'call',
               'org.freedesktop.systemd1', '/org/freedesktop/systemd1',
               'org.freedesktop.systemd1.Manager', *args]
    try:
        done = subprocess.run(command, capture_output=True, timeout=25)
    finally:
        proxy.close()
    item = {'label': label, 'argv': command[1:], 'exit': done.returncode,
            'stdout_sha256': hashlib.sha256(done.stdout).hexdigest(),
            'stderr_sha256': hashlib.sha256(done.stderr).hexdigest(),
            'peer_pid_uid_gid': proxy.peer}
    result['calls'].append(item)
    if done.returncode or proxy.peer is None:
        raise RuntimeError(label + ' failed, exit=' + str(done.returncode) + ' stderr=' + done.stderr.decode(errors='replace')[:500])
    return json.loads(done.stdout), proxy.peer

def systemctl(*args):
    done = subprocess.run(['systemctl', '--user', *args], capture_output=True, text=True, timeout=25)
    result['calls'].append({'label': 'systemctl ' + ' '.join(args), 'exit': done.returncode,
                            'stderr': done.stderr[:500]})
    if done.returncode:
        raise RuntimeError('systemctl failed: ' + done.stderr[:500])
    return done.stdout

def export(label, interface, path):
    proxy = Proxy(ROOT / ('proxy-' + str(len(result['calls']))))
    command = ['busctl', '--address=unix:path=' + proxy.path, '--json=short', '--no-pager', 'call',
               'org.freedesktop.systemd1', path, 'org.freedesktop.DBus.Properties', 'GetAll', 's', interface]
    try:
        done = subprocess.run(command, capture_output=True, timeout=25)
    finally:
        proxy.close()
    item = {'label': label, 'argv': command[1:], 'exit': done.returncode,
            'stdout_sha256': hashlib.sha256(done.stdout).hexdigest(),
            'stderr_sha256': hashlib.sha256(done.stderr).hexdigest(), 'peer_pid_uid_gid': proxy.peer}
    result['calls'].append(item)
    if done.returncode or proxy.peer is None:
        raise RuntimeError(label + ' failed, exit=' + str(done.returncode) + ' stderr=' + done.stderr.decode(errors='replace')[:500])
    return json.loads(done.stdout), proxy.peer

def write_result():
    OUT.write_text(json.dumps(result, sort_keys=True, indent=2) + '\n')

def main():
    if os.getuid() == 0 or os.getuid() != os.geteuid():
        raise RuntimeError('requires equal real/effective non-root uid')
    unit_dir = HOME / '.config/systemd/user'
    unit_dir.mkdir(parents=True, exist_ok=True)
    unit = unit_dir / UNIT
    drop = unit_dir / (UNIT + '.d')
    if unit.exists() or drop.exists():
        raise RuntimeError('fixture already exists')
    unit.write_text('[Unit]\nDescription=Identity discriminator before\n[Service]\nType=oneshot\nExecStart=/usr/bin/true\nEnvironment=ASTRID_IDENTITY_PROBE=before\n')
    try:
        systemctl('daemon-reload')
        response, first_peer = call('load-unit', 'LoadUnit', 's', UNIT)
        path = response['data'][0]
        first = proc(first_peer[0])
        fd = os.pidfd_open(first_peer[0])
        try:
            before_u, p1 = export('unit-before', 'org.freedesktop.systemd1.Unit', path)
            before_s, p2 = export('service-before', 'org.freedesktop.systemd1.Service', path)
            drop.mkdir()
            (drop / 'override.conf').write_text('[Unit]\nDescription=Identity discriminator after\n[Service]\nEnvironment=ASTRID_IDENTITY_PROBE=after\n')
            disk_u, p3 = export('unit-disk-changed-not-reloaded', 'org.freedesktop.systemd1.Unit', path)
            disk_s, p4 = export('service-disk-changed-not-reloaded', 'org.freedesktop.systemd1.Service', path)
            systemctl('daemon-reload')
            after_u, p5 = export('unit-after-reload', 'org.freedesktop.systemd1.Unit', path)
            after_s, p6 = export('service-after-reload', 'org.freedesktop.systemd1.Service', path)
            last = proc(first_peer[0])
            exited = bool(select.poll().poll(0)) if False else None
            poller = select.poll(); poller.register(fd, select.POLLIN); exited = bool(poller.poll(0))
            peers = [first_peer, p1, p2, p3, p4, p5, p6]
            result['manager'] = {'first': first, 'last': last, 'peer_credentials': first_peer,
                                 'pidfd_readable_at_end': exited, 'socket': str(PRIVATE)}
            record('same_upstream_peer_credentials', 'observed' if all(p == first_peer for p in peers) else 'failed', peers)
            record('peer_uid_is_caller', 'observed' if first_peer[1] == os.getuid() else 'failed')
            record('same_process_incarnation_bracket', 'observed' if first == last and not exited else 'failed')
            # busctl JSON is stored only as hashes and selected properties; no ambient values leak.
            def props(v):
                d = v['data'][0]
                return d if isinstance(d, dict) else dict(d)
            bu, du, au = map(props, (before_u, disk_u, after_u))
            bs, ds, ass = map(props, (before_s, disk_s, after_s))
            result['property_names'] = {'unit': sorted(bu), 'service': sorted(bs)}
            for key, versions in {'Description': (bu, du, au), 'NeedDaemonReload': (bu, du, au),
                                  'Environment': (bs, ds, ass), 'ExecStart': (bs, ds, ass)}.items():
                result.setdefault('selected_properties', {})[key] = [v.get(key) for v in versions]
            desc = [str(v.get('Description')) for v in (bu, du, au)]
            env = [str(v.get('Environment')) for v in (bs, ds, ass)]
            record('loaded_definition_changes_only_after_reload',
                   'observed' if 'before' in desc[0] and 'before' in desc[1] and 'after' in desc[2]
                   and 'before' in env[0] and 'before' in env[1] and 'after' in env[2] else 'failed')
            record('complete_effective_definition', 'unknown', 'GetAll coverage, inherited environment, routes, parser profile and independent verifier unproved')
            record('authenticated_manager_epoch', 'unknown', 'peer/process bracket alone does not prove restart/namespace and issuer-barrier requirements')
        finally:
            os.close(fd)
    finally:
        try:
            if drop.exists():
                (drop / 'override.conf').unlink(missing_ok=True); drop.rmdir()
            unit.unlink(missing_ok=True)
            systemctl('daemon-reload')
            result['cleanup']['fixture'] = 'removed_and_reloaded'
        except Exception as exc:
            result['cleanup']['error'] = repr(exc)
            raise

try:
    main()
except Exception as exc:
    result['errors'].append({'type': type(exc).__name__, 'message': str(exc), 'traceback': traceback.format_exc()})
    write_result()
    raise
else:
    write_result()
    if any(x['state'] == 'failed' for x in result['predicates'].values()):
        raise SystemExit(1)
