"""Atomic dependency group installs over real Git repositories and builders."""

from __future__ import annotations

import hashlib
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

from astrid.core.pack.identity import pack_digest, read_pin, write_pin
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


def manifest(pack_id: str, dependencies: tuple[str, ...] = (),
             python_requirements: tuple[str, ...] = ()) -> str:
    dependency_lines = []
    if dependencies:
        dependency_lines.append("  packs: [" + ", ".join(dependencies) + "]")
    if python_requirements:
        dependency_lines.append("  python_requirements: [" + ", ".join(python_requirements) + "]")
    dependency_field = "" if not dependency_lines else "dependencies:\n" + "\n".join(dependency_lines) + "\n"
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


def tree_snapshot(root: Path) -> dict[str, bytes | None]:
    if not root.exists():
        return {}
    return {path.relative_to(root).as_posix():
            (None if path.is_dir() else path.read_bytes()) for path in root.rglob("*")}


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


def contract_builder(request):
    """Writer-shaped M4 result with real bundle and manifest hashes."""
    stage = Path(request["stage_root"])
    source_digest = request["source_digest"]
    graph_digest = hashlib.sha256(b"[]").hexdigest()
    entry = f"/* astrid-graph-index-sha256:{graph_digest} */\nexport default 1;\n".encode()
    bundle_digest = hashlib.sha256(entry).hexdigest()
    build_dir = stage / ".build"
    build_dir.mkdir(exist_ok=True)
    (build_dir / f"{source_digest}.js").write_bytes(entry)
    artifact = {"version": 1, "packId": request["pack_id"], "sourceDigest": source_digest,
                "digest": bundle_digest, "graphIndexDigest": graph_digest,
                "toolchainDigest": request["toolchain"].get("lock_sha256"), "entry": "index.js",
                "extensionManifest": request["extension_manifest"],
                "sharedModules": None,
                "files": [{"path": "index.js", "disk": f".build/{source_digest}.js",
                           "sha256": bundle_digest, "size": len(entry), "mime": "text/javascript"}]}
    return {"version": 1, "ok": True, "pack_id": request["pack_id"],
            "source_digest": source_digest, "artifact_manifest": artifact}


def contract_builds():
    return {pack_id: {"entry": "src/index.tsx", "extension_manifest": {"id": pack_id}}
            for pack_id in GROUP}


def kill_child_at_boundary(repo: Repository, root: Path, tmp_path: Path,
                           revision: str, phase: str, *, browser=None) -> None:
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
        import json, os, signal, sys
        from astrid.core.pack.transaction import install_group
        source, revision, root, phase, options = sys.argv[1:]
        def boundary(current):
            if current == phase:
                os.kill(os.getpid(), signal.SIGKILL)
        install_group(source=source, revision=revision,
            packs={"alpha": "packs/alpha", "shared": "packs/shared"},
            packs_root=root, python_packages={}, boundary=boundary, **json.loads(options))
        raise AssertionError("requested boundary was not reached")
    """), encoding="utf-8")
    child = subprocess.run([sys.executable, str(script), str(repo.bare), revision,
                            str(root), phase, json.dumps(browser or {})], env=child_env,
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


@pytest.mark.parametrize("intent", ["update", "rollback"])
def test_update_and_rollback_intents_keep_user_removals(
        repo: Repository, packs_root: Path, intent: str) -> None:
    old_commit = repo.commit
    alpha_digest = pack_digest(repo.work / "packs" / "alpha")
    shared_digest = pack_digest(repo.work / "packs" / "shared")
    install_group(source=repo.bare, revision=old_commit, packs=GROUP, packs_root=packs_root,
                  python_packages={}, origin="default", intent="defaults",
                  expected_digests={"alpha": alpha_digest, "shared": shared_digest},
                  release_pins={"alpha": {"commit": old_commit, "digest": alpha_digest},
                                "shared": {"commit": old_commit, "digest": shared_digest}})
    install_group(source=repo.bare, packs={}, packs_root=packs_root,
                  python_packages={}, remove=("alpha",))
    older_snapshot = read_group(packs_root)
    assert "alpha" not in older_snapshot.paths
    assert older_snapshot.removals == ("alpha",)

    new_commit = repo.next_revision()
    new_alpha_digest = pack_digest(repo.work / "packs" / "alpha")
    new_shared_digest = pack_digest(repo.work / "packs" / "shared")
    revision = new_commit if intent == "update" else old_commit
    digests = ({"alpha": new_alpha_digest, "shared": new_shared_digest}
               if intent == "update" else {"alpha": alpha_digest})
    pins = ({"alpha": {"commit": new_commit, "digest": new_alpha_digest},
             "shared": {"commit": new_commit, "digest": new_shared_digest}}
            if intent == "update" else
            {"alpha": {"commit": old_commit, "digest": alpha_digest}})
    install_group(source=repo.bare, revision=revision,
                  packs={"alpha": "packs/alpha", **({"shared": "packs/shared"} if intent == "update" else {})},
                  packs_root=packs_root, python_packages={}, origin="default", intent=intent,
                  expected_digests=digests, release_pins=pins)
    snapshot = read_group(packs_root)
    assert "alpha" not in snapshot.paths
    assert snapshot.removals == ("alpha",)
    if intent == "update":
        assert read_pin(snapshot.paths["shared"] / "PIN")["commit"] == new_commit
    else:
        assert read_group(packs_root).revision == older_snapshot.revision + 1


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


def test_real_packs_update_command_and_rollback_restore_previous_generation(
        repo: Repository, packs_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from astrid.core.pack.cli import main as packs_cli_main

    install(repo, packs_root)
    old = read_group(packs_root)
    old_payload = {pack_id: (path / "payload.txt").read_bytes() for pack_id, path in old.paths.items()}
    old_commit = read_pin(old.paths["alpha"] / "PIN")["commit"]
    new_commit = repo.next_revision()
    monkeypatch.setattr("builtins.input", lambda _prompt: "yes")
    result = packs_cli_main(["update", "--source", str(repo.bare), "--revision", new_commit,
                             "--packs-root", str(packs_root), "--pack", "alpha=packs/alpha",
                             "--pack", "shared=packs/shared"])
    assert result == 0
    updated = read_group(packs_root)
    assert all(read_pin(path / "PIN")["commit"] == new_commit for path in updated.paths.values())

    result = packs_cli_main(["rollback", "--packs-root", str(packs_root), "--pack", "alpha", "--pack", "shared"])
    assert result == 0
    rolled_back = read_group(packs_root)
    assert {pack_id: (path / "payload.txt").read_bytes() for pack_id, path in rolled_back.paths.items()} == old_payload
    assert all(read_pin(path / "PIN")["commit"] == old_commit for path in rolled_back.paths.values())


@pytest.mark.parametrize("configured_override", [None, "override"])
def test_update_and_rollback_cli_default_to_configured_user_collection(
        repo: Repository, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, configured_override: str | None) -> None:
    from astrid.core.pack.cli import main as packs_cli_main
    from astrid.core.pack.loader import packs_root as bundled_packs_root

    home = tmp_path / "isolated-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("ASTRID_PACKS_PATH", raising=False)
    unrelated = tmp_path / "unrelated-packs"
    unrelated.mkdir()
    marker = unrelated / "do-not-touch"
    marker.write_text("unrelated\n", encoding="utf-8")
    if configured_override:
        selected = tmp_path / configured_override
        monkeypatch.setenv("ASTRID_PACKS_PATH", str(selected))
    else:
        selected = home / "Astrid" / "packs"
    bundled = bundled_packs_root().resolve()
    bundled_before = tree_snapshot(bundled)
    unrelated_before = tree_snapshot(unrelated)
    original_commit = repo.commit

    monkeypatch.setattr("builtins.input", lambda _prompt: "yes")
    assert packs_cli_main(["update", "--source", str(repo.bare), "--revision", repo.commit,
                           "--pack", "alpha=packs/alpha", "--pack", "shared=packs/shared"]) == 0
    assert set(read_group(selected).paths) == {"alpha", "shared"}
    newer = repo.next_revision()
    assert packs_cli_main(["update", "--source", str(repo.bare), "--revision", newer,
                           "--pack", "alpha=packs/alpha", "--pack", "shared=packs/shared"]) == 0
    assert packs_cli_main(["rollback", "--pack", "alpha", "--pack", "shared"]) == 0
    assert all(read_pin(path / "PIN")["commit"] == original_commit for path in read_group(selected).paths.values())
    assert marker.read_text(encoding="utf-8") == "unrelated\n"
    assert tree_snapshot(bundled) == bundled_before
    assert tree_snapshot(unrelated) == unrelated_before


@pytest.mark.parametrize("configured_root", ["", "relative/packs"])
def test_update_cli_refuses_disabled_or_invalid_configured_collection(
        repo: Repository, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, configured_root: str) -> None:
    from astrid.core.pack.cli import main as packs_cli_main
    from astrid.core.pack.loader import packs_root as bundled_packs_root

    home = tmp_path / "isolated-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("ASTRID_PACKS_PATH", configured_root)
    bundled = bundled_packs_root().resolve()
    bundled_before = tree_snapshot(bundled)
    assert packs_cli_main(["update", "--source", str(repo.bare), "--revision", repo.commit,
                           "--pack", "alpha=packs/alpha"]) == 2
    assert not (home / "Astrid" / "packs").exists()
    assert tree_snapshot(bundled) == bundled_before


@pytest.mark.parametrize("dependency_present", [True, False])
def test_real_update_cli_uses_explicit_selected_external_dependency_roots(
        repo: Repository, packs_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        dependency_present: bool) -> None:
    from astrid.core.pack.cli import main as packs_cli_main

    dependency = "extdep" if dependency_present else "genuinely_missing"
    (repo.work / "packs" / "alpha" / "pack.yaml").write_text(
        manifest("alpha", (dependency,)), encoding="utf-8")
    target = repo.publish()
    external = tmp_path / "selected-external"
    external.mkdir()
    if dependency_present:
        pack = external / "extdep"
        pack.mkdir()
        (pack / "pack.yaml").write_text(manifest("extdep"), encoding="utf-8")
    unrelated = tmp_path / "unselected-root"
    unrelated.mkdir()
    (unrelated / "do-not-read").write_text("unrelated", encoding="utf-8")
    original_iterdir = Path.iterdir
    def guard_unrelated(path):
        if path.resolve() == unrelated.resolve():
            raise AssertionError("unselected root was scanned")
        return original_iterdir(path)
    monkeypatch.setattr(Path, "iterdir", guard_unrelated)
    monkeypatch.setenv("ASTRID_PACKS_PATH", str(external))
    monkeypatch.setattr("builtins.input", lambda _prompt: "yes")
    result = packs_cli_main(["update", "--source", str(repo.bare), "--revision", target,
                             "--packs-root", str(packs_root), "--pack", "alpha=packs/alpha",
                             "--pack", "shared=packs/shared"])
    if dependency_present:
        assert result == 0
        assert packs_cli_main(["update", "--source", str(repo.bare), "--revision", target,
                               "--packs-root", str(packs_root), "--pack", "alpha=packs/alpha",
                               "--pack", "shared=packs/shared"]) == 0
        assert "alpha" in read_group(packs_root).paths
        assert packs_cli_main(["rollback", "--packs-root", str(packs_root),
                               "--pack", "alpha", "--pack", "shared"]) == 0
        assert "alpha" in read_group(packs_root).paths
    else:
        assert result == 2
        assert read_group(packs_root).revision == 0
    assert (unrelated / "do-not-read").read_text(encoding="utf-8") == "unrelated"


def test_real_update_cli_allows_omitted_source_tree_digest_and_rejects_real_release_pin_mismatch(
        repo: Repository, packs_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from astrid.core.pack.cli import main as packs_cli_main

    declarations = tmp_path / "source-declarations.json"
    declarations.write_text(json.dumps({"sources": [{"pack_id": "alpha", "repository": str(repo.bare),
                                                      "revision": repo.commit}]}), encoding="utf-8")
    monkeypatch.setenv("ASTRID_SOURCE_DECLARATIONS", str(declarations))
    monkeypatch.delenv("ASTRID_RELEASE_PACK_PINS", raising=False)
    monkeypatch.setattr("builtins.input", lambda _prompt: "yes")
    assert packs_cli_main(["update", "--source", str(repo.bare), "--revision", repo.commit,
                           "--packs-root", str(packs_root), "--pack", "alpha=packs/alpha",
                           "--pack", "shared=packs/shared"]) == 0

    pinned = tmp_path / "release-pins.json"
    pinned.write_text(json.dumps({"alpha": {"commit": "f" * 40, "digest": "e" * 64}}), encoding="utf-8")
    monkeypatch.setenv("ASTRID_RELEASE_PACK_PINS", str(pinned))
    next_revision = repo.next_revision()
    before = tree_bytes(packs_root)
    current = read_group(packs_root)
    assert packs_cli_main(["update", "--source", str(repo.bare), "--revision", next_revision,
                           "--packs-root", str(packs_root), "--pack", "alpha=packs/alpha",
                           "--pack", "shared=packs/shared"]) == 2
    assert tree_bytes(packs_root) == before
    assert read_group(packs_root).transaction_id == current.transaction_id


def test_rollback_uses_explicit_interpreter_snapshot_for_python_requirements(
        repo: Repository, packs_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from importlib import metadata
    from importlib.metadata import PackageNotFoundError
    from astrid.core.pack.transaction import rollback_group

    # Use only the selected interpreter's installed distribution metadata, with
    # no synthetic package or path injection.
    packages = {dist.metadata.get("Name", "").lower().replace("_", "-").replace(".", "-"): dist.version
                for dist in metadata.distributions() if dist.metadata.get("Name")}
    assert "pytest" in packages
    alpha_manifest = repo.work / "packs" / "alpha" / "pack.yaml"
    alpha_manifest.write_text(manifest("alpha", ("shared",), ("pytest",)), encoding="utf-8")
    first_commit = repo.publish()
    install_group(source=repo.bare, revision=first_commit, packs=GROUP, packs_root=packs_root,
                  python_packages=packages)
    alpha_manifest.write_text(manifest("alpha", ("shared",)), encoding="utf-8")
    second_commit = repo.publish()
    install_group(source=repo.bare, revision=second_commit, packs=GROUP, packs_root=packs_root,
                  python_packages=packages, intent="update")
    current = read_group(packs_root)
    rollback_group(packs_root=packs_root, pack_ids=("alpha", "shared"),
                   transaction_id=current.transaction_id, python_packages=packages)
    restored = read_group(packs_root).paths["alpha"]
    assert "python_requirements: [pytest]" in (restored / "pack.yaml").read_text(encoding="utf-8")

    # A genuinely absent distribution in a retained generation must refuse.
    (repo.work / "packs" / "alpha" / "payload.txt").write_text("alpha-v3\n", encoding="utf-8")
    third_commit = repo.publish()
    install_group(source=repo.bare, revision=third_commit, packs=GROUP, packs_root=packs_root,
                  python_packages=packages, intent="update")
    current = read_group(packs_root)
    missing_name = "astrid-rev-b5-absent-8c6a7d"
    with pytest.raises(PackageNotFoundError):
        metadata.version(missing_name)
    retained_alpha = packs_root.parent / ".astrid-retired" / current.transaction_id / "alpha" / "pack.yaml"
    retained_alpha.write_text(manifest("alpha", ("shared",), (missing_name,)), encoding="utf-8")
    before = group_bytes(packs_root)
    with pytest.raises(PackInstallError) as failure:
        rollback_group(packs_root=packs_root, pack_ids=("alpha", "shared"),
                       transaction_id=current.transaction_id, python_packages=packages)
    assert failure.value.code == "missing_python"
    assert group_bytes(packs_root) == before


def test_rollback_restores_build_hook_metadata_with_the_pack_generation(
        repo: Repository, packs_root: Path, browser) -> None:
    import os
    import sys
    from astrid.core.pack.transaction import rollback_group

    install(repo, packs_root, refresh=accepted_refresh, **browser)
    prior = read_group(packs_root).paths["alpha"]
    prior_digest = pack_digest(prior)
    prior_commit = read_pin(prior / "PIN")["commit"]
    prior_admission = (prior / ".build" / "admission.json").read_bytes()
    prior_manifest_path = prior / ".build" / f"{prior_digest}.artifact-manifest.json"
    prior_manifest = prior_manifest_path.read_bytes()

    new_commit = repo.next_revision()
    second = install(repo, packs_root, revision=new_commit, refresh=accepted_refresh,
                     **browser)
    current = read_group(packs_root).paths["alpha"]
    current_digest = pack_digest(current)
    assert (current / ".build" / "admission.json").read_bytes() != prior_admission
    assert current_digest != prior_digest
    assert (current / ".build" / f"{current_digest}.artifact-manifest.json").is_file()
    current_admission = json.loads((current / ".build" / "admission.json").read_text(encoding="utf-8"))
    assert current_admission["sourceDigest"] == current_digest
    assert read_pin(current / "PIN")["commit"] == new_commit
    manifest_bytes = (current / ".build" / f"{current_digest}.artifact-manifest.json").read_bytes()
    artifact_manifest = read_group(packs_root).admissions["alpha"]["artifact_manifest"]
    assert manifest_bytes == json.dumps(artifact_manifest, sort_keys=True,
                                        separators=(",", ":"), ensure_ascii=False).encode()
    assert b"\n" not in manifest_bytes
    admission_record = json.loads((current / ".build" / "admission.json").read_text(encoding="utf-8"))
    assert admission_record == {"version": 1, "packId": "alpha", "sourceDigest": current_digest,
                                "digest": current_admission["digest"],
                                "artifactManifestDigest": hashlib.sha256(manifest_bytes).hexdigest(),
                                "committedRevision": read_group(packs_root).revision,
                                "transactionId": read_group(packs_root).transaction_id}

    retired = read_retired_group(packs_root=packs_root, transaction_id=second.transaction_id)
    retired_alpha = retired.paths["alpha"]
    assert (retired_alpha / ".build" / "admission.json").read_bytes() == prior_admission
    assert (retired_alpha / ".build" / f"{prior_digest}.artifact-manifest.json").read_bytes() == prior_manifest

    rollback_group(packs_root=packs_root, pack_ids=("alpha", "shared"),
                   transaction_id=second.transaction_id, refresh=accepted_refresh)
    restored = read_group(packs_root).paths["alpha"]
    assert read_pin(restored / "PIN")["commit"] == prior_commit
    assert (restored / ".build" / "admission.json").read_bytes() != prior_admission
    assert (restored / ".build" / f"{prior_digest}.artifact-manifest.json").read_bytes() == prior_manifest
    rollback_record = json.loads((restored / ".build" / "admission.json").read_text(encoding="utf-8"))
    snapshot = read_group(packs_root)
    assert rollback_record["packId"] == "alpha"
    assert rollback_record["sourceDigest"] == pack_digest(restored) == prior_digest
    manifest_bytes = (restored / ".build" / f"{prior_digest}.artifact-manifest.json").read_bytes()
    artifact = json.loads(manifest_bytes)
    assert (rollback_record["digest"], rollback_record["artifactManifestDigest"],
            rollback_record["committedRevision"], rollback_record["transactionId"]) == (
                artifact["digest"], hashlib.sha256(manifest_bytes).hexdigest(),
                snapshot.revision, snapshot.transaction_id)

    # Run Runtime's pinned parser in an isolated interpreter so an earlier
    # import of the working Runtime checkout cannot shadow this exact version.
    runtime_checkout = Path(os.environ["ASTRID_RUNTIME_ADMISSION_CHECKOUT"])
    reader_script = textwrap.dedent("""
        import json, sys
        from runtime_protocol.store import InstalledPackAdmissions, read_pack_admissions
        reader = InstalledPackAdmissions(sys.argv[1])
        revision, records = read_pack_admissions(reader, pack_id="alpha")
        print(json.dumps({"revision": revision, "records": records}, sort_keys=True))
    """)
    runtime_env = dict(os.environ)
    runtime_env["PYTHONPATH"] = str(runtime_checkout)
    checked = subprocess.run([sys.executable, "-c", reader_script, str(packs_root)],
                             check=True, capture_output=True, text=True, env=runtime_env)
    runtime_snapshot = json.loads(checked.stdout)
    assert runtime_snapshot["revision"] == snapshot.revision
    assert len(runtime_snapshot["records"]) == 1
    parsed = runtime_snapshot["records"][0]
    assert (parsed["packId"], parsed["sourceDigest"], parsed["digest"],
            parsed["artifactManifestDigest"]) == (
                rollback_record["packId"], rollback_record["sourceDigest"], rollback_record["digest"],
                rollback_record["artifactManifestDigest"])
    assert parsed["artifactManifest"] == json.loads(manifest_bytes)
    for item in parsed["files"].values():
        data = (restored / item["disk"]).read_bytes()
        assert len(data) == item["size"]
        assert hashlib.sha256(data).hexdigest() == item["sha256"]


def test_contract_admission_tamper_refuses_update_before_publication(
        repo: Repository, packs_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import astrid.core.pack.transaction as transaction

    monkeypatch.setattr(transaction, "build_pack_assets", contract_builder)
    install(repo, packs_root, refresh=accepted_refresh, builds=contract_builds())
    before = group_bytes(packs_root)
    before_state = read_group(packs_root)
    target = repo.next_revision()

    def tampered_builder(request):
        result = contract_builder(request)
        entry = Path(request["stage_root"]) / f".build/{request['source_digest']}.js"
        entry.write_bytes(entry.read_bytes() + b"tampered")
        return result

    monkeypatch.setattr(transaction, "build_pack_assets", tampered_builder)
    with pytest.raises(PackInstallError) as failure:
        install_group(source=repo.bare, revision=target, packs=GROUP, packs_root=packs_root,
                      python_packages={}, builds=contract_builds(), intent="update")
    assert failure.value.code == "hash_mismatch"
    assert group_bytes(packs_root) == before
    assert read_group(packs_root).transaction_id == before_state.transaction_id


@pytest.mark.parametrize("phase", ["before_swap", "swapped"])
def test_contract_admission_interruption_retry_keeps_record_revision_aligned(
        repo: Repository, packs_root: Path, monkeypatch: pytest.MonkeyPatch, phase: str) -> None:
    import astrid.core.pack.transaction as transaction

    monkeypatch.setattr(transaction, "build_pack_assets", contract_builder)
    install(repo, packs_root, refresh=accepted_refresh, builds=contract_builds())
    previous = read_group(packs_root)
    target = repo.next_revision()

    def interrupt(at):
        if at == phase:
            raise RuntimeError("simulated interruption")

    with pytest.raises(RuntimeError, match="simulated interruption"):
        install_group(source=repo.bare, revision=target, packs=GROUP, packs_root=packs_root,
                      python_packages={}, builds=contract_builds(), intent="update",
                      boundary=interrupt)
    visible = read_group(packs_root)
    if phase == "before_swap":
        assert visible.transaction_id == previous.transaction_id
        install_group(source=repo.bare, revision=target, packs=GROUP, packs_root=packs_root,
                      python_packages={}, builds=contract_builds(), intent="update",
                      refresh=accepted_refresh)
        visible = read_group(packs_root)
    else:
        assert visible.revision == previous.revision + 1
        assert retry_refresh(packs_root=packs_root, refresh=accepted_refresh) is False
        visible = read_group(packs_root)
    for pack_id in GROUP:
        record = json.loads((visible.paths[pack_id] / ".build" / "admission.json").read_text(encoding="utf-8"))
        assert record["committedRevision"] == visible.revision
        assert record["transactionId"] == visible.transaction_id
        manifest_bytes = (visible.paths[pack_id] / ".build" /
                          f"{record['sourceDigest']}.artifact-manifest.json").read_bytes()
        assert record["artifactManifestDigest"] == hashlib.sha256(manifest_bytes).hexdigest()


@pytest.mark.parametrize("failure", ["build", "swap"])
def test_real_update_command_failure_keeps_previous_generation(
        repo: Repository, packs_root: Path, monkeypatch: pytest.MonkeyPatch, failure: str) -> None:
    from astrid.core.pack.cli import main as packs_cli_main

    install(repo, packs_root)
    before = group_bytes(packs_root)
    before_state = read_group(packs_root)
    target = repo.next_revision()
    monkeypatch.setattr("builtins.input", lambda _prompt: "yes")
    if failure == "build":
        def fail_build(_request):
            raise PackInstallError("compile_failed", "fixture build failure")
        monkeypatch.setattr("astrid.core.pack.transaction.build_pack_assets", fail_build)
    else:
        def fail_swap(_stage, _destination):
            raise PackInstallError("atomic_swap_unsupported", "fixture swap failure")
        monkeypatch.setattr("astrid.core.pack.transaction._publish", fail_swap)
    result = packs_cli_main(["update", "--source", str(repo.bare), "--revision", target,
                             "--packs-root", str(packs_root), "--pack", "alpha=packs/alpha",
                             "--pack", "shared=packs/shared"])
    assert result == 2
    assert group_bytes(packs_root) == before
    assert read_group(packs_root).transaction_id == before_state.transaction_id


def test_real_update_and_rollback_preserve_removals_and_unrelated_packs(
        repo: Repository, packs_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from astrid.core.pack.cli import main as packs_cli_main

    repo.write_pack("extra", "extra-v1\n")
    base_commit = repo.publish()
    install(repo, packs_root, revision=base_commit)
    install_group(source=repo.bare, revision=base_commit, packs={"extra": "packs/extra"},
                  packs_root=packs_root, python_packages={})
    personal = packs_root / "personal"
    personal.mkdir()
    (personal / "pack.yaml").write_text(manifest("personal"), encoding="utf-8")
    write_pin(personal / "PIN", {"version": 1, "source": personal.as_uri(), "commit": None,
                                  "digest_rule": "pack-tree-v1", "digest": None, "origin": "personal"})
    (personal / "marker.txt").write_text("personal\n")
    install_group(source=repo.bare, packs={}, packs_root=packs_root,
                  python_packages={}, remove=("alpha",))
    new_commit = repo.next_revision()
    monkeypatch.setattr("builtins.input", lambda _prompt: "yes")
    common = ["--packs-root", str(packs_root)]
    assert packs_cli_main(["update", "--source", str(repo.bare), "--revision", new_commit,
                           *common, "--pack", "alpha=packs/alpha", "--pack", "shared=packs/shared"]) == 0
    snapshot = read_group(packs_root)
    assert "alpha" not in snapshot.paths and snapshot.removals == ("alpha",)
    assert read_pin(snapshot.paths["extra"] / "PIN")["commit"] == base_commit
    assert (snapshot.paths["extra"] / "payload.txt").read_text() == "extra-v1\n"
    assert (snapshot.paths["personal"] / "marker.txt").read_text() == "personal\n"
    assert packs_cli_main(["rollback", *common, "--pack", "alpha", "--pack", "shared"]) == 0
    snapshot = read_group(packs_root)
    assert "alpha" not in snapshot.paths and snapshot.removals == ("alpha",)
    assert read_pin(snapshot.paths["extra"] / "PIN")["commit"] == base_commit
    assert (snapshot.paths["extra"] / "payload.txt").read_text() == "extra-v1\n"
    assert (snapshot.paths["personal"] / "marker.txt").read_text() == "personal\n"


def test_rollback_does_not_restore_tombstone_cleared_by_explicit_readd(
        repo: Repository, packs_root: Path) -> None:
    from astrid.core.pack.transaction import rollback_group

    digests = {pack_id: pack_digest(repo.work / subpath) for pack_id, subpath in GROUP.items()}
    install_group(source=repo.bare, revision=repo.commit, packs=GROUP, packs_root=packs_root,
                  python_packages={}, origin="default", intent="defaults", expected_digests=digests)
    install_group(source=repo.bare, packs={}, packs_root=packs_root, python_packages={}, remove=("alpha",))
    assert read_group(packs_root).removals == ("alpha",)
    explicit = install_group(source=repo.bare, revision=repo.commit, packs={"alpha": "packs/alpha"},
                             packs_root=packs_root, python_packages={}, origin="default",
                             expected_digests={"alpha": digests["alpha"]})
    assert read_group(packs_root).removals == ()
    rollback_group(packs_root=packs_root, pack_ids=("alpha",), transaction_id=explicit.transaction_id)
    restored = read_group(packs_root)
    assert "alpha" not in restored.paths
    assert restored.removals == ()
    install_group(source=repo.bare, revision=repo.commit, packs=GROUP, packs_root=packs_root,
                  python_packages={}, origin="default", intent="defaults", expected_digests=digests)
    assert "alpha" in read_group(packs_root).paths


def test_rollback_refuses_conflicting_current_source_pin(repo: Repository, packs_root: Path) -> None:
    from astrid.core.pack.transaction import rollback_group

    install(repo, packs_root)
    target = repo.next_revision()
    install(repo, packs_root, revision=target, intent="update")
    current = read_group(packs_root)
    alpha_pin = read_pin(current.paths["alpha"] / "PIN")
    alpha_pin["source"] = "file:///different/repository#packs/alpha"
    write_pin(current.paths["alpha"] / "PIN", alpha_pin)
    before = group_bytes(packs_root)
    with pytest.raises(PackInstallError, match="conflicting source pins") as failure:
        rollback_group(packs_root=packs_root, pack_ids=("alpha", "shared"))
    assert failure.value.code == "rollback_conflict"
    assert group_bytes(packs_root) == before


@pytest.mark.parametrize("fault", ["entry", "chunk", "manifest", "record", "missing"])
def test_rollback_refuses_corrupt_retired_browser_admission_without_changing_live_generation(
        repo: Repository, packs_root: Path, browser, fault: str) -> None:
    from astrid.core.pack.transaction import rollback_group

    first = install(repo, packs_root, **browser, refresh=accepted_refresh)
    second = install(repo, packs_root, revision=change_browser(repo), **browser, refresh=accepted_refresh)
    prior = packs_root.parent / ".astrid-retired" / second.transaction_id / "alpha"
    record_path = prior / ".build" / "admission.json"
    record = json.loads(record_path.read_bytes())
    manifest_path = prior / ".build" / f'{record["sourceDigest"]}.artifact-manifest.json'
    if fault == "manifest":
        target = manifest_path
    elif fault == "record":
        target = record_path
    elif fault in {"entry", "chunk"}:
        artifact = json.loads(manifest_path.read_bytes())
        item = next(row for row in artifact["files"] if (row["path"] == "index.js") == (fault == "entry"))
        target = prior / item["disk"]
    else:
        artifact = json.loads(manifest_path.read_bytes())
        item = next(row for row in artifact["files"] if row["path"] != "index.js")
        target = prior / item["disk"]
    if fault == "missing":
        target.unlink()
    else:
        target.write_bytes(target.read_bytes() + b"tampered")

    before = tree_bytes(packs_root)
    current = read_group(packs_root)
    with pytest.raises(PackInstallError):
        rollback_group(packs_root=packs_root, pack_ids=("alpha", "shared"),
                       transaction_id=second.transaction_id)
    assert tree_bytes(packs_root) == before
    assert read_group(packs_root).transaction_id == current.transaction_id
    assert read_group(packs_root).revision == current.revision
    assert first.transaction_id != current.transaction_id


def test_rollback_interruption_refuses_without_live_change_and_can_retry(
        repo: Repository, packs_root: Path, browser) -> None:
    from astrid.core.pack.transaction import rollback_group

    initial = install(repo, packs_root, **browser, refresh=accepted_refresh)
    historical_commit = read_pin(read_group(packs_root).paths["alpha"] / "PIN")["commit"]
    second = install(repo, packs_root, revision=change_browser(repo), **browser, refresh=accepted_refresh)
    before = tree_bytes(packs_root)
    current = read_group(packs_root)

    def interrupt(phase: str) -> None:
        if phase == "before_swap":
            raise RuntimeError("simulated rollback interruption")

    with pytest.raises(RuntimeError, match="simulated rollback interruption"):
        rollback_group(packs_root=packs_root, pack_ids=("alpha", "shared"),
                       transaction_id=second.transaction_id, boundary=interrupt)
    assert tree_bytes(packs_root) == before
    assert read_group(packs_root).transaction_id == current.transaction_id

    restored = rollback_group(packs_root=packs_root, pack_ids=("alpha", "shared"),
                              transaction_id=second.transaction_id)
    assert restored.revision == current.revision + 1
    assert read_pin(read_group(packs_root).paths["alpha"] / "PIN")["commit"] == historical_commit
    assert initial.transaction_id != second.transaction_id


def test_rollback_historical_pin_must_match_applicable_release_pin(repo: Repository, packs_root: Path) -> None:
    from astrid.core.pack.transaction import rollback_group

    first = install(repo, packs_root)
    historical_commit = read_pin(read_group(packs_root).paths["alpha"] / "PIN")["commit"]
    second = install(repo, packs_root, revision=repo.next_revision(), intent="update")
    before = group_bytes(packs_root)
    with pytest.raises(PackInstallError) as failure:
        rollback_group(packs_root=packs_root, pack_ids=("alpha", "shared"),
                       transaction_id=second.transaction_id,
                       release_pins={"alpha": {"commit": "f" * 40, "digest": "e" * 64}})
    assert failure.value.code == "pin_mismatch"
    assert group_bytes(packs_root) == before
    # With no applicable release pin, rollback preserves the exact historical
    # source PIN from the retired generation.
    rollback_group(packs_root=packs_root, pack_ids=("alpha", "shared"),
                   transaction_id=second.transaction_id)
    assert read_pin(read_group(packs_root).paths["alpha"] / "PIN")["commit"] == historical_commit
    assert first.transaction_id != second.transaction_id


def test_update_refuses_a_different_installed_source_pin(
        repo: Repository, packs_root: Path, tmp_path: Path) -> None:
    other_repo = Repository(tmp_path / "other-repository")
    install(repo, packs_root)
    before = group_bytes(packs_root)
    with pytest.raises(PackInstallError, match="conflicting source PIN") as failure:
        install_group(source=other_repo.bare, revision=other_repo.commit, packs=GROUP,
                      packs_root=packs_root, python_packages={}, intent="update")
    assert failure.value.code == "pin_mismatch"
    assert group_bytes(packs_root) == before


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


@pytest.fixture
def browser(repo: Repository):
    """Real Git packs and the existing pinned Node/esbuild fixture; no builder fake."""
    required = ("ASTRID_BUILD_TEST_NODE", "ASTRID_BUILD_TEST_NODE_VERSION", "ASTRID_BUILD_TEST_ESBUILD")
    assert all(os.environ.get(key) for key in required), (
        "Browser transaction tests require explicit pinned local Node/esbuild inputs: " + ", ".join(required))
    from tests.pack_builder.test_pack_build import toolchain

    builds = {}
    for pack_id in GROUP:
        extension = {"id": f"{pack_id}.extension", "version": "1.0.0", "label": "Élan",
                     "apiVersion": 1, "contributions": []}
        pack = repo.work / "packs" / pack_id
        (pack / "entry.tsx").write_text(
            "import {defineExtension} from '@astrid/editor-sdk';\n"
            f"export default defineExtension({{manifest: {json.dumps(extension)}, "
            "activate(ctx) { return ctx.effects.register('fixture', () => import('./lazy')); } });\n",
            encoding="utf-8")
        (pack / "lazy.ts").write_text("import icon from './icon.svg'; export default {icon, colour: 'blue'};\n")
        (pack / "icon.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"><title>blue</title></svg>\n')
        builds[pack_id] = {"entry": "entry.tsx", "extension_manifest": extension}
    repo.publish()
    return {"builds": builds, "toolchain": toolchain()}


def tree_bytes(root: Path):
    return {path.relative_to(root).as_posix(): path.read_bytes()
            for path in root.rglob("*") if path.is_file()}


def browser_records(snapshot, *, admitted_revision=None):
    """Check the wire contract from disk, independently of the writer/verifier."""
    records = {}
    for pack_id, path in snapshot.paths.items():
        record_bytes = (path / ".build/admission.json").read_bytes()
        record = json.loads(record_bytes)
        assert set(record) == {"version", "packId", "sourceDigest", "digest", "artifactManifestDigest",
                               "committedRevision", "transactionId"}
        assert type(record["version"]) is int and record["version"] == 1
        assert record["packId"] == pack_id
        assert record["sourceDigest"] == pack_digest(path) == read_pin(path / "PIN")["digest"]
        assert type(record["committedRevision"]) is int
        if admitted_revision is None:
            assert record["committedRevision"] == snapshot.revision
            assert record["transactionId"] == snapshot.transaction_id
        else:
            assert record["committedRevision"] == admitted_revision
        manifest_path = path / ".build" / f'{record["sourceDigest"]}.artifact-manifest.json'
        raw = manifest_path.read_bytes()
        artifact = json.loads(raw)
        canonical = lambda value: json.dumps(value, sort_keys=True, separators=(",", ":"),
                                             ensure_ascii=False).encode("utf-8")
        assert raw == canonical(artifact) and not raw.endswith(b"\n")
        assert b'\xc3\x89lan' in raw
        assert record_bytes == canonical(record)
        assert hashlib.sha256(raw).hexdigest() == record["artifactManifestDigest"]
        assert all(artifact[field] == record[field] for field in ("packId", "sourceDigest", "digest"))
        assert snapshot.admissions[pack_id] == {
            **{field: record[field] for field in ("packId", "sourceDigest", "digest", "artifactManifestDigest",
                                                 "committedRevision", "transactionId")},
            "artifact_manifest": artifact}
        files = artifact["files"]
        assert [f["path"] for f in files] == sorted(f["path"] for f in files)
        assert any(f["path"].startswith("chunks/") for f in files)
        assert any(f["path"].startswith("assets/") for f in files)
        expected = {manifest_path.relative_to(path).as_posix(), ".build/admission.json"}
        graph = []
        for item in files:
            assert item["disk"] == (f'.build/{record["sourceDigest"]}.js' if item["path"] == "index.js"
                                    else f'.build/{record["sourceDigest"]}/{item["path"]}')
            data = (path / item["disk"]).read_bytes()
            assert len(data) == item["size"] and type(item["size"]) is int
            assert hashlib.sha256(data).hexdigest() == item["sha256"]
            expected.add(item["disk"])
            if item["path"] == "index.js":
                assert item["sha256"] == record["digest"]
                assert data.startswith(f'/* astrid-graph-index-sha256:{artifact["graphIndexDigest"]} */\n'.encode())
            else:
                graph.append({"logical_path": item["path"], "sha256": item["sha256"],
                              "size": item["size"], "mime": item["mime"]})
        assert hashlib.sha256(canonical(graph)).hexdigest() == artifact["graphIndexDigest"]
        assert {p.relative_to(path).as_posix() for p in (path / ".build").rglob("*") if p.is_file()} == expected
        records[pack_id] = record
    return records


def change_browser(repo: Repository):
    for pack_id in GROUP:
        (repo.work / "packs" / pack_id / "lazy.ts").write_text(
            "import icon from './icon.svg'; export default {icon, colour: 'red'};\n")
    return repo.next_revision()


def test_browser_install_update_rollback_and_replay_publish_complete_records(
        repo: Repository, packs_root: Path, browser) -> None:
    notifications = []

    def refresh(**kwargs):
        snapshot = read_group(packs_root)
        records = browser_records(snapshot)
        assert snapshot.transaction_id == kwargs["transaction_id"]
        assert snapshot.revision == kwargs["committed_revision"]
        assert set(kwargs["changed_ids"]) == set(GROUP)
        notifications.append(records)
        return accepted_refresh(**kwargs)

    old_commit = repo.commit
    first = install(repo, packs_root, **browser, refresh=refresh)
    old_tree = tree_bytes(packs_root)
    old_records = browser_records(read_group(packs_root))
    second = install(repo, packs_root, revision=change_browser(repo), **browser, refresh=refresh)
    changed = browser_records(read_group(packs_root))
    assert all(changed[i]["digest"] != old_records[i]["digest"] for i in GROUP)
    assert tree_bytes(second.retired_root) == old_tree
    assert browser_records(read_retired_group(packs_root=packs_root,
                          transaction_id=second.transaction_id)) == old_records
    changed_tree = tree_bytes(packs_root)

    # This foundation's rollback route is reinstalling the prior pinned commit.
    # It restores exact B/M/files while recording the new commit revision/tx.
    restored = install(repo, packs_root, revision=old_commit, **browser, refresh=refresh)
    restored_records = browser_records(read_group(packs_root))
    assert tree_bytes(restored.retired_root) == changed_tree
    replay = install(repo, packs_root, revision=old_commit, **browser, refresh=refresh)
    replay_records = browser_records(read_group(packs_root))
    assert [first.revision, second.revision, restored.revision, replay.revision] == [1, 2, 3, 4]
    assert len({first.transaction_id, second.transaction_id, restored.transaction_id, replay.transaction_id}) == 4
    for pack_id in GROUP:
        for field in ("sourceDigest", "digest", "artifactManifestDigest"):
            assert old_records[pack_id][field] == restored_records[pack_id][field] == replay_records[pack_id][field]
    assert len(notifications) == 4  # Every callback validated complete disk records.
    assert not replay.refresh_pending


@pytest.mark.parametrize("fault", ["manifest", "record", "entry", "chunk", "extra", "source", "pin"])
@pytest.mark.parametrize("phase", ["built", "before_swap"])
def test_browser_tamper_refuses_publication_and_keeps_complete_old_tree(
        repo: Repository, packs_root: Path, browser, fault: str, phase: str) -> None:
    initial = install(repo, packs_root, **browser, refresh=accepted_refresh)
    before = tree_bytes(packs_root)
    calls = []

    def tamper(current):
        if current != phase:
            return
        candidates = [p for p in (packs_root.parent / ".astrid-retired").iterdir()
                      if (p / "alpha/.build/admission.json").exists()
                      and p.name != initial.transaction_id]
        assert len(candidates) == 1
        pack = candidates[0] / "alpha"
        record = json.loads((pack / ".build/admission.json").read_bytes())
        manifest_path = pack / ".build" / f'{record["sourceDigest"]}.artifact-manifest.json'
        if fault == "manifest":
            target = manifest_path
        elif fault == "record":
            target = pack / ".build/admission.json"
        elif fault in {"entry", "chunk"}:
            artifact = json.loads(manifest_path.read_bytes())
            item = next(f for f in artifact["files"] if (f["path"] == "index.js") == (fault == "entry"))
            target = pack / item["disk"]
        else:
            target = pack / {"extra": ".build/unlisted.json", "source": "payload.txt", "pin": "PIN"}[fault]
        if fault == "pin":
            pin = json.loads(target.read_bytes())
            pin["digest"] = "0" * 64
            target.write_text(json.dumps(pin, sort_keys=True, separators=(",", ":")) + "\n")
        else:
            target.write_bytes(target.read_bytes() + b"\n" if target.exists() else b"{}")

    with pytest.raises(PackInstallError):
        install(repo, packs_root, revision=change_browser(repo), **browser,
                boundary=tamper, refresh=lambda **kw: calls.append(kw))
    assert tree_bytes(packs_root) == before
    assert read_group(packs_root).transaction_id == initial.transaction_id
    assert not calls


@pytest.mark.parametrize("phase", ["prepared", "before_swap", "swapped", "before_refresh"])
def test_browser_process_death_preserves_complete_records_and_refresh_retry(
        repo: Repository, packs_root: Path, tmp_path: Path, browser, phase: str) -> None:
    initial = install(repo, packs_root, **browser, refresh=accepted_refresh)
    before = tree_bytes(packs_root)
    old_records = browser_records(read_group(packs_root))
    kill_child_at_boundary(repo, packs_root, tmp_path, change_browser(repo), phase, browser=browser)
    snapshot = read_group(packs_root)
    records = browser_records(snapshot)
    after_swap = phase in {"swapped", "before_refresh"}
    if after_swap:
        assert snapshot.revision == 2 and snapshot.transaction_id != initial.transaction_id
        assert all(records[i]["digest"] != old_records[i]["digest"] for i in GROUP)
        assert snapshot.refresh_pending
        retired = read_retired_group(packs_root=packs_root, transaction_id=snapshot.transaction_id)
        assert browser_records(retired) == old_records
        assert tree_bytes(next(iter(retired.paths.values())).parent) == before
    else:
        assert tree_bytes(packs_root) == before
        assert records == old_records
    seen = []

    def refresh(**kwargs):
        assert browser_records(read_group(packs_root)) == records
        seen.append(kwargs)
        return accepted_refresh(**kwargs)

    assert retry_refresh(packs_root=packs_root, refresh=refresh) is False
    assert len(seen) == int(after_swap)
    assert retry_refresh(packs_root=packs_root, refresh=refresh) is False
    assert len(seen) == int(after_swap)  # Accepted outbox does not notify twice.
    if not after_swap:
        completed = install(repo, packs_root, **browser, refresh=accepted_refresh)
        assert completed.revision == 2
        browser_records(read_group(packs_root))


def test_retained_browser_records_keep_their_admission_revision_and_replacement_clears_them(
        repo: Repository, packs_root: Path, browser) -> None:
    first = install(repo, packs_root, **browser, refresh=accepted_refresh)
    before = {i: tree_bytes(packs_root / i) for i in GROUP}
    repo.write_pack("extra", "ordinary-pack\n")
    repo.publish()
    install(repo, packs_root, packs={"extra": "packs/extra"}, refresh=accepted_refresh)
    snapshot = read_group(packs_root)
    assert snapshot.revision == 2
    assert set(snapshot.admissions) == set(GROUP)
    for pack_id in GROUP:
        assert tree_bytes(snapshot.paths[pack_id]) == before[pack_id]
        record = json.loads((snapshot.paths[pack_id] / ".build/admission.json").read_bytes())
        assert record["committedRevision"] == first.revision
        assert record["transactionId"] == first.transaction_id
    install_group(source=repo.bare, packs={}, packs_root=packs_root, python_packages={},
                  remove=("alpha", "extra"), refresh=accepted_refresh)
    snapshot = read_group(packs_root)
    assert snapshot.revision == 3 and set(snapshot.admissions) == {"shared"}
    browser_records(snapshot, admitted_revision=1)
    install(repo, packs_root, packs={"shared": "packs/shared"}, refresh=accepted_refresh)
    snapshot = read_group(packs_root)
    assert snapshot.revision == 4 and snapshot.admissions == {}
    assert not (packs_root / "shared/.build").exists()


@pytest.mark.parametrize("fault", ["compile", "tool_hash", "unsafe_import"])
def test_real_browser_build_refusal_preserves_prior_admitted_tree(
        repo: Repository, packs_root: Path, browser, fault: str) -> None:
    install(repo, packs_root, **browser, refresh=accepted_refresh)
    before = tree_bytes(packs_root)
    if fault == "tool_hash":
        browser["toolchain"]["esbuild_sha256"] = "0" * 64
    else:
        entry = repo.work / "packs/alpha/entry.tsx"
        entry.write_text("export default <broken" if fault == "compile" else
                         "import x from 'https://example.invalid/code.js'; export default x;\n")
        repo.publish()
    with pytest.raises(PackInstallError) as error:
        install(repo, packs_root, **browser)
    assert error.value.code == {"compile": "compile_failed", "tool_hash": "tool_mismatch",
                                "unsafe_import": "unsafe_import"}[fault]
    assert tree_bytes(packs_root) == before
    browser_records(read_group(packs_root))


def test_browser_refresh_refusal_retries_same_records_without_rebuild(
        repo: Repository, packs_root: Path, browser) -> None:
    calls = []

    def refuse(**kwargs):
        browser_records(read_group(packs_root))
        calls.append(kwargs)
        return {"version": 1, "accepted": False, "revision": kwargs["committed_revision"], "reason": "closed"}

    result = install(repo, packs_root, **browser, refresh=refuse)
    assert result.refresh_pending
    records = browser_records(read_group(packs_root))
    before = {i: tree_bytes(packs_root / i) for i in GROUP}

    def accept(**kwargs):
        assert browser_records(read_group(packs_root)) == records
        calls.append(kwargs)
        return accepted_refresh(**kwargs)

    assert retry_refresh(packs_root=packs_root, refresh=accept) is False
    assert calls[0] == calls[1]
    assert all(tree_bytes(packs_root / i) == before[i] for i in GROUP)
    assert retry_refresh(packs_root=packs_root, refresh=accept) is False
    assert len(calls) == 2


@pytest.mark.parametrize("location", ["installed", "candidate"])
@pytest.mark.parametrize("fault", ["manifest", "record", "chunk", "missing", "future_revision", "identity",
                                  "transactionId", "committedRevision"])
def test_unrelated_update_refuses_corrupt_retained_browser_admission(
        repo: Repository, packs_root: Path, browser, location: str, fault: str) -> None:
    install(repo, packs_root, **browser, refresh=accepted_refresh)
    # Give retained records a real historical epoch distinct from revision 1.
    initial = install(repo, packs_root, **browser, refresh=accepted_refresh)
    repo.write_pack("extra", "ordinary-pack\n")
    repo.publish()

    def corrupt(pack):
        record_path = pack / ".build/admission.json"
        record = json.loads(record_path.read_bytes())
        manifest_path = pack / ".build" / f'{record["sourceDigest"]}.artifact-manifest.json'
        if fault == "missing":
            record_path.unlink()
        elif fault == "manifest":
            manifest_path.write_bytes(manifest_path.read_bytes() + b"\n")
        elif fault == "record":
            record_path.write_bytes(record_path.read_bytes() + b"\n")
        elif fault in {"future_revision", "identity", "transactionId", "committedRevision"}:
            field = {"future_revision": "committedRevision", "identity": "packId"}.get(fault, fault)
            record[field] = {"future_revision": 100, "identity": "impostor",
                             "transactionId": "0" * 32, "committedRevision": 1}[fault]
            record_path.write_text(json.dumps(record, sort_keys=True, separators=(",", ":")))
        else:
            artifact = json.loads(manifest_path.read_bytes())
            chunk = next(f for f in artifact["files"] if f["path"].startswith("chunks/"))
            (pack / chunk["disk"]).write_bytes(b"corrupt")

    if location == "installed":
        corrupt(packs_root / "alpha")
    before = tree_bytes(packs_root)
    notifications = []

    def boundary(phase):
        if location == "candidate" and phase == "before_swap":
            candidate = next(p for p in (packs_root.parent / ".astrid-retired").iterdir()
                             if (p / "extra/pack.yaml").exists())
            corrupt(candidate / "alpha")

    with pytest.raises(PackInstallError):
        install(repo, packs_root, packs={"extra": "packs/extra"}, boundary=boundary,
                refresh=lambda **kw: notifications.append(kw))
    assert tree_bytes(packs_root) == before
    assert read_group(packs_root).revision == 2
    assert read_group(packs_root).transaction_id == initial.transaction_id
    assert not notifications
