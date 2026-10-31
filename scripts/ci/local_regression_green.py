#!/usr/bin/env python3
"""Validate broker journals and downloaded hosted artifacts, then evaluate the ledger."""
import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path
import re
import shutil
import sys
import tempfile
import xml.etree.ElementTree as ET

import yaml
import jsonschema
import regression_green as core


def timestamp(value):
    core.require(isinstance(value, str), 'missing timestamp')
    parsed = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
    core.require(parsed.tzinfo is not None, 'timestamp must include timezone')
    return parsed.astimezone(dt.timezone.utc)


def fresh(value, cutoff, now):
    t = timestamp(value)
    core.require(cutoff <= t <= now, f'stale or future evidence: {value}')
    return t


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def component(value):
    core.require(isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_.-]+', value) and value not in ('.', '..'), 'invalid source id')
    return value


def local_evaluate(tuple_path, inventory_path, coverage_path, ledger_path, broker_root, hosted_root, freshness_path, not_before, now=None):
    try:
        now = now or dt.datetime.now(dt.timezone.utc)
        tup = core.read_json(tuple_path)
        core.validate_schema(tup, 'tuple.schema.json')
        identity = core.digest({k: v for k, v in tup.items() if k != 'digest'})
        core.require(tup.get('digest', identity) == identity, 'tuple digest mismatch')
        shas = {k: v['sha'] for k, v in tup['repos'].items()}
        cutoff = max(timestamp(not_before), timestamp(tup['created_at']))
        core.require(cutoff <= now, 'freshness cutoff is in the future')
        inventory = core.read_json(inventory_path)
        lanes = inventory['lanes']
        core.require(isinstance(lanes, list) and lanes, 'inventory has no lanes')
        coverage = core.read_json(coverage_path)
        required = coverage['lanes']
        core.require(isinstance(required, list) and required, 'coverage has no lanes')
        required_keys = [(x['repo'], x['lane'], x['platform']) for x in required]
        lane_keys = [(x['repo'], x['lane'], x['platform']) for x in lanes]
        core.require(len(set(required_keys)) == len(required_keys) and len(set(lane_keys)) == len(lane_keys) and set(required_keys) == set(lane_keys), 'inventory differs from required suite/shard coverage')
        journal = Path(broker_root)
        hosted = Path(hosted_root)
        used = set()
        with tempfile.TemporaryDirectory(prefix='regression-green-') as tmp:
            for lane in lanes:
                key = (lane['repo'], lane['lane'], lane['platform'])
                source = lane['source']
                kind, source_id = source['kind'], component(source['id'])
                core.require((kind, source_id) not in used, f'duplicate source {source_id}')
                used.add((kind, source_id))
                if kind == 'broker':
                    core.require(key[2] == 'linux', 'broker lane must be linux')
                    job = journal / 'jobs' / source_id
                    spec_file = job / 'job.yaml'
                    core.require(sha(spec_file) == source['job_sha256'], f'job spec mismatch: {source_id}')
                    spec = yaml.safe_load(spec_file.read_text(encoding='utf-8'))
                    result = core.read_json(job / 'result.json')
                    core.require(isinstance(spec, dict) and isinstance(spec.get('steps'), list) and spec['steps'], 'invalid broker job')
                    commands = '\n'.join(step['run'] for step in spec['steps'])
                    repos = source['checkouts']
                    core.require(isinstance(repos, list) and repos and len(set(repos)) == len(repos) and key[0] in repos, 'invalid checkout inventory')
                    for repo in repos:
                        core.require(repo in shas and tup['repos'][repo]['url'] in commands and shas[repo] in commands, f'job spec missing repo identity {repo}')
                    core.require(result.get('ok') is True and result.get('released') is True and result.get('release_verified') is True, 'broker job unsuccessful or release unverified')
                    exits = result.get('exit_codes')
                    core.require(isinstance(exits, list) and len(exits) == len(spec['steps']) and all(type(x) is int and x == 0 for x in exits), 'broker step failed')
                    started = fresh(result['started'], cutoff, now)
                    ended = fresh(result['ended'], cutoff, now)
                    core.require(started <= ended, 'invalid job times')
                    pod = result['pod_id']
                    events = [json.loads(line) for line in (journal / 'pods.jsonl').read_text().splitlines() if line.strip()]
                    matches = [x for x in events if x.get('pod_id') == pod]
                    created = [x for x in matches if x.get('event') == 'created']
                    released = [x for x in matches if x.get('event') == 'released']
                    core.require(len(created) == len(released) == 1 and created[0].get('name') == released[0].get('name') and created[0].get('stream') == spec.get('stream') and released[0].get('verified') is True, 'missing or conflicting pod provenance')
                    intents = [x for x in events if x.get('event') == 'intent' and x.get('name') == created[0].get('name')]
                    core.require(len(intents) == 1 and intents[0].get('stream') == spec.get('stream') and started <= timestamp(intents[0]['time']) <= timestamp(created[0]['time']) <= timestamp(released[0]['time']) <= now and timestamp(created[0]['time']) <= ended <= timestamp(released[0]['time']), 'invalid pod lifecycle')
                    downloads = result['downloads']
                    core.require(isinstance(downloads, list) and downloads, 'missing downloads')
                    destinations = [Path(d['to']).resolve() for d in spec['download']]
                    core.require(destinations and all(p.is_absolute() for p in destinations), 'missing download destinations')
                    files = {}
                    for entry in downloads:
                        path = Path(entry['path'])
                        core.require(path.is_absolute() and path.name not in files and path.is_file() and any(path.resolve().is_relative_to(d) for d in destinations), 'invalid or duplicate download')
                        core.require(type(entry['bytes']) is int and path.stat().st_size == entry['bytes'] and sha(path) == entry['sha256'], 'download hash or size mismatch')
                        files[path.name] = path
                    manifest_file, junit_file = files['manifest.json'], files['junit.xml']
                    manifest = core.read_json(manifest_file)
                    core.require(manifest.get('checkouts') == {r: shas[r] for r in repos}, 'wrong effective checkouts')
                    core.require(manifest.get('pod_id') == pod and manifest.get('job_id') == source_id, 'wrong broker result identity')
                elif kind == 'hosted':
                    core.require(key[2] in ('linux', 'macos', 'windows'), 'invalid hosted platform')
                    base = hosted / source_id
                    run = core.read_json(base / 'run.json')
                    manifest_file, junit_file = base / 'manifest.json', base / 'junit.xml'
                    manifest = core.read_json(manifest_file)
                    core.require(run.get('id') == source['run_id'] and run.get('run_attempt') == source['run_attempt'] and run.get('repository') == tup['repos'][key[0]]['url'] and run.get('head_sha') == shas[key[0]] and run.get('status') == 'completed' and run.get('conclusion') == 'success', 'wrong or unsuccessful hosted run')
                    core.require(run.get('workflow_path') == source['workflow_path'] and run.get('job_id') == source['job_id'] and run.get('job_conclusion') == 'success', 'wrong or unsuccessful hosted job')
                    artifact = run['artifact']
                    core.require(type(source['artifact_id']) is int and artifact == {'id': source['artifact_id'], 'name': source_id, 'run_id': source['run_id'], 'run_attempt': source['run_attempt'], 'job_id': source['job_id']}, 'wrong hosted artifact association')
                    started = fresh(run['run_started_at'], cutoff, now)
                    ended = fresh(run['updated_at'], cutoff, now)
                    core.require(started <= ended and manifest.get('run_id') == run['id'] and manifest.get('run_attempt') == run['run_attempt'] and manifest.get('job_id') == run['job_id'], 'hosted identity mismatch')
                    core.require(manifest.get('checkouts') == {r: shas[r] for r in source['checkouts']} and key[0] in source['checkouts'], 'wrong hosted checkouts')
                else:
                    raise ValueError(f'unknown source kind: {kind}')
                code = manifest.get('test_exit_code')
                core.require(type(code) is int and code in (0, 1) and manifest.get('result') == ('success' if code == 0 else 'failure'), 'test command failed to run or was skipped')
                xml = ET.fromstring(junit_file.read_bytes())
                has_failures = bool(xml.findall('.//testcase/failure') or xml.findall('.//testcase/error'))
                core.require(has_failures == (code == 1), 'test outcome conflicts with JUnit')
                observed = fresh(manifest['created_at'], cutoff, now)
                core.require(started <= observed <= ended, 'manifest outside run')
                target = Path(tmp) / core.digest(list(key))
                core.require(not target.exists(), f'duplicate lane {key}')
                target.mkdir()
                shutil.copyfile(manifest_file, target / 'manifest.json')
                shutil.copyfile(junit_file, target / 'junit.xml')
            return core.evaluate(tuple_path, inventory_path, ledger_path, tmp, freshness_path)
    except (OSError, ValueError, KeyError, TypeError, AttributeError, ET.ParseError, yaml.YAMLError, jsonschema.exceptions.ValidationError) as exc:
        return [f'invalid or missing provenance: {exc}'], []


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('tuple', 'inventory', 'coverage', 'ledger', 'broker-root', 'hosted-root', 'freshness', 'not-before'):
        parser.add_argument('--' + name, required=True)
    args = parser.parse_args()
    errors, retirement = local_evaluate(args.tuple, args.inventory, args.coverage, args.ledger, args.broker_root, args.hosted_root, args.freshness, args.not_before)
    for item in retirement:
        print('RETIREMENT:', item)
    for item in errors:
        print('ERROR:', item, file=sys.stderr)
    print('int/regression-green:', 'FAIL' if errors else 'PASS')
    return bool(errors)


if __name__ == '__main__':
    sys.exit(main())
