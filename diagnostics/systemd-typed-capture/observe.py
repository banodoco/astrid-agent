"""Redacted, diagnostic-only wrappers around the candidate's actual calls.

Use in a single-threaded disposable probe process, never in the installer.  The
wrappers call the original functions exactly once and re-raise their failures.
"""
from contextlib import contextmanager
import hashlib
import json
import subprocess
import time


def digest(data):
    return {'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}


def classify(raw, operation):
    result = {'classification': 'unknown', **digest(raw)}
    try:
        def unique(pairs):
            obj = {}
            for key, value in pairs:
                if key in obj:
                    raise ValueError()
                obj[key] = value
            return obj
        obj = json.loads(raw.decode('utf-8', 'strict'), object_pairs_hook=unique,
                         parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if not isinstance(obj, dict) or set(obj) != {'type', 'data'}:
            return result
        if operation == 'resolve':
            result['classification'] = ('expected-object' if obj == {
                'type': 'o', 'data': ['/org/freedesktop/systemd1/unit/astrid_2eservice']}
                else 'unknown')
        elif operation == 'property' and obj['type'] == 'a(sb)' and isinstance(obj['data'], list):
            entries = obj['data']
            if len(entries) > 1024:
                return result
            for entry in entries:
                if (not isinstance(entry, list) or len(entry) != 2 or
                    not isinstance(entry[0], str) or type(entry[1]) is not bool):
                    return result
                encoded = entry[0].encode('utf-8', 'strict')
                if not encoded or len(encoded) > 4096 or b'\0' in encoded:
                    return result
            result['classification'] = 'empty' if not entries else 'populated'
            result['signature'] = 'a(sb)'
            result['count'] = len(entries)
    except (ValueError, UnicodeError, RecursionError, TypeError):
        pass
    return result


def operation(args):
    destination = 'org.freedesktop.systemd1'
    if args == ['call', destination, '/org/freedesktop/systemd1',
                'org.freedesktop.systemd1.Manager', 'GetUnit', 's', 'astrid.service']:
        return 'resolve'
    if args == ['get-property', destination, '/org/freedesktop/systemd1/unit/astrid_2eservice',
                'org.freedesktop.systemd1.Service', 'EnvironmentFiles']:
        return 'property'
    return 'other'


@contextmanager
def capture(service, events):
    """Append observations without writing raw values, stderr, paths or errors."""
    show, bounded = service._transfer_systemctl_show, service._bounded_busctl
    popen = service.subprocess.Popen
    endpoint = 0

    def text_call():
        nonlocal endpoint
        endpoint += 1
        row = {'kind': 'systemctl', 'endpoint': endpoint, 'started_ns': time.time_ns(), 'exit': None}
        try:
            value = show()
            raw = value.encode('utf-8', 'strict')
            keys = [line.partition('=')[0] for line in value.splitlines() if '=' in line]
            wanted = set(service._TRANSFER_UNIT_PROPERTIES)
            seen = [key for key in keys if key in wanted]
            row.update(exit=0, stdout=digest(raw), present=sorted(set(seen)),
                       missing=sorted(wanted - set(seen)),
                       duplicate=sorted(key for key in set(seen) if seen.count(key) > 1))
            return value
        except subprocess.CalledProcessError as exc:
            row['exit'] = exc.returncode
            raise
        finally:
            row['ended_ns'] = time.time_ns()
            events.append(row)

    def typed_call(args):
        row = {'kind': 'busctl', 'endpoint': endpoint, 'operation': operation(args),
               'started_ns': time.time_ns(), 'exit': None, 'classification': 'unknown'}
        processes = []
        def start(*a, **kw):
            child = popen(*a, **kw)
            processes.append(child)
            return child
        service.subprocess.Popen = start
        try:
            raw = bounded(args)
            row.update(classify(raw, row['operation']))
            return raw
        finally:
            service.subprocess.Popen = popen
            if len(processes) == 1:
                row['exit'] = processes[0].returncode
            if row['exit'] != 0:
                row['classification'] = 'unknown'
                row.pop('count', None)
                row.pop('signature', None)
            row['ended_ns'] = time.time_ns()
            events.append(row)

    service._transfer_systemctl_show = text_call
    service._bounded_busctl = typed_call
    try:
        yield
    finally:
        service._transfer_systemctl_show = show
        service._bounded_busctl = bounded
        service.subprocess.Popen = popen
