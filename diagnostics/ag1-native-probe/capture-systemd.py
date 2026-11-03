#!/usr/bin/env python3
"""Capture candidate parser evidence without publishing systemd property values."""
import base64
import hashlib
import json
import re
import subprocess
import sys
import time
from pathlib import Path


def _b64(raw):
    return base64.b64encode(raw).decode('ascii')


def describe_stdout(raw, properties):
    """Report parser-visible keys; retain exact key/delimiter bytes, never values.

    Raw line endings and recognized ASCII key prefixes are retained separately;
    arbitrary values and unrecognized/continuation lines are deliberately omitted.
    The full raw stream is represented only by length and SHA-256.
    """
    text = raw.decode('utf-8', errors='strict')
    wanted = set(properties)
    seen = []
    fragments = []
    current = None
    continuation_count = 0
    unparseable_count = 0
    for whole in text.splitlines(keepends=True):
        line = whole.splitlines()[0] if whole.splitlines() else ''
        ending = whole[len(line):].encode('utf-8')
        name, separator, _ = line.partition('=')
        if separator and name in wanted:
            seen.append(name)
            current = name
            fragments.append({'key': name, 'prefix_base64': _b64((name + '=').encode('utf-8')),
                              'line_ending_base64': _b64(ending)})
        elif current == 'ExecStart':
            continuation_count += 1
        else:
            unparseable_count += 1
    return {'byte_length': len(raw), 'sha256': hashlib.sha256(raw).hexdigest(),
            'present_keys': sorted(set(seen)), 'missing_keys': sorted(wanted - set(seen)),
            'duplicate_keys': sorted({name for name in seen if seen.count(name) > 1}),
            'recognized_line_fragments': fragments,
            'execstart_continuation_lines': continuation_count,
            'unparseable_lines': unparseable_count}


def main():
    candidate, out = map(Path, sys.argv[1:])
    sys.path.insert(0, str(candidate))
    from astrid.installer.service import _TRANSFER_UNIT_PROPERTIES, _snapshot_unit_fields

    argv = ['systemctl', '--user', 'show', 'astrid.service', '--all',
            '--property=' + ','.join(_TRANSFER_UNIT_PROPERTIES)]
    result = {'argv': argv, 'started': time.time(),
              'redaction': 'Property values, unrecognized lines, and stderr bytes omitted; fragments are exact original bytes.'}
    try:
        version = subprocess.run(['systemctl', '--version'], stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, timeout=15)
        first = version.stdout.splitlines()[0] if version.stdout else b''
        match = re.fullmatch(rb'systemd ([0-9]+)(?: .*|)', first)
        result['systemctl_version'] = {'exit': version.returncode,
                                       'major': int(match.group(1)) if match else None}
        proc = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)
        result['exit'] = proc.returncode
        result['stderr'] = {'byte_length': len(proc.stderr),
                            'sha256': hashlib.sha256(proc.stderr).hexdigest()}
        result['stdout'] = describe_stdout(proc.stdout, _TRANSFER_UNIT_PROPERTIES)
        if proc.returncode == 0:
            try:
                fields = _snapshot_unit_fields(proc.stdout.decode('utf-8', errors='strict'))
                result['parser'] = {'status': 'ok', 'keys': sorted(fields)}
            except (ValueError, UnicodeError) as exc:
                # Candidate's known fixed errors have no values; suppress all others.
                known = {'ambiguous systemd property', 'unparseable systemd property',
                         'incomplete systemd properties'}
                result['parser'] = {'status': 'error', 'exception_type': type(exc).__name__,
                                    'exception': str(exc) if str(exc) in known else 'redacted'}
    except (OSError, subprocess.SubprocessError, UnicodeError) as exc:
        result['exception_type'] = type(exc).__name__
    finally:
        result['ended'] = time.time()
        (out / 'systemd-query.json').write_text(json.dumps(result, indent=2) + '\n')


if __name__ == '__main__':
    main()
