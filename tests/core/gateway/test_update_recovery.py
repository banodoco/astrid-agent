"""Public doctor forwarding for bounded update recovery."""

from __future__ import annotations

import json
import os
from pathlib import Path

from astrid import omp_agent
from astrid.core.gateway import main as gateway_main
from astrid.installer import maintenance, update_job
from astrid.installer.core import Version, read_json, write_json
from astrid.installer.service import LABEL, install_selection


class _MaintenanceBackend:
    """Controlled readiness seam; real files still exercise copy preservation."""

    def __init__(self, *_args, **_kwargs) -> None:
        pass

    def maintenance_quiesce(self, _version, _data) -> None:
        return None

    def maintenance_schema(self, _version, data) -> tuple[dict, dict]:
        return {"realm_id": "maintenance-realm", "realm_root": str(data)}, {"version": 31, "target": 31}

    def maintenance_ready(self, version, _data, _realm) -> dict:
        return {"ready_instance_id": "maintenance-ready", "identity": version.document()["identity"]}


def _maintenance_fixture(tmp_path: Path, monkeypatch) -> tuple[Path, Path, Path, dict[str, bytes]]:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("XDG_DATA_HOME", str(home / ".local/share"))

    root, data = tmp_path / "program", tmp_path / "workspace"
    version_root = root / "versions" / "retained-7"
    python = version_root / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    python.parent.mkdir(parents=True)
    python.write_text("retained interpreter", encoding="utf-8")
    profile = version_root / "profile.json"
    write_json(profile, {"profile": "astrid"})
    version = Version(version_root, python, profile, {}, release=7)
    write_json(version_root / "version.json", version.document())
    write_json(version_root / "manifest.json", {"release": 7})
    (root / "current").write_text("retained-7\n", encoding="utf-8")

    (data / "projects").mkdir(parents=True)
    # This models data written after a failed switch.  It must be retained in
    # both the original and the selected working generation.
    (data / "projects" / "post-switch.json").write_text('{"name":"latest"}\n', encoding="utf-8")
    (data / "timelines").mkdir()
    (data / "timelines" / "history.json").write_text('{"history":[1,2]}\n', encoding="utf-8")
    (data / "media.bin").write_bytes(b"retained-media")

    operation_id = "a" * 32
    marker = {
        "owner": LABEL,
        "data_root": str(data),
        "installation_generation": 4,
        "update_job": {
            "state": "launching",
            "operation_id": operation_id,
            "route_disposition": {
                "format": update_job.ROUTE_DISPOSITION_FORMAT,
                "blocker": "held",
                "disposition": "acknowledgement-unknown",
                "confirmation": None,
            },
        },
    }
    write_json(root / "install.json", marker)
    journal = b'{"operation_id":"' + operation_id.encode() + b'","step":"stopping"}\n'
    (root / "update-transaction.json").write_bytes(journal)
    selection = install_selection(home, os.sys.platform)
    write_json(selection, {"owner": LABEL, "program_root": str(root), "data_root": str(data)})
    original = {
        "marker": (root / "install.json").read_bytes(),
        "journal": journal,
        "post_switch": (data / "projects" / "post-switch.json").read_bytes(),
        "history": (data / "timelines" / "history.json").read_bytes(),
    }
    return root, data, selection, original


def test_shipped_astrid_dispatches_update_recovery_help_without_omp(monkeypatch, capsys) -> None:
    monkeypatch.setattr(omp_agent, "_find_launcher", lambda: (_ for _ in ()).throw(AssertionError("OMP must not start")))

    assert omp_agent.main(["doctor", "update-recovery", "--help"]) == 0
    help_text = capsys.readouterr().out
    assert "usage: astrid doctor update-recovery" in help_text
    assert "--apply PLAN_SHA256" in help_text


def test_doctor_update_recovery_help_advertises_reviewed_apply(capsys) -> None:
    assert gateway_main(["doctor", "--help"]) == 0
    assert "astrid doctor update-recovery" in capsys.readouterr().out

    assert gateway_main(["doctor", "update-recovery", "--help"]) == 0
    help_text = capsys.readouterr().out
    assert "--apply PLAN_SHA256" in help_text
    assert "--root PROGRAM_ROOT" in help_text
    assert "--data-root DATA_ROOT" in help_text


def test_doctor_update_recovery_plans_by_default_and_applies_only_with_hash(
        tmp_path: Path, monkeypatch, capsys) -> None:
    root, data = tmp_path / "program", tmp_path / "workspace"
    calls: list[tuple[str, object]] = []
    plan = {"format": "astrid-update-maintenance-plan/v1", "plan_sha256": "b" * 64}

    monkeypatch.setattr(maintenance, "plan_update_recovery",
                        lambda got_root, got_data: calls.append(("plan", (got_root, got_data))) or plan)
    monkeypatch.setattr(maintenance, "apply_update_recovery",
                        lambda got_root, got_data, digest: calls.append(("apply", (got_root, got_data, digest)))
                        or {"state": "restored-working-installation"})

    command = ["doctor", "update-recovery", "--root", str(root), "--data-root", str(data)]
    assert gateway_main(command) == 0
    assert json.loads(capsys.readouterr().out) == plan
    assert calls == [("plan", (root, data))]

    assert gateway_main([*command, "--apply", "b" * 64]) == 0
    assert json.loads(capsys.readouterr().out) == {"state": "restored-working-installation"}
    assert calls[-1] == ("apply", (root, data, "b" * 64))


def test_doctor_update_recovery_lost_ack_is_plan_only_with_no_second_admission(
        tmp_path: Path, monkeypatch, capsys) -> None:
    root, data, _selection, _original = _maintenance_fixture(tmp_path, monkeypatch)
    observed: list[tuple[Path, Path]] = []
    plan = {"format": "astrid-update-maintenance-plan/v1", "plan_sha256": "c" * 64,
            "source_operation_id": "a" * 32}

    monkeypatch.setattr(maintenance, "plan_update_recovery",
                        lambda got_root, got_data: observed.append((got_root, got_data)) or plan)
    monkeypatch.setattr(update_job, "accept",
                        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("no second admission")))
    monkeypatch.setattr(update_job, "launch_job",
                        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("no native retry")))

    assert gateway_main(["doctor", "update-recovery", "--root", str(root), "--data-root", str(data)]) == 0
    assert json.loads(capsys.readouterr().out) == plan
    assert observed == [(root, data)]
    saved = read_json(root / "install.json")["update_job"]
    assert saved["operation_id"] == "a" * 32
    assert saved["route_disposition"]["disposition"] == "acknowledgement-unknown"
    assert saved["route_disposition"]["blocker"] == "held"


def test_doctor_update_recovery_preserves_post_switch_data_on_explicit_apply(
        tmp_path: Path, monkeypatch, capsys) -> None:
    root, data, selection, original = _maintenance_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(maintenance, "Backend", _MaintenanceBackend)
    command = ["doctor", "update-recovery", "--root", str(root), "--data-root", str(data)]

    assert gateway_main(command) == 0
    plan = json.loads(capsys.readouterr().out)
    assert gateway_main([*command, "--apply", plan["plan_sha256"]]) == 0
    result = json.loads(capsys.readouterr().out)

    working = Path(result["receipt"]["working_data_path"])
    archived = Path(result["receipt_path"]).parent / "raw-evidence"
    assert (data / "projects" / "post-switch.json").read_bytes() == original["post_switch"]
    assert (working / "projects" / "post-switch.json").read_bytes() == original["post_switch"]
    assert (working / "timelines" / "history.json").read_bytes() == original["history"]
    assert (root / "update-transaction.json").read_bytes() == original["journal"]
    assert read_json(root / "install.json")["update_job"] == json.loads(original["marker"])["update_job"]
    assert (archived / "install").read_bytes() == original["marker"]
    assert (archived / "journal").read_bytes() == original["journal"]
    assert read_json(selection)["data_root"] == str(working)
