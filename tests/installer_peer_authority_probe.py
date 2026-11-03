#!/usr/bin/env python3
"""Non-authoritative systemd peer-connection/loaded-definition discriminator.

All manager calls use one direct AF_UNIX connection and the native sd-bus peer
API. The peer credentials come from that exact socket. No lifecycle authority.
"""
import ctypes as C
import ctypes.util
import hashlib
import json
import os
import pathlib
import select
import socket
import struct
import subprocess
import traceback

ROOT = pathlib.Path(os.environ['PROOF_ROOT'])
HOME = pathlib.Path(os.environ['HOME'])
OUT = ROOT / 'result.json'
UNIT = 'astrid-identity-discriminator.service'
PRIVATE = pathlib.Path(os.environ['XDG_RUNTIME_DIR']) / 'systemd/private'
MANAGER = '/org/freedesktop/systemd1'
IFACE = 'org.freedesktop.systemd1.Manager'
SERVICE = 'org.freedesktop.systemd1.Service'
UNIT_IFACE = 'org.freedesktop.systemd1.Unit'
result = {'authority': False, 'uid': os.getuid(), 'euid': os.geteuid(),
          'predicates': {name: {'state': 'unknown', 'detail': 'not completed'} for name in (
              'peer_uid_is_caller', 'same_process_incarnation_bracket',
              'loaded_definition_changes_only_after_reload',
              'complete_effective_definition', 'authenticated_manager_epoch')},
          'calls': [], 'errors': [], 'cleanup': {}}

def record(name, value, detail=None):
    result['predicates'][name] = {'state': value, 'detail': detail}

def proc(pid):
    p = pathlib.Path('/proc') / str(pid)
    def observe(fn):
        try:
            return {'state': 'known', 'value': fn()}
        except (OSError, ValueError, IndexError) as exc:
            return {'state': 'unknown', 'error': type(exc).__name__,
                    'errno': getattr(exc, 'errno', None)}
    def ticks():
        stat = (p / 'stat').read_text()
        return int(stat[stat.rfind(')') + 2:].split()[19])
    return {'pid': pid, 'start_ticks': observe(ticks),
            'boot_id': observe(lambda: pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip()),
            'pidns_inode': observe(lambda: (p / 'ns/pid').stat().st_ino),
            'userns_inode': observe(lambda: (p / 'ns/user').stat().st_ino),
            'exe': observe(lambda: os.readlink(p / 'exe')),
            'uid': observe(lambda: p.stat().st_uid)}

lib = C.CDLL(ctypes.util.find_library('systemd'), use_errno=True)
libc = C.CDLL(None)
void = C.c_void_p
string = C.c_char_p

def declare(name, args, ret=C.c_int):
    fn = getattr(lib, name); fn.argtypes = args; fn.restype = ret; return fn

new = declare('sd_bus_new', [C.POINTER(void)])
set_fd = declare('sd_bus_set_fd', [void, C.c_int, C.c_int])
set_bus_client = declare('sd_bus_set_bus_client', [void, C.c_int])
start = declare('sd_bus_start', [void])
call_method = declare('sd_bus_call_method', [void, string, string, string, string, void, C.POINTER(void), string])
read_message = declare('sd_bus_message_read', [void, string])
dump_message = declare('sd_bus_message_dump', [void, void, C.c_uint64])
message_unref = declare('sd_bus_message_unref', [void], void)
bus_unref = declare('sd_bus_unref', [void], void)
get_string = declare('sd_bus_get_property_string', [void, string, string, string, string, void, C.POINTER(void)])
get_strv = declare('sd_bus_get_property_strv', [void, string, string, string, string, void, C.POINTER(void)])
libc.open_memstream.argtypes = [C.POINTER(void), C.POINTER(C.c_size_t)]; libc.open_memstream.restype = void
libc.fclose.argtypes = [void]; libc.fclose.restype = C.c_int
libc.free.argtypes = [void]; libc.free.restype = None

def check(label, rc):
    result['calls'].append({'label': label, 'exit': 0 if rc >= 0 else -rc, 'sd_bus_return': rc})
    if rc < 0:
        raise OSError(-rc, label + ': ' + os.strerror(-rc))

def b(value):
    return None if value is None else value.encode()

class Peer:
    def __init__(self, path, bus_client=False, destination=None):
        self.bus = void(); self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(str(path))
        self.peer = struct.unpack('3i', self.sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize('3i')))
        self.identity_socket = self.sock.dup()
        self.destination = destination
        check('sd_bus_new', new(C.byref(self.bus)))
        if bus_client:
            check('sd_bus_set_bus_client', set_bus_client(self.bus, 1))
        fd = self.sock.detach()
        check('sd_bus_set_fd', set_fd(self.bus, fd, fd))
        check('sd_bus_start', start(self.bus))
    def close(self):
        bus_unref(self.bus); self.identity_socket.close()
    def method(self, label, path, interface, member, signature, value):
        reply = void()
        rc = call_method(self.bus, b(self.destination), b(path), b(interface), b(member), None,
                         C.byref(reply), b(signature), b(value))
        check(label, rc)
        return reply
    def load(self, name):
        reply = self.method('LoadUnit', MANAGER, IFACE, 'LoadUnit', 's', name)
        try:
            path = string()
            check('LoadUnit.read_object_path', read_message(reply, b('o'), C.byref(path)))
            if not path.value:
                raise ValueError('empty object path')
            return path.value.decode()
        finally:
            message_unref(reply)
    def export(self, label, path, interface):
        reply = self.method(label, path, 'org.freedesktop.DBus.Properties', 'GetAll', 's', interface)
        buffer = void(); size = C.c_size_t(); stream = libc.open_memstream(C.byref(buffer), C.byref(size))
        if not stream:
            message_unref(reply); raise OSError('open_memstream failed')
        try:
            rc = dump_message(reply, stream, 0)
        finally:
            libc.fclose(stream); message_unref(reply)
        try:
            check(label + '.decode_dump', rc)
            data = C.string_at(buffer, size.value)
            name = 'export-' + label + '.txt'
            (ROOT / name).write_bytes(data)
            result['calls'][-1].update({'decoded_bytes': len(data), 'decoded_sha256': hashlib.sha256(data).hexdigest(),
                                        'artifact': name})
        finally:
            libc.free(buffer)
    def string_property(self, label, path, interface, prop):
        ptr = void()
        check(label, get_string(self.bus, b(self.destination), b(path), b(interface), b(prop), None, C.byref(ptr)))
        try:
            return C.string_at(ptr).decode()
        finally:
            libc.free(ptr)
    def strv_property(self, label, path, interface, prop):
        ptr = void()
        check(label, get_strv(self.bus, b(self.destination), b(path), b(interface), b(prop), None, C.byref(ptr)))
        try:
            values = []
            if ptr.value:
                array = C.cast(ptr, C.POINTER(void)); i = 0
                while array[i]:
                    values.append(C.string_at(array[i]).decode()); libc.free(array[i]); i += 1
            return values
        finally:
            libc.free(ptr)

def systemctl(*args):
    done = subprocess.run(['systemctl', '--user', *args], capture_output=True, text=True, timeout=25)
    result['calls'].append({'label': 'systemctl ' + ' '.join(args), 'exit': done.returncode, 'stderr': done.stderr[:500]})
    if done.returncode:
        raise RuntimeError('systemctl failed: ' + done.stderr[:500])

def write_result():
    OUT.write_text(json.dumps(result, sort_keys=True, indent=2) + '\n')

def system_manager_pid(label):
    cmd = ['systemctl', '--system', 'show', f'user@{os.getuid()}.service', '--property=MainPID', '--value']
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        result['calls'].append({'label': label, 'exit': done.returncode, 'stdout': done.stdout.strip(), 'stderr': done.stderr[:500]})
        return int(done.stdout.strip()) if done.returncode == 0 and done.stdout.strip().isdigit() and int(done.stdout.strip()) > 0 else None
    except (OSError, subprocess.TimeoutExpired) as exc:
        result['calls'].append({'label': label, 'exit': None, 'error': repr(exc)})
        return None

def system_bus_peer():
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.connect('/run/dbus/system_bus_socket')
            return list(struct.unpack('3i', sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize('3i'))))
    except OSError as exc:
        return {'error': type(exc).__name__, 'errno': exc.errno}

def basic_proc(pid):
    # Never read protected executable or namespace links. These observations do
    # not identify an image or prove a namespace, provenance, or custody.
    def read(path):
        try: return {'value': pathlib.Path(path).read_text().strip()}
        except OSError as exc: return {'error': type(exc).__name__, 'errno': exc.errno}
    return {'stat': read(f'/proc/{pid}/stat'), 'cgroup': read(f'/proc/{pid}/cgroup')}

def negative_peer(expected_pid):
    path = ROOT / 'wrong-peer.sock'
    code = "import socket,sys,time; s=socket.socket(socket.AF_UNIX); s.bind(sys.argv[1]); s.listen(1); print('ready',flush=True); c,_=s.accept(); time.sleep(60)"
    child = subprocess.Popen([os.sys.executable, '-c', code, str(path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    conn = None; fd = None
    try:
        ready = child.stdout.readline().strip()
        if ready != 'ready': raise RuntimeError('wrong-peer listener did not start')
        conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); conn.connect(str(path))
        cred = struct.unpack('3i', conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize('3i')))
        fd = os.pidfd_open(cred[0]); poller = select.poll(); poller.register(fd, select.POLLIN)
        before = bool(poller.poll(0))
        wrong = {'peer_credentials': list(cred), 'spawned_pid': child.pid,
                 'same_uid': cred[1] == os.getuid(), 'different_from_system_manager_pid': None if expected_pid is None else cred[0] != expected_pid,
                 'pidfd_readable_before_death': before}
        child.terminate(); child.wait(timeout=10)
        wrong['child_exit'] = child.returncode
        wrong['pidfd_readable_after_death'] = bool(poller.poll(1000))
        wrong['retained_socket_peer_after_death'] = list(struct.unpack('3i', conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize('3i'))))
        result['wrong_peer_control'] = wrong
        good = cred[0] == child.pid and wrong['same_uid'] and expected_pid is not None and wrong['different_from_system_manager_pid'] and not before and wrong['pidfd_readable_after_death']
        record('wrong_peer_and_death_control', 'observed' if good else ('unknown' if expected_pid is None else 'failed'))
    finally:
        if child.poll() is None: child.kill(); child.wait()
        if conn: conn.close()
        if fd is not None: os.close(fd)
        path.unlink(missing_ok=True)

def main():
    if os.getuid() == 0 or os.getuid() != os.geteuid():
        raise RuntimeError('requires equal real/effective non-root uid')
    names = ('astrid-peer-expected.service', 'astrid-peer-changed.service')
    unit_dir = HOME / '.config/systemd/user'; unit_dir.mkdir(parents=True, exist_ok=True)
    paths = [unit_dir / x for x in names]
    if any(p.exists() or p.is_symlink() for p in paths): raise RuntimeError('fixture already exists')
    expected = 'Peer discriminator expected'; changed = 'Peer discriminator changed'
    try:
        for path, description, marker in zip(paths, (expected, changed), ('expected', 'changed')):
            path.write_text(f'[Unit]\nDescription={description}\n[Service]\nType=oneshot\nExecStart=/usr/bin/true\nEnvironment=ASTRID_PEER_MARKER={marker}\n')
        result['system_bus_peer_start'] = system_bus_peer()
        oracle_before = system_manager_pid('system_manager_MainPID_before')
        peer = Peer(PRIVATE)
        try:
            cred = peer.peer
            result['manager'] = {'peer_credentials': list(cred), 'basic_proc_before': basic_proc(cred[0])}
            fd = None
            try:
                fd = os.pidfd_open(cred[0]); poller = select.poll(); poller.register(fd, select.POLLIN)
                result['manager']['pidfd_readable_before'] = bool(poller.poll(0))
            except OSError as exc:
                result['manager']['pidfd_error'] = {'errno': exc.errno, 'type': type(exc).__name__}
            try:
                values = []
                for index, name in enumerate(names):
                    obj = peer.load(name)
                    peer.export(f'fixture{index}.unit.GetAll', obj, UNIT_IFACE)
                    peer.export(f'fixture{index}.service.GetAll', obj, SERVICE)
                    values.append({'name': name, 'object_path': obj,
                        'Description': peer.string_property(f'fixture{index}.Description', obj, UNIT_IFACE, 'Description'),
                        'Environment': peer.strv_property(f'fixture{index}.Environment', obj, SERVICE, 'Environment')})
                result['fixtures'] = values
                control = (values[0]['Description'] == expected and values[1]['Description'] == changed and
                           'ASTRID_PEER_MARKER=expected' in values[0]['Environment'] and 'ASTRID_PEER_MARKER=changed' in values[1]['Environment'] and
                           values[1]['Description'] != expected and 'ASTRID_PEER_MARKER=expected' not in values[1]['Environment'])
                record('changed_loaded_definition_control', 'observed' if control else 'failed', 'two separately loaded static fixture definitions; no reload ordering claim')
                oracle_after = system_manager_pid('system_manager_MainPID_after')
                end_cred = struct.unpack('3i', peer.identity_socket.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize('3i')))
                result['system_bus_peer_end'] = system_bus_peer()
                result['manager'].update({'peer_credentials_end': list(end_cred), 'basic_proc_after': basic_proc(cred[0]), 'system_manager_pid_before': oracle_before, 'system_manager_pid_after': oracle_after,
                                          'pidfd_readable_after': bool(poller.poll(0)) if fd is not None else None})
                if oracle_before is None or oracle_after is None or fd is None:
                    state = 'unknown'
                else:
                    state = 'observed' if oracle_before == cred[0] == oracle_after and cred == end_cred and cred[1] == os.getuid() and not poller.poll(0) else 'failed'
                record('peer_pid_matches_system_manager_readback', state, 'system bus peer provenance and query-channel binding are not authenticated; pidfd_open follows SO_PEERCRED and has an acquisition race')
                negative_peer(oracle_before)
            finally:
                if fd is not None: os.close(fd)
        finally: peer.close()
    finally:
        for path in paths: path.unlink(missing_ok=True)
        result['cleanup']['fixture_files_removed'] = all(not p.exists() for p in paths)
    record('authenticated_manager_epoch', 'unknown', 'no independently authenticated system-bus image/namespace, process image or acquisition-race-free peer pidfd binding; no Runtime verifier or issuer barrier')
    record('complete_effective_definition', 'unknown', 'GetAll text dumps and selected fixture properties do not establish complete decoded semantics, inherited environment, principal, routes or loaded/disk equivalence')

if __name__ == '__main__':
    result['predicates'] = {k: {'state': 'unknown', 'detail': 'not completed'} for k in ('peer_pid_matches_system_manager_readback', 'wrong_peer_and_death_control', 'changed_loaded_definition_control', 'authenticated_manager_epoch', 'complete_effective_definition')}
    try:
        main()
    except Exception as exc:
        result['errors'].append({'type': type(exc).__name__, 'message': str(exc), 'traceback': traceback.format_exc()})
    finally:
        write_result()
    # Unknown authority is intentionally a failing diagnostic, not admission.
    raise SystemExit(1 if result['errors'] or any(x['state'] != 'observed' for x in result['predicates'].values()) else 0)
