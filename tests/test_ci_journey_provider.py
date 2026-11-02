"""The proposed wire format does not grant provider admission."""
import copy
import json

import pytest

from scripts.ci_journey_provider import (
    ProviderContractError, ProviderRequest, ProviderResult, REQUEST_SCHEMA, RESULT_SCHEMA,
)


def request_data():
    return {
        "schema": REQUEST_SCHEMA,
        "candidate": {"app": "1" * 40, "agent": "2" * 40, "runtime": "3" * 40,
                      "pinokio": "4" * 40, "manifest_sha256": "5" * 64},
        "provider": {"ref": "6" * 40, "sources": {"pinokio/providers/r24.py": "7" * 64}},
        "stop": {"ref": "4" * 40, "path": "pinokio/stop.js", "sha256": "8" * 64},
        "config_sha256": "9" * 64,
        "run_id": "a" * 32,
    }


def result_data(request):
    return {"schema": RESULT_SCHEMA, "request_sha256": request.sha256,
            "observations": [{"kind": kind, "source": "pinokio/providers/r24.py",
                              "evidence_sha256": str(index) * 64, "safe": True}
                             for index, kind in enumerate(("busy", "unknown", "restoration"), 1)]}


def encode(value):
    return json.dumps(value)


def test_valid_contract_binding():
    source = request_data()
    request = ProviderRequest.parse(encode(source))
    result = ProviderResult.parse(encode(result_data(request)), request)
    assert result.request_sha256 == request.sha256
    assert {row.kind for row in result.observations} == {"busy", "unknown", "restoration"}
    assert ProviderRequest.parse(json.dumps(source, sort_keys=True)).sha256 == request.sha256
    source["candidate"]["app"] = "f" * 40
    assert ProviderRequest.parse(encode(source)).sha256 != request.sha256


@pytest.mark.parametrize("mutation", ["missing", "extra", "bad_sha", "foreign_stop", "bad_path", "empty_sources", "bad_run"])
def test_request_rejects_missing_malformed_or_mismatched(mutation):
    data = request_data()
    if mutation == "missing": del data["candidate"]["agent"]
    elif mutation == "extra": data["candidate"]["other"] = "x"
    elif mutation == "bad_sha": data["provider"]["ref"] = "G" * 40
    elif mutation == "foreign_stop": data["stop"]["ref"] = "f" * 40
    elif mutation == "bad_path": data["provider"]["sources"] = {"../provider": "7" * 64}
    elif mutation == "empty_sources": data["provider"]["sources"] = {}
    elif mutation == "bad_run": data["run_id"] = "x"
    with pytest.raises(ProviderContractError):
        ProviderRequest.parse(encode(data))


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "unknown", "unsafe", "truthy", "bad_digest", "foreign_source", "wrong_binding", "extra"])
def test_result_rejects_incomplete_unsafe_or_foreign(mutation):
    request = ProviderRequest.parse(encode(request_data()))
    data = result_data(request)
    if mutation == "missing": data["observations"].pop()
    elif mutation == "duplicate": data["observations"][2]["kind"] = "busy"
    elif mutation == "unknown": data["observations"][2]["kind"] = "other"
    elif mutation == "unsafe": data["observations"][0]["safe"] = False
    elif mutation == "truthy": data["observations"][0]["safe"] = 1
    elif mutation == "bad_digest": data["observations"][0]["evidence_sha256"] = "bad"
    elif mutation == "foreign_source": data["observations"][0]["source"] = "foreign"
    elif mutation == "wrong_binding": data["request_sha256"] = "f" * 64
    elif mutation == "extra": data["observations"][0]["verdict"] = "pass"
    with pytest.raises(ProviderContractError):
        ProviderResult.parse(encode(data), request)


def test_duplicate_keys_and_invalid_json_rejected():
    request = ProviderRequest.parse(encode(request_data()))
    for raw in ("{", "{\"schema\": 1, \"schema\": 2}", "NaN"):
        with pytest.raises(ProviderContractError): ProviderRequest.parse(raw)
    raw = encode(result_data(request)).replace("\"safe\": true", "\"safe\": true, \"safe\": false", 1)
    with pytest.raises(ProviderContractError): ProviderResult.parse(raw, request)


def test_result_does_not_follow_changed_tuple_source_stop_config_or_run():
    original = request_data()
    request = ProviderRequest.parse(encode(original))
    result = encode(result_data(request))
    for field in ("tuple", "source", "stop", "config", "run"):
        changed = copy.deepcopy(original)
        if field == "tuple": changed["candidate"]["agent"] = "f" * 40
        elif field == "source": changed["provider"]["sources"]["pinokio/providers/r24.py"] = "f" * 64
        elif field == "stop": changed["stop"]["sha256"] = "f" * 64
        elif field == "config": changed["config_sha256"] = "f" * 64
        elif field == "run": changed["run_id"] = "f" * 32
        with pytest.raises(ProviderContractError):
            ProviderResult.parse(result, ProviderRequest.parse(encode(changed)))
