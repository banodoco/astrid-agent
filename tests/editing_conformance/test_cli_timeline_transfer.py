"""A CLI-created media timeline survives archive transfer and remains editable."""
from __future__ import annotations

import json
import subprocess

import pytest
from PIL import Image

from astrid.packs.timeline.cli import build_parser
from tests.editing_conformance.test_native_batch_ingress import runtime  # noqa: F401

pytestmark = [pytest.mark.integration, pytest.mark.ffmpeg]


def invoke(world, capsys, project, *args):
    parsed = build_parser(world.client(real_http=True)).parse_args(
        [*args, '--project', project, '--json'])
    code = parsed.handler(parsed)
    output = capsys.readouterr()
    return code, json.loads(output.out or output.err)


def test_cli_media_timeline_export_import_is_one_editable_commit(runtime, tmp_path, capsys):  # noqa: F811
    world = runtime
    source_project = world.project
    code, created = invoke(world, capsys, source_project, 'create', 'transfer-source',
                           '--idempotency-key', 'create-transfer-source')
    assert code == 0 and created['ok'], created

    image = tmp_path / 'card.png'
    Image.new('RGB', (16, 16), 'orange').save(image)
    video = tmp_path / 'source.mp4'
    subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-f', 'lavfi',
                    '-i', 'color=c=blue:s=16x16:r=30:d=4', '-c:v', 'libx264',
                    '-pix_fmt', 'yuv420p', str(video)], check=True, capture_output=True)

    code, still = invoke(world, capsys, source_project, 'edit', 'transfer-source',
                         '--add-layer', 'media', '--layer-asset', str(image), '--on', '0',
                         '--for', '2', '--id', 'still', '--idempotency-key', 'source-still')
    assert code == 0 and still['ok'], still
    code, moving = invoke(world, capsys, source_project, 'edit', 'transfer-source',
                          '--add-layer', 'media', '--layer-asset', str(video), '--on', '2',
                          '--for', '3', '--id', 'moving', '--idempotency-key', 'source-video')
    assert code == 0 and moving['ok'], moving

    archive = tmp_path / 'transfer.astrid'
    code, exported = invoke(world, capsys, source_project, 'export', 'transfer-source',
                            '--out', str(archive))
    assert code == 0 and exported['ok'] and archive.is_file(), exported

    # Creating the destination project through the Runtime service keeps this test
    # focused on the public CLI timeline transfer and edit paths.
    target = world.service().create_project({'slug': 'transfer-target', 'name': 'Transfer target'})['id']
    code, imported = invoke(world, capsys, target, 'import', str(archive), '--name', 'received')
    assert code == 0 and imported['ok'], imported
    timeline = imported['data'].get('timeline') or imported['data'].get('timeline_id')
    assert timeline

    code, shown = invoke(world, capsys, target, 'show', timeline, '--published', '--layers')
    assert code == 0 and shown['ok'], shown
    shown_text = json.dumps(shown['data'])
    assert 'still' in shown_text and 'moving' in shown_text
    shown_clips = shown['data']['clips']
    fields = {clip['clip_id']: clip for clip in shown_clips}
    assert fields['still']['clip_type']
    assert fields['still']['asset_id']
    assert fields['still']['authored_fields']['asset']
    assert fields['moving']['clip_type']
    assert fields['moving']['asset_id']
    assert fields['moving']['authored_fields']['asset']
    assert {key: fields['moving']['authored_fields'][key] for key in ('from', 'to')} == {
        'from': 0, 'to': 3,
    }

    code, cli_history = invoke(world, capsys, target, 'history', timeline)
    assert code == 0 and cli_history['ok'], cli_history
    assert len(cli_history['data']['revisions']) == 1
    assert cli_history['data']['revisions'][0]['source'] == 'cli'
    history = world.service().list_timeline_commits(target, timeline, authorize=lambda: None)
    assert len(history['items']) == 1
    initial = history['items'][0]
    assert initial['source'] == {'via': 'cli'}
    assert imported['data']['new_publication_id'] == initial['publication_id']

    world.restart()
    code, durable_history = invoke(world, capsys, target, 'history', timeline)
    assert code == 0 and durable_history['ok'], durable_history
    assert len(durable_history['data']['revisions']) == 1
    assert durable_history['data']['revisions'][0]['source'] == 'cli'

    before = json.loads(json.dumps(shown['data']))
    code, edited = invoke(world, capsys, target, 'edit', timeline, '--clip', 'moving',
                          '--trim-in', '0.5', '--trim-out', '2.5',
                          '--idempotency-key', 'edit-imported')
    assert code == 0 and edited['ok'], edited
    edited_history = world.service().list_timeline_commits(target, timeline, authorize=lambda: None)
    assert len(edited_history['items']) == 2

    code, undone = invoke(world, capsys, target, 'undo', timeline, '--commit',
                          edited['receipt']['receipt_id'], '--idempotency-key', 'undo-imported')
    assert code == 0 and undone['ok'], undone
    code, restored = invoke(world, capsys, target, 'show', timeline, '--published', '--layers')
    assert code == 0 and restored['ok'], restored
    restored_clips = {clip['clip_id']: clip for clip in restored['data']['clips']}
    assert restored_clips['still'] == fields['still']
    assert restored_clips['moving'] == fields['moving']
    assert restored['data']['clips'] == before['clips']

    code, after_undo = invoke(world, capsys, target, 'history', timeline)
    assert code == 0 and after_undo['ok'], after_undo
    assert len(after_undo['data']['revisions']) == 3
    code, next_edit = invoke(world, capsys, target, 'edit', timeline, '--clip', 'moving',
                             '--on', '4', '--idempotency-key', 'edit-after-undo')
    assert code == 0 and next_edit['ok'], next_edit
    assert len(world.service().list_timeline_commits(
        target, timeline, authorize=lambda: None)['items']) == 4
