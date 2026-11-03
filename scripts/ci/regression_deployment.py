#!/usr/bin/env python3
"""Host deployment adapter for the sealed C-06 coordinator/finalizer seam.

This adapter deliberately has no producer launcher, shell command, publisher
credential or network client.  M-INT's supervised host owns those outer seams;
this code makes their handoff durable and fail closed.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import sys
from pathlib import Path

import regression_aggregate as aggregate
import regression_green as core
import regression_coordinator as coordinator
import regression_finalizer as finalizer


class DeploymentError(RuntimeError):
    pass


def _atomic_input(path, value):
    """Use finalizer's fsyncing writer so restart sees all-or-nothing bytes."""
    finalizer._atomic_json(path, value)  # Deliberately shared, private durability primitive.


class DeploymentAdapter:
    """Drive a sealed invocation once; recovery never replays a started body."""

    def __init__(self, database, invocation_root):
        self.coordinator = coordinator.Coordinator(database)
        self.invocation_root = Path(invocation_root)
        self.invocation_root.mkdir(parents=True, exist_ok=True)

    def close(self):
        self.coordinator.close()

    def invocation_directory(self, sealed):
        # Both fields came from SQLite, never an artifact or user-provided path.
        return self.invocation_root / sealed["batch_id"] / sealed["invocation_id"]

    def finalise(self, batch_id, evaluator=None):
        """Unconditionally attempt the one sealed finalizer invocation.

        Caller-provided ``evaluator`` is an in-process reviewed function for
        synthetic tests or the trusted aggregate implementation.  It is never
        a producer command and cannot cause producer replay.
        """
        sealed = self.coordinator.begin_finalization(batch_id)
        directory = self.invocation_directory(sealed)
        input_path, status_path = directory / "sealed-input.json", directory / "status.json"
        _atomic_input(input_path, sealed)
        try:
            status = finalizer.run(sealed, directory, evaluator=evaluator, status_path=status_path)
        except finalizer.AlreadyStarted:
            # A prior body may have completed before a coordinator crash.  If
            # not, the durable marker proves it was accepted and died: red,
            # with no second evaluator body.
            try:
                status = finalizer.read_complete_status(status_path, sealed)
            except finalizer.FinalizerError:
                return self.coordinator.fail_finalization(batch_id, "finalizer worker died after accepted start")
        except Exception as exc:
            return self.coordinator.fail_finalization(
                batch_id, "finalizer unstartable or interrupted: %s: %s" % (type(exc).__name__, exc)
            )
        try:
            return self.coordinator.record_verdict(batch_id, status)
        except Exception as exc:
            # A malformed, stale or wrong-tuple status must never remain
            # eligible for publication.  This transition is itself durable.
            try:
                return self.coordinator.fail_finalization(
                    batch_id, "finalizer emitted invalid status: %s: %s" % (type(exc).__name__, exc)
                )
            except Exception:
                raise DeploymentError("coordinator could not store finalizer failure") from exc

    def recover(self, batch_id, evaluator=None):
        """Recover a restart without evaluating an invocation body twice."""
        batch = self.coordinator.batch(batch_id)
        if batch["state"] == coordinator.FINALIZE_READY:
            return self.finalise(batch_id, evaluator=evaluator)
        if batch["state"] != coordinator.FINALIZING:
            return batch
        sealed = self.coordinator.begin_finalization(batch_id)
        directory = self.invocation_directory(sealed)
        status_path = directory / "status.json"
        marker = directory / "started.json"
        if marker.exists():
            try:
                status = finalizer.read_complete_status(status_path, sealed)
            except finalizer.FinalizerError:
                return self.coordinator.fail_finalization(batch_id, "finalizer worker died after accepted start")
            return self.coordinator.record_verdict(batch_id, status)
        # No marker means an exec/permission crash before an accepted body.
        # Starting it now is safe and remains the sole evaluator execution.
        return self.finalise(batch_id, evaluator=evaluator)



HANDOFF_VERSION = 'c06-tuple-coordinator/v1'
TERMINAL = frozenset(('success', 'failure', 'cancelled', 'worker-death',
                      'skipped', 'unstartable'))


def require(condition, message):
    if not condition:
        raise ValueError(message)


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def timestamp(value):
    require(isinstance(value, str), 'invalid coordinator cutoff')
    parsed = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
    require(parsed.tzinfo is not None, 'coordinator cutoff needs a timezone')
    return parsed.astimezone(dt.timezone.utc)


def absolute_file(value, label):
    require(isinstance(value, dict) and set(value) == {'path', 'sha256'},
            'invalid sealed ' + label)
    path, digest = Path(value['path']), value['sha256']
    require(path.is_absolute() and path.is_file() and
            isinstance(digest, str) and re.fullmatch(r'[0-9a-f]{64}', digest),
            'invalid sealed ' + label)
    require(sha256(path) == digest, 'forged or stale ' + label)
    return path


def normalise_broker(root, identifier):
    job = root / 'jobs' / identifier
    state_file, result_file = job / 'state', job / 'result.json'
    if not state_file.exists() or not result_file.exists():
        return 'worker-death'
    state = state_file.read_text(encoding='utf-8').strip()
    require(state in ('done', 'failed'), 'broker job is not terminal: ' + identifier)
    try:
        result = read_json(result_file)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError('invalid broker result: ' + identifier) from exc
    require(isinstance(result, dict), 'invalid broker result: ' + identifier)
    exits = result.get('exit_codes')
    require(isinstance(exits, list) and all(type(code) is int for code in exits),
            'invalid broker exit codes: ' + identifier)
    if state == 'done':
        return ('success' if result.get('ok') is True and result.get('released') is True and
                result.get('release_verified') is True and exits and all(code == 0 for code in exits)
                else 'failure')
    if -1 in exits:
        return 'cancelled'
    if result.get('started') is None and result.get('pod_id') is None and not exits:
        # This is the broker's restart-recovery record for an interrupted worker.
        return 'worker-death'
    return 'failure'


def normalise_producers(handoff, broker_root):
    rows = read_json(handoff['producer_results'])
    require(isinstance(rows, list) and rows, 'missing coordinator producer results')
    observed, result = set(), []
    for row in rows:
        require(isinstance(row, dict) and set(row) == {'id', 'kind', 'state'},
                'invalid coordinator producer result')
        identifier, kind, claimed = row['id'], row['kind'], row['state']
        require(isinstance(identifier, str) and re.fullmatch(r'[A-Za-z0-9_.-]+', identifier) and
                kind in ('broker', 'hosted') and claimed in TERMINAL,
                'invalid coordinator producer identity')
        key = (kind, identifier)
        require(key not in observed, 'duplicate coordinator producer result')
        observed.add(key)
        actual = normalise_broker(broker_root, identifier) if kind == 'broker' else claimed
        require(actual == claimed, 'coordinator result disagrees with terminal source: ' + identifier)
        result.append({'id': identifier, 'state': actual})
    review = aggregate.read(aggregate.REVIEW)
    if review.get('complete') is True:
        expected = {(lane['source']['kind'], lane['source']['id']) for lane in review.get('lanes', [])
                    if isinstance(lane, dict) and isinstance(lane.get('source'), dict)}
        require(expected and observed == expected, 'coordinator producer results skip or add reviewed evidence')
    return result


def load_handoff(path):
    handoff = read_json(path)
    required = {'version', 'batch_id', 'tuple_digest', 'tuple', 'ledger', 'freshness',
                'producer_results', 'broker_root', 'not_before', 'publication'}
    require(isinstance(handoff, dict) and set(handoff) == required and
            handoff['version'] == HANDOFF_VERSION, 'invalid tuple coordinator handoff')
    require(isinstance(handoff['batch_id'], str) and
            re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', handoff['batch_id']),
            'invalid coordinator batch id')
    require(isinstance(handoff['tuple_digest'], str) and re.fullmatch(r'[0-9a-f]{64}', handoff['tuple_digest']),
            'invalid coordinator tuple digest')
    tuple_path = absolute_file(handoff['tuple'], 'tuple')
    ledger_path = absolute_file(handoff['ledger'], 'ledger')
    freshness_path = absolute_file(handoff['freshness'], 'freshness receipt')
    producers_path = absolute_file(handoff['producer_results'], 'producer results')
    broker_root = Path(handoff['broker_root'])
    require(broker_root.is_absolute() and broker_root.is_dir(), 'invalid broker root')
    timestamp(handoff['not_before'])
    publication = handoff['publication']
    require(isinstance(publication, dict) and set(publication) == {'sequence'} and
            type(publication['sequence']) is int and publication['sequence'] > 0,
            'invalid coordinator publication binding')
    tup = read_json(tuple_path)
    core.validate_schema(tup, 'tuple.schema.json')
    identity = core.digest({key: value for key, value in tup.items() if key != 'digest'})
    require(tup.get('digest', identity) == identity == handoff['tuple_digest'],
            'wrong or stale coordinator tuple')
    handoff = dict(handoff)
    handoff.update(tuple=tuple_path, ledger=ledger_path, freshness=freshness_path,
                   producer_results=producers_path, broker_root=broker_root)
    return handoff


def mark_publication_failure(status_path, exc):
    """A check that cannot be confirmed may not remain locally green."""
    try:
        status = read_json(status_path)
        if isinstance(status, dict):
            status['result'] = 'fail'
            message = 'check publication failed: %s: %s' % (type(exc).__name__, exc)
            errors = status.setdefault('errors', [])
            if not isinstance(errors, list):
                status['errors'] = errors = []
            errors.append(message)
            if isinstance(status.get('reasons'), list):
                status['reasons'].append(message)
            finalizer._atomic_json(status_path, status)
    except Exception:
        pass



def main(argv=None):
    """Validate published handoffs and invoke the finalizer even on rejection.

    Legacy handoffs lack the sealed coordinator identity, so the candidate
    finalizer reports them red. Required-check publication remains deferred.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--handoff', required=True)
    parser.add_argument('--status', required=True)
    parser.add_argument('--publish', action='store_true')
    parser.add_argument('--publication-state')
    args = parser.parse_args(argv)
    finalizer_args = ['--status', args.status]
    try:
        handoff = load_handoff(args.handoff)
        completed = normalise_producers(handoff, handoff['broker_root'])
        path = Path(args.status).parent / ('.' + handoff['batch_id'] + '-completed-producers.json')
        finalizer._atomic_json(path, completed)
        finalizer_args = ['--status', args.status, '--tuple', str(handoff['tuple']),
                          '--ledger', str(handoff['ledger']), '--broker-root', str(handoff['broker_root']),
                          '--freshness', str(handoff['freshness']), '--completed-producers', str(path),
                          '--not-before', handoff['not_before'], '--batch-id', handoff['batch_id']]
    except Exception as exc:
        print('ERROR: coordinator handoff rejected: %s: %s' % (type(exc).__name__, exc), file=sys.stderr)
    try:
        result = finalizer.main(finalizer_args)
    except Exception as exc:
        print('ERROR: finalizer crashed: %s: %s' % (type(exc).__name__, exc), file=sys.stderr)
        result = 1
    if args.publish:
        mark_publication_failure(args.status, ValueError('required-check publication deferred'))
        return 1
    return 0 if result == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
