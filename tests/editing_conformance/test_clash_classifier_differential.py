"""The retry probe retains the reviewed commit's merge and read contract."""
from __future__ import annotations

import copy
from types import SimpleNamespace

from astrid.sdk.contracts import DomainResult
from astrid.sdk.timeline.clash import _probe


def test_clash_probe_preserves_merge_reads_steps_and_all_commands():
    class Timelines:
        def __init__(self):
            self.body = None

        def commit(self, project, timeline, body, *, idempotency_key):
            self.body = copy.deepcopy(body)
            return DomainResult.success({'status': 'preview'})

    timelines = Timelines()
    session = SimpleNamespace(project_id='project', timeline_id='film',
                              client=SimpleNamespace(timelines=timelines))
    commands = [{'op': 'clip.move'}, {'op': 'clip.trim'}]
    reads = [{'ref': 'parent', 'path': ['clips']}]
    steps = [{'id': 'reviewed', 'reads': copy.deepcopy(reads)}]
    request = {'base_revision_id': 'r1', 'base_publication_id': 'p1',
               'commands': commands, 'merge': 'atomic', 'reads': reads,
               'steps': steps, 'label': 'Reviewed batch'}

    result = _probe(session, request, request['merge'], request['reads'])

    assert result == {'status': 'preview'}
    assert timelines.body['commands'] == commands
    assert timelines.body['merge'] == 'atomic'
    assert timelines.body['reads'] == reads
    assert timelines.body['steps'] == steps
    assert timelines.body['dry_run'] is True
