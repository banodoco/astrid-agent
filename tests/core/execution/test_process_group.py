from __future__ import annotations

import signal
import subprocess
import os
import sys
import select
from pathlib import Path
from types import SimpleNamespace
import threading

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


@pytest.mark.skipif(sys.platform != "darwin", reason="native Darwin exact-token proof")
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


@pytest.mark.skipif(sys.platform != "darwin", reason="Darwin suspended SETEXEC admission proof")
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


@pytest.mark.skipif(sys.platform != "darwin", reason="real Darwin native adapter graph proof")
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
    with pytest.raises(custody.CustodyError, match="terminal candidate member identity changed"):
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
    owner.begin_launch_spawn = lambda: None  # Synthetic producer; no durable launch proof.
    owner.settle_launch = lambda: events.append(("settle-original", process.pid, process.returncode))
    owner.bind_launch = lambda child: setattr(owner, "process", child)
    owner.start_observer = lambda: (_ for _ in ()).throw(admission_error)
    owner.release_launch = lambda: pytest.fail("unadmitted target was released")
    monkeypatch.setattr(groups, "ProcessCustody", lambda: owner)
    monkeypatch.setattr(groups.subprocess, "Popen", lambda *_a, **_kw: process)
    monkeypatch.setattr(groups, "RetainedCustodyPopen", lambda *_a, **_kw: process)
    monkeypatch.setattr(groups, "observe_tree", lambda _: {})
    monkeypatch.setattr(os, "kill", lambda *_: pytest.fail("PID fallback used"))
    monkeypatch.setattr(os, "killpg", lambda *_: pytest.fail("group fallback used"))

    with pytest.raises(RuntimeError) as caught:
        groups.popen_owned_process(["unused-executable"], _delegate_inventory=False)
    assert caught.value is admission_error
    assert caught.value.process is process and caught.value.custody is owner
    assert owner.process is process and process._astrid_custody is owner
    if case in {"suspended", "reap-timeout"}:
        expected = [("exact-signal", signal.SIGKILL),
                    ("original-wait", custody._FAILED_LAUNCH_REAP_TIMEOUT_SECONDS)]
        if case == "suspended":
            expected.append(("settle-original", process.pid, -signal.SIGKILL))
        assert events == expected
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


@pytest.mark.skipif(sys.platform != "darwin", reason="Darwin real suspended admission failure")
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
        assert waits[0] == (process, custody._FAILED_LAUNCH_REAP_TIMEOUT_SECONDS, -signal.SIGKILL)
        assert waits[1:] and all(item == (process, 0, -signal.SIGKILL) for item in waits[1:])
        assert process.returncode == -signal.SIGKILL
        assert process._custody_wait_outcome['returncode'] == -signal.SIGKILL
        assert owner._launch_broker.role_authority._read()['state'] == 'settled'
        groups.verify_tree_absent(process)
    finally:
        # Also settle the exact retained test child when run against old code.
        # This never resumes it or uses a PID/group destructive fallback.
        if retained and retained[0].process.returncode is None:
            owner = retained[0]
            real_signal(owner._launch_broker, signal.SIGKILL, expected_pid=owner.process.pid)
            real_wait(owner.process, timeout=5)
