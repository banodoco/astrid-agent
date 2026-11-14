"""Account identity is installed on ordinary paid selection, before reads."""

import asyncio
from types import SimpleNamespace

import pytest

from astrid.packs.runpod import provider, target_selection as selection
from tests.packs.runpod.test_target_selection import case as case  # noqa: F401


def test_select_rotated_credential_shares_account_before_volume_lookup(case, tmp_path, monkeypatch):
    monkeypatch.setenv("ASTRID_RUNTIME_DATA_ROOT", str(tmp_path / "gate-data"))
    first = provider.account_gate("old-key", account_ref="account-a")
    first.after_response(429, "120")
    factory = case.owner.config_factory

    def rotated(offer):
        config = factory(offer)
        return SimpleNamespace(**{**vars(config), "api_key": "rotated-key"})

    case.owner.config_factory = rotated
    lookups = []

    async def volume(api_key, volume_id):
        lookups.append(api_key)
        # This assertion runs inside the ordinary _select path. The new key
        # has deliberately never been manually bound in the test setup.
        shared = provider.account_gate(api_key)
        assert shared.path == first.path
        with pytest.raises(provider.ProviderRefused):
            shared.before_call(shared.clock() + 1)
        raise RuntimeError("stop before create")

    monkeypatch.setattr(selection, "resolve_existing_volume", volume)
    with pytest.raises(selection.AcquisitionUnresolvedError):
        asyncio.run(case.owner._select(case.grant))
    assert lookups == ["rotated-key"]
    assert provider.account_gate("rotated-key").path == first.path
    assert case.creates == 0


def test_inflight_unbound_gate_cannot_publish_to_abandoned_account(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTRID_RUNTIME_DATA_ROOT", str(tmp_path))
    inflight = provider.account_gate("fake")
    bound = provider.account_gate("fake", account_ref="account-a")
    inflight.after_response(429, "120")
    with pytest.raises(provider.ProviderRefused):
        provider.account_gate("fake").before_call(bound.clock() + 1)
