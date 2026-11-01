#!/usr/bin/env python3
"""Publish the tuple-bound ``int/regression-green`` GitHub Check Run.

This adapter is intentionally local to the trusted coordinator.  It only
accepts the atomic status written by ``regression_aggregate.py`` and keeps a
small, coordinator-owned publication journal.  The journal's monotonically
increasing sequence prevents a late batch from replacing a newer tuple result
on the same Agent commit.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import urllib.error
import urllib.request


API = 'https://api.github.com'
CHECK = 'int/regression-green'
REPOSITORY = 'banodoco/astrid-agent'
STATE_VERSION = 'c06-publication/v1'


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def write_json(path, value):
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix='.regression-publication-', dir=destination.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, sort_keys=True, indent=2)
            stream.write('\n')
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def tuple_identity(tup):
    raw = json.dumps({key: value for key, value in tup.items() if key != 'digest'},
                     sort_keys=True, separators=(',', ':'), ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def validate(status, tup, batch_id, sequence):
    require(isinstance(batch_id, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', batch_id),
            'invalid batch id')
    require(type(sequence) is int and sequence > 0, 'invalid publication sequence')
    require(isinstance(tup, dict) and isinstance(tup.get('repos'), dict), 'invalid tuple')
    agent = tup['repos'].get('agent')
    require(isinstance(agent, dict) and isinstance(agent.get('sha'), str) and
            re.fullmatch(r'[0-9a-f]{40}', agent['sha']), 'invalid Agent tuple SHA')
    digest = tuple_identity(tup)
    require(isinstance(status, dict) and status.get('check') == CHECK and
            status.get('result') in ('pass', 'fail'), 'invalid aggregate status')
    require(status.get('batch_id') == batch_id and status.get('tuple_digest') == digest and
            status.get('target_sha') == agent['sha'], 'stale or wrong-tuple aggregate status')
    return digest, agent['sha']


def request(method, path, body, token=None, opener=urllib.request.urlopen):
    headers = {'Accept': 'application/vnd.github+json',
               'X-GitHub-Api-Version': '2022-11-28',
               'Content-Type': 'application/json'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    payload = json.dumps(body, sort_keys=True, separators=(',', ':')).encode()
    call = urllib.request.Request(API + path, data=payload, headers=headers, method=method)
    try:
        with opener(call, timeout=60) as response:
            raw = response.read()
    except (OSError, urllib.error.HTTPError) as exc:
        raise ValueError('check publication request failed: ' + str(exc)) from exc
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError('check publication returned invalid JSON') from exc


def _state(path):
    destination = Path(path)
    if not destination.exists():
        return {'version': STATE_VERSION, 'targets': {}}
    value = read_json(destination)
    require(value.get('version') == STATE_VERSION and isinstance(value.get('targets'), dict),
            'invalid publication state')
    return value


def publish(status_path, tuple_path, batch_id, sequence, state_path, token=None,
            send=request):
    """Create one terminal Check Run, rejecting stale/conflicting publications.

    ``state_path`` must be durable coordinator-owned storage.  It records only
    identities and the Check Run ID, never the token or evidence bytes.
    """
    status, tup = read_json(status_path), read_json(tuple_path)
    digest, target = validate(status, tup, batch_id, sequence)
    state = _state(state_path)
    prior = state['targets'].get(target)
    identity = {'batch_id': batch_id, 'tuple_digest': digest, 'sequence': sequence,
                'result': status['result']}
    if prior:
        require(type(prior.get('sequence')) is int, 'invalid publication target state')
        if prior['sequence'] > sequence:
            raise ValueError('stale aggregate publication sequence')
        if prior['sequence'] == sequence:
            require(all(prior.get(key) == value for key, value in identity.items()),
                    'conflicting duplicate aggregate publication')
            return prior
    require(isinstance(token, str) and token, 'missing GitHub check publication token')
    conclusion = 'success' if status['result'] == 'pass' else 'failure'
    summary = '\n'.join(status.get('errors', [])) or 'Tuple-bound aggregate completed.'
    payload = {
        'name': CHECK,
        'head_sha': target,
        'external_id': 'c06:%s:%s' % (batch_id, digest),
        'status': 'completed',
        'conclusion': conclusion,
        'output': {'title': '%s: %s' % (CHECK, status['result'].upper()),
                   'summary': summary[:60000]},
    }
    response = send('POST', '/repos/' + REPOSITORY + '/check-runs', payload, token=token)
    require(isinstance(response, dict) and type(response.get('id')) is int and
            response.get('name') == CHECK and response.get('head_sha') == target and
            response.get('status') == 'completed' and response.get('conclusion') == conclusion,
            'check publication response did not confirm the tuple-bound check')
    record = identity | {'check_run_id': response['id']}
    state['targets'][target] = record
    write_json(state_path, state)
    return record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--status', required=True)
    parser.add_argument('--tuple', required=True)
    parser.add_argument('--batch-id', required=True)
    parser.add_argument('--sequence', required=True, type=int)
    parser.add_argument('--state', required=True)
    args = parser.parse_args(argv)
    try:
        record = publish(args.status, args.tuple, args.batch_id, args.sequence, args.state,
                         token=os.environ.get('GH_TOKEN'))
    except Exception as exc:
        print('ERROR: check publication failed: %s: %s' % (type(exc).__name__, exc))
        return 1
    print(json.dumps(record, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
