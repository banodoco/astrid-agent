"""Source/fake protocol tests; no test here is hosted Pinokio evidence."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import pinokio_engine_boundary as boundary


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


@pytest.fixture
def authorities(tmp_path, monkeypatch):
    refs = {
        "candidate": "a" * 40,
        "pterm": "b" * 40,
        "pinokiod": "c" * 40,
        "condition": "d" * 40,
    }
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / "pinokio").mkdir()
    stop = b"stop source fixture\n"
    metadata = json.dumps({"title": "Astrid"}).encode()
    app = b"module.exports = { version: '8.0.0' };\n"
    (candidate / boundary.STOP_PATH).write_bytes(stop)
    (candidate / "pinokio.json").write_bytes(metadata)
    (candidate / "pinokio.js").write_bytes(app)

    pterm = tmp_path / "pterm"
    pterm.mkdir()
    entrypoint = pterm / boundary.CLIENT_ENTRYPOINT
    entrypoint.write_bytes(b"#!/usr/bin/env node\n// source/fake pterm\n")
    entrypoint.chmod(0o755)
    pterm_files = {
        "endpoint.js": b"endpoint fixture\n",
        "rpc.js": b"rpc fixture\n",
        "script.js": b"script fixture\n",
        "target.js": b"target fixture\n",
    }
    for relative, data in pterm_files.items():
        (pterm / relative).write_bytes(data)

    pinokiod = tmp_path / "pinokiod"
    (pinokiod / "server").mkdir(parents=True)
    (pinokiod / "kernel/api").mkdir(parents=True)
    daemon_files = {
        "server/socket.js": b"socket fixture\n",
        "kernel/api/index.js": b"kernel api fixture\n",
    }
    for relative, data in daemon_files.items():
        (pinokiod / relative).write_bytes(data)

    condition = tmp_path / "condition"
    condition.mkdir()
    provider = condition / "provider.py"
    provider.write_bytes(b"#!/usr/bin/env python3\n# source/fake condition provider\n")
    provider.chmod(0o755)

    monkeypatch.setattr(boundary, "PINOKIO_REF", refs["candidate"])
    monkeypatch.setattr(boundary, "STOP_SHA256", _sha(stop))
    monkeypatch.setattr(boundary, "PINOKIO_JSON_SHA256", _sha(metadata))
    monkeypatch.setattr(boundary, "PINOKIO_JS_SHA256", _sha(app))
    monkeypatch.setattr(boundary, "CLIENT_REF", refs["pterm"])
    monkeypatch.setattr(boundary, "ENGINE_REF", refs["pinokiod"])
    monkeypatch.setattr(
        boundary,
        "PTERM_SOURCE_DIGESTS",
        {relative: _sha(data) for relative, data in pterm_files.items()},
    )
    monkeypatch.setattr(
        boundary,
        "PINOKIOD_SOURCE_DIGESTS",
        {relative: _sha(data) for relative, data in daemon_files.items()},
    )

    roots = {
        candidate.resolve(): refs["candidate"],
        pterm.resolve(): refs["pterm"],
        pinokiod.resolve(): refs["pinokiod"],
        condition.resolve(): refs["condition"],
    }

    def fake_git(root, *args):
        if args == ("rev-parse", "HEAD"):
            return roots[root.resolve()]
        if args == ("status", "--porcelain", "--untracked-files=all"):
            return ""
        raise AssertionError(args)

    monkeypatch.setattr(boundary, "_git", fake_git)
    return {
        "refs": refs,
        "candidate": candidate,
        "pterm": pterm,
        "pterm_entrypoint": entrypoint,
        "pinokiod": pinokiod,
        "condition": condition,
        "provider": provider,
    }


def _engine(auth):
    pterm = {
        "protocol": boundary.ENGINE_PROTOCOL,
        "checkout": str(auth["pterm"]),
        "ref": auth["refs"]["pterm"],
        "file": str(auth["pterm_entrypoint"]),
        "sha256": _sha(auth["pterm_entrypoint"].read_bytes()),
        "argv": [str(auth["pterm_entrypoint"])],
        "timeout_seconds": 5,
    }
    return {
        "protocol": boundary.ENGINE_PROTOCOL,
        "role": "pterm-action-engine",
        "pterm": pterm,
        "engine_identity": {
            "name": boundary.ENGINE_NAME,
            "version": boundary.ENGINE_VERSION,
            "ref": auth["refs"]["pinokiod"],
        },
        "client_identity": {
            "name": boundary.CLIENT_NAME,
            "ref": auth["refs"]["pterm"],
        },
        "pinokiod": {
            "checkout": str(auth["pinokiod"]),
            "ref": auth["refs"]["pinokiod"],
            "version": boundary.ENGINE_VERSION,
            "files": dict(boundary.PINOKIOD_SOURCE_DIGESTS),
        },
        "pterm_source": {
            "checkout": str(auth["pterm"]),
            "ref": auth["refs"]["pterm"],
            "files": dict(boundary.PTERM_SOURCE_DIGESTS),
        },
    }


def _condition_authority(auth):
    return {
        "protocol": boundary.CONDITION_PROTOCOL,
        "checkout": str(auth["condition"]),
        "ref": auth["refs"]["condition"],
        "file": str(auth["provider"]),
        "sha256": _sha(auth["provider"].read_bytes()),
        "argv": [str(auth["provider"])],
        "timeout_seconds": 5,
    }


def _config(auth, condition):
    engine = _engine(auth)
    controller = _condition_authority(auth)
    resource = {
        "selector": boundary.PINOKIO_RESOURCE_REF,
        "app_id": boundary.PINOKIO_APP_ID,
        "checkout": str(auth["candidate"]),
        "checkout_ref": auth["refs"]["candidate"],
    }
    return (
        {
            "candidate_checkout": str(auth["candidate"]),
            "candidate_ref": auth["refs"]["candidate"],
            "engine": engine,
            "old_launcher": {
                "ref": boundary.APPROVED_OLD_LAUNCHER_REF,
                "sha256": boundary.APPROVED_OLD_UPDATE_SHA256,
            },
            "registered_resource": resource,
            "conditions": {condition: controller},
        },
        engine,
        controller,
        resource,
    )


def _state(auth, condition):
    return {
        "condition": condition,
        "processes": [{"pid": 11, "start": "stable"}],
        "listeners": [{"port": 17340, "owner": "astrid"}],
        "identity": "astrid-runtime",
        "candidate": auth["refs"]["candidate"],
        "resource": boundary.PINOKIO_RESOURCE_REF,
        "checkout": str(auth["candidate"].resolve()),
        "instance": "instance-1",
        "health": {"status": "healthy", "ready": True},
    }


def _resource_reader(auth, events, *, path=None, version=None, extra=None):
    def read(selector, timeout):
        events.append(("resource-status", selector))
        assert selector == boundary.PINOKIO_RESOURCE_REF
        assert 0 < timeout <= boundary.TIMEOUT_MAX
        status = {
            "ref": selector,
            "app_id": boundary.PINOKIO_APP_ID,
            "path": str(path or auth["candidate"].resolve()),
            "running_scripts": ["pinokio/start.js"],
        }
        if extra:
            status.update(extra)
        return {
            "engine_version": boundary.ENGINE_VERSION if version is None else version,
            "resource": status,
        }

    return read


def _runner(auth, engine, controller, condition, snapshots, events, *, action=None):
    calls = []

    def run(argv, **kwargs):
        calls.append((list(argv), kwargs))
        assert isinstance(argv, list)
        assert kwargs["shell"] is False
        assert kwargs["timeout"] <= boundary.TIMEOUT_MAX
        if argv[0] == controller["argv"][0]:
            operation = argv[argv.index("--condition") + 1]
            events.append(("condition", operation))
            if operation == "observe":
                payload = {
                    "protocol": boundary.CONDITION_PROTOCOL,
                    "state": next(snapshots),
                }
            elif operation == "prepare":
                assert argv[-2:] == ["--expected", condition]
                payload = {
                    "protocol": boundary.CONDITION_PROTOCOL,
                    "condition": condition,
                    "prepared": True,
                }
            else:
                assert operation == "restore"
                assert argv[-2:] == ["--expected", condition]
                payload = {
                    "protocol": boundary.CONDITION_PROTOCOL,
                    "restored": True,
                }
            return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")
        expected = [
            *engine["pterm"]["argv"],
            "start",
            boundary.STOP_PATH,
            "--ref",
            boundary.PINOKIO_RESOURCE_REF,
        ]
        assert argv == expected
        assert kwargs["stdin"] is subprocess.DEVNULL
        events.append(("pterm-start", tuple(argv)))
        if action is not None:
            return action
        marker = (
            "Astrid is busy (2 active tasks)."
            if condition == "busy"
            else "Astrid is still running. Stop it in Pinokio, then try again. fetch failed"
        )
        return SimpleNamespace(returncode=0, stdout=marker, stderr="")

    return run, calls


@pytest.mark.parametrize("condition", ["busy", "unknown"])
def test_source_fake_real_pterm_route_and_complete_restoration(authorities, condition):
    config, engine_raw, controller, _ = _config(authorities, condition)
    # Do not monkeypatch engine authentication: the source/fake checkout must
    # satisfy the same source/ref/file/argv checks as an authentic runner.
    authenticated = boundary._authenticate_engine(engine_raw)
    before = _state(authorities, "idle")
    active = _state(authorities, condition)
    snapshots = iter([before, active, active, before])
    events = []
    runner, _ = _runner(authorities, authenticated, controller, condition, snapshots, events)
    result = boundary.run_stop_condition(
        config,
        condition,
        runner=runner,
        resource_reader=_resource_reader(authorities, events),
    )
    assert result["evidence_class"] == "boundary-observation"
    assert result["result"] == "refused-and-restored"
    assert result["state_unchanged"] is True
    assert result["observations"] == {
        "before": before,
        "active": active,
        "after_stop": active,
        "restored": before,
    }
    assert result["invocation"]["downstream_argv"] == [
        *engine_raw["pterm"]["argv"],
        "start",
        "pinokio/stop.js",
        "--ref",
        boundary.PINOKIO_RESOURCE_REF,
    ]
    assert result["candidate"]["ref"] not in result["invocation"]["downstream_argv"]
    assert [event[0:2] for event in events] == [
        ("resource-status", boundary.PINOKIO_RESOURCE_REF),
        ("condition", "observe"),
        ("condition", "prepare"),
        ("condition", "observe"),
        ("pterm-start", tuple(result["invocation"]["downstream_argv"])),
        ("condition", "observe"),
        ("condition", "restore"),
        ("condition", "observe"),
    ]


def test_git_sha_cannot_be_used_as_pterm_resource_ref(authorities):
    config, _, _, _ = _config(authorities, "busy")
    config["registered_resource"]["selector"] = authorities["refs"]["candidate"]
    with pytest.raises(boundary.BoundaryError, match="registered pinokio"):
        boundary.run_stop_condition(
            config,
            "busy",
            runner=lambda *_a, **_k: pytest.fail("mutated"),
            resource_reader=lambda *_a: pytest.fail("resolved"),
        )


def test_resource_checkout_ref_mismatch_fails_before_status_or_mutation(authorities):
    config, _, _, _ = _config(authorities, "busy")
    config["registered_resource"]["checkout_ref"] = "e" * 40
    with pytest.raises(boundary.BoundaryError, match="differs from candidate ref"):
        boundary.run_stop_condition(
            config,
            "busy",
            runner=lambda *_a, **_k: pytest.fail("mutated"),
            resource_reader=lambda *_a: pytest.fail("resolved"),
        )


@pytest.mark.parametrize(
    ("reader_kwargs", "message"),
    [
        ({"path": Path("/definitely/not/the/candidate")}, "status checkout is unavailable"),
        ({"version": "9.9.9"}, "engine version is absent or mismatched"),
        ({"extra": {"app_id": "other"}}, "app identity mismatch"),
    ],
)
def test_live_resource_mismatch_fails_before_condition_prepare(authorities, reader_kwargs, message):
    config, _, _, _ = _config(authorities, "busy")
    events = []
    with pytest.raises(boundary.BoundaryError, match=message):
        boundary.run_stop_condition(
            config,
            "busy",
            runner=lambda *_a, **_k: pytest.fail("condition mutated"),
            resource_reader=_resource_reader(authorities, events, **reader_kwargs),
        )
    assert events == [("resource-status", boundary.PINOKIO_RESOURCE_REF)]


def test_missing_wrapper_or_fake_engine_cannot_replace_pterm(authorities):
    with pytest.raises(boundary.BoundaryError, match="authority absent"):
        boundary._authenticate_engine(None)
    engine = _engine(authorities)
    engine["role"] = "caller-wrapper"
    with pytest.raises(boundary.BoundaryError, match="wrapper/fake engine"):
        boundary._authenticate_engine(engine)
    engine = _engine(authorities)
    engine["pterm"]["file"] = str(authorities["provider"])
    with pytest.raises(boundary.BoundaryError, match="inside its pinned checkout"):
        boundary._authenticate_engine(engine)


def test_old_launcher_mismatch_fails_before_engine_or_resource(authorities, monkeypatch):
    config, _, _, _ = _config(authorities, "busy")
    config["old_launcher"]["ref"] = "0" * 40
    monkeypatch.setattr(
        boundary, "_authenticate_engine", lambda _raw: pytest.fail("engine authenticated")
    )
    with pytest.raises(boundary.BoundaryError, match="old-launcher provenance"):
        boundary.run_stop_condition(
            config,
            "busy",
            runner=lambda *_a, **_k: pytest.fail("mutated"),
            resource_reader=lambda *_a: pytest.fail("resolved"),
        )


@pytest.mark.parametrize("failure", ["disconnect", "destructive"])
def test_failed_stop_is_observed_before_restore(authorities, failure):
    condition = "unknown"
    config, engine_raw, controller, _ = _config(authorities, condition)
    engine = boundary._authenticate_engine(engine_raw)
    before = _state(authorities, "idle")
    active = _state(authorities, condition)
    after_stop = active if failure == "disconnect" else {**active, "processes": []}
    snapshots = iter([before, active, after_stop, before])
    action = (
        SimpleNamespace(returncode=0, stdout="", stderr="engine disconnected")
        if failure == "disconnect"
        else SimpleNamespace(
            returncode=0,
            stdout="Astrid is still running. Stop it in Pinokio, then try again.",
            stderr="",
        )
    )
    events = []
    runner, _ = _runner(
        authorities,
        engine,
        controller,
        condition,
        snapshots,
        events,
        action=action,
    )
    with pytest.raises(boundary.BoundaryError):
        boundary.run_stop_condition(
            config,
            condition,
            runner=runner,
            resource_reader=_resource_reader(authorities, events),
        )
    operations = [value for kind, value in events if kind == "condition"]
    assert operations == ["observe", "prepare", "observe", "observe", "restore", "observe"]


def test_restore_must_reproduce_condition_health_and_custody(authorities):
    condition = "busy"
    config, engine_raw, controller, _ = _config(authorities, condition)
    engine = boundary._authenticate_engine(engine_raw)
    before = _state(authorities, "idle")
    active = _state(authorities, condition)
    incomplete_restore = {**before, "health": {"status": "degraded", "ready": False}}
    snapshots = iter([before, active, active, incomplete_restore])
    events = []
    runner, _ = _runner(authorities, engine, controller, condition, snapshots, events)
    with pytest.raises(boundary.BoundaryError, match="complete baseline state"):
        boundary.run_stop_condition(
            config,
            condition,
            runner=runner,
            resource_reader=_resource_reader(authorities, events),
        )


def test_malformed_brief_ref_is_diagnosed_without_mutation(authorities):
    with pytest.raises(boundary.BoundaryError, match=r"malformed \(39 hex characters\)"):
        boundary.authenticate_candidate(authorities["candidate"], ref=boundary.SUPPLIED_PINOKIO_REF)


def test_cli_missing_real_authority_fails_closed(authorities, tmp_path, capsys):
    config_file = tmp_path / "config.json"
    config_file.write_text(
        json.dumps(
            {
                "candidate_checkout": str(authorities["candidate"]),
                "candidate_ref": authorities["refs"]["candidate"],
            }
        ),
        encoding="utf-8",
    )
    assert boundary.main(["--config", str(config_file), "--condition", "busy"]) == 2
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "refused"
    assert result["evidence_class"] == "none"
    assert "action engine authority absent" in result["error"]
