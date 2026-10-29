"""Exercise the installed browser door, actual launchd worker and Agent journal."""
import hashlib
import http.client
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib.parse import urlsplit

case = Path(sys.argv[1]); output = Path(sys.argv[2]); program = case / 'program'; data = case / 'data'
output.mkdir(parents=True, exist_ok=True)

def save(name, value):
    (output / name).write_text(json.dumps(value, indent=2, sort_keys=True) + '\n')

def read(path):
    return json.loads(path.read_text())

def call(path, method='GET'):
    discovery = read(data / 'runtime' / 'discovery.json')
    endpoint = urlsplit(discovery['door_endpoint'])
    conn = http.client.HTTPConnection(endpoint.hostname, endpoint.port, timeout=20)
    try:
        conn.request(method, path, body=b'' if method == 'POST' else None,
                     headers={'Origin': 'https://astrid.haus'})
        response = conn.getresponse(); payload = response.read()
        return response.status, json.loads(payload)
    finally:
        conn.close()

def wait_health(release, timeout=180):
    deadline = time.monotonic() + timeout
    error = None
    while time.monotonic() < deadline:
        try:
            status, value = call('/api/runtime/v1/health')
            if status == 200 and value.get('status') == 'ok' and value.get('identity') == {'managed_by': None, 'release': release}:
                return value
            error = (status, value)
        except (OSError, ValueError, KeyError) as exc:
            error = repr(exc)
        time.sleep(2)
    raise RuntimeError(f'release {release} health not ready: {error}')

old = wait_health(0)
old_selected = (program / 'current').read_text().strip()
realm = read(data / 'runtime' / 'discovery.json')['active_realm']
sentinel = data / 'qualification-sentinel.txt'; sentinel.write_text('synthetic modern updater sentinel\n')
sentinel_hash = hashlib.sha256(sentinel.read_bytes()).hexdigest()
status, availability = call('/api/runtime/v1/update/status')
assert status == 200 and availability.get('available') is True, (status, availability)
save('baseline.json', {'health': old, 'selected_version': old_selected, 'realm': realm, 'update_status': availability, 'sentinel_sha256': sentinel_hash})
# Exactly one POST. The door uses the installed private product credential.
status, accepted = call('/api/runtime/v1/update', 'POST')
save('accepted.json', {'http_status': status, 'response': accepted})
assert status == 202 and accepted.get('accepted') is True, (status, accepted)
op = accepted['operation_id']
assert len(op) == 32
(deadline, terminal, last_error) = (time.monotonic() + 1800, None, None)
while time.monotonic() < deadline:
    try:
        status, result = call('/api/runtime/v1/update/status')
        if status == 200 and result.get('operation_id') == op:
            save('status-latest.json', result)
            if result.get('state') in {'terminal', 'recovery-needed'}:
                terminal = result; break
    except (OSError, ValueError, KeyError) as exc:
        last_error = repr(exc)
    time.sleep(3)
if terminal is None: raise RuntimeError(f'update terminal status timeout: {last_error}')
save('terminal.json', terminal)
assert terminal.get('state') == 'terminal' and terminal.get('outcome') == 'updated', terminal
assert terminal.get('operation_id') == op
new = wait_health(1); selected = (program / 'current').read_text().strip(); discovery = read(data / 'runtime' / 'discovery.json')
assert selected != old_selected
assert new['runtime_instance_id'] != old['runtime_instance_id']
assert new['schema_digest'] == 'sha256:5d229bf66ffaeb0b4937844a3c2b08ddd21f97cbcc305101a5cc19a95c971194'
assert discovery['active_realm'] == realm
assert hashlib.sha256(sentinel.read_bytes()).hexdigest() == sentinel_hash
job = read(program / 'install.json')['update_job']; journal = read(program / 'update-transaction.json')
assert job['operation_id'] == op and job['state'] == 'updated'
assert job['platform']['name'] == 'astrid-update-' + op
assert journal['step'] == 'complete' and journal['update_job']['operation_id'] == op
assert job['transaction_id'] == journal['operation_id']
save('result.json', {'test': 'synthetic release transition with identical source SHAs', 'operation_id': op,
    'old_version': old_selected, 'new_version': selected, 'health': new, 'realm': realm,
    'sentinel_sha256': sentinel_hash, 'job_platform': job['platform'], 'launch_status': job.get('launch_status'),
    'transaction_id': journal['operation_id'], 'transaction_step': journal['step']})
print('Synthetic transition complete:', op)
