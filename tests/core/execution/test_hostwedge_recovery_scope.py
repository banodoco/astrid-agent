"""A stale terminal request must not monopolize host admission."""
from __future__ import annotations

# Pytest fixtures are deliberately imported and consumed by argument name.
# ruff: noqa: F811
import hashlib
import io
import json
import multiprocessing
import os
import threading
import time
from pathlib import Path

import pytest

from astrid.core.execution import generic_host
from astrid.core.execution.generic_host import GenericPackHost, HostError
from astrid.sdk import storage_root
from astrid.sdk.storage_ownership import reclaim
from astrid_runtime_client.generated import ApiError
from tests.core.execution.test_bug14_settlement_liveness import settlement_case  # noqa: F401
from tests.core.execution.test_terminal_publication_replay import _saved_failure
from tests.core.generation.test_engine_custody import (
    _call,
    _client,
    _request,
    _wait,
)
from tests.core.generation.test_engine_custody import (
    session as engine_session,  # noqa: F401
)
from tests.test_generic_host_result_json import _success_boundary, host_case  # noqa: F401


@pytest.fixture(autouse=True)
def private_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir(mode=0o700, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    for name in ("ASTRID_RUNTIME_DATA_ROOT", "ASTRID_STATE_HOME", "XDG_STATE_HOME",
                 "XDG_RUNTIME_DIR", "XDG_CACHE_HOME"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(storage_root, "CONFIG_PATH", home / "no-install-config.json")


def _foreign_lstat(monkeypatch, target):
    original = Path.lstat

    def lstat(path, *args, **kwargs):
        metadata = original(path, *args, **kwargs)
        if path == target:
            fields = list(metadata)
            fields[4] = os.getuid() + 1
            return os.stat_result(fields)
        return metadata

    monkeypatch.setattr(Path, "lstat", lstat)


@pytest.mark.parametrize("selection", ["data-root", "persisted", "install-config", "xdg", "home"])
def test_private_attempt_location_precedence(tmp_path, monkeypatch, selection):
    data = tmp_path / "data"
    xdg = tmp_path / "runtime"
    monkeypatch.setattr(generic_host.tempfile, "gettempdir", lambda: str(tmp_path))
    if selection != "home":
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(xdg))
    if selection == "data-root":
        monkeypatch.setenv("ASTRID_RUNTIME_DATA_ROOT", str(data))
    elif selection == "persisted":
        state = Path.home() / ".local" / "state" / "astrid"
        state.mkdir(parents=True)
        (state / "skills.json").write_text(json.dumps({"setup_selection": {"support_root": str(data)}}))
    elif selection == "install-config":
        storage_root.CONFIG_PATH.write_text(json.dumps({"version": 1, "data_root": str(data)}))
    host = GenericPackHost(pack_roots=[], executor_id="location-worker")
    root = host._allocate_attempt_root("task", "attempt")
    private = data if selection in {"data-root", "persisted", "install-config"} else (
        xdg if selection == "xdg" else Path.home() / ".cache" / "astrid")
    digest = hashlib.sha256(host.executor_id.encode()).hexdigest()[:16]
    assert root.parent == private / "attempts" / digest
    for path in (private, private / "attempts", root.parent, root):
        assert path.lstat().st_mode & 0o777 == 0o700
        assert path.lstat().st_uid == os.getuid()


@pytest.mark.parametrize("damage", ["owner", "mode", "symlink"])
def test_squatted_private_base_refuses_startup(host_case, tmp_path, monkeypatch, damage):
    # Product change: private-state failures now refuse startup; no temp fallback.
    case = host_case
    case.host.attempt_root = None
    squat = case.host._default_attempt_base()
    if damage == "owner":
        _foreign_lstat(monkeypatch, squat)
    elif damage == "mode":
        squat.chmod(0o777)
    else:
        target = tmp_path / "squat-target"
        squat.rename(target)
        squat.symlink_to(target, target_is_directory=True)
    _success_boundary(case, monkeypatch)
    monkeypatch.setattr(case.host, "claim_once", lambda: case.host.run_task(case.task, lease_token="lease-result"))
    with pytest.raises(HostError) as error:
        _bounded_run(case.host, lambda: case.host.run(once=True))
    assert str(squat) in str(error.value)
    assert "remove" in str(error.value).lower() or "repair" in str(error.value).lower()
    assert not case.runtime.settlements
    with pytest.raises(HostError):
        case.host._allocate_attempt_root("next", "next")
    assert not hasattr(case.host, "_fallback_attempt_base")


@pytest.mark.parametrize("damage", ["owner", "mode", "symlink"])
def test_unsafe_private_ancestor_refuses_without_following(tmp_path, monkeypatch, damage):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(private))
    if damage == "owner":
        _foreign_lstat(monkeypatch, private)
    elif damage == "mode":
        private.chmod(0o777)
    else:
        target = tmp_path / "outside"
        private.rename(target)
        private.symlink_to(target, target_is_directory=True)
    host = GenericPackHost(pack_roots=[])
    with pytest.raises(HostError, match=str(private)):
        host._allocate_attempt_root("task", "attempt")
    assert not (private / "attempts").exists()
    assert not hasattr(host, "_fallback_attempt_base")


def test_private_spool_is_outside_tmp_reclaim(tmp_path, monkeypatch):
    monkeypatch.setattr(generic_host.tempfile, "gettempdir", lambda: str(tmp_path))
    host = GenericPackHost(pack_roots=[])
    stale = host._allocate_attempt_root("old", "old")
    live = host._allocate_attempt_root("live", "live")
    then = time.time() - 3 * 86400
    os.utime(stale, (then, then))
    os.utime(stale.parent, (then, then))
    report = reclaim(tmp_path / "data", temporary_root=tmp_path, cache_root=tmp_path / "cache")
    attempts = next(row for row in report["roots"] if row["name"] == "attempt-roots")
    assert attempts["count"] == 0
    assert stale.is_dir() and live.is_dir() and live.parent.is_dir()
    assert host._allocate_attempt_root("new", "new").parent == live.parent


def _bounded_run(host, call):
    errors = []
    finished = threading.Event()

    def run():
        try:
            call()
        except Exception as exc:  # noqa: BLE001 - relay worker failures to pytest
            errors.append(exc)
        finally:
            finished.set()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    try:
        assert finished.wait(2), "stale recovery blocked generation admission"
        if errors:
            raise errors[0]
    finally:
        host._shutdown.set()
        thread.join(timeout=2)
        assert not thread.is_alive()


def _unknown_request(root, executor):
    root.mkdir(parents=True)
    request = {"operation": "fail_attempt", "identifier": "unknown-attempt",
               "task_id": "unknown-task", "executor_id": executor,
               "idempotency_key": "stale-failure", "body": {
                   "lease_id": "old-lease", "fence": 1, "runtime_epoch": 1,
                   "error": {"code": "old_failure"}, "outputs": [], "result": {}}}
    path = root / "fail-request.json"
    path.write_text(json.dumps(request))
    return path


@pytest.mark.parametrize("configured", [False, True], ids=["shared-tmp", "configured-root"])
def test_foreign_stale_request_does_not_block_generation(host_case, tmp_path, monkeypatch, capsys, configured):
    case = host_case
    temporary = tmp_path / "temporary"
    temporary.mkdir()
    monkeypatch.setattr(generic_host.tempfile, "gettempdir", lambda: str(temporary))
    case.host.attempt_root = None
    if configured:
        case.host.attempt_base = temporary / "configured"
        stale = case.host.attempt_base / "stale"
    else:
        stale = temporary / "astrid-attempt-stale"
    path = _unknown_request(stale, case.host.executor_id)
    before = path.read_bytes()
    reads = []
    task = case.runtime.task

    def read(task_id):
        reads.append(task_id)
        if task_id == "unknown-task":
            raise ApiError(404, "not_found", "task belongs to another Runtime")
        return task(task_id)

    monkeypatch.setattr(case.runtime, "task", read)
    _success_boundary(case, monkeypatch)
    monkeypatch.setattr(case.host, "claim_once", lambda: case.host.run_task(case.task, lease_token="lease-result"))
    _bounded_run(case.host, lambda: case.host.run(once=True))
    assert len(case.runtime.settlements) == 1
    assert path.read_bytes() == before
    assert sorted(p.name for p in stale.iterdir()) == ["fail-request.json"]
    assert reads.count("unknown-task") == 1
    assert capsys.readouterr().err.count("generic host recovery skipped") == 1


def test_default_attempt_namespaces_are_isolated_and_retained(tmp_path, monkeypatch):
    monkeypatch.setattr(generic_host.tempfile, "gettempdir", lambda: str(tmp_path))
    first = GenericPackHost(pack_roots=[])
    second = GenericPackHost(pack_roots=[], executor_id="second-worker")
    left = first._allocate_attempt_root("task", "attempt")
    right = second._allocate_attempt_root("task", "attempt")
    assert left.parent != right.parent
    assert left.name.startswith("astrid-attempt-")
    assert right.name.startswith("astrid-attempt-")
    assert left.parent.parent == right.parent.parent == Path.home() / ".cache" / "astrid" / "attempts"
    (left / "fail-request.json").write_text("foreign, unreadable evidence")
    reads = []

    class Runtime:
        def task(self, task_id):
            reads.append(task_id)
            raise AssertionError("cross-host read")

    second.client = Runtime()
    second._recover_terminal_requests()
    assert not reads
    assert (left / "fail-request.json").read_text() == "foreign, unreadable evidence"
    then = time.time() - 3 * 86400
    os.utime(left.parent, (then, then))
    os.utime(right.parent, (then, then))
    os.utime(left, (then, then))
    report = reclaim(tmp_path / "data", temporary_root=tmp_path, cache_root=tmp_path / "cache")
    attempts = next(row for row in report["roots"] if row["name"] == "attempt-roots")
    assert attempts["expired_count"] == 0
    assert left.exists() and right.exists()


@pytest.mark.parametrize("foreign", ["executor", "epoch"])
def test_positive_foreign_identity_preserves_all_files(settlement_case, tmp_path, monkeypatch, capsys, foreign):
    case = settlement_case
    root, request = _saved_failure(case, tmp_path)
    if foreign == "executor":
        request["executor_id"] = "another-worker"
    else:
        request["body"]["runtime_epoch"] += 100
    path = root / "fail-request.json"
    path.write_text(json.dumps(request))
    before = {p.name: p.read_bytes() for p in root.iterdir()}
    _bounded_run(case.host, case.host._recover_terminal_requests)
    assert not case.failures
    assert case.service.task(case.task_id)["task"]["status"] == "running"
    assert {p.name: p.read_bytes() for p in root.iterdir()} == before
    assert capsys.readouterr().err.count("generic host recovery skipped") == 1


@pytest.mark.parametrize("damage", ["unreadable", "missing-owner"])
def test_uncertain_own_identity_is_blocked_without_stopping_host(settlement_case, tmp_path, monkeypatch, capsys, damage):
    case = settlement_case
    root, request = _saved_failure(case, tmp_path)
    if damage == "unreadable":
        content = "{broken JSON"
    else:
        request.pop("executor_id")
        content = json.dumps(request)
    (root / "fail-request.json").write_text(content)
    monkeypatch.setattr(generic_host, "_TERMINAL_RECONCILE_POLL_SECONDS", 0)
    claimed = []
    monkeypatch.setattr(case.host, "claim_once", lambda: claimed.append("claimed"))
    _bounded_run(case.host, lambda: case.host.run(once=True))
    assert claimed == ["claimed"]
    assert case.host.recovery_blocked[0]["path"] == str(root / "fail-request.json")
    assert str(root / "fail-request.json") in case.host.recovery_blocked[0]["recovery_step"]
    assert (root / "fail-request.json").read_text() == content
    assert "generic host recovery could not read" in capsys.readouterr().err
    assert not case.failures


def test_known_unrecoverable_evidence_cedes_to_runtime_failure(settlement_case, tmp_path, monkeypatch, capsys):
    case = settlement_case
    root, request = _saved_failure(case, tmp_path)
    original = case.host.client.generated.fail_attempt
    calls = []

    def refuse_evidence(attempt_id, **body):
        calls.append(body)
        if "result" in body:
            raise OSError("evidence endpoint unavailable")
        return original(attempt_id, **body)

    monkeypatch.setattr(case.host.client.generated, "fail_attempt", refuse_evidence)
    monkeypatch.setattr(generic_host, "_TERMINAL_RECONCILE_POLL_SECONDS", 0)
    _bounded_run(case.host, case.host._recover_terminal_requests)
    stored = case.service.task(case.task_id)["task"]
    assert stored["status"] == "failed"
    assert stored["result"]["error"]["code"] == "reconciliation_required"
    assert "recovery exhausted" in stored["result"]["error"]["message"]
    assert len(calls) == generic_host._TERMINAL_RECOVERY_MAX_ATTEMPTS + 1
    assert json.loads((root / "fail-request.json").read_text()) == request
    assert "generic host recovery exhausted" in capsys.readouterr().err
    case.host._shutdown.clear()
    case.host._recover_terminal_requests()
    assert len(calls) == generic_host._TERMINAL_RECOVERY_MAX_ATTEMPTS + 1


def test_recovery_lost_ack_reads_durable_evidence_before_any_retry(settlement_case, tmp_path, monkeypatch):
    case = settlement_case
    _, request = _saved_failure(case, tmp_path)
    generated = case.host.client.generated
    original = generated.fail_attempt
    calls = []
    readbacks = []
    verify = case.host.client._verify_terminal_evidence_readback

    def lost_ack(attempt_id, **body):
        calls.append(body)
        original(attempt_id, **body)
        raise OSError("committed but ACK lost")

    def readback(*args, **kwargs):
        readbacks.append(args)
        return verify(*args, **kwargs)

    monkeypatch.setattr(generated, "fail_attempt", lost_ack)
    monkeypatch.setattr(case.host.client, "_verify_terminal_evidence_readback", readback)
    _bounded_run(case.host, case.host._recover_terminal_requests)
    assert len(calls) == 1
    assert readbacks
    assert readbacks[0][1]["error"] == request["body"]["error"]
    assert case.service.task(case.task_id)["task"]["status"] == "failed"


def test_exhausted_cede_is_visible_and_replayed_exactly_after_restart(settlement_case, tmp_path, monkeypatch):
    case = settlement_case
    root, request = _saved_failure(case, tmp_path)
    generated = case.host.client.generated
    original = generated.fail_attempt
    calls = []

    def outage(attempt_id, **body):
        calls.append(body)
        raise OSError("Runtime unavailable")

    monkeypatch.setattr(generated, "fail_attempt", outage)
    monkeypatch.setattr(generic_host, "_TERMINAL_RECONCILE_POLL_SECONDS", 0)
    claimed = []
    monkeypatch.setattr(case.host, "claim_once", lambda: claimed.append("claimed"))
    _bounded_run(case.host, lambda: case.host.run(once=True))
    assert claimed == ["claimed"]
    assert case.host.recovery_blocked[0]["path"] == str(root / "fail-request.json")
    assert len(calls) == 2 * generic_host._TERMINAL_RECOVERY_MAX_ATTEMPTS
    assert case.service.task(case.task_id)["task"]["status"] == "running"
    assert json.loads((root / "fail-request.json").read_text()) == request
    cede = calls[-1]
    assert "result" not in cede and cede["error"]["code"] == "reconciliation_required"
    saved = root / "fail-request.json.reconciliation.json"
    persisted = saved.read_bytes()

    def restored(attempt_id, **body):
        calls.append(body)
        return original(attempt_id, **body)

    monkeypatch.setattr(generated, "fail_attempt", restored)
    restarted = GenericPackHost(pack_roots=[], client=case.host.client,
                                executor_id=case.host.executor_id, attempt_root=root)
    _bounded_run(restarted, restarted._recover_terminal_requests)
    assert calls[-1] == cede
    assert len(calls) == 2 * generic_host._TERMINAL_RECOVERY_MAX_ATTEMPTS + 1
    assert saved.read_bytes() == persisted
    assert case.service.task(case.task_id)["task"]["status"] == "failed"


def test_success_response_with_unrelated_result_requires_real_failure_readback(settlement_case, tmp_path, monkeypatch):
    case = settlement_case
    _, request = _saved_failure(case, tmp_path)
    generated = case.host.client.generated
    original = generated.fail_attempt
    calls = []

    def uncertain_response(attempt_id, **body):
        calls.append(body)
        if len(calls) == 1:
            return {"task": {**generated.get_task(case.task_id), "result": {"unrelated": True}}}
        return original(attempt_id, **body)

    monkeypatch.setattr(generated, "fail_attempt", uncertain_response)
    monkeypatch.setattr(generic_host, "_TERMINAL_RECONCILE_POLL_SECONDS", 0)
    _bounded_run(case.host, case.host._recover_terminal_requests)
    assert len(calls) == 2 and calls[0] == calls[1]
    stored = case.service.task(case.task_id)["task"]
    assert stored["status"] == "failed"
    assert stored["result"]["error"] == request["body"]["error"]


def _default_spool(case, tmp_path, monkeypatch):
    _, request = _saved_failure(case, tmp_path)
    case.host.attempt_root = None
    monkeypatch.setattr(generic_host.tempfile, "gettempdir", lambda: str(tmp_path))
    root = case.host._allocate_attempt_root(case.task_id, case.attempt["attempt_id"])
    (root / "fail-request.json").write_text(json.dumps(request))
    return root, request


def test_default_spool_survives_same_executor_restart(settlement_case, tmp_path, monkeypatch):
    case = settlement_case
    root, request = _default_spool(case, tmp_path, monkeypatch)
    restarted = GenericPackHost(pack_roots=[], client=case.host.client, executor_id=case.host.executor_id)
    _bounded_run(restarted, restarted._recover_terminal_requests)
    assert len(case.failures) == 1
    assert case.service.task(case.task_id)["task"]["status"] == "failed"
    assert json.loads((root / "fail-request.json").read_text()) == request
    assert restarted._allocate_attempt_root("next", "next").parent == root.parent
    assert root.parent.stat().st_mode & 0o777 == 0o700
    assert root.parent.stat().st_uid == os.getuid()


def test_default_spool_is_invisible_to_different_executor(settlement_case, tmp_path, monkeypatch):
    case = settlement_case
    root, _ = _default_spool(case, tmp_path, monkeypatch)
    before = {p.name: p.read_bytes() for p in root.iterdir()}
    reads = []
    task = case.host.client.task
    monkeypatch.setattr(case.host.client, "task", lambda task_id: (reads.append(task_id), task(task_id))[1])
    restarted = GenericPackHost(pack_roots=[], client=case.host.client, executor_id="different-worker")
    _bounded_run(restarted, restarted._recover_terminal_requests)
    assert not case.failures and not reads
    assert {p.name: p.read_bytes() for p in root.iterdir()} == before
    assert restarted._allocate_attempt_root("next", "next").parent != root.parent



@pytest.mark.parametrize("damage", ["mode", "symlink", "owner"])
@pytest.mark.parametrize("use", ["allocate", "recover"])
def test_default_base_refuses_unsafe_directory(tmp_path, monkeypatch, capsys, damage, use):
    monkeypatch.setattr(generic_host.tempfile, "gettempdir", lambda: str(tmp_path))
    host = GenericPackHost(pack_roots=[], executor_id="private-worker", client=object())
    base = host._allocate_attempt_root("task", "attempt").parent
    if damage == "mode":
        base.chmod(0o755)
    elif damage == "symlink":
        moved = tmp_path / "moved"
        base.rename(moved)
        base.symlink_to(moved, target_is_directory=True)
    else:
        _foreign_lstat(monkeypatch, base)
    with pytest.raises(HostError, match="private.*attempt base"):
        host._ensure_private_attempt_directory(base)
    with pytest.raises(HostError, match=str(base)):
        if use == "allocate":
            host._allocate_attempt_root("next", "next")
        else:
            host._recover_terminal_requests()
    assert not hasattr(host, "_fallback_attempt_base")


def test_legacy_flat_own_request_survives_restart(settlement_case, tmp_path, monkeypatch):
    case = settlement_case
    root, _ = _saved_failure(case, tmp_path)
    legacy = tmp_path / "astrid-attempt-legacy"
    root.rename(legacy)
    monkeypatch.setattr(generic_host.tempfile, "gettempdir", lambda: str(tmp_path))
    restarted = GenericPackHost(pack_roots=[], client=case.host.client, executor_id=case.host.executor_id)
    _bounded_run(restarted, restarted._recover_terminal_requests)
    assert len(case.failures) == 1
    assert case.service.task(case.task_id)["task"]["status"] == "failed"


def test_reused_default_base_ages_children_individually(tmp_path, monkeypatch):
    monkeypatch.setattr(generic_host.tempfile, "gettempdir", lambda: str(tmp_path))
    legacy_base = tmp_path / "astrid-attempt-old-base"
    stale = legacy_base / "astrid-attempt-old"
    live = legacy_base / "astrid-attempt-live"
    stale.mkdir(parents=True, mode=0o700)
    live.mkdir(mode=0o700)
    assert stale.parent == live.parent
    then = time.time() - 3 * 86400
    os.utime(stale, (then, then))
    os.utime(stale.parent, (then, then))
    report = reclaim(tmp_path / "data", temporary_root=tmp_path, cache_root=tmp_path / "cache")
    attempts = next(row for row in report["roots"] if row["name"] == "attempt-roots")
    assert attempts["expired_count"] == 1
    assert not stale.exists() and live.is_dir() and live.parent.is_dir()
    fresh = live.parent / "astrid-attempt-new"
    fresh.mkdir(mode=0o700)
    assert fresh.parent == live.parent

def test_corrupt_identity_and_foreign_request_allow_new_generation(host_case, tmp_path, monkeypatch, capsys):
    case = host_case
    case.host.attempt_root = None
    case.host.attempt_base = tmp_path / "attempts"
    corrupt = _unknown_request(case.host.attempt_base / "broken", case.host.executor_id)
    corrupt.write_text('{"executor_id":')
    foreign = _unknown_request(case.host.attempt_base / "foreign", "foreign-worker")
    before = {p: p.read_bytes() for p in (corrupt, foreign)}
    _success_boundary(case, monkeypatch)
    monkeypatch.setattr(case.host, "claim_once", lambda: case.host.run_task(case.task, lease_token="lease-result"))
    _bounded_run(case.host, lambda: case.host.run(once=True))
    assert len(case.runtime.settlements) == 1
    assert len(case.host.recovery_blocked) == 1
    blocked = case.host.recovery_blocked[0]
    assert blocked["path"] == str(corrupt)
    assert "inspect" in blocked["recovery_step"].lower() and str(corrupt) in blocked["recovery_step"]
    assert {p: p.read_bytes() for p in before} == before
    assert "recovery_blocked" in capsys.readouterr().err


def test_corrupt_identity_never_writes_runtime_and_host_starts(settlement_case, tmp_path, monkeypatch, engine_session):
    case = settlement_case
    root, request = _saved_failure(case, tmp_path)
    request["body"]["fence"] = "invalid"
    marker = root / "fail-request.json"
    marker.write_text(json.dumps(request))
    before = marker.read_bytes()
    old_task = case.service.task(case.task_id)["task"]
    assert old_task["attempt_id"] == case.attempt["attempt_id"]
    assert old_task["lease_fence"] == case.attempt["fence"]
    assert old_task["lease_token"] == case.attempt["lease_id"]
    client, payload, recipe, engine_root, mode, started = engine_session
    owner = _call(client, payload)["owner_id"]
    trace, release = engine_root / "execution-order", engine_root / "release-old"
    old_code = f'''
import time
from pathlib import Path
trace=Path({str(trace)!r})
with trace.open('a') as stream: stream.write('old:start'+chr(10))
while not Path({str(release)!r}).exists(): time.sleep(0.01)
with trace.open('a') as stream: stream.write('old:end'+chr(10))
'''
    errors, results, claimed = [], [], []

    def old_run():
        try:
            results.append(client.run(_request(engine_root, old_code, seconds=20)))
        except BaseException as exc:  # noqa: BLE001 - relay worker failures to the test
            errors.append(exc)

    old = threading.Thread(target=old_run, daemon=True)
    old.start()
    _wait(lambda: trace.exists() or errors)
    assert not errors
    assert len(results) == 0, results
    # A fresh host has no session/preparation from the dead incarnation. Its
    # new claim uses the SAME still-warm supervisor, not a second engine.
    restarted = GenericPackHost(pack_roots=[], client=case.host.client,
                                executor_id=case.host.executor_id, attempt_root=root)
    replacement = _client(client.identity)
    queued = threading.Event()
    new_started = engine_root / "new-started"
    new_code = f"from pathlib import Path; Path({str(new_started)!r}).write_text('yes'); " + (
        f"stream=Path({str(trace)!r}).open('a'); stream.write('new:start'+chr(10)); stream.close()")

    def claim():
        claimed.append("claimed")
        queued.set()
        results.append(replacement.run(_request(engine_root, new_code, seconds=20)))

    monkeypatch.setattr(restarted, "claim_once", claim)

    def new_host():
        try:
            restarted.run(once=True)
        except BaseException as exc:  # noqa: BLE001 - relay worker failures to the test
            errors.append(exc)

    new = threading.Thread(target=new_host, daemon=True)
    try:
        new.start()
        assert queued.wait(5), "new claim did not pass the blocked recovery marker"
        assert not new_started.exists() and not release.exists()
        # The negative interval lets the real supervisor receive the second
        # socket request while the first still holds its work lock.
        time.sleep(0.2)
        assert not new_started.exists() and old.is_alive() and new.is_alive()
        assert not case.failures and not case.cancels and not case.writes
        assert restarted.recovery_blocked[0]["path"] == str(marker)
    finally:
        release.write_text("release")
        old.join(10)
        new.join(10)
        restarted._shutdown.set()
    assert not old.is_alive() and not new.is_alive() and not errors
    assert len(results) == 2 and all(result.exit_code == 0 for result in results)
    assert trace.read_text().splitlines() == ["old:start", "old:end", "new:start"]
    assert _call(replacement, payload, "health")["owner_id"] == owner
    assert len(started.read_text().splitlines()) == 1
    assert claimed == ["claimed"]
    assert not case.failures and not case.cancels and not case.writes
    assert marker.read_bytes() == before
    assert restarted.recovery_blocked[0]["path"] == str(marker)
    assert str(marker) in restarted.recovery_blocked[0]["recovery_step"]
    stored = case.service.task(case.task_id)["task"]
    assert stored["status"] == "running"
    for key in ("attempt_id", "lease_fence", "lease_token", "lease_expires_at", "executor_id"):
        assert stored[key] == old_task[key], key


@pytest.mark.parametrize("damage", ["request-result", "receipt-json", "receipt-type", "request-error"])
def test_readable_identity_corrupt_result_cedes_and_host_starts(settlement_case, tmp_path, monkeypatch, damage):
    case = settlement_case
    root, request = _saved_failure(case, tmp_path)
    if damage == "request-result":
        request["body"]["result"] = ["invalid result"]
    elif damage == "request-error":
        request["body"]["error"] = "invalid error"
    else:
        (root / "result.json").write_text("{broken" if damage == "receipt-json" else "[]")
    marker = root / "fail-request.json"
    marker.write_text(json.dumps(request))
    before = marker.read_bytes()
    receipt_before = (root / "result.json").read_bytes() if damage.startswith("receipt-") else None
    claimed = []
    monkeypatch.setattr(case.host, "claim_once", lambda: claimed.append("claimed"))
    _bounded_run(case.host, lambda: case.host.run(once=True))
    assert claimed == ["claimed"]
    if receipt_before is not None:
        assert (root / "result.json").read_bytes() == receipt_before
    stored = case.service.task(case.task_id)["task"]
    assert stored["status"] == "failed"
    assert stored["result"]["error"]["code"] == "reconciliation_required"
    assert len(case.failures) == 1
    assert "result" not in case.failures[0][1] and "outputs" not in case.failures[0][1]
    assert marker.read_bytes() == before
    assert (root / "fail-request.json.reconciliation.json").is_file()
    assert not case.host.recovery_blocked


def test_exhausted_recovery_is_listed_and_host_starts(settlement_case, tmp_path, monkeypatch):
    case = settlement_case
    root, _ = _saved_failure(case, tmp_path)
    original_task = case.service.task(case.task_id)["task"]
    calls, claimed = [], []
    def outage(attempt_id, **body):
        calls.append(body)
        raise OSError("Runtime unavailable")
    monkeypatch.setattr(case.host.client.generated, "fail_attempt", outage)
    monkeypatch.setattr(generic_host, "_TERMINAL_RECONCILE_POLL_SECONDS", 0)
    def next_claim():
        # The real same-engine overlap boundary is exercised in
        # test_corrupt_identity_never_writes_runtime_and_host_starts above.
        # Here pin the cede/outage path: admission must not retry or fail the
        # blocked attempt, clear its lease, or lose its original Runtime fence.
        assert case.host.recovery_blocked[0]["path"] == str(root / "fail-request.json")
        assert len(calls) == 2 * generic_host._TERMINAL_RECOVERY_MAX_ATTEMPTS
        stored = case.service.task(case.task_id)["task"]
        assert stored["status"] == "running"
        for key in ("attempt_id", "lease_fence", "lease_token", "lease_expires_at", "executor_id"):
            assert stored[key] == original_task[key], key
        assert not case.failures and not case.cancels and not case.writes
        claimed.append("claimed")

    monkeypatch.setattr(case.host, "claim_once", next_claim)
    _bounded_run(case.host, lambda: case.host.run(once=True))
    assert claimed == ["claimed"]
    assert len(calls) == 2 * generic_host._TERMINAL_RECOVERY_MAX_ATTEMPTS
    assert case.host.recovery_blocked[0]["path"] == str(root / "fail-request.json")
    assert "Runtime unavailable" in case.host.recovery_blocked[0]["reason"]
    assert case.service.task(case.task_id)["task"]["status"] == "running"

    assert not case.failures and not case.cancels and not case.writes
    stored = case.service.task(case.task_id)["task"]
    for key in ("attempt_id", "lease_fence", "lease_token", "lease_expires_at", "executor_id"):
        assert stored[key] == original_task[key], key


@pytest.mark.parametrize("contents", ["{broken", "{}", "[]", '{"body": null}'])
def test_unreadable_reconciliation_sidecar_is_blocked(settlement_case, tmp_path, monkeypatch, contents):
    case = settlement_case
    root, _ = _saved_failure(case, tmp_path)
    sidecar = root / "fail-request.json.reconciliation.json"
    sidecar.write_text(contents)
    claimed = []
    monkeypatch.setattr(generic_host, "_TERMINAL_RECONCILE_POLL_SECONDS", 0)
    monkeypatch.setattr(case.host, "claim_once", lambda: claimed.append("claimed"))
    _bounded_run(case.host, lambda: case.host.run(once=True))
    assert claimed == ["claimed"]
    assert not case.failures and not case.cancels
    assert sidecar.read_text() == contents
    assert case.host.recovery_blocked[0]["path"] == str(sidecar)


def test_blocked_recovery_is_exposed_in_ready_status(settlement_case, tmp_path, monkeypatch):
    case = settlement_case
    root, _ = _saved_failure(case, tmp_path)
    (root / "fail-request.json").write_text("{broken")
    status_path = tmp_path / "host-ready.json"
    case.host._ready_marker_path = status_path
    case.host._ready_marker_context = {"status": "ready"}
    claimed = []
    monkeypatch.setattr(case.host, "claim_once", lambda: claimed.append("claimed"))
    _bounded_run(case.host, lambda: case.host.run(once=True))
    status = json.loads(status_path.read_text())
    assert status["status"] == "ready"
    assert status["recovery_blocked"] == case.host.recovery_blocked
    assert len(status["recovery_blocked"]) == 1
    assert claimed == ["claimed"] and not case.failures


def test_missing_runtime_epoch_remains_host_wide_error(settlement_case, tmp_path, monkeypatch):
    case = settlement_case
    _saved_failure(case, tmp_path)
    claimed = []
    monkeypatch.setattr(case.host.client, "health", lambda: {})
    monkeypatch.setattr(case.host, "claim_once", lambda: claimed.append("claimed"))
    with pytest.raises(HostError, match="Runtime recovery health has no epoch"):
        _bounded_run(case.host, lambda: case.host.run(once=True))
    assert not claimed and not case.failures


@pytest.mark.parametrize("damage", ["owner", "group-writable", "world-writable", "symlink", "file"])
@pytest.mark.parametrize("nested", [False, True])
def test_unsafe_legacy_root_never_opens_markers(settlement_case, tmp_path, monkeypatch, capsys, damage, nested):
    case = settlement_case
    root, _ = _saved_failure(case, tmp_path)
    parent = tmp_path / "astrid-attempt-old-base" if nested else tmp_path
    parent.mkdir(mode=0o700, exist_ok=True)
    legacy = parent / "astrid-attempt-unsafe-legacy"
    root.rename(legacy)
    if damage == "owner":
        _foreign_lstat(monkeypatch, legacy)
    elif damage == "group-writable":
        legacy.chmod(0o770)
    elif damage == "world-writable":
        legacy.chmod(0o707)
    else:
        target = tmp_path / "legacy-evidence"
        legacy.rename(target)
        if damage == "symlink":
            legacy.symlink_to(target, target_is_directory=True)
        else:
            legacy.write_text("not a directory")
    marker_paths = {legacy / "fail-request.json", legacy / "cancel-request.json", legacy / "result.json"}
    reads = []
    read_text = Path.read_text
    open_file = io.open

    def record_read(path, *args, **kwargs):
        if path in marker_paths:
            reads.append(path)
        return read_text(path, *args, **kwargs)

    def record_open(path, *args, **kwargs):
        if isinstance(path, (str, Path)) and Path(path) in marker_paths:
            reads.append(Path(path))
        return open_file(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", record_read)
    monkeypatch.setattr(io, "open", record_open)
    monkeypatch.setattr(generic_host.tempfile, "gettempdir", lambda: str(tmp_path))
    restarted = GenericPackHost(pack_roots=[], client=case.host.client, executor_id=case.host.executor_id)
    _bounded_run(restarted, restarted._recover_terminal_requests)
    assert not reads
    assert not case.failures and not case.cancels and not case.writes
    assert case.service.task(case.task_id)["task"]["status"] == "running"
    diagnostic = capsys.readouterr().err
    assert diagnostic.count("generic host recovery skipped unsafe legacy root") == 1
    assert str(legacy) in diagnostic


@pytest.mark.parametrize("swap", ["symlink", "directory"])
def test_private_base_swapped_between_validation_and_allocation_refuses(tmp_path, monkeypatch, swap):
    host = GenericPackHost(pack_roots=[])
    base = host._default_attempt_base()
    original = host._default_attempt_base
    target = tmp_path / "swap-target"
    target.mkdir(mode=0o700)

    def swapped():
        validated = original()
        base.rename(base.with_name(base.name + "-old"))
        if swap == "symlink":
            base.symlink_to(target, target_is_directory=True)
        else:
            base.mkdir(mode=0o700)
        return validated

    monkeypatch.setattr(host, "_default_attempt_base", swapped)
    with pytest.raises(HostError, match=str(base)) as error:
        host._allocate_attempt_root("task", "attempt")
    assert "remove" in str(error.value).lower() or "repair" in str(error.value).lower()
    assert list(target.iterdir()) == []
    if swap == "directory":
        assert list(base.iterdir()) == []


@pytest.mark.parametrize("mode,accepted", [(0o770, False), (0o1777, True)])
def test_private_base_checks_ancestors_above_state_root(tmp_path, monkeypatch, mode, accepted):
    ancestor = tmp_path / "ancestor"
    ancestor.mkdir(mode=mode)
    ancestor.chmod(mode)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(ancestor / "private"))
    host = GenericPackHost(pack_roots=[])
    if accepted:
        # Root-owned sticky shared ancestors are accepted (the CI uid is root).
        assert host._allocate_attempt_root("task", "attempt").is_dir()
    else:
        with pytest.raises(HostError, match=str(ancestor)):
            host._allocate_attempt_root("task", "attempt")
        assert not (ancestor / "private").exists()


@pytest.mark.parametrize("value", ["relative-runtime", "runtime/../escaped"])
def test_invalid_xdg_runtime_source_is_ignored(tmp_path, monkeypatch, capsys, value):
    raw = value if value.startswith("relative") else str(tmp_path / value)
    monkeypatch.setenv("XDG_RUNTIME_DIR", raw)
    host = GenericPackHost(pack_roots=[])
    root = host._allocate_attempt_root("task", "attempt")
    assert root.parent.parent.parent == Path.home() / ".cache" / "astrid"
    diagnostic = capsys.readouterr().err
    assert "XDG_RUNTIME_DIR" in diagnostic and raw in diagnostic


@pytest.mark.parametrize("foreign", [False, True])
def test_nested_legacy_request_survives_restart_only_for_own_executor(settlement_case, tmp_path, monkeypatch, foreign):
    case = settlement_case
    root, _ = _saved_failure(case, tmp_path)
    legacy_base = tmp_path / "astrid-attempt-previous-incarnation"
    legacy_base.mkdir(mode=0o700)
    nested = legacy_base / "astrid-attempt-previous-task"
    root.rename(nested)
    monkeypatch.setattr(generic_host.tempfile, "gettempdir", lambda: str(tmp_path))
    restarted = GenericPackHost(pack_roots=[], client=case.host.client,
                                executor_id="other" if foreign else case.host.executor_id)
    _bounded_run(restarted, restarted._recover_terminal_requests)
    assert len(case.failures) == (0 if foreign else 1)
    assert case.service.task(case.task_id)["task"]["status"] == ("running" if foreign else "failed")


def test_private_base_validates_opened_descriptor(tmp_path, monkeypatch):
    host = GenericPackHost(pack_roots=[])
    base = host._default_attempt_base()
    original = os.fstat
    identity = base.stat()

    def foreign_descriptor(fd):
        metadata = original(fd)
        if (metadata.st_dev, metadata.st_ino) == (identity.st_dev, identity.st_ino):
            fields = list(metadata)
            fields[4] = os.getuid() + 1
            return os.stat_result(fields)
        return metadata

    monkeypatch.setattr(os, "fstat", foreign_descriptor)
    with pytest.raises(HostError, match=str(base)):
        host._allocate_attempt_root("task", "attempt")


def test_unsafe_temp_parent_prevents_legacy_scan(settlement_case, tmp_path, monkeypatch, capsys):
    case = settlement_case
    root, _ = _saved_failure(case, tmp_path)
    temporary = tmp_path / "unsafe-temp"
    temporary.mkdir(mode=0o770)
    temporary.chmod(0o770)
    root.rename(temporary / "astrid-attempt-legacy")
    monkeypatch.setattr(generic_host.tempfile, "gettempdir", lambda: str(temporary))
    restarted = GenericPackHost(pack_roots=[], client=case.host.client, executor_id=case.host.executor_id)
    _bounded_run(restarted, restarted._recover_terminal_requests)
    assert not case.failures and not case.cancels and not case.writes
    diagnostic = capsys.readouterr().err
    assert diagnostic.count("generic host recovery skipped unsafe legacy root") == 1
    assert str(temporary) in diagnostic


@pytest.mark.parametrize("location", ["configured", "private", "temp", "legacy-nested"])
def test_recovery_enumeration_error_is_blocked_per_base_and_admission_continues(settlement_case, tmp_path, monkeypatch, location):
    case = settlement_case
    if location == "configured":
        case.host.attempt_root = None
        case.host.attempt_base = tmp_path / "configured"
        base = case.host.attempt_base
        base.mkdir()
    else:
        case.host.attempt_root = None
        base = case.host._default_attempt_base()
        if location in {"temp", "legacy-nested"}:
            temporary = tmp_path / "legacy-temp"
            temporary.mkdir()
            monkeypatch.setattr(generic_host.tempfile, "gettempdir", lambda: str(temporary))
            base = temporary
            if location == "legacy-nested":
                base = temporary / "astrid-attempt-old-base"
                base.mkdir(mode=0o700)
    original_iterdir, original_glob = Path.iterdir, Path.glob

    def iterdir(path):
        if path == base:
            raise OSError("enumeration denied")
        return original_iterdir(path)

    def glob(path, pattern, *args, **kwargs):
        if path == base:
            raise OSError("enumeration denied")
        return original_glob(path, pattern, *args, **kwargs)

    monkeypatch.setattr(Path, "iterdir", iterdir)
    monkeypatch.setattr(Path, "glob", glob)
    claimed = []
    monkeypatch.setattr(case.host, "claim_once", lambda: claimed.append("claimed"))
    _bounded_run(case.host, lambda: case.host.run(once=True))
    assert claimed == ["claimed"]
    assert not case.failures and not case.cancels and not case.writes
    assert len(case.host.recovery_blocked) == 1
    assert case.host.recovery_blocked[0]["path"] == str(base)
    assert "enumeration denied" in case.host.recovery_blocked[0]["reason"]


def _dead_host_with_preparation(engine_root, executor, ready):
    from astrid.core.execution.managed_tool_session import CapabilityDescriptor
    from astrid.core.generation.engine_platforms import platform_adapter
    from astrid.core.generation.managed_preparation import ManagedEnginePreparation
    from tests.core.execution.test_managed_tool_session import _Adapter, _binding

    host = GenericPackHost(pack_roots=[], executor_id=executor)
    host.managed_tool_session.open(capability=CapabilityDescriptor("vibecomfy.run"),
                                   binding=_binding("dead"), adapter=_Adapter())
    preparation = ManagedEnginePreparation.__new__(ManagedEnginePreparation)
    preparation.lock = platform_adapter("linux-x86_64-cu130").local_install_lock(
        str(engine_root), deadline=time.monotonic() + 10)
    preparation.lock.__enter__()
    ready.send((host.managed_tool_session.occupied, preparation.lock is not None))
    threading.Event().wait(60)


def test_restart_has_no_old_session_or_preparation_and_os_releases_flock(settlement_case, tmp_path):
    from astrid.core.generation.engine_platforms import platform_adapter

    case = settlement_case
    root, _ = _saved_failure(case, tmp_path)
    marker = root / "fail-request.json"
    marker.write_text("{unreadable")
    context = multiprocessing.get_context("fork")
    received, sent = context.Pipe(duplex=False)
    engine_root = tmp_path / "dead-host-engine"
    dead = context.Process(target=_dead_host_with_preparation,
                           args=(engine_root, case.host.executor_id, sent))
    dead.start()
    try:
        assert received.poll(5) and received.recv() == (True, True)
        # Kill instead of graceful close: the OS, not host cleanup, releases flock.
        dead.kill()
        dead.join(5)
        assert not dead.is_alive()
        restarted = GenericPackHost(pack_roots=[], client=case.host.client,
                                    executor_id=case.host.executor_id, attempt_root=root)
        assert not restarted.managed_tool_session.occupied
        assert restarted.managed_tool_session.current_binding is None
        assert restarted._active_processes == set() and restarted._target_bindings is None
        assert restarted.warm_holds.status() == [] and restarted._reconciliation_owners == {}
        _bounded_run(restarted, restarted._recover_terminal_requests)
        assert not restarted.managed_tool_session.occupied
        assert restarted.recovery_blocked[0]["path"] == str(marker)
        assert not case.failures and not case.cancels and not case.writes
        with platform_adapter("linux-x86_64-cu130").local_install_lock(
                str(engine_root), deadline=time.monotonic() + 2):
            pass
    finally:
        if dead.is_alive():
            dead.kill()
            dead.join(5)
        received.close()
        sent.close()
