import pytest

@pytest.fixture(autouse=True)
def inspect_real_binding(monkeypatch):
    from astrid.core.generation.backends.vibecomfy import CheckoutServerAdapter
    original = CheckoutServerAdapter.open_host_session.__func__

    def probe(cls, *, binding, hc03_profile, **kwargs):
        session = hc03_profile['vibecomfy_session']
        print('BINDING_PROBE source_is_revision=', binding.source_digest == session['source_revision'],
              'source_is_content=', binding.source_digest == session['source_content_digest'],
              'identity_is_residency=', binding.execution_identity == hc03_profile['residency_digest'],
              'runtime_epoch=', binding.runtime_epoch,
              'launch_matches=', binding.launch_generation == session['comfy_process_birth_id'], flush=True)
        return original(cls, binding=binding, hc03_profile=hc03_profile, **kwargs)

    monkeypatch.setattr(CheckoutServerAdapter, 'open_host_session', classmethod(probe))
