"""First media through the product CLI and a temporary, authenticated Runtime."""
from __future__ import annotations

import json
import subprocess
import wave
from pathlib import Path

import pytest
from PIL import Image

from astrid.packs.timeline.cli import build_parser
from astrid.sdk.timeline.sync import TimelineSession
from tests.editing_conformance.test_native_batch_ingress import runtime  # noqa: F401

pytestmark = pytest.mark.integration


@pytest.mark.parametrize('track', [None, 'A2'])
def test_first_wav_selects_or_creates_audio_lane(runtime, tmp_path, capsys, track):  # noqa: F811
    source = tmp_path / 'voice.wav'
    with wave.open(str(source), 'wb') as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        audio.writeframes(b'\0\0' * 16000)
    extra = ('--track', track) if track else ()
    before = len(runtime.history()['items'])
    code, added = run(runtime, capsys, 'edit', 'film', '--add-layer', 'media',
                      '--layer-asset', str(source), '--on', '0', '--for', '1',
                      '--id', 'voice', '--idempotency-key', 'first-wav', *extra)
    assert code == 0 and added['ok'], added
    assert len(runtime.history()['items']) == before + 1
    clip = added['data']['canonical_document']['parent_revision']['payload']['clips'][0]
    assert (clip['clipType'], clip['track'], clip['from'], clip['to']) == (
        'audio', track or 'A1', 0, 1)
    assert added['data']['imports'][0]['catalog_write'] is True


def run(world, capsys, *args):
    parsed = build_parser(world.client()).parse_args([*args, '--project', world.project, '--json'])
    code = parsed.handler(parsed)
    output = capsys.readouterr()
    return code, json.loads(output.out or output.err)


@pytest.mark.parametrize('track', ['V1', 'V2'])
def test_new_timeline_first_image_one_cli_commit_and_exact_retry(runtime, tmp_path, capsys, track):  # noqa: F811
    world = runtime
    code, created = run(world, capsys, 'create', 'first-film', '--idempotency-key', 'new-film')
    assert code == 0 and created['ok'], created
    service = world.service()

    def history():
        return service.list_timeline_commits(world.project, 'first-film', authorize=lambda: None)

    initial = TimelineSession.open(world.project, 'first-film', client=world.client())
    bundle = initial._bundle(initial.outbox.read())
    assert [(row['id'], row['kind']) for row in bundle['parent']['config']['tracks']] == [
        ('V1', 'visual'), ('A1', 'audio')]
    assert not bundle['shots'] and not bundle['placements']
    before = len(history()['items'])
    image = tmp_path / 'first.png'
    Image.new('RGB', (16, 16), 'red').save(image)
    args = ('edit', 'first-film', '--add-layer', 'media', '--layer-asset', str(image),
            '--on', '0', '--for', '3', '--track', track, '--idempotency-key', 'first-image')
    code, added = run(world, capsys, *args)
    assert code == 0 and added['ok'], added
    assert len(history()['items']) == before + 1
    body = json.loads(world.calls[-1][0])
    assert body['source'] == {'via': 'cli'}
    assert [call['op'] for call in body['commands']] == (
        ['clip.add-media'] if track == 'V1' else ['track.create', 'clip.add-media'])
    document = added['data']['canonical_document']
    assert not document['shot_revisions'] and not document['internal_timeline_revisions']
    clips = document['parent_revision']['payload']['clips']
    assert len(clips) == 1
    assert (clips[0]['track'], clips[0]['at'], clips[0]['hold']) == (track, 0, 3)
    code, shown = run(world, capsys, 'show', 'first-film', '--published')
    assert code == 0 and shown['ok'], shown
    assert clips[0]['id'] in json.dumps(shown['data'])

    # Neither a later edit nor removal of the source file changes an exact retry.
    code, moved = run(world, capsys, 'edit', 'first-film', '--clip', clips[0]['id'],
                      '--on', '5', '--idempotency-key', 'move-first')
    assert code == 0 and moved['ok'], moved
    head = history()['head']
    count = len(history()['items'])
    image.unlink()
    code, retried = run(world, capsys, *args)
    assert code == 0 and retried['receipt'] == added['receipt'], retried
    assert history()['head'] == head and len(history()['items']) == count
    assert world.calls[-1][0] == world.calls[-3][0]
    changed_args = list(args)
    changed_args[changed_args.index('--for') + 1] = '4'
    code, wrong_key = run(world, capsys, *changed_args)
    assert code == 1 and 'fresh key' in wrong_key['error']['message']
    assert history()['head'] == head
    # The existing literal move gets the same accepted-key retry guarantee.
    code, retried_move = run(world, capsys, 'edit', 'first-film', '--clip', clips[0]['id'],
                             '--on', '5', '--idempotency-key', 'move-first')
    assert code == 0 and retried_move['receipt'] == moved['receipt']
    assert history()['head'] == head and len(history()['items']) == count


def test_first_image_lost_ack_retries_original_import_and_ids(runtime, tmp_path, capsys):  # noqa: F811
    world = runtime
    image = tmp_path / 'first.png'
    Image.new('RGB', (16, 16), 'blue').save(image)
    args = ('edit', 'film', '--add-layer', 'media', '--layer-asset', str(image),
            '--on', '0', '--for', '3', '--track', 'V1', '--idempotency-key', 'lost-image')
    world.lost.add('lost-image')
    code, failure = run(world, capsys, *args)
    assert code == 1 and not failure['ok']
    assert failure['idempotency_key'] == 'lost-image'
    first = world.calls[-1]
    head = world.history()['head']
    image.unlink()
    world.restart()
    code, retried = run(world, capsys, *args)
    assert code == 0 and retried['ok'], retried
    assert world.calls[-1][:2] == first[:2]
    assert world.history()['head'] == head


def test_first_image_bad_track_is_actionable_and_cannot_partially_publish(runtime, tmp_path, capsys):  # noqa: F811
    world = runtime
    image = tmp_path / 'first.png'
    Image.new('RGB', (16, 16), 'blue').save(image)
    before = world.history()
    code, failure = run(world, capsys, 'edit', 'film', '--add-layer', 'media',
                        '--layer-asset', str(image), '--on', '0', '--for', '3',
                        '--track', 'A1', '--idempotency-key', 'bad-track')
    assert code == 1 and '--track V1' in failure['error']['message']
    assert world.history() == before


def test_media_add_without_write_flags_and_reusing_a_managed_asset(runtime, tmp_path, capsys):  # noqa: F811
    world = runtime
    image = tmp_path / 'first.png'
    Image.new('RGB', (16, 16), 'green').save(image)
    parsed = build_parser(world.client()).parse_args([
        'edit', 'film', '--project', world.project, '--add-layer', 'media',
        '--layer-asset', str(image), '--on', '0', '--for', '3', '--id', 'opening',
    ])
    assert parsed.handler(parsed) == 0
    assert 'accepted revision' in capsys.readouterr().out
    assert json.loads(world.calls[-1][0])['source'] == {'via': 'cli'}
    first = world.calls[-1][3]['data']['canonical_document']['parent_revision']['payload']['clips'][0]
    image.unlink()
    code, added = run(world, capsys, 'edit', 'film', '--add-layer', 'media',
                      '--layer-asset', first['asset'], '--on', '4', '--until', '7',
                      '--id', 'second', '--idempotency-key', 'managed-key')
    assert code == 0 and added['ok'], added
    clips = added['data']['canonical_document']['parent_revision']['payload']['clips']
    assert [row['id'] for row in clips] == ['opening', 'second']
    assert (clips[-1]['at'], clips[-1]['hold']) == (4, 3)
    assert clips[-1]['asset'] == first['asset']


def test_first_video_preserves_verified_source_window(runtime, capsys):  # noqa: F811
    world = runtime
    video = Path(__file__).resolve().parents[1] / 'fixtures/reshape/hype_regression/main.mp4'
    code, added = run(world, capsys, 'edit', 'film', '--add-layer', 'media',
                      '--layer-asset', str(video), '--on', '0', '--for', '3',
                      '--id', 'opening', '--idempotency-key', 'first-video')
    assert code == 0 and added['ok'], added
    clips = added['data']['canonical_document']['parent_revision']['payload']['clips']
    assert len(clips) == 1
    assert {key: clips[0][key] for key in ('id', 'from', 'to', 'hold', 'speed')} == {
        'id': 'opening', 'from': 0, 'to': 3, 'hold': 3, 'speed': 1}
    assert json.loads(world.calls[-1][0])['source'] == {'via': 'cli'}


@pytest.mark.ffmpeg
@pytest.mark.parametrize('entry', ['cli', 'sdk'])
def test_added_video_can_be_trimmed_and_shown(runtime, tmp_path, capsys, entry):  # noqa: F811
    world = runtime
    video = tmp_path / 'source.mp4'
    subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-f', 'lavfi',
                    '-i', 'color=c=blue:s=16x16:r=30:d=4', '-c:v', 'libx264',
                    '-pix_fmt', 'yuv420p', str(video)], check=True, capture_output=True)
    if entry == 'cli':
        code, added = run(world, capsys, 'edit', 'film', '--add-layer', 'media',
                          '--layer-asset', str(video), '--on', '0', '--for', '3',
                          '--id', 'opening', '--idempotency-key', 'add-video')
        assert code == 0 and added['ok'], added
    else:
        added = world.session().add_media(video, on=0, duration=3, clip_id='opening',
                                          idempotency_key='add-video')
    before = len(world.history()['items'])
    code, trimmed = run(world, capsys, 'edit', 'film', '--clip', 'opening',
                        '--trim-in', '0.5', '--trim-out', '2.5',
                        '--idempotency-key', 'trim-video')
    assert code == 0 and trimmed['ok'], trimmed
    assert len(world.history()['items']) == before + 1
    request = json.loads(world.calls[-1][0])
    assert request['source'] == {'via': 'cli'}
    assert [command['op'] for command in request['commands']] == ['clip.trim']
    payload = trimmed['data']['canonical_document']['parent_revision']['payload']
    clip = payload['clips'][0]
    assert {key: clip[key] for key in ('from', 'to', 'hold')} == {
        'from': 0.5, 'to': 2.5, 'hold': 2}
    assert payload['registry']['assets'][clip['asset']]['duration'] == 4
    code, shown = run(world, capsys, 'show', 'film', '--published', '--layers', '--clip', 'opening')
    assert code == 0 and shown['ok'], shown
    shown_clip = shown['data']['clips'][0]
    assert (shown_clip['source_from'], shown_clip['source_to']) == (0.5, 2.5)


def test_added_image_refuses_source_trim_with_clear_error(runtime, tmp_path, capsys):  # noqa: F811
    world = runtime
    image = tmp_path / 'image.png'
    Image.new('RGB', (16, 16), 'blue').save(image)
    code, added = run(world, capsys, 'edit', 'film', '--add-layer', 'media',
                      '--layer-asset', str(image), '--on', '0', '--for', '3',
                      '--id', 'still', '--idempotency-key', 'add-image')
    assert code == 0 and added['ok'], added
    before = world.history()
    code, refused = run(world, capsys, 'edit', 'film', '--clip', 'still',
                        '--trim-in', '0.5', '--trim-out', '2.5',
                        '--idempotency-key', 'trim-image')
    assert code == 1 and not refused['ok']
    assert 'images have no source window' in refused['error']['message']
    assert world.history() == before
    parsed = build_parser(world.client()).parse_args([
        'edit', 'film', '--project', world.project, '--clip', 'still',
        '--trim-in', '0.5', '--trim-out', '2.5',
    ])
    assert parsed.handler(parsed) == 1
    output = capsys.readouterr()
    assert 'images have no source window' in (output.out or output.err).splitlines()[0]
    assert world.history() == before
