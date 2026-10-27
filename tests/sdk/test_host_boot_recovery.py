from __future__ import annotations

import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.core.execution import boot_identity as boot
from astrid.sdk import host_bootstrap as host

BEFORE = {"version": 1, "source": boot.WINDOWS_BOOT_SOURCE,
          "value": {"machine_guid": "e954823c-0fac-4bc0-9f84-31e1514078fd", "boot_id": 200}}
AFTER = {**BEFORE, "value": {**BEFORE["value"], "boot_id": 201}}


@pytest.fixture
def recovery(tmp_path, monkeypatch):
    support = tmp_path / "support"
    support.mkdir(mode=0o700)
    state = {"pid": 4321, "process_birth_id": "windows-creation:1234", "support_root": str(support)}
    host._write_object(support / "generic-host.json", state)
    host._write_object(support / "generic-host.ready.json", {**state, "status": "ready"})
    monkeypatch.setattr(host, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(host, "_registered_host_status", lambda _state: "gone")
    monkeypatch.setattr(boot, "capture_boot_identity", lambda: dict(BEFORE))
    proof = host.capture_legacy_host_recovery(support)
    monkeypatch.setattr(boot, "capture_boot_identity", lambda: dict(AFTER))
    return support, state, proof, tmp_path / "quarantine"


def retire(recovery, **kwargs):
    support, _state, proof, quarantine = recovery
    return host.retire_preboot_host(support, proof, prepared_boot=kwargs.get("prepared_boot", BEFORE),
                                   current_boot=kwargs.get("current_boot", AFTER), quarantine=quarantine)


def test_exact_preboot_legacy_markers_quarantined_without_receipt_or_pid_probe(recovery, monkeypatch):
    support, _state, _proof, quarantine = recovery
    def forbidden(*_args, **_kwargs):
        pytest.fail("reboot proof must not signal, census, or fabricate Runtime receipts")
    monkeypatch.setattr(host, "_registered_host_status", forbidden)  # includes reused/foreign numeric PID
    monkeypatch.setattr(host, "terminate_group", forbidden)
    monkeypatch.setattr(host, "_consume_runtime_cleanup_receipt", forbidden)
    assert retire(recovery)["status"] == "retired"
    assert not (support / "generic-host.json").exists()
    assert not (support / "pack-host-job-cleanup.json").exists()
    assert (quarantine / "generic-host.json").is_file()
    assert retire(recovery)["status"] == "already_retired"


@pytest.mark.parametrize("fault", ["sameboot", "missing", "invalid", "forged-current", "wrong-preparation"])
def test_recovery_requires_fresh_valid_different_boot(recovery, monkeypatch, fault):
    support, _state, proof, quarantine = recovery
    kwargs = {}
    if fault == "sameboot":
        monkeypatch.setattr(boot, "capture_boot_identity", lambda: dict(BEFORE))
        kwargs["current_boot"] = BEFORE
    elif fault == "missing":
        proof.pop("boot_identity")
    elif fault == "invalid":
        proof["boot_identity"] = {}
    elif fault == "forged-current":
        kwargs["current_boot"] = {**AFTER, "value": {**AFTER["value"], "boot_id": 202}}
    else:
        kwargs["prepared_boot"] = AFTER
    with pytest.raises((host.PackHostBootstrapError, boot.BootIdentityUnavailable)):
        retire(recovery, **kwargs)
    assert (support / "generic-host.json").exists()
    assert not quarantine.exists()


@pytest.mark.parametrize("marker", ["generic-host.json", "generic-host.ready.json"])
def test_new_live_registration_or_changed_marker_never_retired(recovery, marker):
    support, state, _proof, _quarantine = recovery
    host._write_object(support / marker, {**state, "pid": 9876, "process_birth_id": "new-live-process"})
    with pytest.raises(host.PackHostBootstrapError, match="changed after"):
        retire(recovery)
    assert (support / marker).exists()
    assert (support / "generic-host.json").exists()


@pytest.mark.parametrize("status", ["ours", "reused", "foreign"])
def test_preparation_rejects_live_foreign_or_reused_pid(recovery, monkeypatch, status):
    support, _state, _proof, _quarantine = recovery
    monkeypatch.setattr(boot, "capture_boot_identity", lambda: dict(BEFORE))
    monkeypatch.setattr(host, "_registered_host_status", lambda _state: status)
    with pytest.raises(host.PackHostBootstrapError, match="preparation blocked"):
        host.capture_legacy_host_recovery(support)


def test_preparation_rejects_unknown_or_access_denied_process(recovery, monkeypatch):
    support, _state, _proof, _quarantine = recovery
    monkeypatch.setattr(boot, "capture_boot_identity", lambda: dict(BEFORE))
    def denied(_state):
        raise host.PackHostBootstrapError("native process identity unavailable")
    monkeypatch.setattr(host, "_registered_host_status", denied)
    with pytest.raises(host.PackHostBootstrapError, match="unavailable"):
        host.capture_legacy_host_recovery(support)


def test_partial_retirement_replays_only_with_exact_quarantined_bytes(recovery, monkeypatch):
    support, _state, _proof, quarantine = recovery
    original = Path.unlink
    def interrupted(path, *args, **kwargs):
        if path == support / "generic-host.ready.json":
            raise OSError("interrupted after first marker")
        return original(path, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", interrupted)
        with pytest.raises(OSError):
            retire(recovery)
    assert not (support / "generic-host.json").exists()
    assert retire(recovery)["status"] == "retired"
    host._write_object(support / "generic-host.json", {"pid": 12345})
    with pytest.raises(host.PackHostBootstrapError, match="changed after"):
        retire(recovery)


def test_missing_preboot_marker_without_quarantine_is_not_replay(recovery):
    support, _state, _proof, _quarantine = recovery
    (support / "generic-host.json").unlink()
    with pytest.raises(host.PackHostBootstrapError, match="changed after"):
        retire(recovery)


def test_digest_tampering_and_foreign_support_are_rejected(recovery):
    _support, _state, proof, _quarantine = recovery
    proof["markers"]["generic-host.json"]["sha256"] = "sha256:" + "0" * 64
    with pytest.raises(host.PackHostBootstrapError, match="digest"):
        retire(recovery)


def test_foreign_support_binding_is_rejected(recovery):
    support, _state, proof, _quarantine = recovery
    proof["support_root"] = str(support.parent / "other-runtime")
    with pytest.raises(host.PackHostBootstrapError, match="binding"):
        retire(recovery)


@pytest.mark.parametrize("fault", ["symlink", "permissions", "quarantine-overlap"])
def test_recovery_rejects_unsafe_files_and_paths(recovery, monkeypatch, fault):
    support, _state, _proof, quarantine = recovery
    if fault == "symlink":
        marker = support / "generic-host.ready.json"
        original = Path.lstat
        monkeypatch.setattr(Path, "lstat", lambda path: SimpleNamespace(st_mode=stat.S_IFLNK)
                            if path == marker else original(path))
    elif fault == "permissions":
        original = host.platform.is_owner_only
        marker = support / "generic-host.json"
        monkeypatch.setattr(host.platform, "is_owner_only", lambda path, **kwargs: False if path == marker else original(path, **kwargs))
    else:
        support, state, proof, _quarantine = recovery
        recovery = support, state, proof, support
    with pytest.raises(host.PackHostBootstrapError):
        retire(recovery)
    assert (support / "generic-host.json").exists()


def test_recorded_boot_metadata_alone_does_not_weaken_normal_retirement(recovery):
    support, state, _proof, _quarantine = recovery
    state["boot_identity"] = BEFORE
    host._write_object(support / "generic-host.json", state)
    with pytest.raises(host.PackHostBootstrapError, match="no retained Job custody"):
        host._retire_host_locked(state)
    assert (support / "generic-host.json").exists()


def test_optional_boot_capture_failure_does_not_block_host_startup(monkeypatch):
    monkeypatch.setattr(host, "sys", SimpleNamespace(platform="win32"))
    def unavailable():
        raise boot.BootIdentityUnavailable("unsupported native ABI")
    monkeypatch.setattr(boot, "capture_boot_identity", unavailable)
    state = {"pid": 10}
    host._record_optional_host_boot_identity(state)
    assert state == {"pid": 10}


def test_preflight_routes_legacy_record_to_prepare_before_staging(recovery):
    support, _state, _proof, _quarantine = recovery
    with pytest.raises(host.PackHostBootstrapError) as failure:
        host.preflight_legacy_host_recovery(support)
    assert failure.value.code == "legacy_windows_recovery_required"


@pytest.mark.windows_native
def test_native_same_boot_proof_cannot_retire_exact_legacy_markers(tmp_path):
    """Exercise actual kernel/registry proof and private files; no simulated reboot."""
    import subprocess
    import sys

    support = tmp_path / "native-support"
    host.platform.private_directory(support)
    process = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.buffer.read()"],
                               stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        identity = host.platform.process_identity(process.pid)
        assert identity is not None
        process.communicate(timeout=10)
        assert process.returncode == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
    state = {"pid": process.pid, "process_birth_id": identity["birth_id"], "support_root": str(support)}
    host._write_object(support / "generic-host.json", state)
    host._write_object(support / "generic-host.ready.json", {**state, "status": "ready"})
    before = {name: (support / name).read_bytes() for name in host._RECOVERY_MARKERS}
    proof = host.capture_legacy_host_recovery(support)
    assert proof is not None
    current = boot.capture_boot_identity()
    assert current == proof["boot_identity"]
    quarantine = tmp_path / "native-quarantine"
    with pytest.raises(host.PackHostBootstrapError, match="different current OS boot is unproved"):
        host.retire_preboot_host(support, proof, prepared_boot=proof["boot_identity"],
                                current_boot=current, quarantine=quarantine)
    assert {name: (support / name).read_bytes() for name in host._RECOVERY_MARKERS} == before
    assert not quarantine.exists()
    assert not (support / "pack-host-job-cleanup.json").exists()
