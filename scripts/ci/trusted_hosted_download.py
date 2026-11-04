#!/usr/bin/env python3
"""Fetch reviewed single-job GitHub Actions evidence on the trusted orchestrator."""
import hashlib
import io
import json
import os
from pathlib import Path
import re
import urllib.request
import zipfile

import local_regression_green as local

API = 'https://api.github.com'
MAX_ARCHIVE = 100 * 1024 * 1024
MAX_FILES = 1000


class NoAuthRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected and not newurl.startswith(API + '/'):
            redirected.remove_header('Authorization')
        return redirected


def get(path, binary=False):
    req = urllib.request.Request(API + path, headers={'Accept': 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28', **({'Authorization': 'Bearer ' + os.environ['GH_TOKEN']} if os.environ.get('GH_TOKEN') else {})})
    with urllib.request.build_opener(NoAuthRedirect()).open(req, timeout=60) as response:
        data = response.read(MAX_ARCHIVE + 1)
    if len(data) > MAX_ARCHIVE:
        raise ValueError('response too large')
    return data if binary else json.loads(data)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def download(tup, lanes, destination, fetch=get):
    """Only reviewed source IDs are accepted; destination must be newly created/private."""
    seen_ids, seen_artifacts = set(), set()
    for lane in lanes:
        source = lane['source']
        if source['kind'] != 'hosted':
            continue
        name = local.component(source['id'])
        run_id, attempt, job_id, artifact_id = (source[k] for k in ('run_id', 'run_attempt', 'job_id', 'artifact_id'))
        require(all(type(v) is int and v > 0 for v in (run_id, attempt, job_id, artifact_id)) and attempt == 1, 'hosted source requires positive IDs and first attempt')
        require(name not in seen_ids and artifact_id not in seen_artifacts, 'duplicate hosted source or artifact')
        seen_ids.add(name)
        seen_artifacts.add(artifact_id)
        repo = lane['repo']
        url = tup['repos'][repo]['url']
        require(url == 'https://github.com/banodoco/astrid-' + repo and re.fullmatch(r'[a-z0-9-]+', repo), 'invalid repository')
        prefix = '/repos/banodoco/astrid-' + repo + '/actions'
        run = fetch(f'{prefix}/runs/{run_id}')
        jobs = fetch(f'{prefix}/runs/{run_id}/attempts/{attempt}/jobs?per_page=100')
        artifacts = fetch(f'{prefix}/runs/{run_id}/artifacts?per_page=100')
        require(run.get('id') == run_id and run.get('run_attempt') == attempt and run.get('head_sha') == tup['repos'][repo]['sha'] and run.get('status') == 'completed' and run.get('conclusion') == 'success' and run.get('path') == source['workflow_path'] and run.get('repository', {}).get('html_url') == url, 'wrong or unsuccessful run')
        require(jobs.get('total_count') == 1 and len(jobs.get('jobs', [])) == 1, 'run must contain exactly one job')
        job = jobs['jobs'][0]
        require(job.get('id') == job_id and job.get('run_id') == run_id and job.get('run_attempt') == attempt and job.get('status') == 'completed' and job.get('conclusion') == 'success', 'wrong or unsuccessful job')
        require(type(artifacts.get('total_count')) is int and artifacts['total_count'] <= 100 and len(artifacts.get('artifacts', [])) == artifacts['total_count'], 'incomplete artifact listing')
        matches = [x for x in artifacts['artifacts'] if x.get('id') == artifact_id or x.get('name') == name]
        require(len(matches) == 1, 'missing or duplicate artifact')
        artifact = matches[0]
        require(artifact.get('id') == artifact_id and artifact.get('name') == name and artifact.get('expired') is False and artifact.get('workflow_run', {}).get('id') == run_id, 'wrong artifact association')
        digest = artifact.get('digest')
        require(isinstance(digest, str) and re.fullmatch(r'sha256:[0-9a-f]{64}', digest), 'missing artifact digest')
        raw = fetch(f'{prefix}/artifacts/{artifact_id}/zip', True)
        require(hashlib.sha256(raw).hexdigest() == digest[7:], 'artifact archive digest mismatch')
        base = Path(destination) / name
        base.mkdir(mode=0o700)
        entries, total = [], 0
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            members = archive.infolist()
            require(0 < len(members) <= MAX_FILES, 'invalid archive file count')
            names = set()
            for member in members:
                rel = local.safe_path(member.filename)
                require(rel != 'run.json' and not member.is_dir() and not (member.external_attr >> 16 & 0o170000 == 0o120000) and rel not in names, 'invalid or duplicate archive member')
                names.add(rel)
                total += member.file_size
                require(total <= MAX_ARCHIVE, 'expanded archive too large')
                data = archive.read(member)
                target = base / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                entries.append({'path': rel, 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()})
        require({'manifest.json', 'junit.xml'} <= names, 'missing manifest or junit')
        metadata = {'id': run_id, 'run_attempt': attempt, 'repository': url, 'head_sha': run['head_sha'], 'status': run['status'], 'conclusion': run['conclusion'], 'workflow_path': run['path'], 'job_id': job_id, 'job_conclusion': job['conclusion'], 'run_started_at': run['run_started_at'], 'updated_at': run['updated_at'], 'artifact': {'id': artifact_id, 'name': name, 'run_id': run_id, 'run_attempt': attempt, 'job_id': job_id}, 'files': entries}
        (base / 'run.json').write_text(json.dumps(metadata), encoding='utf-8')
