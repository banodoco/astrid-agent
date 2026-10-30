"""Offline filesystem/mount evidence; no provider, mount or GPU operations."""
import base64
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import os

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
    request = {"pod_id": "pod187", "volume_id": "vol187", "nonce": "a" * 32, "mount": str(mount),
               "model_cache_root": str(cache), "engine_root": str(local), "required_volume_bytes": 10,
               "required_local_bytes": 100}
    request["provider_mapping"] = {"schema":"runpod.provider-mounts.v1", "pod_id":request["pod_id"], "nonce":request["nonce"],
        "volume_mounts":[{"volume_id":"vol187", "volume_type":"NETWORK_VOLUME", "mount_path":str(mount), "read_only":False}],
        "legacy_evidence":{"present":False,"valid":True,"conflict":False}}
    # Faked device separation and capacities: actual tests never mount anything.
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
    line = f"40 1 {device} / {mount} rw - fuse.test server:/vol187 rw\n"
    return request, line


def test_verified_source_and_capacity(storage):
    request, line = storage
    observed = []
    receipt = probe.storage_probe(request, mountinfo=line, diagnostic=observed.append)
    assert observed == [{"schema": probe.DIAGNOSTIC_SCHEMA, "stage": "complete", "reason": "verified", "identity_matches": True}]
    assert receipt["volume_id"] == "vol187" and receipt["free_bytes"] == 1000
    assert receipt["local_free_bytes"] == 1000
    assert "mount_evidence" not in receipt and "server:/vol187" not in json.dumps([receipt, observed])


@pytest.mark.parametrize("case,stage,reason,identity", [
    ("missing", "mount", "absent_or_ambiguous", True),
    ("wrong", "identity", "mismatch", False),
    ("opaque", "identity", "mismatch", False),
    ("ambiguous", "mount", "absent_or_ambiguous", True),
    ("device", "mount", "device_mismatch", True),
    ("capacity", "capacity", "insufficient", True),
    ("local-capacity", "local", "insufficient", True),
    ("invalid-local", "local", "invalid_requirement", True),
    ("malformed", "mountinfo", "malformed", None),
    ("nested", "cache", "nested_mount", True),
    ("escape", "paths", "invalid", True),
    ("symlink", "paths", "symlink", True),
    ("bad-id", "identity", "mismatch", False),
    ("invalid-capacity", "capacity", "invalid_requirement", True),
    ("same-filesystem", "local", "same_filesystem", True),
])
def test_mount_identity_capacity_and_path_fail_closed(storage, case, stage, reason, identity):
    request, line = storage
    line = line.replace("server:/vol187", "server:/vol187/PRIVATE_SOURCE").replace(" rw", " rw,PRIVATE_OPTION")
    if case == "missing": line = ""
    if case == "wrong": request["provider_mapping"]["volume_mounts"][0]["volume_id"] = "other"
    if case == "opaque": request["provider_mapping"]["volume_mounts"][0]["volume_type"] = "VOLUME"
    if case == "ambiguous": line *= 2
    if case == "device": line = line.replace(line.split()[2], "999:999")
    if case == "capacity": request["required_volume_bytes"] = 1001
    if case == "local-capacity": request["required_local_bytes"] = 1001
    if case == "invalid-local": request["required_local_bytes"] = -1
    if case == "malformed": line = line.split(" - ")[0] + " - fuse.mfs\n"
    if case == "nested": line += line.replace(request["mount"], request["model_cache_root"])
    if case == "escape": request["model_cache_root"] = request["mount"] + "/../escape"
    if case == "symlink":
        cache = Path(request["model_cache_root"])
        cache.rmdir()
        cache.symlink_to(cache.parent, target_is_directory=True)
    if case == "bad-id": request["volume_id"] = None
    if case == "invalid-capacity": request["required_volume_bytes"] = True
    if case == "same-filesystem":
        request["engine_root"] = str(Path(request["mount"]).parent / "engine" / "nested")
        Path(request["mount"]).parent.joinpath("engine").mkdir()
    observed = []
    with pytest.raises(ValueError):
        probe.storage_probe(request, mountinfo=line, diagnostic=observed.append)
    assert observed == [{"schema": probe.DIAGNOSTIC_SCHEMA, "stage": stage,
                         "reason": reason, "identity_matches": identity}]
    assert not any(part in json.dumps(observed) for part in ("server:/", "opaque", "PRIVATE_"))


def test_missing_cache_does_not_create_it(storage):
    request, line = storage
    cache = Path(request["model_cache_root"])
    cache.rmdir()
    probe.storage_probe(request, mountinfo=line)
    assert not cache.exists()


@pytest.mark.parametrize("case", ["success", "absent", "duplicate", "device", "opaque", "mismatch", "malformed"])
def test_exact_mount_diagnostic_is_retained_before_success_or_refusal(storage, case):
    request, line = storage
    # Representative Linux mountinfo syntax; these are synthetic observations,
    # not captured provider mount identities.
    line = line.replace(" rw - ", " rw,relatime shared:30 - ")
    if case == "absent": line = ""
    if case == "duplicate": line *= 3
    if case == "device": line = line.replace(line.split()[2], "999:999")
    if case == "opaque": line = line.replace("server:/vol187", "10.23.4.5:/exports/opaque")
    if case == "mismatch": line = line.replace("server:/vol187", "server:/vol187-other")
    if case == "malformed": line = line.split(" - ")[0] + " - fuse.test\n"
    line += "91 1 0:92 / /unrelated rw - fuse.private UNRELATED_SOURCE rw,PRIVATE_OPTION\n"
    observed = []
    if case in ("success", "opaque", "mismatch"):
        probe.storage_probe(request, mountinfo=line, diagnostic=observed.append)
    else:
        with pytest.raises(ValueError):
            probe.storage_probe(request, mountinfo=line, diagnostic=observed.append)
    assert len(observed) == 1
    stage, reason, identity = {
        "success": ("complete", "verified", True),
        "absent": ("mount", "absent_or_ambiguous", True),
        "duplicate": ("mount", "absent_or_ambiguous", True),
        "device": ("mount", "device_mismatch", True),
        "opaque": ("complete", "verified", True),
        "mismatch": ("complete", "verified", True),
        "malformed": ("mountinfo", "malformed", None),
    }[case]
    assert observed == [{"schema": probe.DIAGNOSTIC_SCHEMA, "stage": stage,
                         "reason": reason, "identity_matches": identity}]
    assert not any(part in json.dumps(observed) for part in
                   ("UNRELATED", "PRIVATE_OPTION", "server:/", "10.23.4.5", "fuse.", "mountpoint"))


def test_long_escaped_source_is_compared_raw_and_never_emitted(storage):
    request, line = storage
    source = "https://USER:PRIVATE_PASSWORD@host/vol187?token=PRIVATE_QUERY#PRIVATE_FRAGMENT"
    # Typed mapping is authoritative even when the live source contains opaque
    # or private components. Those components never leave the probe.
    for source in (source, "server:/vol187/" + "x" * 2000 + r"\012\033" + "tail", r"opaque\012\033"):
        observed = []
        receipt = probe.storage_probe(request, mountinfo=line.replace("server:/vol187", source), diagnostic=observed.append)
        assert observed[0]["identity_matches"] is True
        text = json.dumps([receipt, observed])
        assert all(part not in text for part in ("PRIVATE_", "USER", "https://", "server:/", "opaque", "tail"))



@pytest.fixture
def private_storage(storage):
    request, line = storage
    return request, line.replace("server:/vol187", "mfs#vol187/PRIVATE_SOURCE").replace(" rw", " rw,PRIVATE_OPTION")


def test_unexpected_exception_diagnostic_is_bounded(private_storage, monkeypatch):
    request, line = private_storage
    monkeypatch.setattr(probe.os, "statvfs", lambda _: (_ for _ in ()).throw(OSError("PRIVATE_ERROR")))
    observed = []
    with pytest.raises(OSError):
        probe.storage_probe(request, mountinfo=line, diagnostic=observed.append)
    assert observed == [{"schema": probe.DIAGNOSTIC_SCHEMA, "stage": "capacity", "reason": "error", "identity_matches": True}]


@pytest.mark.parametrize("match", [True, False])
def test_transported_main_never_serializes_source_or_options(private_storage, monkeypatch, capsys, match):
    request, line = private_storage
    if not match:
        request["provider_mapping"]["volume_mounts"][0]["volume_id"] = "wrong"
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


@pytest.mark.parametrize("case", ["missing", "schema", "stale", "pod", "duplicate", "wrong-volume", "wrong-type", "wrong-path", "readonly", "bool", "legacy", "unknown", "escape", "malformed"])
def test_typed_mapping_refuses_missing_ambiguous_stale_and_conflicting(storage, case):
    request, line = storage
    provider = request["provider_mapping"]
    item = provider["volume_mounts"][0]
    if case == "missing": request.pop("provider_mapping")
    if case == "schema": provider["schema"] = "wrong"
    if case == "stale": provider["nonce"] = "b" * 32
    if case == "pod": provider["pod_id"] = "other"
    if case == "duplicate": provider["volume_mounts"] *= 2
    if case == "wrong-volume": item["volume_id"] = "wrong"
    if case == "wrong-type": item["volume_type"] = "VOLUME"
    if case == "wrong-path": item["mount_path"] = "/wrong"
    if case == "readonly": item["read_only"] = True
    if case == "bool": item["read_only"] = 0
    if case == "legacy": provider["legacy_evidence"]["conflict"] = True
    if case == "unknown": provider["private"] = "PRIVATE"
    if case == "escape": item["mount_path"] += "/../escape"
    if case == "malformed": provider["volume_mounts"] = ["PRIVATE"]
    observed = []
    with pytest.raises(ValueError, match="typed provider|unexpected fields"):
        probe.storage_probe(request, mountinfo=line, diagnostic=observed.append)
    assert observed == [{"schema":probe.DIAGNOSTIC_SCHEMA,"stage":"unknown" if case == "missing" else "identity",
                         "reason":"error" if case == "missing" else "mismatch","identity_matches":None if case == "missing" else False}]


def test_live_readonly_mount_refuses_even_with_typed_writable_policy(storage):
    request, line = storage
    with pytest.raises(ValueError, match="writable"):
        probe.storage_probe(request, mountinfo=line.replace(" rw - ", " ro - "))


@pytest.mark.parametrize("raw", ['{"pod_id":"safe","pod_id":"PRIVATE"}', '{"nested":{"id":1,"id":2}}'])
def test_transported_main_rejects_duplicate_json_before_echo(raw, monkeypatch, capsys):
    monkeypatch.setattr(probe.sys, "argv", ["probe", base64.b64encode(raw.encode()).decode()])
    assert probe.main() == 1
    captured = capsys.readouterr()
    assert captured.out == "" and "PRIVATE" not in captured.err


def test_local_capacity_requirement_cannot_be_boolean(storage):
    request, line = storage
    request["required_local_bytes"] = True
    with pytest.raises(ValueError, match="local capacity"):
        probe.storage_probe(request, mountinfo=line)


def test_unexpected_request_fields_are_never_echoed(storage):
    request, line = storage
    request["PRIVATE_FIELD"] = "PRIVATE_VALUE"
    observed = []
    with pytest.raises(ValueError, match="unexpected fields"):
        probe.storage_probe(request, mountinfo=line, diagnostic=observed.append)
    assert "PRIVATE" not in json.dumps(observed)


def test_source_redaction_cannot_change_identity_decision(storage):
    request, line = storage
    source = "https://USER:PRIVATE_PASSWORD@host/vol187?token=PRIVATE_QUERY#PRIVATE_FRAGMENT"
    request["provider_mapping"]["volume_mounts"][0]["volume_id"] = "other"
    observed = []
    with pytest.raises(ValueError, match="identity|typed provider"):
        probe.storage_probe(request, mountinfo=line.replace("server:/vol187", source), diagnostic=observed.append)
    assert observed[0]["identity_matches"] is False
    assert all(part not in json.dumps(observed) for part in ("USER", "PRIVATE", "https://"))
