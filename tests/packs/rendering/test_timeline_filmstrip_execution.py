import hashlib
import json
import struct
import subprocess
import wave
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.packs.rendering.actions.timeline_visualize import filmstrip_execution as execution
from astrid.packs.rendering.actions.timeline_visualize.filmstrip_cards import plan_filmstrip
from astrid.packs.rendering.actions.timeline_visualize.filmstrip_options import filmstrip_options
from astrid.packs.rendering.actions.timeline_visualize.inspection_contract import project_input_window


def test_input_only_is_a_materializable_compact_filmstrip_bundle(tmp_path, monkeypatch):
    """Input-only uses the same manifest/receipt contract as the paired view."""
    from astrid.packs.rendering.actions.timeline_visualize import filmstrip_cards

    drawn_text = []
    draw_text = filmstrip_cards._png_draw_text

    def record_text(*args, **kwargs):
        if len(args) >= 3:
            drawn_text.append(str(args[2]))
        return draw_text(*args, **kwargs)

    monkeypatch.setattr(filmstrip_cards, '_png_draw_text', record_text)
    snapshot = {
        'project_slug': 'demo', 'timeline_id': 'main', 'timeline_name': 'Main',
        'render_run_id': 'run-input', 'fps_rational': [30, 1],
        'duration_frames': 120, 'clips': [
            {'id': 'picture-1', 'track': 'picture', 'asset': 'still-a', 'at': 0, 'hold': 2},
            {'id': 'vo-1', 'track': 'vo', 'asset': 'voice-a', 'at': 1, 'hold': 2},
        ], 'tracks': [{'id': 'picture', 'kind': 'visual'}, {'id': 'vo', 'kind': 'audio'}],
        'scripts': [], 'metadata': {
            'selection': 'input_only',
            'authority': 'canonical_parent_revision_closure',
            'parent_revision_id': 'R1',
            'timeline_identity': {'head_event_id': 'R2'},
            'requested_revision_id': 'R2',
        },
    }
    args = Namespace(range_value=None, out=tmp_path / 'output', project_slug='demo')
    result = execution.execute_input_only(
        args, {'input_snapshot': snapshot, 'mode': 'input_only'},
    )

    manifest_path = Path(result['manifest_path'])
    manifest = json.loads(manifest_path.read_text())
    index = json.loads((manifest_path.parent / 'frame-index.json').read_text())
    assert manifest['kind'] == 'timeline_filmstrip'
    assert index['schema'] == 'astrid.filmstrip.v2'
    assert 'navigation' not in index
    assert result['outputs']['pages']
    assert all(Path(page).name.startswith('filmstrip-') for page in result['outputs']['pages'])
    assert result['snapshot_label']['label'] == 'UNCERTIFIED preview (claimed revision R1)'
    assert index['provenance']['revision'] == result['snapshot_label']
    markdown = (manifest_path.parent / 'filmstrip.md').read_text()
    assert '# Input filmstrip · UNCERTIFIED preview (claimed revision R1)' in markdown
    assert 'Revision: `UNCERTIFIED preview (claimed revision R1)`' in markdown
    assert any('UNCERTIFIED preview (claimed revision R1)' in text for text in drawn_text)
    assert (manifest_path.parent / 'render-snapshot.json').is_file()
    assert len(json.dumps(index, separators=(',', ':')).encode()) < 8192

    from astrid.packs.rendering.actions.timeline_visualize.inspection_contract import inspect_filmstrip

    assert inspect_filmstrip(manifest_path, section='summary')['ok']
    placements = inspect_filmstrip(manifest_path, section='placements')
    assert placements['ok']
    assert {row['id'] for row in placements['data']['records']} == {'picture-1', 'vo-1'}
    audio = inspect_filmstrip(manifest_path, section='audio')
    assert audio['ok']
    assert audio['data']['records'][0]['status'] == 'not_available'
    pages = inspect_filmstrip(manifest_path, section='pages')
    assert pages['ok'] and pages['data']['records']

    bundle_bytes = Path(result['outputs']['filmstrip_bundle']).read_bytes()
    raw = {'outputs': {'artifacts': [{
        'name': 'filmstrip_bundle',
        'digest': 'sha256:' + hashlib.sha256(bundle_bytes).hexdigest(),
        'size': len(bundle_bytes),
    }]}}
    client = SimpleNamespace(media=SimpleNamespace(read_bytes=lambda _: bundle_bytes))
    from astrid.sdk.invocation import _materialize_filmstrip_outputs

    hydrated = _materialize_filmstrip_outputs(raw, client, project='demo', cache_root=tmp_path / 'cache')
    assert Path(hydrated).is_file()
    assert json.loads(Path(hydrated).read_text())['kind'] == 'timeline_filmstrip'


@pytest.mark.parametrize('include_media', [False, True])
@pytest.mark.parametrize('show_inputs', [False, True])
def test_composed_capture_uses_remotion_for_requested_frames_without_video_frame_source(
    tmp_path, monkeypatch, include_media, show_inputs,
):
    from PIL import Image
    from astrid.packs.rendering.actions.timeline_visualize import composed_frame

    timeline_path = tmp_path / 'saved-timeline.json'
    assets_path = tmp_path / 'saved-assets.json'
    timeline_path.write_text('{}')
    assets_path.write_text('{"assets":{}}')
    materialized_root = tmp_path / 'materialized'
    materialized_root.mkdir()
    materialized_objects = {'sha256:' + 'b' * 64: str(materialized_root / 'asset')}
    providers = []

    class _FrameProvider:
        def __init__(self, snapshot, **kwargs):
            self.snapshot = snapshot
            self.kwargs = kwargs
            self.requested = []
            providers.append(self)

        def capture(self, cards, out_root, resolution):
            self.requested = [int(card['frame']) for card in cards]
            for card in cards:
                destination = Path(out_root) / str(card['image'])
                destination.parent.mkdir(parents=True, exist_ok=True)
                Image.new('RGB', (64, 36), '#224466').save(destination)
            return {
                'evidence_source': 'fresh_capture',
                'requested_frames': list(self.requested),
                'resolution': resolution,
            }

        def close(self, *, force=False):
            return None

    monkeypatch.setattr(composed_frame, 'RemotionFrameProvider', _FrameProvider)
    revision_id = 'saved-revision-7'
    capture_identity = 'sha256:' + 'c' * 64
    attached_video = tmp_path / 'matching-render.mp4'
    attached_video.write_bytes(b'managed video attachment')
    attached_video_digest = 'sha256:' + hashlib.sha256(attached_video.read_bytes()).hexdigest()
    snapshot = {
        'project_slug': 'demo', 'timeline_id': 'main', 'timeline_name': 'Main',
        'render_run_id': f'composed-capture:main:{revision_id}',
        'video_digest': capture_identity, 'fps_rational': [24, 1],
        'duration_frames': 48, 'clips': [], 'tracks': [], 'scripts': [],
        'occurrences': [], 'registry': {'assets': {}},
        'metadata': {
            'selection': 'composed_frame_capture',
            'capture_authority': {'timeline_id': 'main', 'parent_revision_id': revision_id},
            'capture_identity': capture_identity,
        },
    }
    # A real canonical address in the snapshot makes the outer export, not
    # just build_filmstrip_pack's temporary receipt, the tested boundary.
    from astrid.sdk import timeline_intent as intent
    clip = {'id': 'visible-title', 'clipType': 'text-card', 'track': 'picture',
            'at': 0, 'hold': 2, 'text': {'content': 'Visible'}, 'opacity': 1}
    intent.set_cut(clip, 'c1')
    intent.set_layer(clip, 'title')
    snapshot['config'] = {
        'tracks': [{'id': 'picture', 'kind': 'visual'}], 'clips': [clip],
        'theme_overrides': {'visual': {'canvas': {'width': 64, 'height': 36, 'fps': 24}}},
    }
    snapshot['tracks'] = snapshot['config']['tracks']
    snapshot['clips'] = [{**clip, 'kind': 'visual', 'duration': 2,
                          'start_frame': 0, 'end_frame': 48}]
    authority = {
        'mode': 'filmstrip' if include_media else 'composed_capture',
        'capture_snapshot': snapshot, 'capture_identity': capture_identity,
        'include_media': include_media,
        **({'frame_source': 'composed_capture', 'video_digest': attached_video_digest,
            'video_object_id': attached_video_digest, 'render_run_id': 'matching-render'}
           if include_media else {}),
    }
    args = Namespace(
        filmstrip_authority=json.dumps(authority), project_slug='demo',
        range_value=None, out=tmp_path / 'output', include_media=include_media,
        timeline=timeline_path, assets_registry=assets_path,
        materialized_root=materialized_root, materialized_objects=materialized_objects,
        frame=7, show=['output', 'inputs'] if show_inputs else ['output'],
        **({'rendered_video': attached_video} if include_media else {}),
    )
    if not include_media:
        assert not hasattr(args, 'rendered_video')

    result = execution.execute_filmstrip(args)

    assert result['returncode'] == 0
    import zipfile
    # Verify the actual published ZIP bytes and its manifest-covered final
    # frame-index, after paired/output-only rewriting and atomic packaging.
    with zipfile.ZipFile(result['outputs']['filmstrip_bundle']) as archive:
        exported = archive.read('frame-index.json')
        final_index = json.loads(exported)
        manifest = json.loads(archive.read('manifest.json'))
        member = next(row for row in manifest['outputs'] if row['path'] == 'frame-index.json')
        assert member['bytes'] == len(exported)
        assert member['content_hash'] == 'sha256:' + hashlib.sha256(exported).hexdigest()
        assert len(final_index['cards']) == 1
        assert final_index['cards'][0]['frame'] == 7
        assert final_index['cards'][0]['addresses'] == ['c1.title']
        assert final_index['provenance']['frame_capture']['evidence_source'] == 'fresh_capture'
        assert archive.read('frames/frame-000000007.png').startswith(b'\x89PNG\r\n\x1a\n')
    if show_inputs:
        assert final_index['static_surface']['mode'] == 'paired_rows'
    assert providers[0].requested == [7]
    assert providers[0].snapshot['video_digest'] == capture_identity
    assert attached_video_digest not in json.dumps(providers[0].snapshot)
    assert providers[0].snapshot['metadata']['capture_authority']['parent_revision_id'] == revision_id
    assert providers[0].kwargs['timeline_path'] == timeline_path
    assert providers[0].kwargs['assets_path'] == assets_path
    assert providers[0].kwargs['materialized_objects'] == materialized_objects
    assert 'rendered_video' not in result['cas']
    if include_media:
        frame_index = json.loads((Path(result['outputs']['pack_root']) / 'frame-index.json').read_text())
        assert frame_index['media']['digest'] == attached_video_digest
        assert frame_index['media']['source_digest'] == attached_video_digest
        assert frame_index['media']['render_run_id'] == 'matching-render'
        assert frame_index['provenance']['render_run_id'] == f'composed-capture:main:{revision_id}'
        # Use the real producer and compact serializer's receipt through the
        # actual public facade and FastLane consumer, never a hand-built index.
        from astrid.sdk.client import AstridClient
        from astrid.sdk.remote import RemoteAstridClient
        from astrid.packs.rendering.actions.timeline_visualize import fast_lane
        from tests.sdk.test_native_timeline_facades import _NativeTransport
        import astrid.sdk.invocation as invocation
        remote = RemoteAstridClient(_NativeTransport())
        client = AstridClient(remote=remote)
        actual_media = Path(result['outputs']['pack_root']) / 'media/rendered-video.mp4'
        actual_index = Path(result['outputs']['pack_root']) / 'frame-index.json'
        calls = []
        def dispatch(capability_id, **kwargs):
            calls.append((capability_id, kwargs))
            return SimpleNamespace(ok=True, outputs={'media': str(actual_media), 'frame_index': str(actual_index)})
        monkeypatch.setattr(invocation, '_invoke_internal_result', dispatch)
        assert fast_lane._invoke_video(client, 'demo', 'main', 'matching-render', 7) == actual_media
        assert calls[0][1]['inputs']['render_run'] == 'matching-render'
        assert calls[0][1]['inputs']['composed_capture'] is True
        assert (Path(result['outputs']['pack_root']) / 'media/rendered-video.mp4').is_file()


def test_bundle_publication_keeps_previous_complete_bundle_on_archive_failure(
    tmp_path, monkeypatch
):
    """A failed archive build cannot advertise a partial replacement."""
    source_root = tmp_path / 'filmstrip-view'
    source_root.mkdir()
    (source_root / 'manifest.json').write_text('{"complete":true}\n')
    destination = tmp_path / 'filmstrip-bundle.zip'
    destination.write_bytes(b'previous-complete-bundle')

    def fail_write(self, filename, arcname=None, compress_type=None):
        raise OSError('synthetic archive failure')

    monkeypatch.setattr(execution.zipfile.ZipFile, 'write', fail_write)
    with pytest.raises(OSError, match='synthetic archive failure'):
        execution._write_zip_atomic(destination, source_root)

    assert destination.read_bytes() == b'previous-complete-bundle'
    assert not list(tmp_path.glob('.filmstrip-bundle.zip.*.tmp'))


def test_managed_coverage_projects_actual_overview_source_pack():
    source = plan_filmstrip(
        {
            'fps_rational': [24, 1],
            'duration_frames': 48,
            'clips': [],
            'occurrences': [],
            'scripts': [],
            'metadata': {},
        },
        filmstrip_options({}),
    )

    managed = execution._filmstrip_managed_coverage(source)

    assert managed == {
        'sampling': {
            'mode': 'interval',
            'range': {'start': 0, 'end': 48},
            'step_frames_rational': {'numerator': 12, 'denominator': 1},
            'every': 0.5,
        },
    }


def test_managed_coverage_keeps_exact_frame_in_frozen_range(tmp_path):
    source = plan_filmstrip(
        {
            'fps_rational': [24, 1],
            'duration_frames': 48,
            'clips': [],
            'occurrences': [],
            'scripts': [],
            'metadata': {},
        },
        filmstrip_options({'frame': 7}),
    )

    managed = execution._filmstrip_managed_coverage(source)

    assert managed['sampling']['range'] == {'start': 7, 'end': 8}
    assert 'frame' not in managed['sampling']


def test_input_projection_filters_pinned_shot_alias_membership():
    projection = project_input_window(
        [
            {'id': 'clip-a', 'track': 'picture', 'asset': 'a', 'at': 0, 'hold': 2},
            {'id': 'clip-b', 'track': 'picture', 'asset': 'b', 'at': 2, 'hold': 2},
        ],
        start_frame=0, end_frame=120, fps=30,
        shot_id='shot-b',
        shot_groups=[
            {'shotId': 'shot-a', 'clipIds': ['clip-a'], 'name': 'Opening'},
            {'shotId': 'shot-b', 'clipIds': ['clip-b'], 'name': 'Middle'},
        ],
    )
    clips = projection['tracks'][0]['clips']
    assert [clip['clip_id'] for clip in clips] == ['clip-b']
    assert clips[0]['shot_id'] == 'shot-b'
    assert clips[0]['shot_name'] == 'Middle'


def test_input_audio_rail_is_prominent_but_remains_timing_only(tmp_path):
    from PIL import Image

    projection = {
        'window': {'fps': [30, 1], 'start_frame': 0, 'end_frame': 150},
        'tracks': [{
            'track_id': 'vo',
            'clips': [{
                'clip_id': 'vo_b01',
                'window': [0, 150],
                'source_preview': {'status': 'verified', 'media_type': 'audio'},
                'audio_signifier': {'present': True, 'basis': 'clip_placement'},
            }],
        }],
        'track_bands': [{'track_ids': ['vo'], 'label': 'voice'}],
    }
    paths = execution._render_input_projection_png(
        projection, {'timeline_name': 'Main'}, tmp_path, width=600,
    )
    with Image.open(paths[0]) as image:
        pixels = list(image.getdata())
        # The boosted rail and markers are intentionally much more visible than
        # the old one-pixel amber line, while still encoding placement timing.
        assert pixels.count((255, 192, 120)) >= 150
        assert pixels.count((255, 224, 168)) >= 30
        marker_rows = [
            y for y in range(image.height)
            if any(image.getpixel((x, y)) == (255, 224, 168) for x in range(image.width))
        ]
        # The 56px audio placement starts at y=186; marker extents are
        # balanced around its vertical midpoint (y=214), rather than being
        # anchored against the old bottom-biased rail.
        assert marker_rows and min(marker_rows) + max(marker_rows) == 2 * 214
        # The centered rail passes behind the clip label, which is protected by
        # a dark rounded chip for contrast.
        assert image.getpixel((80, 212)) == (16, 22, 29)


@pytest.mark.ffmpeg
def test_input_audio_visualizer_uses_verified_source_amplitude(tmp_path):
    source = tmp_path / 'source.wav'
    frames = [0.8] * 500 + [0.05] * 500
    with wave.open(str(source), 'wb') as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(1000)
        output.writeframes(b''.join(struct.pack('<h', int(value * 32767)) for value in frames))
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    projection = {
        'window': {'fps': [30, 1], 'start_frame': 0, 'end_frame': 30},
        'tracks': [{
            'track_id': 'vo',
            'clips': [{
                'clip_id': 'vo-source', 'asset_key': 'voice',
                'window': [0, 30], 'window_seconds': [[0, 1], [1, 1]],
                'source_time': [[0, 1], [1, 1]],
                'source_preview': {'status': 'verified', 'media_type': 'audio/wav'},
                'audio_signifier': {'present': True},
            }],
        }],
        'track_bands': [{'track_ids': ['vo'], 'label': 'voice'}],
    }
    execution.attach_input_audio_waveforms(
        projection,
        integrity={'voice': {
            'state': 'verified_original', 'path': str(source),
            'observed_sha256': digest,
        }},
        out_root=tmp_path,
        count=32,
    )
    signifier = projection['tracks'][0]['clips'][0]['audio_signifier']
    assert signifier['basis'] == 'source_audio_analysis'
    assert signifier['visual_encoding'] == 'amplitude_waveform'
    measured = signifier['waveform']['amplitudes']
    assert measured[0] > measured[-1] * 5
    assert signifier['analysis_path'].startswith('source-audio-analysis/')
    assert (tmp_path / signifier['analysis_path']).is_file()

    paths = execution._render_input_projection_png(
        projection, {'timeline_name': 'Main'}, tmp_path, width=600,
    )
    from PIL import Image
    with Image.open(paths[0]) as image:
        mint = (143, 246, 221)
        mint_rows = {
            y for y in range(image.height)
            if any(image.getpixel((x, y)) == mint for x in range(image.width))
        }
    assert mint_rows and min(mint_rows) < 200 < max(mint_rows)


@pytest.mark.ffmpeg
def test_input_audio_visualizer_discovers_audio_embedded_in_verified_video(tmp_path):
    source = tmp_path / 'source.mp4'
    subprocess.run([
        'ffmpeg', '-loglevel', 'error', '-f', 'lavfi', '-i',
        'color=blue:size=32x32:rate=4:duration=1', '-f', 'lavfi', '-i',
        'sine=frequency=440:sample_rate=8000:duration=1', '-c:v', 'libx264',
        '-c:a', 'aac', '-shortest', '-y', str(source),
    ], check=True)
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    projection = {
        'window': {'fps': [4, 1], 'start_frame': 0, 'end_frame': 4},
        'tracks': [{
            'track_id': 'picture',
            'clips': [{
                'clip_id': 'picture-source', 'asset_key': 'picture',
                'window': [0, 4], 'window_seconds': [[0, 1], [1, 1]],
                'source_time': [[0, 1], [1, 1]],
                'source_preview': {'status': 'verified', 'media_type': 'video/mp4'},
            }],
        }],
        'track_bands': [{'track_ids': ['picture'], 'label': 'pictures'}],
    }
    execution.attach_input_audio_waveforms(
        projection,
        integrity={'picture': {
            'state': 'verified_original', 'path': str(source),
            'observed_sha256': digest,
        }},
        out_root=tmp_path,
        count=16,
    )
    signifier = projection['tracks'][0]['clips'][0].get('audio_signifier')
    assert signifier is not None
    assert signifier['basis'] == 'source_audio_analysis'
    assert max(signifier['waveform']['amplitudes']) > 0.1


def test_alignment_uses_decoded_eof_and_marks_excess_tail_unmapped(monkeypatch, tmp_path):
    snapshot = {
        'fps_rational': [24, 1], 'duration_frames': 72,
        'clips': [
            {'id': 'authored', 'at': 0, 'duration': 3, 'start_frame': 0, 'end_frame': 72,
             'shot_id': 'unproven', 'shot_name': 'must not reach tail'},
        ],
        'occurrences': [{'occurrence_id': 'shot-occ', 'shot_id': 'shot',
                         'shot_name': 'Shot', 'start': 0, 'end': 3,
                         'start_frame': 0, 'end_frame': 72}],
        'scripts': [{'start': 0, 'end': 3, 'text': 'line'}],
        'metadata': {},
    }
    monkeypatch.setattr(execution, '_rendered_timing', lambda video, fps: (120, 5.0))

    execution._align_snapshot_to_render(snapshot, tmp_path / 'render.mp4')

    assert snapshot['duration_frames'] == 120
    assert snapshot['metadata']['duration_basis'] == 'rendered_video'
    assert snapshot['metadata']['rendered_duration_seconds'] == 5.0
    assert snapshot['metadata']['authored_duration_seconds'] == 3.0
    tail = snapshot['metadata']['rendered_tail']
    assert tail == snapshot['unmapped_regions'][0]
    assert tail['status'] == tail['mapping_status'] == 'unmapped'
    assert (tail['start_frame'], tail['end_frame']) == (72, 120)
    tail_clip = snapshot['clips'][-1]
    assert tail_clip['render_tail'] is True
    assert tail_clip['mapping_status'] == 'unmapped'
    assert tail_clip['end_frame'] == 120
    assert 'shot_id' not in tail_clip and 'shot_name' not in tail_clip
    assert snapshot['occurrences'][0]['end_frame'] == 72


def test_alignment_clamps_short_render_without_changing_identity(monkeypatch, tmp_path):
    snapshot = {'fps_rational': [24, 1], 'duration_frames': 120,
                'clips': [{'id': 'clip', 'at': 0, 'duration': 5,
                           'start_frame': 0, 'end_frame': 120}],
                'occurrences': [], 'scripts': [], 'metadata': {}}
    monkeypatch.setattr(execution, '_rendered_timing', lambda video, fps: (72, 3.0))

    execution._align_snapshot_to_render(snapshot, tmp_path / 'render.mp4')

    assert snapshot['duration_frames'] == 72
    assert snapshot['unmapped_regions'] == []
    assert snapshot['metadata']['rendered_tail'] is None
    assert snapshot['clips'][0]['end_frame'] == 72


def test_alignment_preserves_declared_authored_clock_when_snapshot_duration_is_rendered(
    monkeypatch, tmp_path
):
    snapshot = {
        'fps_rational': [30, 1], 'duration_frames': 9000,
        'clips': [{'id': 'clip', 'start_frame': 0, 'end_frame': 8910}],
        'occurrences': [], 'scripts': [],
        'metadata': {'authored_duration_frames': 8910},
    }
    monkeypatch.setattr(execution, '_rendered_timing', lambda video, fps: (9000, 300.0))

    execution._align_snapshot_to_render(snapshot, tmp_path / 'render.mp4')

    assert snapshot['duration_frames'] == 9000
    assert snapshot['metadata']['authored_duration_frames'] == 8910
    assert snapshot['metadata']['rendered_tail']['start_frame'] == 8910


def test_managed_execution_verifies_video_before_extracting(tmp_path, monkeypatch):
    video = tmp_path / 'video'
    video.write_bytes(b'actual video')
    snapshot = dict(project_slug='demo', timeline_id='main', render_run_id='run',
                    video_digest='sha256:' + hashlib.sha256(video.read_bytes()).hexdigest(),
                    fps_rational=[24, 1])
    authority = dict(mode='filmstrip', filmstrip_snapshot=snapshot,
                     video_digest=snapshot['video_digest'])
    args = Namespace(filmstrip_authority=json.dumps(authority), project_slug='demo',
                     rendered_video=video, range_value=None, out=tmp_path / 'output')
    called = []
    def build(**kwargs):
        called.append(kwargs)
        root = kwargs['out_root']
        root.mkdir(parents=True)
        (root / 'filmstrip-001.png').write_bytes(b'png')
        (root / 'filmstrip-001.svg').write_text('<svg/>')
        (root / 'filmstrip.md').write_text('# Filmstrip')
        (root / 'frame-index.json').write_text('{}')
        return {
            'frame_index': {
                'provenance': {'fps_rational': [24, 1]},
                'coverage': {'window_seconds': [0.0, 1.0]},
                'sampling': {
                    'mode': 'interval',
                    'density': {'mode': 'every_seconds', 'value': 0.5},
                    'step_frames_rational': [12, 1],
                },
                'cards': [
                    {
                        'frame': 0,
                        'time_seconds': 0.0,
                        'time_rational': [0, 1],
                        'sample_reasons': ['interval'],
                    },
                ],
            },
            'paths': {
                'png': [str(root / 'filmstrip-001.png')],
                'svg': [str(root / 'filmstrip-001.svg')],
                'markdown': str(root / 'filmstrip.md'),
                'json': str(root / 'frame-index.json'),
            },
        }
    monkeypatch.setattr(execution, 'build_filmstrip_pack', build)
    result = execution.execute_filmstrip(args)
    manifest = json.loads(open(result['manifest_path']).read())
    assert manifest['kind'] == 'timeline_filmstrip'
    assert [o['path'] for o in manifest['outputs'] if o['is_primary']] == ['filmstrip-001.png']
    assert 'filmstrip.html' not in {o['path'] for o in manifest['outputs']}
    host_manifest = json.loads((tmp_path / 'output' / 'manifest.json').read_text())
    assert host_manifest['kind'] == 'timeline_filmstrip_result'
    assert {entry['name'] for entry in host_manifest['outputs']} == {
        'filmstrip_manifest', 'filmstrip_bundle'
    }
    assert all('content_hash' in entry and 'bytes' in entry for entry in host_manifest['outputs'])
    bundle_entry = next(entry for entry in host_manifest['outputs'] if entry['name'] == 'filmstrip_bundle')
    manifest_entry = next(entry for entry in host_manifest['outputs'] if entry['name'] == 'filmstrip_manifest')
    assert bundle_entry['path'] == 'filmstrip-bundle.zip'
    assert bundle_entry['is_primary'] is True
    assert bundle_entry['durability'] == 'durable'
    assert manifest_entry['is_primary'] is False
    assert manifest_entry['durability'] == 'temporary'
    expected_coverage = {
        'sampling': {
            'mode': 'interval',
            'range': {'start': 0, 'end': 24},
            'step_frames_rational': {'numerator': 12, 'denominator': 1},
            'every': 0.5,
            'cards': [{
                'frame': 0,
                'time_seconds': 0.0,
                'time_rational': {'numerator': 0, 'denominator': 1},
                'sample_reasons': ['interval'],
            }],
        },
    }
    for entry in host_manifest['outputs']:
        assert entry['producer'] == {'capability_id': 'rendering.timeline_visualize', 'view': 'filmstrip'}
        assert entry['provenance'] == {
            'render_run_id': 'run', 'timeline_id': 'main',
            'video_digest': snapshot['video_digest'],
        }
        regeneration = entry['regeneration']
        assert regeneration['available'] is True
        assert regeneration['capability_id'] == 'rendering.timeline_visualize'
        assert regeneration['source_refs'] == [snapshot['video_digest']]
        assert regeneration['exact_inputs']['render_run_id'] == 'run'
        assert regeneration['exact_inputs']['timeline_id'] == 'main'
        assert regeneration['exact_inputs']['video_digest'] == snapshot['video_digest']
        assert regeneration['recipe_digest'].startswith('sha256:')
        assert entry['coverage'] == expected_coverage
    assert result['identity']['render'] == {
        'render_run_id': 'run', 'timeline_id': 'main',
        'video_digest': snapshot['video_digest'],
    }
    assert result['cas']['rendered_video'] == snapshot['video_digest']
    assert result['identity']['manifest']['content_hash'] == (
        'sha256:' + hashlib.sha256(Path(result['manifest_path']).read_bytes()).hexdigest()
    )
    assert result['entrypoints'] == {
        'manifest': 'filmstrip-view/manifest.json',
        'frame_index': 'filmstrip-view/frame-index.json',
        'png': 'filmstrip-view/filmstrip-001.png',
        'markdown': 'filmstrip-view/filmstrip.md',
        'bundle': 'filmstrip-bundle.zip',
    }
    assert not any(key in host_manifest for key in ('mutation', 'mutation_receipt', 'events'))
    assert called[0]['snapshot'] == snapshot
    video.write_bytes(b'changed')
    with pytest.raises(ValueError, match='digest'):
        execution.execute_filmstrip(args)
    assert len(called) == 1


def test_execution_requires_admitted_authority(tmp_path):
    with pytest.raises(ValueError, match='admission'):
        execution.execute_filmstrip(Namespace(filmstrip_authority=None))


def test_synchronized_surface_composes_output_and_input_panels(tmp_path):
    from PIL import Image

    output = tmp_path / 'filmstrip-001.png'
    inputs = tmp_path / 'input-band-001.png'
    Image.new('RGB', (400, 200), '#101827').save(output)
    Image.new('RGB', (300, 100), '#202f3d').save(inputs)

    pages = execution._compose_synchronized_surface(
        [str(output)], [inputs], pack_root=tmp_path,
    )

    assert pages == [str(output)]
    assert (tmp_path / 'rendered-filmstrip-001.png').is_file()
    with Image.open(output) as image:
        assert image.size == (400, 353)
        assert image.getpixel((200, 201)) == (11, 17, 24)


def test_synchronized_surface_anchors_samples_to_shared_time_coordinates(tmp_path):
    from PIL import Image

    output = tmp_path / 'filmstrip-001.png'
    inputs = tmp_path / 'input-band-001.png'
    Image.new('RGB', (400, 200), '#101827').save(output)
    Image.new('RGB', (1500, 20), '#202f3d').save(inputs)
    Image.new('RGB', (1200, 675), '#c0392b').save(tmp_path / 'frame-0.png')
    Image.new('RGB', (1200, 675), '#27ae60').save(tmp_path / 'frame-5.png')
    Image.new('RGB', (1200, 675), '#2980b9').save(tmp_path / 'frame-10.png')
    Image.new('RGB', (1200, 675), '#8e44ad').save(tmp_path / 'frame-15.png')

    frame_index = {
        'provenance': {'timeline_name': 'Main', 'timeline_id': 'tl', 'render_run_id': 'run', 'fps_rational': [30, 1], 'duration_frames': 600},
        'input_projection': {'window': {'start_frame': 0, 'end_frame': 600}},
        'cards': [
            {'id': 'f0', 'time_seconds': 0.0, 'time_label': '0.000s', 'image': 'frame-0.png', 'clips': []},
            {'id': 'f5', 'time_seconds': 5.0, 'time_label': '5.000s', 'image': 'frame-5.png', 'clips': []},
            {'id': 'f10', 'time_seconds': 10.0, 'time_label': '10.000s', 'image': 'frame-10.png', 'clips': []},
            {'id': 'f15', 'time_seconds': 15.0, 'time_label': '15.000s', 'image': 'frame-15.png', 'clips': []},
        ],
    }

    pages = execution._compose_synchronized_surface(
        [str(output)], [inputs], pack_root=tmp_path,
        frame_index=frame_index, options={'page_size': 50},
    )

    assert pages == [str(output)]
    with Image.open(output) as image:
        # The 5s card starts at the same x-coordinate as the 5s ruler tick:
        # 24 + 5/20 * (1476 - 24) = 387px.
        assert image.getpixel((50, 226)) == (192, 57, 43)
        assert image.getpixel((400, 226)) == (39, 174, 96)


def test_paired_surface_groups_five_samples_and_clips_input_rows(tmp_path):
    from PIL import Image

    output = tmp_path / 'filmstrip-001.png'
    inputs = tmp_path / 'input-band-001.png'
    Image.new('RGB', (400, 200), '#101827').save(output)
    Image.new('RGB', (1500, 20), '#202f3d').save(inputs)
    cards = []
    for index, seconds in enumerate(range(0, 120, 5)):
        frame = tmp_path / 'frames' / f'frame-{index:03d}.jpg'
        frame.parent.mkdir(exist_ok=True)
        Image.new('RGB', (160, 90), (index * 7 % 255, 40, 80)).save(frame)
        cards.append({'id': f'f{index}', 'time_seconds': float(seconds),
                      'time_label': f'{seconds:.3f}s',
                      'image': str(frame.relative_to(tmp_path)), 'clips': [],
                      'captions': [], 'display_scripts': []})
    frame_index = {
        'provenance': {'timeline_name': 'Main', 'timeline_id': 'tl',
                       'render_run_id': 'run', 'fps_rational': [30, 1]},
        'input_projection': {
            'window': {'start_frame': 0, 'end_frame': 3512, 'fps': [30, 1]},
            'tracks': [{'track_id': 'picture', 'clips': [
                {'clip_id': 'pic', 'window': [0, 3512],
                 'source_preview': {'status': 'unavailable'}}]}],
            'track_bands': [{'track_ids': ['picture'], 'label': 'pictures'}],
        },
        'cards': cards, 'components': ['output', 'inputs', 'text', 'audio'],
    }

    pages = execution._compose_synchronized_surface(
        [str(output)], [inputs], pack_root=tmp_path,
        frame_index=frame_index, options={'page_size': 50, 'columns': 5, 'page_size_explicit': False},
    )

    assert len(pages) == 5
    assert pages[0] == str(output)
    surface = frame_index['static_surface']
    assert surface['mode'] == 'paired_rows'
    assert surface['page_size'] == 5
    assert surface['page_count'] == 5
    assert surface['row_count'] == 5
    assert [row['output_card_count'] for row in surface['rows']] == [5, 5, 5, 5, 4]
    assert surface['rows'][0]['time_range'] == [0.0, 25.0]
    assert surface['rows'][-1]['time_range'] == [100.0, 117.06666666666666]
    assert not list(tmp_path.glob('filmstrip-*.svg'))


def test_paired_surface_page_size_respects_smaller_request_and_sparse_card_fills_row(tmp_path):
    from PIL import Image

    output = tmp_path / 'filmstrip-001.png'
    inputs = tmp_path / 'input-band-001.png'
    Image.new('RGB', (400, 200), '#101827').save(output)
    Image.new('RGB', (1500, 20), '#202f3d').save(inputs)
    frame = tmp_path / 'frame.jpg'
    Image.new('RGB', (160, 90), '#c0392b').save(frame)
    frame_index = {
        'provenance': {'timeline_name': 'Main', 'timeline_id': 'tl', 'render_run_id': 'run', 'fps_rational': [30, 1]},
        'input_projection': {
            'window': {'start_frame': 0, 'end_frame': 150, 'fps': [30, 1]},
            'tracks': [{'track_id': 'picture', 'clips': [{'clip_id': 'pic', 'window': [0, 150]}]}],
            'track_bands': [{'track_ids': ['picture'], 'label': 'pictures'}],
        },
        'cards': [{'id': 'f0', 'time_seconds': 0.0, 'time_label': '0.000s',
                   'image': 'frame.jpg', 'clips': [], 'captions': [], 'display_scripts': []}],
        'components': ['output', 'inputs', 'text', 'audio'],
    }
    pages = execution._compose_synchronized_surface(
        [str(output)], [inputs], pack_root=tmp_path, frame_index=frame_index,
        options={'page_size': 3, 'columns': 5},
    )
    assert pages == [str(output)]
    surface = frame_index['static_surface']
    assert surface['page_size'] == 3
    assert surface['page_count'] == 1
    assert surface['rows'][0]['time_range'] == [0.0, 5.0]
    # The single red thumbnail is centered in a full-width card, well beyond
    # the first fifth of the row that the old fixed-slot layout used.
    with Image.open(output) as image:
        red_x = [x for y in range(image.height) for x in range(image.width)
                 if (lambda rgb: rgb[0] > 140 and rgb[1] < 100 and rgb[2] < 120)(image.getpixel((x, y)))]
        assert red_x and min(red_x) > 500


def test_paired_surface_centers_spoken_text_in_card_body(tmp_path, monkeypatch):
    from PIL import Image
    from astrid.packs.rendering.actions.timeline_visualize import filmstrip_cards

    output = tmp_path / 'filmstrip-001.png'
    inputs = tmp_path / 'input-band-001.png'
    Image.new('RGB', (400, 200), '#101827').save(output)
    Image.new('RGB', (1500, 20), '#202f3d').save(inputs)
    frame = tmp_path / 'frame.jpg'
    Image.new('RGB', (160, 90), '#c0392b').save(frame)
    frame_index = {
        'provenance': {'timeline_name': 'Main', 'timeline_id': 'tl', 'render_run_id': 'run', 'fps_rational': [30, 1]},
        'input_projection': {
            'window': {'start_frame': 0, 'end_frame': 150, 'fps': [30, 1]},
            'tracks': [{'track_id': 'picture', 'clips': [{'clip_id': 'pic', 'window': [0, 150]}]}],
            'track_bands': [{'track_ids': ['picture'], 'label': 'pictures'}],
        },
        'cards': [{'id': 'f0', 'time_seconds': 0.0, 'time_label': '0.000s',
                   'image': 'frame.jpg', 'clips': [], 'captions': [{'text': 'A centered spoken line'}],
                   'display_scripts': []}],
        'components': ['output', 'inputs', 'text'],
    }
    calls = []
    original = filmstrip_cards._png_draw_text

    def spy(draw, xy, text, *args, **kwargs):
        if str(text).startswith('“'):
            calls.append((xy, str(text)))
        return original(draw, xy, text, *args, **kwargs)

    monkeypatch.setattr(filmstrip_cards, '_png_draw_text', spy)
    execution._compose_synchronized_surface(
        [str(output)], [inputs], pack_root=tmp_path, frame_index=frame_index,
        options={'page_size': 3, 'columns': 5},
    )
    assert len(calls) == 1
    # One card owns the full 0–5s row. Its caption is centered in the wide
    # body rather than starting at the card's left padding or bottom edge.
    (x, y), _ = calls[0]
    assert x > 500
    # The 2:1 fixture frame reaches the 320px preview cap, so the caption block
    # sits below the preview (header 52+54, preview 320, gap 8) and is centred
    # in the panel that follows, not pinned to the card's bottom edge.
    preview_bottom = 52 + 54 + 320
    assert preview_bottom + 8 <= y <= preview_bottom + 48


def test_paired_surface_omits_empty_lanes_but_preserves_canonical_track_metadata(tmp_path):
    from PIL import Image

    output = tmp_path / 'filmstrip-001.png'
    inputs = tmp_path / 'input-band-001.png'
    Image.new('RGB', (400, 200), '#101827').save(output)
    Image.new('RGB', (1500, 20), '#202f3d').save(inputs)
    frame_index = {
        'provenance': {'timeline_name': 'Main', 'timeline_id': 'tl', 'render_run_id': 'run', 'fps_rational': [30, 1]},
        'input_projection': {
            'window': {'start_frame': 0, 'end_frame': 150, 'fps': [30, 1]},
            'tracks': [
                {'track_id': 'picture', 'clips': [{'clip_id': 'pic', 'window': [0, 150]}]},
                {'track_id': 'terminal_background', 'clips': []},
            ],
            'track_bands': [{'track_ids': ['picture', 'terminal_background'], 'label': 'tracks'}],
        },
        'cards': [{'id': 'f0', 'time_seconds': 0.0, 'time_label': '0.000s', 'image': '', 'clips': []}],
        'components': ['output', 'inputs'],
    }
    execution._compose_synchronized_surface(
        [str(output)], [inputs], pack_root=tmp_path, frame_index=frame_index,
        options={'page_size': 3, 'columns': 5},
    )
    surface = frame_index['static_surface']
    assert surface['canonical_input_tracks'] == ['picture', 'terminal_background']
    assert surface['rows'][0]['input_tracks'] == ['picture']
    assert 'terminal_background' not in surface['rows'][0]['input_tracks']


def test_paired_navigation_describes_pages_and_drill_down():
    from astrid.packs.rendering.actions.timeline_visualize.filmstrip_cards import _navigation_usage

    navigation = _navigation_usage(
        {'project_slug': 'demo', 'timeline_id': 'main', 'render_run_id': 'run'},
        {'components': ['output', 'text', 'audio', 'inputs'], 'columns': 5, 'page_size': 50, 'page_size_explicit': False},
    )
    assert any('one row' in note for note in navigation['notes'])
    assert any('numbered PNG pages' in note for note in navigation['keyboard'])
    assert '--page-size' not in navigation['commands']['rerun_base']


@pytest.mark.ffmpeg
def test_execution_delivers_audio_sidecar_optional_media_and_reuses_cache(tmp_path, monkeypatch):
    video = tmp_path / 'video.mp4'
    subprocess.run([
        'ffmpeg', '-loglevel', 'error', '-f', 'lavfi', '-i', 'color=blue:size=64x64:rate=4:duration=1',
        '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=8000:duration=1',
        '-c:v', 'libx264', '-c:a', 'aac', '-shortest', '-y', str(video)
    ], check=True)
    digest = 'sha256:' + hashlib.sha256(video.read_bytes()).hexdigest()
    snapshot = dict(project_slug='demo', timeline_id='main', timeline_name='Main', render_run_id='run',
                    video_digest=digest, fps_rational=[4, 1], duration_frames=4, clips=[], tracks=[], scripts=[])
    authority = dict(mode='filmstrip', filmstrip_snapshot=snapshot, video_digest=digest)
    calls = []
    original = execution.analyze_audio
    def counted(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)
    monkeypatch.setattr(execution, 'analyze_audio', counted)

    def args(out):
        return Namespace(filmstrip_authority=json.dumps(authority), project_slug='demo', rendered_video=video,
                         range_value=None, out=out, include_media=True)

    first = execution.execute_filmstrip(args(tmp_path / 'one'))
    second = execution.execute_filmstrip(args(tmp_path / 'two'))
    assert len(calls) == 1
    for result in (first, second):
        index = json.loads((Path(result['outputs']['pack_root']) / 'frame-index.json').read_text())
        assert index['schema'] == 'astrid.filmstrip.v2'
        assert 'audio' not in index
        assert index['audio_sidecar']['verified'] is True
        assert index['audio_sidecar']['digest'].startswith('sha256:')
        assert index['media']['source_digest'] == digest
        manifest = json.loads(open(result['manifest_path']).read())
        paths = {entry['path'] for entry in manifest['outputs']}
        assert 'audio-analysis.json' in paths and 'media/rendered-video.mp4' in paths
        host_manifest = json.loads((Path(result['run_root']) / 'manifest.json').read_text())
        assert any(entry['name'] == 'filmstrip_bundle' for entry in host_manifest['outputs'])


def test_filmstrip_preserves_session_primary_through_all_capture_cleanup_layers(tmp_path, monkeypatch, capsys):
    """Real session/protocol parser, worker, provider and outer export; fake host lease only.

    The renderer bridge supplies the same session argument projection without
    invoking compilation or Chrome. This is an error-lifetime proof, not pixels.
    """
    from astrid.packs.rendering.actions.timeline_visualize import composed_frame
    from astrid.packs.rendering.rendering.renderers.remotion import run as backend
    session = backend.PersistentRemotionFrameSession()
    sent = []
    class Channel:
        def __init__(self, reply):
            self.reply = reply
        def recv(self, _size):
            return self.reply
    class HostLease:
        """The inherited host renderer lease; the session's real parser reads each reply."""
        def request(self, identity, request, parser):
            assert identity == "same-owner"
            sent.append(request["type"])
            primary = request["type"] == "render"
            return parser(Channel(json.dumps({"ok": False, "error": "E1 original capture" if primary else "E2 original cleanup",
                "cleanup_uncertain": True,
                "error_details": {"name": "Error", "message": "E1 original capture" if primary else "E2 original cleanup",
                                  "stack": "PRIMARY_STACK" if primary else "CLEANUP_STACK"}}).encode() + b"\n"))
    monkeypatch.setattr("astrid.core.execution.remotion_host_session.host_remotion_client", lambda: HostLease())
    class Renderer:
        def __call__(self, frames, output_dir, resolution):
            session.render(project_dir=tmp_path, composition_id="Fixture", node_executable=tmp_path / "node",
                remotion_cli=tmp_path / "cli", props_path=tmp_path / "props", output_dir=output_dir,
                frames=frames, resolution=tuple(resolution) if resolution is not None else None, port=3333, environment={}, identity="same-owner")
            raise AssertionError("failed request returned success")
        def close(self, *, force=False): session.close(force=force)
    real_provider = composed_frame.RemotionFrameProvider
    providers = []
    def provider(snapshot, **kwargs):
        value = real_provider(snapshot, **kwargs, renderer=Renderer(),
            cache=composed_frame.FrameCaptureCache(tmp_path / "cache"))
        providers.append(value)
        return value
    monkeypatch.setattr(composed_frame, "RemotionFrameProvider", provider)
    snapshot = {'project_slug': 'demo', 'timeline_id': 'main', 'render_run_id': 'capture-main',
        'video_digest': 'sha256:' + 'c' * 64, 'fps_rational': [24, 1], 'duration_frames': 48,
        'clips': [], 'tracks': [], 'scripts': [], 'occurrences': [], 'registry': {'assets': {}},
        'metadata': {'capture_identity': 'sha256:' + 'c' * 64}}
    authority = {'mode': 'composed_capture', 'capture_snapshot': snapshot,
                 'capture_identity': snapshot['video_digest']}
    args = Namespace(filmstrip_authority=json.dumps(authority), project_slug='demo', range_value=None,
        out=tmp_path / 'out', timeline=tmp_path / 'timeline.json', assets_registry=tmp_path / 'assets.json',
        frame=7, show=['output'])
    with pytest.raises(RuntimeError, match='^E1 original capture$') as caught:
        execution.execute_filmstrip(args)
    assert caught.value.frame_owner_failure['error_details']['stack'] == 'PRIMARY_STACK'
    assert caught.value.__cause__ is not None
    assert 'cleanup' in str(caught.value.__cause__)
    assert sent.count('render') == 1 and sent.count('release') >= 1
    assert providers[0].worker._closed and not providers[0].worker.started
    assert not list(tmp_path.glob('astrid-frame-capture-*'))
    diagnostics = [json.loads(line) for line in capsys.readouterr().err.splitlines()]
    assert diagnostics[0]['error'] == 'E1 original capture'
    assert any(row['error'] == 'E2 original cleanup' for row in diagnostics)


def test_filmstrip_successful_capture_does_not_swallow_close_failure(tmp_path, monkeypatch):
    from PIL import Image
    from astrid.packs.rendering.actions.timeline_visualize import composed_frame
    cleanup = RuntimeError('successful capture cleanup remains uncertain')
    class Provider:
        def __init__(self, *_args, **_kwargs): pass
        def capture(self, cards, root, resolution):
            for card in cards:
                target = root / card['image']
                target.parent.mkdir(parents=True, exist_ok=True)
                Image.new('RGB', (32, 18), '#224466').save(target)
            return {'evidence_source': 'fresh_capture', 'requested_frames': [7], 'resolution': resolution}
        def close(self, *, force=False): raise cleanup
    monkeypatch.setattr(composed_frame, 'RemotionFrameProvider', Provider)
    snapshot = {'project_slug': 'demo', 'timeline_id': 'main', 'render_run_id': 'capture-main',
        'video_digest': 'sha256:' + 'c' * 64, 'fps_rational': [24, 1], 'duration_frames': 48,
        'clips': [], 'tracks': [], 'scripts': [], 'occurrences': [], 'registry': {'assets': {}},
        'metadata': {'capture_identity': 'sha256:' + 'c' * 64}}
    authority = {'mode': 'composed_capture', 'capture_snapshot': snapshot,
                 'capture_identity': snapshot['video_digest']}
    args = Namespace(filmstrip_authority=json.dumps(authority), project_slug='demo', range_value=None,
        out=tmp_path / 'out', timeline=tmp_path / 'timeline.json', assets_registry=tmp_path / 'assets.json',
        frame=7, show=['output'])
    with pytest.raises(RuntimeError) as caught:
        execution.execute_filmstrip(args)
    assert caught.value is cleanup


def test_d69_upstream_input_only_is_a_materializable_compact_filmstrip_bundle(tmp_path):
    """Input-only uses the same manifest/receipt contract as the paired view."""
    snapshot = {
        'project_slug': 'demo', 'timeline_id': 'main', 'timeline_name': 'Main',
        'render_run_id': 'run-input', 'fps_rational': [30, 1],
        'duration_frames': 120, 'clips': [
            {'id': 'picture-1', 'track': 'picture', 'asset': 'still-a', 'at': 0, 'hold': 2},
            {'id': 'vo-1', 'track': 'vo', 'asset': 'voice-a', 'at': 1, 'hold': 2},
        ], 'tracks': [{'id': 'picture', 'kind': 'visual'}, {'id': 'vo', 'kind': 'audio'}],
        'scripts': [], 'metadata': {},
    }
    args = Namespace(range_value=None, out=tmp_path / 'output', project_slug='demo')
    result = execution.execute_input_only(
        args, {'input_snapshot': snapshot, 'mode': 'input_only'},
    )

    manifest_path = Path(result['manifest_path'])
    manifest = json.loads(manifest_path.read_text())
    index = json.loads((manifest_path.parent / 'frame-index.json').read_text())
    assert manifest['kind'] == 'timeline_filmstrip'
    assert index['schema'] == 'astrid.filmstrip.v2'
    assert 'navigation' not in index
    assert result['outputs']['pages']
    assert all(Path(page).name.startswith('filmstrip-') for page in result['outputs']['pages'])
    assert (manifest_path.parent / 'render-snapshot.json').is_file()
    assert len(json.dumps(index, separators=(',', ':')).encode()) < 8192

    from astrid.packs.rendering.actions.timeline_visualize.inspection_contract import inspect_filmstrip  # noqa: I001

    assert inspect_filmstrip(manifest_path, section='summary')['ok']
    placements = inspect_filmstrip(manifest_path, section='placements')
    assert placements['ok']
    assert {row['id'] for row in placements['data']['records']} == {'picture-1', 'vo-1'}
    audio = inspect_filmstrip(manifest_path, section='audio')
    assert audio['ok']
    assert audio['data']['records'][0]['status'] == 'not_available'
    pages = inspect_filmstrip(manifest_path, section='pages')
    assert pages['ok'] and pages['data']['records']

    bundle_bytes = Path(result['outputs']['filmstrip_bundle']).read_bytes()
    raw = {'outputs': {'artifacts': [{
        'name': 'filmstrip_bundle',
        'digest': 'sha256:' + hashlib.sha256(bundle_bytes).hexdigest(),
        'size': len(bundle_bytes),
    }]}}
    client = SimpleNamespace(media=SimpleNamespace(read_bytes=lambda _: bundle_bytes))
    from astrid.sdk.invocation import _materialize_filmstrip_outputs

    hydrated = _materialize_filmstrip_outputs(raw, client, project='demo', cache_root=tmp_path / 'cache')
    assert Path(hydrated).is_file()
    assert json.loads(Path(hydrated).read_text())['kind'] == 'timeline_filmstrip'
