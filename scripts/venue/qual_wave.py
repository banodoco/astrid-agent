#!/usr/bin/env python3
"""RC-0 wave packet validation and RunPod request staging. No implicit row adapters."""
import argparse
import hashlib
import json
import pathlib
import re
import sys

TUPLE = {
    'runtime': '47e5949465cb6f3afdfd1cb8a0e90009fd9d4020',
    'agent': 'af5ae8afdb9829beb2bba4f811dbe97765bd72b2',
    'app': 'a55bec9ff384b5bbfee5535962880084ab2adb1b',
    'pinokio': '89ed7aacae72bb48eb97ff9ff95c222bdd7e71e4',
    'vibecomfy': '29670339', 'runpod_lifecycle': '96360a27', 'sdk': '0786b3d',
}
RUNNERS = {'actions-linux': 'ubuntu-24.04', 'actions-macos': 'macos-14', 'actions-windows': 'windows-2022'}
VENUES = set(RUNNERS) | {'runpod-cpu', 'runpod-clean'}

def require(condition, message):
    if not condition:
        raise ValueError(message)

def validate(data):
    require(isinstance(data, dict), 'matrix must be a mapping')
    bound = data.get('published_tuple')
    require(isinstance(bound, dict), 'published_tuple missing')
    for key, expected in TUPLE.items():
        actual = bound.get(key)
        require(isinstance(actual, str) and (actual == expected or (len(expected) < 40 and len(actual) == 40 and re.fullmatch('[0-9a-f]{40}', actual) and actual.startswith(expected))), f'tuple mismatch or missing {key}: expected {expected}, got {actual!r}')
    rows = data.get('rows')
    require(isinstance(rows, list) and len(rows) >= 10, 'at least ten complete rows required')
    selected = rows[:10]
    seen = set()
    for n, row in enumerate(selected, 1):
        require(isinstance(row, dict), f'row {n} must be a mapping')
        rid = row.get('id')
        require(isinstance(rid, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', rid) and rid not in seen, f'row {n} invalid/duplicate id: {rid!r}')
        seen.add(rid)
        for field in ('journey', 'os', 'channel', 'venue', 'entry_command', 'acceptance'):
            require(isinstance(row.get(field), str) and row[field].strip(), f'{rid}: missing {field}')
        require(row['venue'] in VENUES, f'{rid}: unsupported venue {row["venue"]!r}')
        require(type(row.get('timeout_minutes')) is int and 1 <= row['timeout_minutes'] <= 60, f'{rid}: invalid timeout')
        inputs = row.get('inputs')
        require(isinstance(inputs, dict) and inputs.get('manifest') == 'RC-0', f'{rid}: inputs must explicitly bind RC-0')
        require(any(inputs.get(k) for k in ('row_selector', 'slot_id', 'selectors')), f'{rid}: missing selector')
        require(isinstance(inputs.get('tuple', {}), dict), f'{rid}: invalid row tuple')
        for key, value in inputs.get('tuple', {}).items():
            require(key in TUPLE and (value == bound[key]), f'{rid}: row tuple mismatch for {key}: {value!r}')
        artifacts = row.get('expected_artifacts')
        require(isinstance(artifacts, list) and artifacts and all(isinstance(x, str) and x.strip() and '\n' not in x for x in artifacts), f'{rid}: expected artifacts missing/invalid')
        # A prose instruction to dispatch another workflow is not a shell program.
        # The producer must explicitly identify an executable entry and its receipt.
        execution = row.get('execution')
        require(isinstance(execution, dict) and execution.get('shell') == 'bash' and execution.get('command') == row['entry_command'], f'{rid}: executable bash entry not explicitly supplied')
        require(isinstance(execution.get('receipt_path'), str) and execution['receipt_path'] in artifacts, f'{rid}: canonical receipt path must be an expected artifact')
        require(not row.get('depends_on'), f'{rid}: dependent row not supported')
        if row['venue'] in RUNNERS:
            require(row['os'] == row['venue'].removeprefix('actions-'), f'{rid}: OS and venue disagree')
    return selected

def prepare(args):
    import yaml
    source = pathlib.Path(args.matrix).read_bytes()
    data = yaml.safe_load(source)
    rows = validate(data)
    packet = {'matrix_sha256': hashlib.sha256(source).hexdigest(), 'published_tuple': data['published_tuple'], 'rows': rows}
    path = pathlib.Path(args.output)
    require(not path.exists(), f'refusing to overwrite {path}')
    path.write_text(json.dumps(packet, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    print(f'prepared {len(rows)} rows, sha256={packet["matrix_sha256"]}; no jobs submitted')

def matrix(args):
    packet = json.loads(pathlib.Path(args.packet).read_text(encoding='utf-8'))
    rows = validate(packet)
    if args.source:
        require(hashlib.sha256(pathlib.Path(args.source).read_bytes()).hexdigest() == packet.get('matrix_sha256'), 'matrix source hash mismatch')
    include = [{'id': r['id'], 'runner': RUNNERS[r['venue']], 'timeout': r['timeout_minutes']} for r in rows if r['venue'] in RUNNERS]
    require(include, 'no Actions rows: do not dispatch an empty matrix')
    print(json.dumps({'include': include}, separators=(',', ':')))

def stage(args):
    packet = json.loads(pathlib.Path(args.packet).read_text(encoding='utf-8'))
    rows = validate(packet)
    root = pathlib.Path(args.queue).resolve()
    target_rows = [r for r in rows if r['venue'].startswith('runpod-')]
    targets = [(r, root / ('V-08-QUAL-WAVE-' + r['id'])) for r in target_rows]
    for r, target in targets:
        require(not target.exists(), f'refusing to overwrite {target}')
    # No command rewriting or asserted verdict. The broker provides the result.
    for row, target in targets:
        target.mkdir()
        job = {'stream': args.stream, 'image': 'default', 'reuse': False, 'gpu': False,
               'max_minutes': row['timeout_minutes'], 'evidence_lane': 'lane/M-VENUE/V-08-QUAL-WAVE-' + row['id'],
               'repos': {'astrid-agent': TUPLE['agent'], 'astrid-runtime': TUPLE['runtime'], 'astrid-app': TUPLE['app'], 'astrid-pinokio': TUPLE['pinokio']},
               'setup': [], 'run': [row['entry_command']], 'artifacts': row['expected_artifacts']}
        (target / 'runpod-job.json').write_text(json.dumps(job, indent=2) + '\n')
        (target / 'tier').write_text('luna\n')
        (target / 'brief.md').write_text('# ' + row['id'] + '\n\nRun the root runpod-job.json once. Preserve the broker result, inner exit, requested artifacts and row receipt. Report infrastructure failures separately; never infer qualification from job success.\n\nMatrix SHA-256: ' + packet['matrix_sha256'] + '\n\nRow:\n```json\n' + json.dumps(row, indent=2) + '\n```\n')
        print(target)

def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest='action', required=True)
    a = sub.add_parser('prepare'); a.add_argument('matrix'); a.add_argument('output'); a.set_defaults(fn=prepare)
    a = sub.add_parser('matrix'); a.add_argument('packet'); a.add_argument('--source'); a.set_defaults(fn=matrix)
    a = sub.add_parser('stage-runpod'); a.add_argument('packet'); a.add_argument('queue'); a.add_argument('--stream', required=True); a.set_defaults(fn=stage)
    args = p.parse_args()
    try: args.fn(args)
    except (ValueError, OSError, KeyError) as exc:
        print(f'qual-wave: {exc}', file=sys.stderr); return 2
    return 0
if __name__ == '__main__': sys.exit(main())
