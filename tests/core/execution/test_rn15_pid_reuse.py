"""RN-15: a later Linux incarnation never inherits its predecessor's custody."""

import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from astrid.core.execution import custody_broker as custody
from astrid.core.execution import process_group as groups


def _observed_process(monkeypatch, *, root_birth="proc-start-ticks:100;boot:boot;pidns:1234", child_birth="proc-start-ticks:500;boot:boot;pidns:1234"):
    monkeypatch.setattr(groups.sys, "platform", "linux")
    observations = []
    process = SimpleNamespace(
        pid=100,
        returncode=None,
        _astrid_process_birth=root_birth,
        _astrid_tree_members={100: root_birth, 200: child_birth},
        _astrid_custody=SimpleNamespace(
            _handles={}, observe=lambda live: observations.append(dict(live)),
        ),
    )
    return process, observations


def _replacement_snapshot(*, child_birth="proc-start-ticks:900;boot:boot;pidns:1234", child_parent=1):
    return {
        100: groups._ProcessInfo(100, 1, 100, "proc-start-ticks:100;boot:boot;pidns:1234"),
        200: groups._ProcessInfo(200, child_parent, 200, child_birth),
        300: groups._ProcessInfo(300, 200, 200, "proc-start-ticks:901;boot:boot;pidns:1234"),
    }


def test_later_linux_pid_without_handle_retires_only_the_old_incarnation(monkeypatch):
    process, observations = _observed_process(monkeypatch)
    monkeypatch.setattr(groups, "_process_snapshot", _replacement_snapshot)

    assert groups.observe_tree(process) == {100: "proc-start-ticks:100;boot:boot;pidns:1234"}
    assert process._astrid_tree_members == {100: "proc-start-ticks:100;boot:boot;pidns:1234"}
    assert process._astrid_tree_exited[(200, "proc-start-ticks:500;boot:boot;pidns:1234")] == "proc-start-ticks:900;boot:boot;pidns:1234"
    assert observations == [{100: "proc-start-ticks:100;boot:boot;pidns:1234"}]
    assert not hasattr(process, "_astrid_tree_uncertain")
    assert groups.observe_tree(process) == {100: "proc-start-ticks:100;boot:boot;pidns:1234"}


@pytest.mark.parametrize("replacement", ["proc-start-ticks:400;boot:boot;pidns:1234", "proc-start-ticks:0500;boot:boot;pidns:1234", "proc-start-ticks:900;boot:other-boot;pidns:1234", "boot:invalid"])
def test_unproved_reuse_names_the_disagreeing_identity(monkeypatch, replacement):
    process, observations = _observed_process(monkeypatch)
    monkeypatch.setattr(groups, "_process_snapshot", lambda: _replacement_snapshot(child_birth=replacement))

    with pytest.raises(groups.CleanupUncertainError, match="owned process identity was reused") as caught:
        groups.observe_tree(process)
    message = str(caught.value)
    for detail in ("pid=200", "expected_birth=proc-start-ticks:500;boot:boot;pidns:1234", f"observed_birth={replacement}",
                   "parent_pid=1", "root=False", "handle=none"):
        assert detail in message
    assert observations == []
    assert process._astrid_tree_members[200] == "proc-start-ticks:500;boot:boot;pidns:1234"


def test_later_linux_pid_never_proves_an_unreaped_root_exit(monkeypatch):
    process, observations = _observed_process(monkeypatch)
    snapshot = _replacement_snapshot()
    snapshot[100] = groups._ProcessInfo(100, 1, 100, "proc-start-ticks:900;boot:boot;pidns:1234")
    monkeypatch.setattr(groups, "_process_snapshot", lambda: snapshot)

    with pytest.raises(groups.CleanupUncertainError, match="identity was reused") as caught:
        groups.observe_tree(process)
    assert "pid=100" in str(caught.value)
    assert "root=True" in str(caught.value)
    assert observations == []


def test_later_pid_with_live_owned_ancestry_is_admitted_as_a_new_process(monkeypatch):
    process, observations = _observed_process(monkeypatch)
    monkeypatch.setattr(groups, "_process_snapshot", lambda: _replacement_snapshot(child_parent=100))

    assert groups.observe_tree(process) == {100: "proc-start-ticks:100;boot:boot;pidns:1234", 200: "proc-start-ticks:900;boot:boot;pidns:1234", 300: "proc-start-ticks:901;boot:boot;pidns:1234"}
    assert process._astrid_tree_exited[(200, "proc-start-ticks:500;boot:boot;pidns:1234")] == "proc-start-ticks:900;boot:boot;pidns:1234"
    assert observations == [{100: "proc-start-ticks:100;boot:boot;pidns:1234", 200: "proc-start-ticks:900;boot:boot;pidns:1234", 300: "proc-start-ticks:901;boot:boot;pidns:1234"}]


def test_later_birth_rule_does_not_apply_to_darwin_tokens(monkeypatch):
    process, observations = _observed_process(monkeypatch)
    monkeypatch.setattr(groups.sys, "platform", "darwin")
    monkeypatch.setattr(groups, "_process_snapshot", _replacement_snapshot)

    with pytest.raises(groups.CleanupUncertainError, match="identity was reused"):
        groups.observe_tree(process)
    assert observations == []


def test_missing_owned_leader_without_exit_witness_stays_uncertain(monkeypatch):
    process, observations = _observed_process(monkeypatch)
    process._astrid_tree_members = {}
    monkeypatch.setattr(groups, "_process_snapshot", lambda: {
        900: groups._ProcessInfo(900, 1, 900, "proc-start-ticks:900;boot:boot;pidns:1234"),
    })

    with pytest.raises(groups.CleanupUncertainError, match="leader exited before ancestry capture"):
        groups.observe_tree(process)
    assert observations == []


def test_later_birth_does_not_bypass_an_unproved_retained_handle(monkeypatch):
    process, observations = _observed_process(monkeypatch)
    process._astrid_custody._handles[200] = ("proc-start-ticks:500;boot:boot;pidns:1234", 42)
    process._astrid_custody.retained_incarnation_exited = lambda *_: False
    monkeypatch.setattr(groups, "_process_snapshot", _replacement_snapshot)

    with pytest.raises(groups.CleanupUncertainError, match="handle=int"):
        groups.observe_tree(process)
    assert observations == []
    assert process._astrid_tree_members[200] == "proc-start-ticks:500;boot:boot;pidns:1234"


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux pidfds")
@pytest.mark.parametrize("released", [False, True])
def test_registration_replaces_only_a_proven_exited_pidfd(monkeypatch, released):
    owner = custody.ProcessCustody()
    old = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    replacement = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        birth = groups.process_birth_identity(old.pid)
        owner.register(old.pid, birth)
        old_descriptor = owner._handles[old.pid][1]
        old.kill()
        old.wait()
        assert owner.retained_incarnation_exited(old.pid, birth)
        if released:
            owner.release_pidfds(only_exited=True)
        tick, boot, namespace = groups.linux_birth_scope(birth)
        later_birth = groups.platform.linux_birth_id(str(int(tick) + 1), boot_id=boot, pidns_inode=int(namespace))
        # Model reuse of the numeric selector; the retained descriptors and
        # their exit/liveness checks still use real kernel processes.
        real_identify = groups.process_birth_identity
        real_open = groups.platform.pidfd_open
        monkeypatch.setattr(groups, "process_birth_identity", lambda pid=None:
                            later_birth if pid == old.pid else real_identify(pid))
        monkeypatch.setattr(groups.platform, "pidfd_open", lambda pid:
                            real_open(replacement.pid if pid == old.pid else pid))
        owner.register(old.pid, later_birth)
        new_descriptor = owner._handles[old.pid][1]
        assert owner._handles[old.pid][0] == later_birth
        assert isinstance(new_descriptor, int)
        assert not owner.retained_incarnation_exited(old.pid, later_birth)
        assert not owner.retained_incarnation_exited(old.pid, birth)
        assert not os.get_inheritable(new_descriptor)
        if old_descriptor != new_descriptor:
            with pytest.raises(OSError):
                os.fstat(old_descriptor)
    finally:
        old.kill()
        old.wait()
        replacement.kill()
        replacement.wait()
        owner.release_pidfds()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux pidfds")
def test_registration_keeps_a_live_old_handle_at_a_later_numeric_pid(monkeypatch):
    owner = custody.ProcessCustody()
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        birth = groups.process_birth_identity(child.pid)
        owner.register(child.pid, birth)
        entry = owner._handles[child.pid]
        tick, boot, namespace = groups.linux_birth_scope(birth)
        monkeypatch.setattr(groups.platform, "pidfd_open", lambda *_: pytest.fail("rebound a live handle"))
        with pytest.raises(custody.CustodyError, match="registered process identity was reused"):
            owner.register(child.pid, groups.platform.linux_birth_id(str(int(tick) + 1), boot_id=boot, pidns_inode=int(namespace)))
        assert owner._handles[child.pid] == entry
        assert child.poll() is None
    finally:
        child.kill()
        child.wait()
        owner.release_pidfds()


@pytest.mark.parametrize("during", ["adoption", "signal"])
def test_parent_replacement_after_census_never_adopts_or_signals(monkeypatch, during):
    process, observations = _observed_process(monkeypatch)
    parent = process._astrid_process_birth
    child = "proc-start-ticks:900;boot:boot;pidns:1234"
    snapshot = groups._Census(_replacement_snapshot(child_parent=100))
    snapshot.status = "complete"
    process._astrid_tree_members = {100: parent}
    process._astrid_custody._handles = {100: (parent, object())}
    process._astrid_custody.signal = lambda *_a, **_k: pytest.fail("changed parent authorized signal")
    replacement = "proc-start-ticks:999;boot:boot;pidns:1234"
    monkeypatch.setattr(groups, "scoped_linux_identity", lambda pid: {
        "pid": pid, "uid": 1000, "parent_pid": 100 if pid == 200 else 1,
        "birth_id": child if pid == 200 else replacement,
    })
    if during == "adoption":
        monkeypatch.setattr(groups, "_process_snapshot", lambda: snapshot)
        with pytest.raises(groups.CleanupUncertainError, match="parent or child identity changed"):
            groups.observe_tree(process)
        assert observations == []
        assert 200 not in process._astrid_tree_members
    else:
        with pytest.raises(groups.CleanupUncertainError, match="parent or child identity changed"):
            groups._signal_valid_tree_members(process, {200: child}, 15, snapshot, strict=True)


@pytest.mark.parametrize("legacy", ["77", "boot:77", "proc-start-ticks:77;boot:boot"])
def test_legacy_group_journal_never_authorizes_signal(monkeypatch, legacy):
    from astrid.core.execution import platform_helpers

    monkeypatch.setattr(groups.sys, "platform", "linux")
    monkeypatch.setattr(groups.platform, "signal_process", lambda *_a: pytest.fail("legacy journal signalled"))
    monkeypatch.setattr(groups, "_process_snapshot", lambda: pytest.fail("legacy journal reached census"))
    with pytest.raises(OSError, match="legacy or unscoped"):
        platform_helpers._signal_linux_observed_group(1234, 15, legacy)
