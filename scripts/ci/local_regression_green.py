#!/usr/bin/env python3
"""Validate broker/hosted evidence, then evaluate the ledger and feature predicates.

Feature obligations are supplied only by reviewed inventory after comparison with
independent fixed coverage. Each selects a lane, scenario, slot_id, candidate,
attempt and artifact-relative JSON files. A feature manifest must bind the same
scenario/slot/attempt, tuple, lane and checkouts. For hosted artifacts the trusted
orchestrator downloader supplies run.json files entries with path, bytes, sha256,
and the authenticated run/job/attempt/artifact association. Broker entries come
from the local journal. Hash bytes before strict parsing, retain the snapshots and
pass the selected {path, sha256, json} entries to the imported predicate.
Neither an artifact's own claims nor a synthetic fixture authenticates evidence.
"""
import argparse
import datetime as dt
import hashlib
import importlib
import importlib.util
import json
from pathlib import Path, PurePosixPath
import re
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


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
PREDICATE_MODULE = 'scripts.journey_predicates'
PREDICATE_PATH = ROOT / 'scripts/ci/journey_predicates.py'


def load_predicates():
    if PREDICATE_MODULE != 'scripts.journey_predicates':
        return importlib.import_module(PREDICATE_MODULE)
    # Load the published, byte-identical module from our permitted CI path,
    # under its original package name so its relative imports remain intact.
    spec = importlib.util.spec_from_file_location(PREDICATE_MODULE, PREDICATE_PATH)
    core.require(spec is not None and spec.loader is not None, 'missing predicate module')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def safe_path(value):
    core.require(isinstance(value, str) and value and '\\' not in value, 'invalid artifact path')
    path = PurePosixPath(value)
    core.require(not path.is_absolute() and '..' not in path.parts and str(path) == value, 'invalid artifact path')
    return value


def strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            core.require(key not in result, 'duplicate JSON key')
            result[key] = value
        return result
    def invalid_constant(value):
        raise ValueError('invalid JSON constant: ' + value)
    value = json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid_constant)
    core.require(isinstance(value, dict), 'feature JSON must be an object')
    return value


def checked_bytes(path, size, digest):
    core.require(type(size) is int and size >= 0 and isinstance(digest, str) and re.fullmatch(r'[0-9a-f]{64}', digest), 'invalid file metadata')
    raw = path.read_bytes()
    core.require(len(raw) == size and hashlib.sha256(raw).hexdigest() == digest, 'download hash or size mismatch')
    return raw


def local_evaluate(tuple_path, inventory_path, coverage_path, ledger_path, broker_root, hosted_root, freshness_path, not_before, now=None, features=None):
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
        artifact_ids = set()
        download_paths = set()
        selected = {}
        pending = []
        if features is not None:
            core.require(isinstance(features, list) and features, 'missing reviewed feature inventory')
            obligations = set()
            for feature in features:
                key = tuple(feature[k] for k in ('repo', 'lane', 'platform'))
                scenario, slot = feature['scenario'], feature['slot_id']
                core.require(isinstance(scenario, str) and re.fullmatch(r'[a-z][a-z0-9_]*', scenario), 'invalid scenario')
                core.require(isinstance(slot, str) and slot and (scenario, slot) not in obligations, 'duplicate or missing feature obligation')
                obligations.add((scenario, slot))
                core.require(key in lane_keys, 'feature references unknown lane')
                candidate = feature['candidate']
                # The frozen tuple, not the producer or feature inventory, is
                # authoritative for the candidate's release manifest identity.
                expected_candidate = dict(shas)
                if isinstance(candidate, dict) and 'manifest_sha256' in candidate:
                    manifest_sha = tup['generated'].get('manifest_sha256')
                    core.require(isinstance(manifest_sha, str) and re.fullmatch(r'[0-9a-f]{64}', manifest_sha), 'missing or invalid frozen tuple manifest binding')
                    expected_candidate['manifest_sha256'] = manifest_sha
                core.require(isinstance(candidate, dict) and candidate == expected_candidate, 'feature candidate tuple mismatch or unbound candidate fields')
                paths = feature['files']
                core.require(isinstance(paths, list) and paths and len(set(map(safe_path, paths))) == len(paths), 'missing or duplicate feature files')
                core.require(type(feature['attempt']) is int and feature['attempt'] > 0, 'invalid selected attempt')
                selected.setdefault(key, []).append(feature)
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
                    snapshots = {}
                    for entry in downloads:
                        path = Path(entry['path'])
                        core.require(path.is_absolute() and path.name not in files and path.is_file() and any(path.resolve().is_relative_to(d) for d in destinations), 'invalid or duplicate download')
                        core.require(path.resolve() not in download_paths, 'duplicate broker artifact file')
                        download_paths.add(path.resolve())
                        raw = checked_bytes(path, entry['bytes'], entry['sha256'])
                        relative = [path.resolve().relative_to(d).as_posix() for d in destinations if path.resolve().is_relative_to(d)]
                        core.require(len(relative) == 1 and relative[0] not in snapshots, 'ambiguous or duplicate download path')
                        snapshots[safe_path(relative[0])] = (entry['sha256'], raw)
                        files[path.name] = path
                    manifest_file, junit_file = files['manifest.json'], files['junit.xml']
                elif kind == 'hosted':
                    core.require(key[2] in ('linux', 'macos', 'windows'), 'invalid hosted platform')
                    base = hosted / source_id
                    run = core.read_json(base / 'run.json')
                    manifest_file, junit_file = base / 'manifest.json', base / 'junit.xml'
                    core.require(run.get('id') == source['run_id'] and run.get('run_attempt') == source['run_attempt'] and run.get('repository') == tup['repos'][key[0]]['url'] and run.get('head_sha') == shas[key[0]] and run.get('status') == 'completed' and run.get('conclusion') == 'success', 'wrong or unsuccessful hosted run')
                    core.require(run.get('workflow_path') == source['workflow_path'] and run.get('job_id') == source['job_id'] and run.get('job_conclusion') == 'success', 'wrong or unsuccessful hosted job')
                    artifact = run['artifact']
                    core.require(source['artifact_id'] not in artifact_ids, 'duplicate hosted artifact')
                    artifact_ids.add(source['artifact_id'])
                    core.require(type(source['artifact_id']) is int and artifact == {'id': source['artifact_id'], 'name': source_id, 'run_id': source['run_id'], 'run_attempt': source['run_attempt'], 'job_id': source['job_id']}, 'wrong hosted artifact association')
                    started = fresh(run['run_started_at'], cutoff, now)
                    ended = fresh(run['updated_at'], cutoff, now)
                    core.require(started <= ended and key[0] in source['checkouts'], 'invalid hosted times or checkouts')
                    snapshots = {}
                    entries = run['files']  # Trusted downloader's byte inventory.
                    core.require(isinstance(entries, list) and entries, 'missing hosted file inventory')
                    for entry in entries:
                        rel = safe_path(entry['path'])
                        path = base / rel
                        core.require(rel not in snapshots and path.resolve().is_relative_to(base.resolve()) and path.is_file(), 'invalid or duplicate hosted file')
                        snapshots[rel] = (entry['sha256'], checked_bytes(path, entry['bytes'], entry['sha256']))
                else:
                    raise ValueError(f'unknown source kind: {kind}')
                core.require('manifest.json' in snapshots and 'junit.xml' in snapshots, 'missing authenticated manifest or junit')
                manifest = strict_json(snapshots['manifest.json'][1])
                core.require(manifest.get('checkouts') == {r: shas[r] for r in source['checkouts']}, 'wrong authenticated checkouts')
                if kind == 'hosted':
                    core.require(manifest.get('run_id') == run['id'] and manifest.get('run_attempt') == run['run_attempt'] and manifest.get('job_id') == run['job_id'], 'authenticated hosted identity mismatch')
                else:
                    core.require(manifest.get('pod_id') == pod and manifest.get('job_id') == source_id, 'authenticated broker identity mismatch')
                if key in selected:
                    attempt = source.get('run_attempt') if kind == 'hosted' else source.get('attempt')
                    if kind == 'broker':
                        core.require(result.get('attempt') == attempt, 'broker attempt mismatch')
                    core.require(type(attempt) is int and attempt > 0 and manifest.get('attempt') == attempt, 'manifest attempt mismatch')
                    bindings = [{'scenario': f['scenario'], 'slot_id': f['slot_id'], 'attempt': f['attempt']} for f in selected[key]]
                    core.require(all(f['attempt'] == attempt for f in selected[key]) and manifest.get('feature_bindings') == bindings, 'feature binding or attempt mismatch')
                    for feature in selected[key]:
                        entries = []
                        for rel in feature['files']:
                            core.require(rel in snapshots, 'missing authenticated feature file: ' + rel)
                            digest, raw = snapshots[rel]
                            entries.append({'path': rel, 'sha256': digest, 'json': strict_json(raw)})
                        pending.append((feature, entries))
                code = manifest.get('test_exit_code')
                core.require(type(code) is int and code in (0, 1) and manifest.get('result') == ('success' if code == 0 else 'failure'), 'test command failed to run or was skipped')
                xml = ET.fromstring(snapshots['junit.xml'][1])
                core.require(key not in selected or (manifest.get('test_exit_code') == 0 and not xml.findall('.//testcase/skipped')), 'feature job failed or skipped')
                has_failures = bool(xml.findall('.//testcase/failure') or xml.findall('.//testcase/error'))
                core.require(has_failures == (code == 1), 'test outcome conflicts with JUnit')
                observed = fresh(manifest['created_at'], cutoff, now)
                core.require(started <= observed <= ended, 'manifest outside run')
                target = Path(tmp) / core.digest(list(key))
                core.require(not target.exists(), f'duplicate lane {key}')
                target.mkdir()
                (target / 'manifest.json').write_bytes(snapshots['manifest.json'][1])
                (target / 'junit.xml').write_bytes(snapshots['junit.xml'][1])
            errors, retirement = core.evaluate(tuple_path, inventory_path, ledger_path, tmp, freshness_path)
            if errors or features is None:
                return errors, retirement
            try:
                module = load_predicates()
            except Exception as exc:
                return [f'predicate module import failed: {type(exc).__name__}: {exc}'], retirement
            for feature, entries in pending:
                label = 'feature %s/%s' % (feature['scenario'], feature['slot_id'])
                try:
                    predicate = getattr(module, 'check_' + feature['scenario'], None)
                    core.require(callable(predicate), 'missing predicate: ' + feature['scenario'])
                    result = predicate({'slot_id': feature['slot_id'], 'candidate': feature['candidate'], 'files': entries})
                    core.require(isinstance(result, tuple) and len(result) == 2 and type(result[0]) is bool and isinstance(result[1], list) and all(isinstance(x, str) for x in result[1]), 'invalid predicate result')
                    if not result[0] or result[1]:
                        errors.append('%s: %s' % (label, '; '.join(result[1]) or 'predicate failed'))
                except Exception as exc:
                    # A broken or missing predicate must produce a red result at
                    # this API boundary, including for non-aggregate callers.
                    errors.append('%s: %s: %s' % (label, type(exc).__name__, exc))
            return errors, retirement
    except (OSError, ValueError, KeyError, TypeError, AttributeError, ET.ParseError, yaml.YAMLError, jsonschema.exceptions.ValidationError, ImportError) as exc:
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
    print('Ledger/provenance diagnostic:', 'FAIL' if errors else 'PASS')
    print('Feature inventory was not evaluated; use regression_aggregate.py for int/regression-green.', file=sys.stderr)
    return True


if __name__ == '__main__':
    sys.exit(main())
