import io
import json
from types import SimpleNamespace
import urllib.request

import pytest

from scripts import journey_windows_r13_update as row


INSTANCE = "0289a77d5d004a84955c4c3c38215bd6"
SCHEMA = row.jumps.RELEASE13_IDENTITY["schema_digest"]
DISCOVERY = {
    "endpoint": "http://127.0.0.1:58770",
    "pid": 1284,
    "runtime_instance_id": INSTANCE,
    "identity": {"release": 13, "managed_by": None},
}
HEALTH = {
    "status": "ok",
    "runtime_instance_id": INSTANCE,
    "identity": {"release": 13, "managed_by": None},
    "schema_digest": SCHEMA,
}


def mock_health(monkeypatch, payload):
    requests = []

    def open_health(request, timeout):
        requests.append((request.full_url, request.get_header("Origin"), timeout))
        return io.BytesIO(json.dumps(payload).encode())

    monkeypatch.setattr(urllib.request, "build_opener", lambda *_: SimpleNamespace(open=open_health))
    return requests


def test_killed_recovery_reads_schema_from_the_live_r13_owner(monkeypatch, tmp_path):
    # Round 5 used this discovery shape: its identity and instance were right,
    # but the schema digest was present only in the owner's HTTP health.
    requests = mock_health(monkeypatch, HEALTH)
    version = {"root": str(tmp_path)}
    row.journey.write_json(tmp_path / "manifest.json", {
        "agent": {"sha": row.jumps.RELEASE13_IDENTITY["agent"]},
        "runtime": {"sha": row.jumps.RELEASE13_IDENTITY["runtime"]},
    })
    monkeypatch.setattr(row.jumps, "installed_release", lambda _: 13)
    monkeypatch.setattr(row.journey, "installation", lambda *_: (tmp_path / "version.json", tmp_path, version))
    monkeypatch.setattr(row, "kill_update_at_owned_checkpoint", lambda *_: {"killed": True})

    def install(_app, _candidate_path, _shell, evidence, _root, _data):
        if evidence.name == "public-retry":
            raise RuntimeError("Reached the second public update")
        return {
            "exit_code": 3, "command": "install --update", "stderr_tail": [], "invocation": {},
            "receipt_invocation_tied": True,
            "installer_receipt": {"exit_code": 3, "kind": "not-updated", "still": 13},
        }

    def verify(_mode, _version, _data, evidence):
        row.journey.write_json(evidence / "verify-install.json", {"details": {"origin.imports": {"runtime": "R13"}}})

    monkeypatch.setattr(row, "candidate_update_windows", install)
    monkeypatch.setattr(row.journey.windows, "powershell", lambda *_: None)
    monkeypatch.setattr(row.journey, "wait_ready", lambda *_args, **_kw: None)
    monkeypatch.setattr(row, "windows_live_runtime_owners", lambda _: [
        {"data_root": str(tmp_path), "pid": 1284, "runtime_instance_id": INSTANCE},
    ])
    monkeypatch.setattr(row.journey, "assert_direct_service_ownership", lambda *_: {"manager": "task-scheduler"})
    monkeypatch.setattr(row.journey, "healthy", lambda _: DISCOVERY)
    monkeypatch.setattr(row.journey, "verify", verify)
    monkeypatch.setattr(row.journey, "assert_content", lambda *_: None)
    monkeypatch.setattr(row.jumps, "live_workspace", lambda *_: {"reopened": True})
    result = {"timeline_seed": {"composition": {}}, "checks": {}, "steps": [], "data_marker": {}}

    with pytest.raises(row.WindowsR13StepError, match="Reached the second public update"):
        row.recover_killed_r13_update(
            SimpleNamespace(shell="powershell.exe"), result, tmp_path / "result.json",
            app=tmp_path, candidate_path=tmp_path / "candidate.json", root=tmp_path, data=tmp_path,
            evidence=tmp_path, original=version, marker=tmp_path / "marker", digest="digest",
            sentinel=None, birth="sentinel",
        )

    assert result["recovery"]["live_probe"]["health"] == HEALTH
    assert result["recovery"]["timeline_reopen"] == {"reopened": True}
    assert result["checks"]["health"] == "pass"
    assert requests == [("http://127.0.0.1:58770/v1/health", "https://astrid.haus", 10)]


@pytest.mark.parametrize("different", [
    {"identity": {"release": 14, "managed_by": None}},
    {"runtime_instance_id": "different-instance"},
    {"schema_digest": "sha256:" + "0" * 64},
])
def test_runtime_identity_probe_preserves_mismatch_evidence(monkeypatch, different):
    observed = {**HEALTH, **different}
    mock_health(monkeypatch, {**observed, "access_token": "must-not-be-recorded"})
    receipt = {}
    with pytest.raises(RuntimeError, match="Live Runtime identity"):
        row.runtime_identity_probe(DISCOVERY, {"release": 13, "schema_digest": SCHEMA}, receipt=receipt)

    assert receipt["live_probe"]["health"] == observed


def test_runtime_identity_probe_rejects_non_loopback_before_request(monkeypatch):
    requests = mock_health(monkeypatch, HEALTH)
    discovery = {**DISCOVERY, "endpoint": "http://127.0.0.1:58770@other.example:80"}
    with pytest.raises(RuntimeError, match="loopback health endpoint"):
        row.runtime_identity_probe(discovery, {"release": 13, "schema_digest": SCHEMA})

    assert not requests
