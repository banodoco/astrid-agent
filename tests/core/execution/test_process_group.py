from __future__ import annotations

import signal
import subprocess
import os
import sys
import select
from pathlib import Path
from types import SimpleNamespace
import threading
import time

import pytest

from astrid.core.execution import process_group as groups
from astrid.core.execution import custody_broker as custody


class FakeCustody:
    def __init__(self):
        self.registered = {}
        self.signal_owner = lambda *_: None

    def observe(self, members):
        self.registered.update(members)

    def signal(self, pid, birth, signum):
        assert self.registered[pid] == birth
        self.signal_owner(pid, signum)


def _unadmitted_process(tmp_path, monkeypatch):
    process = _process()
    broker = object.__new__(custody.RoleBoundCustodyBroker)
    broker.role = "command"
    owner = object.__new__(custody.ProcessCustody)
    owner.root, owner.owner_epoch = tmp_path / "custody", "fake-owner-epoch"
    owner._lock, owner._uncertain = threading.RLock(), None
    owner._handles = {100: ("root", broker)}
    owner._unadmitted_absent, owner._census_sequence = {}, 0
    owner._launch_broker, owner.process = broker, process
    owner.actor = SimpleNamespace(verify=lambda: {"pid": 99, "uid": 501, "birth_id": "actor"})
    process._astrid_custody, process._astrid_process_group_id = owner, 100
    monkeypatch.setattr(custody.sys, "platform", "darwin")
    monkeypatch.setattr(custody, "default_process_identity", lambda _: None)
    monkeypatch.setattr(custody, "ordinary_candidate_observation", lambda _: {"outcome": "ABSENT"})
    monkeypatch.setattr(custody, "current_process_audit_token", lambda *_: pytest.fail("unadmitted token capture"))
    monkeypatch.setattr(custody, "_darwin_process_records", lambda *_: pytest.fail("fake vector queried native census"))
    return process, owner


def test_unadmitted_disappearance_reconciles_and_cleanup_waits_only_retained_root(tmp_path, monkeypatch):
    process, owner = _unadmitted_process(tmp_path, monkeypatch)
    initial = _snapshot()
    fresh = {100: initial[100], 900: initial[900]}
    snapshots = iter((initial, fresh))
    monkeypatch.setattr(groups, "_process_snapshot", lambda: next(snapshots))
    assert groups.observe_tree(process) == {100: "root"}
    assert 200 in process._astrid_tree_members and 200 not in owner._handles
    assert process._astrid_cleanup_semantics["complete_descendant_history"] == "UNPROVED"
    assert owner._unadmitted_absent[200]["exit_reap"] == "UNPROVED"
    assert not hasattr(process, "_astrid_tree_uncertain")
    calls, waits = [], []
    def signal_root(pid, birth, signum, **kwargs):
        assert (pid, birth) == (100, "root")
        calls.append((pid, signum))
        fresh.pop(100)
    owner.signal = signal_root
    process.poll = lambda: 0 if 100 not in fresh else None
    process.wait = lambda **kw: waits.append(kw) or 0
    monkeypatch.setattr(groups, "_process_snapshot", lambda: fresh)
    groups.terminate_tree(process, grace_seconds=0)
    groups.verify_tree_absent(process)
    assert calls == [(100, signal.SIGTERM)] and waits == [{"timeout": 0}]
    assert owner._unadmitted_absent[200]["subtree"] == "UNPROVED"


@pytest.mark.parametrize("failure", ["reappeared", "reused", "missing-parent-child", "fresh-census-error"])
def test_unadmitted_fresh_census_contradictions_block_tree_cleanup(tmp_path, monkeypatch, failure):
    process, owner = _unadmitted_process(tmp_path, monkeypatch)
    initial = _snapshot()
    fresh = {100: initial[100], 900: initial[900]}
    if failure == "reappeared":
        fresh[200] = initial[200]
    elif failure == "reused":
        fresh[200] = groups._ProcessInfo(200, 1, 200, "replacement")
    elif failure == "missing-parent-child":
        fresh[300] = groups._ProcessInfo(300, 200, 300, "unverified-child")
    snapshots = iter((initial, fresh))
    def snapshot():
        row = next(snapshots)
        if row is fresh and failure == "fresh-census-error":
            raise OSError("census failed")
        return row
    monkeypatch.setattr(groups, "_process_snapshot", snapshot)
    with pytest.raises((custody.CustodyError, OSError)):
        groups.observe_tree(process)
    assert owner._uncertain is not None and process._astrid_tree_uncertain
    assert 200 not in owner._handles
    owner.signal = lambda *_: pytest.fail("unresolved tree signalled")
    monkeypatch.setattr(groups, "_process_snapshot", lambda: {900: initial[900]})
    with pytest.raises(custody.CustodyError):
        groups.terminate_tree(process, grace_seconds=0)


def test_concurrent_census_reconciliation_is_serial_and_tombstone_is_not_erased(tmp_path, monkeypatch):
    process, owner = _unadmitted_process(tmp_path, monkeypatch)
    initial = _snapshot()
    fresh = {100: initial[100], 900: initial[900]}
    calls = []
    def snapshot():
        calls.append(threading.get_ident())
        return initial if len(calls) == 1 else fresh
    monkeypatch.setattr(groups, "_process_snapshot", snapshot)
    results, errors = [], []
    def observe():
        try:
            results.append(groups.observe_tree(process))
        except BaseException as error:
            errors.append(error)
    workers = [threading.Thread(target=observe) for _ in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(2)
        assert not worker.is_alive()
    assert not errors and results == [{100: "root"}, {100: "root"}]
    assert owner._unadmitted_absent[200]["disposition"] == "UNADMITTED_ABSENT"
    assert owner._unadmitted_absent[200]["observation_sequence"] == 1


@pytest.mark.parametrize("contradiction", ["reappearance", "reuse", "missing-parent-child", "admitted-detached"])
def test_final_census_cannot_bypass_retained_obligations_outside_owned_group(tmp_path, monkeypatch, contradiction):
    process, owner = _unadmitted_process(tmp_path, monkeypatch)
    initial = _snapshot()
    fresh = {100: initial[100], 900: initial[900]}
    samples = iter((initial, fresh))
    monkeypatch.setattr(groups, "_process_snapshot", lambda: next(samples))
    groups.observe_tree(process)
    absent = {900: initial[900]}
    final = dict(absent)
    if contradiction == "missing-parent-child":
        final[300] = groups._ProcessInfo(300, 200, 777, "child")
    elif contradiction == "admitted-detached":
        owner._handles[300] = ("retained", object())
        process._astrid_tree_members[300] = "retained"
        final[300] = groups._ProcessInfo(300, 1, 777, "retained")
    else:
        final[200] = groups._ProcessInfo(200, 1, 777, "detached-child" if contradiction == "reappearance" else "replacement")
    samples = iter((absent, final))
    monkeypatch.setattr(groups, "_process_snapshot", lambda: next(samples))
    with pytest.raises(custody.CustodyError):
        groups.verify_tree_absent(process)
    assert owner._uncertain is not None and process._astrid_tree_uncertain


def test_fresh_only_child_enters_tree_bookkeeping_before_reparenting(tmp_path, monkeypatch):
    process, owner = _unadmitted_process(tmp_path, monkeypatch)
    initial = _snapshot()
    fresh = {100: initial[100], 900: initial[900],
             300: groups._ProcessInfo(300, 100, 300, "fresh-child")}
    reparented = {**fresh, 300: groups._ProcessInfo(300, 1, 300, "fresh-child")}
    original = owner.register
    def register(pid, birth, **kwargs):
        if pid == 300:
            owner._handles[pid] = (birth, object())
        else:
            original(pid, birth, **kwargs)
    monkeypatch.setattr(owner, "register", register)
    samples = iter((initial, fresh, reparented))
    monkeypatch.setattr(groups, "_process_snapshot", lambda: next(samples))
    assert groups.observe_tree(process) == {100: "root", 300: "fresh-child"}
    assert process._astrid_tree_members[300] == "fresh-child"
    monkeypatch.setattr(groups, "_process_snapshot", lambda: reparented)
    assert groups.observe_tree(process) == {100: "root", 300: "fresh-child"}


@pytest.mark.darwin_native
def test_native_multiple_exec_keeps_birth_anchor_and_cleans_with_fresh_token():
    source = (
        "import os,sys,time; stage=int(sys.argv[1]); "
        "print('ready:'+str(stage),flush=True); "
        "sys.stdin.readline() if stage<3 else time.sleep(30); "
        "os.execv(sys.executable,[sys.executable,'-c',sys.argv[2],str(stage+1),sys.argv[2]])"
    )
    process = groups.popen_owned_process(
        [sys.executable, "-c", source, "0", source], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        start_new_session=True,
    )
    broker = process._astrid_custody._launch_broker
    authority = broker.role_authority
    anchor = dict(authority._live_start_anchor)
    suspended_token = list(broker.registration["audit_token_words"])
    try:
        for stage in range(4):
            assert select.select([process.stdout], [], [], 5)[0], "target phase timed out"
            assert process.stdout.readline().strip() == f"ready:{stage}"
            assert custody._start_anchor(custody._darwin_process_start(process.pid)) == anchor
            if stage < 3:
                process.stdin.write("exec\n")
                process.stdin.flush()
        original_reference = authority.reference()["target"]
        with pytest.raises(custody.AuditTokenSignalError) as stale:
            custody.signal_audit_token(suspended_token, signal.SIGTERM)
        assert stale.value.result == 3
        groups.terminate_tree(process, grace_seconds=0)
        assert process.poll() == -signal.SIGTERM
        refreshed = authority.reference()["target"]
        assert refreshed["audit_token_pidversion"] != original_reference["audit_token_pidversion"]
        assert refreshed["birth_id"] == original_reference["birth_id"]
        assert authority._live_start_anchor == anchor
    finally:
        if process.poll() is None:
            groups.terminate_tree(process, grace_seconds=0)


def _process():
    return SimpleNamespace(pid=100, _astrid_process_birth="root", poll=lambda: None,
                           wait=lambda **_: 0, _astrid_custody=FakeCustody())


def _snapshot():
    return {
        100: groups._ProcessInfo(100, 1, 100, "root"),
        200: groups._ProcessInfo(200, 100, 200, "detached-child"),
        900: groups._ProcessInfo(900, 1, 900, "unrelated-census-process"),
    }


@pytest.mark.parametrize("failure", ["first-census", "leader-missing", "pid-reuse", "parent-missing"])
def test_observation_uncertainty_persists_without_adopting_unknown_children(monkeypatch, failure):
    process = _process()
    census = _snapshot()
    if failure in {"pid-reuse", "parent-missing"}:
        monkeypatch.setattr(groups, "_process_snapshot", lambda: census)
        groups.observe_tree(process)
    bad = dict(census)
    if failure == "first-census":
        bad = {}
    elif failure == "leader-missing":
        # The escaped writer is now reparented and cannot be adopted by name.
        bad = {200: groups._ProcessInfo(200, 1, 200, "detached-child"), 900: census[900]}
    elif failure == "pid-reuse":
        bad[200] = groups._ProcessInfo(200, 1, 200, "reused-child")
        bad[300] = groups._ProcessInfo(300, 200, 200, "unrelated-grandchild")
    else:
        del bad[200]
        bad[300] = groups._ProcessInfo(300, 200, 300, "unverified-escaped-child")
    monkeypatch.setattr(groups, "_process_snapshot", lambda: bad)
    monkeypatch.setattr(groups.os, "kill", lambda *_: pytest.fail("uncertain tree was signalled"))
    with pytest.raises(groups.CleanupUncertainError):
        groups.observe_tree(process)
    assert 300 not in getattr(process, "_astrid_tree_members", {})
    if failure == "first-census":
        assert not hasattr(process, "_astrid_tree_members")
    monkeypatch.setattr(groups, "_process_snapshot", lambda: census)
    with pytest.raises(groups.CleanupUncertainError):
        groups.verify_tree_absent(process)
    with pytest.raises(groups.CleanupUncertainError):
        groups.terminate_tree(process, grace_seconds=0)


def test_keyboard_interrupt_during_first_census_fails_closed(monkeypatch):
    process = _process()
    signals = []

    def interrupted_census():
        raise KeyboardInterrupt()

    monkeypatch.setattr(groups, "_process_snapshot", interrupted_census)
    process._astrid_custody.signal_owner = lambda *args: signals.append(args)
    with pytest.raises(KeyboardInterrupt):
        groups.observe_tree(process)
    assert process._astrid_tree_uncertain == "KeyboardInterrupt"

    monkeypatch.setattr(groups, "_process_snapshot", _snapshot)
    with pytest.raises(groups.CleanupUncertainError):
        groups.terminate_tree(process, grace_seconds=0)
    assert signals == []


@pytest.mark.parametrize("failure", ["signal-census", "signal-permission", "survivor"])
def test_termination_uncertainty_is_persistent(monkeypatch, failure):
    process = _process()
    census = _snapshot()
    calls = []

    def snapshot():
        return {} if failure == "signal-census" and calls else census

    def kill(pid, sig):
        calls.append((pid, sig))
        if failure == "signal-permission":
            raise PermissionError("injected test signal denial")

    if failure == "signal-census":
        snapshots = iter((census, {}))
        monkeypatch.setattr(groups, "_process_snapshot", lambda: next(snapshots))
    else:
        monkeypatch.setattr(groups, "_process_snapshot", snapshot)
    process._astrid_custody.signal_owner = kill
    ticks = iter(index * 0.2 for index in range(100))
    monkeypatch.setattr(groups.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(groups.time, "sleep", lambda _: None)
    if failure == "survivor":
        def wait(**_):
            raise subprocess.TimeoutExpired("owned test leader", 0)
        process.wait = wait
    with pytest.raises(groups.CleanupUncertainError):
        groups.terminate_tree(process, grace_seconds=0)
    assert hasattr(process, "_astrid_tree_uncertain")
    monkeypatch.setattr(groups, "_process_snapshot", lambda: {900: census[900]})
    with pytest.raises(groups.CleanupUncertainError):
        groups.verify_tree_absent(process)
    assert all(pid != 900 for pid, _ in calls)


@pytest.mark.parametrize("failure", ["kernel", "registration"])
def test_first_signal_callee_metadata_survives_task_wrapper_without_clearing_uncertainty(monkeypatch, failure):
    process = _process()
    census = _snapshot()
    monkeypatch.setattr(groups, "_process_snapshot", lambda: census)
    original = (custody.AuditTokenSignalError(3, 0) if failure == "kernel"
                else custody.CustodyError("custody target identity observation is absent (stage=signal-identity)"))
    process._astrid_custody.signal_owner = lambda *_: (_ for _ in ()).throw(original)
    with pytest.raises(groups.CleanupUncertainError) as error:
        groups.terminate_tree(process, grace_seconds=0)
    message = str(error.value)
    assert "candidate_pid=200" in message and "root_pid=100" in message
    assert "stage=custody-signal" in message and f"signal={int(signal.SIGTERM)}" in message
    assert f"error_type={type(original).__name__}" in message
    assert ("kernel_result=3, errno=0" if failure == "kernel" else "kernel_result=unknown") in message
    assert error.value.__cause__ is original
    assert process._astrid_tree_uncertain == message
    monkeypatch.setattr(groups, "_process_snapshot", lambda: {900: census[900]})
    with pytest.raises(groups.CleanupUncertainError):
        groups.verify_tree_absent(process)


def test_verified_detached_tree_stops_leaves_before_ancestors_and_leader(monkeypatch):
    process = _process()
    census = _snapshot()
    census[300] = groups._ProcessInfo(300, 200, 200, "node")
    census[400] = groups._ProcessInfo(400, 300, 400, "browser")
    census[500] = groups._ProcessInfo(500, 400, 400, "worker")
    monkeypatch.setattr(groups, "_process_snapshot", lambda: dict(census))
    calls = []

    def kill(pid, sig):
        calls.append((pid, sig))
        del census[pid]

    process._astrid_custody.signal_owner = kill
    groups.terminate_tree(process, grace_seconds=0)
    groups.verify_tree_absent(process)
    assert calls == [(pid, signal.SIGTERM) for pid in (500, 400, 300, 200, 100)]
    assert list(census) == [900]


def test_previously_observed_detached_child_remains_owned_after_leader_exit(monkeypatch):
    process = _process()
    census = _snapshot()
    monkeypatch.setattr(groups, "_process_snapshot", lambda: dict(census))
    groups.observe_tree(process)
    del census[100]
    census[200] = groups._ProcessInfo(200, 1, 200, "detached-child")
    process.poll = lambda: 0
    calls = []

    def kill(pid, sig):
        calls.append((pid, sig))
        del census[pid]

    process._astrid_custody.signal_owner = kill
    groups.terminate_tree(process, grace_seconds=0)
    groups.verify_tree_absent(process)
    assert calls == [(200, signal.SIGTERM)]
    assert list(census) == [900]


@pytest.mark.parametrize("failure", ["group-census", "group-survivor"])
def test_final_verification_uncertainty_cannot_be_cleared_by_a_later_census(monkeypatch, failure):
    process = _process()
    process._astrid_process_group_id = process.pid
    census = _snapshot()
    monkeypatch.setattr(groups, "_process_snapshot", lambda: census)
    groups.observe_tree(process)
    absent = {900: census[900]}
    bad = {} if failure == "group-census" else {
        **absent, 700: groups._ProcessInfo(700, 1, 100, "late-group-member"),
    }
    snapshots = iter((absent, bad))
    monkeypatch.setattr(groups, "_process_snapshot", lambda: next(snapshots))
    with pytest.raises(groups.CleanupUncertainError):
        groups.verify_tree_absent(process)
    monkeypatch.setattr(groups, "_process_snapshot", lambda: absent)
    with pytest.raises(groups.CleanupUncertainError):
        groups.verify_tree_absent(process)


@pytest.mark.darwin_native
def test_target_code_waits_until_suspended_launch_observer_is_ready(monkeypatch, tmp_path):
    from astrid.core.execution import custody_broker

    marker = tmp_path / "target-started"
    observer_ready = []
    original = custody_broker.ProcessCustody.start_observer

    def start_observer_while_suspended(owner):
        assert not marker.exists()
        original(owner)
        observer_ready.append(True)
        assert not marker.exists()

    monkeypatch.setattr(custody_broker.ProcessCustody, "start_observer", start_observer_while_suspended)
    code = "from pathlib import Path; import sys; Path(sys.argv[1]).write_text('started')"
    process = groups.popen_owned_process(
        [sys.executable, "-c", code, str(marker)], start_new_session=True,
    )
    try:
        assert observer_ready == [True]
        assert os.getpgid(process.pid) == process.pid
        assert process.wait(timeout=5) == 0
        assert marker.read_text() == "started"
        groups.verify_tree_absent(process)
    finally:
        if process.poll() is None:
            groups.terminate_tree(process, grace_seconds=0.1)


def test_darwin_snapshot_has_no_spawned_observer_and_keeps_all_public_rows(monkeypatch):
    monkeypatch.setattr(groups.sys, "platform", "darwin")
    monkeypatch.setattr(groups.subprocess, "run", lambda *_a, **_kw: pytest.fail("ps observer spawned"))
    rows = [{"pid": 0, "parent_pid": 0, "pgid": 0, "start_sec": 0},
            {"pid": 100, "parent_pid": 1, "pgid": 100, "start_sec": 1791421200},
            {"pid": 200, "parent_pid": 100, "pgid": 200, "start_sec": 1791421201, "status": 5},
            {"pid": 900, "parent_pid": 1, "pgid": 900, "start_sec": 1791421202, "uid": 0}]
    for row in rows:
        row.setdefault("status", 2)
        row["start_usec"] = 0
    monkeypatch.setattr(groups, "_darwin_process_records", lambda: rows)
    snapshot = groups._process_snapshot()
    assert set(snapshot) == {0, 100, 200, 900}
    assert snapshot[0].birth == "" and snapshot[200].ppid == 100
    assert snapshot[100].birth == custody._ps_lstart_birth(1791421200)
    assert groups.process_birth_identity(100) == snapshot[100].birth


def test_native_census_error_latches_existing_tree_uncertainty(monkeypatch):
    process = _process()
    monkeypatch.setattr(groups.sys, "platform", "darwin")
    def failed(): raise custody.CustodyError("sysctl read incomplete")
    monkeypatch.setattr(groups, "_darwin_process_records", failed)
    with pytest.raises(custody.CustodyError, match="incomplete"):
        groups.observe_tree(process)
    monkeypatch.setattr(groups, "_darwin_process_records", lambda: [])
    with pytest.raises(custody.CustodyError, match="incomplete"):
        groups.observe_tree(process)
    assert process._astrid_tree_uncertain == "sysctl read incomplete"


def _terminal_process(tmp_path, monkeypatch):
    process, owner = _unadmitted_process(tmp_path, monkeypatch)
    sec, usec = 1791433449, 654321
    birth = custody._ps_lstart_birth(sec)
    native = {"pid": 200, "uid": 501, "parent_pid": 100, "pgid": 200,
              "status": 5, "start_sec": sec, "start_usec": usec}
    monkeypatch.setattr(custody, "ordinary_candidate_observation", lambda _: {
        "outcome": "ZOMBIE", "native": native,
        "identity": {"pid": 200, "uid": 501, "parent_pid": 100, "birth_id": birth}})
    snapshot = _snapshot()
    snapshot[200] = groups._ProcessInfo(200, 100, 200, birth, 5, sec, usec)
    return process, owner, snapshot


def test_terminal_candidate_is_not_actionable_but_present_group_cannot_be_clean(tmp_path, monkeypatch):
    process, owner, census = _terminal_process(tmp_path, monkeypatch)
    monkeypatch.setattr(groups, "_process_snapshot", lambda: census)
    assert groups.observe_tree(process) == {100: "root"}
    assert 200 in process._astrid_tree_members and 200 not in owner._handles
    assert groups.observe_tree(process) == {100: "root"}
    assert 200 in owner._unadmitted_absent and 200 not in owner._handles
    # Even inherited-group callers must validate retained terminal observations.
    del process._astrid_process_group_id
    child = census[200]
    remaining = {200: groups._ProcessInfo(200, 1, child.pgid, child.birth,
                                         child.status, child.start_sec, child.start_usec),
                 900: census[900]}
    monkeypatch.setattr(groups, "_process_snapshot", lambda: remaining)
    with pytest.raises(custody.CustodyError, match="terminal candidate remains"):
        groups.verify_tree_absent(process)
    assert owner._uncertain is not None


def test_terminal_observation_disappears_without_manufactured_descendant_wait(tmp_path, monkeypatch):
    process, owner, census = _terminal_process(tmp_path, monkeypatch)
    monkeypatch.setattr(groups, "_process_snapshot", lambda: census)
    groups.observe_tree(process)
    absence = {900: census[900]}
    monkeypatch.setattr(groups, "_process_snapshot", lambda: absence)
    groups.verify_tree_absent(process)
    assert owner._unadmitted_absent[200]["absence_observed"]
    assert owner._unadmitted_absent[200]["exit_reap"] == "UNPROVED"
    assert 200 not in owner._handles and not hasattr(owner, "descendant_wait_exit")


def test_terminal_census_concurrent_observers_retain_one_non_authority_record(tmp_path, monkeypatch):
    process, owner, census = _terminal_process(tmp_path, monkeypatch)
    monkeypatch.setattr(groups, "_process_snapshot", lambda: census)
    errors, results = [], []
    def observe():
        try: results.append(groups.observe_tree(process))
        except BaseException as error: errors.append(error)
    workers = [threading.Thread(target=observe) for _ in range(2)]
    for worker in workers: worker.start()
    for worker in workers:
        worker.join(2)
        assert not worker.is_alive()
    assert not errors and results == [{100: "root"}, {100: "root"}]
    assert len(owner._unadmitted_absent) == 1 and not owner._unadmitted_absent[200]["admitted"]
    assert owner._unadmitted_absent[200]["observation_sequence"] == 1


def test_native_census_preserves_terminal_status_and_precise_birth(monkeypatch):
    row = {"pid": 103, "parent_pid": 100, "pgid": 103, "uid": 501,
           "status": 5, "start_sec": 1791433449, "start_usec": 987654}
    monkeypatch.setattr(groups.sys, "platform", "darwin")
    monkeypatch.setattr(groups, "_darwin_process_records", lambda: [row])
    info = groups._process_snapshot()[103]
    assert (info.status, info.start_sec, info.start_usec) == (5, row["start_sec"], 987654)
    evidence = groups._census_evidence({103: info})[103]
    assert evidence["start_usec"] == 987654 and evidence["status"] == 5
    assert evidence["birth_id"] == custody._ps_lstart_birth(row["start_sec"])


@pytest.mark.darwin_native
def test_native_vibe_birth_observation_owned_child_has_no_helper_descendants(tmp_path):
    import json
    child = tmp_path / "native_observer.py"
    source_root = Path(__file__).resolve().parents[3]
    child.write_text("""import sys,os,subprocess
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from astrid.core.execution.vibecomfy_engine_adapter import _ObservationSubprocess,_file_pin
class Original:
 def run(self,*a,**kw): raise AssertionError('native observation spawned helper')
 def __getattr__(self,n): return getattr(subprocess,n)
boundary=_ObservationSubprocess(Original(),{'ps':_file_pin(Path('/bin/ps').resolve())})
for _ in range(8):
 result=boundary.run(['ps','-p',str(os.getpid()),'-o','lstart='],capture_output=True,text=True,check=False,timeout=2)
 assert result.returncode==0 and result.stdout.strip()
print('native-ready',flush=True)
sys.stdin.readline()
""")
    process = groups.popen_owned_process([sys.executable, "-I", "-B", str(child), str(source_root)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        assert select.select([process.stdout], [], [], 5)[0]
        assert process.stdout.readline().strip() == "native-ready"
        observed = groups.observe_tree(process)
        assert observed == {process.pid: process._astrid_process_birth}
        owner = process._astrid_custody
        assert not getattr(process, "_astrid_tree_uncertain", None)
        process.stdin.write("exit\n");process.stdin.flush();assert process.wait(timeout=5) == 0
        groups.verify_tree_absent(process)
        retained = owner.root / "observed-containment.json"
        if retained.exists():
            assert json.loads(retained.read_text())["capture_failures"] == []
        (tmp_path / "native-owned-graph-proof.json").write_text(json.dumps({"original_wait":0,"positive_tree_absence":True,"native_observations":8,"spawned_helpers":0,"capture_failures":0,"no_preimage_custody_failure_forced":True}))
    finally:
        if process.poll() is None: groups.terminate_tree(process, grace_seconds=0)
        for stream in (process.stdin, process.stdout, process.stderr): stream.close()

@pytest.mark.parametrize('stubborn', [False, True])
def test_original_root_term_wait_precedes_kill_and_preserves_escalation_budget(monkeypatch, stubborn):
    process = _process()
    initial = _snapshot()
    census = {100: initial[100], 900: initial[900]}
    monkeypatch.setattr(groups, '_process_snapshot', lambda: dict(census))
    events = []
    def signal_root(pid, sig):
        assert pid == 100
        events.append(('signal', sig))
        if sig == signal.SIGKILL: census.pop(100)
    def wait(*, timeout):
        events.append(('wait', timeout))
        if 100 in census:
            if stubborn: raise subprocess.TimeoutExpired(['original-root'], timeout)
            assert timeout > 0
            census.pop(100)
        return 0
    process._astrid_custody.signal_owner = signal_root
    process.poll = lambda: None if 100 in census else 0
    process.wait = wait
    groups.terminate_tree(process, grace_seconds=1)
    signal_events = [event for event in events if event[0] == 'signal']
    assert signal_events == ([('signal', signal.SIGTERM), ('signal', signal.SIGKILL)] if stubborn else [('signal', signal.SIGTERM)])
    first_wait = next(index for index, event in enumerate(events) if event[0] == 'wait')
    assert 0 < events[first_wait][1] <= .5
    if stubborn: assert first_wait < events.index(('signal', signal.SIGKILL))
    assert events[-1] == ('wait', 0)
    assert 100 not in census and 900 in census

@pytest.mark.darwin_native
def test_native_original_root_term_wait_avoids_unnecessary_kill(monkeypatch, tmp_path):
    import json, select, sys
    assert sys.platform == 'darwin'
    code = "import signal,sys\ndef stop(signum,frame):\n print('TERM-EXIT',flush=True)\n raise SystemExit(0)\nsignal.signal(signal.SIGTERM,stop)\nprint('READY',flush=True)\nsys.stdin.read()\n"
    process = groups.popen_owned_process([sys.executable, '-I', '-B', '-c', code], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True, _delegate_inventory=False)
    owner = process._astrid_custody
    events = []
    original_signal = owner.signal
    def retained_signal(pid, birth, signum, **kwargs):
        events.append({'pid': pid, 'signal': int(signum), 'original_root': pid == process.pid})
        return original_signal(pid, birth, signum, **kwargs)
    monkeypatch.setattr(owner, 'signal', retained_signal)
    trace = {'proof': 'actual original-root TERM handler, bounded original wait, final native absence'}
    try:
        assert select.select([process.stdout], [], [], 5)[0], 'original root readiness timeout'
        assert process.stdout.readline().strip() == 'READY'
        groups.terminate_tree(process, grace_seconds=1)
        assert process.returncode == 0
        assert process.stdout.read().strip() == 'TERM-EXIT'
        assert events == [{'pid': process.pid, 'signal': int(signal.SIGTERM), 'original_root': True}]
        groups.verify_tree_absent(process)
        assert not hasattr(process, '_astrid_tree_uncertain')
        trace.update(original_wait=0, signals=events, final_tree_absent=True)
    finally:
        if process.poll() is None: groups.terminate_tree(process, grace_seconds=.2)
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None: stream.close()
        (tmp_path / 'native-root-term-trace.json').write_text(json.dumps(trace, indent=2) + '\n')


def test_terminal_member_birth_mismatch_cannot_be_filtered_away(tmp_path, monkeypatch):
    process, owner, census = _terminal_process(tmp_path, monkeypatch)
    monkeypatch.setattr(groups, "_process_snapshot", lambda: census)
    assert groups.observe_tree(process) == {100: "root"}
    evidence = groups._census_evidence(census)
    with pytest.raises(custody.CustodyError, match="candidate lacks its first census identity"):
        owner.observe_census({100: "root", 200: "replacement"}, census=evidence, fresh_census=lambda: evidence)
    assert owner._uncertain is not None and 200 not in owner._handles


@pytest.mark.parametrize("case", ["suspended", "reap-timeout", "unvalidated", "authority-failure", "released"])
def test_failed_launch_preserves_admission_error_and_original_custody(monkeypatch, case):
    admission_error = RuntimeError("observer admission failed")
    cleanup_error = custody.CustodyError("exact authority refused cleanup")
    events = []
    process = SimpleNamespace(pid=103, returncode=None)

    def original_wait(*, timeout):
        events.append(("original-wait", timeout))
        if case == "reap-timeout":
            raise subprocess.TimeoutExpired("retained-child", timeout)
        process.returncode = -signal.SIGKILL
        return process.returncode

    process.wait = original_wait
    broker = object.__new__(custody.RoleBoundCustodyBroker)
    broker.error = admission_error if case == "released" else None
    broker._post_exec_authority_validated = case != "unvalidated"
    broker._release_sent = case == "released"
    broker.registration = {"identity": {"pid": process.pid}}
    broker.cleanup_actor, broker.custody_generation = object(), 7

    def exact_signal(**kwargs):
        assert kwargs["actor"] is broker.cleanup_actor
        assert kwargs["generation"] == 7 and kwargs["expected_target"] == {"pid": 103}
        events.append(("exact-signal", kwargs["signum"]))
        if case == "authority-failure":
            raise cleanup_error

    broker.role_authority = SimpleNamespace(signal=exact_signal, reference=lambda: {"target": {"pid": 103}})
    owner = object.__new__(custody.ProcessCustody)
    owner.process, owner._launch_broker, owner._inventory_owner, owner._uncertain = None, broker, None, None
    owner._handles = {103: ("retained-birth", broker)}
    owner.prepare_launch = lambda argv, **_: (argv, {})
    owner.bind_launch = lambda child: setattr(owner, "process", child)
    owner.start_observer = lambda: (_ for _ in ()).throw(admission_error)
    owner.release_launch = lambda: pytest.fail("unadmitted target was released")
    monkeypatch.setattr(groups, "ProcessCustody", lambda: owner)
    monkeypatch.setattr(groups.subprocess, "Popen", lambda *_a, **_kw: process)
    monkeypatch.setattr(groups, "observe_tree", lambda _: {})
    monkeypatch.setattr(os, "kill", lambda *_: pytest.fail("PID fallback used"))
    monkeypatch.setattr(os, "killpg", lambda *_: pytest.fail("group fallback used"))

    with pytest.raises(RuntimeError) as caught:
        groups.popen_owned_process(["unused-executable"], _delegate_inventory=False)
    assert caught.value is admission_error
    assert caught.value.process is process and caught.value.custody is owner
    assert owner.process is process and process._astrid_custody is owner
    if case in {"suspended", "reap-timeout"}:
        assert events == [("exact-signal", signal.SIGKILL),
                          ("original-wait", custody._FAILED_LAUNCH_REAP_TIMEOUT_SECONDS)]
        if case == "suspended":
            assert process.returncode == -signal.SIGKILL and owner._uncertain is None
            return
        assert isinstance(caught.value.__cause__, subprocess.TimeoutExpired)
    elif case == "released":
        assert events == [("exact-signal", signal.SIGTERM)]
        assert process.returncode is None and owner._uncertain is None
        return
    elif case == "unvalidated":
        assert events == [] and process.returncode is None
        assert "no validated post-exec authority" in str(caught.value.__cause__)
    else:
        assert events == [("exact-signal", signal.SIGKILL)]
        assert caught.value.__cause__ is cleanup_error
    assert owner._uncertain is caught.value.__cause__
    assert process._astrid_tree_uncertain == str(caught.value.__cause__)


@pytest.mark.darwin_native
def test_failed_suspended_admission_kills_and_reaps_original_child(monkeypatch):
    admission_error = RuntimeError("injected observer admission failure")
    retained, signals, waits = [], [], []
    real_signal = custody.RoleBoundCustodyBroker.signal_failed_admission
    real_wait = subprocess.Popen.wait

    def fail_before_release(owner):
        retained.append(owner)
        assert owner._launch_broker._post_exec_authority_validated
        assert not owner._launch_broker._release_sent
        rows = custody._darwin_process_records(owner.process.pid)
        assert len(rows) == 1 and rows[0]["status"] == 4
        raise admission_error

    def exact_signal(broker, signum, *, expected_pid):
        signals.append((signum, expected_pid))
        return real_signal(broker, signum, expected_pid=expected_pid)

    def original_wait(process, timeout=None):
        result = real_wait(process, timeout=timeout)
        waits.append((process, timeout, result))
        return result

    monkeypatch.setattr(custody.ProcessCustody, "start_observer", fail_before_release)
    monkeypatch.setattr(custody.RoleBoundCustodyBroker, "signal_failed_admission", exact_signal)
    monkeypatch.setattr(subprocess.Popen, "wait", original_wait)
    try:
        with pytest.raises(RuntimeError) as caught:
            groups.popen_owned_process(["/bin/sleep", "30"], start_new_session=True,
                                       _delegate_inventory=False)
        owner = retained[0]
        process = owner.process
        assert caught.value is admission_error
        assert caught.value.process is process and caught.value.custody is owner
        assert not owner._launch_broker._release_sent
        assert signals == [(signal.SIGKILL, process.pid)]
        assert waits == [(process, custody._FAILED_LAUNCH_REAP_TIMEOUT_SECONDS, -signal.SIGKILL)]
        assert process.returncode == -signal.SIGKILL
        groups.verify_tree_absent(process)
    finally:
        # Also settle the exact retained test child when run against old code.
        # This never resumes it or uses a PID/group destructive fallback.
        if retained and retained[0].process.returncode is None:
            owner = retained[0]
            real_signal(owner._launch_broker, signal.SIGKILL, expected_pid=owner.process.pid)
            real_wait(owner.process, timeout=5)


@pytest.mark.parametrize("signal_failure", [False, True])
def test_sticky_cleanup_contains_only_frozen_retained_handles_and_keeps_first_error(tmp_path, monkeypatch, signal_failure):
    process, owner = _unadmitted_process(tmp_path, monkeypatch)
    owner.root.mkdir()
    owner._handles[150] = ("known-child", object())
    owner._uncertain = custody.CustodyError("first acquisition failure")
    process._astrid_tree_uncertain = "first acquisition failure"
    process._astrid_tree_members = {100: "root", 150: "known-child", 200: "UNADMITTED"}
    calls, waits = [], []
    process.wait = lambda *, timeout: waits.append(timeout) or -signal.SIGTERM
    def send(pid, birth, signum, *, cleanup_deadline):
        assert pid in {100, 150} and pid != 200
        calls.append((pid, birth, signum, cleanup_deadline))
        if signal_failure and pid == 150:
            raise custody.CustodyError("injected retained child signal failure")
    owner.signal = send
    def fail(original, **kwargs):
        assert original is process
        assert type(kwargs["deadline"]) is float
        raise owner._uncertain
    monkeypatch.setattr(groups, "observe_tree", fail)
    monkeypatch.setattr(groups, "_process_snapshot", lambda: pytest.fail("sticky cleanup performed a census"))
    with pytest.raises(custody.CustodyError, match="first acquisition failure"):
        groups.terminate_tree(process, grace_seconds=0)
    assert [(pid, sig) for pid, _, sig, _ in calls] == [
        (150, signal.SIGTERM), (100, signal.SIGTERM),
        (150, signal.SIGKILL), (100, signal.SIGKILL)]
    assert len({row[3] for row in calls}) == 1 and len(waits) == 2
    assert process._astrid_tree_uncertain == "first acquisition failure"
    assert owner._uncertain is not None and 200 not in owner._handles
    import json
    report = json.loads((owner.root / "retained-cleanup-containment.json").read_text())
    assert report["status"] == "UNCERTAIN_RETAINED_HANDLES_ONLY"
    assert report["first_error"] == "first acquisition failure"
    assert report["original_wait"] == -signal.SIGTERM
    assert [row["pid"] for row in report["retained_members"]] == [150, 100]
    assert any(row["stage"] == "signal" for row in report["errors"]) is signal_failure


def test_retained_cleanup_uses_one_deadline_and_never_starts_late_signal(tmp_path, monkeypatch):
    process, owner = _unadmitted_process(tmp_path, monkeypatch)
    owner.root.mkdir()
    owner._handles[150] = ("known-child", object())
    clock = [10.0]
    monkeypatch.setattr(groups.time, "monotonic", lambda: clock[0])
    calls, waits = [], []
    def send(pid, birth, signum, *, cleanup_deadline):
        assert cleanup_deadline == 11.0
        calls.append(pid)
        clock[0] = 11.0
        raise custody.CustodyError("bounded signal used remaining budget")
    owner.signal = send
    def wait(*, timeout):
        waits.append(timeout)
        raise subprocess.TimeoutExpired("owned-root", timeout)
    process.wait = wait
    groups.contain_retained_handles(process, cleanup_deadline=11.0, first_error="sticky")
    assert calls == [150] and waits == [0.0, 0.0]
    report = process._astrid_retained_containment
    assert any(row.get("pid") == 100 and row.get("error") == "deadline expired" for row in report["errors"])
    assert report["original_wait"] is None and report["status"].startswith("UNCERTAIN")



def test_tree_parent_deadline_includes_first_census_and_remains_sticky(tmp_path, monkeypatch):
    process, owner = _unadmitted_process(tmp_path, monkeypatch)
    now = [10.0]
    monkeypatch.setattr(custody.time, "monotonic", lambda: now[0])
    def snapshot():
        now[0] = 10.2
        return _snapshot()
    monkeypatch.setattr(groups, "_process_snapshot", snapshot)
    with pytest.raises(custody.CustodyError, match="deadline"):
        groups.observe_tree(process, deadline=10.1)
    assert owner._uncertain is not None and process._astrid_tree_uncertain
    assert 200 not in owner._handles


def test_renderer_expiry_during_census_finishes_custody_then_cleans_owned_tree(tmp_path, monkeypatch):
    from astrid.core.rendering import transport
    from astrid.core.rendering.errors import RendererTimeoutError

    process, owner = _unadmitted_process(tmp_path, monkeypatch)
    owner._handles[200] = ("detached-child", object())
    live = _snapshot()
    now = [10.0]
    events, signals, waits = [], [], []
    process.returncode = None
    process.poll = lambda: process.returncode
    process.wait = lambda **kwargs: waits.append(kwargs) or process.returncode

    def snapshot():
        events.append("census")
        now[0] = 10.6  # Past renderer expiry, inside the unchanged custody cap.
        return dict(live)

    def signal_member(pid, birth, signum, **kwargs):
        assert owner._uncertain is None
        assert events.count("census") >= 2  # Cleanup takes its own fresh census.
        assert owner._handles[pid][0] == birth
        signals.append((pid, signum))
        live.pop(pid)
        if pid == process.pid:
            process.returncode = -signum

    def communicate(*, timeout):
        assert process.returncode is not None  # No extra renderer wait after expiry.
        events.append("pipes-after-reap")
        return "", ""

    owner.signal = signal_member
    process.communicate = communicate
    monkeypatch.setattr(groups.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(groups, "_process_snapshot", snapshot)
    monkeypatch.setattr(transport, "popen_owned_process", lambda *args, **kwargs: process)
    request = tmp_path / "request.json"
    request.write_text("{}")

    with pytest.raises(RendererTimeoutError) as caught:
        transport.CommandTransport("test.renderer", termination_grace=0.05).run(
            "render", [sys.executable], request_path=request,
            result_path=tmp_path / "result.json", cwd=tmp_path, timeout=0.5,
        )

    assert caught.value.details["timeout_seconds"] == 0.5
    assert signals == [(200, signal.SIGTERM), (100, signal.SIGTERM)]
    assert live == {900: _snapshot()[900]}  # Host/sibling census member survives.
    assert waits and events[-1] == "pipes-after-reap"
    assert owner._uncertain is None and not getattr(process, "_astrid_tree_uncertain", None)


def test_termination_passes_existing_total_deadline_without_extension(monkeypatch):
    process = _process()
    monkeypatch.setattr(groups.time, "monotonic", lambda: 10.0)
    calls = []
    monkeypatch.setattr(groups, "_terminate_tree", lambda process, **kw: calls.append(kw))
    groups.terminate_tree(process, grace_seconds=1.0)
    assert calls == [{"grace_seconds": 1.0, "cleanup_deadline": 10.0 + 1.0 + groups._CLEANUP_FLOOR_SECONDS}]


@pytest.mark.parametrize("role", ["command", "renderer", "lookalike-command"])
@pytest.mark.parametrize("parent_deadline", [None, 10.1])
def test_reconciliation_local_cap_is_only_for_actual_ordinary_command(tmp_path, monkeypatch, role, parent_deadline):
    process, owner = _unadmitted_process(tmp_path, monkeypatch)
    if role == "lookalike-command":
        owner._launch_broker = SimpleNamespace(role="command")
        owner._handles[100] = ("root", owner._launch_broker)
    else:
        owner._launch_broker.role = role
    now = [10.0]
    monkeypatch.setattr(custody.time, "monotonic", lambda: now[0])
    def snapshot():
        now[0] = 12.0
        return {100: _snapshot()[100]}
    monkeypatch.setattr(groups, "_process_snapshot", snapshot)
    if role == "command" or parent_deadline is not None:
        with pytest.raises(custody.CustodyError, match="deadline"):
            groups.observe_tree(process, deadline=parent_deadline)
        assert owner._uncertain is not None and process._astrid_tree_uncertain
    else:
        assert groups.observe_tree(process) == {100: "root"}
        assert owner._uncertain is None
        assert not getattr(process, "_astrid_tree_uncertain", None)


@pytest.mark.parametrize("operation", ["observe_tree", "verify_tree_absent"])
def test_expired_parent_deadline_preserves_first_process_uncertainty(tmp_path, monkeypatch, operation):
    process, owner = _unadmitted_process(tmp_path, monkeypatch)
    first = custody.CustodyError("original capture failure")
    owner._uncertain = first
    process._astrid_tree_uncertain = str(first)
    monkeypatch.setattr(custody.time, "monotonic", lambda: 10.0)
    monkeypatch.setattr(groups, "_process_snapshot", lambda: pytest.fail("expired deadline queried census"))
    with pytest.raises(custody.CustodyError, match="deadline"):
        getattr(groups, operation)(process, deadline=9.0)
    assert owner._uncertain is first
    assert process._astrid_tree_uncertain == "original capture failure"


def _hold_custody_lock(owner):
    held, release = threading.Event(), threading.Event()
    def hold():
        with owner._lock:
            held.set(); release.wait(5)
    thread = threading.Thread(target=hold); thread.start(); held.wait(5)
    return release, thread


def test_observe_tree_lock_busy_is_retryable_and_not_sticky(tmp_path, monkeypatch):
    process, owner = _unadmitted_process(tmp_path, monkeypatch)
    initial = _snapshot()
    snapshots = iter((initial, {100: initial[100], 900: initial[900]}))
    monkeypatch.setattr(groups, "_process_snapshot", lambda: next(snapshots))
    monkeypatch.setattr(groups, "_candidate_reconciliation_deadline", lambda d: time.monotonic() + 0.05)
    release, thread = _hold_custody_lock(owner)
    try:
        with pytest.raises(custody.CustodyLockBusy):
            groups.observe_tree(process)
    finally:
        release.set(); thread.join()
    assert not getattr(process, "_astrid_tree_uncertain", None) and owner._uncertain is None
    monkeypatch.setattr(groups, "_candidate_reconciliation_deadline", lambda d: time.monotonic() + 5)
    assert groups.observe_tree(process)[100] == "root"


def test_final_verification_still_fails_closed_when_lock_stays_busy(tmp_path, monkeypatch):
    process, owner = _unadmitted_process(tmp_path, monkeypatch)
    monkeypatch.setattr(groups, "_process_snapshot", lambda: _snapshot())
    release, thread = _hold_custody_lock(owner)
    try:
        with pytest.raises(custody.CustodyError, match="acquiring custody lock"):
            groups.verify_tree_absent(process, deadline=time.monotonic() + 0.05)
    finally:
        release.set(); thread.join()
    assert process._astrid_tree_uncertain


linux_only = pytest.mark.linux_native


_BOUND_OWNERS: list = []


@pytest.fixture(autouse=True)
def _release_bound_sleeper_pidfds():
    """Each test kills and reaps its sleeper; release the pidfds its custody retained."""
    yield
    while _BOUND_OWNERS:
        _BOUND_OWNERS.pop().release_pidfds()


def _bound_sleeper():
    """A bound, once-observed child under real custody, as popen_owned_process leaves it."""
    owner = custody.ProcessCustody()
    _BOUND_OWNERS.append(owner)
    argv, _env = owner.prepare_launch([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
    process = subprocess.Popen(argv, start_new_session=True)
    process._astrid_custody = owner
    process._astrid_process_group_id = process.pid
    owner.bind_launch(process)
    process._astrid_process_birth = owner._handles[process.pid][0]
    groups.observe_tree(process)
    return owner, process


def _exit_without_reaping(process):
    process.kill()
    os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOWAIT)


@linux_only
def test_census_witnesses_an_unreaped_zombie_without_listing_it_live():
    owner, process = _bound_sleeper()
    try:
        birth = owner._handles[process.pid][0]
        _exit_without_reaping(process)
        census = groups._process_snapshot()
        assert process.pid not in census
        assert census.exited[process.pid] == birth
    finally:
        process.kill()
        process.wait()


@linux_only
def test_first_observer_census_accepts_a_launch_with_zombie_witness():
    owner, process = _bound_sleeper()
    try:
        _exit_without_reaping(process)
        owner.start_observer()  # exited after ancestry capture; same-census zombie of the bound birth
        assert owner._uncertain is None
    finally:
        process.wait()


@linux_only
def test_first_observer_census_still_fails_closed_without_witness():
    owner, process = _bound_sleeper()
    process.kill()
    process.wait()  # reaped: absent, and no zombie row proves the exit
    with pytest.raises(custody.CustodyError, match="observer census is unresolved") as raised:
        owner.start_observer()
    assert "omitted the suspended launch" in str(raised.value.__cause__)


@linux_only
def test_zombie_witness_requires_the_exact_bound_incarnation():
    owner, process = _bound_sleeper()
    try:
        _exit_without_reaping(process)
        groups._observe_tree(process)
        birth = owner._handles[process.pid][0]
        assert groups.launch_exit_witnessed(process, birth)
        assert not groups.launch_exit_witnessed(process, birth + "0")
        assert not groups.launch_exit_witnessed(process, "")
    finally:
        process.wait()


def test_instantly_exiting_launches_are_a_known_outcome_not_a_custody_error():
    # The child can exit (a zombie we hold, not yet reaped) before custody records
    # its birth identity; that is an ordinary outcome, never a registration failure.
    for _ in range(40):
        process = groups.popen_owned_process(["/bin/sh", "-c", "exit 97"], stdout=subprocess.DEVNULL)
        try:
            assert process.wait(timeout=10) == 97
            groups.terminate_group(process)
        finally:
            groups.release_group(process)


def test_held_child_birth_identity_names_an_exited_unreaped_child(monkeypatch):
    monkeypatch.setattr(groups.sys, "platform", "linux")
    monkeypatch.setattr(groups, "process_birth_identity", lambda *_: "")
    monkeypatch.setattr(groups, "_zombie_birth_identity", lambda *_: "boot:123")
    assert groups.held_child_birth_identity(7) == "boot:123"
    monkeypatch.setattr(groups, "_zombie_birth_identity", lambda *_: "")
    assert groups.held_child_birth_identity(7) == ""
