"""The provider credential name is restricted, and the key value never leaves the host."""
import json
from pathlib import Path

import pytest

from astrid.core.execution.warm_hold import WarmHolds
from astrid.packs.runpod.adapter import PaidSelectionError, TargetProviderBindings, build_host_adapter

SECRET = "sk-fixture-SECRET-VALUE"


def _raw(credential_ref: str) -> dict:
    return {"provider_account_ref": "canonical-owner", "gpu_types": ["GPU fixture"],
            "image": "fixture/cuda130@sha256:" + "1" * 64,
            "volume": {"id": None, "name": None, "size_gb": 0, "mount_path": "/workspace"},
            "idle_timeout_seconds": 1800, "credential_ref": credential_ref, "budget_cap_usd": 2}


@pytest.mark.parametrize("credential_ref", ["env:OTHER_SECRET", "env:PATH", "env:runpod_api_key",
                                            "env:RUNPOD_API_KEY_X/../PATH"])
def test_host_refuses_a_credential_name_outside_runpod_prefix(credential_ref):
    fetched = []

    def credentials(name):
        fetched.append(name)
        return SECRET

    holds = WarmHolds()
    try:
        # Either the preference validator or the host adapter refuses; neither may resolve the name.
        with pytest.raises((PaidSelectionError, ValueError)):
            build_host_adapter(preferences=_raw(credential_ref), engine="comfyui", holds=holds,
                               quiesce=lambda *a, **k: None, credentials=credentials, environ={})
    finally:
        holds.close()
    assert fetched == [], "the host must not resolve a refused credential name"


def test_api_key_value_never_appears_in_results_or_error_records(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTRID_RUNTIME_DATA_ROOT", str(tmp_path / "machine"))
    source = Path(__file__).resolve().parents[3]
    holds = WarmHolds()
    bindings = TargetProviderBindings(pack_roots=[source / "astrid/packs/runpod"], holds=holds,
                                      credentials={"RUNPOD_API_KEY": SECRET}.__getitem__, environ={})
    policy = {"kind": "runpod", "engine": "comfyui", "preferences": _raw("env:RUNPOD_API_KEY")}
    seen = []
    try:
        for action in ("setup", "status", "ls"):
            try:
                seen.append(json.dumps(bindings.operate(policy, action), default=str))
            except Exception as exc:  # error records are part of the surface too
                seen.append(f"{type(exc).__name__}: {exc}")
        # An error path: a refused credential name must not echo the secret.
        bad = {**policy, "preferences": _raw("env:OTHER_SECRET")}
        try:
            bindings.operate(bad, "setup")
        except Exception as exc:
            seen.append(f"{type(exc).__name__}: {exc}")
    finally:
        bindings.close()
        holds.close()
    assert seen, "expected at least one recorded result"
    for text in seen:
        assert SECRET not in text
