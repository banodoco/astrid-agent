"""A job record written without a home is checked on its platform fields, like any other job.

Release 9 wrote accepted update records with no ``home`` key. Liveness must not read that
key: a record that is still running stays active, a positively finished one resolves, an
uncertain check changes nothing, and only work that reads the install selection refuses,
with the reason ``legacy-update-record``.
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from astrid.installer import update_job
from astrid.installer import update_job_platform as platform
from astrid.installer.core import write_json
from astrid.installer.service import LABEL

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="The fake manager answers systemctl queries")

OPERATION = "0123456789abcdef0123456789abcdef"
UNIT = f"astrid-update-{OPERATION}.service"


class FakeSystemd:
    """Answers the systemctl queries the Linux update-job path makes."""

    def __init__(self):
        self.active_state = "inactive"
        self.query_error = False

    def __call__(self, args):
        if args[:3] == ["systemctl", "--user", "show"] and args[3] == UNIT:
            if self.query_error:
                return subprocess.CompletedProcess(args, 1, "", "Failed to connect to user bus")
            return subprocess.CompletedProcess(args, 0, self.active_state + "\n", "")
        if args[:3] == ["systemctl", "--user", "reset-failed"]:
            return subprocess.CompletedProcess(args, 0, "", "")
        raise AssertionError(f"unexpected command: {args}")


@pytest.fixture
def systemd(tmp_path, monkeypatch):
    fake = FakeSystemd()
    monkeypatch.setattr(platform, "_run", fake)
    monkeypatch.setattr(update_job, "update_job_availability", lambda: {"available": True})
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return fake


def release_nine_record(tmp_path, state):
    """Write the shape Release 9 wrote: no home key on the record or on its descriptor."""
    root, data = tmp_path / "Astrid", tmp_path / "Workspace"
    (root / "logs").mkdir(parents=True)
    log = root / "logs" / f"update-{OPERATION}.log"
    base = {"format": update_job.JOB_FORMAT, "operation_id": OPERATION,
            "job_name": f"astrid-update-{OPERATION}", "program_root": str(root),
            "data_root": str(data), "log_path": str(log), "target_release": 8}
    # Written out by hand, so the fixture does not depend on the code under test.
    descriptor = {"format": "astrid-update-platform/v1", "system": "linux",
                  "kind": "systemd-user-transient", "name": UNIT, "unit": UNIT,
                  "user": str(os.getuid()), "operation_id": OPERATION, "program_root": str(root),
                  "log_path": str(log), "command": ["/opt/astrid/python", "-I", "-c", "pass"]}
    record = {**base, "state": state, "message": "Update accepted.", "platform": descriptor}
    assert "home" not in record and "home" not in descriptor
    write_json(root / "install.json", {"owner": LABEL, "data_root": str(data), "update_job": record})
    return root, record


def reconcile_request(root, tmp_path):
    return {"root": str(root), "data_root": str(tmp_path / "Workspace"),
            "selection_path": str(tmp_path / "home" / ".config" / "astrid" / "install.json"),
            "operation_id": OPERATION}


def test_describe_of_a_record_without_home_reproduces_its_stored_descriptor(tmp_path, monkeypatch):
    monkeypatch.setattr(platform, "_run", lambda args: subprocess.CompletedProcess(args, 0, "", ""))
    _root, record = release_nine_record(tmp_path, "active")
    base = {key: value for key, value in record.items() if key not in {"platform", "state", "message"}}
    assert platform.describe(base, ["/opt/astrid/python", "-I", "-c", "pass"], system="linux") == record["platform"]


def test_sweep_keeps_a_record_without_home_active_while_its_unit_runs(tmp_path, systemd):
    root, _record = release_nine_record(tmp_path, "active")
    systemd.active_state = "active"
    before = (root / "install.json").read_bytes()

    results = update_job.sweep_owned_jobs(root)

    assert [result["state"] for result in results] == ["active"]
    assert (root / "install.json").read_bytes() == before


def test_sweep_resolves_a_record_without_home_once_its_unit_has_finished(tmp_path, systemd):
    root, _record = release_nine_record(tmp_path, "active")
    systemd.active_state = "inactive"

    [result] = update_job.sweep_owned_jobs(root)

    assert result["state"] == "failed"
    assert result["cleanup_status"] == "clean"
    assert update_job.read_job(root)["cleanup_status"] == "clean"


def test_sweep_leaves_a_record_without_home_unchanged_when_the_check_is_uncertain(tmp_path, systemd):
    root, _record = release_nine_record(tmp_path, "active")
    systemd.query_error = True
    before = (root / "install.json").read_bytes()

    results = update_job.sweep_owned_jobs(root)

    assert [result["state"] for result in results] == ["active"]
    assert (root / "install.json").read_bytes() == before


@pytest.mark.parametrize("active_state, query_error", [("active", False), ("inactive", False), ("inactive", True)])
def test_status_reports_a_record_without_home_as_stored(tmp_path, systemd, active_state, query_error):
    root, record = release_nine_record(tmp_path, "active")
    systemd.active_state, systemd.query_error = active_state, query_error

    status = update_job.read_job(root)

    assert status["state"] == "active"
    assert status["platform"] == record["platform"]
    assert "home" not in status


def test_reconcile_treats_a_record_without_home_as_active_while_its_unit_runs(tmp_path, systemd):
    root, _record = release_nine_record(tmp_path, "active")
    systemd.active_state = "active"
    before = (root / "install.json").read_bytes()

    with pytest.raises(update_job._BusyRefusal, match="still active") as caught:
        update_job.reconcile(reconcile_request(root, tmp_path))

    assert caught.value.reason == "busy"
    assert (root / "install.json").read_bytes() == before


def test_reconcile_leaves_a_record_without_home_unchanged_when_liveness_is_unknown(tmp_path, systemd):
    root, _record = release_nine_record(tmp_path, "active")
    systemd.query_error = True
    before = (root / "install.json").read_bytes()

    with pytest.raises(update_job._BusyRefusal, match="liveness is unknown") as caught:
        update_job.reconcile(reconcile_request(root, tmp_path))

    assert caught.value.reason == "busy"
    assert (root / "install.json").read_bytes() == before


def test_reconcile_refuses_a_finished_record_without_home_at_the_selection_read(tmp_path, systemd):
    root, _record = release_nine_record(tmp_path, "active")
    systemd.active_state = "inactive"
    before = (root / "install.json").read_bytes()

    with pytest.raises(update_job._BusyRefusal, match="installation home") as caught:
        update_job.reconcile(reconcile_request(root, tmp_path))

    assert caught.value.reason == "legacy-update-record"
    assert (root / "install.json").read_bytes() == before


def test_reconcile_helper_reports_the_legacy_record_reason(tmp_path, systemd, capsys, monkeypatch):
    root, _record = release_nine_record(tmp_path, "active")
    systemd.active_state = "inactive"
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(
        {"action": "reconcile", **reconcile_request(root, tmp_path)})))

    with pytest.raises(SystemExit):
        update_job.main()

    response = json.loads(capsys.readouterr().out)
    assert response["reason"] == "legacy-update-record"
