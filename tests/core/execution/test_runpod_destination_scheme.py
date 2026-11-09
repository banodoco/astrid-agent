"""RunPod's control-plane destinations are declared with an explicit https:// scheme.

A bare ``host:port`` declaration is scheme-agnostic in the broker, so it would admit
``http://host:443``. These tests pin the RunPod pack to scheme-qualified destinations
and check that plain http is refused for every one of them.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from astrid.core.execution.network_broker import ObservableNetworkBroker

PACK = Path(__file__).resolve().parents[3] / "astrid/packs/runpod/pack.yaml"


def _broker(*routes: str) -> ObservableNetworkBroker:
    return ObservableNetworkBroker().register_admission(
        {"network_nonce": "nonce", "admission_digest": "0" * 64}, allowed_routes=list(routes), auth_token="token")


def _runpod_destinations() -> list[str]:
    raw = yaml.safe_load(PACK.read_text(encoding="utf-8"))
    out: list[str] = []
    for action in raw["actions"].values():
        policy = action.get("metadata", {}).get("network_policy") or action.get("network_policy") or {}
        out.extend(policy.get("allowed_destinations", []))
    return out


def test_every_runpod_destination_declares_https_scheme():
    destinations = _runpod_destinations()
    assert destinations, "runpod pack declares no network destinations"
    assert all(d.startswith("https://") for d in destinations), [d for d in destinations if not d.startswith("https://")]


def test_runpod_destinations_admit_https_and_refuse_plain_http():
    destinations = _runpod_destinations()
    broker = _broker(*destinations)
    for destination in destinations:
        host_port = destination.removeprefix("https://")
        assert broker._route_allowed(f"https://{host_port}/"), destination
        assert not broker._route_allowed(f"http://{host_port}/"), destination
