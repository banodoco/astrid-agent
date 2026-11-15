"""Shared fakes for the retained-host H3 SDK contracts (test_h3_endpoint_r4, test_runpod_h3_host_r5).

Plan 32 T6b-2 removed the legacy RunPod front-door tests that lived here, with the endpoint-checkout route
they exercised; the prepared-owner fake and the paid controls stay because the H3 contracts import them.
"""
from copy import deepcopy
import json


from astrid.core.generation import preflight
from astrid.packs.runpod.prepared_task import PreparedWorkerSelection


PAID = dict(consent=('paid',), max_usd=2.0, deadline='2099-01-01T00:00:00Z')


class FakeOwner:
    """R03 preparation and F07 retained owner; R04 release is admission only."""
    def __init__(self, root, validation_target):
        self.events = []
        self.validation = validation_target
        self.retained = {}
        claim = root / 'claim.json'
        claim.write_text(json.dumps({'state': 'claimed', 'pod_id': 'pod-a',
            'provider_account_ref': 'account-a', 'network_volume_id': 'volume-a'}))
        self.selection = PreparedWorkerSelection(claim, root / 'profile.json', root / 'staging.json',
            root / 'journal', {'kind': 'runpod', 'pod_id': 'pod-a',
                'provider_account_ref': 'account-a', 'storage': {'network_volume_id': 'volume-a'}}, {}, {})
        self.activated = set()
        self.retired = set()

    def validation_target(self):
        self.events.append('validate-target')
        return self.validation

    def monitor(self, task):
        key = (task['task_id'], task['run_id'])
        if task['state'] in {'succeeded', 'failed', 'cancelled'}:
            if key not in self.retired:
                self.retired.add(key)
                self.events.append(('retire', key))
        elif key not in self.activated:
            self.activated.add(key)
            self.events.append(('activate', key, deepcopy(task.get('execution_binding'))))

    def prepare(self, **kwargs):
        self.events.append(('prepare', kwargs))
        assert kwargs['consent'] == ('paid',)
        assert kwargs['max_usd'] == 2.0 and kwargs['deadline'] == PAID['deadline']
        return preflight.PreparedRunPodGeneration(self.selection, '/fake/selection.json', self.monitor)

    def validate_h3(self, inputs):
        self.events.append(('validate-h3', deepcopy(inputs)))
        if inputs['request'] == 'invalid.json':
            raise ValueError('invalid H3 request')
        return preflight.readiness_envelope(entry={'id': 'h3'}, target={'placement': 'runpod'})

    def prepare_h3(self, **kwargs):
        self.events.append(('prepare-h3', kwargs))
        return preflight.PreparedRunPodGeneration(self.selection, '/fake/selection.json', self.monitor)

    def retain(self, task, monitor):
        self.events.append(('retain', task['task_id']))
        self.retained[task['task_id']] = monitor

    def describe(self):
        self.events.append('describe')
        return {'engine': 'comfyui', 'placement': 'runpod', 'pod_id': 'pod-a', 'state': 'cleanup_pending'}

    def release(self, *, wait):
        self.events.append(('release', wait))
        # R04 fake: an admitted release never asserts provider absence.
        return {'task_id': 'release-task-a', 'idempotency_key': 'runpod-release:operation-a',
                'state': 'cleanup_pending'}
