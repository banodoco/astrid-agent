"""Focused tests for the source-shaped, explicitly modeled R0 owner records."""
import json
import os
import time

import pytest

from tests.r0_reality.discovery import R0Owner, darwin_birth_id, linux_birth_id, windows_birth_id


def test_birth_formats_and_observed_linux_example(monkeypatch):
    # The ticks here are the observed value cited in the TEST LADDER; this is
    # a format example, not a reconstructed complete discovery record.
    assert linux_birth_id(11220) == "proc-start-ticks:11220"
    assert windows_birth_id((1 << 32) | 7) == "windows-creation:4294967303"
    previous_tz = os.environ.get("TZ")
    monkeypatch.setenv("TZ", "UTC")
    if hasattr(time, "tzset"):
        time.tzset()
    try:
        local = time.localtime(0)
        assert darwin_birth_id(0) == ("ps-lstart:" + time.strftime("%a %b ", local)
                                      + f"{local.tm_mday:2d}" + time.strftime(" %H:%M:%S %Y", local))
    finally:
        if previous_tz is None:
            monkeypatch.delenv("TZ", raising=False)
        else:
            monkeypatch.setenv("TZ", previous_tz)
        if hasattr(time, "tzset"):
            time.tzset()


@pytest.mark.parametrize("fn,value", [(linux_birth_id, -1), (linux_birth_id, True),
                                      (windows_birth_id, -1), (windows_birth_id, 2**64)])
def test_birth_inputs_are_not_silently_fabricated(fn, value):
    with pytest.raises(ValueError):
        fn(value)


def test_matching_owner_records_and_leftover_marker(tmp_path):
    owner = R0Owner(321, linux_birth_id(11220), "modeled-instance", "modeled-realm", "/modeled/realm")
    discovery = owner.discovery(endpoint="http://127.0.0.1:1234", coordinator_epoch="modeled-epoch",
                                protocol_version="workspace.v1", schema_version="workspace-schema-v1",
                                capability_digest="modeled-digest", credential_file="/modeled/credentials/astrid.json",
                                advertised_at=1.0, worker_scopes=("modeled-scope",))
    marker_path = owner.leave_marker(tmp_path / "runtime")
    marker = json.loads(marker_path.read_text())
    assert set(marker) == {"pid", "process_birth_id", "runtime_instance_id", "realm_id", "realm_root"}
    assert all(marker[k] == discovery[k] for k in ("pid", "process_birth_id", "runtime_instance_id", "realm_root"))
    assert marker["realm_id"] == discovery["active_realm"]
    assert discovery["worker_scopes"] == ["modeled-scope"]
    assert "token" not in json.dumps(marker)
    # The on-disk marker persists independently of process lifetime; this
    # constructor does not acquire a kernel lock or start a process.
    assert marker_path.exists()


def test_optional_door_pair():
    owner = R0Owner(1, windows_birth_id(1), "instance", "realm", "C:/realm")
    kwargs = dict(endpoint="http://127.0.0.1:1", coordinator_epoch=None,
                  protocol_version="workspace.v1", schema_version="workspace-schema-v1",
                  capability_digest="digest", credential_file="C:/credential", advertised_at=0)
    assert "door_origin" not in owner.discovery(**kwargs)
    with pytest.raises(ValueError):
        owner.discovery(**kwargs, door_endpoint="http://127.0.0.1:2")
    value = owner.discovery(**kwargs, door_endpoint="http://127.0.0.1:2", door_origin="http://127.0.0.1:2")
    assert value["door_endpoint"] == value["door_origin"]
