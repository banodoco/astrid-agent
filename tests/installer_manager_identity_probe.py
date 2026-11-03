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
    stat = (p / 'stat').read_text()
    fields = stat[stat.rfind(')') + 2:].split()
    return {'pid': pid, 'start_ticks': int(fields[19]),
            'boot_id': pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
            'pidns_inode': (p / 'ns/pid').stat().st_ino,
            'userns_inode': (p / 'ns/user').stat().st_ino,
            'exe': os.readlink(p / 'exe'), 'uid': p.stat().st_uid}

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

def main():
    if os.getuid() == 0 or os.getuid() != os.geteuid():
        raise RuntimeError('requires equal real/effective non-root uid')
    unit_dir = HOME / '.config/systemd/user'; unit_dir.mkdir(parents=True, exist_ok=True)
    unit = unit_dir / UNIT; drop = unit_dir / (UNIT + '.d')
    if unit.exists() or drop.exists():
        raise RuntimeError('fixture already exists')
    unit.write_text('[Unit]\nDescription=Identity discriminator before\n[Service]\nType=oneshot\nExecStart=/usr/bin/true\nEnvironment=ASTRID_IDENTITY_PROBE=before\n')
    try:
        systemctl('daemon-reload')
        peer = Peer(PRIVATE)
        try:
            peer_id = peer.peer; first = proc(peer_id[0]); fd = os.pidfd_open(peer_id[0])
            result['manager'] = {'peer_credentials': peer_id, 'first': first, 'socket': str(PRIVATE)}
            record('peer_uid_is_caller', 'observed' if peer_id[1] == os.getuid() else 'failed')
            try:
                path = peer.load(UNIT); result['unit_object_path'] = path
                def snapshot(label):
                    peer.export(label + '.unit.GetAll', path, UNIT_IFACE)
                    peer.export(label + '.service.GetAll', path, SERVICE)
                    return {'Description': peer.string_property(label + '.Description', path, UNIT_IFACE, 'Description'),
                            'Environment': peer.strv_property(label + '.Environment', path, SERVICE, 'Environment'),
                            'User': peer.string_property(label + '.User', path, SERVICE, 'User'),
                            'Group': peer.string_property(label + '.Group', path, SERVICE, 'Group'),
                            'WorkingDirectory': peer.string_property(label + '.WorkingDirectory', path, SERVICE, 'WorkingDirectory')}
                versions = [snapshot('before')]
                drop.mkdir()
                (drop / 'override.conf').write_text('[Unit]\nDescription=Identity discriminator after\n[Service]\nEnvironment=ASTRID_IDENTITY_PROBE=after\n')
                versions.append(snapshot('disk_changed_before_reload'))
                systemctl('daemon-reload')
                versions.append(snapshot('after_reload'))
                result['selected_properties'] = versions
                last = proc(peer_id[0]); poller = select.poll(); poller.register(fd, select.POLLIN)
                exited = bool(poller.poll(0))
                end_peer = struct.unpack('3i', peer.identity_socket.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize('3i')))
                result['manager'].update({'last': last, 'peer_credentials_end': end_peer, 'pidfd_readable_at_end': exited})
                record('same_process_incarnation_bracket', 'observed' if first == last and peer_id == end_peer and not exited else 'failed')
                descriptions = [v['Description'] for v in versions]
                envs = [v['Environment'] for v in versions]
                passed = descriptions == ['Identity discriminator before', 'Identity discriminator before', 'Identity discriminator after']
                passed = passed and 'ASTRID_IDENTITY_PROBE=before' in envs[0] and 'ASTRID_IDENTITY_PROBE=before' in envs[1] and 'ASTRID_IDENTITY_PROBE=after' in envs[2]
                record('loaded_definition_changes_only_after_reload', 'observed' if passed else 'failed')
                record('complete_effective_definition', 'unknown', 'GetAll coverage, inherited environment, routes, parser profile and independent verifier unproved')
                record('authenticated_manager_epoch', 'unknown', 'peer/process bracket alone does not prove restart/namespace and issuer-barrier requirements')
            finally:
                os.close(fd)
        finally:
            peer.close()
    finally:
        try:
            if drop.exists():
                (drop / 'override.conf').unlink(missing_ok=True); drop.rmdir()
            unit.unlink(missing_ok=True); systemctl('daemon-reload')
            result['cleanup']['fixture'] = 'removed_and_reloaded'
        except Exception as exc:
            result['cleanup']['error'] = repr(exc); raise

if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        result['errors'].append({'type': type(exc).__name__, 'message': str(exc), 'traceback': traceback.format_exc()})
        write_result(); raise
    else:
        write_result()
        if any(x['state'] == 'failed' for x in result['predicates'].values()):
            raise SystemExit(1)
