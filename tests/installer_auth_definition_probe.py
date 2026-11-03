#!/usr/bin/env python3
"""Non-authoritative changed-input AG1 authentication/definition discriminator.

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
import time
import sys
import fcntl

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

# A diagnostic lossless typed traversal, not a reviewed authority semantic profile.
peek = declare('sd_bus_message_peek_type', [void, C.POINTER(C.c_char), C.POINTER(string)])
enter = declare('sd_bus_message_enter_container', [void, C.c_char, string])
leave = declare('sd_bus_message_exit_container', [void])
get_signature = declare('sd_bus_message_get_signature', [void, C.c_int], string)

def decode(message, depth=0, budget=None):
    if budget is None:
        budget = [100000]
    if depth > 32 or budget[0] <= 0:
        raise ValueError('decoder bound exceeded')
    budget[0] -= 1
    kind = C.c_char(); contents = string()
    rc = peek(message, C.byref(kind), C.byref(contents))
    if rc < 0:
        raise OSError(-rc, 'peek')
    if rc == 0:
        return None
    t = kind.value.decode(); c = contents.value.decode() if contents.value else ''
    if t in ('a', 'r', 'e', 'v'):
        check('decode.enter.' + t, enter(message, kind, contents))
        children = []
        while True:
            child = decode(message, depth + 1, budget)
            if child is None:
                break
            children.append(child)
        check('decode.leave.' + t, leave(message))
        return {'type': t, 'contents': c, 'value': children}
    types = {'s': string, 'o': string, 'g': string, 'b': C.c_int,
             'y': C.c_uint8, 'n': C.c_int16, 'q': C.c_uint16,
             'i': C.c_int32, 'u': C.c_uint32, 'x': C.c_int64,
             't': C.c_uint64, 'd': C.c_double}
    if t not in types:
        raise ValueError('unsupported native type ' + t)
    value = types[t]()
    check('decode.read.' + t, read_message(message, b(t), C.byref(value)))
    v = value.value
    if t in ('s', 'o', 'g'):
        v = v.decode('utf-8', 'strict')
    return {'type': t, 'value': bool(v) if t == 'b' else v}

def typed_export(peer, label, obj, interface, persist=True):
    reply = peer.method(label, obj, 'org.freedesktop.DBus.Properties', 'GetAll', 's', interface)
    try:
        if get_signature(reply, 1) != b'a{sv}':
            raise ValueError('unexpected GetAll signature')
        tree = decode(reply)
        if decode(reply) is not None:
            raise ValueError('trailing GetAll values')
    finally:
        message_unref(reply)
    # Strictly validate envelope and duplicate keys; preserve every variant type.
    if tree['type'] != 'a' or tree['contents'] != '{sv}':
        raise ValueError('invalid GetAll envelope')
    props = {}
    for entry in tree['value']:
        key, variant = entry['value']
        if entry['type'] != 'e' or entry['contents'] != 'sv' or key['type'] != 's' or variant['type'] != 'v' or len(variant['value']) != 1 or key['value'] in props:
            raise ValueError('invalid/duplicate native property')
        props[key['value']] = variant
    raw = json.dumps(tree, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    if len(raw) > 2 * 1024 * 1024:
        raise ValueError('export size bound exceeded')
    if persist:
        path = ROOT / ('export-' + label + '.json')
        path.write_bytes(raw + b'\n')
        result.setdefault('exports', {})[label] = {'properties': len(props), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
    return props

def exact_system_bus_query():
    system = Peer('/run/dbus/system_bus_socket', bus_client=True, destination='org.freedesktop.systemd1')
    try:
        info = identity(system.identity_socket)
        reply = system.method('system.GetUnit', MANAGER, IFACE, 'GetUnit', 's', f'user@{os.getuid()}.service')
        try:
            path = string()
            check('system.GetUnit.path', read_message(reply, b'o', C.byref(path)))
            obj = path.value.decode()
        finally:
            message_unref(reply)
        props = typed_export(system, 'system_user_manager.service', obj, SERVICE, persist=False)
        # Export only the process identifier: the system service can have a private environment.
        return {'peer': info, 'MainPID': value(props, 'MainPID'), 'authority': False}
    finally:
        system.close()

def value(props, name):
    variant = props[name]
    def unwrap(node):
        if node['type'] in ('a', 'r', 'e', 'v'):
            return [unwrap(child) for child in node['value']]
        return node['value']
    return unwrap(variant['value'][0])

def observed(label, condition, detail=None):
    record(label, 'observed' if condition else 'failed', detail)

def safe(fn):
    try:
        return {'state': 'observed', 'value': fn()}
    except (OSError, ValueError) as exc:
        return {'state': 'unknown', 'error': type(exc).__name__, 'errno': getattr(exc, 'errno', None)}

def readable(fd):
    poll = select.poll(); poll.register(fd, select.POLLIN)
    return bool(poll.poll(100))

def fd_pid(fd):
    data = pathlib.Path(f'/proc/self/fdinfo/{fd}').read_text()
    return int(next(line.split()[1] for line in data.splitlines() if line.startswith('Pid:')))

def socket_pidfd(sock):
    # Linux asm-generic ABI (x86_64/aarch64 runner): SO_PEERPIDFD = 77.
    # No fallback to pidfd_open(numeric SO_PEERCRED PID).
    fd = struct.unpack('i', sock.getsockopt(socket.SOL_SOCKET, 77, 4))[0]
    os.set_inheritable(fd, False)
    return fd

def identity(sock):
    credentials = list(struct.unpack('3i', sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)))
    out = {'credentials': credentials, 'proc': proc(credentials[0])}
    fd = None
    try:
        fd = socket_pidfd(sock)
        out['socket_pidfd'] = {'state': 'observed', 'pid': fd_pid(fd), 'readable': readable(fd)}
        # pidfd namespace ioctls exist on newer kernels, but may require ptrace access.
        # Linux PIDFD_GET_PID_NAMESPACE = _IO(0xff, 5), USER_NAMESPACE = _IO(0xff, 9).
        for name, nr in (('pid_namespace_ioctl', 5), ('user_namespace_ioctl', 9)):
            def ns_inode():
                nsfd = fcntl.ioctl(fd, (0xff << 8) | nr)
                try:
                    return os.fstat(nsfd).st_ino
                finally:
                    os.close(nsfd)
            out[name] = safe(ns_inode)
    except OSError as exc:
        out['socket_pidfd'] = {'state': 'unknown', 'error': type(exc).__name__, 'errno': exc.errno}
    finally:
        if fd is not None:
            os.close(fd)
    return out

def replacement_control():
    path = ROOT / 'replace.sock'
    code = "import socket,sys; s=socket.socket(socket.AF_UNIX); s.bind(sys.argv[1]); s.listen(2); print('ready',flush=True); c,_=s.accept(); print('accepted',flush=True); sys.stdin.read()"
    children = []; sockets = []; fds = []
    try:
        def connect():
            child = subprocess.Popen([sys.executable, '-c', code, str(path)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
            children.append(child)
            if child.stdout.readline().strip() != 'ready':
                raise ValueError('listener not ready')
            sock = socket.socket(socket.AF_UNIX); sock.settimeout(10); sock.connect(str(path)); sockets.append(sock)
            if child.stdout.readline().strip() != 'accepted':
                raise ValueError('listener not connected')
            fd = socket_pidfd(sock); fds.append(fd)
            return child, sock, fd
        first, old, oldfd = connect()
        before = fd_pid(oldfd) == first.pid and not readable(oldfd)
        first.terminate(); first.wait(timeout=10); path.unlink()
        second, new_sock, newfd = connect()
        # Re-query the old connected socket after pathname replacement: it must not bind the new listener.
        reacquired = safe(lambda: socket_pidfd(old))
        old_requery = None
        if reacquired['state'] == 'observed':
            fd = reacquired['value']; fds.append(fd)
            old_requery = {'pid': fd_pid(fd), 'readable': readable(fd)}
        data = {'first_pid': first.pid, 'second_pid': second.pid,
                'old_dead_readable': readable(oldfd), 'old_pid_after_death': fd_pid(oldfd),
                'new_pid': fd_pid(newfd), 'new_readable': readable(newfd),
                'old_socket_requery': old_requery, 'requery_error': None if old_requery else reacquired}
        result['replacement_control'] = data
        observed('socket_pidfd_replacement_and_death', before and readable(oldfd) and fd_pid(newfd) == second.pid and not readable(newfd) and first.pid != second.pid and (old_requery is None or old_requery['pid'] != second.pid))
    except OSError as exc:
        record('socket_pidfd_replacement_and_death', 'unknown', {'errno': exc.errno, 'error': type(exc).__name__})
    finally:
        for child in children:
            if child.poll() is None:
                child.terminate(); child.wait(timeout=10)
        for fd in fds:
            os.close(fd)
        for sock in sockets:
            sock.close()
        path.unlink(missing_ok=True)

def command(label, argv, required=True):
    done = subprocess.run(argv, capture_output=True, timeout=30)
    result['calls'].append({'label': label, 'argv': argv, 'exit': done.returncode,
                            'stderr_sha256': hashlib.sha256(done.stderr).hexdigest()})
    if required and done.returncode:
        raise RuntimeError(label + ' failed')
    return done

def ctl(*args, required=True):
    return command('systemctl ' + ' '.join(args), ['systemctl', '--user', *args], required)

def snapshot(peer, obj, label):
    u = typed_export(peer, label + '.unit', obj, UNIT_IFACE)
    s = typed_export(peer, label + '.service', obj, SERVICE)
    selected = {key: value(u if key in u else s, key) for key in (
        'Description', 'ActiveState', 'FragmentPath', 'DropInPaths', 'NeedDaemonReload',
        'Environment', 'EnvironmentFiles', 'User', 'Group', 'SupplementaryGroups',
        'ExecStart', 'WorkingDirectory', 'TriggeredBy')}
    result.setdefault('snapshots', {})[label] = selected
    return selected

def separate_selected_readback(obj):
    other = Peer(PRIVATE)
    try:
        return {'peer': identity(other.identity_socket),
                'Description': other.string_property('separate.Description', obj, UNIT_IFACE, 'Description'),
                'Environment': other.strv_property('separate.Environment', obj, SERVICE, 'Environment')}
    finally:
        other.close()

def main(local_controls=False):
    if not local_controls and (os.getuid() == 0 or os.geteuid() == 0):
        raise RuntimeError('non-root required')
    replacement_control()
    if local_controls:
        return
    peer = Peer(PRIVATE)
    retained_fd = None
    try:
        retained_fd = socket_pidfd(peer.identity_socket)
        result['retained_socket_pidfd_before'] = {'pid': fd_pid(retained_fd), 'readable': readable(retained_fd)}
    except OSError as exc:
        result['retained_socket_pidfd_before'] = {'state': 'unknown', 'errno': exc.errno}
    paths = []
    unit_owned = False; drop_owned = False
    unit_name = 'astrid-auth-definition-discriminator.service'
    unit_dir = HOME / '.config/systemd/user'
    unit = unit_dir / unit_name; dropdir = unit_dir / (unit_name + '.d'); drop = dropdir / 'override.conf'
    envfile = ROOT / 'fixture.env'; capture = ROOT / 'launch.json'; script = ROOT / 'launch.py'
    try:
        result['manager_before'] = identity(peer.identity_socket)
        result['system_bus_socket'] = safe(lambda: system_socket_identity('/run/dbus/system_bus_socket'))
        result['exact_system_bus_query'] = safe(exact_system_bus_query)
        result['system_manager_private'] = safe(lambda: system_socket_identity('/run/systemd/private'))
        # Foreign-manager control uses PID 1's real native private endpoint, where available.
        foreign = result['system_manager_private']
        if foreign['state'] == 'observed':
            observed('foreign_manager_rejected_by_uid', foreign['value']['credentials'][1] != os.getuid())
        before_identity = result['manager_before']
        pfd = before_identity['socket_pidfd']
        if pfd['state'] == 'observed':
            observed('socket_bound_live_user_peer', pfd['pid'] == before_identity['credentials'][0] and not pfd['readable'] and before_identity['credentials'][1] == os.getuid())
        unit_dir.mkdir(parents=True, exist_ok=True)
        for path in (unit, dropdir, envfile, capture, script):
            if path.exists():
                raise RuntimeError('fixture collision')
        script.write_text('import os,json,pathlib,sys\npathlib.Path(sys.argv[1]).write_text(json.dumps({"marker":os.environ.get("ASTRID_FILE_MARKER"),"unit_marker":os.environ.get("ASTRID_UNIT_MARKER"),"argv":sys.argv,"uid":os.getuid(),"gid":os.getgid(),"groups":os.getgroups()},sort_keys=True)+"\\n")\n')
        paths.extend((script, capture, envfile, unit, drop))
        envfile.write_text('ASTRID_FILE_MARKER=before\n')
        unit.write_text('[Unit]\nDescription=Auth definition before\n[Service]\nType=oneshot\nRemainAfterExit=yes\nEnvironment=ASTRID_UNIT_MARKER=before\nEnvironmentFile=' + str(envfile) + '\nExecStart=/usr/bin/python3 ' + str(script) + ' ' + str(capture) + '\n')
        unit_owned = True
        ctl('daemon-reload'); ctl('start', unit_name)
        obj = peer.load(unit_name)
        before = snapshot(peer, obj, 'before')
        result['separate_readback_before'] = separate_selected_readback(obj)
        first_launch = json.loads(capture.read_text()); result['launch_before'] = first_launch
        # Unit remains active, so inactive-unit collection cannot explain this interval.
        envfile.write_text('ASTRID_FILE_MARKER=after\n')
        middle = snapshot(peer, obj, 'environment_file_changed')
        observed('environment_pointer_unchanged', before['EnvironmentFiles'] == middle['EnvironmentFiles'] and before['Environment'] == middle['Environment'] and middle['ActiveState'] == 'active')
        ctl('restart', unit_name)
        second_launch = json.loads(capture.read_text()); result['launch_after_file_change'] = second_launch
        observed('launch_environment_changed_without_reload', first_launch['marker'] == 'before' and second_launch['marker'] == 'after' and second_launch['unit_marker'] == 'before' and second_launch['uid'] == os.getuid())
        dropdir.mkdir(); drop_owned = True
        drop.write_text('[Unit]\nDescription=Auth definition after\n[Service]\nEnvironment=ASTRID_UNIT_MARKER=after\n')
        stale = snapshot(peer, obj, 'disk_dropin_without_reload')
        observed('active_loaded_definition_stale', stale['ActiveState'] == 'active' and stale['Description'] == 'Auth definition before' and 'ASTRID_UNIT_MARKER=before' in stale['Environment'])
        ctl('daemon-reload')
        after = snapshot(peer, obj, 'after_reload')
        result['separate_readback_after'] = separate_selected_readback(obj)
        observed('separate_connection_selected_readback', all(
            result['separate_readback_' + label]['Description'] == snap['Description'] and
            result['separate_readback_' + label]['Environment'] == snap['Environment'] and
            result['separate_readback_' + label]['peer']['credentials'] == before_identity['credentials']
            for label, snap in (('before', before), ('after', after))))
        observed('loaded_definition_changed_after_reload', after['Description'] == 'Auth definition after' and 'ASTRID_UNIT_MARKER=after' in after['Environment'])
        ctl('restart', unit_name)
        result['launch_after_reload'] = json.loads(capture.read_text())
        observed('effective_fixture_principal_argv', result['launch_after_reload']['unit_marker'] == 'after' and result['launch_after_reload']['uid'] == os.getuid() and result['launch_after_reload']['gid'] == os.getgid() and result['launch_after_reload']['argv'] == [str(script), str(capture)])
        envfile.chmod(0)
        unreadable = safe(lambda: envfile.read_bytes().decode())
        result['unreadable_environment_file'] = unreadable
        observed('unreadable_file_remains_unknown', unreadable['state'] == 'unknown' and unreadable.get('errno') == 13)
        envfile.chmod(0o600)
        # Query an explicitly unsupported field; preserve native failure rather than an empty value.
        ptr = void()
        rc = get_string(peer.bus, None, b(obj), b(SERVICE), b('AstridUnsupportedAuthorityField'), None, C.byref(ptr))
        if ptr.value:
            libc.free(ptr)
        result['unsupported_property'] = {'sd_bus_return': rc, 'state': 'unknown' if rc < 0 else 'unexpectedly_supported'}
        observed('unsupported_field_remains_unknown', rc < 0)
        result['manager_after'] = identity(peer.identity_socket)
        result['exact_system_bus_query_after'] = safe(exact_system_bus_query)
        if retained_fd is not None:
            result['retained_socket_pidfd_after'] = {'pid': fd_pid(retained_fd), 'readable': readable(retained_fd)}
            observed('socket_bound_live_user_peer', pfd['state'] == 'observed' and fd_pid(retained_fd) == before_identity['credentials'][0] and not readable(retained_fd) and result['manager_after']['credentials'] == before_identity['credentials'] and before_identity['credentials'][1] == os.getuid())
    finally:
        if unit_owned:
            ctl('stop', unit_name, required=False)
        for path in paths:
            path.unlink(missing_ok=True)
        if drop_owned:
            dropdir.rmdir()
        if unit_owned:
            ctl('daemon-reload', required=False)
        if retained_fd is not None:
            os.close(retained_fd)
        peer.close()
        result['cleanup']['fixture_files_removed'] = all(not p.exists() for p in paths)

def system_socket_identity(path):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(10); sock.connect(path)
        return identity(sock)

if __name__ == '__main__':
    result['predicates'] = {name: {'state': 'unknown', 'detail': 'not completed'} for name in (
        'socket_pidfd_replacement_and_death', 'socket_bound_live_user_peer', 'foreign_manager_rejected_by_uid',
        'environment_pointer_unchanged', 'launch_environment_changed_without_reload',
        'active_loaded_definition_stale', 'loaded_definition_changed_after_reload',
        'effective_fixture_principal_argv', 'unreadable_file_remains_unknown', 'unsupported_field_remains_unknown',
        'separate_connection_selected_readback')}
    result['remaining_authority'] = {name: {'state': 'unknown'} for name in (
        'authenticated_system_bus_image_namespace', 'authenticated_user_manager_image_epoch',
        'complete_effective_definition_semantics', 'loaded_disk_equivalence',
        'routes_and_pending_jobs_complete', 'independent_runtime_verifier', 'runtime_issuer_barrier',
        'historical_containment', 'replacement_safe_mutation')}
    local = '--local-controls' in sys.argv
    try:
        main(local)
    except Exception as exc:
        result['errors'].append({'type': type(exc).__name__, 'errno': getattr(exc, 'errno', None),
                                 'frames': [{'line': f.lineno, 'function': f.name} for f in traceback.extract_tb(exc.__traceback__)]})
    finally:
        required = ['socket_pidfd_replacement_and_death'] if local else list(result['predicates'])
        result['diagnostic_exit'] = int(bool(result['errors']) or any(result['predicates'][k]['state'] != 'observed' for k in required))
        result['admission_exit'] = 1
        OUT.write_text(json.dumps(result, sort_keys=True, indent=2) + '\n')
    # A completed experiment may exit zero; authority and admission always refuse.
    raise SystemExit(result['diagnostic_exit'])

