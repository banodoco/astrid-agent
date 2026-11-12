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
    monkeypatch.setattr(settings, "machine_policy", lambda _kind, client=None: preferences)
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
    monkeypatch.setattr(settings, "machine_policy", lambda _kind, client=None: saved)
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

    _saved, batch_calls, describe, claim = _claim_fixture(monkeypatch, preferences={})
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    code = machines_cli.main(["claim", "--gpu", "RTX 4090", "--for", "2h"],
                             describe=describe, claim=claim,
                             setup=lambda policy: pytest.fail("batch claim must not save setup"))
    assert code == 0 and len(batch_calls) == 1
    assert batch_calls[0]["preferences"]["gpu_types"] == ["RTX 4090"]
    assert batch_calls[0]["consent"].max_usd == 1
    assert batch_calls[0]["consent"].idle_seconds == 7200
    assert _saved == {}
    capsys.readouterr()

    _saved, _calls, describe, claim = _claim_fixture(monkeypatch, preferences={})
    code = machines_cli.main(["claim", "--max-usd", "3"], describe=describe, claim=claim,
                             setup=lambda policy: pytest.fail("incomplete batch claim must not save setup"))
    assert code == 2
    output = capsys.readouterr().out
    assert "machines setup" in output
    assert "--gpu" in output


def test_batch_first_claim_uses_empty_saved_preferences_without_saving_setup(monkeypatch, capsys):
    import sys

    _saved, calls, describe, claim = _claim_fixture(monkeypatch, preferences={})
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    def priced_description(**kwargs):
        return {"waiting_policy": {"preferences": {}},
                "prices": [{"gpu_type": "RTX 4090", "datacenter_id": "EU-RO-1", "hourly_usd": 0.5}]}

    result = machines_cli.main(["claim", "--gpu", "RTX 4090", "--for", "2h"],
                               describe=priced_description, claim=claim,
                               setup=lambda _policy: pytest.fail("batch claim must not save setup"))
    assert result == 0
    assert len(calls) == 1
    assert calls[0]["consent"].max_usd == 1
    assert calls[0]["preferences"]["gpu_types"] == ["RTX 4090"]
    assert _saved == {}
    capsys.readouterr()


def test_real_settings_lookup_uses_no_start_client_for_claim_and_inline_setup(monkeypatch, capsys):
    import sys

    from astrid.core.generation import settings

    events = []
    store_content = {"placement": {}, "defaults": {}, "machines": {"runpod": {
        "provider_account_ref": "env:RUNPOD_API_KEY", "credential_ref": "env:RUNPOD_API_KEY",
        "gpu_types": ["RTX 4090"], "image": DEFAULT_RUNPOD_WORKER_IMAGE, "budget_cap_usd": 2.0,
    }}}

    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def get_user_app_state(self, _key):
            events.append("settings")
            return {"content": store_content, "version": 1}

        def invoke_result(self, *_args, **_kwargs):
            events.append("setup")
            return SimpleNamespace(ok=True)

    class ClientFactory:
        @staticmethod
        def open_from_launcher(*, start_pack_host=True):
            events.append(("open", start_pack_host))
            return Client()

    monkeypatch.setattr("astrid.sdk.AstridClient", ClientFactory)
    settings.set_store_factory(lambda: pytest.fail("settings lookup must use the explicit Runtime client"))
    monkeypatch.setattr(machines_cli, "_leases", lambda: [{"id": "pod", "release_at": 2000000000}])
    monkeypatch.setattr("astrid.sdk.generation.GenerationFacade.claim",
                        lambda _facade, **_kwargs: SimpleNamespace(
                            ok=True, raw_result={"result": {"target_operation": {"pod_id": "pod"}}}))
    monkeypatch.setattr("astrid.sdk.generation._runpod_status",
                        lambda **_kwargs: {"offer": {"hourly_usd": 0.5}})
    assert machines_cli.main(["claim"], input_fn=lambda _prompt: pytest.fail("saved policy avoids setup")) == 0
    assert events[:2] == [("open", False), "settings"]
    capsys.readouterr()

    events.clear()
    store_content["machines"] = {}
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    assert machines_cli.main(["claim", "--gpu", "RTX 4090", "--max-usd", "2"],
                             volumes=lambda: [], datacenters=lambda: [],
                             offered_gpus=lambda _dc, _volume: ["RTX 4090"],
                             input_fn=lambda _prompt: "") == 0
    assert events[0] == ("open", False)
    assert events[1] == "settings"
    assert "setup" in events
    capsys.readouterr()


@pytest.mark.parametrize("argv, option", [(["claim", "--for", "forever"], "--for"),
                                           (["claim", "--idle", "0s"], "--idle")])
def test_claim_duration_errors_name_the_right_option_and_example(monkeypatch, capsys, argv, option):
    _saved, _calls, describe, claim = _claim_fixture(monkeypatch)
    assert machines_cli.main(argv, describe=describe, claim=claim) == 2
    message = capsys.readouterr().out
    assert f"{option} duration" in message
    assert "30m or 2h" in message


def test_claim_cli_supplies_paid_consent_authority(monkeypatch, capsys):
    _saved, calls, describe, claim = _claim_fixture(monkeypatch)
    assert machines_cli.main(["claim", "--max-usd", "3"], describe=describe, claim=claim) == 0
    assert calls[0]["consent"].consent == ("paid",)
    capsys.readouterr()
