import hashlib
from copy import deepcopy
from types import SimpleNamespace

import pytest

from astrid.sdk.timeline_filmstrip import build_filmstrip_snapshot, matching_composed_render, prepare_filmstrip
from astrid.sdk.exceptions import CapabilityValidationError

TEXT = b'Take the blue pill.'
DIGEST = 'sha256:' + hashlib.sha256(TEXT).hexdigest()
VIDEO = 'sha256:' + 'a' * 64
MANAGED_VIDEO_REFERENCE = 'runtime-managed-video-reference'
DIRECT_RENDER_RUN = '084ba36177a5400f9c4e844444931d25'
DIRECT_RENDER_TASK = '36e4f00dae80429b8f48630f702ea816'
DIRECT_TIMELINE = 'c9c685ea84fe4f3c880d55e9e28b8f02'


def envelope():
    return {'authority_context': {'timeline_id': 'tl', 'timeline_slug': 'cut', 'config_version': 1,
        'expansion': {'children': [], 'shots': [{'shot_id': 'sh', 'name': 'Blue', 'text_bindings': [
            {'binding_id': 'binding', 'head': 1, 'media_id': DIGEST, 'content_hash': DIGEST,
             'byte_size': len(TEXT), 'kind': 'voiceover_script'}]}]}},
        'inputs': {'review_context': {'shots': [{'shot_id': 'sh', 'at': 0, 'hold': 3}]},
            'timeline_snapshot': {'config': {'theme_overrides': {'visual': {'canvas': {'fps': 24}}},
                'tracks': [{'id': 'picture', 'kind': 'visual'}, {'id': 'vo', 'kind': 'audio'}],
                'clips': [{'id': 'picture', 'track': 'picture', 'at': 0, 'from': 70, 'to': 73},
                          {'id': 'voice', 'track': 'vo', 'at': 1, 'from': 0, 'to': 1}]}}}}


class FakeClient:
    def __init__(self):
        self.current_version = 1
        self.current_head = 1
        self.raw = TEXT
        self.owned = True
        self.read_binding = False

    def get_object(self, object_id): return {'data': self.raw}
    def get_project(self, project): return {'id': 'p', 'metadata': {'default_timeline_id': 'tl'}}
    def list_timelines(self, project, **kwargs):
        return [[{'timeline_id': 'tl', 'slug': 'cut', 'config_version': self.current_version}], None]
    def list_project_runs(self, project, **kwargs): return [[self.get_run('run')], None]
    def get_run(self, ref): return {'id': 'run', 'project_id': 'p', 'capability': 'rendering.render', 'status': 'succeeded', 'task_ids': ['task']}
    def get_task(self, ref): return {'state': 'succeeded', 'capability_id': 'rendering.render', 'spec': {'spec': envelope()}, 'result': {'outputs': [{'name': 'video', 'digest': VIDEO}]}}
    def get_timeline(self, ref): return {'config_version': self.current_version}
    def get_project_shot_text_binding(self, project, binding):
        self.read_binding = True
        return {'head': self.current_head, 'content_hash': DIGEST}
    def list_project_objects(self, project, **kwargs): return [[{'object_id': VIDEO}] if self.owned else [], None]


class DirectRenderClient(FakeClient):
    def get_run(self, ref):
        assert ref == DIRECT_RENDER_RUN
        return {'id': DIRECT_RENDER_RUN, 'project_id': 'p', 'capability': 'rendering.render',
            'status': 'succeeded', 'task_ids': [DIRECT_RENDER_TASK]}

    def get_task(self, ref):
        assert ref == DIRECT_RENDER_TASK
        frozen = envelope()
        authority = frozen.pop('authority_context')
        authority.update({
            'project_id': 'p', 'project_slug': 'p', 'timeline_id': DIRECT_TIMELINE,
            'timeline_ulid': DIRECT_TIMELINE, 'head_event_id': '1',
            'head_hash': 'head', 'config_hash': 'config',
            'registry_hash': 'registry', 'materialized_registry_hash': 'registry',
        })
        frozen['inputs']['timeline_authority'] = authority
        frozen['inputs']['timeline_ref'] = DIRECT_TIMELINE
        frozen['timeline_snapshot'] = frozen['inputs'].pop('timeline_snapshot')
        return {'task_id': DIRECT_RENDER_TASK, 'state': 'succeeded',
            'capability_id': 'rendering.render', 'spec': {'spec': frozen},
            'result': {'outputs': [{'name': 'video', 'digest': VIDEO}]}}

    def list_timelines(self, project, **kwargs):
        return [[{'timeline_id': DIRECT_TIMELINE, 'slug': 'cut'}], None]


class CandidateFirstClient(FakeClient):
    """A newer unpublished preview must not mask the canonical render."""

    def list_project_runs(self, project, **kwargs):
        return [[
            {'id': 'candidate', 'created_at': '2026-09-23T02:00:00Z'},
            {'id': 'run', 'created_at': '2026-09-23T01:00:00Z'},
        ], None]

    def get_run(self, ref):
        return {'id': ref, 'project_id': 'p', 'capability': 'rendering.render',
                'status': 'succeeded', 'task_ids': [f'{ref}-task']}

    def get_task(self, ref):
        value = envelope()
        if ref == 'candidate-task':
            value['authority_context']['render_mode'] = 'authoring_candidate_preview'
            value['authority_context']['authoring_preview'] = {'candidate_digest': 'sha256:' + 'b' * 64}
        return {'state': 'succeeded', 'capability_id': 'rendering.render',
                'spec': {'spec': value},
                'result': {'outputs': [{'name': 'video', 'digest': VIDEO}]}}


class ComposedCaptureClient:
    def __init__(
        self, *, saved_revision='saved-revision', run_revision=None, run_exists=True,
        run_project='p', candidate=False,
    ):
        self.saved_revision = saved_revision
        self.run_revision = run_revision or saved_revision
        self.run_exists = run_exists
        self.run_project = run_project
        self.candidate = candidate

    def get_project(self, project):
        return {'project_id': 'p', 'id': 'p', 'slug': 'demo'}

    def list_timelines(self, project, **kwargs):
        return [[{
            'project_id': 'p', 'project_slug': 'demo', 'timeline_id': 'tl',
            'slug': 'cut', 'head_revision_id': 'current-revision',
            'parent_revision_id': 'current-revision',
        }], None]

    def get_run(self, run_id):
        if not self.run_exists:
            raise KeyError(run_id)
        return {
            'id': run_id, 'project_id': self.run_project, 'capability': 'rendering.render',
            'status': 'succeeded', 'task_ids': ['render-task'],
        }

    def get_task(self, task_id):
        authority = {
            'project_id': self.run_project, 'project_slug': 'demo', 'timeline_id': 'tl',
            'parent_revision_id': self.run_revision,
        }
        if self.candidate:
            authority['authoring_preview'] = {'candidate_digest': 'sha256:' + 'b' * 64}
        frozen = {
            'inputs': {
                'timeline_authority': authority,
                'timeline_snapshot': {
                    'config': {
                        'theme_overrides': {'visual': {'canvas': {'fps': 24}}},
                        'tracks': [], 'clips': [],
                    },
                    'registry': {'assets': {}},
                },
                'profile': {'fps_rational': [24, 1]},
            },
        }
        return {
            'id': task_id, 'state': 'succeeded', 'capability_id': 'rendering.render',
            'spec': {'spec': frozen},
            'result': {'outputs': [{
                'output_port': 'video', 'digest': VIDEO,
                'managed_object_reference': {'object_id': MANAGED_VIDEO_REFERENCE},
                'filename': 'render.mp4', 'media_type': 'video/mp4', 'size': 100,
                'ordinal': 0, 'role': 'result',
            }]},
        }

    def list_project_objects(self, project, **kwargs):
        return [[{'object_id': MANAGED_VIDEO_REFERENCE, 'digest': VIDEO, 'size': 100}], None]


class DivergentInputClient:
    """A current listing whose requested input view is an older exact parent."""

    def get_project(self, project):
        return {'project_id': 'p', 'id': 'p', 'slug': 'demo'}

    def list_timelines(self, project, **kwargs):
        return [[{
            'project_id': 'p', 'timeline_id': 'tl', 'slug': 'cut',
            'head_revision_id': 'current-revision',
            'parent_revision_id': 'current-revision',
            'config_version': 9, 'head_event_id': 'current-event',
            'head_hash': 'current-head-hash', 'config_hash': 'current-config-hash',
            'registry_hash': 'current-registry-hash',
        }], None]

    def get_project_parent_composition_revision(self, project, timeline, revision):
        assert (project, timeline, revision) == ('p', 'tl', 'historical-revision')
        return {
            'project_id': 'p', 'timeline_id': 'tl', 'revision_id': revision,
            'content_digest': 'sha256:' + 'd' * 64,
            'payload': {
                'config': {
                    'theme_overrides': {'visual': {'canvas': {'fps': 24}}},
                    'tracks': [{'id': 'picture', 'kind': 'visual'}],
                    'clips': [{'id': 'historical-clip', 'track': 'picture', 'at': 0, 'hold': 1}],
                },
                'registry': {'assets': {}}, 'occurrences': [],
            },
        }

    def get_project_shot_revision(self, *_args):
        raise AssertionError('the flat historical fixture has no child reads')

    def get_project_timeline_revision(self, *_args):
        raise AssertionError('the flat historical fixture has no child reads')


def test_exact_old_render_uses_frozen_script_without_current_binding_reads():
    client = FakeClient(); client.current_version = 9; client.current_head = 7
    value = envelope()
    value['authority_context']['expansion']['occurrences'] = [
        {'shot_occurrence_id': 'shot-occ-0000-sh', 'shot_id': 'sh', 'at': 0, 'hold': 3}
    ]
    client.get_task = lambda ref: {'state': 'succeeded', 'capability_id': 'rendering.render', 'spec': {'spec': value}, 'result': {'outputs': [{'name': 'video', 'digest': VIDEO}]}}
    result = prepare_filmstrip(
        {'render_run': 'run', 'timeline_ref': 'tl'}, project='p', client=client,
    )
    snapshot = result['filmstrip_snapshot']
    assert snapshot['duration_frames'] == 72
    assert snapshot['scripts'][0]['head'] == 1
    assert snapshot['scripts'][0]['text'] == TEXT.decode()
    assert snapshot['scripts'][0]['timing_basis'] == 'shot_script'
    assert (snapshot['scripts'][0]['start'], snapshot['scripts'][0]['end']) == (0, 3)
    assert not client.read_binding
    assert snapshot['metadata']['selection'] == 'explicit_historical_render'


def test_auto_selected_current_render_has_distinct_provenance_label():
    result = prepare_filmstrip(
        {'render_run': 'run', 'timeline_ref': 'tl', 'auto_selected_render': True},
        project='p', client=FakeClient(),
    )
    assert result['filmstrip_snapshot']['metadata']['selection'] == 'latest_current_render'


def test_direct_render_uses_frozen_inputs_authority_and_exact_run():
    result = prepare_filmstrip(
        {'render_run': DIRECT_RENDER_RUN, 'timeline_ref': DIRECT_TIMELINE},
        project='p', client=DirectRenderClient(),
    )

    assert result['render_run_id'] == DIRECT_RENDER_RUN
    assert result['timeline_id'] == DIRECT_TIMELINE


@pytest.mark.parametrize('wrapper', ['transport', 'remote', 'public'])
def test_real_filmstrip_preflight_accepts_client_wrappers(wrapper):
    from astrid.sdk.client import AstridClient
    from astrid.sdk.remote import RemoteAstridClient
    from astrid.sdk.invocation import _validate_timeline_visualize_inputs

    client = DirectRenderClient()
    if wrapper in {'remote', 'public'}:
        client = RemoteAstridClient(client)
    if wrapper == 'public':
        client = AstridClient(remote=client)
    result = _validate_timeline_visualize_inputs(
        {'render_run': DIRECT_RENDER_RUN, 'timeline_ref': DIRECT_TIMELINE},
        project='p', _client=client,
    )
    assert result['render_run_id'] == DIRECT_RENDER_RUN
    assert result['timeline_id'] == DIRECT_TIMELINE


def test_latest_selection_skips_newer_unpublished_candidate_preview():
    result = prepare_filmstrip({}, project='p', client=CandidateFirstClient())
    assert result['render_run_id'] == 'run'


def test_matching_render_fails_closed_without_exact_timeline_identity(monkeypatch):
    import astrid.sdk.project_render as project_render

    monkeypatch.setattr(project_render, "_lookup_managed_output", lambda **kwargs: {"verified": True})
    client = FakeClient()
    assert matching_composed_render(
        client,
        project_id='p',
        timeline={'timeline_id': 'tl', 'config_version': 1},
    ) == 'run'
    assert matching_composed_render(client, project_id='p', timeline={'timeline_id': 'tl'}) is None
    assert matching_composed_render(
        client,
        project_id='p',
        timeline={'timeline_id': 'different', 'config_version': 1},
    ) is None


def test_final_output_matching_reuses_candidate_render_only_after_exact_publication(monkeypatch):
    import astrid.sdk.project_render as project_render

    monkeypatch.setattr(project_render, "_lookup_managed_output", lambda **kwargs: {"verified": True})
    pins = {
        "timeline_id": "tl", "parent_revision_id": "published-revision",
        "config_version": 4, "head_event_id": "published-revision",
        "head_hash": "candidate-content", "config_hash": "candidate-config",
        "registry_hash": "candidate-registry", "materialized_registry_hash": "candidate-materialized",
    }

    class CandidateRuntime:
        explicit_profile = None

        def list_project_runs(self, project, **kwargs):
            # A next cursor must not cause an unbounded history walk.
            return [[{
                "id": "candidate-run", "created_at": "2026-09-28T12:00:00Z",
                "capability": "rendering.render", "status": "succeeded",
            }], "more"]

        def get_run(self, run_id):
            return {"id": run_id, "project_id": "p", "capability": "rendering.render",
                    "status": "succeeded", "task_ids": ["candidate-task"]}

        def get_task(self, task_id):
            authority = {
                **pins,
                "project_id": "p",
                "render_mode": "authoring_candidate_preview",
                "authoring_preview": {
                    "candidate_digest": "sha256:candidate",
                    "publication_digest": "sha256:publication",
                    "candidate_parent_revision_id": "published-revision",
                },
            }
            return {
                "id": task_id, "state": "succeeded", "capability_id": "rendering.render",
                "spec": {"spec": {"inputs": {
                    "timeline_authority": authority,
                    **({"profile": self.explicit_profile} if self.explicit_profile is not None else {}),
                }}},
                "result": {"outputs": [{"output_port": "video", "digest": VIDEO}]},
            }

    runtime = CandidateRuntime()
    exact_timeline = {"project_id": "p", **pins}
    assert matching_composed_render(runtime, project_id="p", timeline=exact_timeline) == "candidate-run"
    stale_timeline = {**exact_timeline, "parent_revision_id": "different-revision"}
    assert matching_composed_render(runtime, project_id="p", timeline=stale_timeline) is None
    runtime.explicit_profile = {"resolution": [640, 360]}
    assert matching_composed_render(runtime, project_id="p", timeline=exact_timeline) is None


def test_exact_unpublished_candidate_is_admitted_and_labelled_truthfully():
    result = prepare_filmstrip(
        {'render_run': 'candidate', 'timeline_ref': 'tl'},
        project='p', client=CandidateFirstClient(),
    )

    assert result['render_run_id'] == 'candidate'
    assert result['filmstrip_snapshot']['metadata']['selection'] == 'explicit_candidate_preview'


def test_exact_pinned_input_inspection_does_not_fall_back_to_current_timeline():
    result = prepare_filmstrip(
        {'render_run': DIRECT_RENDER_RUN, 'timeline_ref': DIRECT_TIMELINE,
         'show': ['inputs'], 'hide': ['output']},
        project='p', client=DirectRenderClient(),
    )
    assert result['mode'] == 'input_only'
    assert result['render_run_id'] == DIRECT_RENDER_RUN
    assert result['input_snapshot']['metadata']['selection'] == 'render_pinned_input_only'


def test_requested_input_revision_reads_historical_content_when_head_diverges():
    result = prepare_filmstrip(
        {
            'timeline_ref': 'tl', 'revision_id': 'historical-revision',
            'show': ['inputs'], 'hide': ['output'],
        },
        project='p', client=DivergentInputClient(),
    )

    snapshot = result['input_snapshot']
    assert snapshot['clips'][0]['id'] == 'historical-clip'
    assert snapshot['metadata']['parent_revision_id'] == 'historical-revision'
    assert snapshot['metadata']['parent_revision_id'] != 'current-revision'
    assert snapshot['metadata']['timeline_identity'] == {'timeline_id': 'tl', 'slug': 'cut'}


def _patch_composed_parent(monkeypatch, *, unresolvable=()):
    from astrid.packs.rendering.actions.render import managed_timeline

    reads = []

    def project_exact_parent(*, client, project_id, timeline_id, parent_revision_id):
        reads.append((project_id, timeline_id, parent_revision_id))
        if parent_revision_id in unresolvable:
            raise ValueError(f'Runtime has no immutable parent revision {parent_revision_id}')
        config = {
            'theme_overrides': {'visual': {'canvas': {'fps': 24}}},
            'tracks': [], 'clips': [],
        }
        registry = {'assets': {}}
        parent = {
            'project_id': project_id, 'timeline_id': timeline_id,
            'revision_id': parent_revision_id, 'content_digest': 'sha256:' + 'd' * 64,
        }
        closure = {
            'parent_revision_id': parent_revision_id,
            'children': [], 'shots': [], 'occurrences': [],
        }
        return parent, SimpleNamespace(config=config, registry=registry), closure

    monkeypatch.setattr(managed_timeline, '_project_exact_parent_head', project_exact_parent)
    monkeypatch.setattr(
        managed_timeline, '_runtime_snapshot_registry',
        lambda registry, **kwargs: deepcopy(dict(registry)),
    )
    return reads


def test_composed_capture_explicit_revision_reads_that_saved_revision(monkeypatch):
    reads = _patch_composed_parent(monkeypatch)
    result = prepare_filmstrip(
        {'composed_capture': True, 'revision_id': 'historical-revision', 'timeline_ref': 'tl'},
        project='p', client=ComposedCaptureClient(),
    )

    assert result['mode'] == 'composed_capture'
    assert result['revision_id'] == 'historical-revision'
    assert reads == [('p', 'tl', 'historical-revision')]
    assert result['capture_snapshot']['metadata']['capture_authority']['parent_revision_id'] == 'historical-revision'


def test_composed_capture_explicit_run_resolves_to_its_saved_revision(monkeypatch):
    reads = _patch_composed_parent(monkeypatch)
    result = prepare_filmstrip(
        {'composed_capture': True, 'render_run': 'render-run', 'timeline_ref': 'tl'},
        project='p', client=ComposedCaptureClient(run_revision='run-saved-revision'),
    )

    assert result['mode'] == 'composed_capture'
    assert result['revision_id'] == 'run-saved-revision'
    assert reads == [('p', 'tl', 'run-saved-revision')]
    assert result['capture_snapshot']['metadata']['selection'] == 'composed_frame_capture'
    assert result['capture_snapshot']['metadata']['capture_authority']['parent_revision_id'] == 'run-saved-revision'


@pytest.mark.parametrize('authority_kind', ['canonical', 'missing_pin', 'candidate'])
def test_composed_explicit_run_consumes_managed_snapshot_authority(monkeypatch, authority_kind):
    from dataclasses import replace
    from astrid.packs.rendering.actions.render.managed_timeline import ManagedRenderSnapshot

    reads = _patch_composed_parent(monkeypatch)
    client = ComposedCaptureClient(run_revision='run-saved-revision')
    task = client.get_task('render-task')
    frozen = task['spec']['spec']['inputs']['timeline_snapshot']
    snapshot = ManagedRenderSnapshot(
        project_id='p', project_slug='demo', timeline_id='tl', timeline_ulid='tl',
        timeline_slug='cut', config_version=1, head_event_id='run-saved-revision',
        head_hash='d' * 64, config=frozen['config'], registry=frozen['registry'],
        config_hash='a' * 64, registry_hash='b' * 64, materialized_registry_hash='b' * 64,
    )
    if authority_kind == 'candidate':
        snapshot = replace(snapshot, authoring_preview={
            'candidate_parent_revision_id': 'unpublished-parent',
            'candidate_digest': 'sha256:' + 'c' * 64,
        })
    authority = snapshot.authority()
    if authority_kind == 'missing_pin':
        authority.pop('parent_revision_id')
    task['spec']['spec']['inputs']['timeline_authority'] = authority
    client.get_task = lambda task_id: deepcopy(task)

    inputs = {'composed_capture': True, 'render_run': 'render-run', 'timeline_ref': 'tl'}
    if authority_kind != 'canonical':
        message = 'immutable saved parent revision' if authority_kind == 'missing_pin' else 'unpublished candidate'
        with pytest.raises(CapabilityValidationError, match=message):
            prepare_filmstrip(inputs, project='p', client=client)
        assert reads == []
        return
    result = prepare_filmstrip(inputs, project='p', client=client)
    assert reads == [('p', 'tl', 'run-saved-revision')]
    assert result['revision_id'] == 'run-saved-revision'
    assert result['capture_snapshot']['metadata']['capture_authority']['parent_revision_id'] == 'run-saved-revision'


def test_composed_capture_rejects_conflicting_explicit_revision_and_run(monkeypatch):
    reads = _patch_composed_parent(monkeypatch)
    with pytest.raises(CapabilityValidationError, match='conflicts with the saved revision'):
        prepare_filmstrip(
            {
                'composed_capture': True, 'render_run': 'render-run',
                'revision_id': 'different-revision', 'timeline_ref': 'tl',
            },
            project='p', client=ComposedCaptureClient(run_revision='run-saved-revision'),
        )
    assert reads == []


@pytest.mark.parametrize(
    ('client_options', 'message'),
    [
        ({'run_project': 'other-project'}, 'belongs to a different project'),
        ({'candidate': True}, 'unpublished candidate'),
    ],
)
def test_composed_capture_rejects_wrong_project_and_candidate_runs(
    monkeypatch, client_options, message,
):
    reads = _patch_composed_parent(monkeypatch)
    with pytest.raises(CapabilityValidationError, match=message):
        prepare_filmstrip(
            {'composed_capture': True, 'render_run': 'render-run', 'timeline_ref': 'tl'},
            project='p', client=ComposedCaptureClient(**client_options),
        )
    assert reads == []


def test_composed_capture_keeps_matching_video_only_as_include_media_attachment(monkeypatch):
    reads = _patch_composed_parent(monkeypatch)
    result = prepare_filmstrip(
        {
            'composed_capture': True, 'render_run': 'render-run',
            'timeline_ref': 'tl', 'include_media': True,
        },
        project='p', client=ComposedCaptureClient(run_revision='run-saved-revision'),
    )

    assert result['mode'] == 'filmstrip'
    assert result['frame_source'] == 'composed_capture'
    assert result['revision_id'] == 'run-saved-revision'
    assert result['capture_snapshot']['metadata']['capture_authority']['parent_revision_id'] == 'run-saved-revision'
    assert result['capture_snapshot']['video_digest'] == result['capture_identity']
    assert result['video_digest'] == VIDEO
    assert result['video_object_id'] == MANAGED_VIDEO_REFERENCE
    assert result['managed_object_reference'] == MANAGED_VIDEO_REFERENCE
    assert result['render_run_id'] == 'render-run'
    assert reads == [('p', 'tl', 'run-saved-revision')]


def test_auto_include_media_attaches_exact_render_without_changing_frame_revision(monkeypatch):
    import astrid.sdk.timeline_filmstrip as timeline_filmstrip

    reads = _patch_composed_parent(monkeypatch)
    match_calls = []

    def matching_render(client, *, project_id, timeline, limit):
        match_calls.append((project_id, dict(timeline), limit))
        return 'render-run'

    monkeypatch.setattr(timeline_filmstrip, 'matching_composed_render', matching_render)
    result = prepare_filmstrip(
        {'composed_capture': True, 'timeline_ref': 'tl', 'include_media': True},
        project='p', client=ComposedCaptureClient(run_revision='current-revision'),
    )

    assert result['mode'] == 'filmstrip'
    assert result['frame_source'] == 'composed_capture'
    assert result['capture_snapshot']['metadata']['capture_authority']['parent_revision_id'] == 'current-revision'
    assert result['capture_identity'] == result['capture_snapshot']['metadata']['capture_identity']
    assert result['video_digest'] == VIDEO
    assert match_calls == [(
        'p', {
            'project_id': 'p', 'project_slug': 'demo', 'timeline_id': 'tl',
            'parent_revision_id': 'current-revision',
        }, 50,
    )]
    assert reads == [('p', 'tl', 'current-revision')]


def test_include_media_fails_clearly_when_no_exact_render_attachment_exists(monkeypatch):
    import astrid.sdk.timeline_filmstrip as timeline_filmstrip

    _patch_composed_parent(monkeypatch)
    monkeypatch.setattr(timeline_filmstrip, 'matching_composed_render', lambda *args, **kwargs: None)

    with pytest.raises(CapabilityValidationError, match='include_media requires an exact managed rendered video'):
        prepare_filmstrip(
            {'composed_capture': True, 'timeline_ref': 'tl', 'include_media': True},
            project='p', client=ComposedCaptureClient(run_revision='current-revision'),
        )


def test_composed_capture_fails_closed_for_stale_revision_and_unresolvable_run(monkeypatch):
    _patch_composed_parent(monkeypatch, unresolvable=('stale-revision',))
    with pytest.raises(CapabilityValidationError, match='cannot resolve admitted saved revision'):
        prepare_filmstrip(
            {'composed_capture': True, 'revision_id': 'stale-revision', 'timeline_ref': 'tl'},
            project='p', client=ComposedCaptureClient(),
        )

    with pytest.raises(CapabilityValidationError, match='cannot be resolved to a saved revision'):
        prepare_filmstrip(
            {'composed_capture': True, 'render_run': 'missing-run', 'timeline_ref': 'tl'},
            project='p', client=ComposedCaptureClient(run_exists=False),
        )


def test_identical_legacy_duplicates_remain_compatible():
    client = DirectRenderClient()
    original = client.get_task

    def get_task(ref):
        task = original(ref)
        frozen = task['spec']['spec']
        frozen['authority_context'] = deepcopy(frozen['inputs']['timeline_authority'])
        frozen['inputs']['timeline_snapshot'] = deepcopy(frozen['timeline_snapshot'])
        return task

    client.get_task = get_task
    result = prepare_filmstrip(
        {'render_run': DIRECT_RENDER_RUN, 'timeline_ref': DIRECT_TIMELINE},
        project='p', client=client,
    )

    assert result['render_run_id'] == DIRECT_RENDER_RUN


@pytest.mark.parametrize('mutation, message', [
    (lambda value: value.__setitem__('authority_context', dict(
        value['inputs']['timeline_authority'], timeline_slug='other')),
     'authority sources disagree'),
    (lambda value: value.__setitem__('authority_context', dict(
        value['inputs']['timeline_authority'], project_id='other')),
     'authority sources disagree'),
    (lambda value: value.__setitem__('authority_context', dict(
        value['inputs']['timeline_authority'], head_hash='other')),
     'authority sources disagree'),
    (lambda value: value['inputs'].__setitem__('timeline_snapshot', dict(
        value['timeline_snapshot'], config={'conflicting': True})),
     'timeline snapshots disagree'),
])
def test_conflicting_duplicate_frozen_sources_are_rejected(mutation, message):
    client = DirectRenderClient()
    original = client.get_task

    def get_task(ref):
        task = original(ref)
        mutation(task['spec']['spec'])
        return task

    client.get_task = get_task
    with pytest.raises(CapabilityValidationError, match=message):
        prepare_filmstrip(
            {'render_run': DIRECT_RENDER_RUN, 'timeline_ref': DIRECT_TIMELINE},
            project='p', client=client,
        )


@pytest.mark.parametrize('mutation, message', [
    (lambda value: value['inputs'].__setitem__('timeline_authority', 'malformed'), 'malformed'),
    (lambda value: value['inputs']['timeline_authority'].__setitem__('timeline_id', 'other'), 'No successful render'),
])
def test_direct_render_refuses_malformed_or_mismatched_authority(mutation, message):
    client = DirectRenderClient()
    original = client.get_task

    def get_task(ref):
        task = original(ref)
        mutation(task['spec']['spec'])
        return task

    client.get_task = get_task
    with pytest.raises(CapabilityValidationError, match=message):
        prepare_filmstrip(
            {'render_run': DIRECT_RENDER_RUN, 'timeline_ref': DIRECT_TIMELINE},
            project='p', client=client,
        )


def test_filmstrip_exposes_spoken_text_only_not_generation_prompts():
    value = envelope()
    value['authority_context']['expansion']['occurrences'] = [
        {'shot_occurrence_id': 'shot-occ-0000-sh', 'shot_id': 'sh', 'at': 0, 'hold': 3}
    ]
    bindings = value['authority_context']['expansion']['shots'][0]['text_bindings']
    bindings.extend([
        {'binding_id': 'positive', 'head': 1, 'media_id': DIGEST,
         'content_hash': DIGEST, 'byte_size': len(TEXT), 'kind': 'prompt', 'slot': 'positive'},
        {'binding_id': 'negative', 'head': 1, 'media_id': DIGEST,
         'content_hash': DIGEST, 'byte_size': len(TEXT), 'kind': 'prompt', 'slot': 'negative'},
        {'binding_id': 'unmarked-transcript', 'head': 1, 'media_id': DIGEST,
         'content_hash': DIGEST, 'byte_size': len(TEXT), 'kind': 'transcript'},
        {'binding_id': 'spoken-transcript', 'head': 1, 'media_id': DIGEST,
         'content_hash': DIGEST, 'byte_size': len(TEXT), 'kind': 'transcript', 'spoken': True},
    ])
    result = build_filmstrip_snapshot(value, client=FakeClient(), project='p', run_id='run', video_digest=VIDEO)
    assert [script['kind'] for script in result['scripts']] == ['voiceover_script', 'transcript']
    assert [script['binding_id'] for script in result['scripts']] == ['binding', 'spoken-transcript']


def test_shotless_verified_speech_is_available_without_shot_identity():
    value = envelope()
    value['inputs']['speech_annotations'] = [{
        'id': 'speech-line', 'start': 0, 'end': 1,
        'text': 'Shotless spoken line', 'source_type': 'verified_speech',
    }]
    value['inputs']['speech_occurrences'] = [{
        'id': 'speech-occurrence', 'from': 0, 'to': 1, 'placement': 10,
    }]

    result = build_filmstrip_snapshot(value, client=FakeClient(), project='p', run_id='run', video_digest=VIDEO)
    phrases = result['audio']['speech']['phrases']

    assert len(phrases) == 1
    assert phrases[0]['canonical_text'] == 'Shotless spoken line'
    assert phrases[0]['status'] == 'projected'
    assert phrases[0]['occurrence_id'] == 'speech-occurrence'
    assert 'shot_id' not in phrases[0]
    assert phrases[0]['render_interval']['start'] == [10, 1]
    assert phrases[0]['render_interval']['end'] == [11, 1]


def test_speech_annotation_allowlist_excludes_prompt_and_unmarked_transcript():
    value = envelope()
    value['inputs']['speech_annotations'] = [
        {'id': 'prompt', 'start': 0, 'end': 1, 'text': 'PROMPT_TRAP', 'source_type': 'generation_prompt'},
        {'id': 'unmarked', 'start': 0, 'end': 1, 'text': 'UNMARKED_TRAP', 'source_type': 'transcript'},
        {'id': 'spoken', 'start': 0, 'end': 1, 'text': 'Verified speech', 'source_type': 'verified_speech'},
    ]
    value['inputs']['speech_occurrences'] = [{'id': 'speech-occurrence', 'from': 0, 'to': 1, 'placement': 2}]

    result = build_filmstrip_snapshot(value, client=FakeClient(), project='p', run_id='run', video_digest=VIDEO)
    texts = [phrase['canonical_text'] for phrase in result['audio']['speech']['phrases']]

    assert texts == ['Verified speech']


def test_shotless_speech_without_explicit_render_occurrence_does_not_guess_timing():
    value = envelope()
    value['inputs']['speech_annotations'] = [{
        'id': 'source-only', 'start': 4, 'end': 5, 'text': 'Source-only line',
        'source_type': 'verified_speech',
    }]
    value['inputs']['speech_occurrences'] = []

    result = build_filmstrip_snapshot(value, client=FakeClient(), project='p', run_id='run', video_digest=VIDEO)

    assert result['audio']['speech']['status'] == 'no_occurrences'
    assert result['audio']['speech']['phrases'] == []


@pytest.mark.parametrize('field', ['current_version', 'current_head'])
def test_default_refuses_stale_timeline_or_script(field):
    client = FakeClient(); setattr(client, field, 2)
    with pytest.raises(CapabilityValidationError, match='stale'):
        prepare_filmstrip({}, project='p', client=client)


def test_latest_render_refuses_canonical_head_change_before_projection():
    """A current-head change cannot be mixed into a selected render view."""
    client = FakeClient()

    def changed_head(_ref):
        # The render was admitted at version one. Simulate the canonical
        # timeline advancing after run selection but before filmstrip
        # projection; the request must fail closed rather than joining the
        # old render with a new timeline revision.
        return {'config_version': 2}

    client.get_timeline = changed_head
    with pytest.raises(CapabilityValidationError, match='stale'):
        prepare_filmstrip({}, project='p', client=client)


def test_refuses_changed_script_bytes():
    client = FakeClient(); client.raw = b'wrong'
    with pytest.raises(CapabilityValidationError, match='bytes'):
        prepare_filmstrip({'render_run': 'run'}, project='p', client=client)


def test_missing_placement_does_not_guess_script_from_clip_name():
    value = envelope(); del value['inputs']['review_context']
    value['authority_context']['expansion']['occurrences'] = []
    result = build_filmstrip_snapshot(value, client=FakeClient(), project='p', run_id='run', video_digest=VIDEO)
    assert result['scripts'] == []
    assert not result['metadata']['script_mapping_available']


def test_flattened_image_clip_uses_admission_occurrence_identity():
    value = envelope()
    authority = value['authority_context']
    authority['expansion']['occurrences'] = [{
        'shot_occurrence_id': 'shot-occ-0000-sh', 'shot_id': 'sh',
        'name': 'Blue', 'at': 0, 'hold': 3,
        'timeline_document_id': 'child', 'source_index': 0,
    }]
    config = value['inputs']['timeline_snapshot']['config']
    config['clips'][0].update(
        clipType='image', shot_id='sh', shot_name='forged child name',
        shot_occurrence_id='shot-occ-0000-sh',
    )
    # The sidecar is the source of truth for the name/script; no timing join
    # or filename/clip-type inference is needed for this flattened payload.
    result = build_filmstrip_snapshot(value, client=FakeClient(), project='p', run_id='run', video_digest=VIDEO)
    assert result['metadata']['script_mapping_available']
    assert result['scripts'][0]['occurrence_id'] == 'shot-occ-0000-sh'
    assert result['clips'][0]['shot_name'] == 'Blue'
    assert result['clips'][0]['shot_id'] == 'sh'


def test_rejects_foreign_output_and_arbitrary_path():
    client = FakeClient(); client.owned = False
    with pytest.raises(CapabilityValidationError, match='owned'):
        prepare_filmstrip({'render_run': 'run'}, project='p', client=client)
    with pytest.raises(CapabilityValidationError, match='managed'):
        prepare_filmstrip({'rendered_video': '/tmp/video.mp4'}, project='p', client=client)


def test_repeated_shots_keep_distinct_occurrences_and_canonical_duration():
    value = envelope()
    value['authority_context']['expansion']['occurrences'] = [
        {'shot_occurrence_id': 'one', 'shot_id': 'sh', 'at': 0, 'hold': 3},
        {'shot_occurrence_id': 'two', 'shot_id': 'sh', 'at': 4.01, 'hold': 1.01},
    ]
    value['inputs']['timeline_snapshot']['config']['clips'].append(
        {'id': 'repeat', 'track': 'picture', 'at': 4.01, 'hold': 1.01,
         'shot_occurrence_id': 'two'})
    result = build_filmstrip_snapshot(value, client=FakeClient(), project='p', run_id='run', video_digest=VIDEO)
    assert len({s['occurrence_id'] for s in result['scripts']}) == 2
    assert result['clips'][-1]['occurrence_id'] == result['scripts'][-1]['occurrence_id']
    assert result['clips'][-1]['start_frame'] == 96
    assert result['duration_frames'] == 120  # rounded start + rounded duration, not ceil(5.02*24)


def test_authored_review_context_never_infers_shot_labels():
    value = envelope()
    value['inputs']['timeline_snapshot']['config']['clips'][0].update(
        shot_id='forged', shot_name='Authored-only label'
    )
    result = build_filmstrip_snapshot(value, client=FakeClient(), project='p', run_id='run', video_digest=VIDEO)
    assert result['occurrences'] == []
    assert result['scripts'] == []
    assert not any(key in result['clips'][0] for key in ('shot_id', 'shot_name', 'occurrence_id', 'occurrence_ids'))


def test_legacy_output_fps_hint_is_not_treated_as_render_evidence():
    value = envelope()
    config = value['inputs']['timeline_snapshot']['config']
    del config['theme_overrides']
    config['output'] = {'fps': 30}
    with pytest.raises(CapabilityValidationError, match='frame rate'):
        build_filmstrip_snapshot(value, client=FakeClient(), project='p', run_id='run', video_digest=VIDEO)


def test_tracks_are_frozen_including_empty_lanes():
    value = envelope()
    tracks = value['inputs']['timeline_snapshot']['config']['tracks']
    tracks.append({'id': 'empty', 'kind': 'audio', 'label': 'Unused soundtrack'})
    result = build_filmstrip_snapshot(value, client=FakeClient(), project='p', run_id='run', video_digest=VIDEO)
    assert result['tracks'][-1] == tracks[-1]
    tracks[-1]['label'] = 'Changed after freezing'
    assert result['tracks'][-1]['label'] == 'Unused soundtrack'
