from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.sdk import invocation
from astrid.sdk.exceptions import CapabilityValidationError

DIGEST = 'sha256:' + 'b' * 64


def test_filmstrip_preflight_supports_intersecting_filters(monkeypatch):
    received = {}
    def prepare(inputs, *, project, client):
        received.update(inputs)
        assert project == 'p'
        assert client == 'runtime'
        return {'mode': 'filmstrip'}
    monkeypatch.setattr('astrid.sdk.timeline_filmstrip.prepare_filmstrip', prepare)
    result = invocation._validate_timeline_visualize_inputs(
        {'view': 'filmstrip', 'shot': 'shot1', 'clip': 'clip1', 'asset': 'a'},
        project='p', _client='runtime')
    assert result['mode'] == 'filmstrip'
    assert received['shot'] == 'shot1'
    assert received['clip'] == 'clip1'


def test_visualize_preflight_defaults_to_filmstrip(monkeypatch):
    received = {}

    def prepare(inputs, *, project, client):
        received.update(inputs)
        return {'mode': 'filmstrip'}

    monkeypatch.setattr('astrid.sdk.timeline_filmstrip.prepare_filmstrip', prepare)
    result = invocation._validate_timeline_visualize_inputs({}, project='p', _client='runtime')

    assert result['mode'] == 'filmstrip'
    assert received == {}


def test_caller_cannot_supply_authority():
    with pytest.raises(CapabilityValidationError, match='host-owned'):
        invocation._validate_timeline_visualize_inputs(
            {'view': 'filmstrip', 'filmstrip_authority': '{}'}, project='p')


def test_filmstrip_cache_parent_is_project_namespaced(tmp_path, monkeypatch):
    monkeypatch.setattr(invocation.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(invocation, "_runtime_data_root", lambda: None)
    assert invocation._filmstrip_cache_parent(project="astrid intro/v1") == (
        tmp_path / "Library" / "Caches" / "Astrid" / "timeline-visualize" / "astrid_intro_v1"
    )


def test_filmstrip_default_uses_installation_owned_data_root(monkeypatch, tmp_path):
    monkeypatch.setattr(invocation, "_runtime_data_root", lambda: tmp_path / ".astrid-data")
    (tmp_path / "elsewhere").mkdir()
    monkeypatch.chdir(tmp_path / "elsewhere")
    assert invocation._filmstrip_cache_parent(project="astrid intro/v1") == (
        tmp_path / ".astrid-data" / "timeline-visualize" / "astrid_intro_v1"
    )


def test_managed_video_is_in_task_authorization_manifest():
    recorded = {}
    class ReachedAdmission(Exception): pass
    def create(**kwargs):
        recorded.update(kwargs)
        raise ReachedAdmission
    client = SimpleNamespace(tasks=SimpleNamespace(create=create))
    authority = {'mode': 'filmstrip', 'video_object_id': DIGEST}
    with pytest.raises(ReachedAdmission):
        invocation._kernel_invoke(SimpleNamespace(id='rendering.timeline_visualize', capability_type='executor'),
            kind='executor', project='p', outputs={},
            inputs={'rendered_video': {'object_id': DIGEST, 'digest': DIGEST}},
            idempotency_context=authority, _client=client)
    assert recorded['input_manifest'] == [DIGEST]
    assert recorded['spec']['authority_context'] == authority


def test_mismatched_video_identity_rejected_before_admission():
    with pytest.raises(CapabilityValidationError, match='identity mismatch'):
        invocation._kernel_invoke(SimpleNamespace(id='rendering.timeline_visualize', capability_type='executor'),
            kind='executor', project='p', outputs={},
            inputs={'rendered_video': {'object_id': DIGEST, 'digest': 'sha256:' + 'c' * 64}},
            idempotency_context={'mode': 'filmstrip', 'video_object_id': DIGEST})


def test_bundle_rehydration_preserves_static_pages_without_html(tmp_path):
    import hashlib
    import io
    import json
    import shutil
    import zipfile
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, 'w') as bundle:
        bundle.writestr('manifest.json', json.dumps({'kind': 'timeline_filmstrip', 'outputs': [
            {'path': name, 'content_hash': 'sha256:' + hashlib.sha256(content).hexdigest(), 'bytes': len(content)}
            for name, content in [('filmstrip-001.png', b'png'), ('filmstrip-001.svg', b'<svg/>'), ('filmstrip.md', b'# Filmstrip'), ('frame-index.json', b'{}')]
        ]}))
        bundle.writestr('filmstrip-001.png', b'png')
        bundle.writestr('filmstrip-001.svg', b'<svg/>')
        bundle.writestr('filmstrip.md', b'# Filmstrip')
        bundle.writestr('frame-index.json', '{}')
    data = archive.getvalue()
    raw = {'outputs': {'artifacts': [{'name': 'filmstrip_bundle', 'digest': 'sha256:' + hashlib.sha256(data).hexdigest(), 'size': len(data)}]}}
    client = SimpleNamespace(media=SimpleNamespace(read_bytes=lambda _: data))
    manifest = invocation._materialize_filmstrip_outputs(
        raw, client, project="demo", cache_root=tmp_path
    )
    try:
        result = invocation._invocation_outputs(raw, manifest_path=manifest, capability_id='rendering.timeline_visualize')
        assert len(result['pages']) == 1
        assert 'svg_pages' not in result
        assert result['markdown'].endswith('filmstrip.md')
        assert 'html' not in result
        assert Path(manifest).parent == tmp_path / "demo" / hashlib.sha256(data).hexdigest()
        assert "/private/tmp/astrid-filmstrip-" not in manifest
    finally:
        shutil.rmtree(raw['outputs']['pack_root'])


def test_bundle_traversal_rejected():
    import hashlib
    import io
    import zipfile
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, 'w') as bundle:
        bundle.writestr('../escape', 'bad')
    data = archive.getvalue()
    raw = {'outputs': {'artifacts': [{'name': 'filmstrip_bundle', 'digest': 'sha256:' + hashlib.sha256(data).hexdigest(), 'size': len(data)}]}}
    client = SimpleNamespace(media=SimpleNamespace(read_bytes=lambda _: data))
    with pytest.raises(invocation.CapabilityInvocationError, match='unsafe'):
        invocation._materialize_filmstrip_outputs(raw, client)


def test_bundle_rehydration_exposes_verified_audio_and_media(tmp_path):
    import hashlib
    import io
    import json
    import shutil
    import zipfile

    members = {
        'filmstrip-001.png': b'png',
        'filmstrip-001.svg': b'<svg/>',
        'filmstrip.md': b'# Filmstrip',
        'audio-analysis.json': b'{"status":"ok"}\n',
        'media/rendered-video.mp4': b'video',
    }
    index = json.dumps({'media': {'path': 'media/rendered-video.mp4'}, 'audio_sidecar': {'path': 'audio-analysis.json'}}).encode()
    members['frame-index.json'] = index
    manifest_members = [
        {'path': name, 'content_hash': 'sha256:' + hashlib.sha256(content).hexdigest(), 'bytes': len(content)}
        for name, content in members.items()
    ]
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, 'w') as bundle:
        bundle.writestr('manifest.json', json.dumps({'kind': 'timeline_filmstrip', 'outputs': manifest_members}))
        for name, content in members.items():
            bundle.writestr(name, content)
    data = archive.getvalue()
    raw = {'outputs': {'artifacts': [{'name': 'filmstrip_bundle', 'digest': 'sha256:' + hashlib.sha256(data).hexdigest(), 'size': len(data)}]}}
    client = SimpleNamespace(media=SimpleNamespace(read_bytes=lambda _: data))
    manifest = invocation._materialize_filmstrip_outputs(
        raw, client, project="demo", cache_root=tmp_path
    )
    try:
        assert raw['outputs']['audio_analysis'].endswith('audio-analysis.json')
        assert raw['outputs']['media'].endswith('rendered-video.mp4')
        assert Path(raw['outputs']['audio_analysis']).read_bytes() == members['audio-analysis.json']
    finally:
        shutil.rmtree(Path(manifest).parent)


def test_structural_view_is_removed():
    with pytest.raises(CapabilityValidationError, match='structural timeline view was removed'):
        invocation._validate_timeline_visualize_inputs({'view': 'structure'}, project='p')


def test_structural_navigation_options_are_removed():
    with pytest.raises(CapabilityValidationError, match='legacy structural visualization options'):
        invocation._validate_timeline_visualize_inputs({'from_view': '/tmp/manifest.json'}, project='p')


def test_invalid_sampling_rejected_before_runtime():
    with pytest.raises(CapabilityValidationError, match='positive'):
        invocation._validate_timeline_visualize_inputs({'view': 'filmstrip', 'every_frames': 0}, project='p')


@pytest.mark.parametrize("address", [{"frame": 29}, {"at": 0.5}, {"range": "0.5..1.5"}])
def test_out_of_range_render_frame_is_a_sdk_validation_error_before_admission(monkeypatch, address):
    snapshot = {
        'fps_rational': [30, 1],
        'duration_frames': 60,
        'clips': [],
        'metadata': {'timeline_range': {
            'start_frame': 30,
            'end_frame': 60,
            'fps_rational': [30, 1],
        }},
    }
    monkeypatch.setattr(
        'astrid.sdk.timeline_filmstrip.prepare_filmstrip',
        lambda *_, **__: {'mode': 'filmstrip', 'filmstrip_snapshot': snapshot},
    )

    with pytest.raises(CapabilityValidationError, match=r'render that range, or ask for a time inside it'):
        invocation._validate_timeline_visualize_inputs(
            {'view': 'filmstrip', 'render_run': 'run-1', **address}, project='p')


@pytest.mark.parametrize("address", [
    {"at": 6.6},
    {"range": "6.6..8.8"},
])
def test_sdk_preflight_accepts_decimal_values_at_render_boundaries(monkeypatch, address):
    snapshot = {
        'fps_rational': [30, 1],
        'duration_frames': 264,
        'clips': [],
        'metadata': {'timeline_range': {
            'start_frame': 198,
            'end_frame': 264,
            'fps_rational': [30, 1],
        }},
    }
    monkeypatch.setattr(
        'astrid.sdk.timeline_filmstrip.prepare_filmstrip',
        lambda *_, **__: {'mode': 'filmstrip', 'filmstrip_snapshot': snapshot},
    )

    invocation._validate_timeline_visualize_inputs(
        {'view': 'filmstrip', 'render_run': 'run-1', **address}, project='p')


def test_sdk_preflight_rejects_decimal_timestamp_at_exclusive_render_end(monkeypatch):
    snapshot = {
        'fps_rational': [30, 1],
        'duration_frames': 198,
        'clips': [],
        'metadata': {'timeline_range': {
            'start_frame': 0,
            'end_frame': 198,
            'fps_rational': [30, 1],
        }},
    }
    monkeypatch.setattr(
        'astrid.sdk.timeline_filmstrip.prepare_filmstrip',
        lambda *_, **__: {'mode': 'filmstrip', 'filmstrip_snapshot': snapshot},
    )

    with pytest.raises(CapabilityValidationError, match='outside the rendered range'):
        invocation._validate_timeline_visualize_inputs(
            {'view': 'filmstrip', 'render_run': 'run-1', 'at': 6.6}, project='p')
