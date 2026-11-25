"""RN-15: Linux census parity without spawning an observer process."""

import json
import subprocess
import sys

import pytest

from astrid.core.execution import process_group as groups

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux /proc census")


def _proc_tree(tmp_path, monkeypatch):
    boot = tmp_path / "sys/kernel/random/boot_id"
    boot.parent.mkdir(parents=True)
    boot.write_text("boot\n")
    monkeypatch.setattr(groups, "_PROC_ROOT", tmp_path)
    return tmp_path


def _row(root, pid, *, ppid=0, pgid=100, session=100, tick=500, state="S", comm="name (with) ) (", uid=123):
    path = root / str(pid)
    path.mkdir(exist_ok=True)
    fields = [state, str(ppid), str(pgid), str(session), *(["0"] * 15), str(tick)]
    (path / "stat").write_bytes(f"{pid} ({comm}) {' '.join(fields)}\n".encode())
    (path / "status").write_text(f"Name:\t{comm}\nUid:\t456\t{uid}\t456\t456\n")
    return path


@pytest.mark.parametrize("comm", ["space name", "nested (x) ) (", "newline\n) name", "non-ascii café"])
def test_proc_record_keeps_topology_birth_and_effective_uid_with_tricky_comm(tmp_path, monkeypatch, comm):
    root = _proc_tree(tmp_path, monkeypatch)
    _row(root, 100, comm=comm, pgid=81, session=71)
    monkeypatch.setattr(groups.subprocess, "run", lambda *_a, **_k: pytest.fail("census spawned ps"))

    snapshot = groups._process_snapshot()
    assert snapshot == {100: groups._ProcessInfo(100, 0, 81, "boot:500", uid=123, session=71)}
    assert groups._census_evidence(snapshot)[100] == {
        "pid": 100, "parent_pid": 0, "pgid": 81, "birth_id": "boot:500", "uid": 123, "session": 71,
    }


def test_proc_census_keeps_only_exact_zombie_witnesses(tmp_path, monkeypatch):
    root = _proc_tree(tmp_path, monkeypatch)
    _row(root, 100)
    _row(root, 200, ppid=100, state="Z", tick=501)
    _row(root, 300, ppid=100, state="X", tick=502)
    (root / "400").mkdir()  # vanished after enumeration: no stat, no witness

    snapshot = groups._process_snapshot()
    assert set(snapshot) == {100}
    assert snapshot.exited == {200: "boot:501"}
    assert snapshot.failure is None


def test_proc_census_refreshes_a_child_reparented_during_enumeration(tmp_path, monkeypatch):
    root = _proc_tree(tmp_path, monkeypatch)
    _row(root, 100)
    _row(root, 300, ppid=200)
    read = groups._linux_process_record
    calls = 0

    def reparent(path, boot):
        nonlocal calls
        info = read(path, boot)
        if path.name == "300":
            calls += 1
            if calls == 1:
                _row(root, 300, ppid=100)
        return info

    monkeypatch.setattr(groups, "_linux_process_record", reparent)
    assert groups._process_snapshot()[300].ppid == 100
    assert calls == 2


def test_proc_census_never_splices_reused_child_birth_and_parent(tmp_path, monkeypatch):
    root = _proc_tree(tmp_path, monkeypatch)
    _row(root, 100)
    _row(root, 300, ppid=200)
    read = groups._linux_process_record

    def reuse(path, boot):
        info = read(path, boot)
        if path.name == "300":
            _row(root, 300, ppid=100, tick=900)
        return info

    monkeypatch.setattr(groups, "_linux_process_record", reuse)
    assert 300 not in groups._process_snapshot()


@pytest.mark.parametrize("failure", ["stat", "uid", "boot", "permission"])
def test_proc_census_read_errors_are_unavailable_not_absence(tmp_path, monkeypatch, failure):
    root = _proc_tree(tmp_path, monkeypatch)
    path = _row(root, 100)
    if failure == "stat":
        (path / "stat").write_text("malformed")
    elif failure == "uid":
        (path / "status").write_text("Uid: unknown")
    elif failure == "boot":
        (root / "sys/kernel/random/boot_id").write_text("")
    else:
        def denied(*_):
            raise PermissionError(13, "process census denied")
        monkeypatch.setattr(groups, "_linux_process_record", denied)
    snapshot = groups._process_snapshot()
    assert not snapshot
    assert "/proc census unavailable" in snapshot.failure
    assert not snapshot.exited


def test_live_proc_tree_matches_ps_fields_and_legacy_births_without_spawning(monkeypatch):
    child_code = (
        "import ctypes,os,time; "
        "ctypes.CDLL(None).prctl(15,b'rn15) child (',0,0,0); "
        "print(os.getpid(),flush=True); time.sleep(30)"
    )
    root_code = (
        "import ctypes,json,os,subprocess,sys; "
        "ctypes.CDLL(None).prctl(15,b'rn15 (root) )',0,0,0); "
        f"child=subprocess.Popen([sys.executable,'-c',{child_code!r}],stdout=subprocess.PIPE,text=True); "
        "pid=int(child.stdout.readline()); print(json.dumps([os.getpid(),pid]),flush=True); "
        "sys.stdin.readline(); child.terminate(); child.wait()"
    )
    root = subprocess.Popen([sys.executable, "-c", root_code], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, text=True, start_new_session=True)
    try:
        pids = json.loads(root.stdout.readline())
        selectors = ",".join(map(str, pids))
        fields = subprocess.run(["ps", "-p", selectors, "-o", "pid=,ppid=,pgid=,sid=,euid="],
                                capture_output=True, text=True, check=True)
        legacy = subprocess.run(["ps", "-p", selectors, "-o", "pid=,ppid=,pgid=,lstart="],
                                capture_output=True, text=True, check=True)
        old = groups._parse_census(legacy.stdout)
        monkeypatch.setattr(groups.subprocess, "run", lambda *_a, **_k: pytest.fail("census spawned ps"))
        snapshot = groups._process_snapshot()
        assert snapshot.failure is None
        rows = [tuple(map(int, row.split())) for row in fields.stdout.splitlines()]
        assert {row[0] for row in rows} == set(pids)
        for pid, ppid, pgid, session, uid in rows:
            info = snapshot[pid]
            assert (info.pid, info.ppid, info.pgid, info.session, info.uid) == (pid, ppid, pgid, session, uid)
            assert info.birth == old[pid].birth == groups.process_birth_identity(pid)
        assert snapshot[pids[1]].ppid == pids[0]
        assert snapshot[pids[0]].pgid == snapshot[pids[0]].session == pids[0]
    finally:
        root.communicate("stop\n", timeout=5)
        assert root.returncode == 0
