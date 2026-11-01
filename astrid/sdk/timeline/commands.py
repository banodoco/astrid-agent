"""Small native EditCommand engine over main's lossless authoring bundle."""
from __future__ import annotations

import copy
import json
from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from pathlib import Path

from jsonschema import Draft202012Validator

from ._command_state import CommandError, address, equal, spelling
from ._recording import Recording, guard, selection, selection_matches

_SCHEMA = json.loads((Path(__file__).parents[2] / 'core/timeline/schema/edit-command.schema.json').read_text())
_VALIDATOR = Draft202012Validator(_SCHEMA)
TRACK_CREATE_PARAMETERS = _SCHEMA['$defs']['TrackCreateArgs']


@dataclass(frozen=True)
class Operation:
    parameters: dict
    handler: Callable[[Recording, dict], None]
    intent: dict
    singleton: bool = False


def operations():
    """Native recipe entries; the full extensible registry belongs to M2."""
    from ._clip_commands import clip_add
    from ._clip_scalar_commands import clip_set, clip_set_element
    from ._delete_commands import delete
    from ._doc_patch import patch
    from ._scalar_commands import clip_set_params, track_set
    from ._shot_commands import SHOT_INTENT, shot_set
    from ._split_commands import clip_split
    from ._trim_commands import clip_trim
    from .capture import MOVE_INTENT, move, track_create
    from .native_initialize import initialize
    from .native_media_admission import PARAMETERS as MEDIA_INPUT_PARAMETERS
    from .native_media_admission import admit
    from .native_occurrence_create import occurrence_create
    from .native_occurrence_delete import occurrence_delete
    from .native_occurrence_duplicate import OCCURRENCE_INTENT, occurrence_duplicate
    from .native_occurrence_fork import FORK_PARAMETERS, occurrence_fork
    from .native_occurrence_media_commands import (
        MEDIA_INTENT,
        add_media,
        parent_swap_media,
        swap_media,
    )
    from .native_occurrence_shared import PARAMETERS as SHARED_PARAMETERS
    from .native_occurrence_shared import occurrence_place_shared
    from .native_parent_media_commands import (
        DUPLICATE_MEDIA_PARAMETERS,
        assembled_target,
        duplicate_media,
    )
    from .native_parent_media_commands import add_media as parent_add_media
    from .native_placement_commands import (
        PLACEMENT_INTENT,
        placement_create,
        placement_move,
        placement_unplace,
    )
    from .native_restructure import PARAMETERS as RESTRUCTURE_PARAMETERS
    from .native_restructure import restructure
    from .native_shot_pool_commands import POOL_INTENT, shot_pool
    from .native_text_binding import TEXT_INTENT, text_binding_set
    from .native_timeline_duplicate import PARAMETERS as DUPLICATE_PARAMETERS
    from .native_timeline_duplicate import duplicate
    from .native_word_anchor import declare
    from .producer import producer_guard
    return {'doc.patch': Operation(_SCHEMA['$defs']['DocPatchArgs'], patch, {}),
            'media.admit': Operation(MEDIA_INPUT_PARAMETERS, admit, {}),
            'text-binding.set': Operation(_SCHEMA['$defs']['TextBindingSetArgs'], text_binding_set, TEXT_INTENT, True),
            'timeline.duplicate': Operation(DUPLICATE_PARAMETERS, duplicate, {}),
            'timeline.restructure': Operation(RESTRUCTURE_PARAMETERS, restructure, {}),
            'timeline.init': Operation(_SCHEMA['$defs']['TimelineInitArgs'], initialize, {}),
            'clip.anchor': Operation(_SCHEMA['$defs']['ClipAnchorArgs'], declare, {'anchor': 'follow'}, True),
            'occurrence.create': Operation(_SCHEMA['$defs']['OccurrenceCreateArgs'], occurrence_create,
                                          OCCURRENCE_INTENT),
            'clip.move': Operation(_SCHEMA['$defs']['ClipMoveArgs'], move, MOVE_INTENT, True),
            'placement.move': Operation(_SCHEMA['$defs']['ClipMoveArgs'], placement_move, MOVE_INTENT, True),
            'occurrence.delete': Operation(_SCHEMA['$defs']['OccurrenceDeleteArgs'], occurrence_delete, OCCURRENCE_INTENT, True),
            'placement.create': Operation(_SCHEMA['$defs']['PlacementCreateArgs'], placement_create,
                                          PLACEMENT_INTENT, True),
            'placement.unplace': Operation(_SCHEMA['$defs']['PlacementUnplaceArgs'], placement_unplace,
                                           PLACEMENT_INTENT, True),
            'shot.pool': Operation(_SCHEMA['$defs']['ShotPoolArgs'], shot_pool, POOL_INTENT, True),
            'occurrence.duplicate': Operation(_SCHEMA['$defs']['OccurrenceDuplicateArgs'], occurrence_duplicate,
                                              OCCURRENCE_INTENT, True),
            'occurrence.fork': Operation(FORK_PARAMETERS, occurrence_fork, OCCURRENCE_INTENT, True),
            'occurrence.place-shared': Operation(SHARED_PARAMETERS, occurrence_place_shared,
                                                OCCURRENCE_INTENT, True),
            'clip.delete': Operation(_SCHEMA['$defs']['ClipDeleteArgs'], delete, {}, True),
            'occurrence.add-media': Operation(_SCHEMA['$defs']['OccurrenceAddMediaArgs'], add_media, MEDIA_INTENT, True),
            'occurrence.swap-media': Operation(_SCHEMA['$defs']['OccurrenceSwapMediaArgs'], swap_media, MEDIA_INTENT, True),
            'clip.swap-media': Operation(_SCHEMA['$defs']['OccurrenceSwapMediaArgs'], parent_swap_media,
                                         {'ownership': 'parent'}, True),
            'track.create': Operation(TRACK_CREATE_PARAMETERS, track_create, {}),
            'clip.set': Operation(_SCHEMA['$defs']['ClipSetArgs'], clip_set, {}, True),
            'clip.set-element': Operation(_SCHEMA['$defs']['ClipSetElementArgs'], clip_set_element, {}, True),
            'clip.add': Operation(_SCHEMA['$defs']['ClipAddArgs'], clip_add, {}),
            'clip.trim': Operation(_SCHEMA['$defs']['ClipTrimArgs'], clip_trim, {}, True),
            'clip.split': Operation(_SCHEMA['$defs']['ClipSplitArgs'], clip_split, {}, True),
            'track.set': Operation(_SCHEMA['$defs']['TrackSetArgs'], track_set, {}, True),
            'shot.set': Operation(_SCHEMA['$defs']['ShotSetArgs'], shot_set, SHOT_INTENT, True),
            'clip.set-params': Operation(_SCHEMA['$defs']['ClipSetParamsArgs'], clip_set_params, {}, True),
            'timeline.assembled-target': Operation(_SCHEMA['$defs']['AssembledTargetArgs'], assembled_target, {}),
            'clip.add-media': Operation(_SCHEMA['$defs']['ClipAddMediaArgs'], parent_add_media, {}),
            'clip.duplicate-media': Operation(DUPLICATE_MEDIA_PARAMETERS, duplicate_media,
                                               {'ownership': 'parent'}, True),
            'producer.guard': Operation(_SCHEMA['$defs']['ProducerGuardArgs'], producer_guard, {})}


@dataclass
class Plan:
    bundle: dict
    records: list[dict]
    semantic_events: list[dict] = dataclass_field(default_factory=list)
    steps: list[dict] = dataclass_field(default_factory=list)
    label: str = 'Timeline edit'
    merge_mode: str = 'fields'

    @property
    def commands(self):
        return [copy.deepcopy(record['command']) for record in self.records]


def validate_command(command):
    try:
        json.dumps(command, allow_nan=False)
    except (ValueError, TypeError) as exc:
        raise CommandError("command must be finite JSON") from exc
    errors = sorted(_VALIDATOR.iter_errors(command), key=lambda e: str(e.path))
    if errors:
        raise CommandError(errors[0].message)
    if command['version'] != '1':
        raise CommandError("unsupported operation version", kind='unsupported_version')
    if command['op'] not in (*operations(), 'timeline.inverse', 'timeline.restore'):
        raise CommandError(f"unsupported operation {command['op']!r}", kind='unsupported_operation')
    operation = operations().get(command['op'])
    if operation is not None:
        errors = list(_VALIDATOR.evolve(schema=operation.parameters).iter_errors(command['args']))
        if errors:
            raise CommandError(errors[0].message)
    if command['op'] == 'doc.patch':
        from ._doc_patch import validate_submitted_change
        for change in command['args']['changes']:
            validate_submitted_change(change)
    if command['op'] == 'track.create':
        if command['select'] != 'parent' and not (
                isinstance(command['select'], str) and command['select'].startswith('occurrence:')
                and '/' not in command['select']):
            raise CommandError("track.create requires parent or occurrence owner")
        if command.get('intent', {}) != {}:
            raise CommandError("track.create has no intention overrides")

    if command['op'] == 'producer.guard':
        pin = command['args']['pin']
        if any(type(pin[key]) is not int for key in ('fence', 'runtime_epoch')):
            raise CommandError('producer fence and epoch must be positive integers')


def _normalize(command):
    command = copy.deepcopy(command)
    validate_command(command)
    operation = operations().get(command['op'])
    resolved = command.get('resolved', [command['select']] if operation and operation.singleton else [])
    for field in ('expect', 'authorship'):
        if field not in command:
            continue
        normalized = {}
        for key, value in command[field].items():
            ref, path = address(key, resolved)
            canonical = ref + '::' + spelling(path)
            if canonical in normalized and not equal(normalized[canonical], value):
                raise CommandError("inconsistent equivalent property guards", ref=ref, path=path)
            normalized[canonical] = value
        command[field] = normalized
    return command


def _verify_claims(ctx, command):
    for ref in command.get('created', []):
        if ctx.state.entity(ref, optional=True) is not None:
            raise CommandError('created identity is occupied', kind='identity_collision', ref=ref)
    for key, expected in command.get('expect', {}).items():
        ref, path = address(key, command.get('resolved', []))
        guard(ctx.state, ref, path, expected)
    for read in command.get('reads', []):
        guard(ctx.state, read['ref'], read['path'], read['state'])
    for selected in command.get('selections', []):
        actual = selection(ctx.state, selected['key'], selected['owner'], selected['members'])
        if not selection_matches(selected, actual):
            raise CommandError("original selection membership/order changed", kind='selection_changed', ref=selected['owner'])


def execute(bundle, commands, *, inverse_records=(), intervening_records=(), id_map=None,
            closures=None, verified_media=(), schema_inputs=(), realm_id=None, resolve_inputs=None,
            verified_producer=None, commit_request=None, evaluated_head=None, replay_bindings=None):
    """Execute atomically. History inputs must already be authenticated by provider.

    Empty intervening_records means proven empty ancestry, never unavailable
    history. Incoming recorder fields are checked claims; generated records are
    the authority. No persistence, mutable reads or renderer lives here.
    """
    from .inverse import apply_inverse

    if not commands:
        raise CommandError("command batch must not be empty")
    from .producer import guard_pin
    guard_pin(commands)
    if id_map and any(key != value for key, value in id_map.items()):
        raise CommandError("id_map must contain identity pairs only", kind='identity_collision')
    if any(call.get('op') == 'timeline.restore' for call in commands) and len(commands) != 1:
        raise CommandError("restore must be the sole command", kind='invalid_commands')
    from ._commit_patch import CommitPatch

    for supplied in commands:
        validate_command(supplied)
    candidate, records = copy.deepcopy(bundle), []
    if any(call['op'] == 'timeline.inverse' for call in commands):
        from ._root_inverse import normalize_root_inputs

        normalize_root_inputs(candidate, inverse_records or ())
    metadata = CommitPatch(commit_request, commands, bundle, evaluated_head=evaluated_head)
    for ordinal, supplied in enumerate(commands):
        command = _normalize(supplied)
        ctx = Recording(candidate, command, ordinal, closures=closures, verified_media=verified_media,
                        schema_inputs=schema_inputs, realm_id=realm_id,
                        verified_producer=verified_producer)
        ctx.patch_replay_bindings = replay_bindings or {}
        metadata.before(ctx)
        # Declarations are dependency observations, not native authored claims.
        # Restore them after exact command capture validation for history/undo.
        inherited_reads, ctx.record['reads'] = ctx.record['reads'], []
        _verify_claims(ctx, command)
        if resolve_inputs is not None:
            ctx.prior_records = records
            ctx.schema_inputs = resolve_inputs(ctx)
        operation = operations().get(command['op'])
        if operation is not None:
            from .native_word_anchor import guard_basis

            guard_basis(ctx, command)
            from .capture import move_intent
            intent = move_intent(command['select']) if command['op'] in ('clip.move', 'placement.move') else operation.intent
            supplied_intent = {**intent, **command.get('intent', {})}
            if supplied_intent != intent:
                raise CommandError("unsupported operation intention policy")
            ctx.record['command']['intent'] = dict(intent)
            operation.handler(ctx, command)
            singleton = operation.singleton and not (
                command['op'] == 'occurrence.place-shared' and 'source_ref' in command['args'])
            ctx.record['command']['resolved'] = [command['select']] if singleton else []
        elif command['op'] == 'timeline.restore':
            ctx.read('timeline')
            ctx.write('timeline', [], command['args'])
            ctx.record['command']['intent'] = {}
            ctx.record['command']['resolved'] = []
        else:
            from .native_occurrence_shared import prepare_shared_inverse

            prepare_shared_inverse(ctx, inverse_records)
            apply_inverse(ctx, inverse_records, intervening_records, staged_records=records)
            ctx.record['command']['intent'] = {}
            ctx.record['command']['resolved'] = []
        if ordinal == len(commands) - 1:
            ctx.compile_inputs()
            from .native_word_anchor import compile_recorded

            compile_recorded(ctx, inverse_records=inverse_records)
            metadata.compiler(ctx, records)
        ctx.compile_mirrors()
        record = ctx.finish(capture=True)
        metadata.after(ctx, inverse_records=inverse_records)
        for field in ('resolved', 'expect', 'authorship', 'reads', 'selections', 'bindings', 'created'):
            if field not in command:
                continue
            matches = equal(command[field], record['command'][field])
            if field == 'selections':
                matches = len(command[field]) == len(record['command'][field]) and all(
                    selection_matches(claimed, actual)
                    for claimed, actual in zip(command[field], record['command'][field]))
            if field == 'reads':
                actual = record['command']['reads']
                matches = len(command[field]) == len(actual) and all(
                    claimed['ref'] == read['ref'] and claimed['path'] == read['path']
                    and (equal(claimed['state'], read['state'])
                         or ctx.command_input.matches(read['ref'], read['path'], claimed['state']))
                    for claimed, read in zip(command[field], actual))
            if field == 'expect' and command[field].keys() == record['command'][field].keys():
                matches = all(ctx.command_input.matches(*address(key, command.get('resolved', [])), expected)
                              for key, expected in command[field].items())
            if not matches:
                raise CommandError(f"submitted {field} does not match authoritative capture", kind='capture_mismatch')
        record['reads'] = [*inherited_reads, *(read for read in record['reads']
                           if not any(equal(read, inherited) for inherited in inherited_reads))]
        # Keep original claims and metadata after they have been verified.
        record['command'].update({k: v for k, v in command.items() if k != 'intent'})
        records.append(record)
    from .native_media_admission import validate_admissions

    validate_admissions(candidate, commands)
    return metadata.finish(Plan(candidate, records))
