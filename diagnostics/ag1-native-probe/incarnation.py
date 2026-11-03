"""Same-run Linux observations only; never an admission or custody decision."""
import hashlib
import json
import os
from pathlib import Path
import select
import threading
import time
from urllib.parse import urlsplit


def alive(fd):
    poll = select.poll()
    poll.register(fd, select.POLLIN | select.POLLHUP | select.POLLERR)
    return not poll.poll(0)


def process(pid):
    p = Path('/proc') / str(pid)
    fields = (p / 'stat').read_text().rsplit(')', 1)[1].split()
    return {'pid': pid, 'uid': p.stat().st_uid, 'state': fields[0], 'start_ticks': fields[19],
            'pidns': (p / 'ns/pid').stat().st_ino,
            'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
            'cgroup': (p / 'cgroup').read_text(),
            'cmdline': (p / 'cmdline').read_bytes().replace(b'\0', b' ').decode(errors='replace')}


def file_record(path):
    # Evidence is diagnostic, with an isolated same-user trust boundary.
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        st = os.fstat(fd)
        chunks = []
        while True:
            part = os.read(fd, 65536)
            if not part:
                break
            chunks.append(part)
        raw = b''.join(chunks)
        return {'device': st.st_dev, 'inode': st.st_ino, 'sha256': hashlib.sha256(raw).hexdigest(),
                'value': json.loads(raw)}
    finally:
        os.close(fd)


def snapshot(pid, fd, data):
    start = time.monotonic_ns()
    if not alive(fd):
        raise RuntimeError('held pidfd reports process exited')
    before = process(pid)
    disc_path = data / 'runtime/discovery.json'
    discovery = file_record(disc_path)
    marker = file_record(data / 'runtime/instance.lock')
    catalog = file_record(data / 'runtime/catalog.json')
    d, m, c = discovery['value'], marker['value'], catalog['value']
    realms = c['realms']
    if len(realms) != 1:
        raise ValueError('selected catalog is not exactly one realm')
    realm = realms[0]
    root = Path(realm['data_root'])
    lock = root / 'owner.lock'
    st = lock.stat(follow_symlinks=False)
    p = urlsplit(d['endpoint'])
    if p.scheme != 'http' or p.hostname != '127.0.0.1' or p.port is None or p.path not in ('', '/'):
        raise ValueError('endpoint is not R0 IPv4 loopback')
    addr = f'0100007F:{p.port:04X}'
    sockets = []
    for line in (Path('/proc') / str(pid) / 'net/tcp').read_text().splitlines()[1:]:
        row = line.split()
        if row[1] == addr and row[3] == '0A':
            sockets.append(int(row[9]))
    fds = []
    for entry in (Path('/proc') / str(pid) / 'fd').iterdir():
        try:
            stat = entry.stat()
            fds.append({'fd': int(entry.name), 'device': stat.st_dev, 'inode': stat.st_ino,
                        'target': os.readlink(entry)})
        except FileNotFoundError:
            continue
    lock_id = f'{os.major(st.st_dev):02x}:{os.minor(st.st_dev):02x}:{st.st_ino}'
    lock_rows = [line for line in Path('/proc/locks').read_text().splitlines()
                 if len(line.split()) >= 8 and line.split()[1:5] == ['FLOCK', 'ADVISORY', 'WRITE', str(pid)]
                 and line.split()[5].lower() == lock_id.lower() and line.split()[6:8] == ['0', 'EOF']]
    after = process(pid)
    discovery_again = file_record(disc_path)
    checks = {
        'pidfd_alive_at_end': alive(fd), 'process_identity_stable': before == after,
        'publication_stable': discovery == discovery_again,
        'pid_and_birth': type(d['pid']) is int and d['pid'] == pid and d['process_birth_id'] == 'proc-start-ticks:' + before['start_ticks'],
        'marker_binding': all(m.get(a) == d.get(b) for a, b in [('pid','pid'), ('process_birth_id','process_birth_id'), ('runtime_instance_id','runtime_instance_id'), ('realm_id','active_realm'), ('realm_root','realm_root')]),
        'selected_realm': bool(d['active_realm']) and c['selected_realm_id'] == realm['realm_id'] == d['active_realm'] and str(root) == d['realm_root'],
        'listener_fd': len(sockets) == 1 and any(f['target'] == f'socket:[{sockets[0]}]' for f in fds),
        'owner_lock_fd': any((f['device'], f['inode']) == (st.st_dev, st.st_ino) for f in fds),
        'owner_lock_flock': len(lock_rows) == 1,
    }
    return {'start_monotonic_ns': start, 'end_monotonic_ns': time.monotonic_ns(),
            'process': before, 'discovery': discovery, 'marker': marker, 'catalog': catalog,
            'listener_inodes': sockets, 'owner_lock': {'path': str(lock), 'device': st.st_dev, 'inode': st.st_ino, 'proc_locks': lock_rows},
            'fds': fds, 'checks': checks, 'all_checks': all(checks.values())}


class Witness:
    def __init__(self, data, program):
        self.data, self.program = data, program
        self.fd = None
        self.pid = None
        self.initial = None
        self.error = None
        self.stop = threading.Event()
        if data.exists() or data.is_symlink():
            raise RuntimeError('disposable data root already existed before installation')
        self.thread = threading.Thread(target=self.watch, daemon=True)
        self.thread.start()

    def watch(self):
        while not self.stop.is_set():
            for proc in Path('/proc').iterdir():
                if not proc.name.isdigit():
                    continue
                try:
                    pid = int(proc.name)
                    info = process(pid)
                    if info['uid'] != os.getuid() or '/astrid.service' not in info['cgroup']:
                        continue
                    if str(self.program) not in info['cmdline'] or 'from astrid_runtime.cli import main' not in info['cmdline']:
                        continue
                    fd = os.pidfd_open(pid)
                    # pidfd is already retained when absence is checked.
                    if not alive(fd) or (self.data / 'runtime/discovery.json').exists():
                        os.close(fd)
                        self.error = 'process exited or first discovery check was too late'
                        return
                    again = process(pid)
                    if info != again:
                        os.close(fd)
                        self.error = 'process identity changed during pidfd acquisition'
                        return
                    self.pid, self.fd = pid, fd
                    self.initial = {'process': again, 'discovery_absent_after_pidfd': True,
                                    'monotonic_ns': time.monotonic_ns()}
                    return
                except (OSError, ValueError, IndexError):
                    continue
            self.stop.wait(0.01)

    def capture(self):
        self.thread.join(timeout=1)
        if self.fd is None:
            raise RuntimeError(self.error or 'no pre-publication process handle captured')
        return snapshot(self.pid, self.fd, self.data)

    def close(self):
        self.stop.set()
        self.thread.join(timeout=2)
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
