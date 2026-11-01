#!/usr/bin/env python3
"""Collect the current Actions run's completed jobs and receipt artifacts."""
import hashlib
import json
import os
from pathlib import Path
import re
import urllib.error
import urllib.request

root = Path(os.environ['RESULT_ROOT'])
root.mkdir(parents=True, exist_ok=True)
repo = os.environ['GITHUB_REPOSITORY']
run = os.environ['GITHUB_RUN_ID']
attempt = os.environ['GITHUB_RUN_ATTEMPT']
base = f'https://api.github.com/repos/{repo}'
headers = {'Authorization': 'Bearer ' + os.environ['GH_TOKEN'], 'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28'}
errors = []

def request(url):
    return urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=90)

def pages(path, key):
    values = []
    for page in range(1, 101):
        with request(f'{base}{path}?per_page=100&page={page}') as response:
            result = json.load(response)[key]
        values.extend(result)
        if len(result) < 100:
            return values
    raise RuntimeError('pagination limit exceeded: ' + path)

def save_json(name, data):
    (root / name).write_text(json.dumps(data, indent=2, sort_keys=True) + '\n', encoding='utf-8')

save_json('identity.json', {'repository': repo, 'run_id': run, 'attempt': attempt,
    'caller_sha': os.environ['GITHUB_SHA'], 'ref': os.environ['GITHUB_REF'],
    'workflow': os.environ['GITHUB_WORKFLOW'], 'workflow_ref': os.environ.get('GITHUB_WORKFLOW_REF'),
    'workflow_sha': os.environ.get('GITHUB_WORKFLOW_SHA'), 'reusable_result': os.environ['JOURNEY_RESULT']})
try:
    jobs = pages(f'/actions/runs/{run}/attempts/{attempt}/jobs', 'jobs')
    save_json('jobs.json', jobs)
    for job in jobs:
        if job['status'] != 'completed':
            continue
        try:
            with request(f"{base}/actions/jobs/{job['id']}/logs") as response:
                (root / f"job-{job['id']}.log").write_bytes(response.read())
        except Exception as exc:
            errors.append(f"job {job['id']} log: {type(exc).__name__}: {exc}")
except Exception as exc:
    jobs = []
    errors.append(f'jobs listing: {type(exc).__name__}: {exc}')
try:
    artifacts = pages(f'/actions/runs/{run}/artifacts', 'artifacts')
    save_json('artifacts.json', artifacts)
    (root / 'artifacts').mkdir(exist_ok=True)
    for artifact in artifacts:
        if not artifact['name'].startswith('journey-receipt-'):
            continue
        name = re.sub(r'[^A-Za-z0-9_.-]', '_', artifact['name'])
        if artifact.get('expired') or artifact.get('size_in_bytes', 0) > 90 * 1024 * 1024:
            errors.append(f"artifact {artifact['id']} {name}: expired or exceeds 90 MiB publication limit")
            continue
        try:
            with request(f"{base}/actions/artifacts/{artifact['id']}/zip") as response:
                data = response.read(90 * 1024 * 1024 + 1)
            if len(data) > 90 * 1024 * 1024:
                raise ValueError('download exceeds 90 MiB publication limit')
            (root / 'artifacts' / f"{name}-{artifact['id']}.zip").write_bytes(data)
        except Exception as exc:
            errors.append(f"artifact {artifact['id']} {name}: {type(exc).__name__}: {exc}")
except Exception as exc:
    artifacts = []
    errors.append(f'artifacts listing: {type(exc).__name__}: {exc}')
for row in ('linux', 'macos', 'windows'):
    if not any(a['name'] == f'journey-receipt-direct-{row}' for a in artifacts):
        errors.append(f'missing journey-receipt-direct-{row} artifact')
save_json('collection-status.json', {'errors': errors, 'note': 'Job conclusions and steps are in jobs.json; this collector itself is still in progress. Missing logs or artifacts are not passing evidence.'})
with (root / 'SHA256SUMS').open('w', encoding='utf-8') as sums:
    for path in sorted(root.rglob('*')):
        if path.is_file() and path.name != 'SHA256SUMS':
            sums.write(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.relative_to(root).as_posix()}\n")
