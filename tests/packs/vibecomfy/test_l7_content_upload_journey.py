"""Exercise content-named uploads through local workflow submission."""
import hashlib
import importlib
import json
from urllib.parse import urlsplit

import pytest

from astrid.core.generation.backends import comfy_target
from tests.core.generation.backends.test_l7_content_named_upload import Engine
from tests.packs.vibecomfy.test_local_server_media import (
    VIDEO, _archive, _declare_source_video, comfy_video,  # noqa: F401
)


@pytest.mark.integration
def test_reused_input_reaches_workflow_and_preserves_task_evidence(comfy_video, tmp_path, monkeypatch):
    _declare_source_video(monkeypatch)
    engine = Engine()
    real_open = comfy_target.open_comfy_http

    def open_with_inputs(request, *, timeout):
        path = urlsplit(request.full_url).path
        if path == '/upload/image' or (path == '/view' and request.get_method() == 'HEAD'):
            return engine.open(request, timeout=timeout)
        return real_open(request, timeout=timeout)

    monkeypatch.setattr(comfy_target, 'open_comfy_http', open_with_inputs)
    action = importlib.import_module('astrid.packs.vibecomfy.actions.run.run')
    digest = hashlib.sha256(VIDEO).hexdigest()
    for task in ('task-1', 'task-2'):
        archive = _archive(tmp_path / (task + '.zip'), {'source_video': (tmp_path / (task + '.mp4'), VIDEO)})
        output = tmp_path / task / 'outputs'
        result = action._run_and_settle(comfy_video.workflow, output, task_identity=task,
                                        comfy_url=comfy_video.url, comfy_output_dir=str(comfy_video.output),
                                        managed_assets=str(archive))
        envelope = json.loads((output / result['managed_generation_result_path']).read_text())
        assert envelope['task_id'] == task
        prompt = envelope['evidence']['producer']['attempt']['record']['compiled_prompt']
        assert prompt['99']['inputs']['video'] == f'astrid/{digest}.mp4'
        ledger = json.loads((output / 'engine-input' / '.managed-asset-digests.json').read_text())
        assert digest in ledger.values()
    assert len(engine.uploads) == 1
    assert [p['99']['inputs']['video'] for p in comfy_video.prompts] == [f'astrid/{digest}.mp4'] * 2
