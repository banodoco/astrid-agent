"""Offline filesystem/mount evidence; no provider, mount or GPU operations."""
import base64
import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location("pod_storage_probe", Path(__file__).parents[2] / "scripts/pod_storage_probe.py")
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


@pytest.fixture
def storage(tmp_path, monkeypatch):
    mount = tmp_path / "workspace"
    mount.mkdir()
    cache = mount / "astrid/models"
    cache.mkdir(parents=True)
    local = tmp_path / "engine"
    request = {"pod_id": "pod187", "volume_id": "vol187", "nonce": "once", "mount": str(mount),
               "model_cache_root": str(cache), "engine_root": str(local), "required_volume_bytes": 10,
               "required_local_bytes": 100}
    original_stat = Path.stat
    def fake_stat(path, *args, **kwargs):
        result = original_stat(path, *args, **kwargs)
        if path == tmp_path:
            values = list(result)
            values[2] += 1
            return os.stat_result(values)
        return result
    monkeypatch.setattr(Path, "stat", fake_stat)
    monkeypatch.setattr(probe.os, "statvfs", lambda path: SimpleNamespace(f_bavail=1000, f_frsize=1, f_blocks=2000))
    device = f"{os.major(mount.lstat().st_dev)}:{os.minor(mount.lstat().st_dev)}"
    # Captured-shape FUSE only: every identifier and option here is synthetic.
    line = f"102 93 {device} / {mount} rw,PRIVATE_MOUNT_OPTION shared:30 - fuse.mfs mfs#vol187/PRIVATE_SOURCE rw,PRIVATE_SUPER_OPTION\n"
    return request, line


def test_verified_source_and_capacity(storage):
    request, line = storage
    observed = []
    receipt = probe.storage_probe(request, mountinfo=line, diagnostic=observed.append)
    assert receipt["volume_id"] == "vol187" and receipt["free_bytes"] == 1000
    assert receipt["local_free_bytes"] == 1000
    assert observed == [{"schema": probe.DIAGNOSTIC_SCHEMA, "stage": "complete", "reason": "verified", "identity_matches": True}]
    assert "mfs#" not in json.dumps([receipt, observed])
    assert "PRIVATE_" not in json.dumps([receipt, observed])


@pytest.mark.parametrize("case,stage,reason,identity", [
    ("missing", "mount", "absent_or_ambiguous", None),
    ("ambiguous", "mount", "absent_or_ambiguous", None),
    ("malformed", "mountinfo", "malformed", None),
    ("wrong", "identity", "mismatch", False),
    ("opaque", "identity", "mismatch", False),
    ("bad-id", "identity", "invalid_request", None),
    ("device", "mount", "device_mismatch", True),
    ("capacity", "capacity", "insufficient", True),
    ("invalid-capacity", "capacity", "invalid_requirement", True),
    ("local-capacity", "local", "insufficient", True),
    ("invalid-local", "local", "invalid_requirement", True),
    ("same-filesystem", "local", "same_filesystem", True),
    ("nested", "cache", "nested_mount", True),
    ("escape", "paths", "invalid", True),
    ("symlink", "paths", "symlink", True),
])
def test_mount_identity_capacity_and_path_fail_closed(storage, case, stage, reason, identity):
    request, line = storage
    if case == "missing": line = ""
    if case == "wrong": line = line.replace("#vol187/", "#vol187-other/")
    if case == "bad-id": request["volume_id"] = None
    if case == "opaque": line = line.replace("#vol187/", "#opaque/")
    if case == "ambiguous": line *= 2
    if case == "malformed": line = line.split(" - ")[0] + " - fuse.mfs\n"
    if case == "device": line = line.replace(line.split()[2], "999:999")
    if case == "capacity": request["required_volume_bytes"] = 1001
    if case == "invalid-capacity": request["required_volume_bytes"] = True
    if case == "local-capacity": request["required_local_bytes"] = 1001
    if case == "invalid-local": request["required_local_bytes"] = -1
    if case == "same-filesystem":
        request["engine_root"] = str(Path(request["mount"]).parent / "engine" / "nested")
        Path(request["mount"]).parent.joinpath("engine").mkdir()
    if case == "nested": line += line.replace(request["mount"], request["model_cache_root"])
    if case == "escape": request["model_cache_root"] = request["mount"] + "/../escape"
    if case == "symlink":
        cache = Path(request["model_cache_root"])
        cache.rmdir()
        cache.symlink_to(cache.parent, target_is_directory=True)
    observed = []
    with pytest.raises(ValueError):
        probe.storage_probe(request, mountinfo=line, diagnostic=observed.append)
    assert observed == [{"schema": probe.DIAGNOSTIC_SCHEMA, "stage": stage, "reason": reason, "identity_matches": identity}]
    assert "PRIVATE_" not in json.dumps(observed) and "mfs#" not in json.dumps(observed)


def test_missing_cache_does_not_create_it(storage):
    request, line = storage
    cache = Path(request["model_cache_root"])
    cache.rmdir()
    probe.storage_probe(request, mountinfo=line)
    assert not cache.exists()


def test_unexpected_exception_diagnostic_is_bounded(storage, monkeypatch):
    request, line = storage
    monkeypatch.setattr(probe.os, "statvfs", lambda _: (_ for _ in ()).throw(OSError("PRIVATE_ERROR")))
    observed = []
    with pytest.raises(OSError):
        probe.storage_probe(request, mountinfo=line, diagnostic=observed.append)
    assert observed == [{"schema": probe.DIAGNOSTIC_SCHEMA, "stage": "capacity", "reason": "error", "identity_matches": True}]


@pytest.mark.parametrize("match", [True, False])
def test_transported_main_never_serializes_source_or_options(storage, monkeypatch, capsys, match):
    request, line = storage
    if not match:
        line = line.replace("#vol187/", "#opaque/")
    original_read = Path.read_text
    monkeypatch.setattr(Path, "read_text", lambda path, *a, **kw: line if str(path) == "/proc/self/mountinfo" else original_read(path, *a, **kw))
    monkeypatch.setattr(probe.sys, "argv", ["probe", base64.b64encode(json.dumps(request).encode()).decode()])
    assert probe.main() == (0 if match else 1)
    captured = capsys.readouterr()
    assert "mfs#" not in captured.out + captured.err
    assert "PRIVATE_" not in captured.out + captured.err
    records = [line.removeprefix(probe.DIAGNOSTIC_PREFIX) for line in captured.err.splitlines() if line.startswith(probe.DIAGNOSTIC_PREFIX)]
    assert len(records) == 1 and json.loads(records[0])["identity_matches"] is match


def test_malformed_request_reports_unknown_without_leaking(monkeypatch, capsys):
    monkeypatch.setattr(probe.sys, "argv", ["probe", "PRIVATE_BAD_INPUT"])
    assert probe.main() == 1
    captured = capsys.readouterr()
    assert "PRIVATE" not in captured.out + captured.err
    record = captured.err.splitlines()[0].removeprefix(probe.DIAGNOSTIC_PREFIX)
    assert json.loads(record) == {"schema": probe.DIAGNOSTIC_SCHEMA, "stage": "unknown", "reason": "error", "identity_matches": None}


def test_source_redaction_cannot_change_identity_decision(storage):
    request, line = storage
    source = "https://USER:PRIVATE_PASSWORD@host/vol187?token=PRIVATE_QUERY#PRIVATE_FRAGMENT"
    observed = []
    with pytest.raises(ValueError, match="identity"):
        probe.storage_probe(request, mountinfo=line.replace("mfs#vol187/PRIVATE_SOURCE", source), diagnostic=observed.append)
    assert observed[0]["identity_matches"] is False
    assert all(part not in json.dumps(observed) for part in ("USER", "PRIVATE", "https://"))


def test_long_escaped_source_is_compared_raw_and_never_emitted(storage):
    request, line = storage
    source = "mfs#vol187/PRIVATE_SOURCE" + "x" * 2000 + r"\012\033" + "tail"
    observed = []
    receipt = probe.storage_probe(request, mountinfo=line.replace("mfs#vol187/PRIVATE_SOURCE", source), diagnostic=observed.append)
    assert observed[0]["identity_matches"] is True
    assert "PRIVATE" not in json.dumps([receipt, observed])
    assert "mfs#" not in json.dumps([receipt, observed])
