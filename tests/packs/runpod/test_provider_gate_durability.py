"""Crash injection uses only local files and fake provider operations."""

import json
import threading

import pytest

from astrid.packs.runpod import provider


@pytest.mark.parametrize("step", ["truncate", "write", "flush"])
def test_interrupted_writer_preserves_cooldown_and_release(tmp_path, monkeypatch, step):
    path = tmp_path / "gate.json"
    gate = provider.ProviderGate(path, clock=lambda: 1000.)
    gate.after_response(429, "120")
    dump = json.dump

    def crash(state, stream, **kwargs):
        # At these points the old implementation has already truncated its
        # only copy. Model a lost write buffer at flush, not a clean close.
        if step == "write":
            stream.write('{"cooldown":')
        elif step == "flush":
            dump(state, stream, **kwargs)
            stream.seek(0)
            stream.truncate()
        raise OSError("writer died at " + step)

    with monkeypatch.context() as failure:
        failure.setattr(json, "dump", crash)
        with pytest.raises(OSError, match="writer died"):
            gate.after_error("180")

    fresh = provider.ProviderGate(path, clock=lambda: 1000.)
    with pytest.raises(provider.ProviderRefused) as refused:
        fresh.before_call(1001.)
    assert refused.value.retry_after >= 120
    released = []
    with provider.provider_budget(1, cleanup=True):
        fresh.call(lambda: released.append("exact-pod"))
    assert released == ["exact-pod"]


@pytest.mark.parametrize("raw", ["", "{", "[]", '{"cooldown":"soon"}', '{"cooldown":NaN}'])
def test_invalid_state_recovers_conservatively_once_and_cleanup_proceeds(tmp_path, caplog, raw):
    path = tmp_path / "gate.json"
    path.write_text(raw)
    now = [1000.]
    gate = provider.ProviderGate(path, clock=lambda: now[0])
    with pytest.raises(provider.ProviderRefused) as refused:
        gate.before_call(1001.)
    assert 0 < refused.value.retry_after <= 60
    assert "recover" in caplog.text.lower()
    fresh = provider.ProviderGate(path, clock=lambda: now[0])
    with provider.provider_budget(1, cleanup=True):
        assert fresh.call(lambda: "released-exact-pod") == "released-exact-pod"
    now[0] += 60
    fresh.before_call(now[0] + 1)


def test_missing_initialized_state_is_not_a_fresh_account(tmp_path):
    path = tmp_path / "gate.json"
    gate = provider.ProviderGate(path, clock=lambda: 1000.)
    gate.after_error("120")
    path.unlink()
    fresh = provider.ProviderGate(path, clock=lambda: 1000.)
    with pytest.raises(provider.ProviderRefused) as refused:
        fresh.before_call(1001.)
    assert 0 < refused.value.retry_after <= 60


def test_invalid_field_never_erases_a_valid_longer_cooldown(tmp_path):
    path = tmp_path / "gate.json"
    path.write_text('{"cooldown":1120,"attempt":"torn"}')
    with pytest.raises(provider.ProviderRefused) as refused:
        provider.ProviderGate(path, clock=lambda: 1000.).before_call(1001.)
    assert refused.value.retry_after == 120


def test_concurrent_replacements_hold_one_stable_real_lock(tmp_path):
    path = tmp_path / "gate.json"
    start = threading.Barrier(3)
    errors = []

    def writer():
        gate = provider.ProviderGate(path, clock=lambda: 1000.)
        start.wait()
        try:
            for _ in range(20):
                gate._state(lambda state: state.update(attempt=state.get("attempt", 0) + 1))
        except BaseException as exc:
            errors.append(exc)

    workers = [threading.Thread(target=writer) for _ in range(2)]
    for worker in workers:
        worker.start()
    start.wait()
    for worker in workers:
        worker.join(2)
    assert not any(worker.is_alive() for worker in workers)
    assert errors == []
    assert json.loads(path.read_text())["attempt"] == 40
    assert path.with_suffix(path.suffix + ".lock").exists()


def test_cleanup_ignores_unreadable_alias_and_state(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTRID_RUNTIME_DATA_ROOT", str(tmp_path))
    gate = provider.account_gate("fake", account_ref="account")
    gate.path.write_text("{")
    alias = next(gate.path.parent.glob("*.account.json"))
    alias.write_text("{")
    with provider.provider_budget(1, cleanup=True):
        assert provider.provider_call("fake", lambda: "exact-pod-deleted") == "exact-pod-deleted"


def test_replace_is_fsynced_before_and_after_publish(tmp_path, monkeypatch):
    import os
    import stat

    events = []
    fsync, replace = os.fsync, os.replace
    gate = provider.ProviderGate(tmp_path / "gate.json")
    gate.after_error("5")

    def sync(fd):
        events.append("directory" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file")
        return fsync(fd)

    def publish(*args):
        events.append("replace")
        return replace(*args)

    monkeypatch.setattr(os, "fsync", sync)
    monkeypatch.setattr(os, "replace", publish)
    gate.after_error("10")
    assert events == ["file", "replace", "directory"]
