"""Source/fake tests for the fail-closed Pinokio engine boundary."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import pinokio_engine_boundary as boundary


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def source_candidate(tmp_path, monkeypatch):
    root = tmp_path / "candidate"
    root.mkdir()
    (root / "pinokio").mkdir()
    stop = b"'use strict';\nmodule.exports = { run: [app.customStep(app.stopOwnedStart)] };\n"
    meta = json.dumps({"title": "Astrid"}).encode()
    app = b"// fake Pinokio app identity fixture\nmodule.exports = { version: '8.0.0' };\n"
    (root / "pinokio/stop.js").write_bytes(stop)
    (root / "pinokio.json").write_bytes(meta)
    (root / "pinokio.js").write_bytes(app)
    monkeypatch.setattr(boundary, "PINOKIO_REF", "a" * 40)
    monkeypatch.setattr(boundary, "STOP_SHA256", _sha(stop))
    monkeypatch.setattr(boundary, "PINOKIO_JSON_SHA256", _sha(meta))
    monkeypatch.setattr(boundary, "PINOKIO_JS_SHA256", _sha(app))

    def fake_git(_root, *args):
        if args == ("rev-parse", "HEAD"):
            return "a" * 40
        if args == ("status", "--porcelain", "--untracked-files=all"):
            return ""
        raise AssertionError(args)

    monkeypatch.setattr(boundary, "_git", fake_git)
    return root


def _authority(tmp_path, name, protocol):
    folder = tmp_path / name
    folder.mkdir()
    provider = folder / "provider.py"
    provider.write_text("# source fake provider\n", encoding="utf-8")
    provider.chmod(0o755)
    value = {
        "protocol": protocol,
        "checkout": str(folder),
        "ref": "a" * 40,
        "file": str(provider),
        "sha256": _sha(provider.read_bytes()),
        "argv": [str(provider)],
        "timeout_seconds": 5,
    }
    if protocol == boundary.ENGINE_PROTOCOL:
        value.update(
            {
                "role": "pinned-owner-wrapper",
                "engine_identity": {
                    "name": boundary.ENGINE_NAME,
                    "version": boundary.ENGINE_VERSION,
                    "ref": boundary.ENGINE_REF,
                },
                "client_identity": {"name": boundary.CLIENT_NAME, "ref": boundary.CLIENT_REF},
            }
        )
    return value


def _engine_response(candidate, condition):
    action = Path(candidate["checkout"]) / "pinokio/stop.js"
    return {
        "protocol": boundary.ENGINE_PROTOCOL,
        "route_kind": "pterm-start-script",
        "transport": "pterm-websocket-json-rpc",
        "engine_entrypoint": "pinokiod.kernel.api.process",
        "candidate_ref": candidate["ref"],
        "app_root": candidate["checkout"],
        "app_identity": candidate["identity"],
        "action_path": str(action.resolve()),
        "action_sha256": candidate["stop"]["sha256"],
        "engine_name": boundary.ENGINE_NAME,
        "engine_ref": boundary.ENGINE_REF,
        "engine_version": boundary.ENGINE_VERSION,
        "client_name": boundary.CLIENT_NAME,
        "client_ref": boundary.CLIENT_REF,
        "downstream_argv": ["pterm", "start", "stop.js", "--ref", candidate["ref"]],
        "request_id": "request-fake-1",
        "task_id": "task-fake-1",
        "accepted": True,
        "settled": True,
        "terminal": True,
        "settlement": "terminal",
        "result": "refused",
        "refusal_reason": condition,
    }


def test_candidate_authenticates_immutable_stop_and_app_identity(source_candidate):
    observed = boundary.authenticate_candidate(source_candidate)
    assert observed["ref"] == "a" * 40
    assert observed["stop"] == {"path": "pinokio/stop.js", "sha256": boundary.STOP_SHA256}
    assert observed["identity"]["title"] == "Astrid"
    assert observed["identity"]["version"] == "8.0.0"


def test_supplied_malformed_ref_fails_closed(source_candidate):
    with pytest.raises(boundary.BoundaryError, match=r"malformed \(39 hex characters\)"):
        boundary.authenticate_candidate(source_candidate, ref=boundary.SUPPLIED_PINOKIO_REF)


def test_missing_action_engine_authority_fails_closed(source_candidate):
    with pytest.raises(boundary.BoundaryError, match="action engine authority absent"):
        boundary.run_stop_condition({"candidate_checkout": str(source_candidate)}, "busy")


@pytest.mark.parametrize("condition", ["busy", "unknown"])
def test_fake_boundary_protocol_refuses_and_restores_state(source_candidate, tmp_path, condition):
    engine = _authority(tmp_path, "engine", boundary.ENGINE_PROTOCOL)
    controller = _authority(tmp_path, "controller", boundary.CONDITION_PROTOCOL)
    config = {
        "candidate_checkout": str(source_candidate),
        "candidate_ref": "a" * 40,
        "engine": engine,
        "conditions": {condition: controller},
    }
    baseline = {
        "processes": [{"pid": 12, "start": "x"}],
        "listeners": [17340],
        "identity": "astrid",
    }
    active = {
        "processes": [{"pid": 12, "start": "x"}],
        "listeners": [17340],
        "identity": "astrid",
        "condition": condition,
    }
    snapshots = iter([baseline, active, baseline, baseline])
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        assert isinstance(argv, list)
        assert kwargs["shell"] is False
        assert kwargs["timeout"] <= boundary.TIMEOUT_MAX
        if argv[0] == controller["argv"][0]:
            op = argv[-1]
            if op == "observe":
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps(
                        {"protocol": boundary.CONDITION_PROTOCOL, "state": next(snapshots)}
                    ),
                    stderr="",
                )
            if op == "prepare":
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps(
                        {
                            "protocol": boundary.CONDITION_PROTOCOL,
                            "condition": condition,
                            "prepared": True,
                        }
                    ),
                    stderr="",
                )
            if op == "restore":
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps({"protocol": boundary.CONDITION_PROTOCOL, "restored": True}),
                    stderr="",
                )
        if argv[0] == engine["argv"][0]:
            action = Path(argv[argv.index("--action") + 1])
            assert action == source_candidate / "pinokio/stop.js"
            assert argv[argv.index("--protocol") + 1] == boundary.ENGINE_PROTOCOL
            assert argv[argv.index("--app-root") + 1] == str(source_candidate.resolve())
            route = json.loads(argv[argv.index("--downstream-argv-json") + 1])
            assert route == ["pterm", "start", "stop.js", "--ref", "a" * 40]
            return SimpleNamespace(
                returncode=42,
                stdout=json.dumps(
                    _engine_response(boundary.authenticate_candidate(source_candidate), condition)
                ),
                stderr="",
            )
        raise AssertionError(argv)

    result = boundary.run_stop_condition(config, condition, runner=fake_run)
    assert result["result"] == "refused-and-restored"
    assert result["state_unchanged"] is True
    assert result["invocation"]["downstream_argv"] == [
        "pterm",
        "start",
        "stop.js",
        "--ref",
        "a" * 40,
    ]
    assert result["observations"]["restored"] == baseline
    engine_call = next(call for call in calls if call[0][0] == engine["argv"][0])
    assert "--action" in engine_call[0]
    assert "--protocol" in engine_call[0]
    assert str(source_candidate / "pinokio/stop.js") in engine_call[0]
    assert "--app-root" in engine_call[0]
    assert "--downstream-argv-json" in engine_call[0]


def test_changed_descriptor_or_app_identity_is_rejected(source_candidate):
    (source_candidate / "pinokio/stop.js").write_text("// changed\n", encoding="utf-8")
    with pytest.raises(boundary.BoundaryError, match="bytes differ"):
        boundary.authenticate_candidate(source_candidate)


def test_pinokio_app_semantic_version_must_be_800(source_candidate, monkeypatch):
    app = b"module.exports = { version: '7.9.9' };\n"
    (source_candidate / "pinokio.js").write_bytes(app)
    monkeypatch.setattr(boundary, "PINOKIO_JS_SHA256", _sha(app))
    with pytest.raises(boundary.BoundaryError, match="semantic version is not exactly 8.0.0"):
        boundary.authenticate_candidate(source_candidate)


def test_condition_authority_missing_fails_closed_after_candidate_and_engine_auth(
    source_candidate, tmp_path
):
    engine = _authority(tmp_path, "engine", boundary.ENGINE_PROTOCOL)
    with pytest.raises(boundary.BoundaryError, match="busy condition authority absent"):
        boundary.run_stop_condition(
            {"candidate_checkout": str(source_candidate), "engine": engine}, "busy"
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("settled", False, "not terminally settled"),
        ("terminal", False, "not terminally settled"),
        ("settlement", "queued", "not terminally settled"),
        ("action_sha256", "0" * 64, "action_sha256 does not attest"),
        ("action_path", "/tmp/other/stop.js", "action_path does not attest"),
        ("downstream_argv", ["node", "pinokio/stop.js"], "downstream_argv does not attest"),
    ],
)
def test_fake_engine_queued_unsettled_or_mismatched_provenance_fails(
    source_candidate, tmp_path, field, value, message
):
    engine = _authority(tmp_path, "engine", boundary.ENGINE_PROTOCOL)
    controller = _authority(tmp_path, "controller", boundary.CONDITION_PROTOCOL)
    config = {
        "candidate_checkout": str(source_candidate),
        "engine": engine,
        "conditions": {"busy": controller},
    }
    before = {"processes": ["owned"], "listeners": [17340], "identity": "astrid"}
    active = {**before, "condition": "busy"}
    snapshots = iter([before, active, before, before])
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if argv[0] == controller["argv"][0]:
            operation = argv[-1]
            if operation == "observe":
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps(
                        {"protocol": boundary.CONDITION_PROTOCOL, "state": next(snapshots)}
                    ),
                    stderr="",
                )
            if operation == "prepare":
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps(
                        {
                            "protocol": boundary.CONDITION_PROTOCOL,
                            "condition": "busy",
                            "prepared": True,
                        }
                    ),
                    stderr="",
                )
            if operation == "restore":
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps({"protocol": boundary.CONDITION_PROTOCOL, "restored": True}),
                    stderr="",
                )
        payload = _engine_response(boundary.authenticate_candidate(source_candidate), "busy")
        payload[field] = value
        return SimpleNamespace(returncode=42, stdout=json.dumps(payload), stderr="")

    with pytest.raises(boundary.BoundaryError, match=message):
        boundary.run_stop_condition(config, "busy", runner=fake_run)
    assert any(call[0] == controller["argv"][0] and call[-1] == "restore" for call in calls)


def test_fake_destructive_stop_is_observed_before_provider_restore(source_candidate, tmp_path):
    engine = _authority(tmp_path, "engine", boundary.ENGINE_PROTOCOL)
    controller = _authority(tmp_path, "controller", boundary.CONDITION_PROTOCOL)
    config = {
        "candidate_checkout": str(source_candidate),
        "engine": engine,
        "conditions": {"busy": controller},
    }
    baseline = {"processes": ["owned"], "listeners": [17340], "identity": "astrid"}
    active = {**baseline, "condition": "busy"}
    destructive = {"processes": [], "listeners": [], "identity": "astrid"}
    snapshots = iter([baseline, active, destructive, baseline])
    events = []

    def fake_run(argv, **kwargs):
        if argv[0] == controller["argv"][0]:
            operation = argv[-1]
            events.append(operation)
            if operation == "observe":
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps(
                        {"protocol": boundary.CONDITION_PROTOCOL, "state": next(snapshots)}
                    ),
                    stderr="",
                )
            if operation == "prepare":
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps(
                        {
                            "protocol": boundary.CONDITION_PROTOCOL,
                            "condition": "busy",
                            "prepared": True,
                        }
                    ),
                    stderr="",
                )
            if operation == "restore":
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps({"protocol": boundary.CONDITION_PROTOCOL, "restored": True}),
                    stderr="",
                )
        events.append("stop")
        return SimpleNamespace(
            returncode=42,
            stdout=json.dumps(
                _engine_response(boundary.authenticate_candidate(source_candidate), "busy")
            ),
            stderr="",
        )

    with pytest.raises(boundary.BoundaryError, match="changed after Stop attempt"):
        boundary.run_stop_condition(config, "busy", runner=fake_run)
    assert (
        events.index("stop")
        < events.index("observe", events.index("stop"))
        < events.index("restore")
    )


def test_missing_engine_fails_before_condition_mutation(source_candidate, tmp_path):
    controller = _authority(tmp_path, "controller", boundary.CONDITION_PROTOCOL)
    config = {"candidate_checkout": str(source_candidate), "conditions": {"busy": controller}}

    def forbidden_runner(*_args, **_kwargs):
        raise AssertionError("condition provider must not be invoked without engine authority")

    with pytest.raises(boundary.BoundaryError, match="action engine authority absent"):
        boundary.run_stop_condition(config, "busy", runner=forbidden_runner)


def test_unpinned_engine_identity_fails_before_condition_mutation(source_candidate, tmp_path):
    engine = _authority(tmp_path, "engine", boundary.ENGINE_PROTOCOL)
    engine["engine_identity"]["ref"] = "0" * 40
    controller = _authority(tmp_path, "controller", boundary.CONDITION_PROTOCOL)
    config = {
        "candidate_checkout": str(source_candidate),
        "engine": engine,
        "conditions": {"busy": controller},
    }

    def forbidden_runner(*_args, **_kwargs):
        raise AssertionError("condition provider must not run under an unpinned engine identity")

    with pytest.raises(boundary.BoundaryError, match="engine identity/version/ref"):
        boundary.run_stop_condition(config, "busy", runner=forbidden_runner)
