#!/usr/bin/env python3
"""Redacted diagnostic against a disposable non-root systemd user manager."""
import hashlib
import json
import os
import subprocess
import time
from pathlib import Path

from astrid.installer import service
from astrid.installer.core import Version, write_json

base = Path(os.environ["PROOF_ROOT"])
home = Path.home()
result = {"schema": "systemd-typed-observation-v1", "cases": {}, "wire": [],
          "controls": {}, "uid": os.getuid(), "euid": os.geteuid(),
          "start_ns": time.time_ns(), "authority": False}

def digest(raw):
    return hashlib.sha256(raw).hexdigest()

def command(args):
    p = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=25)
    return p.returncode, p.stdout, p.stderr

def systemctl(*args):
    rc, _, _ = command(["systemctl", "--user", *args])
    if rc:
        raise RuntimeError("fixture manager command failed")

def do_case(name, fn):
    try:
        detail = fn()
        result["cases"][name] = {"exit": 0, "detail": detail}
    except Exception as exc:
        # Exceptions, command lines and property values may carry secrets.
        result["cases"][name] = {"exit": 1, "error_type": type(exc).__name__}

def expect_refusal(fn, types=(ValueError, RuntimeError)):
    try:
        fn()
    except types as exc:
        return type(exc).__name__
    raise AssertionError("expected refusal")

def no_mutation(*_args, **_kwargs):
    raise AssertionError("unexpected service operation")

manager = service.Service(base / "program", base / "log", no_mutation, home=home, system="linux")
data = base / "data"
version_root = manager.root / "versions" / "r0"
version = Version(version_root, version_root / "py/bin/python", version_root / "profile.json", {})
write_json(manager.selection, {"owner": service.LABEL, "program_root": str(manager.root), "data_root": str(data)})
version_root.mkdir(parents=True, exist_ok=True)
write_json(version_root / "version.json", version.document())
(manager.root / "current").write_text("r0\n")
manager.path.parent.mkdir(parents=True, exist_ok=True)
dropdir = manager.path.parent / "astrid.service.d"
dropdir.mkdir()
dropin = dropdir / "proof.conf"
env1, env2 = base / "fixture-a.env", base / "fixture-b.env"
# The optional files are deliberately absent until the creation control.
unit = "[Unit]\nDescription=Disposable diagnostic fixture\n[Service]\nType=simple\nExecStart=/bin/sleep 300\n"
manager.path.write_text(unit)
original_bus = service._bounded_busctl
original_typed = service._typed_environment_files

def observed_bus(args):
    kind = "get_unit" if args[0] == "call" else "get_property"
    try:
        raw = original_bus(args)
    except Exception as exc:
        result["wire"].append({"operation": kind, "exit": 1, "error_type": type(exc).__name__})
        raise
    row = {"operation": kind, "exit": 0, "sha256": digest(raw), "bytes": len(raw)}
    try:
        obj = json.loads(raw)
        row["signature"] = obj.get("type")
        if obj.get("type") == "o":
            row["canonical_object"] = obj.get("data") == ["/org/freedesktop/systemd1/unit/astrid_2eservice"]
        elif obj.get("type") == "a(sb)" and isinstance(obj.get("data"), list):
            row["count"] = len(obj["data"])
    except (ValueError, AttributeError):
        row["decoding"] = "invalid"
    result["wire"].append(row)
    return raw
service._bounded_busctl = observed_bus

def fixture(lines):
    dropin.write_text("[Service]\n" + "".join("EnvironmentFile=" + line + "\n" for line in lines))
    systemctl("daemon-reload")
    # Make GetUnit load an inactive fixture.
    systemctl("show", "astrid.service", "--property=LoadState")

def observe(expected, stopped=False):
    typed = original_typed()
    assert typed == expected
    obs = manager._observe_systemd_transfer_definition(data)
    assert obs.environment_files == expected
    if stopped:
        assert manager._observe_stopped_systemd_transfer_definition(data).environment_files == expected
    refusal = expect_refusal(manager._prepare_lifecycle_transfer, (RuntimeError,))
    snap = manager._snapshot_linux_r0(version, data, known_children=())
    predicates = {k: v.result for k, v in snap.predicates.items()}
    assert predicates["environment_files"] == ("present" if expected else "absent")
    assert predicates["effective_environment_files"] == predicates["manager_identity"] == "unknown"
    assert snap.authority is False
    return {"typed_count": len(typed), "ignore_errors": [flag for _, flag in typed],
            "observation": "stable", "stopped_observation": stopped, "refusal": refusal,
            "environment_files": predicates["environment_files"],
            "effective_environment_files": predicates["effective_environment_files"],
            "manager_identity": predicates["manager_identity"], "authority": False}

def empty():
    fixture([])
    return observe((), True)

def single():
    fixture(["-" + str(env1)])
    return observe(((str(env1), True),), True)

def multiple():
    fixture(["-" + str(env1), "-" + str(env1), "-" + str(env2)])
    return observe(((str(env1), True), (str(env1), True), (str(env2), True)), True)

def glob_case():
    pattern = str(base / "fixture-*.env")
    fixture(["-" + pattern])
    return observe(((pattern, True),), True)

def created():
    fixture(["-" + str(env1), "-" + str(env1), "-" + str(env2)])
    env1.write_text("FIXTURE_ONLY=1\n")
    return observe(((str(env1), True), (str(env1), True), (str(env2), True)), True)

def running():
    systemctl("start", "astrid.service")
    try:
        summary = observe(((str(env1), True), (str(env1), True), (str(env2), True)))
        summary["stopped_guard"] = expect_refusal(lambda: manager._observe_stopped_systemd_transfer_definition(data))
        return summary
    finally:
        systemctl("stop", "astrid.service")

def changed():
    fixture([])
    count = 0
    def change_after_sample():
        nonlocal count
        value = original_typed()
        count += 1
        if count == 1:
            fixture(["-" + str(env2)])
        return value
    service._typed_environment_files = change_after_sample
    try:
        refusal = expect_refusal(lambda: manager._observe_systemd_transfer_definition(data), (ValueError,))
    finally:
        service._typed_environment_files = original_typed
    assert count == 2
    return {"refusal": refusal, "endpoint_samples": count}

def failed_endpoint():
    fixture([])
    count = 0
    def failure_at_second():
        nonlocal count
        count += 1
        if count == 2:
            # Ask the genuine manager for a non-existent property, then use the
            # candidate's bounded query failure path, with stderr suppressed.
            return original_bus(["get-property", "org.freedesktop.systemd1", "/org/freedesktop/systemd1/unit/astrid_2eservice", "org.freedesktop.systemd1.Service", "NotAProperty"])
        return original_typed()
    service._typed_environment_files = failure_at_second
    try:
        refusal = expect_refusal(lambda: manager._observe_systemd_transfer_definition(data), (ValueError,))
    finally:
        service._typed_environment_files = original_typed
    assert count == 2
    return {"refusal": refusal, "endpoint_samples": count, "injection": "real missing property"}

def manager_failure():
    fixture([])
    old = os.environ["DBUS_SESSION_BUS_ADDRESS"]
    os.environ["DBUS_SESSION_BUS_ADDRESS"] = "unix:path=/nonexistent/systemd-proof-bus"
    try:
        refusal = expect_refusal(lambda: manager._observe_systemd_transfer_definition(data))
        snap = manager._snapshot_linux_r0(version, data, known_children=())
    finally:
        os.environ["DBUS_SESSION_BUS_ADDRESS"] = old
    p = {k: v.result for k, v in snap.predicates.items()}
    assert p["environment_files"] == p["sample_stability"] == "unknown"
    assert not snap.authority
    return {"refusal": refusal, "environment_files": p["environment_files"],
            "sample_stability": p["sample_stability"], "authority": False}

def unknown_snapshot():
    fixture([])
    def unavailable():
        return original_bus(["get-property", "org.freedesktop.systemd1", "/org/freedesktop/systemd1/unit/astrid_2eservice", "org.freedesktop.systemd1.Service", "NotAProperty"])
    service._typed_environment_files = unavailable
    try:
        snap = manager._snapshot_linux_r0(version, data, known_children=())
    finally:
        service._typed_environment_files = original_typed
    p = {k: v.result for k, v in snap.predicates.items()}
    assert p["environment_files"] == p["effective_environment_files"] == p["sample_stability"] == "unknown"
    assert not snap.authority
    return {"environment_files": p["environment_files"], "effective_environment_files": p["effective_environment_files"], "sample_stability": p["sample_stability"], "authority": False}

def controls():
    common = ["busctl", "--user", "--json=short", "--no-pager", "--timeout=15", "--auto-start=no"]
    for name, args in {
        "wrong_property": ["get-property", "org.freedesktop.systemd1", "/org/freedesktop/systemd1/unit/astrid_2eservice", "org.freedesktop.systemd1.Service", "NotAProperty"],
        "wrong_interface": ["get-property", "org.freedesktop.systemd1", "/org/freedesktop/systemd1/unit/astrid_2eservice", "org.freedesktop.systemd1.NoSuchInterface", "EnvironmentFiles"],
        "wrong_object": ["get-property", "org.freedesktop.systemd1", "/org/freedesktop/systemd1/unit/does_2dnot_2dexist_2eservice", "org.freedesktop.systemd1.Service", "EnvironmentFiles"],
    }.items():
        rc, out, err = command(common + args)
        result["controls"][name] = {"exit": rc, "stdout_sha256": digest(out), "stderr_sha256": digest(err)}
        assert rc != 0
    return "all refused"

try:
    assert os.getuid() > 0 and os.getuid() == os.geteuid()
    rc, out, _ = command(["busctl", "--user", "status", "org.freedesktop.systemd1"])
    result["manager_bus_status"] = {"exit": rc, "stdout_sha256": digest(out)}
    # Status may be denied even when the actual user-bus queries work.
    result["boot_id_sha256"] = digest(Path("/proc/sys/kernel/random/boot_id").read_bytes())
    result["pid_namespace_inode"] = Path("/proc/self/ns/pid").stat().st_ino
    for name, fn in (("empty_stopped", empty), ("single_optional_stopped", single),
                     ("multiple_duplicate_stopped", multiple), ("glob_stopped", glob_case), ("optional_created", created),
                     ("running", running), ("changed_endpoints", changed),
                     ("failed_second_endpoint", failed_endpoint), ("manager_unavailable", manager_failure), ("unknown_snapshot", unknown_snapshot),
                     ("invalid_dbus_controls", controls)):
        do_case(name, fn)
except Exception as exc:
    result["setup_error_type"] = type(exc).__name__
finally:
    result["end_ns"] = time.time_ns()
    result["exit"] = 1 if result.get("setup_error_type") or any(v["exit"] for v in result["cases"].values()) or len(result["cases"]) != 11 else 0
    (base / "result.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
raise SystemExit(result["exit"])
