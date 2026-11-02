#!/usr/bin/env python3
"""Exercise the comparator with fabricated bytes; never touch a real capture index."""
import copy
import hashlib
import json
import pathlib
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
COMPARATOR = HERE.parent / 'comparator.py'
FIXTURE = json.loads((HERE / 'fixtures.json').read_text())
SURFACES = 'options errors preview inputs raw_engine media witnesses receipt manifest delivery export lifecycle shared'.split()

def encoded(obj):
    return (json.dumps(obj, sort_keys=True, indent=2) + '\n').encode()

def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)

def meta(path, data):
    return {'path': path, 'sha256': hashlib.sha256(data).hexdigest(), 'size': len(data)}

def make_capture(root):
    items = []
    for surface in SURFACES:
        kind = 'bytes' if surface in ('media', 'raw_engine') else 'json'
        data = FIXTURE['media'].encode() if kind == 'bytes' else encoded(FIXTURE['json_baseline'])
        rel = 'artifacts/' + surface + ('.bin' if kind == 'bytes' else '.json')
        write(root / rel, data)
        items.append(dict(meta(rel, data), surface=surface, kind=kind))
    evidence = b'fabricated supporting evidence\n'
    write(root / 'evidence.txt', evidence)
    return {'schema': 'h3-capture/v1', 'tuple': FIXTURE['tuple'],
            'coverage': {f'P{i}': 'captured' for i in range(1, 13)},
            'cases': [{'id': 'synthetic-only', 'inputs_sha256': hashlib.sha256(FIXTURE['inputs'].encode()).hexdigest(),
                       'artifacts': items, 'evidence': [meta('evidence.txt', evidence)]}]}

def run_check(name, expected_code, expected_status, mutate=None, verify=None):
    with tempfile.TemporaryDirectory(prefix='.g0-synthetic-', dir=pathlib.Path.cwd()) as tmp:
        root = pathlib.Path(tmp)
        left, right = root / 'baseline', root / 'candidate'
        base, candidate = make_capture(left), make_capture(right)
        allowed = {'schema': 'h3-allowed-diff/v1', 'differences': []}
        if mutate:
            mutate(root, candidate, allowed)
        write(left / 'capture-index.json', encoded(base))
        write(right / 'capture-index.json', encoded(candidate))
        write(root / 'allowed.json', encoded(allowed))
        result = subprocess.run([sys.executable, str(COMPARATOR), str(left / 'capture-index.json'),
                                 str(right / 'capture-index.json'), str(root / 'allowed.json'),
                                 str(root / 'comparison.json')], capture_output=True, text=True)
        if result.returncode != expected_code:
            raise AssertionError(f'{name}: exit {result.returncode}, expected {expected_code}; {result.stderr}')
        report = json.loads((root / 'comparison.json').read_text())
        if report['schema'] != 'h3-comparison/v1' or report['status'] != expected_status:
            raise AssertionError(f'{name}: unexpected report {report}')
        if verify:
            verify(report)
    print(f'PASS {name}: comparator exit={expected_code}, status={expected_status}')

def item(capture, surface):
    return next(a for a in capture['cases'][0]['artifacts'] if a['surface'] == surface)

def change_receipt(root, candidate, allowed, approve):
    a = item(candidate, 'receipt')
    data = encoded(dict(FIXTURE['json_baseline'], revision=2))
    write(root / 'candidate' / a['path'], data)
    a.update(meta(a['path'], data))
    if approve:
        allowed['differences'].append({'case': 'synthetic-only', 'surface': 'receipt', 'pointer': '/revision', 'old': 1, 'new': 2, 'reason': 'synthetic-only test approval'})

def verify_allowed(report):
    diffs = report['cases'][0]['differences']
    assert len(diffs) == 1 and diffs[0]['pointer'] == '/revision' and diffs[0]['allowed'] is True

def verify_unapproved(report):
    diffs = report['cases'][0]['differences']
    assert len(diffs) == 1 and diffs[0]['pointer'] == '/revision' and diffs[0]['allowed'] is False

def require_error(text):
    def check(report):
        assert text in report.get('error', ''), report
    return check

def coverage(root, capture, allowed):
    capture['coverage']['P4'] = 'uncaptured'

def missing_surface(root, capture, allowed):
    capture['cases'][0]['artifacts'] = [a for a in capture['cases'][0]['artifacts'] if a['surface'] != 'export']

def unsafe(path):
    def change(root, capture, allowed):
        item(capture, 'media')['path'] = path
    return change

def symlink(root, capture, allowed):
    a = item(capture, 'media')
    (root / 'candidate' / 'link').symlink_to('artifacts', target_is_directory=True)
    a['path'] = 'link/media.bin'

def bad_digest(root, capture, allowed):
    item(capture, 'media')['sha256'] = '0' * 64

def bad_size(root, capture, allowed):
    item(capture, 'media')['size'] += 1

def bad_evidence(root, capture, allowed):
    capture['cases'][0]['evidence'][0]['sha256'] = '0' * 64

def forbidden(root, capture, allowed):
    allowed['differences'].append({'case': 'synthetic-only', 'surface': 'media', 'pointer': '/revision', 'old': 1, 'new': 2, 'reason': 'synthetic'})

checks = [
    ('equal evidence', 0, 'PASS', None, None),
    ('exact allowed JSON leaf', 0, 'PASS', lambda r, c, a: change_receipt(r, c, a, True), verify_allowed),
    ('unapproved JSON leaf', 1, 'FAIL', lambda r, c, a: change_receipt(r, c, a, False), verify_unapproved),
    ('missing P coverage', 2, 'BLOCKED', coverage, require_error('uncaptured P coverage')),
    ('missing required surface', 2, 'BLOCKED', missing_surface, lambda report: (require_error('missing surface/case')(report), 'export' in report['missing_surfaces'] or (_ for _ in ()).throw(AssertionError(report)))),
    ('unsafe parent path', 2, 'BLOCKED', unsafe('../outside'), require_error('unsafe artifact path')),
    ('unsafe absolute path', 2, 'BLOCKED', unsafe('/outside'), require_error('unsafe artifact path')),
    ('malformed path', 2, 'BLOCKED', unsafe('artifacts//media.bin'), require_error('unsafe artifact path')),
    ('symlink component', 2, 'BLOCKED', symlink, require_error('symlink artifact')),
    ('digest mismatch', 2, 'BLOCKED', bad_digest, require_error('artifact hash/size')),
    ('size mismatch', 2, 'BLOCKED', bad_size, require_error('artifact hash/size')),
    ('evidence digest mismatch', 2, 'BLOCKED', bad_evidence, require_error('artifact hash/size')),
    ('forbidden media allowance', 2, 'BLOCKED', forbidden, require_error('forbidden allowed diff')),
]
print('SYNTHETIC ONLY: fabricated tuple and captured labels are not R8 evidence.')
for check in checks:
    run_check(*check)
print(f'SYNTHETIC CHECKS PASS: {len(checks)}/{len(checks)}; equal=1 allowed=1 rejected=1 blocked=10')
