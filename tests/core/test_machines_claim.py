from types import SimpleNamespace
import json
import pytest

from astrid.core import machines_cli
from astrid.packs.runpod.target_selection import DEFAULT_RUNPOD_WORKER_IMAGE


def test_claim_discloses_policy_without_consent_prompt(monkeypatch, capsys):
    from astrid.core.generation import settings

    preferences = {
        "provider_account_ref": "env:RUNPOD_API_KEY",
        "gpu_types": ["RTX 4090"],
        "image": DEFAULT_RUNPOD_WORKER_IMAGE,
        "budget_cap_usd": 3.0,
    }
    monkeypatch.setattr(settings, "machine_policy", lambda _kind: preferences)
    calls = []
    monkeypatch.setattr(machines_cli, "_leases", lambda: [{"id": "pod", "release_at": 2000000000}])

    def claim(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(ok=True, raw_result={"result": {"target_operation": {"pod_id": "pod"}}})

    def no_prompt(prompt):
        raise AssertionError(f"unexpected prompt: {prompt}")

    result = machines_cli.main(
        ["claim", "--max-usd", "3"],
        describe=lambda **kwargs: {"waiting_policy": {"preferences": preferences},
                                   "offer": {"hourly_usd": 0.5}},
        claim=claim, input_fn=no_prompt,
    )
    output = capsys.readouterr()
    assert result == 0
    assert len(calls) == 1
    assert output.err.count("RunPod claim policy:") == 1


def _claim_fixture(monkeypatch, *, preferences=None, hourly=0.5):
    """Inject saved preferences and an available quote without provider access."""
    from astrid.core.generation import settings

    saved = preferences if preferences is not None else {
        "provider_account_ref": "env:RUNPOD_API_KEY", "gpu_types": ["RTX 4090"],
        "image": DEFAULT_RUNPOD_WORKER_IMAGE, "budget_cap_usd": 2.0,
        "idle_timeout_seconds": 1800,
    }
    monkeypatch.setattr(settings, "machine_policy", lambda _kind: saved)
    calls = []

    def describe(**kwargs):
        return {"waiting_policy": {"preferences": saved},
                "offer": {"hourly_usd": hourly}}

    def claim(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(ok=True, raw_result={"result": {"target_operation": {"pod_id": "pod"}}})

    monkeypatch.setattr(machines_cli, "_leases", lambda: [{"id": "pod", "release_at": 2000000000}])
    return saved, calls, describe, claim


def test_claim_for_bounds_spend_and_max_usd_bounds_lifetime(monkeypatch, capsys):
    for argv, expected in [
        (["claim", "--for", "2h"], (1.0, 7200)),
        (["claim", "--max-usd", "3"], (3.0, 21600)),
        (["claim", "--max-usd", "3", "--for", "2h"], (3.0, 7200)),
        (["claim", "--max-usd", "0.5", "--for", "2h"], (0.5, 3600)),
        (["claim"], (2.0, 14400)),
    ]:
        _saved, calls, describe, claim = _claim_fixture(monkeypatch)
        assert machines_cli.main(argv, describe=describe, claim=claim) == 0
        consent = calls[0]["consent"]
        assert (consent.max_usd, consent.idle_seconds) == expected
        capsys.readouterr()


def test_claim_cli_overrides_saved_gpu_and_datacenter_without_persisting(monkeypatch, capsys):
    saved, calls, describe, claim = _claim_fixture(monkeypatch)
    original = json.dumps(saved, sort_keys=True)
    assert machines_cli.main(["claim", "--gpu", "RTX 5090", "--datacenter", "EU-RO-1",
                              "--max-usd", "3"], describe=describe, claim=claim) == 0
    assert calls[0]["preferences"]["gpu_types"] == ["RTX 5090"]
    assert calls[0]["datacenter"] == "EU-RO-1"
    assert json.dumps(saved, sort_keys=True) == original
    capsys.readouterr()


def test_claim_idle_override_caps_saved_idle_retention(monkeypatch, capsys):
    _saved, calls, describe, claim = _claim_fixture(monkeypatch)
    assert machines_cli.main(["claim", "--max-usd", "3", "--idle", "10m"],
                             describe=describe, claim=claim) == 0
    assert calls[0]["requested_idle_seconds"] == 600
    capsys.readouterr()


def test_claim_collects_missing_setup_on_tty_but_never_prompts_in_batch(monkeypatch, capsys):
    import sys

    saved, calls, describe, claim = _claim_fixture(monkeypatch, preferences={})
    saved_policy = []
    prompts = iter(["", "", "RTX 4090", "3"])
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    def save_policy(policy):
        saved_policy.append(policy)
        saved.update(policy)

    code = machines_cli.main(["claim"], describe=describe, claim=claim,
                             setup=save_policy,
                             volumes=lambda: [], datacenters=lambda: [],
                             offered_gpus=lambda _dc, _volume: ["RTX 4090"],
                             input_fn=lambda prompt: next(prompts))
    assert code == 0
    assert len(saved_policy) == 1 and len(calls) == 1
    capsys.readouterr()

    _saved, batch_calls, describe, claim = _claim_fixture(monkeypatch, preferences={
        "provider_account_ref": "env:RUNPOD_API_KEY", "image": DEFAULT_RUNPOD_WORKER_IMAGE,
    })
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    code = machines_cli.main(["claim", "--gpu", "RTX 4090", "--max-usd", "3"],
                             describe=describe, claim=claim,
                             setup=lambda policy: pytest.fail("batch claim must not save setup"))
    assert code == 0 and len(batch_calls) == 1
    capsys.readouterr()

    _saved, _calls, describe, claim = _claim_fixture(monkeypatch, preferences={
        "provider_account_ref": "env:RUNPOD_API_KEY", "image": DEFAULT_RUNPOD_WORKER_IMAGE,
    })
    code = machines_cli.main(["claim", "--max-usd", "3"], describe=describe, claim=claim,
                             setup=lambda policy: pytest.fail("incomplete batch claim must not save setup"))
    assert code == 2
    output = capsys.readouterr().out
    assert "machines setup" in output
    assert "--gpu" in output


def test_claim_cli_supplies_paid_consent_authority(monkeypatch, capsys):
    _saved, calls, describe, claim = _claim_fixture(monkeypatch)
    assert machines_cli.main(["claim", "--max-usd", "3"], describe=describe, claim=claim) == 0
    assert calls[0]["consent"].consent == ("paid",)
    capsys.readouterr()
