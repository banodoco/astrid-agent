#!/usr/bin/env python3
"""Capture the exact candidate query and parser result in the probe context."""
import json
import subprocess
import sys
import time
from pathlib import Path

candidate, out = map(Path, sys.argv[1:])
sys.path.insert(0, str(candidate))
from astrid.installer.service import _TRANSFER_UNIT_PROPERTIES, _snapshot_unit_fields

argv = ['systemctl', '--user', 'show', 'astrid.service', '--all',
        '--property=' + ','.join(_TRANSFER_UNIT_PROPERTIES)]
result = {'argv': argv, 'started': time.time()}
try:
    proc = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True, encoding='utf-8', errors='strict', timeout=15)
    (out / 'systemd-query.stdout').write_text(proc.stdout, encoding='utf-8')
    (out / 'systemd-query.stderr').write_text(proc.stderr, encoding='utf-8')
    result['exit'] = proc.returncode
    if proc.returncode == 0:
        try:
            fields = _snapshot_unit_fields(proc.stdout)
            result['parser'] = {'status': 'ok', 'keys': sorted(fields)}
        except (ValueError, UnicodeError) as exc:
            result['parser'] = {'status': 'error', 'exception': f'{type(exc).__name__}: {exc}'}
except (OSError, subprocess.SubprocessError, UnicodeError) as exc:
    result['exception'] = f'{type(exc).__name__}: {exc}'
finally:
    result['ended'] = time.time()
    (out / 'systemd-query.json').write_text(json.dumps(result, indent=2) + '\n')
