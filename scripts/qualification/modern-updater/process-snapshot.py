"""Capture the selected runtime/worker and descendants without credential values."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys

case, output, label = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
root = case / 'program'
accepted = output / 'accepted.json'
op = json.loads(accepted.read_text())['response']['operation_id'] if accepted.exists() else None
names = ['haus.astrid.runtime'] + ([f'astrid-update-{op}'] if op else [])
roots = set()
for name in names:
    p = subprocess.run(['launchctl', 'print', f'gui/{os.getuid()}/{name}'], capture_output=True, text=True)
    (output / f'{label}-{name}.launchctl.txt').write_text(f'exit={p.returncode}\n{p.stdout}\n{p.stderr}')
    roots.update(int(x) for x in re.findall(r'^\tpid = (\d+)$', p.stdout, re.MULTILINE))
discovery = case / 'data' / 'runtime' / 'discovery.json'
if discovery.exists():
    d = json.loads(discovery.read_text())
    roots.add(d['pid'])
    filtered = {k: d.get(k) for k in ('pid', 'process_birth_id', 'runtime_instance_id', 'active_realm', 'identity')}
    (output / f'{label}-discovery.json').write_text(json.dumps(filtered, indent=2) + '\n')
# Keep pre-stop PIDs visible in the post-stop snapshot without claiming they
# still refer to the same process if the PID was reused.
before = output / 'before-stop-processes.json'
if label == 'after-stop' and before.exists():
    prior = json.loads(before.read_text())
    roots.update(prior['root_pids'])
    roots.update(row['pid'] for row in prior['processes'])
p = subprocess.run(['ps', '-axo', 'pid=,ppid=,etime=,comm='], check=True, capture_output=True, text=True)
rows = []
for line in p.stdout.splitlines():
    parts = line.split(None, 3)
    if len(parts) == 4:
        rows.append({'pid': int(parts[0]), 'ppid': int(parts[1]), 'elapsed': parts[2], 'executable': parts[3]})
owned = set(roots)
while True:
    expanded = owned | {row['pid'] for row in rows if row['ppid'] in owned}
    if expanded == owned:
        break
    owned = expanded
snapshot = {'root_pids': sorted(roots), 'processes': [row for row in rows if row['pid'] in owned]}
(output / f'{label}-processes.json').write_text(json.dumps(snapshot, indent=2) + '\n')
