"""The synchronous projection keeps Runtime identities after spool cleanup."""
from types import SimpleNamespace

import pytest

from astrid.core.generation.backends.base import GenerationResult
from astrid.sdk.results import InvocationResult, _reconstruct_generation_result


def terminal(*, ok=True):
    source = {'digest': 'sha256:' + 'a' * 64, 'sha256': 'a' * 64,
              'bytes': 17, 'filename': 'original.py'}
    settings = {'object_id': 'sha256:' + 'b' * 64, 'sha256': 'b' * 64,
                'bytes': 23, 'filename': 'executed-api.json'}
    association = {'association_id': 'assoc-manifest', 'output_port': 'result_manifest',
                   'object_id': 'sha256:' + 'c' * 64, 'filename': 'managed-generation-result.json'}
    settled = {'manifest_ref': association['object_id'], 'inputs': {'effective_settings': settings},
               'manifest_path': '/deleted/spool/result.json'}
    task = {'task_id': 'task-1', 'state': 'succeeded' if ok else 'failed',
            'spec': {'inputs': {'source': source}}, 'result': settled}
    result = InvocationResult('vibecomfy.run', 'executor', 'command', ok,
                              kernel_task_id='task-1', error=None if ok else {'message': 'producer failed'},
                              raw_result={'task': task, 'managed_outputs': [association]})
    return SimpleNamespace(result=result, source=source, settings=settings, association=association)


def test_terminal_projection_uses_task_manifest_and_input_refs():
    fixture = terminal()
    result = _reconstruct_generation_result(fixture.result)
    assert result.task_id == 'task-1'
    assert result.manifest_ref == fixture.association['object_id']
    assert result.result_manifest == fixture.association
    assert result.source_ref == fixture.source
    assert result.effective_settings_ref == fixture.settings
    assert '/deleted/spool' not in str(result.to_dict())
    assert GenerationResult.from_dict(result.to_dict()) == result


def test_manifest_association_alone_is_a_durable_projection():
    fixture = terminal()
    del fixture.result.raw_result['task']['result']['manifest_ref']
    result = _reconstruct_generation_result(fixture.result)
    assert result.manifest_ref == fixture.association['object_id']
    assert result.result_manifest['association_id'] == 'assoc-manifest'


def test_failed_readback_keeps_manifest_and_input_refs_on_exception():
    fixture = terminal(ok=False)
    with pytest.raises(Exception, match='producer failed') as caught:
        _reconstruct_generation_result(fixture.result)
    assert caught.value.task_id == 'task-1'
    assert caught.value.manifest_ref == fixture.association['object_id']
    assert caught.value.source_ref == fixture.source
    assert caught.value.effective_settings_ref == fixture.settings


def test_unassociated_staged_settings_are_not_public_object_refs():
    fixture = terminal()
    fixture.result.raw_result['task']['result']['inputs']['effective_settings'] = {
        'sha256': 'b' * 64, 'bytes': 23, 'filename': 'api.json', 'path': '/deleted/api.json'}
    result = _reconstruct_generation_result(fixture.result)
    assert result.effective_settings_ref is None


def test_runtime_nested_spec_and_associated_refs_drop_spool_locators():
    fixture = terminal()
    raw = fixture.result.raw_result
    raw['task']['spec'] = {'input_object_ids': [fixture.source['digest']],
                           'spec': raw['task']['spec']}
    raw['managed_outputs'][0]['path'] = '/deleted/spool/result.json'
    raw['task']['result']['inputs']['effective_settings'] = {
        **fixture.settings, 'path': '/deleted/spool/settings.json'}
    result = _reconstruct_generation_result(fixture.result)
    assert result.source_ref == fixture.source
    assert result.effective_settings_ref == fixture.settings
    assert '/deleted/spool' not in str(result.to_dict())


def test_runtime_unprefixed_cas_refs_keep_their_original_identity():
    fixture = terminal()
    fixture.source['digest'] = 'a' * 64
    fixture.settings['object_id'] = 'b' * 64
    fixture.association['object_id'] = 'c' * 64
    fixture.result.raw_result['task']['result']['manifest_ref'] = 'c' * 64
    result = _reconstruct_generation_result(fixture.result)
    assert result.manifest_ref == 'c' * 64
    assert result.result_manifest['object_id'] == 'c' * 64
    assert result.source_ref == fixture.source
    assert result.effective_settings_ref == fixture.settings
