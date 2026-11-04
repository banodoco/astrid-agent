"""A missing H3 adapter cannot fall back to the independent local launcher."""
from types import SimpleNamespace

import pytest

from astrid.sdk import invocation
from astrid.sdk.exceptions import CapabilityPreconditionError


@pytest.mark.parametrize('kind', ['action', 'orchestrator'])
@pytest.mark.parametrize('task_marker', [None, 'caller-supplied-marker'])
def test_h3_requires_parent_adapter_before_launcher_or_admission(monkeypatch, kind, task_marker):
    if task_marker is None:
        monkeypatch.delenv('ASTRID_TASK_RUN_ID', raising=False)
    else:
        monkeypatch.setenv('ASTRID_TASK_RUN_ID', task_marker)
    capability = SimpleNamespace(id='h3_av.transform', capability_type='orchestrator')
    sdk = SimpleNamespace(_load_registries=lambda **_: (), get_capability=lambda *a, **k: capability)
    monkeypatch.setattr(invocation, '_sdk_module', lambda: sdk)
    monkeypatch.setattr(invocation, '_invoke_local_orchestrator',
                        lambda *a, **k: pytest.fail('started independent H3 children'))
    monkeypatch.setattr(invocation, '_kernel_invoke',
                        lambda *a, **k: pytest.fail('admitted a parent without an authenticated bundle mapping'))
    with pytest.raises(CapabilityPreconditionError, match='Runtime-admitted parent.*adapter'):
        invocation.invoke('h3_av.transform', kind=kind, project='project',
                          inputs={'request': 'request.json', 'asset_map': 'map.json'})
