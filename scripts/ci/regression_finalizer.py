#!/usr/bin/env python3
"""Trusted local caller: collect evidence and always invoke the aggregate."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile

import regression_aggregate as aggregate


TERMINAL_STATES = frozenset(('success', 'failure', 'cancelled', 'worker-death',
                             'skipped', 'unstartable'))


def completed_producers(path):
    """Read the coordinator's terminal result list without running producers."""
    results = json.loads(Path(path).read_text(encoding='utf-8'))
    if not isinstance(results, list) or not results:
        raise ValueError('completed producers must be a nonempty list')
    seen = set()
    for entry in results:
        if not isinstance(entry, dict) or set(entry) != {'id', 'state'}:
            raise ValueError('invalid completed producer record')
        identifier, state = entry['id'], entry['state']
        if (not isinstance(identifier, str) or not identifier or identifier in seen or
                state not in TERMINAL_STATES):
            raise ValueError('invalid or duplicate completed producer')
        seen.add(identifier)
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('status', 'tuple', 'ledger', 'broker-root', 'freshness', 'producer-plan'):
        parser.add_argument('--' + name, required=name != 'producer-plan')
    parser.add_argument('--completed-producers')
    parser.add_argument('--batch-id')
    parser.add_argument('--not-before', default=aggregate.CUTOFF)
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        if exc.code == 0:
            return 0
        # The aggregate locates --status even when its arguments are invalid.
        return aggregate.main(list(sys.argv[1:] if argv is None else argv))
    with tempfile.TemporaryDirectory(prefix='regression-finalizer-') as tmp:
        root = Path(tmp)
        receipt = root / 'collection.json'
        hosted_root = root / 'hosted'
        hosted_root.mkdir(mode=0o700)
        collection, prerequisites = 'failure', 'failure'
        try:
            if bool(args.producer_plan) == bool(args.completed_producers):
                raise ValueError('supply exactly one of producer plan or completed producers')
            if args.completed_producers:
                results = completed_producers(args.completed_producers)
                prerequisites = ('success' if all(row['state'] == 'success' for row in results)
                                 else 'failure')
            else:
                plan = json.loads(Path(args.producer_plan).read_text(encoding='utf-8'))
                if not isinstance(plan, list) or not plan or any(not isinstance(cmd, list) or not cmd or any(not isinstance(v, str) or not v for v in cmd) for cmd in plan):
                    raise ValueError('producer plan must be a nonempty list of command argument lists')
                results = []
                for command in plan:
                    try:
                        results.append(subprocess.run(command, check=False).returncode)
                    except OSError as exc:
                        print(f'producer failed to start: {type(exc).__name__}: {exc}', file=sys.stderr)
                        results.append(None)
                prerequisites = 'success' if all(code == 0 for code in results) else 'failure'
            review = aggregate.read(aggregate.REVIEW)
            if review.get('complete') is not True:
                raise ValueError('reviewed inventory incomplete')
            import trusted_hosted_download as hosted
            hosted.download(aggregate.read(args.tuple), review['lanes'], hosted_root)
            collection = 'success'
        except Exception as exc:
            print(f'collection failed: {type(exc).__name__}: {exc}', file=sys.stderr)
        finally:
            receipt.write_text(json.dumps({'collection': collection, 'prerequisites': prerequisites}), encoding='utf-8')
            return aggregate.main(['--status', args.status, '--tuple', args.tuple, '--ledger', args.ledger,
                '--broker-root', args.broker_root, '--hosted-root', str(hosted_root), '--freshness', args.freshness,
                '--collection', str(receipt), '--not-before', args.not_before,
                *(['--batch-id', args.batch_id] if args.batch_id else [])])


if __name__ == '__main__':
    sys.exit(main())
