"""Focused R-24 journey-hook tests; all success data is synthetic."""
import base64
import copy
import hashlib
import json
from pathlib import Path

import pytest

from scripts import ci_journey_provider as provider
from scripts import ci_journey_r24 as hook


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def request_fixture(run_id="a" * 32):
    data = {
        "schema": provider.REQUEST_SCHEMA,
        "candidate": hook.PUBLISHED_CANDIDATE,
        "provider": {"ref": provider.LIVE_PRODUCER_REF,
                     "sources": provider.LIVE_PRODUCER_FILES},
        "stop": provider.LIVE_STOP,
        "config_sha256": "c" * 64,
        "run_id": run_id,
    }
    return provider.ProviderRequest.parse(canonical(data))


def export_fixture(root: Path, request, *, session="d" * 32):
    root.mkdir()
    rows = []
    engine = {"pid": 123, "boot_id": "0" * 8 + "-" + "0" * 4 + "-" + "0" * 4 + "-" + "0" * 4 + "-" + "0" * 12,
              "start_ticks": "1", "port": 42000, "node_sha256": "e" * 64}
    runtime = {"pid": 456, "process_birth_id": "proc-start-ticks:2;boot:test",
               "runtime_instance_id": "runtime-1", "runtime_session_id": "runtime-session-1",
               "runtime_epoch": 1, "endpoint": "http://127.0.0.1:42001", "realm_id": "realm-1"}
    def captured(data):
        return {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                "base64": base64.b64encode(data).decode()}
    request_data = json.loads(request.canonical)
    action = {"schema": "astrid.r24.action-invocation.v1", "request_sha256": request.sha256,
              "run_id": request_data["run_id"], "session": session,
              "source": "pinokio/providers/r24-action.cjs",
              "source_sha256": provider.LIVE_PRODUCER_FILES["pinokio/providers/r24-action.cjs"],
              "engine": engine, "runtime_execution_sources": provider.LIVE_RUNTIME_EXECUTION_SOURCES,
              "returncode": 0, "exit_code": 0, "signal": None,
              "raw_result": captured(b'{"synthetic":"action"}'), "stderr": captured(b""),
              "raw_result_validated": True}
    action_digest = hashlib.sha256(canonical(action)).hexdigest()
    common = {"schema": "astrid.r24.live-observation.v1",
              "request_sha256": request.sha256,
              "run_id": request_data["run_id"], "session": session, "engine": engine,
              "provider_ref": provider.LIVE_PRODUCER_REF, "runtime": runtime,
              "runtime_execution_sources": provider.LIVE_RUNTIME_EXECUTION_SOURCES,
              "action_invocation_sha256": action_digest}
    values = {
        "busy": {**common, "kind": "busy", "engine_binding": {}, "busy": {},
                 "action_invocation": action, "protected_before": {}, "protected_after": {}},
        "unknown": {**common, "kind": "unknown", "unknown": {},
                    "protected_before": {}, "protected_after": {}},
        "restoration": {**common, "kind": "restoration", "restoration": {},
                        "protected_before": {}, "protected_after": {},
                        "control_removed": True},
    }
    source = "pinokio/providers/r24-action.cjs"
    for kind in ("busy", "unknown", "restoration"):
        raw = canonical(values[kind])
        (root / f"observation-{kind}.json").write_bytes(raw)
        rows.append({"kind": kind, "source": source,
                     "evidence_sha256": hashlib.sha256(raw).hexdigest(), "safe": True})
    result_data = {"schema": provider.RESULT_SCHEMA, "request_sha256": request.sha256,
                   "observations": rows}
    result_raw = canonical(result_data) + b"\n"
    (root / "observation-result.json").write_bytes(result_raw)
    (root / "junit.xml").write_bytes(b"<testsuite><testcase><skipped/></testcase></testsuite>\n")
    initial = []
    for path in sorted(root.iterdir()):
        raw = path.read_bytes()
        initial.append({"path": path.name, "bytes": len(raw),
                        "sha256": hashlib.sha256(raw).hexdigest()})
    manifest = {"schema": "astrid.r24.producer-export.v1",
                "qualification": "not-evaluated", "files": initial}
    (root / "manifest.json").write_bytes(canonical(manifest) + b"\n")
    parsed = provider.ProviderResult.parse(result_raw, request)
    return provider.LinuxProviderOutput(parsed, b"", canonical(result_data) + b"\n", 0)


def refresh_export(root: Path, request):
    """Rebind synthetic artifacts so an inner action-record tamper reaches its validator."""
    rows = []
    for kind in ("busy", "unknown", "restoration"):
        raw = (root / f"observation-{kind}.json").read_bytes()
        rows.append({"kind": kind, "source": "pinokio/providers/r24-action.cjs",
                     "evidence_sha256": hashlib.sha256(raw).hexdigest(), "safe": True})
    result_data = {"schema": provider.RESULT_SCHEMA, "request_sha256": request.sha256,
                   "observations": rows}
    result_raw = canonical(result_data) + b"\n"
    (root / "observation-result.json").write_bytes(result_raw)
    inventory = []
    for path in sorted(root.iterdir()):
        if path.name != "manifest.json":
            raw = path.read_bytes()
            inventory.append({"path": path.name, "bytes": len(raw),
                              "sha256": hashlib.sha256(raw).hexdigest()})
    manifest = {"schema": "astrid.r24.producer-export.v1",
                "qualification": "not-evaluated", "files": inventory}
    (root / "manifest.json").write_bytes(canonical(manifest) + b"\n")
    return provider.LinuxProviderOutput(provider.ProviderResult.parse(result_raw, request),
                                        b"", result_raw, 0)


def owner_root(tmp_path, run_id="a" * 32):
    root = tmp_path / "run"
    root.mkdir()
    (root / "tst-b1-owner.json").write_text(json.dumps({
        "schema_version": 1, "run_id": run_id,
        "root": str(root / "program"), "data_root": str(root / "data"),
    }))
    return root


def test_export_binds_stdout_artifacts_run_request_and_one_session(tmp_path):
    request = request_fixture()
    output = export_fixture(tmp_path / "export", request)
    session, inventory = hook.validate_export(tmp_path / "export", request, output)
    assert session == "d" * 32
    assert {row["path"] for row in inventory} == hook.EXPORT_NAMES


@pytest.mark.parametrize("tamper", ["run", "session", "request", "provider", "digest", "manifest", "stdout",
                                     "action", "action_stream", "runtime"])
def test_export_rejects_identity_and_inventory_tamper(tmp_path, tamper):
    request = request_fixture()
    export = tmp_path / "export"
    output = export_fixture(export, request)
    if tamper in {"run", "session", "request", "provider"}:
        path = export / "observation-unknown.json"
        value = json.loads(path.read_bytes())
        key = {"run": "run_id", "session": "session", "request": "request_sha256",
               "provider": "provider_ref"}[tamper]
        value[key] = "e" * (32 if tamper in {"run", "session"} else 64 if tamper == "request" else 40)
        path.write_bytes(canonical(value))
    elif tamper == "digest":
        value = json.loads(output.stdout)
        value["observations"][0]["evidence_sha256"] = "e" * 64
        output = provider.LinuxProviderOutput(
            provider.ProviderResult.parse(canonical(value), request), b"", canonical(value), 0)
    elif tamper == "manifest":
        value = json.loads((export / "manifest.json").read_bytes())
        value["qualification"] = "pass"
        (export / "manifest.json").write_bytes(canonical(value))
    elif tamper == "stdout":
        output = provider.LinuxProviderOutput(output.result, b"", b"{}", 0)
    elif tamper == "action":
        path = export / "observation-busy.json"
        value = json.loads(path.read_bytes())
        value["action_invocation"]["raw_result_validated"] = False
        digest = hashlib.sha256(canonical(value["action_invocation"])).hexdigest()
        path.write_bytes(canonical(value))
        for kind in ("busy", "unknown", "restoration"):
            member = export / f"observation-{kind}.json"
            current = json.loads(member.read_bytes())
            current["action_invocation_sha256"] = digest
            member.write_bytes(canonical(current))
        output = refresh_export(export, request)
    elif tamper == "action_stream":
        path = export / "observation-busy.json"
        value = json.loads(path.read_bytes())
        value["action_invocation"]["raw_result"]["base64"] = "WA=="
        digest = hashlib.sha256(canonical(value["action_invocation"])).hexdigest()
        path.write_bytes(canonical(value))
        for kind in ("busy", "unknown", "restoration"):
            member = export / f"observation-{kind}.json"
            current = json.loads(member.read_bytes())
            current["action_invocation_sha256"] = digest
            member.write_bytes(canonical(current))
        output = refresh_export(export, request)
    elif tamper == "runtime":
        path = export / "observation-unknown.json"
        value = json.loads(path.read_bytes())
        value["runtime"]["endpoint"] = "https://example.invalid"
        path.write_bytes(canonical(value))
        output = refresh_export(export, request)
    with pytest.raises((hook.JourneyProviderError, provider.ProviderContractError)):
        hook.validate_export(export, request, output)


def test_published_tuple_rejects_stale_producer_binding():
    assert hook.PUBLISHED_CANDIDATE == {
        "app": "933c53a6fb1a11e5f8a01335d6252016d703beec",
        "agent": "ab43bad818ef65ca1e929b973a2f61cd9fc4bf61",
        "runtime": "2776ba973b6c8f3555b5629b0840d9691a98d3c2",
        "pinokio": "3a6a98ae251ca126505245286e2d3e760f4348e9",
        "manifest_sha256": "b2584f0bb5a949890364004572a0182f2b568adcdba061e73b739e1d3d2d623f",
    }
    assert hook.PUBLISHED_CANDIDATE != provider.LIVE_CANDIDATE
    assert provider.LIVE_STOP["ref"] == hook.PUBLISHED_CANDIDATE["pinokio"]
    with pytest.raises(hook.JourneyProviderError,
                       match="producer binding candidate differs from published tuple"):
        hook._validate_producer_binding({"candidate": provider.LIVE_CANDIDATE},
                                        hook.PUBLISHED_CANDIDATE)


def test_hook_never_invokes_when_exact_candidate_validation_fails(tmp_path, monkeypatch):
    root = owner_root(tmp_path)
    called = False
    def reject(**unused):
        raise hook.JourneyProviderError("producer binding candidate differs from published tuple")
    def invoke(*unused):
        nonlocal called
        called = True
    monkeypatch.setattr(hook, "prepare_request", reject)
    monkeypatch.setattr(provider, "invoke_linux_provider_export", invoke)
    with pytest.raises(hook.JourneyProviderError):
        hook.execute_live_hook(run_root=root, candidate_file=tmp_path / "candidate",
                               config_file=tmp_path / "config", producer_root=tmp_path / "producer",
                               checkouts={name: tmp_path / name for name in ("app", "agent", "runtime", "pinokio")})
    assert called is False
    receipt = json.loads((root / "evidence/r24-live/r24-invocation.json").read_bytes())
    assert receipt["invocation"]["status"] == "not-run"
    assert receipt["admission"]["authenticated_custody"] is False


def test_success_shaped_hook_retains_stream_exit_export_and_awaits_custody(tmp_path, monkeypatch):
    root = owner_root(tmp_path)
    request = request_fixture()
    config = canonical({"synthetic": True})
    monkeypatch.setattr(hook, "prepare_request", lambda **unused: (
        request, config, {"candidate": hook.PUBLISHED_CANDIDATE},
        {"run_id": "a" * 32}))
    def invoke(unused_request, unused_config, unused_root, export):
        return export_fixture(Path(export), request)
    monkeypatch.setattr(provider, "invoke_linux_provider_export", invoke)
    receipt = hook.execute_live_hook(
        run_root=root, candidate_file=tmp_path / "candidate", config_file=tmp_path / "config",
        producer_root=tmp_path / "producer",
        checkouts={name: tmp_path / name for name in ("app", "agent", "runtime", "pinokio")})
    assert receipt["status"] == "captured-untrusted"
    assert receipt["invocation"]["exit"] == 0
    assert receipt["session"] == "d" * 32
    assert receipt["admission"] == {"status": "awaiting-evidence",
                                     "authenticated_custody": False,
                                     "qualification": "not-evaluated"}
    assert (root / "evidence/r24-live/provider-stdout.json").is_file()
    assert (root / "evidence/r24-live/provider-stderr.log").is_file()


def test_invocation_error_retains_raw_streams_and_exit(tmp_path, monkeypatch):
    root = owner_root(tmp_path)
    request = request_fixture()
    monkeypatch.setattr(hook, "prepare_request", lambda **unused: (
        request, b"{}", {"candidate": hook.PUBLISHED_CANDIDATE}, {"run_id": "a" * 32}))
    def fail(*unused):
        raise provider.ProviderInvocationError("failed", b"diagnostic", stdout=b"partial", returncode=7)
    monkeypatch.setattr(provider, "invoke_linux_provider_export", fail)
    with pytest.raises(provider.ProviderInvocationError):
        hook.execute_live_hook(
            run_root=root, candidate_file=tmp_path / "candidate", config_file=tmp_path / "config",
            producer_root=tmp_path / "producer",
            checkouts={name: tmp_path / name for name in ("app", "agent", "runtime", "pinokio")})
    receipt = json.loads((root / "evidence/r24-live/r24-invocation.json").read_bytes())
    assert receipt["invocation"]["exit"] == 7
    assert (root / "evidence/r24-live/provider-stdout.json").read_bytes() == b"partial"
    assert (root / "evidence/r24-live/provider-stderr.log").read_bytes() == b"diagnostic"


def test_post_validation_source_race_is_recorded_as_rejected(tmp_path, monkeypatch):
    root = owner_root(tmp_path)
    request = request_fixture()
    monkeypatch.setattr(hook, "prepare_request", lambda **unused: (
        request, b"{}", {"candidate": hook.PUBLISHED_CANDIDATE}, {"run_id": "a" * 32}))
    monkeypatch.setattr(provider, "invoke_linux_provider_export", lambda *unused: (
        _ for _ in ()).throw(provider.ProviderContractError("producer source digest mismatch")))
    with pytest.raises(provider.ProviderContractError):
        hook.execute_live_hook(
            run_root=root, candidate_file=tmp_path / "candidate", config_file=tmp_path / "config",
            producer_root=tmp_path / "producer",
            checkouts={name: tmp_path / name for name in ("app", "agent", "runtime", "pinokio")})
    receipt = json.loads((root / "evidence/r24-live/r24-invocation.json").read_bytes())
    assert receipt["status"] == "fail"
    assert receipt["invocation"] == {"status": "rejected", "exit": None}
