"""Exercise the public H3 video CLI against settled Runtime associations."""
import hashlib
import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid import console
from astrid.core.generation import cli
from astrid.sdk.remote import RemoteMedia
from astrid.sdk.results import InvocationResult


@pytest.mark.parametrize('failure', [None, 'corrupt-audio', 'unsealed-video', 'missing-manifest'])
def test_h3_video_cli_delivers_parent_media(tmp_path, monkeypatch, capsys, failure):
    objects, metadata, rows, inventory, reads = {}, [], [], [], []

    def add(data, name, port, media_type):
        digest = 'sha256:' + hashlib.sha256(data).hexdigest()
        objects[digest] = data
        row = {'association_id': port, 'project_id': 'gpu-shakedown', 'task_id': 'parent-task',
               'output_port': port, 'ordinal': 0, 'role': 'result', 'filename': name,
               'media_type': media_type, 'size': len(data), 'digest': digest, 'object_id': digest}
        metadata.append(row)
        return row

    for data, name, port, media_type in [
        (b'parent verified video', 'verified-candidate.media', 'verified_candidate', 'video/mp4'),
        (b'parent audio', 'sound.wav', 'audio', 'audio/wav'),
        (b'parent receipt', 'final-receipt.json', 'final_receipt', 'application/json'),
    ]:
        row = add(data, name, port, media_type)
        rows.append(row)
        inventory.append({'name': port, 'path': name, 'bytes': len(data),
                          'content_hash': row['digest'], 'media_type': media_type})
    if failure == 'unsealed-video':
        inventory[0]['content_hash'] = 'sha256:' + '0' * 64
    manifest = json.dumps({'kind': 'h3_av.transform', 'schema_version': 1,
                           'inputs': {}, 'outputs': inventory, 'warnings': [], 'created': 'now'}).encode()
    manifest_row = add(manifest, 'parent-manifest.json', 'manifest', 'application/json')
    if failure != 'missing-manifest':
        rows.append(manifest_row)
    if failure == 'corrupt-audio':
        objects[rows[1]['object_id']] = b'corrupt audio'

    state = SimpleNamespace(closed=False, calls=[])

    def get_object(digest):
        assert not state.closed
        reads.append(digest)
        return {'data': objects[digest]}

    media = RemoteMedia(SimpleNamespace(get_object=get_object,
        list_project_objects=lambda project, **kwargs: (metadata, None)))

    def invoke(capability, **kwargs):
        state.calls.append((capability, kwargs))
        assert capability == 'h3_av.transform'
        assert kwargs['wait'] and kwargs['read_managed_outputs']
        return InvocationResult(capability, 'executor', 'action', True,
            kernel_task_id='parent-task', outputs={'managed_outputs': rows},
            raw_result={'task': {'task_id': 'parent-task', 'state': 'succeeded',
                                 'result': {'outputs': rows}}, 'managed_outputs': rows})

    @contextmanager
    def connection():
        try:
            yield SimpleNamespace(invoke_result=invoke, media=media)
        finally:
            state.closed = True

    monkeypatch.setattr(cli, '_open_client', connection)
    inputs = tmp_path / 'inputs.json'
    inputs.write_text(json.dumps({'request': str(tmp_path / 'request.json'),
                                  'asset_map': str(tmp_path / 'assets.json')}))
    out = tmp_path / 'output.mp4'
    out.write_bytes(b'previous output')
    from astrid.core.pack.discovery import pack_discovery_scope
    with pack_discovery_scope():
        code = console.main(['generate', 'video', '--entry', 'h3', '--mode', 'transform',
                             '--target', 'managed', '--inputs-json', str(inputs),
                             '--project', 'gpu-shakedown', '--consent', 'user_env',
                             '--out', str(out), '--json'])
    payload = json.loads(capsys.readouterr().out)
    assert state.closed and len(state.calls) == 1
    if failure:
        assert code == 2 and payload['issues'][0]['code'] == 'output_delivery_failed'
        assert payload['task_id'] == 'parent-task'
        assert out.read_bytes() == b'previous output'
        assert not (tmp_path / 'output-2.wav').exists()
    else:
        assert code == 0
        assert payload['image_paths'] == [str(out), str(tmp_path / 'output-2.wav')]
        assert [Path(path).read_bytes() for path in payload['image_paths']] == [
            b'parent verified video', b'parent audio']
        assert reads == [manifest_row['object_id'], rows[0]['object_id'], rows[1]['object_id']]


def test_h3_parent_manifest_is_harvested_with_its_verified_candidate(tmp_path):
    import yaml

    from astrid.core._shared.result_manifest import harvest_staged_outputs
    from astrid.packs.h3_av.actions.transform.run import _write_parent_manifests

    pack = Path(__file__).parents[3] / 'astrid/packs/h3_av/pack.yaml'
    declaration = yaml.safe_load(pack.read_text())['actions']['transform']['outputs']
    assert next(row for row in declaration if row['name'] == 'manifest')['path_template'] == (
        '{out}/parent-manifest.json')
    (tmp_path / 'verified-candidate.media').write_bytes(b'candidate')
    (tmp_path / 'final-receipt.json').write_bytes(b'{}')
    _write_parent_manifests(tmp_path, 'video/mp4')
    rows = harvest_staged_outputs(tmp_path)
    assert [row['name'] for row in rows] == ['verified_candidate', 'final_receipt', 'manifest']
    manifest = json.loads(Path(rows[2]['path']).read_bytes())
    candidate = manifest['outputs'][0]
    assert candidate['name'] == rows[0]['name']
    assert candidate['content_hash'] == rows[0]['content_hash']
    assert candidate['bytes'] == rows[0]['bytes']
