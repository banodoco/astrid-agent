#!/usr/bin/env python3
"""Selected-install admission gate. Never treats diagnostic evidence as custody.

The current Agent graph cannot bind install and the current observer refuses.
This unit therefore records the first actual graph refusal and never dispatches.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import resource
import stat
import subprocess
import sys

if __package__:
    from scripts.m46_selected_install_preflight import (REFS, SOURCE_HASHES, Refusal,
        absolute_existing_directory, exact_keys, git, pairs_no_duplicates, validate_checkout)
else:
    from m46_selected_install_preflight import (REFS, SOURCE_HASHES, Refusal,
        absolute_existing_directory, exact_keys, git, pairs_no_duplicates, validate_checkout)

REQUEST_SCHEMA = 'astrid.m46.selected-install-executor-request.v1'
RESULT_SCHEMA = 'astrid.m46.selected-install-executor-result.v1'
BASE = '0e4a4d5b50aeddd2998f371e56a8f73ca0469831'
MAX_OUTPUT = 65536
GRAPH_CODE = r'''import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from scripts.m4_6_pinokio_custody_provider import PinokioActionGraph, PinokioCustodyRefused
try:
    graph = PinokioActionGraph.collect(Path(sys.argv[2]))
except PinokioCustodyRefused as exc:
    print(type(exc).__name__ + ': ' + str(exc), file=sys.stderr)
    sys.exit(3)
print(json.dumps({'graph_sha256': graph.graph_sha256, 'roles': [f.role for f in graph.files]}, sort_keys=True))
'''


def validate_shape(request):
    exact_keys(request, ('schema', 'runner_head', 'request_id', 'action', 'mode', 'checkouts', 'output_directory'), 'request')
    if request['schema'] != REQUEST_SCHEMA or request['action'] != 'install' or request['mode'] != 'backend':
        raise Refusal('unsupported schema, action or mode')
    if not isinstance(request['runner_head'], str) or not re.fullmatch('[0-9a-f]{40}', request['runner_head']):
        raise Refusal('runner_head must be a full lowercase commit')
    if not isinstance(request['request_id'], str) or not re.fullmatch('[0-9a-f]{32}', request['request_id']):
        raise Refusal('request_id must be 32 lowercase hexadecimal characters')
    exact_keys(request['checkouts'], (*REFS, 'runner'), 'checkouts')


def validate_request(request, script=Path(__file__)):
    validate_shape(request)
    roots = {k: absolute_existing_directory(v, 'checkouts.' + k) for k, v in request['checkouts'].items()}
    output = absolute_existing_directory(request['output_directory'], 'output_directory')
    paths = [*roots.values(), output]
    if any(a == b or a in b.parents or b in a.parents for i, a in enumerate(paths) for b in paths[i+1:]):
        raise Refusal('checkout and output directories must be disjoint')
    if script.resolve(strict=True) != roots['runner'] / 'scripts/m46_selected_install_executor.py':
        raise Refusal('executable is not from the selected runner checkout')
    for name, ref in {**REFS, 'runner': request['runner_head']}.items():
        validate_checkout(roots[name], ref)
    head = request['runner_head']
    if git(roots['runner'], 'rev-list', '--parents', '-n', '1', head).split() != [head, BASE]:
        raise Refusal('runner must be a direct single-parent child of the fixed base')
    info = output.stat()
    if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700 or any(output.iterdir()):
        raise Refusal('output directory must be empty, owned by caller, and mode 0700')
    hashes = {}
    for (name, relative), expected in SOURCE_HASHES.items():
        file = roots[name] / relative
        if file.is_symlink() or file.resolve(strict=True) != file:
            raise Refusal('source path is symlinked')
        digest = hashlib.sha256(file.read_bytes()).hexdigest()
        if digest != expected:
            raise Refusal('source hash mismatch: ' + name + '/' + relative)
        hashes[name + '/' + relative] = digest
    manifest = json.loads((roots['app'] / 'public/install-manifest.json').read_bytes(), object_pairs_hook=pairs_no_duplicates)
    if manifest.get('agent', {}).get('sha') != REFS['agent'] or manifest.get('runtime', {}).get('sha') != REFS['runtime']:
        raise Refusal('manifest tuple mismatch')
    if git(roots['pinokio'], 'rev-parse', 'HEAD^{tree}') != '837855e0c3aee7148f76d43faa020a3d01bd0959':
        raise Refusal('Pinokio tree mismatch')
    if git(roots['pinokio'], 'rev-list', '--parents', '-n', '1', REFS['pinokio']).split() != [REFS['pinokio'], '969c2810412b7df34334cf10e589454c4a9c4f93']:
        raise Refusal('Pinokio parent mismatch')
    return roots, output, hashes


def limited_file(path):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    return os.fdopen(fd, 'wb')


def capture_graph(roots, output):
    # Restrict file size in the child, retain the actual bytes (including on error),
    # and never confuse this validator's exit with an install action exit.
    command = [sys.executable, '-I', '-B', '-c', GRAPH_CODE, str(roots['agent']), str(roots['pinokio'])]
    def limit():
        resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_OUTPUT, MAX_OUTPUT))
    with limited_file(output / 'graph.stdout') as stdout, limited_file(output / 'graph.stderr') as stderr:
        try:
            process = subprocess.run(command, cwd='/', env={'PATH': os.defpath, 'HOME': '/nonexistent'},
                                     stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                                     timeout=30, check=False, preexec_fn=limit)
            exit_code = process.returncode
        except subprocess.TimeoutExpired:
            exit_code = None
    files = {}
    for name in ('graph.stdout', 'graph.stderr'):
        data = (output / name).read_bytes()
        files[name] = {'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data)}
    return command, exit_code, files


def run(request):
    if sys.platform != 'linux':
        raise Refusal('Linux is required')
    roots, output, hashes = validate_request(request)
    command, exit_code, files = capture_graph(roots, output)
    # No current Agent graph includes install: even a successful start/stop/update
    # graph must not be treated as authorizing this different action.
    if exit_code == 3:
        reason = 'agent-action-graph-refused'
    elif exit_code != 0:
        reason = 'agent-action-graph-check-failed'
    else:
        reason = 'install-not-covered-by-agent-action-graph'
    result = {'schema': RESULT_SCHEMA, 'status': 'blocked', 'reason': reason,
              'request_id': request['request_id'], 'runner_head': request['runner_head'],
              'request_sha256': hashlib.sha256(json.dumps(request, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()).hexdigest(),
              'tuple': REFS, 'action': 'pinokio/install.js', 'mode': 'backend',
              'source_sha256': hashes, 'graph_check': {'argv': command, 'exit_code': exit_code, 'files': files},
              'action_invoked': False, 'action_exit': None, 'readback': None,
              'observer_binding': 'not-produced', 'settlement': 'unknown', 'slot_receipt': 'not-produced'}
    with limited_file(output / 'result.json') as stream:
        stream.write((json.dumps(result, sort_keys=True, indent=2) + '\n').encode())
    return 2


def main(argv):
    if len(argv) != 2 or not os.path.isabs(argv[1]):
        print('usage: m46_selected_install_executor.py /absolute/request.json', file=sys.stderr)
        return 2
    try:
        with open(argv[1], 'rb') as stream:
            raw = stream.read(65537)
        if len(raw) > 65536:
            raise Refusal('request exceeds size limit')
        return run(json.loads(raw, object_pairs_hook=pairs_no_duplicates))
    except (ValueError, OSError, UnicodeError) as exc:
        print('selected-install refused: ' + str(exc), file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main(sys.argv))
