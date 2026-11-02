"""Focused typed-provider adapter checks; no observations are authenticated."""

import hashlib
import json

import pytest

from scripts import ci_journey_pinokio_provider as adapter
from scripts import ci_journey_provider as provider


def _input(tmp_path):
    config = {
        "checkouts": {name: f"/fixture/{name}" for name in ("app", "agent", "runtime", "pinokio")},
        "pterm": "/fixture/pterm",
        "node": "/fixture/node",
        "node_sha256": "a" * 64,
        "pterm_entry_sha256": "b" * 64,
        "unws_sha256": "c" * 64,
    }
    request = {
        "schema": provider.REQUEST_SCHEMA,
        "candidate": provider._CANDIDATE.copy(),
        "provider": {
            "ref": provider._SUCCESS_PRODUCER_REF,
            "sources": provider._SUCCESS_PRODUCER_FILES.copy(),
        },
        "stop": provider._STOP.copy(),
        "config_sha256": hashlib.sha256(provider._canonical(config)).hexdigest(),
        "run_id": "24" * 16,
    }
    return {"request": request, "config": config, "producer_root": str(tmp_path.resolve())}


def _output(raw):
    request = provider.ProviderRequest.parse(provider._canonical(raw["request"]))
    source = next(iter(provider._SUCCESS_PRODUCER_FILES))
    result = provider.ProviderResult.parse(json.dumps({
        "schema": provider.RESULT_SCHEMA,
        "request_sha256": request.sha256,
        "observations": [
            {"kind": kind, "source": source, "evidence_sha256": str(index) * 64, "safe": True}
            for index, kind in enumerate(("busy", "unknown", "restoration"), 1)
        ],
    }), request)
    return provider.LinuxProviderOutput(result, b"")


def test_adapter_invokes_typed_consumer_without_promoting_output(tmp_path, monkeypatch):
    raw = _input(tmp_path)
    expected = _output(raw)
    calls = []

    def invoke(request, config_json, producer_root):
        calls.append((request, config_json, producer_root))
        return expected

    monkeypatch.setattr(adapter, "invoke_linux_provider", invoke)
    assert adapter.invoke_linux_provider_for_journey(raw, tmp_path / "evidence") is expected
    assert len(calls) == 1
    assert json.loads(calls[0][1]) == raw["config"]
    assert calls[0][2] == raw["producer_root"]
    assert not (tmp_path / "evidence" / adapter.DIAGNOSTIC_NAME).exists()


def test_adapter_retains_separate_failure_diagnostic_and_fails_closed(tmp_path, monkeypatch):
    raw = _input(tmp_path)
    diagnostic = b'{"schema":"astrid.r24.producer-diagnostic.v1"}\n'

    def invoke(*_args):
        raise provider.ProviderInvocationError("producer failed", diagnostic)

    monkeypatch.setattr(adapter, "invoke_linux_provider", invoke)
    evidence = (tmp_path / "evidence").resolve()
    with pytest.raises(adapter.JourneyProviderError, match="failed closed"):
        adapter.invoke_linux_provider_for_journey(raw, evidence)
    assert (evidence / adapter.DIAGNOSTIC_NAME).read_bytes() == diagnostic


@pytest.mark.parametrize("mutation", ["extra", "relative_root", "request_extra", "config_extra"])
def test_adapter_rejects_unbound_materializer_inputs(tmp_path, monkeypatch, mutation):
    raw = _input(tmp_path)
    if mutation == "extra":
        raw["extra"] = True
    elif mutation == "relative_root":
        raw["producer_root"] = "producer"
    elif mutation == "request_extra":
        raw["request"]["extra"] = True
    elif mutation == "config_extra":
        raw["config"]["extra"] = True
    monkeypatch.setattr(
        adapter,
        "invoke_linux_provider",
        lambda *_args: pytest.fail("consumer must not be invoked"),
    )
    with pytest.raises(adapter.JourneyProviderError):
        adapter.invoke_linux_provider_for_journey(raw, (tmp_path / "evidence").resolve())
