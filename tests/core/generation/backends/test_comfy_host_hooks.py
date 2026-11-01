"""F07 ComfyHostHooks: fail-closed binding mapping and control results."""

from __future__ import annotations

import pytest

from astrid.core.execution.managed_tool_session import AdmissionToken, SessionBinding
from astrid.core.generation.backends.vibecomfy import (
    CheckoutServerAdapter,
    ComfyHostHooks,
    _binding_mismatch,
)

RESIDENCY = "sha256:" + "a" * 64
SOURCE = "sha256:" + "b" * 64
CONFIG = "sha256:" + "c" * 64


def _verified(**overrides):
    base = {
        "session_dir": "/var/lib/astrid/session-1",
        "runtime_instance_id": "runtime-1",
        "process_birth_id": "supervisor-birth-1",
        "server_url": "http://127.0.0.1:8188",
        "source_content_digest": SOURCE,
        "config_digest": CONFIG,
        "comfy_process_birth_id": "engine-birth-1",
    }
    base.update(overrides)
    return base


def _binding(**overrides):
    fields = {
        "session_id": "/var/lib/astrid/session-1",
        "runtime_instance_id": "runtime-1",
        "process_birth_id": "supervisor-birth-1",
        "endpoint": "http://127.0.0.1:8188",
        "source_digest": SOURCE,
        "config_digest": CONFIG,
        "execution_identity": RESIDENCY,
        "launch_generation": "engine-birth-1",
        "engine_birth_id": "engine-birth-1",
    }
    fields.update(overrides)
    return SessionBinding(**fields)


def _profile(**overrides):
    profile = {"residency_digest": RESIDENCY}
    profile.update(overrides)
    return profile


def _hooks(binding=None):
    return ComfyHostHooks(
        binding=binding or _binding(),
        hc03_profile=_profile(),
        endpoint_transport=None,
        verified=_verified(),
        expected_comfyui_version=None,
    )


def test_matching_evidence_passes_the_mapping():
    assert _binding_mismatch(_binding(), _verified(), _profile()) is None


def test_launch_generation_must_equal_the_engine_birth():
    mismatch = _binding_mismatch(_binding(launch_generation="other"), _verified(), _profile())
    assert mismatch == "binding launch_generation does not match verified evidence"


def test_engine_birth_must_equal_comfy_process_birth():
    mismatch = _binding_mismatch(_binding(engine_birth_id="old"), _verified(), _profile())
    assert mismatch == "binding engine_birth_id does not match verified evidence"


def test_missing_residency_digest_refuses_execution_identity():
    mismatch = _binding_mismatch(_binding(), _verified(), {})
    assert mismatch == "execution_identity unavailable: no residency digest in hc03 profile"


def test_residency_digest_must_match_the_binding():
    other = "sha256:" + "d" * 64
    mismatch = _binding_mismatch(_binding(), _verified(), _profile(residency_digest=other))
    assert mismatch == "binding execution_identity does not match the residency digest"


def test_runtime_epoch_is_refused_on_the_attached_path():
    mismatch = _binding_mismatch(_binding(runtime_epoch=3), _verified(), _profile())
    assert mismatch == "runtime_epoch is not verifiable on the attached path"


def test_open_host_session_refuses_a_non_binding():
    with pytest.raises(ValueError, match="requires a SessionBinding"):
        CheckoutServerAdapter.open_host_session(binding={}, hc03_profile={})


def test_cancel_and_release_fail_closed():
    hooks = _hooks()
    cancelled = hooks.cancel(reason="test")
    released = hooks.release(reason="test")
    assert cancelled["ok"] is False and cancelled["cancelled"] is False
    assert released["ok"] is False and released["released"] is False


def test_fence_is_recorded_and_blocks_invoke():
    hooks = _hooks()
    assert hooks.fence(reason="owner lost") == {"ok": True, "fenced": True, "reason": "owner lost"}
    token = AdmissionToken(
        token_id="t1",
        session_id=_binding().session_id,
        capability_id="cap",
        invocation_id="inv",
        generation=1,
        binding_identity=hooks._binding_identity(),
    )
    result = hooks.invoke(token=token, model_id="m", template_id="tpl")
    assert result["ok"] is False
    assert result["reason"].startswith("slot is fenced")


def test_invoke_refuses_a_token_for_another_session():
    hooks = _hooks()
    token = AdmissionToken(
        token_id="t1",
        session_id="other-session",
        capability_id="cap",
        invocation_id="inv",
        generation=1,
        binding_identity=hooks._binding_identity(),
    )
    result = hooks.invoke(token=token, model_id="m", template_id="tpl")
    assert result == {"ok": False, "reason": "token session does not match the bound owner"}


def test_invoke_requires_an_admission_token():
    result = _hooks().invoke(token={"token_id": "t1"}, model_id="m", template_id="tpl")
    assert result == {"ok": False, "reason": "invoke requires an AdmissionToken"}


def test_observe_refuses_a_different_binding():
    result = _hooks().observe(binding=_binding(endpoint="http://127.0.0.1:9999"))
    assert result == {"ok": False, "reason": "binding does not match the bound owner"}
