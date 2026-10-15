"""H-DATA: disposable JSON only; no runtime, film, media or provider access."""
from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.core.timeline import astrid_ns as ns
from astrid.sdk import timeline_intent as intent
from astrid.sdk.timeline import store
from astrid.sdk.timeline.checkout import Checkout
from astrid.sdk.timeline.doc import _serialize, _strip_derived

TINY = Path(__file__).resolve().parents[1] / 'fixtures/timeline_editing/tiny_publishable.json'


def collision_bundle():
    """Repository tiny JSON with invented opaque values, including parent/base clips."""
    bundle = json.loads(TINY.read_text())
    opaque = {'_vendor': {'_index': [None, {'_timeline': 'opaque'}]},
              'astrid': {'why': 'current', '_legacy': {'why': 'old', '_opaque': [1, None]}}}
    parent_clip = {'id': 'parent-opaque', 'clipType': 'media', 'at': 0, 'hold': 1,
                   'track': 'music', 'app': copy.deepcopy(opaque)}
    bundle['parent']['clips'] = [parent_clip]
    bundle['base_parent_payload']['clips'] = [copy.deepcopy(parent_clip)]
    for shot in bundle['shots'].values():
        for field in ('internal_timeline', 'base_internal_timeline'):
            shot[field]['clips'][0]['app'] = copy.deepcopy(opaque)
    bundle['parent']['config']['app'] = {'astrid': {'_legacy': {'chapters': 'opaque'}}, '_private': [0]}
    bundle['base_parent_payload']['config'] = copy.deepcopy(bundle['parent']['config'])
    return bundle


def assert_opaque(bundle, expected):
    for field in ('parent', 'base_parent_payload'):
        assert bundle[field]['clips'][0]['app'] == expected[field]['clips'][0]['app']
        app = bundle[field]['config']['app']
        assert app['_private'] == expected[field]['config']['app']['_private']
        assert app['astrid']['_legacy'] == expected[field]['config']['app']['astrid']['_legacy']
    for sid in expected['shots']:
        for field in ('internal_timeline', 'base_internal_timeline'):
            assert bundle['shots'][sid][field]['clips'][0]['app'] == expected['shots'][sid][field]['clips'][0]['app']


def test_collision_lifecycle(tmp_path, monkeypatch):
    monkeypatch.setenv('BANODOCO_LOCAL_DATA_ROOT', str(tmp_path))
    # Pixel measurement is a media-service concern, outside JSON preservation.
    monkeypatch.setattr(Checkout, '_native_outline_integrity', lambda *a: None)
    path = tmp_path / 'drafts' / 'disposable.json'
    path.parent.mkdir()
    expected = collision_bundle()
    path.write_text(_serialize(expected))
    tl = Checkout.load(path)
    assert_opaque(tl.document(), expected)
    tl.save(quiet=True)
    assert_opaque(Checkout.load(path).document(), expected)

    # Concurrent working-copy merge uses the real save, merge and undo paths.
    other = Checkout.load(path)
    tl.bundle['shots']['A']['internal_timeline']['clips'][1]['params']['x'] = 42
    other.bundle['shots']['B']['internal_timeline']['clips'][1]['params']['text'] = 'Other'
    other.save(quiet=True)
    tl.save(quiet=True)
    assert tl.merged
    assert_opaque(tl.document(), expected)
    tl.undo()
    assert_opaque(tl.document(), expected)

    # Only the runtime transport is fake. Exercise Checkout.publish and compilation,
    # real store merge, then reset/reopen from the disposable fake head.
    state = {'head': copy.deepcopy(expected)}
    state['head']['base_parent']['revision_id'] = 'concurrent-disposable-head'
    monkeypatch.setattr(store, 'fetch_bundle', lambda *a, **k: copy.deepcopy(state['head']))
    client = SimpleNamespace(timelines=SimpleNamespace(open_composition=lambda *a, **k:
        SimpleNamespace(ok=True, data={'summary': {'head_revision_id': state['head']['base_parent']['revision_id']}})))
    import astrid.sdk.authoring_bundle as authoring
    import astrid.sdk.autobootstrap as bootstrap
    import astrid.sdk.workspace_client as workspace
    monkeypatch.setattr(bootstrap, 'ensure_runtime', lambda **k: {'endpoint': 'disposable', 'credential_file': str(tmp_path/'unused')})
    monkeypatch.setattr(workspace, 'resolve_runtime_connection', lambda *a: ('disposable', 'fake'))
    publications = []

    class Transport:
        def publish_parent_composition(self, project, timeline, publication, **kwargs):
            publications.append(copy.deepcopy(publication))
            assert publication['parent_composition']['clips'][0]['app'] == expected['parent']['clips'][0]['app']
            return {'new_head': 'published-disposable-head'}

    monkeypatch.setattr(workspace, 'WorkspaceClient', lambda *a: Transport())
    real_publish = authoring.publish_authoring_candidate

    def publish(candidate, writer, **kwargs):
        assert_opaque(candidate, expected)
        result = real_publish(candidate, writer, **kwargs)
        state['head'] = copy.deepcopy(candidate)
        state['head']['base_parent']['revision_id'] = 'published-disposable-head'
        return result

    monkeypatch.setattr(authoring, 'publish_authoring_candidate', publish)
    tl.bundle['shots']['A']['internal_timeline']['clips'][1]['params']['x'] = 43
    receipt = tl.publish('H-DATA disposable fixture', client=client)
    assert receipt['new_head'] == 'published-disposable-head'
    assert len(publications) == 1
    assert_opaque(Checkout.open('disposable', 'disposable', client=client).document(), expected)
    assert_opaque(Checkout.load(path).document(), expected)


def test_migration_parent_collisions_and_exact_byte_idempotence():
    bundle = collision_bundle()
    for parent in ('parent', 'base_parent_payload'):
        bundle[parent]['clips'][0]['app']['why'] = 'flat collision'
    for shot in bundle['shots'].values():
        for field in ('internal_timeline', 'base_internal_timeline'):
            shot[field]['clips'][0]['app']['why'] = 'flat collision'
    intent.migrate(bundle)
    for parent in ('parent', 'base_parent_payload'):
        assert bundle[parent]['clips'][0]['app']['astrid']['_legacy']['why#2'] == 'flat collision'
    once = _serialize(bundle).encode()
    intent.migrate(bundle)
    assert _serialize(bundle).encode() == once
    assert ns.migrate_bundle(bundle) is False


@pytest.mark.parametrize('current,legacy', [(True, 1), (1, 1.0), ({'x': True}, {'x': 1})])
def test_migration_keeps_json_type_collisions(current, legacy):
    clip = {'app': {'why': legacy, 'astrid': {'why': current, '_legacy': {'why': current}}}}
    assert ns.migrate_clip(clip)
    assert type(clip['app']['astrid']['_legacy']['why#2']) is type(legacy)
    assert clip['app']['astrid']['_legacy']['why#2'] == legacy
    before = _serialize(clip).encode()
    assert not ns.migrate_clip(clip)
    assert _serialize(clip).encode() == before


@pytest.mark.parametrize('value', [None, [], 'malformed', 7, False])
def test_migration_preserves_malformed_namespaces_and_legacy(value):
    for place in ('app', 'astrid', '_legacy'):
        if place == 'app':
            clip = {'app': copy.deepcopy(value)}
            config = {'app': copy.deepcopy(value), 'chapters': ['legacy']}
        elif place == 'astrid':
            clip = {'app': {'astrid': copy.deepcopy(value), 'why': 'flat'}}
            config = {'app': {'astrid': copy.deepcopy(value)}, 'chapters': ['legacy']}
        else:
            clip = {'app': {'astrid': {'why': 'current', '_legacy': copy.deepcopy(value)}, 'why': 'flat'}}
            config = {'app': {'astrid': {'chapters': ['current'], '_legacy': copy.deepcopy(value)}}, 'chapters': ['legacy']}
        bundle = {'parent': {'config': config, 'clips': [clip]}, 'shots': {}}
        before = copy.deepcopy(bundle)
        intent.migrate(bundle)
        assert bundle == before
        intent.migrate(bundle)
        assert bundle == before


def test_strip_only_owned_paths_and_preserve_malformed_helpers():
    doc = collision_bundle()
    doc['_index'] = {'vendor': 'collision'}
    doc['_root'] = {'_timeline': 'opaque', '_index': None}
    clip = doc['shots']['A']['internal_timeline']['clips'][1]
    clip['_timeline'] = {'start': 1, 'end': 2}
    clip['params']['_timeline'] = {'start': 1, 'end': 2}
    parent = doc['parent']['clips'][0]
    parent['_timeline'] = {'start': 0, 'end': 1}
    stripped = _strip_derived(doc)
    assert '_timeline' not in stripped['shots']['A']['internal_timeline']['clips'][1]
    assert '_timeline' not in stripped['parent']['clips'][0]
    assert stripped['_index'] == doc['_index']
    assert stripped['_root'] == doc['_root']
    assert stripped['shots']['A']['internal_timeline']['clips'][1]['params'] == clip['params']
    clip['_timeline'] = ['vendor', None]
    path_values = _strip_derived(doc)
    assert path_values['shots']['A']['internal_timeline']['clips'][1]['_timeline'] == ['vendor', None]


def test_index_ownership_requires_our_note_and_valid_numeric_fields():
    from astrid.sdk.timeline._common import EDITABLE_NOTE

    index = {'note': EDITABLE_NOTE, 'fps': 30, 'duration_s': 1, 'cuts': [], 'words': []}
    assert _strip_derived({'_index': index}) == {}
    for patch in ({'note': 'vendor'}, {'fps': {}}, {'duration_s': None}, {'fps': True}):
        collision = {'_index': {**index, **patch}}
        assert _strip_derived(collision) == collision


def test_malformed_helpers_survive_save_and_reopen(tmp_path, monkeypatch):
    monkeypatch.setenv('BANODOCO_LOCAL_DATA_ROOT', str(tmp_path))
    bundle = collision_bundle()
    bundle['_index'] = ['vendor collision']
    clip = bundle['shots']['A']['internal_timeline']['clips'][0]
    clip['_timeline'] = {'start': 'malformed', 'end': None, '_opaque': 7}
    tl = Checkout(bundle)
    path = tl.save(tmp_path / 'opaque.json', quiet=True)
    saved = json.loads(path.read_text())
    assert saved['_index'] == ['vendor collision']
    assert saved['shots']['A']['internal_timeline']['clips'][0]['_timeline'] == clip['_timeline']
    assert Checkout.load(path).document()['_index'] == ['vendor collision']


@pytest.mark.parametrize('value', [None, [], 'malformed', 7, False])
def test_intent_writers_refuse_malformed_containers_without_loss(value):
    for owner in ({'app': value}, {'app': {'astrid': value}}):
        before = copy.deepcopy(owner)
        with pytest.raises(ValueError, match='cannot write Astrid intent'):
            intent.set_why(owner, 'new')
        assert owner == before
