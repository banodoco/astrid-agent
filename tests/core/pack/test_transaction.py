"""Atomic dependency group installs over real Git repositories and builders."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from astrid.core.pack.identity import pack_digest, read_pin
from astrid.core.pack.install import PackInstallError, install_pack
from astrid.core.pack.transaction import (
    install_group,
    read_group,
    read_retired_group,
    retry_refresh,
)


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], check=True, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout.strip()


def manifest(pack_id: str, dependencies: tuple[str, ...] = ()) -> str:
    packs = "" if not dependencies else "  packs: [" + ", ".join(dependencies) + "]\n"
    dependency_field = "" if not dependencies else "dependencies:\n" + packs
    return (f"schema_version: 3\nid: {pack_id}\nname: {pack_id.title()}\nversion: 1.0.0\n"
            'pack_api: "1.0"\n' + dependency_field +
            "documentation: {kind: none, reason: Transaction fixture.}\n")


class Repository:
    """A local published Git source containing two same-commit packs."""

    def __init__(self, root: Path):
        self.work = root / "work"
        self.bare = root / "source.git"
        self.work.mkdir(parents=True)
        git(self.work, "init", "-b", "main")
        git(self.work, "config", "user.name", "Transaction Test")
        git(self.work, "config", "user.email", "transaction@example.invalid")
        self.write_pack("shared", "shared-v1\n")
        self.write_pack("alpha", "alpha-v1\n", dependencies=("shared",))
        self.commit = self.publish()
        git(root, "clone", "--bare", str(self.work), str(self.bare))

    def write_pack(self, pack_id: str, payload: str, *, dependencies: tuple[str, ...] = ()) -> None:
        pack = self.work / "packs" / pack_id
        pack.mkdir(parents=True, exist_ok=True)
        (pack / "pack.yaml").write_text(manifest(pack_id, dependencies), encoding="utf-8")
        (pack / "payload.txt").write_text(payload, encoding="utf-8")

    def publish(self) -> str:
        git(self.work, "add", "-A")
        git(self.work, "commit", "-m", "fixture")
        self.commit = git(self.work, "rev-parse", "HEAD")
        if self.bare.exists():
            git(self.work, "push", str(self.bare), "HEAD:refs/heads/main")
        return self.commit

    def next_revision(self) -> str:
        self.write_pack("shared", "shared-v2\n")
        self.write_pack("alpha", "alpha-v2\n", dependencies=("shared",))
        return self.publish()


@pytest.fixture
def repo(tmp_path: Path) -> Repository:
    return Repository(tmp_path / "repository")


@pytest.fixture
def packs_root(tmp_path: Path) -> Path:
    return tmp_path / "Astrid" / "packs"


GROUP = {"alpha": "packs/alpha", "shared": "packs/shared"}


def install(repo: Repository, root: Path, *, packs=GROUP, revision=None, **kwargs):
    return install_group(source=repo.bare, revision=revision or repo.commit, packs=packs,
                         packs_root=root, python_packages={}, **kwargs)


def group_bytes(root: Path) -> dict[str, bytes]:
    snapshot = read_group(root)
    return {pack_id: (path / "payload.txt").read_bytes() for pack_id, path in snapshot.paths.items()}


def assert_group_pins(root: Path, expected_commit: str, expected_payloads: dict[str, bytes]) -> None:
    snapshot = read_group(root)
    assert set(snapshot.paths) == set(expected_payloads)
    for pack_id, path in snapshot.paths.items():
        pin = read_pin(path / "PIN")
        assert pin["commit"] == expected_commit
        assert pin["digest"] == pack_digest(path)
        assert (path / "payload.txt").read_bytes() == expected_payloads[pack_id]
    assert snapshot.transaction_id
    assert snapshot.revision >= 1


def accepted_refresh(**kwargs):
    return {"version": 1, "accepted": True, "revision": kwargs["committed_revision"], "reason": "ok"}


def kill_child_at_boundary(repo: Repository, root: Path, tmp_path: Path,
                           revision: str, phase: str) -> None:
    """Run one transaction in a minimal child and SIGKILL it at a named seam."""
    source_root = Path(__file__).resolve().parents[3]
    import runtime_protocol
    runtime_root = Path(runtime_protocol.__file__).resolve().parents[1]
    child_env = {
        "PATH": os.defpath,
        "HOME": str(tmp_path / "child-home"),
        "TMPDIR": str(tmp_path),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": os.pathsep.join((str(source_root), str(runtime_root))),
        "ASTRID_RUNTIME_CHECKOUT": str(runtime_root),
    }
    Path(child_env["HOME"]).mkdir()
    script = tmp_path / "crash_at_boundary.py"
    script.write_text(textwrap.dedent("""
        import os, signal, sys
        from astrid.core.pack.transaction import install_group
        source, revision, root, phase = sys.argv[1:]
        def boundary(current):
            if current == phase:
                os.kill(os.getpid(), signal.SIGKILL)
        install_group(source=source, revision=revision,
            packs={"alpha": "packs/alpha", "shared": "packs/shared"},
            packs_root=root, python_packages={}, boundary=boundary)
        raise AssertionError("requested boundary was not reached")
    """), encoding="utf-8")
    child = subprocess.run([sys.executable, str(script), str(repo.bare), revision,
                            str(root), phase], env=child_env,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=45)
    assert child.returncode == -signal.SIGKILL, child.stderr.decode("utf-8", "replace")


@pytest.mark.parametrize("phase", [
    "preparing", "validated", "built:alpha", "built:shared", "built", "prepared",
    "before_swap", "swapped", "durable", "before_refresh", "refreshed",
])
def test_process_death_at_every_transaction_boundary_exposes_complete_old_or_new_group(
        repo: Repository, packs_root: Path, tmp_path: Path, phase: str) -> None:
    initial = install(repo, packs_root, refresh=accepted_refresh)
    old_commit = repo.commit
    old_bytes = group_bytes(packs_root)
    new_commit = repo.next_revision()
    # The child receives only its selected import/runtime environment and its
    # private HOME/TMPDIR. SIGKILL bypasses Python cleanup at the chosen seam.
    kill_child_at_boundary(repo, packs_root, tmp_path, new_commit, phase)

    visible = read_group(packs_root)
    if phase in {"swapped", "durable", "before_refresh", "refreshed"}:
        assert_group_pins(packs_root, new_commit,
                          {"alpha": b"alpha-v2\n", "shared": b"shared-v2\n"})
        assert visible.refresh_pending
        assert visible.transaction_id != initial.transaction_id
    else:
        assert_group_pins(packs_root, old_commit,
                          {"alpha": b"alpha-v1\n", "shared": b"shared-v1\n"})
        assert group_bytes(packs_root) == old_bytes
        assert visible.refresh_pending is False
        assert visible.transaction_id == initial.transaction_id


@pytest.mark.parametrize("phase", [
    "preparing", "validated", "built:alpha", "built:shared", "built", "prepared",
    "before_swap", "swapped", "durable", "before_refresh", "refreshed",
])
def test_process_death_during_first_install_exposes_absence_or_complete_new_group(
        repo: Repository, packs_root: Path, tmp_path: Path, phase: str) -> None:
    kill_child_at_boundary(repo, packs_root, tmp_path, repo.commit, phase)
    snapshot = read_group(packs_root)
    after_swap = phase in {"swapped", "durable", "before_refresh", "refreshed"}
    if after_swap:
        assert_group_pins(packs_root, repo.commit,
                          {"alpha": b"alpha-v1\n", "shared": b"shared-v1\n"})
        assert snapshot.refresh_pending
    else:
        assert not snapshot.paths
        assert snapshot.revision == 0
        assert snapshot.transaction_id is None
        assert not snapshot.refresh_pending


def test_old_generation_reader_keeps_previous_pin_and_bytes_after_next_commit(
        repo: Repository, packs_root: Path) -> None:
    first = install(repo, packs_root, refresh=accepted_refresh)
    old_pin_bytes = {pack_id: (path / "PIN").read_bytes()
                     for pack_id, path in read_group(packs_root).paths.items()}
    old_payloads = group_bytes(packs_root)
    new_commit = repo.next_revision()
    second = install(repo, packs_root, revision=new_commit, refresh=accepted_refresh)

    retired = read_retired_group(packs_root=packs_root, transaction_id=second.transaction_id)
    assert retired.transaction_id == first.transaction_id
    assert retired.revision == first.revision
    assert retired.paths == {pack_id: second.retired_root / pack_id for pack_id in old_payloads}
    assert {pack_id: (path / "PIN").read_bytes() for pack_id, path in retired.paths.items()} == old_pin_bytes
    assert {pack_id: (path / "payload.txt").read_bytes() for pack_id, path in retired.paths.items()} == old_payloads
    assert_group_pins(packs_root, new_commit,
                      {"alpha": b"alpha-v2\n", "shared": b"shared-v2\n"})


def test_interrupt_after_kernel_exchange_keeps_new_root_and_retired_old_generation(
        repo: Repository, packs_root: Path, tmp_path: Path) -> None:
    initial = install(repo, packs_root, refresh=accepted_refresh)
    old_pin_bytes = {pack_id: (path / "PIN").read_bytes()
                     for pack_id, path in read_group(packs_root).paths.items()}
    old_payloads = group_bytes(packs_root)
    old_commit = repo.commit
    new_commit = repo.next_revision()
    source_root = Path(__file__).resolve().parents[3]
    import runtime_protocol
    runtime_root = Path(runtime_protocol.__file__).resolve().parents[1]
    child_env = {
        "PATH": os.defpath,
        "HOME": str(tmp_path / "child-home"),
        "TMPDIR": str(tmp_path),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": os.pathsep.join((str(source_root), str(runtime_root))),
        "ASTRID_RUNTIME_CHECKOUT": str(runtime_root),
    }
    Path(child_env["HOME"]).mkdir()
    script = tmp_path / "interrupt_after_exchange.py"
    script.write_text(textwrap.dedent("""
        import json, sys
        from pathlib import Path
        from astrid.core.pack import transaction
        source, revision, root_value, expected_old = sys.argv[1:]
        root = Path(root_value)
        transaction_file = Path(transaction.__file__).resolve()
        lines = transaction_file.read_text(encoding='utf-8').splitlines()
        target_line = next(index for index, line in enumerate(lines, 1)
                           if line.lstrip().startswith('published = True'))
        interrupted = []
        def trace(frame, event, arg):
            if (event == 'line' and Path(frame.f_code.co_filename).resolve() == transaction_file
                    and frame.f_lineno == target_line):
                interrupted.append(frame.f_lineno)
                sys.settrace(None)
                raise KeyboardInterrupt('after atomic exchange')
            return trace
        sys.settrace(trace)
        try:
            try:
                transaction.install_group(source=source, revision=revision,
                    packs={'alpha': 'packs/alpha', 'shared': 'packs/shared'},
                    packs_root=root, python_packages={})
            except KeyboardInterrupt:
                pass
        finally:
            sys.settrace(None)
        assert interrupted == [target_line]
        current = transaction.read_group(root)
        assert current.revision == 2 and current.transaction_id
        assert current.refresh_pending
        retired = transaction.read_retired_group(
            packs_root=root, transaction_id=current.transaction_id)
        assert retired.transaction_id == expected_old
        assert retired.revision == 1
        print(json.dumps({'current': current.transaction_id,
                          'retired': retired.transaction_id}), flush=True)
    """), encoding="utf-8")
    child = subprocess.run([sys.executable, str(script), str(repo.bare), new_commit,
                            str(packs_root), initial.transaction_id], env=child_env, text=True,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=45)
    assert child.returncode == 0, child.stderr
    result = json.loads(child.stdout)
    current = read_group(packs_root)
    retired = read_retired_group(packs_root=packs_root, transaction_id=result["current"])
    assert current.transaction_id == result["current"]
    assert retired.transaction_id == result["retired"] == initial.transaction_id
    assert_group_pins(packs_root, new_commit,
                      {"alpha": b"alpha-v2\n", "shared": b"shared-v2\n"})
    assert {pack_id: (path / "payload.txt").read_bytes()
            for pack_id, path in retired.paths.items()} == old_payloads
    assert {pack_id: (path / "PIN").read_bytes()
            for pack_id, path in retired.paths.items()} == old_pin_bytes
    assert old_commit != new_commit


def test_real_whole_root_swap_permission_failure_preserves_old_group(
        repo: Repository, packs_root: Path, tmp_path: Path) -> None:
    install(repo, packs_root, refresh=accepted_refresh)
    before = group_bytes(packs_root)
    before_transaction = read_group(packs_root).transaction_id
    new_commit = repo.next_revision()
    source_root = Path(__file__).resolve().parents[3]
    import runtime_protocol
    runtime_root = Path(runtime_protocol.__file__).resolve().parents[1]
    child_env = {
        "PATH": os.defpath,
        "HOME": str(tmp_path / "child-home"),
        "TMPDIR": str(tmp_path),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": os.pathsep.join((str(source_root), str(runtime_root))),
        "ASTRID_RUNTIME_CHECKOUT": str(runtime_root),
    }
    Path(child_env["HOME"]).mkdir()
    script = tmp_path / "deny_group_swap.py"
    script.write_text(textwrap.dedent("""
        import ctypes, json, os, sys
        from pathlib import Path
        from astrid.core.pack.install import PackInstallError
        from astrid.core.pack.transaction import install_group
        source, revision, root_value = sys.argv[1:]
        root = Path(root_value)
        if os.getuid() == 0:
            class Header(ctypes.Structure):
                _fields_ = [('version', ctypes.c_uint32), ('pid', ctypes.c_int)]
            class Data(ctypes.Structure):
                _fields_ = [('effective', ctypes.c_uint32), ('permitted', ctypes.c_uint32), ('inheritable', ctypes.c_uint32)]
            libc = ctypes.CDLL(None, use_errno=True)
            header, caps = Header(0x20080522, 0), (Data * 2)()
            assert libc.capget(ctypes.byref(header), caps) == 0
            caps[0].effective &= ~6
            caps[0].permitted &= ~6
            caps[0].inheritable &= ~6
            assert libc.capset(ctypes.byref(header), caps) == 0
        def deny_swap(phase):
            if phase == 'before_swap':
                root.parent.chmod(0o500)
        try:
            try:
                install_group(source=source, revision=revision,
                    packs={'alpha': 'packs/alpha', 'shared': 'packs/shared'},
                    packs_root=root, python_packages={}, boundary=deny_swap)
            except PackInstallError as error:
                print(json.dumps({'code': error.code}), flush=True)
            else:
                raise AssertionError('whole-root publication should have failed')
        finally:
            root.parent.chmod(0o700)
    """), encoding="utf-8")
    child = subprocess.run([sys.executable, str(script), str(repo.bare), new_commit,
                            str(packs_root)], env=child_env, text=True,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=45)
    assert child.returncode == 0, child.stderr
    assert json.loads(child.stdout) == {"code": "swap_failed"}
    assert group_bytes(packs_root) == before
    assert read_group(packs_root).transaction_id == before_transaction


def test_single_pack_primitive_refuses_to_mutate_a_group_transaction_root(
        repo: Repository, packs_root: Path) -> None:
    install(repo, packs_root, refresh=accepted_refresh)
    before = group_bytes(packs_root)
    with pytest.raises(PackInstallError) as failure:
        install_pack(source=repo.bare, revision=repo.commit, subpath="packs/alpha",
                     pack_id="alpha", packs_root=packs_root, python_packages={})
    assert failure.value.code == "group_transaction_required"
    assert group_bytes(packs_root) == before


def test_scan_waits_for_stable_group_lock_and_reads_only_new_commit(
        repo: Repository, packs_root: Path, tmp_path: Path) -> None:
    install(repo, packs_root, refresh=accepted_refresh)
    new_commit = repo.next_revision()
    source_root = Path(__file__).resolve().parents[3]
    import runtime_protocol
    runtime_root = Path(runtime_protocol.__file__).resolve().parents[1]
    child_env = {
        "PATH": os.defpath,
        "HOME": str(tmp_path / "child-home"),
        "TMPDIR": str(tmp_path),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": os.pathsep.join((str(source_root), str(runtime_root))),
        "ASTRID_RUNTIME_CHECKOUT": str(runtime_root),
    }
    Path(child_env["HOME"]).mkdir()
    ready = tmp_path / "at-before-swap"
    gate = tmp_path / "allow-swap"
    script = tmp_path / "pause_before_swap.py"
    script.write_text(textwrap.dedent("""
        import sys, time
        from pathlib import Path
        from astrid.core.pack.transaction import install_group
        source, revision, root, ready, gate = sys.argv[1:]
        ready, gate = Path(ready), Path(gate)
        def boundary(phase):
            if phase == 'before_swap':
                ready.touch()
                deadline = time.monotonic() + 20
                while not gate.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                assert gate.exists(), 'test did not release paused writer'
        install_group(source=source, revision=revision,
            packs={'alpha': 'packs/alpha', 'shared': 'packs/shared'},
            packs_root=root, python_packages={}, boundary=boundary)
    """), encoding="utf-8")
    child = subprocess.Popen([sys.executable, str(script), str(repo.bare), new_commit,
                              str(packs_root), str(ready), str(gate)], env=child_env,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 20
        while not ready.exists() and child.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists(), "writer did not reach before_swap"
        from astrid.core.pack.loader import scan_packs

        with ThreadPoolExecutor(max_workers=1) as pool:
            reader = pool.submit(scan_packs, packs_root)
            time.sleep(0.2)
            assert not reader.done(), "scan passed the held group lock before publication"
            gate.touch()
            stdout, stderr = child.communicate(timeout=30)
            assert child.returncode == 0, stderr.decode("utf-8", "replace")
            scan = reader.result(timeout=20)
        assert {pack.id for pack in scan.packs} == {"alpha", "shared"}
        assert {(pack.id, (pack.root / "payload.txt").read_bytes()) for pack in scan.packs} == {
            ("alpha", b"alpha-v2\n"), ("shared", b"shared-v2\n")}
        assert_group_pins(packs_root, new_commit,
                          {"alpha": b"alpha-v2\n", "shared": b"shared-v2\n"})
    finally:
        if child.poll() is None:
            child.kill()
            child.communicate(timeout=10)


def test_refresh_happens_after_durable_commit_and_failed_ack_is_retryable(
        repo: Repository, packs_root: Path) -> None:
    phases: list[str] = []
    seen: list[dict[str, object]] = []

    def boundary(phase: str) -> None:
        phases.append(phase)

    def fail_refresh(**kwargs):
        seen.append(kwargs)
        assert "durable" in phases
        assert phases[-1] == "before_refresh"
        raise OSError("refresh unavailable")

    result = install(repo, packs_root, refresh=fail_refresh, boundary=boundary)
    assert result.refresh_pending
    snapshot = read_group(packs_root)
    assert snapshot.refresh_pending
    assert seen == [{"transaction_id": result.transaction_id,
                     "committed_revision": result.revision,
                     "changed_ids": ("alpha", "shared")}]

    retried: list[dict[str, object]] = []

    def acknowledge(**kwargs):
        retried.append(kwargs)
        return {"version": 1, "accepted": True, "revision": kwargs["committed_revision"], "reason": "ok"}

    assert retry_refresh(packs_root=packs_root, refresh=acknowledge) is False
    assert retried == seen
    assert read_group(packs_root).refresh_pending is False


@pytest.mark.parametrize("ack_kind", ["stale", "malformed"])
def test_stale_or_malformed_refresh_ack_keeps_committed_retry_pending(
        repo: Repository, packs_root: Path, ack_kind: str) -> None:
    def bad_ack(**kwargs):
        if ack_kind == "stale":
            return {"version": 1, "accepted": True,
                    "revision": kwargs["committed_revision"] - 1, "reason": "stale"}
        return {"version": 1, "accepted": True, "reason": "missing revision"}

    result = install(repo, packs_root, refresh=bad_ack)
    assert result.refresh_pending
    assert read_group(packs_root).refresh_pending
    assert retry_refresh(packs_root=packs_root, refresh=accepted_refresh) is False
    assert not read_group(packs_root).refresh_pending


def test_refresh_callback_can_read_the_committed_group_while_writer_owns_lock(
        repo: Repository, packs_root: Path) -> None:
    observed = []

    def refresh(**kwargs):
        current = read_group(packs_root)
        observed.append((current.transaction_id, current.revision, current.refresh_pending))
        assert current.transaction_id == kwargs["transaction_id"]
        assert current.revision == kwargs["committed_revision"]
        assert current.refresh_pending
        return accepted_refresh(**kwargs)

    result = install(repo, packs_root, refresh=refresh)
    assert observed == [(result.transaction_id, result.revision, True)]
    assert not read_group(packs_root).refresh_pending


def test_new_refresh_ack_covers_all_changes_since_previous_ack(
        repo: Repository, packs_root: Path) -> None:
    first = install(repo, packs_root)
    assert first.refresh_pending
    repo.write_pack("extra", "extra-v1\n")
    new_commit = repo.publish()
    acknowledgments = []

    def refresh(**kwargs):
        acknowledgments.append(kwargs)
        return accepted_refresh(**kwargs)

    second = install(repo, packs_root, packs={"extra": "packs/extra"},
                     revision=new_commit, refresh=refresh)
    expected = ("alpha", "extra", "shared")
    assert second.changed_ids == expected
    assert acknowledgments == [{"transaction_id": second.transaction_id,
                                "committed_revision": second.revision,
                                "changed_ids": expected}]
    assert not read_group(packs_root).refresh_pending

    # Replaying recovery after the acknowledgment has no work to repeat.
    assert retry_refresh(packs_root=packs_root, refresh=refresh) is False
    assert len(acknowledgments) == 1


def test_durable_sync_failure_keeps_new_group_and_refresh_can_recover(
        repo: Repository, packs_root: Path, tmp_path: Path) -> None:
    install(repo, packs_root, refresh=accepted_refresh)
    old_commit = repo.commit
    new_commit = repo.next_revision()
    source_root = Path(__file__).resolve().parents[3]
    import runtime_protocol
    runtime_root = Path(runtime_protocol.__file__).resolve().parents[1]
    child_env = {
        "PATH": os.defpath,
        "HOME": str(tmp_path / "child-home"),
        "TMPDIR": str(tmp_path),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": os.pathsep.join((str(source_root), str(runtime_root))),
        "ASTRID_RUNTIME_CHECKOUT": str(runtime_root),
    }
    Path(child_env["HOME"]).mkdir()
    script = tmp_path / "fail_durable_sync.py"
    script.write_text(textwrap.dedent("""
        import ctypes, json, os, sys
        from pathlib import Path
        from astrid.core.pack.transaction import GroupCommitError, install_group
        source, revision, root_value = sys.argv[1:]
        root = Path(root_value)
        retired = root.parent / '.astrid-retired'
        if os.getuid() == 0:
            class Header(ctypes.Structure):
                _fields_ = [('version', ctypes.c_uint32), ('pid', ctypes.c_int)]
            class Data(ctypes.Structure):
                _fields_ = [('effective', ctypes.c_uint32), ('permitted', ctypes.c_uint32), ('inheritable', ctypes.c_uint32)]
            libc = ctypes.CDLL(None, use_errno=True)
            header, caps = Header(0x20080522, 0), (Data * 2)()
            assert libc.capget(ctypes.byref(header), caps) == 0
            caps[0].effective &= ~6
            caps[0].permitted &= ~6
            caps[0].inheritable &= ~6
            assert libc.capset(ctypes.byref(header), caps) == 0
        def deny_sync(phase):
            if phase == 'swapped':
                retired.chmod(0)
        try:
            try:
                install_group(source=source, revision=revision,
                    packs={'alpha': 'packs/alpha', 'shared': 'packs/shared'},
                    packs_root=root, python_packages={}, boundary=deny_sync)
            except GroupCommitError as error:
                print(json.dumps({'transaction_id': error.transaction_id,
                                  'revision': error.revision}), flush=True)
            else:
                raise AssertionError('durability failure should have been reported')
        finally:
            retired.chmod(0o700)
    """), encoding="utf-8")
    child = subprocess.run([sys.executable, str(script), str(repo.bare), new_commit,
                            str(packs_root)], env=child_env, text=True,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=45)
    assert child.returncode == 0, child.stderr
    failure = json.loads(child.stdout)
    committed = read_group(packs_root)
    assert committed.transaction_id == failure["transaction_id"]
    assert committed.revision == failure["revision"] == 2
    assert committed.refresh_pending
    assert_group_pins(packs_root, new_commit,
                      {"alpha": b"alpha-v2\n", "shared": b"shared-v2\n"})
    assert retry_refresh(packs_root=packs_root, refresh=accepted_refresh) is False
    assert not read_group(packs_root).refresh_pending
    assert old_commit != new_commit


def test_default_conflict_refuses_user_pack_without_changing_group(
        repo: Repository, packs_root: Path) -> None:
    install(repo, packs_root)
    before = group_bytes(packs_root)
    digest = pack_digest(repo.work / "packs" / "alpha")
    with pytest.raises(PackInstallError) as failure:
        install_group(source=repo.bare, revision=repo.commit, packs={"alpha": "packs/alpha"},
                     packs_root=packs_root, python_packages={}, origin="default", intent="defaults",
                     expected_digests={"alpha": digest},
                     release_pins={"alpha": {"commit": repo.commit, "digest": digest}})
    assert failure.value.code == "default_pin_conflict"
    assert group_bytes(packs_root) == before


def test_removal_tombstone_survives_default_upgrades_and_rollbacks(repo: Repository, packs_root: Path) -> None:
    old_commit = repo.commit
    alpha_digest = pack_digest(repo.work / "packs" / "alpha")
    shared_digest = pack_digest(repo.work / "packs" / "shared")
    install_group(source=repo.bare, revision=old_commit, packs=GROUP, packs_root=packs_root,
                  python_packages={}, origin="default", intent="defaults",
                  expected_digests={"alpha": alpha_digest, "shared": shared_digest},
                  release_pins={"alpha": {"commit": old_commit, "digest": alpha_digest},
                                "shared": {"commit": old_commit, "digest": shared_digest}},
                  refresh=accepted_refresh)
    install_group(source=repo.bare, packs={}, packs_root=packs_root,
                  python_packages={}, remove=("alpha",))
    assert "alpha" not in read_group(packs_root).paths
    new_commit = repo.next_revision()
    new_alpha_digest = pack_digest(repo.work / "packs" / "alpha")
    new_shared_digest = pack_digest(repo.work / "packs" / "shared")

    # A managed upgrade omits the explicitly removed pack but still advances
    # the retained default pack from the common source revision.
    install_group(source=repo.bare, revision=new_commit, packs=GROUP, packs_root=packs_root,
                  python_packages={}, origin="default", intent="defaults",
                  expected_digests={"alpha": new_alpha_digest, "shared": new_shared_digest},
                  release_pins={"alpha": {"commit": new_commit, "digest": new_alpha_digest},
                                "shared": {"commit": new_commit, "digest": new_shared_digest}},
                  refresh=accepted_refresh)
    snapshot = read_group(packs_root)
    assert set(snapshot.paths) == {"shared"}
    assert snapshot.removals == ("alpha",)
    assert read_pin(snapshot.paths["shared"] / "PIN")["commit"] == new_commit

    # Rollback/replay of an older default declaration does not resurrect it.
    install_group(source=repo.bare, revision=old_commit, packs={"alpha": "packs/alpha"},
                  packs_root=packs_root, python_packages={}, origin="default", intent="defaults",
                  expected_digests={"alpha": alpha_digest},
                  release_pins={"alpha": {"commit": old_commit, "digest": alpha_digest}})
    assert set(read_group(packs_root).paths) == {"shared"}
    assert read_group(packs_root).removals == ("alpha",)


def test_removing_dependency_needed_by_retained_pack_refuses_whole_group(
        repo: Repository, packs_root: Path) -> None:
    install(repo, packs_root, refresh=accepted_refresh)
    before = group_bytes(packs_root)
    before_snapshot = read_group(packs_root)
    with pytest.raises(PackInstallError) as failure:
        install_group(source=repo.bare, packs={}, packs_root=packs_root,
                      python_packages={}, remove=("shared",))
    assert failure.value.code == "missing_dep"
    assert group_bytes(packs_root) == before
    after = read_group(packs_root)
    assert after.transaction_id == before_snapshot.transaction_id
    assert after.removals == before_snapshot.removals


def test_explicit_reinstall_clears_a_previously_committed_removal(
        repo: Repository, packs_root: Path) -> None:
    install(repo, packs_root, refresh=accepted_refresh)
    install_group(source=repo.bare, packs={}, packs_root=packs_root,
                  python_packages={}, remove=("alpha",))
    assert read_group(packs_root).removals == ("alpha",)
    install_group(source=repo.bare, revision=repo.commit,
                  packs={"alpha": "packs/alpha"}, packs_root=packs_root,
                  python_packages={})
    snapshot = read_group(packs_root)
    assert "alpha" in snapshot.paths
    assert snapshot.removals == ()


def test_folder_source_group_uses_real_pack_copy_and_folder_pins(
        repo: Repository, packs_root: Path) -> None:
    result = install_group(source=repo.work, revision=None, packs=GROUP,
                           packs_root=packs_root, python_packages={})
    snapshot = read_group(packs_root)
    assert set(snapshot.paths) == {"alpha", "shared"}
    assert result.revision == snapshot.revision == 1
    for path in snapshot.paths.values():
        pin = read_pin(path / "PIN")
        assert pin["origin"] == "folder"
        assert pin["commit"] is None
        assert pin["digest"] is None
        assert not (path / ".git").exists()


def test_committed_group_is_visible_through_real_loader_and_external_discovery(
        repo: Repository, packs_root: Path, tmp_path: Path) -> None:
    install(repo, packs_root, refresh=accepted_refresh)
    from astrid.core.pack.discovery import discover_canonical_pack_metadata
    from astrid.core.pack.loader import scan_packs

    scan = scan_packs(packs_root)
    assert {pack.id for pack in scan.packs} == {"alpha", "shared"}
    discovered = discover_canonical_pack_metadata(
        project_root=tmp_path / "project", extra_pack_roots=(str(packs_root),))
    external = {item.id: item for item in discovered if item.source_kind == "extra"}
    assert set(external) == {"alpha", "shared"}
    assert all(item.pack_dir.parent == packs_root for item in external.values())


def test_named_update_does_not_implicitly_update_other_user_packs(repo: Repository, packs_root: Path) -> None:
    old_commit = repo.commit
    install(repo, packs_root)
    repo.next_revision()
    # Install an independently named pack from the new commit. The existing
    # user-owned dependency is retained byte-for-byte until explicitly named.
    repo.write_pack("extra", "extra-v2\n")
    new_commit = repo.publish()
    install(repo, packs_root, packs={"extra": "packs/extra"}, revision=new_commit)
    snapshot = read_group(packs_root)
    assert read_pin(snapshot.paths["alpha"] / "PIN")["commit"] == old_commit
    assert (snapshot.paths["alpha"] / "payload.txt").read_bytes() == b"alpha-v1\n"
    assert read_pin(snapshot.paths["shared"] / "PIN")["commit"] == old_commit
    assert read_pin(snapshot.paths["extra"] / "PIN")["commit"] == new_commit


@pytest.mark.parametrize("failure", ["source", "build"])
def test_source_or_real_builder_failure_preserves_old_group(
        repo: Repository, packs_root: Path, tmp_path: Path, failure: str) -> None:
    initial = install(repo, packs_root, refresh=accepted_refresh)
    before = group_bytes(packs_root)
    kwargs = {}
    if failure == "source":
        kwargs.update(source=tmp_path / "missing-source.git", revision=repo.commit)
    else:
        kwargs.update(source=repo.bare, revision=repo.commit,
                      builds={"alpha": {"entry": "missing.tsx"}})
    with pytest.raises(PackInstallError):
        install_group(packs=GROUP, packs_root=packs_root, python_packages={}, **kwargs)
    assert group_bytes(packs_root) == before
    snapshot = read_group(packs_root)
    assert snapshot.transaction_id == initial.transaction_id
    assert snapshot.refresh_pending is False
