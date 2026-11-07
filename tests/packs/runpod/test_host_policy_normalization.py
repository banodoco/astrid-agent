"""Retained setup identity follows the authoritative preference serialization."""
from pathlib import Path
import socket

from astrid.core.execution.warm_hold import WarmHolds
from astrid.packs.runpod.adapter import TargetProviderBindings
from astrid.packs.runpod.target_selection import read_runpod_preferences


def test_setup_optional_volume_round_trip_retains_the_same_owner(tmp_path, monkeypatch):
    def forbidden(*args):
        raise AssertionError("setup/status attempted provider I/O")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setenv("ASTRID_RUNTIME_DATA_ROOT", str(tmp_path / "machine"))
    raw = {"provider_account_ref": "canonical-owner", "gpu_types": ["GPU fixture"],
        "image": "fixture/cuda130@sha256:" + "1" * 64,
        "volume": {"id": None, "name": None, "size_gb": 0, "mount_path": "/workspace"},
        "idle_timeout_seconds": 1800, "credential_ref": "env:FAKE_CREDENTIAL", "budget_cap_usd": 2}
    source = Path(__file__).resolve().parents[3]
    holds = WarmHolds()
    bindings = TargetProviderBindings(pack_roots=[source / "astrid/packs/runpod"], holds=holds,
        credentials={"FAKE_CREDENTIAL": "synthetic-only"}.__getitem__,
        environ={"ASTRID_RUNPOD_SOURCE_ROOT": str(source)})
    policy = {"kind": "runpod", "engine": "comfyui", "preferences": raw}
    try:
        setup = bindings.operate(policy, "setup")
        assert setup["state"] == "saved"
        owner = bindings.provider
        canonical = read_runpod_preferences().as_mapping()
        assert "volume" not in canonical
        assert setup["preferences"] == canonical
        assert bindings.operate({**policy, "preferences": canonical}, "status") == {"state": "absent"}
        assert bindings.provider is owner
        assert bindings.operate(policy, "setup")["state"] == "saved"
        assert bindings.provider is owner
        assert policy["preferences"] is raw and "volume" in raw
    finally:
        bindings.close()
        holds.close()
