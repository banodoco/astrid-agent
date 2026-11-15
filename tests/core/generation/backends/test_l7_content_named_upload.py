"""Content reuse is decided from the engine on each upload attempt."""
import hashlib
import io
import json
from email.parser import BytesParser
from email.policy import default
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

from astrid.core.generation.backends.comfy_target import ComfyTarget, ComfyTargetClient


class Engine:
    def __init__(self):
        self.files = {}
        self.uploads = []
        self.probe_error = False
        self.suffix = False
        self.hide_suffix = False

    def open(self, request, *, timeout):
        url = urlsplit(request.full_url)
        if url.path == '/view':
            if self.probe_error:
                raise OSError('unavailable')
            q = parse_qs(url.query)
            key = (q['subfolder'][0], q['filename'][0])
            if self.hide_suffix and ' (1)' in key[1]:
                raise OSError('probe failed')
            if key not in self.files:
                raise HTTPError(request.full_url, 404, 'missing', {}, None)
            return Reply(b'', {'Content-Length': str(len(self.files[key]))})
        assert url.path == '/upload/image'
        msg = BytesParser(policy=default).parsebytes(
            b'Content-Type: ' + request.headers['Content-type'].encode() + b'\r\nMIME-Version: 1.0\r\n\r\n' + request.data)
        parts = {p.get_param('name', header='content-disposition'): p for p in msg.iter_parts()}
        folder = parts['subfolder'].get_content()
        filename = parts['image'].get_filename()
        data = parts['image'].get_payload(decode=True)
        if self.suffix and folder == 'astrid':
            filename = filename.replace('.png', ' (1).png')
        self.files[(folder, filename)] = data
        self.uploads.append((folder, filename, data))
        return Reply(json.dumps({'name': filename, 'subfolder': folder, 'type': 'input'}).encode())


class Reply(io.BytesIO):
    status = 200
    def __init__(self, data, headers=None):
        super().__init__(data)
        self.headers = headers or {}


def test_reuse_count_and_safe_fallbacks(tmp_path):
    engine = Engine()
    client = ComfyTargetClient(ComfyTarget('http://127.0.0.1:8188', 'attached'), opener=engine.open)
    first = tmp_path / 'first.png'
    second = tmp_path / 'another.png'
    first.write_bytes(b'one')
    second.write_bytes(b'one')
    digest = hashlib.sha256(b'one').hexdigest()
    expected = f'astrid/{digest}.png'
    assert client.upload_media(first, task_id='task-1') == expected
    assert len(engine.uploads) == 1
    assert client.upload_media(second, task_id='task-2') == expected
    assert len(engine.uploads) == 1
    second.write_bytes(b'two')
    assert client.upload_media(second, task_id='task-3') == f"astrid/{hashlib.sha256(b'two').hexdigest()}.png"
    assert len(engine.uploads) == 2
    del engine.files[('astrid', digest + '.png')]
    assert client.upload_media(first, task_id='task-4') == expected
    assert len(engine.uploads) == 3
    engine.files[('astrid', digest + '.png')] = b'wrong size'
    assert client.upload_media(first, task_id='task-5') == 'astrid/task-5/first.png'
    assert engine.files[('astrid', digest + '.png')] == b'wrong size'
    engine.probe_error = True
    assert client.upload_media(first, task_id='task-6') == 'astrid/task-6/first.png'
    engine.probe_error = False
    del engine.files[('astrid', digest + '.png')]
    engine.suffix = True
    assert client.upload_media(first, task_id='task-7') == f'astrid/{digest} (1).png'
    engine.hide_suffix = True
    assert client.upload_media(first, task_id='task-8') == 'astrid/task-8/first.png'
    assert engine.uploads[-1][:2] == ('astrid/task-8', 'first.png')
