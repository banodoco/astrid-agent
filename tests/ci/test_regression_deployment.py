import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts/ci'))
import regression_aggregate as aggregate
import regression_check_publisher as publisher
import regression_deployment as deployment
import regression_green as core


class DeploymentAdapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.p = Path(self.tmp.name)
        self.tuple = {
            'version': 1, 'name': 'synthetic coordinator tuple',
            'repos': {repo: {'url': 'https://github.com/banodoco/astrid-' + repo, 'sha': sha}
                      for repo, sha in aggregate.CP1.items()},
            'generated': {}, 'toolchain': {}, 'created_at': aggregate.CUTOFF,
        }
        self.tuple_digest = core.digest(self.tuple)
        self.write('tuple.json', self.tuple)
        (self.p / 'ledger.jsonl').write_text('')
        self.write('freshness.json', {'check': 'C-07', 'result': 'pass',
                                      'tuple_digest': self.tuple_digest,
                                      'repo_shas': aggregate.CP1})
        self.broker = self.p / 'broker'
        self.set_broker('job-1', 'done', {'ok': True, 'released': True,
                                           'release_verified': True, 'exit_codes': [0]})

    def write(self, name, value):
        path = self.p / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding='utf-8')
        return path

    def sealed(self, name):
        path = self.p / name
        return {'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}

    def set_broker(self, identifier, state, result=None):
        job = self.broker / 'jobs' / identifier
        job.mkdir(parents=True, exist_ok=True)
        (job / 'state').write_text(state)
        if result is not None:
            (job / 'result.json').write_text(json.dumps(result))

    def handoff(self, rows=None, digest=None):
        rows = rows or [{'id': 'job-1', 'kind': 'broker', 'state': 'success'}]
        self.write('producer-results.json', rows)
        return self.write('handoff.json', {
            'version': deployment.HANDOFF_VERSION, 'batch_id': 'cp1-1',
            'tuple_digest': self.tuple_digest if digest is None else digest,
            'tuple': self.sealed('tuple.json'), 'ledger': self.sealed('ledger.jsonl'),
            'freshness': self.sealed('freshness.json'),
            'producer_results': self.sealed('producer-results.json'),
            'broker_root': str(self.broker), 'not_before': aggregate.CUTOFF,
            'publication': {'sequence': 1},
        })

    def test_handoff_passes_existing_terminal_result_to_finalizer_without_execution(self):
        handoff = self.handoff()
        status = self.p / 'status.json'
        with mock.patch.object(deployment.finalizer, 'main', return_value=1) as invoked:
            self.assertEqual(deployment.main(['--handoff', str(handoff), '--status', str(status)]), 1)
        args = invoked.call_args.args[0]
        completed = Path(args[args.index('--completed-producers') + 1])
        self.assertEqual(json.loads(completed.read_text()), [{'id': 'job-1', 'state': 'success'}])
        self.assertNotIn('--producer-plan', args)
        self.assertEqual(args[args.index('--batch-id') + 1], 'cp1-1')

    def test_invalid_handoff_still_calls_finalizer_once(self):
        status = self.p / 'status.json'
        with mock.patch.object(deployment.finalizer, 'main', return_value=1) as invoked:
            self.assertEqual(deployment.main(['--handoff', str(self.p / 'missing.json'), '--status', str(status)]), 1)
        invoked.assert_called_once_with(['--status', str(status)])

    def test_worker_death_cancellation_duplicate_and_skipped_results_fail_closed(self):
        # Missing result after a terminal broker state is a worker death.
        self.set_broker('dead', 'failed', None)
        handoff = self.handoff([{'id': 'dead', 'kind': 'broker', 'state': 'worker-death'}])
        loaded = deployment.load_handoff(handoff)
        self.assertEqual(deployment.normalise_producers(loaded, self.broker),
                         [{'id': 'dead', 'state': 'worker-death'}])

        self.set_broker('cancelled', 'failed', {'ok': False, 'exit_codes': [-1]})
        handoff = self.handoff([{'id': 'cancelled', 'kind': 'broker', 'state': 'cancelled'}])
        loaded = deployment.load_handoff(handoff)
        self.assertEqual(deployment.normalise_producers(loaded, self.broker)[0]['state'], 'cancelled')

        handoff = self.handoff([{'id': 'h-1', 'kind': 'hosted', 'state': 'skipped'},
                                {'id': 'h-1', 'kind': 'hosted', 'state': 'skipped'}])
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            deployment.normalise_producers(deployment.load_handoff(handoff), self.broker)
        handoff = self.handoff([{'id': 'h-1', 'kind': 'hosted', 'state': 'skipped'}])
        self.assertEqual(deployment.normalise_producers(deployment.load_handoff(handoff), self.broker),
                         [{'id': 'h-1', 'state': 'skipped'}])

    def test_tampered_or_wrong_tuple_handoff_is_rejected_before_finalization(self):
        handoff = self.handoff()
        self.write('tuple.json', self.tuple | {'name': 'tampered'})
        with self.assertRaisesRegex(ValueError, 'forged or stale tuple'):
            deployment.load_handoff(handoff)
        handoff = self.handoff(digest='0' * 64)
        with self.assertRaisesRegex(ValueError, 'wrong or stale'):
            deployment.load_handoff(handoff)


class RequiredCheckPublisherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.p = Path(self.tmp.name)
        self.tuple = {'repos': {'agent': {'sha': 'a' * 40}}}
        self.digest = publisher.tuple_identity(self.tuple)
        (self.p / 'tuple.json').write_text(json.dumps(self.tuple))
        self.status = self.p / 'status.json'
        self.status.write_text(json.dumps({'check': publisher.CHECK, 'result': 'fail', 'batch_id': 'batch-2',
                                           'tuple_digest': self.digest, 'target_sha': 'a' * 40,
                                           'errors': ['missing evidence']}))

    def send(self, method, path, payload, token=None):
        self.assertEqual((method, path, token), ('POST', '/repos/banodoco/astrid-agent/check-runs', 'token'))
        self.assertEqual(payload['name'], publisher.CHECK)
        self.assertEqual(payload['conclusion'], 'failure')
        return {'id': 71, 'name': publisher.CHECK, 'head_sha': 'a' * 40,
                'status': 'completed', 'conclusion': 'failure'}

    def test_publisher_binds_check_and_rejects_stale_or_wrong_tuple_result(self):
        state = self.p / 'publication.json'
        record = publisher.publish(self.status, self.p / 'tuple.json', 'batch-2', 2, state,
                                   token='token', send=self.send)
        self.assertEqual(record['check_run_id'], 71)
        with self.assertRaisesRegex(ValueError, 'stale'):
            publisher.publish(self.status, self.p / 'tuple.json', 'batch-2', 1, state,
                              token='token', send=self.send)
        bad = json.loads(self.status.read_text())
        bad['tuple_digest'] = '0' * 64
        self.status.write_text(json.dumps(bad))
        with self.assertRaisesRegex(ValueError, 'wrong-tuple'):
            publisher.publish(self.status, self.p / 'tuple.json', 'batch-2', 3, state,
                              token='token', send=self.send)

    def test_unconfirmed_publication_marks_local_result_red(self):
        # The deployment caller owns this last fail-closed transition.
        deployment.mark_publication_failure(self.status, RuntimeError('network lost'))
        status = json.loads(self.status.read_text())
        self.assertEqual(status['result'], 'fail')
        self.assertTrue(any('network lost' in item for item in status['errors']))


if __name__ == '__main__':
    unittest.main()
