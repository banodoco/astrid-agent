"""Focused R-24 journey-hook tests; all success data is synthetic."""
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
        "stop": {"ref": hook.PUBLISHED_CANDIDATE["pinokio"],
                 "path": "pinokio/stop.js", "sha256": "b" * 64},
        "config_sha256": "c" * 64,
        "run_id": run_id,
    }
    return provider.ProviderRequest.parse(canonical(data))


def export_fixture(root: Path, request, *, session="d" * 32):
    root.mkdir()
    rows = []
    common = {"schema": "astrid.r24.live-observation.v1",
              "request_sha256": request.sha256,
              "run_id": json.loads(request.canonical)["run_id"],
              "session": session, "engine": {"pid": 123},
              "provider_ref": provider.LIVE_PRODUCER_REF}
    values = {
        "busy": {**common, "kind": "busy", "engine_binding": {}, "busy": {},
                 "protected_before": {}, "protected_after": {}},
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


@pytest.mark.parametrize("tamper", ["run", "session", "request", "provider", "digest", "manifest", "stdout"])
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
    with pytest.raises((hook.JourneyProviderError, provider.ProviderContractError)):
        hook.validate_export(export, request, output)


def test_live_producer_binding_matches_published_tuple_and_diagnostic_does_not():
    assert hook.PUBLISHED_CANDIDATE == provider.LIVE_CANDIDATE
    assert provider.LIVE_STOP["ref"] == hook.PUBLISHED_CANDIDATE["pinokio"]
    assert provider._CANDIDATE != hook.PUBLISHED_CANDIDATE


def test_runner_identity_is_committed_and_distinct_from_installed_agent():
    runner = hook._runner_identity()
    assert runner["head"] != hook.PUBLISHED_CANDIDATE["agent"]
    assert runner["candidate_agent_distinct"] is True
    assert set(runner["sources"]) == {
        "scripts/ci_journey.py", "scripts/ci_journey_provider.py",
        "scripts/ci_journey_r24.py",
    }


def test_runner_identity_rejects_installed_agent_as_runner(monkeypatch):
    root = Path(hook.__file__).resolve().parent.parent
    actual_git = hook._git

    def installed_identity(path, *args):
        if args == ("rev-parse", "HEAD"):
            return hook.PUBLISHED_CANDIDATE["agent"].encode()
        return actual_git(path, *args)

    monkeypatch.setattr(hook, "_git", installed_identity)
    with pytest.raises(hook.JourneyProviderError, match="must differ"):
        hook._runner_identity()


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
    assert receipt["runner"]["head"] != hook.PUBLISHED_CANDIDATE["agent"]
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
