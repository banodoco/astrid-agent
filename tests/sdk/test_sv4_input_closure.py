"""Step inputs retain their own immutable boundary through later commands."""
import copy

from astrid.sdk.timeline._doc_patch import Closure
from astrid.sdk.timeline.commands import execute
from astrid.sdk.timeline.commit import bundle_from_document
from tests.sdk.test_timeline_commit_provider_document import document_fixture


def test_input_closure_remains_owned_after_later_step(monkeypatch):
    bundle = bundle_from_document(document_fixture(shared=False))
    initial = bundle['parent']['name']
    changes = [{'op': 'set', 'ref': 'parent', 'path': ['name'],
                'before': {'state': 'present', 'value': before}, 'value': after}
               for before, after in ((initial, 'First'), ('First', 'Second'))]
    commands = [{'schema_version': 1, 'op': 'doc.patch', 'version': '1', 'select': None,
                 'args': {'changes': [change]}} for change in changes]
    steps = [{'id': str(i), 'label': str(i), 'intent': '', 'tags': [],
              'command_ordinals': [i], 'patch': [change]} for i, change in enumerate(changes)]
    head = {'revision_id': bundle['base_parent']['revision_id'], 'publication_id': 'bootstrap'}
    request = {'base_revision_id': head['revision_id'], 'base_publication_id': head['publication_id'],
               'commands': commands, 'steps': steps}
    saved = []
    init = Closure.__init__

    def record(self, *args, **kwargs):
        init(self, *args, **kwargs)
        if not saved:
            saved.append((self, copy.deepcopy(self.bundle), copy.deepcopy(self.document)))

    monkeypatch.setattr(Closure, '__init__', record)
    plan = execute(bundle, commands, commit_request=request, evaluated_head=head)
    closure, input_bundle, input_document = saved[0]
    assert closure.bundle == input_bundle
    assert closure.document == input_document
    assert input_bundle['parent']['name'] == initial
    assert plan.bundle['parent']['name'] == 'Second'
    assert plan._sequential_execution[0]['input'] == input_document
