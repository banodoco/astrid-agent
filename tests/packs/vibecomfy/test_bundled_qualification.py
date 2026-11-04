"""Central execution gates bundled Wan sources, leaving authored graphs alone."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from astrid.core.contracts.errors import AstridError
from astrid.core.generation import availability
from astrid.packs.vibecomfy import production_engine as engine


def canonical_source(path: Path, *, template='video/wan_fake'):
    from vibecomfy.workflow import VibeWorkflow, VibeNode, WorkflowSource
    from vibecomfy.workflow_bundle import emit_bundle
    workflow = VibeWorkflow(template, WorkflowSource(template))
    workflow.nodes['1'] = VibeNode('1', 'VHS_VideoCombine', uid='fake-output')
    workflow.metadata['ready_template'] = template
    bundle = emit_bundle(workflow, path, {'operation': 'authored'})
    path.with_name('source.json').write_text(json.dumps(bundle.materialize_ui()))
    return bundle.python_path


@pytest.mark.parametrize('source_kind', ['bundled-flat', 'bundled-directory', 'selector'])
@pytest.mark.parametrize('qualified', [False, True])
def test_central_bundled_qualification_precedes_dispatch(tmp_path, monkeypatch, source_kind, qualified):
    import vibecomfy
    root = tmp_path / 'library'
    bundled = canonical_source(root / ('ready_templates/video/wan_fake/workflow.py'
        if source_kind == 'bundled-directory' else 'ready_templates/video/wan_fake.py'))
    source = bundled
    monkeypatch.setattr(vibecomfy, '__file__', str(root / 'vibecomfy/__init__.py'))
    if source_kind == 'selector':
        source = tmp_path / 'selector.json'
        source.write_text(json.dumps({'template_id': 'video/wan_fake', 'bindings': {}}))
    declaration = {**availability.availability_declaration()}
    declaration['models'] = {'wan-templates': {'available': qualified, 'error': {
        'code': 'generation_route_unqualified', 'message': 'missing bundled route qualification'}}}
    monkeypatch.setattr(availability, 'availability_declaration', lambda: declaration)
    dispatch = Mock(return_value=engine.ProductionRunResult(outputs=()))
    monkeypatch.setattr(engine, '_run_profile_result', dispatch)
    from vibecomfy.security.provenance import Provenance
    from vibecomfy.workflow_bundle import load_bundle
    monkeypatch.setattr(engine, '_load_workflow', lambda *a: load_bundle(bundled, trust=Provenance.USER_CONFIRMED))
    if qualified:
        assert engine.run_workflow_path(source, tmp_path / 'out', task_identity='fake-task') == ()
        dispatch.assert_called_once()
    else:
        with pytest.raises(AstridError) as caught:
            engine.run_workflow_path(source, tmp_path / 'out', task_identity='fake-task')
        assert caught.value.code == 'generation_route_unqualified'
        assert 'video/wan_fake' in str(caught.value) and 'missing bundled route qualification' in str(caught.value)
        assert caught.value.fix
        dispatch.assert_not_called()


def test_authored_workflow_with_wan_metadata_remains_unchanged(tmp_path, monkeypatch):
    source = canonical_source(tmp_path / 'authored/workflow.py')
    dispatch = Mock(return_value=engine.ProductionRunResult(outputs=()))
    monkeypatch.setattr(engine, '_run_profile_result', dispatch)
    assert engine.run_workflow_path(source, tmp_path / 'out', task_identity='fake-task') == ()
    dispatch.assert_called_once()


@pytest.mark.parametrize('target', [None, 'auto'])
@pytest.mark.parametrize('dry_run', [False, True])
def test_sdk_bundled_snapshot_is_refused_before_custody_probe_or_dispatch(tmp_path, monkeypatch, target, dry_run):
    import vibecomfy
    import astrid.sdk as sdk
    from astrid.core.generation import preflight
    from astrid.sdk.generation import GenerationFacade
    root = tmp_path / 'library'
    source = canonical_source(root / 'ready_templates/video/wan_fake.py')
    monkeypatch.setattr(vibecomfy, '__file__', str(root / 'vibecomfy/__init__.py'))
    probe = Mock(side_effect=AssertionError('bundled raw workflow probed'))
    import_file = Mock(side_effect=AssertionError('unqualified source entered custody'))
    dispatch = Mock(side_effect=AssertionError('unqualified source dispatched'))
    monkeypatch.setattr(preflight, 'select_generation_target', probe)
    monkeypatch.setattr(sdk, 'invoke_result', dispatch)
    from types import SimpleNamespace
    client = SimpleNamespace(media=SimpleNamespace(import_file=import_file))
    with pytest.raises(AstridError) as caught:
        GenerationFacade().workflow(workflow=source, target=target, client=client,
                                    project='demo', dry_run=dry_run)
    assert caught.value.code == 'generation_route_unqualified'
    assert 'video/wan_fake' in str(caught.value) and caught.value.fix
    probe.assert_not_called()
    import_file.assert_not_called()
    dispatch.assert_not_called()


def test_central_typed_template_execute_refuses_before_dispatch(tmp_path, monkeypatch):
    dispatch = Mock(side_effect=AssertionError('typed Wan template dispatched'))
    monkeypatch.setattr(engine, '_run_profile_result', dispatch)
    request = tmp_path / 'request.json'
    request.write_text(json.dumps({
        'schema_version': 'astrid.vibecomfy.production-engine.v3',
        'workflow': {'template_id': 'video/wan22_animate_native_first_stage', 'bindings': {}},
        'references': {}, 'profile': engine._PROFILE_CONFIGS['pip_embedded'], 'capability_id': 'fake',
        'model_id': 'wan', 'template_id': 'video/wan22_animate_native_first_stage',
        'task_identity': 'fake-task'}))
    with pytest.raises(AstridError) as caught:
        engine.execute(request, tmp_path / 'out', tmp_path / 'result.json')
    assert caught.value.code == 'generation_route_unqualified'
    assert 'video/wan22_animate_native_first_stage' in str(caught.value) and caught.value.fix
    dispatch.assert_not_called()


def test_central_real_template_selector_retains_bundled_provenance(tmp_path, monkeypatch):
    source = tmp_path / 'selector.json'
    source.write_text(json.dumps({'template_id': 'video/wan22_animate_native_first_stage', 'bindings': {}}))
    dispatch = Mock(side_effect=AssertionError('raw Wan template dispatched'))
    monkeypatch.setattr(engine, '_run_profile_result', dispatch)
    with pytest.raises(AstridError) as caught:
        engine.run_workflow_path(source, tmp_path / 'out', task_identity='fake-task')
    assert caught.value.code == 'generation_route_unqualified'
    assert 'video/wan22_animate_native_first_stage' in str(caught.value) and caught.value.fix
    dispatch.assert_not_called()
