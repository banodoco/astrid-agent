"""Spike: the Agent-owned shot text-binding command (design/m2-text-binding-contract.md).

Test-local seam, product code unchanged. The recipe harness has no text-binding entity
yet, so this module teaches it, and only here, the following. Each names the product gap
it stands in for (design/m2-text-binding-contract.md section 4):

* G-SCHEMA: the scoped-ref patterns admit `occurrence:<id>/text-binding:<key>`;
* G-OP: `validate_command` admits the op, and `operations()` registers it;
* G-STATE: `occurrence:<id>/text-binding:<key>` is a keyed row in the owner shot's
  `payload.text_bindings` (`State.entity` / `State.set`);
* G-INV1: a removed keyed row is restored by assignment, not by `create`, which needs
  a collection (`Recording.create`, reached from `inverse.apply_inverse`).

The Runtime port presence check runs in the integration lane; acceptance is
covered by test_commit_runtime_text_binding.py.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from astrid.sdk.timeline import commands
from astrid.sdk.timeline._command_state import ABSENT, CommandError, State, present
from astrid.sdk.timeline._recording import Recording
from astrid.sdk.timeline.commands import Operation, execute
from astrid.sdk.timeline.rebase import replay
from tests.editing_conformance.test_recipe_m1_commands import bundle, inverse, later

OP = 'text-binding.set'
MEDIA_TYPE = 'text/plain; charset=utf-8'
MAX_BYTES = 1048576
DIGEST = {'type': 'string', 'pattern': '^sha256:[0-9a-f]{64}$'}
PARAMETERS = {
    'type': 'object',
    'required': ['binding'],
    'additionalProperties': False,
    'properties': {
        'binding': {
            'type': 'object', 'required': ['kind', 'slot'], 'additionalProperties': False,
            'properties': {
                'kind': {'enum': ['prompt', 'voiceover_script', 'transcript']},
                'slot': {'anyOf': [{'type': 'null'}, {'type': 'string', 'pattern': '^[a-z][a-z0-9-]{0,63}$'}]},
            },
            'allOf': [{'if': {'properties': {'kind': {'const': 'prompt'}}, 'required': ['kind']},
                       'else': {'properties': {'slot': {'type': 'null'}}}}],
        },
        'text': {
            'type': 'object', 'required': ['object_id', 'digest', 'size', 'media_type'],
            'additionalProperties': False,
            'properties': {
                'object_id': DIGEST,
                'digest': DIGEST,
                'size': {'type': 'integer', 'minimum': 0, 'maximum': MAX_BYTES},
                'media_type': {'const': MEDIA_TYPE},
            },
        },
        'clear': {'const': True},
    },
    'oneOf': [{'required': ['text']}, {'required': ['clear']}],
}
OWNER = 'occurrence:occ-1'
VOICE = {'kind': 'voiceover_script', 'slot': None}
PROMPT = {'kind': 'prompt', 'slot': 'hero'}
REF = OWNER + '/text-binding:voiceover_script'


def _widen(schema):
    """Spike-only gap G-SCHEMA: admit `occurrence:<id>/text-binding:<key>` in the scoped-ref patterns."""
    widened = json.dumps(schema).replace('occurrence:[^/]+(?:/shot)?', 'occurrence:[^/]+(?:/shot|/text-binding:[^/]+)?')
    return json.loads(widened)


EXTENDED_VALIDATOR = Draft202012Validator(_widen(commands._SCHEMA))


def descriptor(value: bytes) -> dict:
    """Stub of the existing immutable object: object_id == digest == sha256 of the UTF-8 bytes."""
    digest = 'sha256:' + hashlib.sha256(value).hexdigest()
    return {'object_id': digest, 'digest': digest, 'size': len(value), 'media_type': MEDIA_TYPE}


def _key(row):
    return row['kind'] if row['slot'] is None else row['kind'] + ':' + row['slot']


def _rows(state, owner):
    rows = state.bundle['shots'][state.occurrence(owner)['shot_id']]['payload']['text_bindings']
    if any('kind' not in row for row in rows):
        # Main's authoring fixture still carries inline `text` rows; they are not authority.
        raise CommandError('legacy inline text binding is not a registered binding', kind='unsupported_shape',
                           ref=owner, path=['text_bindings'])
    return rows


def _row(binding, text):
    return {'kind': binding['kind'], 'slot': binding['slot'], 'media_id': text['object_id'],
            'content_hash': text['digest'], 'byte_size': text['size'], 'mime_type': text['media_type']}


def text_set(ctx, command):
    owner, args = command['select'], command['args']
    ctx.read(owner + '/shot', ['shot_id'])
    ref = owner + '/text-binding:' + _key(args['binding'])
    if 'clear' in args:
        if ctx.read(ref, []) is None:
            # Clear removes presence. A no-op clear is refused: apply_inverse cannot restore an
            # absent->absent path-[] write (inverse.py:146, reported as G-INV2).
            raise CommandError('clear needs a present binding', kind='absent_binding', ref=ref)
        ctx.clear(ref, [])
        return
    ctx.read(ref, [])
    text = args['text']
    if text['object_id'] != text['digest']:
        raise CommandError('text object id must equal its content digest', ref=ref)
    ctx.dependency('text:' + text['digest'], 'media', text)
    ctx.write(ref, [], _row(args['binding'], text))


@pytest.fixture
def text_seam(monkeypatch):
    # Isolate the historical singleton seam from the native ordered collection.
    from astrid.sdk.timeline import _collections
    from astrid.sdk.timeline import inverse as native_inverse

    original_member = _collections.member

    def member(ref):
        return None if '/text-binding:' in ref else original_member(ref)

    monkeypatch.setattr(_collections, 'member', member)
    monkeypatch.setattr(native_inverse, 'member', member)
    original_validate = commands.validate_command
    original_operations = commands.operations
    original_entity, original_set = State.entity, State.set
    original_create = Recording.create

    def validate(command):
        if command.get('op') != OP:
            return original_validate(command)
        errors = sorted(EXTENDED_VALIDATOR.iter_errors(command), key=lambda e: str(e.path))
        if errors:
            raise CommandError(errors[0].message)
        if command['version'] != '1':
            raise CommandError('unsupported operation version', kind='unsupported_version')
        errors = list(Draft202012Validator(PARAMETERS).iter_errors(command['args']))
        if errors:
            raise CommandError(errors[0].message)
        select = command['select']
        if not (isinstance(select, str) and select.startswith('occurrence:') and '/' not in select):
            raise CommandError('text-binding.set requires an occurrence owner')

    def operations():
        return {**original_operations(), OP: Operation(PARAMETERS, text_set, {'ownership': 'occurrence'}, True)}

    def entity(self, ref, *, optional=False):
        if '/text-binding:' not in ref:
            return original_entity(self, ref, optional=optional)
        owner, key = ref.split('/text-binding:', 1)
        return next((row for row in _rows(self, owner) if _key(row) == key), None)

    def set_state(self, ref, path, state):
        if '/text-binding:' not in ref:
            return original_set(self, ref, path, state)
        if path:
            raise CommandError('text binding rows are written whole', ref=ref, path=path)
        owner, key = ref.split('/text-binding:', 1)
        rows = _rows(self, owner)
        index = next((i for i, row in enumerate(rows) if _key(row) == key), None)
        if state['state'] == 'absent':
            if index is not None:
                del rows[index]
        elif index is None:
            rows.append(copy.deepcopy(state['value']))
        else:
            rows[index] = copy.deepcopy(state['value'])

    def create(self, owner, kind, row, **options):
        if kind != 'text-binding':
            return original_create(self, owner, kind, row, **options)
        ref = owner + '/text-binding:' + _key(row)
        self.assign(ref, [], present(row))
        return ref

    monkeypatch.setattr(commands, 'validate_command', validate)
    monkeypatch.setattr(commands, 'operations', operations)
    monkeypatch.setattr(State, 'entity', entity)
    monkeypatch.setattr(State, 'set', set_state)
    monkeypatch.setattr(Recording, 'create', create)


def owner_payload(base):
    return base['shots'][State(base).occurrence(OWNER)['shot_id']]['payload']


@pytest.fixture
def original(text_seam):
    # Spike-only: the owner's legacy inline row is cleared. Migrating it is a recorded
    # command that this spike does not cover (see test_legacy_inline_row_is_refused).
    base = bundle()
    owner_payload(base)['text_bindings'] = []
    return base


def command(args, owner=OWNER):
    return {'schema_version': 1, 'op': OP, 'version': '1', 'select': owner, 'args': args,
            'intent': {'ownership': 'occurrence'}}


def capture(bundle_, args):
    return execute(bundle_, [command(args)]).commands[0]


def row_of(bundle_, key='voiceover_script'):
    return State(bundle_).entity(OWNER + '/text-binding:' + key, optional=True)


@pytest.mark.parametrize('args', [
    {'binding': VOICE, 'text': descriptor(b'a')},
    {'binding': PROMPT, 'text': descriptor(b'')},
    {'binding': VOICE, 'clear': True},
])
def test_closed_schema_accepts_exactly_text_or_clear(args):
    Draft202012Validator(PARAMETERS).validate(args)


@pytest.mark.parametrize('args', [
    {'binding': VOICE},
    {'binding': VOICE, 'text': descriptor(b'a'), 'clear': True},
    {'binding': VOICE, 'text': dict(descriptor(b'a'), extra=1)},
    {'binding': VOICE, 'text': dict(descriptor(b'a'), media_type='text/plain')},
    {'binding': VOICE, 'text': dict(descriptor(b'a'), size=-1)},
    {'binding': VOICE, 'text': dict(descriptor(b'a'), size=MAX_BYTES + 1)},
    {'binding': {'kind': 'voiceover_script', 'slot': 'hero'}, 'clear': True},
    {'binding': {'kind': 'prompt', 'slot': 'Not Valid'}, 'clear': True},
    {'binding': VOICE, 'clear': False},
    {'binding': VOICE, 'clear': True, 'value': None},
])
def test_closed_arguments_refuse_before_execution(args, original):
    with pytest.raises(CommandError):
        execute(original, [command(args)])


def test_legacy_inline_row_is_refused_not_read_as_authority(text_seam):
    legacy = bundle()
    assert owner_payload(legacy)['text_bindings'] == [{'slot': 'voiceover', 'text': 'hello'}]
    with pytest.raises(CommandError) as error:
        execute(legacy, [command({'binding': VOICE, 'text': descriptor(b'voice')})])
    assert error.value.kind == 'unsupported_shape'


def test_object_id_must_equal_digest(original):
    text = dict(descriptor(b'a'), object_id='sha256:' + 'f' * 64)
    with pytest.raises(CommandError, match='content digest'):
        execute(original, [command({'binding': VOICE, 'text': text})])


def test_set_round_trip_records_exact_identity_and_dependency(original):
    before = copy.deepcopy(original)
    text = descriptor(b'voice')
    plan = execute(original, [capture(original, {'binding': VOICE, 'text': text})])
    assert original == before
    assert row_of(plan.bundle) == {'kind': 'voiceover_script', 'slot': None, 'media_id': text['object_id'],
                                   'content_hash': text['digest'], 'byte_size': 5, 'mime_type': MEDIA_TYPE}
    record = plan.records[0]
    assert record['writes'] == [{'ref': REF, 'path': [], 'before': ABSENT,
                                 'after': present(row_of(plan.bundle))}]
    assert record['dependencies'] == [{'key': 'text:' + text['digest'], 'kind': 'media',
                                       'pin': text, 'policy': 'exact'}]
    assert plan.commands[0]['expect'] == {REF + '::': ABSENT}
    assert plan.commands[0]['intent'] == {'ownership': 'occurrence'}


def test_inverse_restores_absence_and_redo_restores_exact_row(original):
    plan = execute(original, [capture(original, {'binding': VOICE, 'text': descriptor(b'voice')})])
    undone = inverse(plan.bundle, plan.records)
    assert undone.bundle == original
    assert inverse(undone.bundle, undone.records).bundle == plan.bundle


def test_empty_text_is_present_zero_byte_and_distinct_from_clear_and_absent(original):
    empty = descriptor(b'')
    assert empty['digest'] == 'sha256:' + hashlib.sha256(b'').hexdigest()
    set_empty = execute(original, [capture(original, {'binding': VOICE, 'text': empty})])
    assert row_of(set_empty.bundle)['byte_size'] == 0
    assert row_of(set_empty.bundle) != row_of(original)
    cleared = execute(set_empty.bundle, [capture(set_empty.bundle, {'binding': VOICE, 'clear': True})])
    assert row_of(cleared.bundle) is None
    assert cleared.records[0]['writes'][0]['after'] == ABSENT
    assert cleared.bundle == original
    assert cleared.bundle != set_empty.bundle
    # Undoing the clear restores the present zero-byte row exactly, not absence.
    assert inverse(cleared.bundle, cleared.records).bundle == set_empty.bundle


def test_clear_of_absent_binding_is_refused_not_a_silent_noop(original):
    with pytest.raises(CommandError) as error:
        execute(original, [capture(original, {'binding': VOICE, 'clear': True})])
    assert error.value.kind == 'absent_binding'


def test_same_value_set_is_recorded_not_silently_skipped(original):
    text = descriptor(b'voice')
    plan = execute(original, [capture(original, {'binding': VOICE, 'text': text})])
    again = execute(plan.bundle, [capture(plan.bundle, {'binding': VOICE, 'text': text})])
    assert again.bundle == plan.bundle
    write = again.records[0]['writes'][0]
    assert write['before'] == write['after'] == present(row_of(plan.bundle))


def test_inverse_refuses_any_intervening_write_to_the_binding(original):
    plan = execute(original, [capture(original, {'binding': VOICE, 'text': descriptor(b'voice')})])
    with pytest.raises(CommandError) as error:
        inverse(plan.bundle, plan.records, [later(REF, [], present(row_of(plan.bundle)))])
    assert error.value.kind == 'intervening_write'


def test_replay_refuses_changed_guard_and_rebases_disjoint_key(original):
    plan = execute(original, [capture(original, {'binding': VOICE, 'text': descriptor(b'voice')})])
    other_voice = execute(original, [capture(original, {'binding': VOICE, 'text': descriptor(b'other')})]).bundle
    with pytest.raises(CommandError) as error:
        replay(plan, other_voice)
    assert error.value.kind == 'dependency_changed'
    prompt = execute(original, [capture(original, {'binding': PROMPT, 'text': descriptor(b'hero')})]).bundle
    rebased = replay(plan, prompt).bundle
    assert row_of(rebased, 'prompt:hero') == row_of(prompt, 'prompt:hero')
    assert row_of(rebased) == row_of(plan.bundle)


@pytest.mark.integration
def test_runtime_port_declares_exact_text_selection():
    from tests.helpers.runtime import use_runtime

    use_runtime()
    import runtime_protocol.timeline_commit as port

    assert 'def text_selection(' in Path(port.__file__).read_text()
