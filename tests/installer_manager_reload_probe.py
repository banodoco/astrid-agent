#!/usr/bin/env python3
"""Non-authoritative systemd unit-lifetime/reload discriminator.

One direct peer subscribes before the experiment and receives manager events.
A separate active oneshot control is held loaded throughout the interval.
Signal timestamps are reception times, not manager emission timestamps.
"""
import ctypes as C
import hashlib
import json
import os
import pathlib
import subprocess
import time
import traceback

import installer_manager_identity_probe as native

ROOT = pathlib.Path(os.environ['PROOF_ROOT'])
HOME = pathlib.Path(os.environ['HOME'])
UNITS = {'idle': 'astrid-reload-idle.service', 'active': 'astrid-reload-active.service'}
result = {'authority': False, 'uid': os.getuid(), 'euid': os.geteuid(),
          'events': [], 'calls': [], 'snapshots': {}, 'manager_samples': {},
          'errors': [], 'cleanup': {}, 'outcome': 'unknown'}
native.result = result


def event(action, **data):
    result['events'].append({'seq': len(result['events']), 'realtime_ns': time.time_ns(),
                             'monotonic_ns': time.monotonic_ns(), 'action': action, **data})


# Record begin/end times for each native operation, including nested calls.
_original_check = native.check
def traced_check(label, rc):
    event('sd_bus.return', label=label, sd_bus_return=rc)
    return _original_check(label, rc)
native.check = traced_check

def trace_method(name):
    original = getattr(native.Peer, name)
    def traced(self, *args, **kwargs):
        event('native.begin', operation=name, arguments=list(args))
        try:
            value = original(self, *args, **kwargs)
        except Exception as exc:
            event('native.error', operation=name, error=repr(exc))
            raise
        event('native.end', operation=name)
        return value
    setattr(native.Peer, name, traced)

for _method in ('method', 'load', 'export', 'string_property', 'strv_property'):
    trace_method(_method)


def run(*args):
    event('command.begin', argv=['systemctl', '--user', *args])
    done = subprocess.run(['systemctl', '--user', *args], capture_output=True, text=True, timeout=25)
    event('command.end', argv=['systemctl', '--user', *args], exit=done.returncode,
          stdout=done.stdout, stderr=done.stderr)
    if done.returncode:
        raise RuntimeError('systemctl failed: ' + done.stderr[:500])


def write(path, data):
    event('file.write.begin', path=str(path), sha256=hashlib.sha256(data.encode()).hexdigest())
    with path.open('x') as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    event('file.write.end', path=str(path))


# The match is installed before Subscribe; callback lifetime spans the peer.
CALLBACK = C.CFUNCTYPE(C.c_int, native.void, native.void, native.void)
add_match = native.declare('sd_bus_add_match', [native.void, C.POINTER(native.void), native.string, CALLBACK, native.void])
process = native.declare('sd_bus_process', [native.void, C.POINTER(native.void)])
wait = native.declare('sd_bus_wait', [native.void, C.c_uint64])
get_member = native.declare('sd_bus_message_get_member', [native.void], native.string)
get_trivial = native.declare('sd_bus_get_property_trivial', [native.void, native.string, native.string, native.string, native.string, native.void, C.c_char, native.void])
slot_unref = native.declare('sd_bus_slot_unref', [native.void], native.void)


@CALLBACK
def signal(message, userdata, error):
    try:
        member = get_member(message).decode()
        data = {'member': member}
        if member == 'Reloading':
            value = C.c_int()
            rc = native.read_message(message, b'b', C.byref(value))
            data.update(decode_return=rc, active=bool(value.value) if rc > 0 else None)
        elif member in ('UnitNew', 'UnitRemoved'):
            name, path = native.string(), native.string()
            rc = native.read_message(message, b'so', C.byref(name), C.byref(path))
            data.update(decode_return=rc, unit=name.value.decode() if rc > 0 else None,
                        path=path.value.decode() if rc > 0 else None)
        event('signal.received', **data)
    except Exception as exc:
        event('signal.decode_error', error=repr(exc))
    return 0


def drain(peer):
    while True:
        rc = process(peer.bus, None)
        if rc < 0:
            native.check('sd_bus_process', rc)
        if rc == 0:
            return


def interval(peer, label, seconds):
    event('interval.begin', label=label, seconds=seconds)
    deadline = time.monotonic() + seconds
    while True:
        drain(peer)
        left = deadline - time.monotonic()
        if left <= 0:
            break
        native.check('sd_bus_wait.' + label, wait(peer.bus, int(left * 1_000_000)))
    drain(peer)
    event('interval.end', label=label)


def sample_manager(peer, label):
    event('manager.sample.begin', label=label)
    values = {}
    # NReloads is not exposed by every manager; preserve error as unknown.
    for prop, typ, cls in [('NReloads', b'u', C.c_uint32),
                           ('GeneratorsStartTimestampMonotonic', b't', C.c_uint64),
                           ('GeneratorsFinishTimestampMonotonic', b't', C.c_uint64),
                           ('UnitsLoadStartTimestampMonotonic', b't', C.c_uint64),
                           ('UnitsLoadFinishTimestampMonotonic', b't', C.c_uint64),
                           ('FinishTimestampMonotonic', b't', C.c_uint64)]:
        value = cls()
        rc = get_trivial(peer.bus, native.b(peer.destination), native.b(native.MANAGER),
                         native.b(native.IFACE), native.b(prop), None, typ, C.byref(value))
        values[prop] = {'return': rc, 'value': value.value if rc >= 0 else None}
        event('manager.property', property=prop, sd_bus_return=rc, value=values[prop]['value'])
    result['manager_samples'][label] = values
    drain(peer)
    event('manager.sample.end', label=label)


def snapshot(peer, kind, path, label):
    tag = label + '.' + kind
    event('snapshot.begin', label=tag, path=path)
    peer.export(tag + '.unit.GetAll', path, native.UNIT_IFACE)
    peer.export(tag + '.service.GetAll', path, native.SERVICE)
    values = {'Description': peer.string_property(tag + '.Description', path, native.UNIT_IFACE, 'Description'),
              'Environment': peer.strv_property(tag + '.Environment', path, native.SERVICE, 'Environment'),
              'User': peer.string_property(tag + '.User', path, native.SERVICE, 'User'),
              'Group': peer.string_property(tag + '.Group', path, native.SERVICE, 'Group'),
              'WorkingDirectory': peer.string_property(tag + '.WorkingDirectory', path, native.SERVICE, 'WorkingDirectory'),
              'ActiveState': peer.string_property(tag + '.ActiveState', path, native.UNIT_IFACE, 'ActiveState')}
    result['snapshots'].setdefault(label, {})[kind] = values
    drain(peer)
    event('snapshot.end', label=tag, values=values)


def verify_monitor():
    signals = [e for e in result['events'] if e['action'] == 'signal.received' and e.get('member') == 'Reloading']
    start = next(e['seq'] for e in result['events'] if e['action'] == 'manager.sample.begin' and e['label'] == 'before_no_reload_interval')
    end = next(e['seq'] for e in result['events'] if e['action'] == 'no_reload_window.end')
    in_window = [e for e in signals if start <= e['seq'] < end]
    after = [e for e in signals if e['seq'] > end]
    result['signal_counts'] = {'no_reload_window': len(in_window), 'positive_control': len(after)}
    result['monitor_positive_control'] = [e.get('active') for e in after]
    if (in_window or [e.get('active') for e in after] != [True, False]
            or any(e['action'] == 'signal.decode_error' for e in result['events'])
            or result['snapshots']['before']['active']['ActiveState'] != 'active'
            or result['snapshots']['disk_changed_before_reload']['active']['ActiveState'] != 'active'):
        raise RuntimeError('reload monitor or active control inconclusive')


def main():
    if os.getuid() == 0 or os.getuid() != os.geteuid():
        raise RuntimeError('requires equal real/effective non-root uid')
    directory = HOME / '.config/systemd/user'
    directory.mkdir(parents=True, exist_ok=True)
    units = {k: directory / v for k, v in UNITS.items()}
    drops = {k: directory / (v + '.d') for k, v in UNITS.items()}
    if any(p.exists() for p in [*units.values(), *drops.values()]):
        raise RuntimeError('fixture already exists')
    peer = None
    slot = native.void()
    try:
        for kind, path in units.items():
            write(path, '[Unit]\nDescription=Reload ' + kind + ' before\n[Service]\nType=oneshot\n'
                  'RemainAfterExit=yes\nExecStart=/usr/bin/true\nEnvironment=ASTRID_RELOAD_' + kind.upper() + '=before\n')
        event('peer.connect.begin', path=str(native.PRIVATE))
        peer = native.Peer(native.PRIVATE)
        event('peer.connect.end', credentials=peer.peer)
        rc = add_match(peer.bus, C.byref(slot), b"type='signal',interface='org.freedesktop.systemd1.Manager'", signal, None)
        native.check('sd_bus_add_match', rc)
        event('subscribe.begin')
        reply = peer.method('Subscribe', native.MANAGER, native.IFACE, 'Subscribe', None, None)
        native.message_unref(reply)
        drain(peer)
        event('subscribe.end')
        run('daemon-reload')
        drain(peer)
        run('start', UNITS['active'])
        drain(peer)
        paths = {}
        event('load.begin', unit=UNITS['idle'])
        paths['idle'] = peer.load(UNITS['idle'])
        event('load.end', unit=UNITS['idle'], path=paths['idle'])
        event('load.begin', unit=UNITS['active'])
        paths['active'] = peer.load(UNITS['active'])
        event('load.end', unit=UNITS['active'], path=paths['active'])
        drain(peer)
        snapshot(peer, 'idle', paths['idle'], 'before')
        snapshot(peer, 'active', paths['active'], 'before')
        sample_manager(peer, 'before_no_reload_interval')
        interval(peer, 'before_disk_change_no_reload', 2)
        for kind, drop in drops.items():
            event('mkdir.begin', path=str(drop))
            drop.mkdir()
            event('mkdir.end', path=str(drop))
            write(drop / 'override.conf', '[Unit]\nDescription=Reload ' + kind + ' after\n[Service]\n'
                  'Environment=ASTRID_RELOAD_' + kind.upper() + '=after\n')
        interval(peer, 'after_disk_change_no_reload', 2)
        snapshot(peer, 'idle', paths['idle'], 'disk_changed_before_reload')
        snapshot(peer, 'active', paths['active'], 'disk_changed_before_reload')
        sample_manager(peer, 'after_no_reload_interval')
        event('no_reload_window.end')
        run('daemon-reload')
        interval(peer, 'explicit_reload_positive_control', 1)
        snapshot(peer, 'idle', paths['idle'], 'after_reload')
        snapshot(peer, 'active', paths['active'], 'after_reload')
        sample_manager(peer, 'after_explicit_reload')
        verify_monitor()
        result['outcome'] = 'captured'
    finally:
        try:
            if peer is not None:
                drain(peer)
                if slot.value:
                    slot_unref(slot)
                peer.close()
                event('peer.closed')
            # Stop the control before removing any of the disposable unit files.
            if units['active'].exists():
                run('stop', UNITS['active'])
            for kind in UNITS:
                if drops[kind].exists():
                    event('unlink', path=str(drops[kind] / 'override.conf'))
                    (drops[kind] / 'override.conf').unlink(missing_ok=True)
                    event('rmdir', path=str(drops[kind]))
                    drops[kind].rmdir()
                event('unlink', path=str(units[kind]))
                units[kind].unlink(missing_ok=True)
            run('daemon-reload')
            result['cleanup']['fixture'] = 'removed_and_reloaded'
        except Exception as exc:
            result['cleanup']['error'] = repr(exc)
            raise


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        result['errors'].append({'type': type(exc).__name__, 'message': str(exc), 'traceback': traceback.format_exc()})
        result['outcome'] = 'inconclusive'
    finally:
        (ROOT / 'result.json').write_text(json.dumps(result, sort_keys=True, indent=2) + '\n')
    if result['outcome'] != 'captured':
        raise SystemExit(1)
