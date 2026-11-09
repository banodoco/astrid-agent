"""The broker's destination matcher decides what the hivemind pack may reach.

Hivemind declares its Supabase destination with an explicit ``https://`` scheme. The
broker treats a scheme-qualified declaration as a scheme fence (http is refused) and a
bare ``host:port`` declaration as scheme-agnostic. These tests pin both behaviours.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from astrid.core.execution.network_broker import ObservableNetworkBroker

HOST = "ujlwuvkrxlvoswwkerdf.supabase.co"
PACK = Path(__file__).resolve().parents[3] / "astrid/core/hivemind/pack.yaml"


def _broker(*routes: str) -> ObservableNetworkBroker:
    return ObservableNetworkBroker().register_admission(
        {"network_nonce": "nonce", "admission_digest": "0" * 64}, allowed_routes=list(routes), auth_token="token")


def _pack_destinations() -> list[str]:
    raw = yaml.safe_load(PACK.read_text(encoding="utf-8"))
    out = []
    for action in raw["actions"].values():
        policy = action.get("metadata", {}).get("network_policy") or action.get("network_policy") or {}
        out.extend(policy.get("allowed_destinations", []))
    return out


def test_hivemind_pack_declares_https_destination_for_every_action():
    destinations = _pack_destinations()
    assert destinations, "hivemind pack declares no allowed destinations"
    assert set(destinations) == {f"https://{HOST}:443"}


def test_https_declaration_admits_https_and_fences_scheme_and_port():
    broker = _broker(f"https://{HOST}:443")
    assert broker._route_allowed(f"https://{HOST}:443/rest/v1/items")
    assert broker._route_allowed(f"https://{HOST}/rest/v1/items")  # default https port is 443
    assert not broker._route_allowed(f"http://{HOST}/rest/v1/items")  # scheme fence
    assert not broker._route_allowed(f"https://{HOST}:8443/rest/v1/items")  # port fence
    assert not broker._route_allowed("https://example.invalid/rest/v1/items")  # host fence


def test_bare_host_port_declaration_is_scheme_agnostic():
    broker = _broker(f"{HOST}:443")
    assert broker._route_allowed(f"https://{HOST}:443/rest/v1/items")
    # The plain-host form also admits plain http to the same host:port: the scheme is not fenced.
    assert broker._route_allowed(f"http://{HOST}:443/rest/v1/items")
    assert not broker._route_allowed(f"https://{HOST}:8443/rest/v1/items")
