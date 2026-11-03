"""D20 shared-placement migration through the real provider and current Runtime."""
from __future__ import annotations

import copy
import inspect
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from astrid.sdk.timeline._command_state import CommandError
from astrid.sdk.timeline.commit import bundle_from_document
from astrid.sdk.timeline.native_occurrence_shared import capture_occurrence_place_shared
from tests.editing_conformance.test_doc_patch_v2 import step, submit
from tests.editing_conformance.test_recipe_occurrence_fork import fork_document
from tests.helpers.runtime import use_runtime
from tests.sdk.test_timeline_commit_provider import CommitRejected, DetachedRealm, inverse
from tests.sdk.test_timeline_commit_provider import runtime_abi as runtime_abi

OWNER = 'occurrence:occ-0'


def shared(bundle, owner=OWNER, identity='f2-occurrence-copy'):
    return capture_occurrence_place_shared(bundle, owner, identity, on=12)


@pytest.mark.parametrize('count', [1, 2])
def test_provider_full_closure_preview_retry_inverse(count):
    before = fork_document(count=count)
    realm = DetachedRealm(before)
    call = shared(bundle_from_document(before))
    preview = submit(realm, call, reads=[], dry_run=True)
    assert preview['receipt'] is None and preview['data']['would_status'] == 'accepted'
    result = submit(realm, call, reads=[], label='Shared placement',
                    steps=[step('shared', 0, call)], source={'via': 'app'},
                    rationale='D20 old shared writer migration', group_id='popup')
    after = result['data']['canonical_document']
    assert_shared(before, after)
    assert realm.prepared[-1]['records']['commands'][0]['created'] == [
        {'ref': 'occurrence:f2-occurrence-copy', 'kind': 'occurrence', 'owner': 'parent'}]
    undone = submit(realm, inverse([result['receipt']['receipt_id']]), reads=[])
    assert undone['data']['canonical_document'] == before
    redone = submit(realm, inverse([undone['receipt']['receipt_id']]), reads=[])
    assert redone['data']['canonical_document'] == after


def assert_shared(before, after):
    from astrid.core.timeline.authoring_bundle import _rederive_root_identity

    old = before['parent_revision']['payload']['occurrences']
    rows = after['parent_revision']['payload']['occurrences']
    assert rows[:-1] == old
    assert rows[-1]['occurrence_id'] == 'f2-occurrence-copy'
    assert rows[-1]['placement']['start_ms'] == 12000
    for key in ('shot_id', 'shot_revision_id'):
        assert rows[-1][key] == old[0][key]
    expected = copy.deepcopy(old[0])
    expected['occurrence_id'] = 'f2-occurrence-copy'
    expected['placement']['start_ms'] = 12000
    _rederive_root_identity(expected, before['project_id'], before['timeline_id'],
                            expected['shot_revision_id'])
    assert rows[-1] == expected
    for key in ('shot_revisions', 'internal_timeline_revisions', 'dependency_manifest'):
        assert after[key] == before[key]


@pytest.mark.parametrize('mutation', ['source', 'membership', 'order', 'occupied', 'foreign', 'forged'])
def test_provider_guard_no_write(mutation):
    before = fork_document()
    realm = DetachedRealm(before)
    bundle = bundle_from_document(before)
    call = shared(bundle)
    if mutation == 'source':
        call['reads'][3]['state']['value'] = 'forged-shot'
    elif mutation == 'membership':
        call['selections'][0]['members'].append('occurrence:foreign')
    elif mutation == 'order':
        call['selections'][0]['order'].reverse()
    elif mutation == 'occupied':
        call['args']['occurrence_id'] = 'occ-1'
    elif mutation == 'foreign':
        call['select'] = 'occurrence:foreign'
    else:
        call['args']['shot_id'] = 'forged-shot'
    head, entries = copy.deepcopy(realm.head), copy.deepcopy(realm.entries)
    with pytest.raises((CommitRejected, CommandError)):
        submit(realm, call, reads=[])
    assert realm.head == head and realm.entries == entries
    assert bundle == bundle_from_document(before)


@pytest.mark.integration
def test_current_runtime_public_commit_retry_undo_and_negatives(tmp_path, monkeypatch):
    script = '''
import sys
from pathlib import Path
import pytest
from tests.helpers.runtime import use_runtime
use_runtime()
from tests.editing_conformance.test_occurrence_shared_d20 import _prove_runtime
with pytest.MonkeyPatch.context() as patch:
    _prove_runtime(Path(sys.argv[1]), patch)
'''
    result = subprocess.run([sys.executable, '-c', script, str(tmp_path)],
                            env=os.environ, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


def _prove_runtime(tmp_path, monkeypatch):
    runtime = use_runtime()
    monkeypatch.syspath_prepend(str(runtime / 'tests'))
    from runtime_protocol.auth import CredentialStore
    from runtime_protocol.errors import RuntimeErrorBase
    from runtime_protocol.service import RuntimeService
    from runtime_protocol.store import RealmStore
    from runtime_protocol.timeline_commit_provider import load_timeline_commit_provider
    from test_timeline_commit_read_basis import _canonical_from_public_reads

    from astrid.sdk.timeline.capture import capture_track_create
    from astrid.sdk.timeline.native_occurrence_create import capture_occurrence_create
    from astrid.sdk.timeline.native_occurrence_media_commands import capture_occurrence_add_media
    from tests.editing_conformance.test_occurrence_hold_current_runtime import _import_image

    root = tmp_path / 'realm'
    RealmStore.initialize(root).close()
    service = RuntimeService(root)
    credentials = CredentialStore(tmp_path / 'credentials')
    token, _ = credentials.provision('d20-editor', ['projects:read', 'projects:write'])

    def authorize():
        return credentials.require(token, 'projects:write')

    try:
        project = service.create_project({'slug': 'd20', 'name': 'D20'})['id']
        service.register_timeline_commit_provider(load_timeline_commit_provider('astrid'))
        service.create_timeline(project, 'film', idempotency_key='bootstrap')

        def history(timeline='film'):
            return service.list_timeline_commits(project, timeline, authorize=authorize)

        def document():
            return _canonical_from_public_reads(service, project, 'film', history())

        def body(call, key, timeline='film'):
            head = service.get_project_timeline(project, timeline)
            return {'api_version': 1, 'project_id': project, 'timeline_id': timeline,
                    'base_revision_id': head['head_revision_id'],
                    'base_publication_id': head['head_publication_id'], 'commands': [call],
                    'label': key, 'steps': [step(key, 0, call, reads=[])],
                    'merge': 'fields', 'reads': [], 'dry_run': False,
                    'source': {'via': 'app'}, 'rationale': 'D20 shared migration', 'group_id': key}

        def commit(request, key):
            timeline = request['timeline_id']
            return service.commit_timeline(project, timeline, request, idempotency_key=key,
                publisher='d20-editor', invocation_path=f'/v1/projects/{project}/timelines/{timeline}/commit',
                authorize=authorize)

        def setup(call, key):
            return commit(body(call, key), key)

        setup(capture_track_create(bundle_from_document(document()), 'parent',
                                   {'id': 'picture', 'kind': 'visual'}), 'parent-track')
        setup(capture_occurrence_create(bundle_from_document(document()), 'occ-0', 'source-shot',
              name='Source', track='picture', on=0, duration=5), 'birth')
        setup(capture_track_create(bundle_from_document(document()), OWNER,
                                   {'id': 'picture', 'kind': 'visual'}), 'child-track')
        media, verified = _import_image(service, project, 'd20-image')
        add = capture_occurrence_add_media(bundle_from_document(document()), OWNER,
              {'id': 'source-image', 'clipType': 'hold', 'asset': media['asset'],
               'track': 'picture', 'at': 0, 'hold': 2, 'opaque': [None, False]},
              media, verified_media=[verified])
        setup(add, 'image')
        before = document()
        call = shared(bundle_from_document(before))
        # Raw public envelope: authoritative source observations come from the provider.
        raw = {key: copy.deepcopy(call[key]) for key in
               ('schema_version', 'op', 'version', 'select', 'args', 'intent', 'created')}
        request = body(raw, 'shared')
        preview_request = {**copy.deepcopy(request), 'dry_run': True}
        snapshot = tuple(service.store.conn.iterdump())
        preview = commit(preview_request, 'preview')
        assert preview['receipt'] is None and preview['data']['would_status'] == 'accepted'
        assert tuple(service.store.conn.iterdump()) == snapshot
        result = commit(request, 'shared')
        after = document()
        assert_shared(before, after)
        assert result['data']['canonical_document'] == after
        snapshot = tuple(service.store.conn.iterdump())
        assert commit(request, 'shared') == result
        assert tuple(service.store.conn.iterdump()) == snapshot
        undone = setup(inverse([result['receipt']['receipt_id']]), 'undo')
        assert undone['data']['canonical_document'] == before
        assert document() == before
        redone = setup(inverse([undone['receipt']['receipt_id']]), 'redo')
        assert redone['data']['canonical_document'] == after
        assert setup(inverse([redone['receipt']['receipt_id']]), 'undo-redo')['data']['canonical_document'] == before

        def refused(request, key):
            snapshot = tuple(service.store.conn.iterdump())
            with pytest.raises(RuntimeErrorBase) as error:
                commit(request, key)
            assert error.value.status in (409, 422)
            assert tuple(service.store.conn.iterdump()) == snapshot
            return {'status': error.value.status, 'details': error.value.details}

        negatives = {}
        for kind in ('occupied', 'foreign', 'forged'):
            bad = copy.deepcopy(raw)
            if kind == 'occupied':
                bad['args']['occurrence_id'] = 'occ-0'
            elif kind == 'foreign':
                bad['select'] = 'occurrence:foreign'
            else:
                bad['args']['shot_revision_id'] = 'forged-revision'
            negatives[kind] = refused(body(bad, kind), kind)
        service.create_timeline(project, 'other-film', idempotency_key='other-bootstrap')
        negatives['foreign-timeline'] = refused(body(raw, 'foreign-timeline', 'other-film'), 'foreign-timeline')
        negatives['retired'] = refused(body(raw, 'retired'), 'retired')
        stale = body(capture_occurrence_place_shared(bundle_from_document(document()), OWNER,
                     'stale-copy', on=12), 'stale')
        setup({'schema_version': 1, 'op': 'shot.set', 'version': '1', 'select': OWNER + '/shot',
               'args': {'values': {'name': 'Changed'}, 'clear': []}}, 'change-source')
        negatives['stale'] = refused(stale, 'stale')
        assert negatives['stale']['details']['reason'] == 'dependency_changed'
        final_document = document()
        service.close()
        service = RuntimeService(root)
        service.register_timeline_commit_provider(load_timeline_commit_provider('astrid'))
        snapshot = tuple(service.store.conn.iterdump())
        assert commit(request, 'shared') == result
        assert tuple(service.store.conn.iterdump()) == snapshot
        assert document() == final_document
        if os.environ.get('D20_EVIDENCE_DIR'):
            import runtime_protocol.service as runtime_service

            import astrid.sdk.timeline.commit as agent_commit
            destination = Path(os.environ['D20_EVIDENCE_DIR'])
            destination.mkdir(parents=True, exist_ok=True)
            (destination / 'public-shared.json').write_text(json.dumps({
                'before': before, 'request': request, 'preview': preview, 'result': result,
                'after': after, 'undo': undone, 'redo': redone,
                'negatives': negatives, 'history': history(),
                'origins': {'agent': inspect.getsourcefile(agent_commit),
                            'runtime': inspect.getsourcefile(runtime_service)},
            }, indent=2, sort_keys=True))
    finally:
        service.close()
