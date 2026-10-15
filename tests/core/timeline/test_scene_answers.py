"""Authoritative-answer projection tests; fake owner replies are not render proof."""
import copy
import json
import os
from pathlib import Path
import sys
import time
import pytest
from astrid.core.timeline import scene_answers as scene
from astrid.core.timeline.roles import role_of
from astrid.core.execution import remotion_evaluation as evaluation
from astrid.core.execution.remotion_frame_owner import FrameOwnerError
from astrid.packs.rendering.actions.timeline_visualize.overlays.labels import labels_for, addresses_at

CONFIG = {'tracks': [{'id': 'plate', 'kind': 'visual'}], 'clips': [
    {'id': 'p', 'clipType': 'test-card', 'track': 'plate', 'at': 0, 'hold': 2,
     'app': {'cut': 'c01', 'layer': 'picture'}}]}
CANVAS = {'width': 200, 'height': 100}

def answer(frame=0, **changes):
    value = {'frame': frame, 'coverage': 'UNKNOWN', 'content': None,
        'findings': [{'code': 'unknown-component-bounds', 'clipId': 'p', 'reason': 'No declaration'}],
        'layers': [{'clipId': 'p', 'track': 'plate', 'z': 0, 'role': 'picture', 'visible': 'unknown',
                    'opacity': None, 'bounds': {'status': 'unknown', 'reason': 'No declaration'},
                    'fullFrame': False, 'findings': [{'code': 'unknown-component-bounds', 'clipId': 'p', 'reason': 'No declaration'}]}]}
    value.update(changes)
    return value

@pytest.fixture
def owner(tmp_path, monkeypatch):
    # This injected transport models trusted-local construction.
    monkeypatch.delenv('ASTRID_INTERNAL_INVOCATION', raising=False)
    calls = []
    project = tmp_path / 'selected-project'
    component = tmp_path / 'astrid/packs/example/elements/effects/card/component.tsx'
    package = project / 'node_modules/@banodoco/timeline-composition/package.json'
    font = project / 'public/fonts/font.woff2'
    for path in (component, package, font):
        path.parent.mkdir(parents=True, exist_ok=True)
    component.write_text('one')
    package.write_text('{"version":"1"}')
    font.write_bytes(b'font-one')
    from astrid.packs.rendering.actions.timeline_visualize import composed_frame
    monkeypatch.setattr(composed_frame, '_checkout_root', lambda: tmp_path)
    monkeypatch.setenv('ASTRID_REMOTION_PROJECT_DIR', str(project))
    closure = composed_frame.renderer_environment_identity
    class Transport:
        def request(self, identity, request, parser):
            props = json.loads(Path(request['propsPath']).read_text())
            assert request['compositionId'] == 'AstridTrackEvaluation'
            assert set(props) == {'scene', 'frames'}
            calls.append((identity, props))
            return {'ok': True, 'evaluation': [answer(frame) for frame in props['frames']]}
    transport = Transport()
    monkeypatch.setattr(evaluation, 'host_remotion_client', lambda: transport)
    client = evaluation.RemotionEvaluationClient(project_dir=project, node_executable=sys.executable,
        attempt_root=tmp_path, renderer_closure=closure)
    previous = scene.use_client(client)
    yield client, calls, (component, package, font)
    scene.use_client(previous)


def test_scene_cache_repeat_under_100ms_real_content_invalidation_and_unrelated_edit(owner):
    client, calls, files = owner
    first = scene.frames_at(CONFIG, [0, 3], 10, CANVAS)
    start = time.perf_counter(); second = scene.frames_at(CONFIG, [0, 3], 10, CANVAS)
    elapsed = time.perf_counter() - start
    print(f'unchanged-repeat-seconds={elapsed:.9f}')
    assert elapsed < .1 and second == first and len(calls) == 1
    config = copy.deepcopy(CONFIG); config['notes'] = 'unrelated edit'
    config['assets'] = {'assets': {'unreferenced': {'file': 'other.jpg', 'type': 'image/jpeg'}}}
    scene.frames_at(config, [0, 3], 10, CANVAS)
    assert len(calls) == 1
    for index, file in enumerate(files):
        before = file.stat()
        file.write_bytes(bytes([65 + index]) * before.st_size)
        os.utime(file, ns=(before.st_atime_ns, before.st_mtime_ns))
        scene.frames_at(CONFIG, [0, 3], 10, CANVAS)
    assert len(calls) == 4
    first[0]['layers'].clear()
    assert scene.frames_at(CONFIG, [0, 3], 10, CANVAS)[0]['layers']
    config['clips'][0]['opacity'] = .4
    scene.frames_at(config, [0, 3], 10, CANVAS)
    assert len(calls) == 5


def test_labels_and_unknown_coverage_are_exact_shared_answers(owner):
    layers = labels_for(CONFIG, 3, 10, CANVAS)
    assert layers[0].address == 'c01.picture'
    assert layers[0].opacity is None and layers[0].bounds is None
    assert addresses_at(CONFIG, 3, 10, CANVAS) == ['c01.picture']
    spans = scene.coverage(CONFIG, 10, CANVAS, {}, start_frame=0, end_frame=241)
    assert len(spans) == 1 and spans[0].cls == 'UNKNOWN'
    assert spans[0].findings == ('unknown-component-bounds p: No declaration',)
    assert spans[0].end_frame == 241
    calls = owner[1]
    assert [len(props['frames']) for _, props in calls][-3:] == [120, 120, 1]


def test_owner_unavailable_is_explicit_unknown_and_pure_roles_still_work(monkeypatch):
    scene.use_client(None)
    monkeypatch.setattr('astrid.core.execution.remotion_host_session.host_remotion_client', lambda: None)
    spans = scene.coverage(CONFIG, 10, CANVAS, {}, start_frame=0, end_frame=20)
    assert spans[0].cls == 'UNKNOWN' and 'frame_owner_unavailable' in spans[0].findings[0]
    assert role_of({'clipType': 'x'}, 'plate', {}) == 'picture'
    meta = {'x': {'coverage': 'picture', 'background_unless': ['asset', 'params.src']}}
    assert role_of({'clipType': 'x'}, 'v1', meta) == 'background'
    assert role_of({'clipType': 'x', 'params': {'src': 'a'}}, 'v1', meta) == 'picture'
    with pytest.raises(FrameOwnerError, match='frame_owner_unavailable'):
        labels_for(CONFIG, 0, 10, CANVAS)


def test_missing_visibility_fields_refuse_instead_of_being_cached(owner, monkeypatch):
    client = owner[0]
    class Bad:
        def request(self, identity, request, parser):
            return {'ok': True, 'evaluation': [{'frame': 0, 'layers': []}]}
    monkeypatch.setattr(client._client, 'request', Bad().request)
    with pytest.raises(FrameOwnerError, match='evaluation_response_invalid'):
        scene.frames_at(CONFIG, [0], 10, CANVAS)
    assert not client._cache


def test_declaration_role_cache_reloads_changed_metadata_without_renderer(tmp_path, monkeypatch):
    from astrid.core.timeline import roles
    manifest = tmp_path / 'example/elements/effects/card/element.yaml'
    manifest.parent.mkdir(parents=True)
    manifest.write_text('metadata: {coverage: background}\n')
    monkeypatch.setattr(roles, '_PACKS_ROOT', tmp_path)
    monkeypatch.setattr(roles, '_COVERAGE_CACHE', {})
    assert roles.role_of({'clipType': 'card'}, 'plate', roles.element_coverage(['card'])) == 'background'
    manifest.write_text('metadata: {coverage: picture}\n')
    assert roles.role_of({'clipType': 'card'}, 'plate', roles.element_coverage(['card'])) == 'picture'


def test_optional_filmstrip_addresses_survive_offline_with_named_unknown(monkeypatch):
    from astrid.packs.rendering.actions.timeline_visualize.filmstrip_cards import attach_frame_addresses
    scene.use_client(None)
    monkeypatch.setattr('astrid.core.execution.remotion_host_session.host_remotion_client', lambda: None)
    receipt = {'cards': [{}]}
    attach_frame_addresses(receipt, {'config': CONFIG, 'fps_rational': [10, 1]}, [{'frame': 0}])
    row = receipt['cards'][0]
    assert row['addresses'] == [] and row['address_status'] == 'unknown'
    assert row['address_findings'] == [{'code': 'evaluation-unavailable', 'reason': 'frame_owner_unavailable'}]
    with pytest.raises(FrameOwnerError, match='frame_owner_unavailable'):
        labels_for(CONFIG, 0, 10, CANVAS)
