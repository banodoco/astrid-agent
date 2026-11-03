"""Native text command capture, keyed guards and ordered structural inverses."""
from __future__ import annotations

import copy
import hashlib
import json

import pytest

from astrid.sdk.timeline._command_state import ABSENT, CommandError, State, overlaps, present
from astrid.sdk.timeline.commands import execute, validate_command
from astrid.sdk.timeline.native_text_binding import TEXT_MEDIA_TYPE, capture_text_binding_set
from astrid.sdk.timeline.rebase import replay
from tests.editing_conformance.test_recipe_m1_commands import bundle, inverse, later
from tests.timeline.test_authoring_bundle import _runtime_document

OWNER = 'occurrence:occ-1'
VOICE = {'kind': 'voiceover_script', 'slot': None}
PROMPT = {'kind': 'prompt', 'slot': 'hero'}
TRANSCRIPT = {'kind': 'transcript', 'slot': None}
REF = OWNER + '/text-binding:voiceover_script'


def descriptor(data):
    identity = 'sha256:' + hashlib.sha256(data).hexdigest()
    return {'object_id': identity, 'digest': identity, 'size': len(data), 'media_type': TEXT_MEDIA_TYPE}


def payload(base):
    return base['shots'][State(base).occurrence(OWNER)['shot_id']]['payload']


def _attach_authenticated_source(base):
    """Keep the conformance bundle paired with its immutable source closure."""
    parent = {
        'revision_id': base['base_parent']['revision_id'],
        'project_id': base['project_id'],
        'timeline_id': base['timeline_id'],
        'content_digest': base['base_parent']['content_digest'],
        'payload': copy.deepcopy(base['base_parent_payload']),
    }
    parent['payload']['occurrences'] = []
    for placement in base['placements']:
        source = base['source_mapping']['shots'][placement['shot_id']]
        occurrence = copy.deepcopy(placement)
        occurrence['shot_revision_id'] = source['revision_id']
        parent['payload']['occurrences'].append(occurrence)
    shots, internals = [], []
    for shot_id, shot in base['shots'].items():
        source = base['source_mapping']['shots'][shot_id]
        shots.append({
            'shot_id': shot_id,
            'revision_id': source['revision_id'],
            'internal_timeline_revision_id': source['internal_timeline_revision_id'],
            'content_digest': source['content_digest'],
            'payload': copy.deepcopy(shot['base_payload']),
        })
        internals.append({
            'timeline_id': source.get('internal_timeline_id', base['timeline_id']),
            'revision_id': source['internal_timeline_revision_id'],
            'content_digest': source['internal_timeline_content_digest'],
            'payload': copy.deepcopy(shot['base_internal_timeline']),
        })
    parent['content_digest'] = 'sha256:' + hashlib.sha256(
        json.dumps(parent['payload'], sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()
    ).hexdigest()
    base['source_mapping']['_runtime_document'] = _runtime_document(parent, shots, internals)
    return base


@pytest.fixture
def original():
    base = bundle(shared=False)
    payload(base)['text_bindings'] = []
    return _attach_authenticated_source(base)


def apply(base, binding=VOICE, data=b'voice', *, clear=False):
    pin = descriptor(data)
    verified = [{**pin, 'utf8': True}]
    call = capture_text_binding_set(base, OWNER, binding, text=None if clear else pin,
                                    clear=clear, verified_text=verified)
    validate_command(call)
    return execute(base, [call], verified_media=verified)


def test_exact_capture_inverse_and_same_value_write(original):
    base = copy.deepcopy(original)
    plan = apply(original)
    assert original == base
    assert plan.records[0]['dependencies'] == [{'key': 'text:' + descriptor(b'voice')['digest'],
                                               'kind': 'media', 'pin': descriptor(b'voice'), 'policy': 'exact'}]
    assert plan.commands[0]['expect'][REF + '::'] == ABSENT
    assert plan.commands[0]['created'] == []
    assert State(plan.bundle).entity(REF) == {**VOICE, 'media_id': descriptor(b'voice')['object_id'],
        'content_hash': descriptor(b'voice')['digest'], 'byte_size': 5, 'mime_type': TEXT_MEDIA_TYPE}
    undone = inverse(plan.bundle, plan.records)
    assert undone.bundle == base
    # Redo insertion needs an accepted boundary (covered by real Runtime tests).
    again = apply(plan.bundle)
    assert again.bundle == plan.bundle
    assert again.records[0]['writes'][0]['before'] == again.records[0]['writes'][0]['after']


def test_empty_clear_and_original_noncanonical_order(original):
    first = apply(original, TRANSCRIPT)
    second = apply(first.bundle, VOICE, b'')
    third = apply(second.bundle, PROMPT)
    before = copy.deepcopy(third.bundle)
    assert [row['kind'] for row in payload(before)['text_bindings']] == ['transcript', 'voiceover_script', 'prompt']
    cleared = apply(before, clear=True)
    assert State(cleared.bundle).entity(REF, optional=True) is None
    assert cleared.records[0]['writes'][0]['after'] == ABSENT
    restored = inverse(cleared.bundle, cleared.records)
    assert restored.bundle == before
    assert State(restored.bundle).entity(REF)['byte_size'] == 0
    assert before == third.bundle


def test_absent_collection_is_restored_exactly(original):
    payload(original).pop('text_bindings')
    plan = apply(original)
    assert inverse(plan.bundle, plan.records).bundle == original


def test_rebind_preserves_opaque_and_other_keys_and_disjoint_rebase(original):
    one = apply(original)
    two = apply(one.bundle, PROMPT)
    payload(two.bundle)['opaque'] = {'null': None, 'arrays': [False, {}]}
    State(two.bundle).entity(REF)['opaque'] = [None, False]
    voice = apply(two.bundle, data=b'new')
    prompt = apply(two.bundle, PROMPT, b'other')
    result = replay(voice, prompt.bundle, verified_media=[{**descriptor(b'new'), 'utf8': True}])
    assert payload(result.bundle)['opaque'] == payload(two.bundle)['opaque']
    assert State(result.bundle).entity(REF)['opaque'] == [None, False]
    assert State(result.bundle).entity(OWNER + '/text-binding:prompt:hero') == State(prompt.bundle).entity(OWNER + '/text-binding:prompt:hero')
    assert inverse(voice.bundle, voice.records).bundle == two.bundle
    with pytest.raises(CommandError) as error:
        replay(voice, apply(two.bundle, data=b'different').bundle,
               verified_media=[{**descriptor(b'new'), 'utf8': True}])
    assert error.value.kind == 'dependency_changed'


def test_membership_and_order_are_guarded_for_structural_edits(original):
    one = apply(apply(original).bundle, PROMPT)
    cleared = apply(one.bundle, clear=True)
    payload(cleared.bundle)['text_bindings'].append({**TRANSCRIPT,
        'media_id': descriptor(b'voice')['object_id'], 'content_hash': descriptor(b'voice')['digest'],
        'byte_size': 5, 'mime_type': TEXT_MEDIA_TYPE})
    with pytest.raises(CommandError) as error:
        inverse(cleared.bundle, cleared.records)
    assert error.value.kind == 'selection_changed'


def test_aba_and_payload_alias_intervening_writes_refuse(original):
    plan = apply(original)
    for write in (later(REF, [], present(State(plan.bundle).entity(REF))),
                  later(OWNER + '/shot', ['payload', 'text_bindings'], present([]))):
        with pytest.raises(CommandError) as error:
            inverse(plan.bundle, plan.records, [write])
        assert error.value.kind == 'intervening_write'
    assert overlaps((REF, ()), (OWNER + '/shot', ('payload', 'text_bindings'))) is False
    # Alias overlap is resolved against the verified shot owner in inverse.py.


@pytest.mark.parametrize('damage', ['inline', 'shared', 'no-input', 'digest', 'clear-absent',
                                  'both', 'neither', 'extra', 'mime', 'slot', 'parent', 'duplicate'])
def test_closed_refusals(original, damage):
    call = {'schema_version': 1, 'op': 'text-binding.set', 'version': '1', 'select': OWNER,
            'args': {'binding': copy.deepcopy(VOICE), 'text': descriptor(b'voice')}}
    verified = [{**descriptor(b'voice'), 'utf8': True}]
    if damage == 'inline':
        payload(original)['text_bindings'] = [{'slot': 'voiceover', 'text': 'hello'}]
    elif damage == 'shared':
        original['placements'].append({**original['placements'][0], 'occurrence_id': 'sibling'})
    elif damage == 'no-input':
        verified = []
    elif damage == 'digest':
        call['args']['text']['digest'] = descriptor(b'other')['digest']
    elif damage == 'clear-absent':
        call['args'] = {'binding': VOICE, 'clear': True}
    elif damage == 'both':
        call['args']['clear'] = True
    elif damage == 'neither':
        del call['args']['text']
    elif damage == 'extra':
        call['args']['text']['text'] = 'voice'
    elif damage == 'mime':
        call['args']['text']['media_type'] = 'text/plain;charset=utf-8'
    elif damage == 'slot':
        call['args']['binding']['slot'] = 'hero'
    elif damage == 'parent':
        call['select'] = 'parent'
    else:
        row = {**VOICE, 'media_id': descriptor(b'voice')['object_id']}
        payload(original)['text_bindings'] = [row, row]
    before = copy.deepcopy(original)
    with pytest.raises(CommandError):
        execute(original, [call], verified_media=verified)
    assert original == before


def test_shared_source_is_refused_even_after_authoring_aliases_materialize():
    original = bundle(shared=True)
    for shot in original['shots'].values():
        shot['payload']['text_bindings'] = []
    assert original['placements'][0]['shot_id'] != original['placements'][1]['shot_id']
    with pytest.raises(CommandError) as error:
        apply(original)
    assert error.value.kind == 'unsupported_ownership'


def test_binding_order_change_refuses_captured_clear(original):
    base = apply(apply(original).bundle, PROMPT).bundle
    cleared = apply(base, clear=True)
    head = copy.deepcopy(base)
    payload(head)['text_bindings'].reverse()
    with pytest.raises(CommandError) as error:
        replay(cleared, head)
    assert error.value.kind == 'selection_changed'


def test_disjoint_births_rebase_with_recorded_append_order(original):
    voice, prompt = apply(original), apply(original, PROMPT, b'prompt')
    rebased = replay(voice, prompt.bundle, verified_media=[{**descriptor(b'voice'), 'utf8': True}])
    assert [row['kind'] for row in payload(rebased.bundle)['text_bindings']] == ['prompt', 'voiceover_script']
    selected = next(row for row in rebased.records[0]['selections'] if row['key'].endswith('.append'))
    assert selected['order'] == [OWNER + '/text-binding:prompt:hero']
    assert rebased.commands == voice.commands
    assert inverse(rebased.bundle, rebased.records).bundle == prompt.bundle


def test_multiple_births_in_absent_collection_have_exact_batch_inverse(original):
    payload(original).pop('text_bindings')
    voice = apply(original)
    prompt = apply(voice.bundle, PROMPT, b'prompt')
    assert inverse(prompt.bundle, [*voice.records, *prompt.records]).bundle == original


def test_birth_inverse_preserves_later_disjoint_rebind(original):
    prompt = apply(original, PROMPT, b'old prompt')
    voice = apply(prompt.bundle)
    changed = apply(voice.bundle, PROMPT, b'new prompt')
    restored = inverse(changed.bundle, voice.records, changed.records)
    expected = apply(original, PROMPT, b'new prompt')
    assert restored.bundle == expected.bundle


@pytest.mark.parametrize('clear', [False, True])
def test_trusted_inverse_restores_exact_legacy_descriptor_and_position(original, clear):
    base = apply(apply(apply(original, TRANSCRIPT).bundle).bundle, PROMPT).bundle
    legacy = State(base).entity(REF)
    legacy.update(mime_type='text/plain', text='legacy cached text', opaque=[None, False])
    before = copy.deepcopy(base)
    plan = apply(base, data=b'replacement', clear=clear)
    assert inverse(plan.bundle, plan.records).bundle == before
    assert payload(before)['text_bindings'][1] == legacy
    # New selections still use the closed canonical MIME schema, even when the
    # provider's captured inputs claim otherwise. Only accepted inverse is trusted.
    pin = {**descriptor(b'new legacy'), 'media_type': 'text/plain'}
    with pytest.raises(CommandError):
        capture_text_binding_set(base, OWNER, VOICE, text=pin, verified_text=[{**pin, 'utf8': True}])
