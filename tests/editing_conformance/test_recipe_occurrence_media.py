"""Native popup capture, exact recorder parity and scoped asset birth/inverse."""
from __future__ import annotations

import copy
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.sdk.timeline._command_state import CommandError, State, overlaps
from astrid.sdk.timeline.commands import execute, validate_command
from astrid.sdk.timeline.commit import (
    _digest,
    _manifest,
    bundle_from_document,
    document_from_bundle,
)
from astrid.sdk.timeline.native_occurrence_media_commands import (
    capture_occurrence_add_media,
    capture_occurrence_swap_media,
)
from astrid.sdk.timeline.rebase import replay
from tests.editing_conformance import test_commit_runtime_occurrence_media as runtime_occurrence
from tests.editing_conformance.fixture_loader import load_fixture
from tests.editing_conformance.test_recipe_m1_commands import inverse, later
from tests.sdk.test_timeline_commit_provider import (
    CommitRejected,
    DetachedPort,
    DetachedRealm,
    refusal,
)
from tests.sdk.test_timeline_commit_provider import inverse as inverse_call
from tests.sdk.test_timeline_commit_provider_document import MEDIA, document_fixture

OWNER = 'occurrence:occ-a'
TARGET = OWNER + '/clip:clip-a'
NEW = 'sha256:' + '2' * 64
FIXTURE = Path(__file__).parent / 'fixtures' / 'recipe_m2_occurrence_media.json'


@pytest.fixture(autouse=True)
def _runtime_abi(monkeypatch, request):
    if {'genuine_same_alias_realm', 'genuine_video_editing'} & set(request.fixturenames):
        return
    from tests.sdk.test_timeline_commit_provider import runtime_abi

    runtime_abi.__wrapped__(monkeypatch)


def inputs(kind='image'):
    document = document_fixture()
    payload = document['internal_timeline_revisions'][0]['payload']
    payload.pop('source_window')
    payload['registry']['assets']['picture']['type'] = kind
    if kind == 'video':
        payload['clips'][0].update({'from': 2, 'to': 6, 'hold': 4, 'speed': 2})
    for row in document['parent_revision']['payload']['occurrences']:
        row.update(source_offset=0, speed=1)
    document['parent_revision']['payload']['occurrences'][0]['placement']['start_ms'] = 8000
    for image in [document['parent_revision'], *document['internal_timeline_revisions']]:
        image['content_digest'] = _digest(image['payload'])
    document['dependency_manifest'] = _manifest(document['shot_revisions'],
        document['internal_timeline_revisions'], document['dependency_manifest']['media'], [])
    old = {'asset': 'picture', 'generation_id': 'generation-old', 'variant_id': 'variant-old',
           'object_id': MEDIA, 'catalog_digest': 'sha256:' + 'a' * 64}
    new = {'asset': 'reserved-new', 'generation_id': 'generation-new', 'variant_id': 'variant-new',
           'object_id': NEW, 'catalog_digest': 'sha256:' + 'b' * 64}
    verified = [{'selection': copy.deepcopy(pin), 'media_type': kind + '/fixture', 'size': 123,
                 **({'duration_seconds': 6} if kind == 'video' else {})} for pin in (old, new)]
    return document, old, new, verified


def captured(operation, kind='image', *, clip_type=None):
    document, old, new, verified = inputs(kind)
    bundle = bundle_from_document(document)
    if operation == 'add':
        row = {'id': 'reserved-child', 'clipType': kind if clip_type is None else clip_type,
               'asset': new['asset'], 'track': 'picture',
               'at': .125, 'hold': 4 if kind == 'video' else 1.25,
               'params': None, 'opaque': [None, False, {}],
               **({'from': 2, 'to': 6, 'speed': 2} if kind == 'video' else {})}
        command = capture_occurrence_add_media(bundle, OWNER, row, new, verified_media=verified)
    else:
        if clip_type is not None:
            State(bundle).entity(TARGET)['clipType'] = clip_type
        command = capture_occurrence_swap_media(bundle, TARGET, old, new, verified_media=verified)
    return document, bundle, command, verified


@pytest.mark.parametrize('kind', ['image', 'video'])
def test_same_alias_rebind_updates_known_consumers_and_preserves_opaque_registry_fields(kind):
    document, old, new, verified = inputs(kind)
    bundle = bundle_from_document(document)
    old = copy.deepcopy(old)
    new = {**copy.deepcopy(new), 'asset': old['asset']}
    verified = [
        {**row, 'selection': {**row['selection'], 'asset': old['asset']}}
        if row['selection']['asset'] == 'reserved-new' else row
        for row in verified
    ]
    State(bundle).config(OWNER)['registry']['assets'][old['asset']]['label'] = 'keep-label'
    State(bundle).config(OWNER)['registry']['assets'][old['asset']]['opaque'] = {'keep': [1, None]}
    if kind == 'video':
        State(bundle).registry(OWNER)['assets'][old['asset']]['duration'] = 6
        verified[-1]['duration_seconds'] = 8
        for row in State(bundle).config(OWNER)['clips']:
            if row.get('asset') == old['asset'] and 'from' not in row:
                row.update({'from': 0, 'to': row['hold']})
    State(bundle).entity(TARGET)['asset'] = old['asset']
    State(bundle).occurrence(OWNER).update(source_offset=0, speed=1)
    before = copy.deepcopy(bundle)
    command = capture_occurrence_swap_media(bundle, TARGET, old, new, verified_media=verified)

    plan = execute(bundle, [command], verified_media=verified)
    entry = State(plan.bundle).config(OWNER)['registry']['assets'][old['asset']]
    assert entry['media_id'] == NEW
    assert entry['generationId'] == new['generation_id']
    assert entry['variantId'] == new['variant_id']
    assert entry['label'] == 'keep-label'
    assert entry['opaque'] == {'keep': [1, None]}
    if kind == 'video':
        assert entry['duration'] == 8
    assert State(plan.bundle).entity(TARGET)['asset'] == old['asset']
    assert plan.bundle['shots'][State(plan.bundle).occurrence(OWNER)['shot_id']]['payload']['items'][0]['media_id'] == NEW
    assert inverse(plan.bundle, plan.records).bundle == before
    aba = copy.deepcopy(plan.bundle)
    State(aba).config(OWNER)['registry']['assets'][old['asset']]['media_id'] = old['object_id']
    media_path = ['registry', 'assets', old['asset'], 'media_id']
    with pytest.raises(CommandError, match='intervening'):
        inverse(aba, plan.records, [{
            'writes': [{'ref': OWNER + '/source', 'path': media_path,
                        'before': {'state': 'present', 'value': NEW},
                        'after': {'state': 'present', 'value': old['object_id']}}]
        }])


@pytest.mark.parametrize('operation', ['add', 'swap'])
@pytest.mark.parametrize('kind', ['image', 'video'])
def test_exact_capture_replay_scoped_birth_and_inverse(operation, kind):
    document, bundle, command, verified = captured(operation, kind)
    before = copy.deepcopy(bundle)
    validate_command(command)
    plan = execute(bundle, [command], verified_media=verified)
    assert replay(plan, bundle, verified_media=verified).records == plan.records
    assert inverse(plan.bundle, plan.records).bundle == before
    assert bundle == before
    assert plan.bundle['parent'] == before['parent']
    sibling = before['placements'][1]['shot_id']
    assert plan.bundle['shots'][sibling] == before['shots'][sibling]
    assert command['created'] == [OWNER + '/asset:reserved-new'] + (
        [OWNER + '/clip:reserved-child'] if operation == 'add' else [])
    assert NEW not in {pin['media_id'] for pin in document['dependency_manifest']['media']}
    output = document_from_bundle(plan.bundle)
    assert NEW in {pin['media_id'] for pin in output['dependency_manifest']['media']}
    row = State(plan.bundle).entity(OWNER + '/clip:reserved-child' if operation == 'add' else TARGET)
    assert row['at'] == (.125 if operation == 'add' else 1)
    if kind == 'video':
        assert (row['from'], row['to'], row['hold'], row['speed']) == (2, 6, 4, 2)


@pytest.mark.parametrize('operation', ['add', 'swap'])
def test_image_hold_clip_type_preserves_literal_hold_semantics(operation):
    _, bundle, command, verified = captured(operation, 'image', clip_type='hold')
    before = copy.deepcopy(bundle)
    plan = execute(bundle, [command], verified_media=verified)
    row = State(plan.bundle).entity(OWNER + '/clip:reserved-child' if operation == 'add' else TARGET)
    assert row['clipType'] == 'hold'
    assert row['hold'] == (1.25 if operation == 'add' else 2)
    assert all(key not in row for key in ('from', 'to', 'speed'))
    assert inverse(plan.bundle, plan.records).bundle == before


@pytest.mark.parametrize('operation', ['add', 'swap'])
def test_portable_goldens(operation):
    golden = load_fixture(FIXTURE)[operation]
    document, bundle, command, verified = captured(operation, 'video')
    plan = execute(bundle, [command], verified_media=verified)
    assert golden == {'input': document, 'verified_media': verified, 'commands': [command],
                      'records': plan.records, 'output': document_from_bundle(plan.bundle)}


@pytest.mark.parametrize('operation', ['add', 'swap'])
def test_existing_binding_is_required_exactly_without_birth(operation):
    _, bundle, command, verified = captured(operation)
    ctx = State(bundle)
    assets = ctx.config(OWNER)['registry']['assets']
    assets['reserved-new'] = {'media_id': NEW, 'type': 'image',
                             'generationId': 'generation-new', 'variantId': 'variant-new'}
    plain = {key: command[key] for key in ('schema_version', 'op', 'version', 'select', 'args', 'intent')}
    plan = execute(bundle, [plain], verified_media=verified)
    assert OWNER + '/asset:reserved-new' not in plan.commands[0]['created']
    assert inverse(plan.bundle, plan.records).bundle == bundle
    assets['reserved-new']['opaque'] = None
    with pytest.raises(CommandError, match='existing registry binding'):
        execute(bundle, [plain], verified_media=verified)


@pytest.mark.parametrize('operation', ['add', 'swap'])
@pytest.mark.parametrize('damage', ['missing-input', 'wrong-object', 'wrong-catalog', 'shared', 'parent',
    'display-child', 'track', 'source-map', 'window', 'zero-speed', 'negative-speed', 'boolean-speed',
    'outside', 'hold-clip-type', 'membership', 'order', 'birth', 'binding'])
def test_refusals_preserve_input(operation, damage):
    _, bundle, command, verified = captured(operation, 'video')
    if damage == 'missing-input':
        verified.pop()
    elif damage in ('wrong-object', 'wrong-catalog'):
        command['args']['media']['object_id' if damage == 'wrong-object' else 'catalog_digest'] = MEDIA
    elif damage == 'shared':
        command['intent']['ownership'] = 'shared'
    elif damage == 'parent':
        command['select'] = 'parent'
    elif damage == 'display-child':
        command['select'] = OWNER + '/clip:occ-a:clip-a'
    elif damage == 'track':
        State(bundle).config(OWNER)['tracks'][0]['hidden'] = True
    elif damage == 'source-map':
        State(bundle).occurrence(OWNER)['source_offset'] = 1
    elif damage in ('window', 'zero-speed', 'negative-speed', 'boolean-speed', 'outside'):
        row = command['args']['row'] if operation == 'add' else State(bundle).entity(TARGET)
        if damage == 'window':
            row['to'] = 7
        elif damage == 'outside':
            row['at'] = 9
        else:
            row['speed'] = {'zero-speed': 0, 'negative-speed': -1, 'boolean-speed': True}[damage]
    elif damage == 'hold-clip-type':
        row = command['args']['row'] if operation == 'add' else State(bundle).entity(TARGET)
        row['clipType'] = 'hold'
    elif damage in ('membership', 'order'):
        rows = State(bundle).config(OWNER)['tracks' if operation == 'add' else 'clips']
        if damage == 'membership':
            rows.append(dict(copy.deepcopy(rows[0]), id='other'))
        else:
            rows.insert(0, dict(copy.deepcopy(rows[0]), id='other'))
    elif damage == 'birth':
        command['created'] = []
    else:
        command['bindings']['media.owner'] = 'occurrence:foreign'
    before = copy.deepcopy(bundle)
    with pytest.raises(CommandError):
        execute(bundle, [command], verified_media=verified)
    assert bundle == before


def test_asset_address_alias_overlap_and_guarded_dependency():
    assert overlaps((OWNER + '/asset:reserved-new', ()),
                    (OWNER + '/source', ('registry', 'assets', 'reserved-new', 'type')))
    _, bundle, command, verified = captured('swap')
    plan = execute(bundle, [command], verified_media=verified)
    # A later clip uses the created alias even when the object was already pinned.
    State(plan.bundle).entity(OWNER + '/clip:clip-b')['asset'] = 'reserved-new'
    with pytest.raises(CommandError, match='surviving dependents'):
        inverse(plan.bundle, plan.records)


def test_disjoint_edit_rebases_but_same_value_asset_write_blocks_undo():
    _, bundle, command, verified = captured('swap')
    plan = execute(bundle, [command], verified_media=verified)
    head = copy.deepcopy(bundle)
    State(head).entity('parent/clip:clip-b')['at'] = 6
    rebased = replay(plan, head, verified_media=verified,
                     intervening_records=[later('parent/clip:clip-b', ['at'], 6)])
    assert State(inverse(rebased.bundle, rebased.records).bundle).entity('parent/clip:clip-b')['at'] == 6
    with pytest.raises(CommandError, match='intervening'):
        inverse(plan.bundle, plan.records, [later(OWNER + '/source',
            ['registry', 'assets', 'reserved-new', 'type'], 'image')])


class MediaRealm(DetachedRealm):
    def __init__(self, document, verified):
        super().__init__(document)
        self.verified = copy.deepcopy(verified)

    def call(self, calls, *, base=None, dry_run=False):
        from astrid.sdk.timeline.commit import create_runtime_commit_service

        class MediaPort(DetachedPort):
            def selected_media(self, pin):
                matches = [value for value in self.realm.verified if value['selection'] == pin]
                if not matches:
                    raise refusal('dependency_unavailable')
                return copy.deepcopy(matches[0])

        context = self.context(calls, base=base, dry_run=dry_run)
        return create_runtime_commit_service().commit(context, MediaPort(self, context))


def _genuine_editing_fixture(kind, operation, tmp_path, monkeypatch):
    """Reuse the current Runtime bootstrap and registered Agent provider."""
    fixture = runtime_occurrence.editing.__wrapped__(
        SimpleNamespace(param=kind), tmp_path, monkeypatch, operation,
        OWNER, lambda media_kind, name: name.encode(),
    )
    try:
        yield next(fixture)
    finally:
        next(fixture, None)


@pytest.fixture
def genuine_same_alias_realm(tmp_path, monkeypatch):
    yield from _genuine_editing_fixture('image', 'swap', tmp_path, monkeypatch)


@pytest.fixture
def genuine_video_editing(request, tmp_path, monkeypatch):
    operation = request.getfixturevalue('operation')
    yield from _genuine_editing_fixture('video', operation, tmp_path, monkeypatch)


def _genuine_request(editing, commands, label, *, dry_run=False):
    patches = [change for command in commands if command['op'] == 'doc.patch'
               for change in command['args']['changes']]
    return {
        **runtime_occurrence.body(editing, commands, dry_run=dry_run),
        'label': label,
        'steps': [{
            'id': label, 'label': label, 'intent': 'Occurrence media edit',
            'tags': ['occurrence-media'], 'command_ordinals': list(range(len(commands))),
            'patch': patches, 'reads': [],
        }],
        'merge': 'atomic', 'reads': [],
        'rationale': 'Apply an occurrence media edit.', 'group_id': 'F-D86-occurrence-recipe3',
    }


def _genuine_preview(editing):
    raw = runtime_occurrence.raw_command(editing)
    runtime_occurrence.commit(
        editing, _genuine_request(editing, [raw], 'occurrence-media-preview', dry_run=True),
        'occurrence-media-preview',
    )


def _genuine_capture(editing, operation, *, same_alias=False, document=None):
    bundle = bundle_from_document(document or editing.closures[-1])
    verified = copy.deepcopy(editing.selected)
    media = copy.deepcopy(editing.new)
    if same_alias:
        media['asset'] = editing.old['asset']
        for value in verified:
            if value['selection']['object_id'] == media['object_id']:
                value['selection']['asset'] = media['asset']
    if operation == 'add':
        raw = runtime_occurrence.raw_command(editing)
        return capture_occurrence_add_media(
            bundle, OWNER, raw['args']['row'], media, verified_media=verified,
        )
    return capture_occurrence_swap_media(
        bundle, TARGET, editing.old, media, verified_media=verified,
    )


def _genuine_same_alias_baseline(editing):
    document = copy.deepcopy(editing.closures[-1])
    bundle = bundle_from_document(document)
    shot = State(bundle).occurrence(OWNER)['shot_id']
    before = bundle['shots'][shot]['payload']['unknown']['keep']
    patch = {
        'schema_version': 1, 'op': 'doc.patch', 'version': '1', 'select': None,
        'args': {'changes': [{
            'op': 'set', 'ref': 'shot:' + shot, 'path': ['unknown', 'keep'],
            'before': {'state': 'present', 'value': before}, 'value': True,
        }]},
    }
    result = _genuine_commit(editing, patch, 'same-alias-baseline', 'same-alias-baseline')
    return copy.deepcopy(result['data']['canonical_document'])


def _genuine_commit(editing, command, label, key):
    return runtime_occurrence.commit(
        editing, _genuine_request(editing, [command], label), key,
    )


@pytest.fixture
def same_alias_realm():
    document, old, new, verified = inputs()
    new['asset'] = old['asset']
    verified[1]['selection']['asset'] = old['asset']
    document['shot_revisions'][0]['payload']['unknown']['keep'] = True
    document['shot_revisions'][0]['content_digest'] = _digest(document['shot_revisions'][0]['payload'])
    document['dependency_manifest'] = _manifest(document['shot_revisions'],
        document['internal_timeline_revisions'], document['dependency_manifest']['media'], [])
    realm = MediaRealm(document, verified)
    command = capture_occurrence_swap_media(bundle_from_document(document), TARGET, old, new,
                                            verified_media=verified)
    created = realm.call([command])
    return realm, document, created


@pytest.mark.integration
def test_same_alias_materialized_whole_payload_read_allows_exact_undo_redo(genuine_same_alias_realm):
    editing = genuine_same_alias_realm
    _genuine_preview(editing)
    document = _genuine_same_alias_baseline(editing)
    command = _genuine_capture(editing, 'swap', same_alias=True, document=document)
    created = _genuine_commit(editing, command, 'same-alias', 'same-alias')
    undo = _genuine_commit(
        editing, inverse_call([created['receipt']['receipt_id']]), 'same-alias-undo', 'same-alias-undo',
    )
    assert undo['data']['canonical_document'] == document
    redo = _genuine_commit(
        editing, inverse_call([undo['receipt']['receipt_id']]), 'same-alias-redo', 'same-alias-redo',
    )
    assert redo['data']['canonical_document'] == created['data']['canonical_document']


@pytest.mark.parametrize('damage', ['opaque-number', 'item-identity', 'compiler-link', 'source'])
def test_same_alias_materialized_payload_still_guards_all_other_input(same_alias_realm, damage):
    from tests.editing_conformance.test_occurrence_media_manifest_inverse import accepted_records

    realm, _, created = same_alias_realm
    records = accepted_records(realm, realm.entries[-1])
    bundle = bundle_from_document(created['data']['canonical_document'])
    shot = State(bundle).occurrence(OWNER)['shot_id']
    payload = bundle['shots'][shot]['payload']
    if damage == 'opaque-number':
        payload['unknown']['keep'] = 1
    elif damage == 'item-identity':
        payload['items'][0]['item_id'] = 'unverified-item'
    elif damage == 'compiler-link':
        payload['internal_timeline_revision_id'] = 'unverified-link'
    else:
        bundle['source_mapping']['shots'][shot]['shot_id'] = 'unverified-shot'
    before = copy.deepcopy((bundle, realm.head, realm.entries))
    with pytest.raises(CommandError):
        inverse(bundle, records)
    assert (bundle, realm.head, realm.entries) == before


@pytest.mark.parametrize('operation', ['add', 'swap'])
@pytest.mark.integration
def test_provider_exact_reload_inverse_redo_and_retired_asset_reservation(
        operation, genuine_video_editing, monkeypatch):
    editing = genuine_video_editing
    _genuine_preview(editing)
    document = copy.deepcopy(editing.closures[0])
    command = _genuine_capture(editing, operation)
    request = _genuine_request(editing, [command], 'provider-exact')
    created = runtime_occurrence.commit(editing, request, 'provider-exact')
    undo_request = _genuine_request(
        editing, [inverse_call([created['receipt']['receipt_id']])], 'provider-exact-undo',
    )
    undo = runtime_occurrence.commit(editing, undo_request, 'provider-exact-undo')
    assert undo['data']['canonical_document'] == document
    history = editing.service.list_timeline_commits(
        editing.project, 'film', authorize=editing.authorize,
    )
    # A fresh request cannot reuse an alias retired by accepted history.
    from runtime_protocol.errors import RuntimeErrorBase
    with pytest.raises(RuntimeErrorBase) as error:
        _genuine_commit(editing, command, 'provider-exact-fresh', 'provider-exact-fresh')
    assert error.value.details['reason'] == 'identity_collision'
    assert editing.service.list_timeline_commits(
        editing.project, 'film', authorize=editing.authorize,
    ) == history
    root = editing.root
    editing.service.close()
    from runtime_protocol.service import RuntimeService
    from runtime_protocol.timeline_commit_provider import load_timeline_commit_provider
    editing.service = RuntimeService(root)
    editing.service.register_timeline_commit_provider(load_timeline_commit_provider('astrid'))
    with monkeypatch.context() as retry:
        from astrid.sdk.timeline import commands
        retry.setattr(commands, 'execute', lambda *args, **kwargs: pytest.fail('exact retry executed semantics'))
        assert runtime_occurrence.commit(editing, request, 'provider-exact') == created
        assert runtime_occurrence.commit(editing, undo_request, 'provider-exact-undo') == undo
    redo = _genuine_commit(
        editing, inverse_call([undo['receipt']['receipt_id']]), 'provider-exact-redo', 'provider-exact-redo',
    )
    assert redo['data']['canonical_document'] == created['data']['canonical_document']
    again = _genuine_commit(
        editing, inverse_call([redo['receipt']['receipt_id']]), 'provider-exact-again', 'provider-exact-again',
    )
    assert again['data']['canonical_document'] == document


@pytest.mark.parametrize('operation', ['add', 'swap'])
def test_missing_port_pin_refuses_before_acceptance(operation):
    document, _, command, verified = captured(operation)
    realm = MediaRealm(document, verified[:-1])
    head, history = copy.deepcopy(realm.head), copy.deepcopy(realm.entries)
    with pytest.raises(CommitRejected, match='dependency_unavailable'):
        realm.call([command])
    assert (realm.head, realm.entries) == (head, history)
    assert not realm.prepared


@pytest.mark.parametrize('scope', ['local-alias', 'foreign-ref', 'shot-ref'])
def test_asset_removal_checks_declared_references_across_owners(scope):
    _, bundle, command, verified = captured('swap')
    plan = execute(bundle, [command], verified_media=verified)
    ref = OWNER + '/asset:reserved-new'
    if scope == 'local-alias':
        State(plan.bundle).entity(OWNER + '/clip:clip-b')['asset'] = 'reserved-new'
    elif scope == 'foreign-ref':
        State(plan.bundle).entity('occurrence:occ-b/clip:clip-b')['asset_ref'] = ref
    else:
        plan.bundle['shots'][plan.bundle['placements'][1]['shot_id']]['payload']['asset_ref'] = ref
    with pytest.raises(CommandError, match='surviving dependents'):
        inverse(plan.bundle, plan.records)


@pytest.mark.parametrize('entry', [None, False, [], {'media_id': NEW, 'type': 'video'}])
def test_occupied_asset_alias_is_never_replaced(entry):
    _, bundle, command, verified = captured('swap')
    State(bundle).config(OWNER)['registry']['assets']['reserved-new'] = copy.deepcopy(entry)
    before = copy.deepcopy(bundle)
    plain = {key: command[key] for key in ('schema_version', 'op', 'version', 'select', 'args', 'intent')}
    with pytest.raises(CommandError):
        execute(bundle, [plain], verified_media=verified)
    assert bundle == before


def test_noninteger_positive_speed_preserves_source_units_and_half_open_boundary():
    _, bundle, command, verified = captured('add', 'video')
    plain = {key: command[key] for key in ('schema_version', 'op', 'version', 'select', 'args', 'intent')}
    plain['args']['row'].update(at=7, hold=4.5, speed=1.5, **{'from': 1.5, 'to': 6})
    plan = execute(bundle, [plain], verified_media=verified)
    row = State(plan.bundle).entity(OWNER + '/clip:reserved-child')
    assert (row['at'], row['hold'], row['speed'], row['from'], row['to']) == (7, 4.5, 1.5, 1.5, 6)
    assert inverse(plan.bundle, plan.records).bundle == bundle
    plain['args']['row']['at'] = 7.001
    with pytest.raises(CommandError, match='exceeds occurrence'):
        execute(bundle, [plain], verified_media=verified)
