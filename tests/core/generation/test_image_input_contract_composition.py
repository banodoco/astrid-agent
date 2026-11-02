from __future__ import annotations

import pytest

from astrid.core.generation import entries
from vibecomfy.registry.ready import workflow_from_ready
from vibecomfy.workflow import VibeNode, VibeWorkflow, WorkflowSource


def test_load_image_upload_and_qwen_media_reference_coexist():
    workflow = VibeWorkflow('uploads', WorkflowSource('uploads'))
    workflow.nodes['1'] = VibeNode('1', 'LoadImage', inputs={'image': 'input.png'})
    workflow.nodes['2'] = VibeNode('2', 'CustomNode', inputs={'image': 'input.png'})
    workflow.register_input('load', '1', 'image', 'input.png', type='IMAGE')
    workflow.register_input('custom', '2', 'image', 'input.png', type='IMAGE')
    upload = {row['name']: row for row in entries._workflow_inputs(
        {'binds': {}, 'defaults': {}}, 'edit', workflow=workflow)}
    qwen = workflow_from_ready('edit/qwen_image_2_1_turbo_edit')
    reference = {row['name']: row for row in entries._workflow_inputs(
        {'binds': {}, 'defaults': {}}, 'edit', workflow=qwen)}

    assert upload['load']['type'] == 'file'
    assert upload['custom']['type'] == 'image'
    assert (reference['image']['type'], reference['image']['media_semantics']) == ('string', 'image')
    for port in (upload['load'], reference['image']):
        name = port['name']
        assert entries.validate_inputs({'inputs': [port]}, {name: 'input.png'}, required=False) == {name: 'input.png'}
        for invalid in (42, True, {'path': 'input.png'}):
            with pytest.raises(ValueError, match='requires a string'):
                entries.validate_inputs({'inputs': [port]}, {name: invalid}, required=False)
    with pytest.raises(ValueError, match='no declared scalar type'):
        entries.validate_inputs({'inputs': [upload['custom']]}, {'custom': 'input.png'}, required=False)
