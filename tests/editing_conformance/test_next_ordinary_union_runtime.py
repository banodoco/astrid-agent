"""New occurrence roots and keyed text rows share one authenticated inverse path."""
from __future__ import annotations

import pytest

from astrid.sdk.timeline import commands
from astrid.sdk.timeline.commit import bundle_from_document
from astrid.sdk.timeline.native_occurrence_create import capture_occurrence_create
from astrid.sdk.timeline.native_text_binding import capture_text_binding_set
from tests.editing_conformance import test_commit_runtime_parent_media as parent
from tests.editing_conformance.test_next_command_cohort_runtime import reopen
from tests.editing_conformance.test_pristine_initialize import batch
from tests.editing_conformance.test_recipe_text_binding_native import descriptor
from tests.helpers.runtime import use_runtime

pytestmark = pytest.mark.integration


@pytest.mark.parametrize('data', [b'', b'new occurrence text\r\n'])
def test_born_occurrence_text_order_clear_grouped_inverse_and_redo(tmp_path, monkeypatch, data):
    use_runtime()
    realm = parent.media_realm(tmp_path, monkeypatch, 'video', historical=False, pristine=True)
    case = next(realm)
    try:
        original = parent.current(case)
        initialized = parent.commit(case, parent.body(case, batch(bundle_from_document(original))), 'union-init')
        pin = descriptor(data)
        case.service.ingest(case.project, data, media_type=pin['media_type'], idempotency_key='union-text')
        birth = capture_occurrence_create(bundle_from_document(parent.current(case)), 'text-occ', 'text-shot',
                                          name='Text chapter', track='V1', on=0, duration=8)

        def text(bundle, kind, *, clear=False):
            return capture_text_binding_set(
                bundle, 'occurrence:text-occ', {'kind': kind, 'slot': None},
                text=None if clear else pin, clear=clear,
                verified_text=[{**pin, 'utf8': True}])

        birth_request = parent.body(case, [birth])
        birth_accepted = parent.commit(case, birth_request, 'union-birth')
        text_bundle = bundle_from_document(parent.current(case))
        transcript = text(text_bundle, 'transcript')
        text_bundle = commands.execute(
            text_bundle, [transcript], verified_media=[{**pin, 'utf8': True}]).bundle
        voiceover = text(text_bundle, 'voiceover_script')
        request = parent.body(case, [transcript, voiceover])
        accepted = parent.commit(case, request, 'union-birth-text')
        document = parent.current(case)
        shot = next(row for row in document['shot_revisions'] if row['shot_id'] == 'text-shot')
        assert [row['kind'] for row in shot['payload']['text_bindings']] == ['transcript', 'voiceover_script']
        assert case.service.object(pin['object_id'])[1] == data
        cleared = parent.commit(
            case, parent.body(case, [text(bundle_from_document(document), 'transcript', clear=True)]),
            'union-clear')
        restored = parent.undo(case, cleared, 'union-restore-text-order')
        assert parent.current(case) == document
        # Reverse the clear/restore edges too, retaining their authenticated order.
        inverse = parent.body(case, [parent.call('timeline.inverse', {'publications': [
            item['receipt']['receipt_id'] for item in
            (initialized, birth_accepted, accepted, cleared, restored)]}, None)])
        undone = parent.commit(case, inverse, 'union-group-undo')
        assert parent.current(case) == original
        redone = parent.undo(case, undone, 'union-group-redo')
        assert parent.current(case) == redone['data']['canonical_document'] == document
        reopen(case)
        assert parent.current(case) == document
        with monkeypatch.context() as retry:
            retry.setattr(commands, 'execute', lambda *a, **kw: pytest.fail('retry executed semantics'))
            assert parent.commit(case, birth_request, 'union-birth') == birth_accepted
            assert parent.commit(case, request, 'union-birth-text') == accepted
            assert parent.commit(case, inverse, 'union-group-undo') == undone
        assert case.service.store.integrity_report()['ok']
    finally:
        realm.close()
