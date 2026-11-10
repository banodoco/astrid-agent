"""Real pod job preparation on a verified CPU engine installation."""
from __future__ import annotations

import hashlib
import json
import time

import pytest

from astrid.core.generation.local_job import run_job
from tests.integrations.test_managed_host_preparation import (  # noqa: F401
    _workflow, artifact_server, managed_install, session,
)

pytestmark = pytest.mark.integration


@pytest.mark.parametrize('mode', ['align', 'trust'])
def test_pod_job_prepares_once_and_runs_on_owned_engine(tmp_path, monkeypatch, managed_install, artifact_server, mode):
    from vibecomfy.nodes.core import EmptyImage, SaveImage
    from vibecomfy.security.provenance import Provenance
    from vibecomfy.templates import OutputSpec, ReadyMetadata, new_workflow
    from vibecomfy.workflow_bundle import emit_bundle_with_candidate
    from astrid.packs.vibecomfy.actions.run import run as action

    client, payload, recipe, root, started, bootstrap = managed_install
    url, data, hits = artifact_server
    asset = {'name': 'pod-required.bin', 'url': url, 'subdir': 'checkpoints',
             'sha256': hashlib.sha256(data).hexdigest(), 'size_bytes': len(data)}
    with new_workflow(ReadyMetadata.build(template_id='pod-cpu', capability='image', model_assets=[asset])) as workflow:
        image = EmptyImage(width=16, height=16, batch_size=1, color=0)
        saved = SaveImage(images=image.out('IMAGE'), filename_prefix='astrid')
        workflow.finalize({}, outputs=[OutputSpec(node=saved)])
    bundle = tmp_path / 'bundle'
    bundle.mkdir()
    emit_bundle_with_candidate(workflow, bundle / 'workflow.py', Provenance.USER_CONFIRMED, None)
    source = tmp_path / 'source.json'
    source.write_text(json.dumps(_workflow()))
    job = {'schema': 'astrid.machine-job.v1', 'entry': None, 'mode': None, 'align': mode,
           'task_identity': 'task', 'attempt_identity': 'attempt', 'inputs': {}, 'bindings': {},
           'execution': {'python': str(bundle / 'workflow.py'), 'companion': str(bundle / 'workflow.vibe.json'),
                         'source': str(source)}}
    job_path = tmp_path / 'job.json'
    job_path.write_text(json.dumps(job))
    client.stop_owned(deadline=time.monotonic() + 15)
    started.unlink()
    calls = []
    original = action._engine_for
    def engine_for(manifest):
        engine = original(manifest)
        cls = type(engine)
        if not getattr(cls, '_pod_test_spy', False):
            align = cls.align
            def spy(self, manifest, mode, **kwargs):
                calls.append((manifest, mode, kwargs.get('workflow')))
                return align(self, manifest, mode, **kwargs)
            monkeypatch.setattr(cls, 'align', spy)
            monkeypatch.setattr(cls, '_pod_test_spy', True, raising=False)
        return engine
    monkeypatch.setattr(action, '_engine_for', engine_for)
    result = run_job('workflow', job_path, tmp_path / 'out')
    assert len(calls) == 1 and calls[0][1] == mode and calls[0][2] is not None
    assert (tmp_path / 'out' / result['managed_generation_result_path']).is_file()
    assert started.is_file()
    artifact = root / 'models/checkpoints/pod-required.bin'
    if mode == 'align':
        assert artifact.read_bytes() == data and hits
    else:
        assert not artifact.exists() and not hits
    client.stop_owned(deadline=time.monotonic() + 15)


def test_pod_ui_json_is_frozen_before_preparation(tmp_path, managed_install):
    client, payload, recipe, root, started, bootstrap = managed_install
    source = tmp_path / 'input.json'
    source.write_text(json.dumps(_workflow()))
    job_path = tmp_path / 'job.json'
    job_path.write_text(json.dumps({'schema': 'astrid.machine-job.v1', 'entry': None, 'mode': None,
        'align': 'trust', 'task_identity': 'task', 'attempt_identity': 'attempt', 'inputs': {}, 'bindings': {},
        'execution': {'workflow': str(source)}}))
    client.stop_owned(deadline=time.monotonic() + 15)
    original = source.read_bytes()
    result = run_job('workflow', job_path, tmp_path / 'out')
    assert (tmp_path / 'out' / result['managed_generation_result_path']).is_file()
    assert source.read_bytes() == original
    client.stop_owned(deadline=time.monotonic() + 15)


def test_managed_dry_preview_uses_receipt_model_root(tmp_path, monkeypatch, managed_install):
    from astrid.core.generation import engine_recipe
    from astrid.core.generation.managed_preparation import ManagedEnginePreparation, configured_models_root
    from astrid.sdk.generation import GenerationFacade
    from astrid.packs.vibecomfy.production_engine import load_workflow_path, _canonical_bundle_value, loaded_workflow_execution_identity
    from tests.integrations.test_dry_run_equals_live_run import Recorder, _built
    from vibecomfy.workflow_bundle import emit_bundle

    client, payload, recipe, root, started, bootstrap = managed_install
    monkeypatch.setattr(engine_recipe, 'load_recipe', lambda engine, platform, **kwargs: recipe)
    receipt_path = root / '.astrid-engine/install.json'
    receipt = json.loads(receipt_path.read_text())
    custom = tmp_path / 'custom-model-cache'
    custom.mkdir()
    receipt['models_root'] = str(custom)
    for row in receipt['models']:
        row['path'] = str(custom / str(row['path']).split('/models/', 1)[1])
    receipt_path.write_text(json.dumps(receipt))
    expected = configured_models_root(root=str(root))
    recorder = Recorder(monkeypatch)
    dry = GenerationFacade().image(placement='managed', prompt='a boat', seed=7, dry_run=True)
    _, _, workflow = _built(recorder.take())
    assert expected == str(custom)
    assert dry['entry']['requirements']['models_root'] == expected
    folder = tmp_path / 'custom-workflow'
    folder.mkdir()
    emit_bundle(workflow, folder / 'workflow.py', {'operation': 'authored'})
    (folder / 'source.json').write_text('{}')
    members = {'python': folder / 'workflow.py', 'companion': folder / 'workflow.vibe.json',
               'source': folder / 'source.json'}
    digests = {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in members.items()}
    loaded = load_workflow_path(members['python'], tmp_path / 'custom-load')
    prepare = ManagedEnginePreparation(task='task', attempt='attempt', mode='trust',
        deadline=time.monotonic() + 60, cancelled=lambda: False, scratch=tmp_path / 'custom-prepare')
    try:
        report = prepare.prepare(members['python'], execution_identity=loaded_workflow_execution_identity(loaded),
            inputs=members, canonical_workflow=_canonical_bundle_value(loaded.resolved).workflow, input_digests=digests)
        assert report['manifest'] == dry['entry']['requirements']
    finally:
        prepare.close()
