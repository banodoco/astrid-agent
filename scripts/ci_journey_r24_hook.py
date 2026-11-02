#!/usr/bin/env python3
"""Invoke the pinned R-24 collector; output is not a hosted qualification receipt."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import signal
import subprocess
import sys

if __package__:
    from . import ci_journey_provider as typed
else:
    import ci_journey_provider as typed

REF = '6bf95fd631864a5dbfa86df5ffc165493fffc41e'
SOURCES = {
    'pinokio/providers/r24-linux.py': '9428f7b1224fdad6c29b037904920d4c49b80f676454c859a180f1a98f832392',
    'pinokio/providers/r24-action.cjs': '042f533200723898aba2f15f78edfe98bc3197f9b161f4391b706a38469ca0f3',
    'pinokio/providers/r24-live-action.js': '0df87acefad6a365e97f7a846a7d6136e06d026840a10796f640396a7187ae78',
    'pinokio/providers/r24-linux-binding.json': '11ec4932731b93bc0622f934a8974bcdcfb2350d932037e7a087a878f8c9834c',
    'pinokio/providers/r24-schema.json': '6cd5dbc55a11f607f184c6e1ce281ea10633b01fb1a739df4b12673525d48421',
}
CANDIDATE = {'app': '5c95abb00525e1a77d00c32042b6453d9f14f4e7',
             'agent': 'e425903f7a9fa5b82973281d15054c0716b46e2d',
             'runtime': '5453366458f7b4ee874c94d15ae7952e3e82d3c3',
             'pinokio': '2f085c5fc0cde192ac4a022065883c85dcccd0de',
             'manifest_sha256': 'a6eef2804e58e198d56899f1b36d97688d5cbc4a0847a82078ba144df4a207eb'}
STOP = {'ref': CANDIDATE['pinokio'], 'path': 'pinokio/stop.js',
        'sha256': 'f9d62e4e6a826fccd9791ee211dae933f46c8f99019e145abb3db3bf9595fd48'}
MEMBERS = {'junit.xml', 'manifest.json', 'observation-result.json',
           'observation-busy.json', 'observation-unknown.json', 'observation-restoration.json'}


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def identity(env: dict[str, str]) -> dict[str, str]:
    keys = ('GITHUB_REPOSITORY', 'GITHUB_RUN_ID', 'GITHUB_RUN_ATTEMPT', 'GITHUB_JOB',
            'GITHUB_WORKFLOW_REF', 'GITHUB_WORKFLOW_SHA', 'GITHUB_SHA', 'RUNNER_NAME', 'RUNNER_OS')
    if sys.platform != 'linux' or env.get('GITHUB_ACTIONS') != 'true' or env.get('RUNNER_OS') != 'Linux':
        raise ValueError('Linux Actions environment required')
    if any(not env.get(k) or len(env[k]) > 512 or any(ord(c) < 32 for c in env[k]) for k in keys):
        raise ValueError('incomplete runner identity')
    if (not re.fullmatch(r'[^/\s]+/[^/\s]+', env['GITHUB_REPOSITORY']) or
        any(not re.fullmatch(r'[1-9][0-9]*', env[k]) for k in ('GITHUB_RUN_ID', 'GITHUB_RUN_ATTEMPT')) or
        any(not re.fullmatch(r'[0-9a-f]{40}', env[k]) for k in ('GITHUB_WORKFLOW_SHA', 'GITHUB_SHA'))):
        raise ValueError('invalid runner identity')
    return {k: env[k] for k in keys}


def verify_checkout(root: Path) -> None:
    if not root.is_absolute() or root.is_symlink() or not root.is_dir():
        raise ValueError('absolute producer checkout required')
    root = root.resolve(strict=True)
    def git(*args: str) -> bytes:
        return subprocess.check_output(['git', '-C', str(root), *args], stderr=subprocess.DEVNULL, timeout=30).strip()
    if (Path(os.fsdecode(git('rev-parse', '--show-toplevel'))).resolve() != root or
        git('rev-parse', 'HEAD').decode() != REF or git('status', '--porcelain=v1', '--untracked-files=all')):
        raise ValueError('producer identity or cleanliness mismatch')
    for name, digest in SOURCES.items():
        path = root / name
        if path.is_symlink() or not path.is_file() or sha(path.read_bytes()) != digest:
            raise ValueError('producer source mismatch')
        blob = subprocess.check_output(['git', '-C', str(root), 'show', REF + ':' + name], timeout=30)
        if sha(blob) != digest:
            raise ValueError('producer object mismatch')


def verify_export(directory: Path, request: typed.ProviderRequest, stdout: bytes, config: dict) -> tuple[dict, str]:
    if directory.is_symlink() or not directory.is_dir() or {p.name for p in directory.iterdir()} != MEMBERS:
        raise ValueError('export must contain exactly six files')
    contents = {}
    for name in MEMBERS:
        path = directory / name
        if path.is_symlink() or not path.is_file():
            raise ValueError('export member is not a regular file')
        contents[name] = path.read_bytes()
    manifest = typed._decode(contents['manifest.json'])
    typed._object(manifest, {'schema', 'qualification', 'files'}, 'export manifest')
    if manifest['schema'] != 'astrid.r24.producer-export.v1' or manifest['qualification'] != 'not-evaluated':
        raise ValueError('invalid export manifest')
    expected = [{'path': name, 'bytes': len(contents[name]), 'sha256': sha(contents[name])}
                for name in sorted(MEMBERS - {'manifest.json'})]
    if manifest['files'] != expected:
        raise ValueError('export inventory mismatch')
    result = typed.ProviderResult.parse(stdout, request)
    if typed._decode(stdout) != typed._decode(contents['observation-result.json']):
        raise ValueError('export result mismatch')
    request_data = typed._decode(request.canonical)
    sessions = set()
    engines = []
    values = {}
    for observation in result.observations:
        data = contents['observation-' + observation.kind + '.json']
        if observation.source != 'pinokio/providers/r24-action.cjs' or sha(data) != observation.evidence_sha256:
            raise ValueError('observation digest mismatch')
        value = typed._decode(data)
        if (not isinstance(value, dict) or value.get('schema') != 'astrid.r24.live-observation.v1' or
            value.get('kind') != observation.kind or value.get('request_sha256') != request.sha256 or
            value.get('run_id') != request_data['run_id'] or value.get('provider_ref') != REF or
            not isinstance(value.get('session'), str) or not re.fullmatch(r'[0-9a-f]{32}', value['session'])):
            raise ValueError('observation identity mismatch')
        sessions.add(value['session'])
        engines.append(value.get('engine'))
        values[observation.kind] = value
    if len(sessions) != 1:
        raise ValueError('producer sessions differ')
    session = sessions.pop()
    engine = config['engine']
    expected_engine = {key: engine[key] for key in ('pid', 'boot_id', 'start_ticks', 'port')}
    expected_engine['node_sha256'] = config['node_sha256']
    if any(value != expected_engine for value in engines):
        raise ValueError('engine identity differs from invocation')
    live = '~/api/astrid/pinokio/providers/r24-live-action.js?r24-session=' + session
    stop = '~/api/astrid/pinokio/stop.js?r24-session=' + session
    try:
        busy, unknown, restoration = (values[k] for k in ('busy', 'unknown', 'restoration'))
        if (busy['engine_binding']['request_id'] != live + '-probe' or
            busy['busy']['stop_refused'] is not True or
            busy['busy']['fence']['request_id'] != stop + '-busy-stop' or
            unknown['unknown']['fence']['request_id'] != live + '-unknown' or
            restoration['restoration']['request_id'] != live + '-restoration'):
            raise ValueError('inner action session binding mismatch')
    except (KeyError, TypeError) as exc:
        raise ValueError('inner action result unavailable') from exc
    return {name: {'bytes': len(data), 'sha256': sha(data)} for name, data in sorted(contents.items())}, session


def invoke(config_raw: bytes, producer_root: Path, output: Path, env: dict[str, str]) -> dict:
    session = identity(env)
    config = typed._decode(config_raw)
    typed._object(config, {'checkouts', 'pterm', 'pinokiod', 'node', 'node_sha256', 'pterm_entry_sha256',
                           'unws_sha256', 'engine', 'scratch_root'}, 'config')
    typed._object(config['checkouts'], {'app', 'agent', 'runtime', 'pinokio'}, 'checkouts')
    if any(not isinstance(p, str) or not os.path.isabs(p) for p in config['checkouts'].values()):
        raise ValueError('absolute candidate checkouts required')
    typed._object(config['engine'], {'pid', 'boot_id', 'start_ticks', 'pinokio_home', 'port'}, 'engine')
    config_bytes = typed._canonical(config)
    request = typed.ProviderRequest.parse(typed._canonical({'schema': typed.REQUEST_SCHEMA, 'candidate': CANDIDATE,
        'provider': {'ref': REF, 'sources': SOURCES}, 'stop': STOP, 'config_sha256': sha(config_bytes),
        'run_id': secrets.token_hex(16)}))
    verify_checkout(producer_root)
    if not output.is_absolute() or output.exists() or output.is_symlink():
        raise ValueError('new absolute output directory required')
    parent = output.parent.resolve(strict=True)
    if parent != output.parent or not parent.is_dir():
        raise ValueError('output parent must be an existing non-symlink path')
    protected_roots = [producer_root.resolve()] + [Path(p).resolve() for p in config['checkouts'].values()]
    if any(parent == root or root in parent.parents for root in protected_roots):
        raise ValueError('output must be outside source checkouts')
    output.mkdir(mode=0o700)
    receipt = {'schema': 'astrid.r24.runner-invocation.v1', 'status': 'AWAITING_EVIDENCE',
        'qualification': 'not-evaluated', 'dispatch_unverified': session, 'producer_ref': REF,
        'request_sha256': request.sha256, 'config_sha256': sha(config_bytes), 'exit_code': None,
        'inner_action_exit_code': None, 'inner_action_stderr': None,
        'custody': 'not-authenticated', 'export': None, 'producer_session': None}
    (output / 'request.json').write_bytes(request.canonical)
    (output / 'config.json').write_bytes(config_bytes)
    try:
        proc = subprocess.Popen([sys.executable, '-B', str(producer_root / 'pinokio/providers/r24-linux.py'),
                                 str(output / 'request.json'), str(output / 'config.json'), str(output / 'export')],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        try:
            stdout, stderr = proc.communicate(timeout=240)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            stdout, stderr = proc.communicate()
            receipt['timed_out'] = True
        receipt['exit_code'] = proc.returncode
        (output / 'stdout.bin').write_bytes(stdout)
        (output / 'stderr.bin').write_bytes(stderr)
        receipt['stdout_sha256'] = sha(stdout)
        receipt['stderr_sha256'] = sha(stderr)
        if proc.returncode != 0 or receipt.get('timed_out'):
            raise ValueError('producer failed or timed out')
        receipt['export'], receipt['producer_session'] = verify_export(output / 'export', request, stdout, config)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        receipt['error'] = str(exc)
    finally:
        (output / 'receipt.json').write_bytes(typed._canonical(receipt) + b'\n')
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--producer-root', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    try:
        result = invoke(args.config.read_bytes(), args.producer_root, args.output, dict(os.environ))
        print(json.dumps(result, sort_keys=True))
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f'R-24 invocation rejected: {exc}', file=sys.stderr)
    # No authenticated custody or complete inner action result is available yet.
    return 1


if __name__ == '__main__':
    sys.exit(main())
