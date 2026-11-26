import json
import os
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from astrid.core import user_state as state
from astrid.core.user_state_import import (
    MAX_RECEIPTS,
    RECEIPT_KEY,
    ImportSpec,
    run_import,
)
from tests.user_state_fakes import InMemoryAppState

STAMP = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
VALUE = ["pack-a", "pack-b"]
DESIRED = {"disabled": VALUE}


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "legacy.json"
    path.write_text(json.dumps(VALUE), encoding="utf-8")
    # Set a known older mtime so a rewrite cannot hide within filesystem resolution.
    os.utime(path, ns=(1_700_000_000_000_000_000, 1_700_000_000_000_000_000))

    def read_source():
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8") or "null") or None

    return path, ImportSpec("disabled-defaults", "skills.disabled", "legacy.json",
                            read_source, lambda value: {"disabled": value})


def receipt(store, spec):
    return state.read(RECEIPT_KEY, store=store).content[spec.import_id]


def test_first_and_repeated_import_keep_legacy_file(source):
    path, spec = source
    store = InMemoryAppState()
    old_bytes, old_mtime = path.read_bytes(), path.stat().st_mtime_ns
    result = run_import(spec, store=store, now=lambda: STAMP)
    digest = state.content_digest(VALUE)
    assert (result.status, result.target_version, result.source_digest) == ("imported", 1, digest)
    assert state.read(spec.target_key, store=store) == state.Document(DESIRED, 1)
    assert state.read(RECEIPT_KEY, store=store) == state.Document({spec.import_id: {
        "source": spec.source_label, "source_digest": digest, "target_key": spec.target_key,
        "outcome": "imported", "target_version": 1, "at": STAMP.isoformat(),
    }}, 1)
    assert store.writes == [(spec.target_key, 0), (RECEIPT_KEY, 0)]
    writes = store.writes.copy()
    result = run_import(spec, store=store)
    assert (result.status, result.target_version, result.source_digest) == (
        "already_imported", 1, digest,
    )
    assert store.writes == writes
    assert path.read_bytes() == old_bytes
    assert path.stat().st_mtime_ns == old_mtime


def test_changed_source_reports_both_digests_without_writes(source):
    path, spec = source
    store = InMemoryAppState()
    original = run_import(spec, store=store).source_digest
    path.write_text('["other"]', encoding="utf-8")
    old_bytes, old_mtime = path.read_bytes(), path.stat().st_mtime_ns
    writes = store.writes.copy()
    changed = run_import(spec, store=store)
    assert changed.status == "source_changed"
    assert changed.source_digest == state.content_digest(["other"])
    assert original in changed.note and changed.source_digest in changed.note
    assert "edited after the import" in changed.note
    assert store.writes == writes
    assert state.read(spec.target_key, store=store) == state.Document(DESIRED, 1)
    assert receipt(store, spec)["source_digest"] == original
    assert (path.read_bytes(), path.stat().st_mtime_ns) == (old_bytes, old_mtime)


@pytest.mark.parametrize("empty", [False, True])
def test_no_source_does_not_record_receipt_and_can_import_later(source, empty):
    path, spec = source
    store = InMemoryAppState()
    if empty:
        path.write_text("[]", encoding="utf-8")
    else:
        path.unlink()
    assert run_import(spec, store=store).status == "nothing_to_import"
    assert store.writes == []
    assert state.read(RECEIPT_KEY, store=store) == state.Document(None, 0)
    path.write_text(json.dumps(VALUE), encoding="utf-8")
    assert run_import(spec, store=store).status == "imported"
    assert state.read(spec.target_key, store=store) == state.Document(DESIRED, 1)


@pytest.mark.parametrize("existing", [{"disabled": ["runtime"]}, {}])
def test_existing_runtime_value_wins_even_if_empty(source, existing):
    _, spec = source
    store = InMemoryAppState()
    state.write(spec.target_key, existing, expected_version=0, idempotency_key="seed", store=store)
    result = run_import(spec, store=store)
    assert (result.status, result.target_version) == ("target_kept", 1)
    assert state.read(spec.target_key, store=store) == state.Document(existing, 1)
    assert receipt(store, spec)["outcome"] == "kept"
    assert store.writes == [(spec.target_key, 0), (RECEIPT_KEY, 0)]
    assert run_import(spec, store=store).status == "already_imported"
    assert len(store.writes) == 2


@pytest.mark.parametrize("failed_write", [1, 2])
def test_interrupted_copy_recovers_without_extra_target_version(source, failed_write):
    _, spec = source
    store = InMemoryAppState()
    store.fail_writes = {failed_write}
    with pytest.raises(state.UserStateError) as error:
        run_import(spec, store=store)
    assert error.value.code == "unavailable"
    assert state.read(RECEIPT_KEY, store=store) == state.Document(None, 0)
    expected = state.Document(None, 0) if failed_write == 1 else state.Document(DESIRED, 1)
    assert state.read(spec.target_key, store=store) == expected
    store.fail_writes.clear()
    result = run_import(spec, store=store)
    assert (result.status, result.target_version) == ("imported", 1)
    assert state.read(spec.target_key, store=store) == state.Document(DESIRED, 1)
    assert state.read(RECEIPT_KEY, store=store).version == 1
    assert receipt(store, spec)["outcome"] == "imported"
    assert run_import(spec, store=store).status == "already_imported"
    assert store.writes == ([(spec.target_key, 0)] * (2 if failed_write == 1 else 1)
                            + [(RECEIPT_KEY, 0)] * (2 if failed_write == 2 else 1))


def test_distinct_imports_preserve_each_others_receipts(source):
    _, spec = source
    other = replace(spec, import_id="second", target_key="skills.other")
    store = InMemoryAppState()
    assert run_import(spec, store=store, now=lambda: STAMP).status == "imported"
    first = receipt(store, spec)
    assert run_import(other, store=store).status == "imported"
    assert receipt(store, spec) == first
    assert receipt(store, other)["target_key"] == other.target_key
    assert state.read(RECEIPT_KEY, store=store).version == 2
    assert state.read(other.target_key, store=store) == state.Document(DESIRED, 1)


def test_receipt_limit_refuses_new_import_without_writing(source):
    _, spec = source
    store = InMemoryAppState()
    full = {f"import-{i}": {"source": "old", "source_digest": state.content_digest(VALUE),
                            "target_key": "old", "outcome": "imported", "target_version": 1,
                            "at": STAMP.isoformat()} for i in range(MAX_RECEIPTS)}
    state.write(RECEIPT_KEY, full, expected_version=0, idempotency_key="full", store=store)
    writes = store.writes.copy()
    with pytest.raises(state.UserStateError) as error:
        run_import(spec, store=store)
    assert error.value.code == "too_large"
    assert store.writes == writes
    assert state.read(RECEIPT_KEY, store=store) == state.Document(full, 1)
    assert state.read(spec.target_key, store=store) == state.Document(None, 0)
    existing = replace(spec, import_id="import-0")
    assert run_import(existing, store=store).status == "already_imported"
    assert store.writes == writes


def test_no_store_does_not_read_source_or_write(source, monkeypatch):
    _, spec = source
    store = InMemoryAppState()
    monkeypatch.setattr(state, "current_store", lambda: None)

    def must_not_read():
        pytest.fail("source must not be read without a Runtime store")

    result = run_import(replace(spec, read_source=must_not_read))
    assert (result.status, result.target_version, result.source_digest) == ("unavailable", 0, None)
    assert store.writes == []


def test_unreachable_store_writes_nothing(source):
    _, spec = source
    store = InMemoryAppState()
    store.unreachable = True
    with pytest.raises(state.UserStateError) as error:
        run_import(spec, store=store)
    assert error.value.code == "unavailable"
    assert store.writes == []


def test_concurrent_target_creation_keeps_runtime_value(source):
    _, spec = source

    class RacingStore(InMemoryAppState):
        def update_user_app_state(self, key, content, *, expected_version, idempotency_key):
            if key == spec.target_key and not self.writes:
                super().update_user_app_state(key, {}, expected_version=0, idempotency_key="race")
            return super().update_user_app_state(key, content, expected_version=expected_version,
                                                 idempotency_key=idempotency_key)

    store = RacingStore()
    assert run_import(spec, store=store).status == "target_kept"
    assert state.read(spec.target_key, store=store) == state.Document({}, 1)
    assert receipt(store, spec)["outcome"] == "kept"
