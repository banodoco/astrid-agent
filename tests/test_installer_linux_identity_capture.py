"""The opt-in comparison record never changes identity decisions."""
import json
from unittest.mock import Mock

import pytest
from runtime_protocol import platform

from astrid.installer.backend import Backend

PID = 4242
LEGACY = "proc-start-ticks:123456"
SCOPED = LEGACY + ";boot:12345678-1234-4234-8234-123456789abc;pidns:4026531836"


def _capture(capsys):
    output = capsys.readouterr().err
    assert output.count("\n") == 1
    assert len(output) < 1024
    return json.loads(output.removeprefix("installer-linux-identity "))


def _run(monkeypatch, observed, bridge):
    identity = Mock(return_value=observed)
    monkeypatch.setattr(platform, "process_identity", identity)
    monkeypatch.setattr(platform, "legacy_linux_birth_matches_live_owner", bridge, raising=False)
    try:
        result = Backend._discovery_owner_state({"pid": PID, "process_birth_id": LEGACY})
    except RuntimeError as exc:
        result = str(exc)
    identity.assert_called_once_with(PID)
    return result


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("case", ["exact", "bridge_true", "bridge_false", "pid_mismatch", "tick_mismatch", "missing"])
def test_capture_preserves_comparison(monkeypatch, capsys, enabled, case):
    monkeypatch.setattr("astrid.installer.backend.sys.platform", "linux")
    if enabled:
        monkeypatch.setenv("ASTRID_LINUX_IDENTITY_CAPTURE", "1")
    else:
        monkeypatch.delenv("ASTRID_LINUX_IDENTITY_CAPTURE", raising=False)
    observed = {"pid": PID, "uid": 501, "birth_id": SCOPED}
    bridge = Mock(return_value=case == "bridge_true")
    if case == "exact":
        observed["birth_id"] = LEGACY
    if case == "pid_mismatch":
        observed["pid"] += 1
    if case == "tick_mismatch":
        observed["birth_id"] = SCOPED.replace("123456;", "123457;")
    if case == "missing":
        monkeypatch.delattr(platform, "legacy_linux_birth_matches_live_owner", raising=False)
        identity = Mock(return_value=observed)
        monkeypatch.setattr(platform, "process_identity", identity)
        with pytest.raises(RuntimeError, match="^Runtime discovery owner identity changed; refusing to change it\\.$"):
            Backend._discovery_owner_state({"pid": PID, "process_birth_id": LEGACY})
        identity.assert_called_once_with(PID)
    else:
        result = _run(monkeypatch, observed, bridge)
        if case in ("exact", "bridge_true"):
            assert result == "live"
        else:
            assert result == "Runtime discovery owner identity changed; refusing to change it."
        assert bridge.call_count == (0 if case in ("exact", "pid_mismatch") else 1)
    if not enabled:
        assert capsys.readouterr().err == ""
        return
    item = _capture(capsys)
    assert item["discovery_pid"] == PID and item["discovery_birth"] == LEGACY
    assert item["observed_pid"] == observed["pid"] and item["observed_uid"] == 501
    assert item["observed_birth"] == observed["birth_id"]
    assert item["pid_equal"] == (case != "pid_mismatch")
    assert item["birth_equal"] == (case == "exact")
    assert item["match"] == (case in ("exact", "bridge_true"))
    assert item["bridge"] == {"exact": "exact_match", "bridge_true": "true", "bridge_false": "not_true", "pid_mismatch": "not_called_pid_mismatch", "tick_mismatch": "not_true", "missing": "missing_attribute"}[case]


@pytest.mark.parametrize("match", [True, False])
def test_output_failure_cannot_change_decision(monkeypatch, match):
    monkeypatch.setattr("astrid.installer.backend.sys.platform", "linux")
    monkeypatch.setenv("ASTRID_LINUX_IDENTITY_CAPTURE", "1")
    monkeypatch.setattr("astrid.installer.linux_identity_capture.sys.stderr.write", Mock(side_effect=OSError("output failed")))
    result = _run(monkeypatch, {"pid": PID, "uid": 501, "birth_id": SCOPED}, Mock(return_value=match))
    assert result == ("live" if match else "Runtime discovery owner identity changed; refusing to change it.")


def test_record_does_not_emit_arbitrary_identity_text(monkeypatch, capsys):
    from astrid.installer.linux_identity_capture import record
    secret = "secret\n" * 10000
    record(PID, secret, {"pid": PID, "uid": 501, "birth_id": secret}, True, {})
    item = _capture(capsys)
    assert item["discovery_birth"] is None and item["observed_birth"] is None
    assert secret not in json.dumps(item)
