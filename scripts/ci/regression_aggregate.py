#!/usr/bin/env python3
"""Always call locally after collection; write the int/regression-green status."""
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import sys
import tempfile
import re

ROOT = Path(__file__).resolve().parents[2]
COVERAGE = ROOT / '.github/integration/cp1-coverage.json'
# This is a reviewed, version-controlled authority, never an evidence-provider input.
REVIEW = ROOT / '.github/integration/cp1-reviewed-inventory.json'
CP1 = {
    'runtime': '3b12fface79a3be029f3b6623765a7f3e1e5e24f',
    'agent': '0a47bf37e309741ef2a90d18739fa94758cf8f3f',
    'app': '8a05d31ae702ad83d8e61867d82f0fc2feb52ce3',
    'pinokio': '8d8e1b46948f6a0cdf8d3f81f4db3f14c21f11b2',
}
CUTOFF = '2026-10-31T23:36:50Z'


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def evaluate(args):
    # Imports are deliberately deferred: missing evaluator dependencies must still emit red.
    import local_regression_green as local
    import regression_green as core
    tup = read(args.tuple)
    core.validate_schema(tup, 'tuple.schema.json')
    if set(tup['repos']) != set(CP1):
        raise ValueError('CP1 requires exactly four repositories')
    for repo, sha in CP1.items():
        if tup['repos'][repo]['sha'] != sha or tup['repos'][repo]['url'] != 'https://github.com/banodoco/astrid-' + repo:
            raise ValueError('wrong CP1 repository or SHA: ' + repo)
    if local.timestamp(args.not_before) < local.timestamp(CUTOFF):
        raise ValueError('cutoff predates CP1 publication')
    receipt = read(args.collection)
    if receipt != {'collection': 'success', 'prerequisites': 'success'}:
        raise ValueError('collection or prerequisite did not succeed')
    coverage = read(COVERAGE)
    reviewed = read(REVIEW)
    if reviewed.get('complete') is not True:
        raise ValueError('CP1 test-node and source inventory is not reviewed and complete')
    identity = core.digest({k: v for k, v in tup.items() if k != 'digest'})
    if reviewed.get('tuple_digest') != identity:
        raise ValueError('reviewed inventory tuple mismatch')
    features = reviewed.get('features')
    required_features = coverage.get('features')
    if reviewed.get('features_complete') is not True or not isinstance(features, list) or not features or not isinstance(required_features, list) or not required_features:
        raise ValueError('feature coverage and reviewed feature inventory are missing or incomplete')
    feature_key = lambda x: tuple(x[k] for k in ('repo', 'lane', 'platform', 'scenario', 'slot_id'))
    if (len({feature_key(x) for x in features}) != len(features) or
            len({feature_key(x) for x in required_features}) != len(required_features) or
            {feature_key(x) for x in features} != {feature_key(x) for x in required_features}):
        raise ValueError('reviewed feature inventory does not match required coverage')
    lanes = reviewed['lanes']
    key = lambda x: (x['repo'], x['lane'], x['platform'])
    required = coverage['lanes']
    if (len({key(x) for x in required}) != len(required) or
            len({key(x) for x in lanes}) != len(lanes) or
            {key(x) for x in required} != {key(x) for x in lanes}):
        raise ValueError('reviewed inventory does not match fixed G3 coverage')
    by_key = {key(x): x for x in lanes}
    for row in required:
        lane = by_key[key(row)]
        if lane['source']['kind'] != row['kind']:
            raise ValueError('wrong evidence transport: ' + str(key(row)))
        if row['protected'] and not all(t.get('protected') is True for t in lane['tests']):
            raise ValueError('protected lane contains waivable tests: ' + str(key(row)))
    with tempfile.TemporaryDirectory(prefix='regression-aggregate-') as tmp:
        inventory = Path(tmp) / 'inventory.json'
        inventory.write_text(json.dumps({'tuple_digest': identity, 'lanes': lanes}), encoding='utf-8')
        return local.local_evaluate(args.tuple, inventory, COVERAGE, args.ledger,
                                    args.broker_root, args.hosted_root, args.freshness,
                                    args.not_before, features=features)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--status', default='regression-green-status.json')
    for name in ('tuple', 'ledger', 'broker-root', 'hosted-root', 'freshness', 'collection'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--not-before', default=CUTOFF)
    parser.add_argument('--batch-id')
    # Find the output location even when normal argument parsing fails.
    raw = list(sys.argv[1:] if argv is None else argv)
    status_path = 'regression-green-status.json'
    for i, arg in enumerate(raw):
        if arg.startswith('--status=') and arg != '--status=':
            status_path = arg.split('=', 1)[1]
        elif arg == '--status' and i + 1 < len(raw) and not raw[i + 1].startswith('--'):
            status_path = raw[i + 1]
    status = {'check': 'int/regression-green', 'result': 'fail', 'errors': [], 'retirement': [],
              'evaluated_at': dt.datetime.now(dt.timezone.utc).isoformat(), 'repo_shas': CP1,
              'batch_id': None, 'tuple_digest': None, 'target_sha': None}
    try:
        args = parser.parse_args(argv)
        if args.batch_id is not None and (not isinstance(args.batch_id, str) or
                                          not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', args.batch_id)):
            raise ValueError('invalid batch id')
        tup = read(args.tuple)
        import regression_green as core
        core.validate_schema(tup, 'tuple.schema.json')
        status.update(batch_id=args.batch_id,
                      tuple_digest=core.digest({k: v for k, v in tup.items() if k != 'digest'}),
                      target_sha=tup['repos']['agent']['sha'])
        errors, retirement = evaluate(args)
        status.update(errors=errors, retirement=retirement, result='fail' if errors else 'pass')
    except SystemExit as exc:
        if exc.code == 0:
            return 0
        status['errors'] = ['invalid or missing aggregate arguments']
    except Exception as exc:
        status['errors'] = [f'aggregate failed: {type(exc).__name__}: {exc}']
    try:
        output = Path(status_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix='.regression-green-', dir=output.parent)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                json.dump(status, stream, sort_keys=True, indent=2)
                stream.write('\n')
            os.replace(temporary, output)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    except Exception as exc:
        status['result'] = 'fail'
        status['errors'].append(f'cannot publish status: {type(exc).__name__}: {exc}')
    print(json.dumps(status, sort_keys=True))
    return 0 if status['result'] == 'pass' else 1


if __name__ == '__main__':
    sys.exit(main())
