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


def diagnostic_fixture(tmp_path, monkeypatch):
    """Synthetic subprocess fixture: exercises parsing only, not custody or a journey."""
    import hashlib
    import subprocess
    from types import SimpleNamespace
    from scripts import ci_journey_provider as provider

    root = tmp_path / "producer"
    files = {}
    for path in provider._PRODUCER_FILES:
        file = root / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(path.encode())
        files[path] = hashlib.sha256(path.encode()).hexdigest()
    monkeypatch.setattr(provider, "_PRODUCER_FILES", files)
    config = {"checkouts": {k: "/fixture/" + k for k in ("app", "agent", "runtime", "pinokio")},
              "pterm": "/fixture/pterm", "node": "/fixture/node", "node_sha256": "a" * 64,
              "pterm_entry_sha256": "b" * 64, "unws_sha256": "c" * 64}
    request = {"schema": REQUEST_SCHEMA, "candidate": provider._CANDIDATE.copy(),
               "provider": {"ref": provider._PRODUCER_REF, "sources": files.copy()},
               "stop": provider._STOP.copy(), "config_sha256": hashlib.sha256(provider._canonical(config)).hexdigest(),
               "run_id": "a" * 32}
    parsed = ProviderRequest.parse(encode(request))
    output = {"schema": "astrid.r24.producer-diagnostic.v1", "request_sha256": parsed.sha256,
              "action": {"end": "observer-error", "settlement": "unknown"}, "busy": "not-run",
              "unknown": "not-run", "restoration": "unknown", "runtime_identity": "unknown",
              "custody": "unknown", "protected_state": "unknown"}
    def fake_git(argv, **kwargs):
        args = argv[3:]
        if args == ["rev-parse", "--show-toplevel"]: return str(root).encode()
        if args == ["rev-parse", "HEAD"]: return provider._PRODUCER_REF.encode()
        if args[0] == "status": return b""
        if args[0] == "show": return args[1].split(":", 1)[1].encode()
        raise AssertionError(args)
    monkeypatch.setattr(subprocess, "check_output", fake_git)
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=0, stdout=encode(output).encode()))
    return provider, root, config, request, output


def test_source_backed_diagnostic_invocation_is_never_qualification(tmp_path, monkeypatch):
    provider, root, config, request, _ = diagnostic_fixture(tmp_path, monkeypatch)
    parsed = ProviderRequest.parse(encode(request))
    assert provider.invoke_linux_diagnostic(parsed, encode(config), str(root)) == provider.DiagnosticResult(parsed.sha256)


@pytest.mark.parametrize("mutation", ["tuple", "provider_ref", "source", "stop", "config", "forged_request"])
def test_diagnostic_invocation_rejects_wrong_binding(tmp_path, monkeypatch, mutation):
    provider, root, config, request, _ = diagnostic_fixture(tmp_path, monkeypatch)
    if mutation == "tuple": request["candidate"]["agent"] = "f" * 40
    elif mutation == "provider_ref": request["provider"]["ref"] = "f" * 40
    elif mutation == "source": request["provider"]["sources"][next(iter(provider._PRODUCER_FILES))] = "f" * 64
    elif mutation == "stop": request["stop"]["sha256"] = "f" * 64
    parsed = ProviderRequest.parse(encode(request))
    if mutation == "config": config["node"] = "/other"
    if mutation == "forged_request": parsed = ProviderRequest(parsed.canonical, "f" * 64)
    with pytest.raises(ProviderContractError):
        provider.invoke_linux_diagnostic(parsed, encode(config), str(root))


@pytest.mark.parametrize("mutation", ["schema", "request", "busy", "unknown", "restoration", "runtime_identity", "custody", "protected_state", "missing", "extra"])
def test_diagnostic_invocation_rejects_unexpected_output(tmp_path, monkeypatch, mutation):
    provider, root, config, request, output = diagnostic_fixture(tmp_path, monkeypatch)
    if mutation == "missing": del output["busy"]
    elif mutation == "extra": output["session"] = "untrusted"
    else: output[{"request": "request_sha256"}.get(mutation, mutation)] = "unexpected"
    with pytest.raises(ProviderContractError):
        provider.invoke_linux_diagnostic(ProviderRequest.parse(encode(request)), encode(config), str(root))


def test_diagnostic_invocation_rejects_altered_source(tmp_path, monkeypatch):
    provider, root, config, request, _ = diagnostic_fixture(tmp_path, monkeypatch)
    (root / next(iter(provider._PRODUCER_FILES))).write_text("changed")
    with pytest.raises(ProviderContractError):
        provider.invoke_linux_diagnostic(ProviderRequest.parse(encode(request)), encode(config), str(root))


def success_fixture(tmp_path, monkeypatch):
    """Use synthetic sources and mocked process output, never live evidence."""
    provider, root, config, request, _ = diagnostic_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(provider, "LIVE_PRODUCER_FILES", provider._PRODUCER_FILES)
    monkeypatch.setattr(provider, "_PRODUCER_REF", provider._SUCCESS_PRODUCER_REF)
    monkeypatch.setattr(provider, "LIVE_PRODUCER_REF", provider._SUCCESS_PRODUCER_REF)
    request["candidate"] = provider.LIVE_CANDIDATE.copy()
    request["stop"] = provider.LIVE_STOP.copy()
    request["provider"]["ref"] = provider.LIVE_PRODUCER_REF
    config.update({"pinokiod": "/fixture/pinokiod", "scratch_root": "/fixture/scratch",
                   "engine": {"pid": 123, "boot_id": "0" * 36, "start_ticks": "1",
                              "pinokio_home": "/fixture/home", "port": 42000}})
    request["config_sha256"] = __import__("hashlib").sha256(provider._canonical(config)).hexdigest()
    parsed = ProviderRequest.parse(encode(request))
    source = next(iter(provider.LIVE_PRODUCER_FILES))
    output = {"schema": RESULT_SCHEMA, "request_sha256": parsed.sha256,
              "observations": [{"kind": kind, "source": source,
                                "evidence_sha256": str(i) * 64, "safe": True}
                               for i, kind in enumerate(("busy", "unknown", "restoration"), 1)]}
    return provider, root, config, request, output


def test_success_invocation_passes_canonical_files_and_separate_stderr(tmp_path, monkeypatch):
    import subprocess
    import sys
    from pathlib import Path
    from types import SimpleNamespace

    provider, root, config, request, output = success_fixture(tmp_path, monkeypatch)
    parsed = ProviderRequest.parse(encode(request))
    def run(argv, **kwargs):
        assert argv[:4] == [sys.executable, "-I", "-B", str(root / "pinokio/providers/r24-linux.py")]
        assert len(argv) == 6
        assert Path(argv[4]).read_bytes() == parsed.canonical
        assert Path(argv[5]).read_bytes() == provider._canonical(config)
        assert kwargs["stdout"] == subprocess.PIPE and kwargs["stderr"] == subprocess.PIPE
        return SimpleNamespace(returncode=0, stdout=encode(output).encode() + b"\n", stderr=b"diagnostic")
    monkeypatch.setattr(subprocess, "run", run)
    result = provider.invoke_linux_provider(parsed, encode(config), str(root))
    assert result.result == ProviderResult.parse(encode(output), parsed)
    assert result.stderr == b"diagnostic"


@pytest.mark.parametrize("failure", ["nonzero", "empty", "extra", "duplicate", "malformed", "mismatch", "diagnostic"])
def test_success_invocation_rejects_failure_and_retains_stderr(tmp_path, monkeypatch, failure):
    import subprocess
    from types import SimpleNamespace
    provider, root, config, request, output = success_fixture(tmp_path, monkeypatch)
    stdout = encode(output).encode()
    code = 0
    if failure == "nonzero": code = 1
    elif failure == "empty": stdout = b""
    elif failure == "extra": stdout += b" extra"
    elif failure == "duplicate": stdout += b"\n" + stdout
    elif failure == "malformed": stdout = b"{"
    elif failure == "mismatch":
        output["request_sha256"] = "f" * 64
        stdout = encode(output).encode()
    elif failure == "diagnostic": stdout = b'{"schema":"astrid.r24.producer-diagnostic.v1"}'
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=code, stdout=stdout, stderr=b"separate"))
    with pytest.raises(provider.ProviderInvocationError) as exc:
        provider.invoke_linux_provider(ProviderRequest.parse(encode(request)), encode(config), str(root))
    assert exc.value.stderr == b"separate"


@pytest.mark.parametrize("mutation", ["candidate", "ref", "source", "config", "forged_request", "changed_file"])
def test_success_invocation_rejects_unbound_inputs(tmp_path, monkeypatch, mutation):
    provider, root, config, request, _ = success_fixture(tmp_path, monkeypatch)
    if mutation == "candidate": request["candidate"]["agent"] = "f" * 40
    elif mutation == "ref": request["provider"]["ref"] = "f" * 40
    elif mutation == "source": request["provider"]["sources"][next(iter(provider.LIVE_PRODUCER_FILES))] = "f" * 64
    elif mutation == "config": config["node"] = "/other"
    elif mutation == "changed_file": (root / next(iter(provider.LIVE_PRODUCER_FILES))).write_text("changed")
    parsed = ProviderRequest.parse(encode(request))
    if mutation == "forged_request": parsed = ProviderRequest(parsed.canonical, "f" * 64)
    with pytest.raises(ProviderContractError):
        provider.invoke_linux_provider(parsed, encode(config), str(root))


def test_final_live_provider_pins_are_exact():
    from scripts import ci_journey_provider as provider
    assert provider.LIVE_PRODUCER_REF == "cd86889f4299c7f7aca720f0f20a11be01dacb34"
    assert provider.LIVE_PRODUCER_FILES == {
        "pinokio/providers/r24-linux.py": "9fe1698833a0e6eb7a4c1337ad5fa0493db4444e6320d2d46b4ff2ccbfce1b2e",
        "pinokio/providers/r24-action.cjs": "042f533200723898aba2f15f78edfe98bc3197f9b161f4391b706a38469ca0f3",
        "pinokio/providers/r24-live-action.js": "0df87acefad6a365e97f7a846a7d6136e06d026840a10796f640396a7187ae78",
        "pinokio/providers/r24-linux-binding.json": "89582d987efb569bfc7f41a4ecb3ec800c9f3e10e7e849b0100a42a78b292f52",
        "pinokio/providers/r24-schema.json": "233f989b29f54d082e1287c200b70b14b0315a9d763d05bcff824e556c648ffc",
    }
    assert provider.LIVE_CANDIDATE == {
        "app": "ce1502e94b935fe6ef1e2f363fc6b55aade4a204",
        "agent": "2de0726d1eaea7f7bd141251e088aa9fdf224525",
        "runtime": "5453366458f7b4ee874c94d15ae7952e3e82d3c3",
        "pinokio": "3a6a98ae251ca126505245286e2d3e760f4348e9",
        "manifest_sha256": "920477210d01615a6698cda30f248552f13c712734e86ea56f283300cb1a9c87",
    }
    assert provider.LIVE_STOP == {
        "ref": "3a6a98ae251ca126505245286e2d3e760f4348e9",
        "path": "pinokio/stop.js",
        "sha256": "f9d62e4e6a826fccd9791ee211dae933f46c8f99019e145abb3db3bf9595fd48",
    }


def test_live_export_invocation_adds_only_fixed_export_argument(tmp_path, monkeypatch):
    import subprocess
    from pathlib import Path
    from types import SimpleNamespace

    provider, root, config, request, output = success_fixture(tmp_path, monkeypatch)
    parsed = ProviderRequest.parse(encode(request))
    export = tmp_path / "export"
    def run(argv, **kwargs):
        assert Path(argv[-1]) == export
        assert len(argv) == 7
        return SimpleNamespace(returncode=0, stdout=encode(output).encode(), stderr=b"")
    monkeypatch.setattr(subprocess, "run", run)
    result = provider.invoke_linux_provider_export(parsed, encode(config), str(root), str(export))
    assert result.returncode == 0
    assert result.result == ProviderResult.parse(encode(output), parsed)
