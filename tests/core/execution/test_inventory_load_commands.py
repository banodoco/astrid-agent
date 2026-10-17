"""Typed inspection authority over the existing retained original-owner channel."""

import json
import os
import shutil
import socket
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.core.execution import custody_broker as cb
from astrid.core.execution import declared_executables as de
from astrid.core.execution import generic_host as host
from astrid.core.execution import process_group as pg


@pytest.fixture
def inspection(tmp_path, monkeypatch):
    installed = tmp_path / "installed"
    installed.mkdir()
    selected = installed / "helper"
    selected.write_bytes(b"executable")
    selected.chmod(0o700)
    alias = installed / "alias"
    alias.symlink_to(selected)
    library = installed / "lib.dylib"
    library.write_bytes(b"library")
    tool = installed / "otool"
    tool.write_bytes(b"tool")
    tool.chmod(0o700)
    pin = de.FilePin.measure(alias)
    admission = de._InspectionAdmission({"helper": pin}, (installed,))
    toolpin = de.FilePin.measure(tool)
    tool_binding = SimpleNamespace(executable=toolpin, verify=toolpin.verify)
    policy = {"inspection": admission, "inspection_tool": tool_binding, "cwd": str(tmp_path)}
    owner = cb._CommandInventoryOwner(SimpleNamespace(root=tmp_path, owner_epoch="epoch"), policy)
    binding = {
        "run_id": "run",
        "owner_epoch": "epoch",
        "generation": 1,
        "target": {
            "pid": 123,
            "uid": 501,
            "birth_id": "precise",
            "audit_token_sha256": "digest",
            "audit_token_pidversion": 9,
        },
    }
    monkeypatch.setattr(owner, "_authenticate", lambda channel: dict(binding))
    frame = {
        "version": cb._COMMAND_INVENTORY_VERSION,
        "command": "inventory_load_commands",
        "binding": dict(binding),
        "operation_id": 1,
        "selection": "helper",
        "image": str(selected),
    }
    return owner, frame, selected, alias, library, tool


def fake_helper(monkeypatch, output=b"", *, code=0, wait=0, closure=None, launch=None):
    events = []

    class Helper:
        pid = 456
        _astrid_custody = SimpleNamespace(_observer=None)

        def __init__(self):
            for name, data in [("stdout", output), ("stderr", b"")]:
                read, write = os.pipe()
                os.write(write, data)
                os.close(write)
                setattr(self, name, os.fdopen(read, "rb", buffering=0))

        def poll(self):
            return code

        def wait(self, timeout):
            events.append("wait")
            return wait

    helper = Helper()

    def spawn(argv, **kw):
        events.append(("launch", argv, kw))
        if launch:
            launch()
        return helper

    def verify(process):
        assert process is helper
        events.append("closure")
        if closure:
            raise cb.CustodyError(closure)

    monkeypatch.setattr(pg, "popen_owned_process", spawn)
    monkeypatch.setattr(pg, "verify_tree_absent", verify)
    return helper, events


def test_selected_alias_dependency_and_sequential_original_wait(inspection, monkeypatch):
    owner, frame, selected, alias, library, tool = inspection
    output = f"cmd LC_RPATH\npath @loader_path (offset 12)\ncmd LC_LOAD_DYLIB\nname @rpath/{library.name} (offset 24)\n".encode()
    helper, events = fake_helper(monkeypatch, output)
    left, right = socket.socketpair()
    try:
        frame["image"] = str(alias)
        assert owner._execute(frame, right).returncode == 0
        assert events[0][1] == [str(tool), "-l", str(selected)]
        assert events[0][2]["env"] == {"PATH": "/usr/bin:/bin", "LC_ALL": "C"}
        assert events[0][2]["_delegate_inventory"] is False
        assert events[1:] == ["wait", "closure"]
        helper, events = fake_helper(monkeypatch)
        frame.update(operation_id=2, image=str(library))
        assert owner._execute(frame, right).returncode == 0
        assert owner.last_operation == 2 and owner.pending is None and len(owner.helpers) == 2
        assert events[1:] == ["wait", "closure"]
        owner.done.set()
        owner.finish()
        assert owner.accepting is False
        frame["operation_id"] = 3
        with pytest.raises(cb.CustodyError):
            owner._execute(frame, right)
    finally:
        left.close()
        right.close()


@pytest.mark.parametrize(
    "fault",
    [
        "digest",
        "argv",
        "flags",
        "cwd",
        "environment",
        "callsite",
        "command",
        "version",
        "sequence",
        "epoch",
        "generation",
        "pid",
        "uid",
        "birth_id",
        "audit_token_sha256",
        "audit_token_pidversion",
        "pending",
        "closed",
        "sticky",
        "unselected",
        "unknown_file",
        "unadmitted_dependency",
        "alias",
        "file",
        "tool",
        "platform",
        "owner",
    ],
)
def test_inspection_refuses_before_spawn(inspection, monkeypatch, fault):
    owner, frame, selected, alias, library, tool = inspection
    monkeypatch.setattr(
        pg, "popen_owned_process", lambda *a, **kw: pytest.fail("rejected inspection spawned")
    )
    if fault in {"digest", "argv", "flags", "cwd", "environment", "callsite"}:
        frame[fault] = de.FilePin.measure(selected).sha256
    elif fault == "command":
        frame["command"] = "arbitrary_read"
    elif fault == "version":
        frame["version"] = "unknown"
    elif fault == "sequence":
        frame["operation_id"] = 2
    elif fault == "epoch":
        frame["binding"]["owner_epoch"] = "other"
    elif fault == "generation":
        frame["binding"]["generation"] = 2
    elif fault in frame["binding"]["target"]:
        frame["binding"]["target"] = dict(frame["binding"]["target"])
        frame["binding"]["target"][fault] = "other"
    elif fault == "pending":
        owner.pending = 1
    elif fault == "closed":
        owner.accepting = False
    elif fault == "sticky":
        owner.error = RuntimeError("retained")
    elif fault == "unselected":
        frame["selection"] = "otool"
    elif fault == "unknown_file":
        frame["image"] = "/etc/hosts"
    elif fault == "unadmitted_dependency":
        frame["image"] = str(library)
    elif fault == "alias":
        alias.unlink()
        alias.symlink_to(library)
    elif fault == "file":
        selected.write_bytes(b"changed")
    elif fault == "tool":
        tool.write_bytes(b"changed")
    elif fault == "platform":
        monkeypatch.setattr(de.os, "uname", lambda: ("other",))
    elif fault == "owner":
        owner.policy.pop("inspection")
    left, right = socket.socketpair()
    try:
        with pytest.raises((cb.CustodyError, de.ExecutableAdmissionError)):
            owner._execute(frame, right)
        assert not owner.helpers
    finally:
        left.close()
        right.close()


@pytest.mark.parametrize("edge", ["outside", "escape", "dotdot"])
def test_dependency_output_is_not_a_root_grant(inspection, tmp_path, edge):
    owner, frame, selected, alias, library, tool = inspection
    foreign = tmp_path / "foreign"
    foreign.write_bytes(b"correct-digest-is-not-authority")
    if edge == "escape":
        target = selected.parent / "escaped"
        target.symlink_to(foreign)
    elif edge == "dotdot":
        target = selected.parent / ".." / "foreign"
    else:
        target = foreign
    admission = owner.policy["inspection"]
    with pytest.raises(de.ExecutableAdmissionError, match="outside|escapes"):
        admission.complete(
            "helper", str(selected), f"cmd LC_LOAD_DYLIB\nname {target} (offset 24)\n"
        )
    with pytest.raises(de.ExecutableAdmissionError):
        admission.verify("helper", str(target))


@pytest.mark.parametrize(
    "fault",
    [
        "missing_wait",
        "unresolved",
        "reparented",
        "frontier",
        "alias_after",
        "tool_after",
        "disconnect",
        "overflow",
    ],
)
def test_inspection_never_completes_uncertain_helper(inspection, monkeypatch, fault):
    owner, frame, selected, alias, library, tool = inspection
    left, right = socket.socketpair()

    def launch():
        if fault == "alias_after":
            alias.unlink()
            alias.symlink_to(library)
        if fault == "tool_after":
            tool.write_bytes(b"replaced")
        if fault == "disconnect":
            left.close()

    helper, events = fake_helper(
        monkeypatch,
        wait=None if fault == "missing_wait" else 0,
        closure=fault if fault in {"unresolved", "reparented", "frontier"} else None,
        launch=launch,
    )
    if fault == "overflow":
        monkeypatch.setattr(cb.os, "read", lambda fd, n: b"x" * (16 * 1024 * 1024 + 1))
    try:
        with pytest.raises(cb.CustodyError, match="ownership remains uncertain"):
            owner._execute(frame, right)
        assert owner.error is not None and owner.pending == 1 and owner.helpers[1] is helper
        owner.done.set()
        with pytest.raises(cb.CustodyError):
            owner.finish()
    finally:
        left.close()
        right.close()


def test_private_client_missing_owner_never_runs_local_tool(monkeypatch):
    monkeypatch.delenv(cb._COMMAND_INVENTORY_FD, raising=False)
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: pytest.fail("local fallback"))
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: pytest.fail("local fallback"))
    with pytest.raises(cb.CustodyError, match="original owner"):
        cb._inventory_load_commands("helper", Path("/etc/hosts"))
    with pytest.raises(cb.CustodyError, match="selection"):
        de._load_commands(Path("/etc/hosts"))


def write_source(tmp_path, binary):
    from tests.test_generic_host import _write_manifest

    pack = tmp_path / "pack"
    manifest = _write_manifest(pack)
    value = json.loads(manifest.read_text())
    value["isolation"] = {"mode": "subprocess", "network": False, "binaries": [binary]}
    manifest.write_text(json.dumps(value))
    return pack


def run_assertions_with_retained_owner(tmp_path, nodeid, binaries):
    """Run one assertion body inside a real Host-inspection owner launch."""
    pack = write_source(tmp_path, binaries[0])
    manifests = list(pack.rglob("executor.json")) or list(pack.rglob("executor.yaml"))
    manifest = manifests[0]
    import yaml

    value = yaml.safe_load(manifest.read_text())
    value["isolation"]["binaries"] = list(binaries)
    manifest.write_text(json.dumps(value))

    node_bin = "/Users/peteromalley/.nvm/versions/node/v20.19.4/bin"
    path = os.environ["PATH"]
    if "node" in binaries:
        path = node_bin + os.pathsep + path
    env = dict(
        os.environ,
        PATH=path,
        PYTHONPATH=str(Path.cwd()),
        PYTHONDONTWRITEBYTECODE="1",
        XDG_STATE_HOME=str(tmp_path / "no-state"),
        ASTRID_TEST_RETAINED_OWNER_CHILD="1",
    )
    env.pop("ASTRID_SOURCE_STATE", None)
    launch = cb._HostInspectionLaunch((pack,), Path.cwd(), env)
    script = tmp_path / "run-retained-owner-test.py"
    script.write_text(
        """from pathlib import Path
import sys
import pytest
from tests.core.execution import test_confinement_launch_integration as confinement
from tests.core.execution import test_remotion_attempt_policy as policy
from tests.core.execution import test_remotion_closure_binding as closure_tests
from tests.core.execution import test_renderer_retained_transport as transport

root = Path(sys.argv[2])
root.mkdir()
patch = pytest.MonkeyPatch()
try:
    roots = closure_tests.roots.__wrapped__(root, patch)
    selector = sys.argv[1]
    if selector == "test_admitted_project_read_only_in_real_child":
        policy.test_admitted_project_read_only_in_real_child(root, roots, patch, None)
    elif selector in {
        "test_changed_binding_refused_before_projection",
        "test_other_capability_does_not_inherit_renderer_read_scope",
    }:
        getattr(policy, selector)(root, roots, None)
    elif selector == "test_node_adopts_owner_retained_listener_without_child_bind":
        launch = confinement.launch.__wrapped__(root)
        transport.test_node_adopts_owner_retained_listener_without_child_bind(
            launch, roots, root, None
        )
    else:
        raise AssertionError(f"unexpected retained-owner selector: {selector}")
finally:
    patch.undo()
""",
        encoding="utf-8",
    )
    process = None
    try:
        process = pg.popen_owned_group(
            [sys.executable, "-B", str(script), nodeid.rsplit("::", 1)[1], str(tmp_path / "child-fixtures")],
            cwd=str(Path.cwd()),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            _inspection_launch=launch,
        )
        stdout, stderr = process.communicate(timeout=40)
        process.wait(timeout=0)
        owner = process._astrid_custody._inventory_owner
        if process.returncode != 0:
            raise AssertionError(
                "retained-owner assertion process failed:\n"
                + stdout.decode(errors="replace")
                + stderr.decode(errors="replace")
                + f"\nowner_error={owner.error!r}; first_failure={owner.first_failure!r}; "
                + f"pending={owner.pending!r}; accepting={owner.accepting!r}"
            )
        assert len(owner.helpers) >= len(binaries)
        if "ffprobe" in binaries:
            assert len(launch.inspection.images["ffprobe"]) > 1
        owner.finish()
        pg.verify_tree_absent(process)
        assert not owner.accepting and owner.pending is None and owner.error is None
        assert not (tmp_path / "no-state").exists()
    finally:
        if process is not None and process.poll() is None:
            pg.terminate_tree(process)
            process.wait(timeout=5)


def test_no_managed_state_trusted_policy_is_retained_and_git_exact(tmp_path, monkeypatch):
    pack = write_source(tmp_path, "python3")
    (tmp_path / ".git").mkdir()
    env = {"PATH": os.environ["PATH"], "XDG_STATE_HOME": str(tmp_path / "absent")}
    monkeypatch.setattr(
        cb, "_managed_inventory_launch_policy", lambda *a: pytest.fail("read default user state")
    )
    launch = cb._HostInspectionLaunch((pack,), tmp_path, env)
    policy = cb._inventory_launch_policy(env, str(tmp_path), launch)
    assert policy["inspection"].selections["python3"] and policy["inspection_tool"]
    assert policy["roots"] == {} and "state_path" not in policy
    owner = cb._CommandInventoryOwner(SimpleNamespace(), policy)
    owner._validate_policy(str(tmp_path), str(tmp_path), env, "revision")
    for operation, root in [
        ("revision", str(pack)),
        ("clean_status", str(tmp_path)),
        ("tracked_files", str(tmp_path)),
    ]:
        with pytest.raises(cb.CustodyError):
            owner._validate_policy(root, str(tmp_path), env, operation)
    assert not (tmp_path / "absent").exists()
    assert not list(tmp_path.rglob("pack-sources.json"))


def test_vcs_revision_routes_only_fixed_request(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    calls = []
    monkeypatch.setattr(
        cb,
        "command_inventory_git",
        lambda args: calls.append(args) or subprocess.CompletedProcess(args, 0, "revision\n", ""),
    )
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: pytest.fail("local Git"))
    assert host._vcs_revision(tmp_path) == "revision"
    assert calls == [["-C", str(tmp_path), "rev-parse", "HEAD"]]


@pytest.mark.skipif(sys.platform != "darwin", reason="native retained Darwin owner")
def test_actual_platform_tool_original_owner_channel_and_waits(tmp_path):
    pack = write_source(tmp_path, "true")
    manifest = (
        next(pack.rglob("executor.json"))
        if list(pack.rglob("executor.json"))
        else next(pack.rglob("executor.yaml"))
    )
    # The helper fixture's JSON is valid YAML as well.
    import yaml

    value = yaml.safe_load(manifest.read_text())
    names = ["true", "false"]
    if shutil.which("ffprobe") is not None:
        names.append("ffprobe")  # Inspect dependencies; never execute ffprobe/media actions.
    value["isolation"]["binaries"] = names
    manifest.write_text(json.dumps(value))
    env = dict(
        os.environ,
        PATH=os.environ["PATH"],
        PYTHONPATH=str(Path.cwd()),
        PYTHONDONTWRITEBYTECODE="1",
        XDG_STATE_HOME=str(tmp_path / "no-state"),
    )
    env.pop("ASTRID_SOURCE_STATE", None)
    env["INSPECTION_TEST_NAMES"] = json.dumps(names)
    launch = cb._HostInspectionLaunch((pack,), tmp_path, env)
    script = """
from astrid.core.execution.declared_executables import ExecutableAdmission
from astrid.core.execution import custody_broker as cb
import os,json
bindings=ExecutableAdmission(os.environ['PATH']).require(tuple(json.loads(os.environ['INSPECTION_TEST_NAMES'])))
print(json.dumps({'names':[b.name for b in bindings],'channel':cb._COMMAND_INVENTORY_FD in os.environ}),flush=True)
for client in cb._COMMAND_CLIENTS.values():client.channel.close()
"""
    process = None
    record = {"helpers": [], "settlement": "UNPROVED"}
    try:
        process = pg.popen_owned_group(
            [sys.executable, "-B", "-c", script],
            cwd=str(tmp_path),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            _inspection_launch=launch,
        )
        stdout, stderr = process.communicate(timeout=40)
        record["root_native_start"] = process._astrid_custody._launch_broker.registration.get(
            "bootstrap_start_anchor"
        )
        record["names"] = names
        record["root_pid"] = process.pid
        record["root_wait"] = process.wait(timeout=0)
        owner = process._astrid_custody._inventory_owner
        for operation, helper in owner.helpers.items():
            broker = helper._astrid_custody._launch_broker
            registration = broker.registration or {}
            record["helpers"].append(
                {
                    "operation": operation,
                    "pid": helper.pid,
                    "original_wait": helper.returncode,
                    "identity": registration.get("identity"),
                    "native_start": registration.get("bootstrap_start_anchor"),
                    "audit_token_sha256": registration.get("audit_token_sha256"),
                    "pidversion": registration.get("audit_token_pidversion"),
                    "retained": sorted(helper._astrid_custody._handles),
                    "unadmitted": sorted(helper._astrid_custody._unadmitted_absent),
                    "error_type": type(helper._astrid_custody._uncertain).__name__
                    if helper._astrid_custody._uncertain
                    else None,
                }
            )
        record["first_failure"] = owner.first_failure
        assert process.returncode == 0, stderr.decode()
        assert json.loads(stdout) == {"names": names, "channel": True}
        assert len(owner.helpers) >= len(names)
        if "ffprobe" in names:
            assert len(launch.inspection.images["ffprobe"]) > 1
        owner.finish()
        pg.verify_tree_absent(process)
        assert not owner.accepting and owner.pending is None and owner.error is None
        assert not (tmp_path / "no-state").exists()
        record["settlement"] = "VERIFIED"
    finally:
        if process is not None and process.poll() is None:
            pg.terminate_tree(process)
            process.wait(timeout=5)
        evidence = os.environ.get("ASTRID_J117_EVIDENCE")
        if evidence:
            Path(evidence, "native-tool-waits-final.json").write_text(
                json.dumps(record, indent=2) + "\n"
            )


def test_failed_tool_returns_original_code_without_admitting_edges(inspection, monkeypatch):
    owner, frame, selected, alias, library, tool = inspection
    helper, events = fake_helper(
        monkeypatch, f"cmd LC_LOAD_DYLIB\nname {library} (offset 24)\n".encode(), code=7, wait=7
    )
    left, right = socket.socketpair()
    try:
        assert owner._execute(frame, right).returncode == 7
        assert events[1:] == ["wait", "closure"]
        with pytest.raises(de.ExecutableAdmissionError):
            owner.policy["inspection"].verify("helper", str(library))
        monkeypatch.setattr(
            cb, "_inventory_load_commands", lambda *a: subprocess.CompletedProcess([], 7, "", "")
        )
        with pytest.raises(de.ExecutableAdmissionError, match="cannot inspect"):
            de._load_commands(selected)
    finally:
        left.close()
        right.close()


def test_close_admission_drains_original_pending_operation(inspection, monkeypatch):
    owner, frame, *_ = inspection
    helper, events = fake_helper(monkeypatch)
    launched = threading.Event()
    release = threading.Event()
    helper.poll = lambda: 0 if release.is_set() else None
    original_launch = pg.popen_owned_process

    def launch(*a, **kw):
        result = original_launch(*a, **kw)
        launched.set()
        return result

    monkeypatch.setattr(pg, "popen_owned_process", launch)
    left, right = socket.socketpair()
    errors = []

    def execute():
        try:
            owner._execute(frame, right)
        except BaseException as error:  # noqa: BLE001 - propagate worker failures to the assertion.
            errors.append(error)
        finally:
            owner.done.set()

    worker = threading.Thread(target=execute)
    worker.start()
    assert launched.wait(2)
    finisher = threading.Thread(target=owner.finish)
    finisher.start()
    try:
        with owner.lock:
            assert owner.accepting is False and owner.pending == 1
        assert finisher.is_alive()
        release.set()
        worker.join(2)
        finisher.join(2)
        assert not errors and not worker.is_alive() and not finisher.is_alive()
        assert owner.pending is None and events[1:] == ["wait", "closure"]
    finally:
        release.set()
        left.close()
        right.close()


@pytest.mark.parametrize("fault", ["alias", "developer", "developer_same_target", "platform"])
def test_platform_tool_selection_and_alias_are_pins(inspection, tmp_path, monkeypatch, fault):
    _, _, selected, alias, library, tool = inspection
    binding = de._InspectionTool.__new__(de._InspectionTool)
    binding.platform = tuple(os.uname())
    binding.alias = de.FilePin.measure(alias)
    binding.executable = de.FilePin.measure(tool)
    binding.developer = tmp_path / "developer"
    binding.developer.symlink_to(tmp_path, target_is_directory=True)
    binding.base = tmp_path
    _, binding.selection_identity = binding._developer_selection()
    binding.verify()
    if fault == "alias":
        alias.unlink()
        alias.symlink_to(library)
    elif fault in {"developer", "developer_same_target"}:
        binding.developer.unlink()
        binding.developer.symlink_to(
            selected.parent if fault == "developer" else tmp_path, target_is_directory=True
        )
    else:
        monkeypatch.setattr(de.os, "uname", lambda: ("other",))
    with pytest.raises(de.ExecutableAdmissionError):
        binding.verify()


def test_changed_dependency_parent_cannot_authorize_further_inspection(inspection):
    owner, frame, selected, alias, library, tool = inspection
    nested = selected.parent / "nested.dylib"
    nested.write_bytes(b"nested")
    admission = owner.policy["inspection"]
    admission.complete("helper", str(selected), f"cmd LC_LOAD_DYLIB\nname {library} (offset 24)\n")
    admission.complete("helper", str(library), f"cmd LC_LOAD_DYLIB\nname {nested} (offset 24)\n")
    admission.verify("helper", str(nested))
    library.write_bytes(b"replaced dependency parent")
    with pytest.raises(de.ExecutableAdmissionError):
        admission.verify("helper", str(nested))


def test_declared_bind_private_caller_preserves_selection_and_parser(inspection, monkeypatch):
    owner, frame, selected, alias, library, tool = inspection
    calls = []

    def inspect(name, path):
        calls.append((name, path))
        output = f"cmd LC_LOAD_DYLIB\nname {library} (offset 24)\n" if path == selected else ""
        return subprocess.CompletedProcess([], 0, output, "")

    monkeypatch.setattr(cb, "_inventory_load_commands", inspect)
    binding = de._bind("helper", alias)
    assert calls == [("helper", selected), ("helper", library)]
    assert binding.libraries[0].path == library
    assert de._INSPECTION_SELECTION.get() is None


def test_inspection_without_repository_does_not_admit_ancestor_git(tmp_path):
    pack = write_source(tmp_path, "true")
    env = {"PATH": "/usr/bin:/bin", "XDG_STATE_HOME": str(tmp_path / "absent")}
    launch = cb._HostInspectionLaunch((pack,), tmp_path, env)
    policy = cb._inventory_launch_policy(env, str(tmp_path), launch)
    assert "revision_checkout" not in policy and "git" not in policy
    owner = cb._CommandInventoryOwner(SimpleNamespace(), policy)
    with pytest.raises(cb.CustodyError):
        owner._validate_policy(str(tmp_path), str(tmp_path), env, "revision")
