import json
from dataclasses import fields
import pytest

@pytest.fixture(autouse=True)
def inspect_real_binding(monkeypatch):
    from astrid.core.generation.backends.vibecomfy import CheckoutServerAdapter
    original = CheckoutServerAdapter.open_host_session.__func__
    def probe(cls, *, binding, hc03_profile, **kwargs):
        s = hc03_profile['vibecomfy_session']
        expected = {'session_id': s['session_dir'], 'runtime_instance_id': hc03_profile['runtime']['runtime_instance_id'], 'process_birth_id': s['process_birth_id'], 'endpoint': s['server_url'], 'source_digest': s['source_content_digest'], 'config_digest': s['config_digest'], 'execution_identity': hc03_profile['residency_digest'], 'runtime_epoch': None, 'launch_generation': s['comfy_process_birth_id'], 'engine_birth_id': s['comfy_process_birth_id']}
        print('BINDING_CAPTURE ' + json.dumps({'actual': {f.name: getattr(binding, f.name) for f in fields(binding)}, 'expected': expected, 'host_provided': kwargs.get('host') is not None}, sort_keys=True), flush=True)
        result = original(cls, binding=binding, hc03_profile=hc03_profile, **kwargs)
        print('REAL_FACTORY_ACCEPTED ' + type(result).__name__, flush=True)
        original_release = result.release
        def release(*, reason):
            evidence = original_release(reason=reason)
            print('REAL_RELEASE_RESULT ' + json.dumps(evidence, sort_keys=True), flush=True)
            return evidence
        result.release = release
        return result
    monkeypatch.setattr(CheckoutServerAdapter, 'open_host_session', classmethod(probe))
