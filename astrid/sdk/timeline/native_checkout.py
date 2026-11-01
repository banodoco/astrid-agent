"""Bounded ordinary clip verbs over the existing durable command session."""
from __future__ import annotations

import copy
import difflib
import re

from astrid.core.timeline import clip_fields
from astrid.sdk import timeline_intent as intent

from ._command_state import State, equal
from ._common import AUDIO_KINDS, TIME_RE, TimelineEditError, _length_seconds, _r
from .capture import move_intent, number, occurrence_start, rate
from .checkout import Checkout
from .commands import execute
from .model import Clip, _is_formula_or_moment
from .selection import Selection
from .units import from_canvas


def _held(*args, **kwargs):
    raise TimelineEditError('this verb is held for attached checkouts; use bounded clip verbs and flush()')


def _seconds(value):
    if isinstance(value, str):
        match = TIME_RE.fullmatch(value.strip())
        if match:
            return int(match.group(1) or 0) * 60 + float(match.group(2))
    return number(value, 'literal timeline seconds')


def _publishable_parent_command(command):
    if (not isinstance(command, dict) or not isinstance(command.get('select'), str)
            or not isinstance(command.get('op'), str)):
        return False
    if command.get('op') == 'track.create' and command['select'] == 'parent':
        args = command.get('args')
        row = args.get('row') if isinstance(args, dict) else None
        identity = row.get('id') if isinstance(row, dict) else None
        target = 'parent/track:' + identity if isinstance(identity, str) else None
        if (target is None or not identity or '/' in identity or '::' in identity
                or not isinstance(row.get('kind'), str) or not row['kind']):
            return False
        captured = ({'intent', 'resolved', 'reads', 'selections', 'bindings',
                     'created', 'expect', 'authorship'} <= set(command)
                    and command['intent'] == {}
                    and command['resolved'] == []
                    and command['created'] == [target]
                    and command['bindings'] == {}
                    and command['expect'] == {target + '::': {'state': 'absent'}}
                    and command['authorship'] == {target + '::': 'direct'}
                    and command['reads'] == [{'ref': target, 'path': [],
                                              'state': {'state': 'absent'}}])
        if not captured:
            return False
        selection = command['selections']
        return (isinstance(selection, list) and len(selection) == 1
                and isinstance(selection[0], dict)
                and selection[0].get('query_version') == 1
                and selection[0].get('policy') == 'guard-membership'
                and selection[0].get('key') == 'track.create'
                and selection[0].get('owner') == 'parent'
                and isinstance(selection[0].get('members'), list)
                and target not in selection[0]['members']
                and selection[0].get('order') == selection[0]['members']
                and selection[0].get('boundaries') == selection[0]['members'])
    if (command.get('op') == 'track.set'
            and re.fullmatch(r'parent/track:[^/]+', command['select']) is not None):
        target = command['select']
        captured = ({'intent', 'resolved', 'reads', 'selections', 'bindings',
                     'created', 'expect', 'authorship'} <= set(command)
                    and command['resolved'] == [target]
                    and isinstance(command['reads'], list)
                    and any(read.get('ref') == target and read.get('path') == ['id']
                            for read in command['reads'] if isinstance(read, dict))
                    and isinstance(command['bindings'], dict)
                    and command['bindings'].get('scalar.target') == target
                    and isinstance(command['expect'], dict)
                    and target + '::label' in command['expect']
                    and isinstance(command['authorship'], dict)
                    and command['authorship'].get(target + '::label') == 'direct')
        if not captured:
            return False
        args = command.get('args')
        values = args.get('values') if isinstance(args, dict) else None
        clear = args.get('clear') if isinstance(args, dict) else None
        return (isinstance(args, dict) and set(args) == {'values', 'clear'}
                and isinstance(values, dict) and set(values).issubset({'label'})
                and isinstance(clear, list) and set(clear).issubset({'label'})
                and not set(values).intersection(clear)
                and (values or clear))
    if re.fullmatch(r'parent/clip:[^/]+', command['select']) is None:
        return False
    if command.get('op') == 'clip.move':
        return True
    args = command.get('args')
    if (command.get('op') == 'clip.trim' and isinstance(args, dict)
            and set(args) == {'in_s', 'out_s'}):
        return True
    if command.get('op') == 'clip.delete' and args == {}:
        return True
    if (command.get('op') == 'clip.anchor' and isinstance(args, dict)
            and set(args) == {'field', 'declaration', 'choice'}
            and args.get('field') in {'on', 'until'}
            and (args.get('choice') is None or isinstance(args.get('choice'), dict))):
        return True
    if (command.get('op') == 'clip.split' and isinstance(args, dict)
            and set(args) == {'on', 'right_id'}):
        return True
    values = args.get('values') if isinstance(args, dict) else None
    if (command.get('op') == 'clip.set-params' and isinstance(args, dict)
            and isinstance(values, dict) and isinstance(args.get('clear'), list)
            and isinstance(args.get('schema_source'), dict)):
        return True
    return (command.get('op') == 'clip.set' and isinstance(args, dict)
            and ((args.get('clear') == [] and isinstance(values, dict)
                  and set(values) == {'hold'} and values['hold'] is not None)
                 or (set(args) == {'values', 'clear'} and values == {}
                     and args.get('clear') in (['asset'], ['transition']))))


class AttachedCheckout(Checkout):
    """A raw scoped preview, never flattened or published as a candidate."""

    def __init__(self, session):
        super().__init__({})
        self.session = session
        self._read_pending()

    def _read_pending(self):
        with self.session.outbox.locked():
            state = self.session._state()
        try:
            self.bundle = self._bundle(state)
        except Exception:
            # A selected PARAM flight already contains its exact retained pin
            # and is retried verbatim.  Its portable bytes need not be reread
            # just to reopen the draft preview after a lost acknowledgement.
            if (state['flight'] is None or not any(
                    command.get('op') == 'clip.set-params'
                    and isinstance(command.get('args', {}).get('schema_source'), dict)
                    for command in state['pending'])):
                raise
            from .store import fetch_bundle

            self.bundle = fetch_bundle(self.session.project_id, self.session.timeline_id,
                                       revision_id=state['base']['revision_id'], client=self.session.client)
        self._basis = copy.deepcopy(state['base'])
        self._pending = copy.deepcopy(state['pending'])
        self._preview = copy.deepcopy(self.bundle)
        self.fps = float(rate(self.bundle['parent'].get('config', {}).get('output', {}).get('fps')))
        self._mcache = None

    def _bundle(self, state, extra_commands=()):
        """Materialize pending selected PARAM commands from their exact pins."""
        from astrid.sdk.timeline.native_source_inputs import materialize_transcript_inputs
        from astrid.sdk.timeline_elements import _read_selected_param_inputs

        from .store import fetch_bundle

        bundle = fetch_bundle(self.session.project_id, self.session.timeline_id,
                              revision_id=state['base']['revision_id'], client=self.session.client)
        materialize_transcript_inputs(
            bundle, [*state['pending'], *extra_commands], client=self.session.client.timelines._client,
            project=self.session.project_id)
        if not state['pending']:
            return bundle
        inputs = []
        for command in state['pending']:
            if command.get('op') != 'clip.set-params':
                continue
            source = command.get('args', {}).get('schema_source')
            if not isinstance(source, dict):
                continue
            row = State(bundle).entity(command['select'])
            selected = row.get('elementRef') or row.get('element_ref')
            _, captured, _ = _read_selected_param_inputs(
                self.session.client.timelines,
                self.session.project_id,
                self.session.timeline_id,
                realm=self.session.identity['realm_id'],
                through=state['base'],
                select=command['select'],
                source=source,
                element=selected,
            )
            inputs.extend(captured)
        if not inputs:
            return execute(bundle, state['pending']).bundle
        return execute(bundle, state['pending'], schema_inputs=inputs,
                       realm_id=self.session.identity['realm_id']).bundle

    def _unchanged_preview(self):
        if not equal(self.bundle, self._preview):
            raise TimelineEditError('unqueued local changes in attached checkout; reattach before editing')
        with self.session.outbox.locked():
            self._unchanged_basis(self.session._state())

    def _unchanged_basis(self, state):
        if not equal(state['base'], self._basis) or not equal(state['pending'], self._pending):
            raise TimelineEditError('attached checkout basis changed; reattach before editing')

    def _queue(self, command, *, schema_inputs=(), realm_id=None):
        self._unchanged_preview()
        plans = []

        def capture(state):
            self._unchanged_basis(state)
            bundle = self._bundle(state, [command])
            if not self._same_preview(bundle):
                raise TimelineEditError('attached checkout basis changed; reattach before editing')
            plan = execute(bundle, [command], schema_inputs=schema_inputs, realm_id=realm_id)
            plans.append(plan)
            return plan.commands

        self.session._stage(capture)
        # Only expose the candidate after the existing outbox's atomic write.
        self._pending.extend(copy.deepcopy(plans[0].commands))
        self.bundle = plans[0].bundle
        self._preview = copy.deepcopy(self.bundle)
        self._mcache = None

    def _same_preview(self, bundle):
        """Compare authored preview state; retained source hydration is read-only."""
        def authored(value):
            value = copy.deepcopy(value)
            mapping = value.get('source_mapping')
            if isinstance(mapping, dict):
                mapping['pinned_documents'] = []
            return value
        return equal(authored(bundle), authored(self._preview))

    @property
    def clips(self):
        state = State(self.bundle)
        owners = ['parent', *('occurrence:' + row['occurrence_id'] for row in self.bundle['placements'])]
        pool = [AttachedClip(self, owner, row['id']) for owner in owners
                for row in state.collection(owner, 'clip')]
        return Selection(sorted(pool, key=lambda clip: (clip.start, clip.id)), kind='clip', tl=self)

    def clip(self, query, *, cut=None, near=None):
        if cut is not None or near is not None:
            _held()
        # A raw scope ref is also accepted, but never inferred from a display ID.
        matches = [clip for clip in self.clips if query in (clip.id, clip._ref)]
        if len(matches) != 1:
            raise TimelineEditError('attached checkout needs one exact clip ID or raw scoped clip reference')
        return matches[0]

    def flush(self, **options):
        self._unchanged_preview()
        result = self.session.flush(**options)
        self._read_pending()
        return result

    def publish(self, message='', *, idempotency_key=None, client=None, force=False, restructure=False):
        """Publish one captured parent command from an explicitly selected outbox.

        The outbox path is this draft's recovery identity. Reattach that same
        session after a restart; detached checkouts never discover its intent.
        Other attached verbs continue to use ``flush()``. Parent move, hold,
        trim, delete, split, asset clear, and selected PARAM commands retain
        their original command reads and guards. Parent track labels use the
        existing ``track.set`` command captured into the same outbox.
        """
        if (not getattr(self.session, '_explicit_outbox', False) or force or restructure
                or (client is not None and client is not self.session.client)):
            _held()
        self._unchanged_preview()

        def prepare(state):
            self._unchanged_basis(state)
            commands = state['pending']
            if len(commands) != 1 or not _publishable_parent_command(commands[0]):
                _held()
            # Rationale and request are sealed in one existing outbox write.
            # A retry may omit the message, but cannot replace the original.
            rationale = state.get('rationale', '')
            if state['flight'] is not None:
                if message and message != rationale:
                    raise TimelineEditError('in-flight publish rationale is immutable; retry its original message')
            elif message:
                state['rationale'] = message

        result = self.session._flush(idempotency_key=idempotency_key, prepare=prepare)
        self._read_pending()
        data = result['data']
        return {'old_head': data['previous_revision_id'], 'new_head': data['accepted_revision_id'],
                'structure': 'changed', 'receipt': result['receipt'],
                'idempotency_key': result['receipt']['idempotency_key'],
                'narration_pinned': [], 'transcripts': [], 'message': data.get('rationale', '')}

    def undo(self, publications, **options):
        """Reverse accepted publications through the session's guarded inverse."""
        self._unchanged_preview()
        result = self.session.undo(publications, **options)
        self._read_pending()
        return result

    save = edit = step = redo = discard = adopt = _held
    cut = cut_picture = voice = sequence = pin_narration = _held
    add = add_cut = set_cut_note = ripple_delete = close_gap = insert_time = _held
    insert_line = remove_line = apply_script = fill_slot = fill_standins = _held
    declare_gaps = reflow = retime = resolve = film_end = replace_asset = _held

    @property
    def cuts(self):
        _held()


class AttachedClip(Clip):
    def __init__(self, checkout, owner, identity):
        self._tl, self.shot_id = checkout, owner
        self._ref = owner + '/clip:' + identity

    @property
    def data(self):
        # Handles remain live after preview replacement, including materialization.
        return State(self._tl.bundle).entity(self._ref)

    @property
    def element(self):
        return self.data.get('clipType') or self.data.get('clip_type') or 'media'

    @property
    def at(self):
        return self.data.get('at', self.data.get('at_ms', 0) / 1000)

    @property
    def start(self):
        state = State(self._tl.bundle)
        return self.at + (0 if self.shot_id == 'parent' else occurrence_start(state.occurrence(self.shot_id)))

    @property
    def is_audio(self):
        tracks = State(self._tl.bundle).config(self.shot_id).get('tracks', [])
        return self.track in AUDIO_KINDS or any(
            row['id'] == self.track and row.get('kind') in AUDIO_KINDS for row in tracks)

    def _command(self, op, args):
        command = {'schema_version': 1, 'op': op, 'version': '1', 'select': self._ref, 'args': args}
        if op == 'clip.move':
            command['intent'] = move_intent(self._ref)
        self._tl._queue(command)
        return self

    def _anchor_command(self, value):
        """Queue the existing ``clip.anchor`` carrier, when already captured.

        Attached ``set`` is intentionally narrower than the legacy moment
        parser here. The value is the registered operation's own arguments,
        so a stable ``word_ids`` + ``DocumentPin`` choice remains the source
        of meaning and the local command engine can verify its existing
        ``source_mapping.pinned_documents`` capture before staging anything.
        """
        if self.shot_id != 'parent':
            _held()
        if value is None:
            args = {'field': 'on', 'declaration': None, 'choice': None}
        elif isinstance(value, dict) and set(value) == {'field', 'declaration', 'choice'}:
            if value['field'] != 'on':
                raise TimelineEditError(f'{self.address}: attached .set(on=...) only accepts field="on"')
            args = copy.deepcopy(value)
        else:
            raise TimelineEditError(
                f'{self.address}: attached word anchors require the existing '
                'clip.anchor carrier {field, declaration, choice}; text-only or mixed values are unsupported')
        command = {
            'schema_version': 1,
            'op': 'clip.anchor',
            'version': '1',
            'select': self._ref,
            'args': args,
            'intent': {'anchor': 'follow'},
        }
        self._tl._queue(command)
        return self

    def on(self, moment):
        if intent.cut_of(self.data):
            _held()
        return self._command('clip.move', {'on': _seconds(moment)})

    def enter_at(self, when, *, offset=0.0, anchor=True):
        return self.on(_seconds(when) + number(offset, 'offset'))

    def nudge(self, seconds=0.0, *, frames=0):
        if intent.on(self.data):
            _held()
        return self.on(self.start + number(seconds, 'seconds') + number(frames, 'frames') / self._tl.fps)

    def trim(self, in_s=None, out_s=None):
        return self._command('clip.trim', {'in_s': in_s, 'out_s': out_s})

    def set_duration(self, seconds):
        # Public duration is playback seconds; the native scalar is source hold.
        speed = self.data.get('speed')
        duration = self._tl.quantize(number(seconds, 'duration', positive=True))
        hold = _r(duration * number(1 if speed is None else speed, 'speed', positive=True))
        if self.data.get('from') is not None or self.data.get('to') is not None:
            if (self.data.get('from') is None or self.data.get('to') is None
                    or 'hold' not in self.data):
                _held()
            return self.trim(out_s=self.data['from'] + hold)
        return self._command('clip.set', {'values': {'hold': hold}, 'clear': []})

    def hold_for(self, seconds):
        return self.set_duration(_length_seconds(seconds, self._tl.fps))

    for_ = hold_for

    def extend(self, seconds=0.0, *, frames=0):
        return self.set_duration(self.duration + number(seconds, 'seconds') + number(frames, 'frames') / self._tl.fps)

    def clear_asset(self):
        return self._command('clip.set', {'values': {}, 'clear': ['asset']})

    def _check_selected_param_keys(self, keys, schema):
        properties = schema.get('properties') or {}
        tops = [re.split(r'[.\[]', key, maxsplit=1)[0] for key in keys]
        timing = [key for key in tops
                  if key in self._TIMING_WORDS and key not in properties]
        if timing:
            raise TimelineEditError(f'{self.address}: {", ".join(timing)} is not a param: use '
                                    + '; '.join(f'{key} → {self._TIMING_WORDS[key]}' for key in timing))
        bad = [key for key in tops if key not in properties and key not in self.params]
        if bad:
            declared = sorted(properties)
            hints = [f'{key} (did you mean {", ".join(difflib.get_close_matches(key, declared, n=2))}?)'
                     if difflib.get_close_matches(key, declared, n=2) else key for key in bad]
            raise TimelineEditError(f'{self.address} is an attached selected element, which has no param '
                                    f'{", ".join(hints)}. It takes: {", ".join(declared)} '
                                    ' (an undeclared param on purpose: .set(..., _allow_new=True))')

    def set(self, **params):
        allow_new = params.pop('_allow_new', False)
        if 'on' in params:
            if len(params) != 1:
                raise TimelineEditError(f'{self.address}: attached word anchors cannot be mixed with other .set fields')
            return self._anchor_command(params['on'])
        if not allow_new and params == {'transition': None}:
            if self.shot_id != 'parent':
                _held()
            return self._command('clip.set', {'values': {}, 'clear': ['transition']})
        selected = self.data.get('elementRef') or self.data.get('element_ref')
        if isinstance(selected, dict) and selected.get('packId'):
            if any('.' in key or '[' in key or _is_formula_or_moment(value) for key, value in params.items()):
                _held()
            if intent.formulas(self.data):
                _held()
            if any(key in clip_fields.FIELDS or clip_fields.is_reserved(key) for key in params):
                _held()
            self._tl._unchanged_preview()
            with self._tl.session.outbox.locked():
                state = self._tl.session._state()
                through = copy.deepcopy(state['base'])
            try:
                from astrid.sdk.timeline_elements import _read_selected_param_inputs

                _, schema_inputs, descriptor = _read_selected_param_inputs(
                    self._tl.session.client.timelines,
                    self._tl.session.project_id,
                    self._tl.session.timeline_id,
                    realm=self._tl.session.identity['realm_id'],
                    through=through,
                    select=self._ref,
                )
            except Exception as exc:
                raise TimelineEditError(
                    f'selected pack params require exact schema inputs (retained declaration): {exc}') from exc
            if not allow_new:
                self._check_selected_param_keys(list(params), descriptor['schema'])
            if not params:
                return self
            command = {
                'schema_version': 1,
                'op': 'clip.set-params',
                'version': '1',
                'select': self._ref,
                'args': {
                    # Selected pack declarations are not substituted with a
                    # locally installed alias.  The native command receives
                    # the caller's literal values and validates them against
                    # the retained descriptor above.
                    'values': {key: value
                               for key, value in params.items() if value is not None},
                    'clear': [key for key, value in params.items() if value is None],
                    'schema_source': copy.deepcopy(schema_inputs[0][0]['pin']),
                },
            }
            self._tl._queue(command, schema_inputs=schema_inputs,
                            realm_id=self._tl.session.identity['realm_id'])
            return self
        if any('.' in key or '[' in key or _is_formula_or_moment(value) for key, value in params.items()):
            _held()
        if intent.formulas(self.data):
            _held()
        from astrid.sdk.timeline_address import element_schema

        declared = element_schema(self.element).get('properties', {})
        if any(key not in declared and (key in clip_fields.FIELDS or clip_fields.is_reserved(key))
               for key in params):
            _held()
        if not allow_new:
            self._check_param_keys(list(params))
        if not params:
            return self
        return self._command('clip.set-params', {
            'values': {key: from_canvas(self.element, key, value) for key, value in params.items() if value is not None},
            'clear': [key for key, value in params.items() if value is None],
        })

    def set_param(self, path, value):
        return self.set(**{path: value}, _allow_new=True)

    def remove(self):
        if self.shot_id != 'parent':
            _held()
        return self._command('clip.delete', {})

    until = end_at = slip = keep = remove_layer = _held
    set_look = tag = move_to_cut = set_track = swap_asset = set_beats = keyframe_at = _held
