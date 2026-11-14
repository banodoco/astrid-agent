"""Missing target nodes must have a typed refusal and a usable next step."""
from types import SimpleNamespace

import pytest
from vibecomfy.workflow import VibeNode, VibeWorkflow, WorkflowSource

from astrid.core.contracts.errors import AstridError
from astrid.core.generation import entries
from astrid.core.generation.preflight import GenerationReadinessError


def test_attached_missing_nodes_name_the_install_or_target_recovery():
    workflow = VibeWorkflow('missing-nodes', WorkflowSource('missing-nodes'))
    workflow.nodes['1'] = VibeNode('1', 'BasicGuider')
    workflow.nodes['2'] = VibeNode('2', 'SamplerCustomAdvanced')
    client = SimpleNamespace(object_info=lambda: {})

    with pytest.raises(GenerationReadinessError) as caught:
        entries.target_schema({'id': 'h3'}, workflow, client)

    error = caught.value
    assert isinstance(error, AstridError)
    assert error.code == 'missing_node_pack'
    issues = error.readiness['issues']
    assert [issue['details']['node'] for issue in issues] == [
        'BasicGuider', 'SamplerCustomAdvanced']
    for issue in issues:
        assert repr(issue['details']['node']) in issue['message']
        assert 'install it on the requested ComfyUI target' in issue['message']
        assert 'choose a target that provides it' in issue['message']
    assert str(error) == issues[0]['message']
