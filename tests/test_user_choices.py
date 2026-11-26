"""Portable skill preferences, import receipts and compatibility mirror rules."""

import json

import pytest

from astrid.core import user_choices, user_state, user_state_import
from tests.user_state_fakes import InMemoryAppState


KEY = user_choices.KEY
EMPTY = {"claude": [], "codex": [], "hermes": []}
LOCAL = {"claude": ["a", "b"], "codex": [], "hermes": ["h"]}
REMOTE = {"claude": ["remote"], "codex": ["c"], "hermes": []}


def document(value, mirror):
    return {
        "schema": user_choices.SCHEMA,
        "skills": {"disabled_defaults": value, "mirror_digest": mirror},
    }


def reconcile(value=LOCAL, *, present=True):
    return user_choices.reconcile_skills(file_present=present, file_value=value)


def test_normalization():
    malformed = {"claude": ["b", "", "a", "b", 1, None, {}],
                 "codex": "not a list", "hermes": ["h"], "unknown": ["x"]}
    assert user_choices.normalize_disabled(malformed) == LOCAL
    assert user_choices.normalize_disabled(None) == EMPTY
    assert user_choices.normalize_disabled([]) == EMPTY
    assert user_choices.disabled_digest(malformed) == user_state.content_digest(LOCAL)


def test_no_store(monkeypatch):
    monkeypatch.setattr(user_state, "current_store", lambda: None)
    assert reconcile() == user_choices.Reconciled(LOCAL, "file", False)
    assert not user_choices.push_skills(LOCAL)


def test_first_import_has_receipt():
    store = InMemoryAppState()
    with user_state.using(store):
        assert reconcile() == user_choices.Reconciled(LOCAL, "imported", True)
        assert user_state.read(KEY).content == document(LOCAL, user_choices.disabled_digest(LOCAL))
        receipt = user_state.read(user_state_import.RECEIPT_KEY).content["skills-disabled-defaults"]
        assert receipt["outcome"] == "imported"
        assert receipt["target_key"] == KEY
        assert receipt["source_digest"] == user_choices.disabled_digest(LOCAL)
        assert reconcile() == user_choices.Reconciled(LOCAL, "runtime", False)
        assert len(store.writes) == 2


def test_empty_file_writes_nothing():
    store = InMemoryAppState()
    with user_state.using(store):
        assert reconcile({"claude": [None, ""]}) == user_choices.Reconciled(EMPTY, "file", False)
        assert user_state.read(KEY).content is None
        assert user_state.read(user_state_import.RECEIPT_KEY).content is None
        assert not store.writes


def test_fresh_profile_uses_runtime():
    store = InMemoryAppState()
    with user_state.using(store):
        assert user_choices.push_skills(REMOTE)
        assert reconcile(EMPTY, present=False) == user_choices.Reconciled(REMOTE, "runtime", False)
        assert len(store.writes) == 1


def test_unchanged_mirror_uses_runtime_with_normalized_digest():
    store = InMemoryAppState()
    store.documents[KEY] = (document(REMOTE, user_choices.disabled_digest(LOCAL)), 1)
    with user_state.using(store):
        assert reconcile({"claude": ["b", "a", "b"], "hermes": ["h"]}) == (
            user_choices.Reconciled(REMOTE, "runtime", False)
        )
        assert not store.writes


def test_edited_file_wins_and_updates_digest():
    store = InMemoryAppState()
    with user_state.using(store):
        assert user_choices.push_skills(REMOTE)
        assert reconcile() == user_choices.Reconciled(LOCAL, "file-newer", True)
        assert user_state.read(KEY).content == document(LOCAL, user_choices.disabled_digest(LOCAL))
        assert reconcile() == user_choices.Reconciled(LOCAL, "runtime", False)


def test_push_then_reconcile_and_no_machine_records():
    store = InMemoryAppState()
    with user_state.using(store):
        assert user_choices.push_skills({**LOCAL, "installs": ["/machine/path"], "setup_selection": "x"})
        assert reconcile() == user_choices.Reconciled(LOCAL, "runtime", False)
        content = user_state.read(KEY).content
        assert set(content) == {"schema", "skills"}
        assert set(content["skills"]) == {"disabled_defaults", "mirror_digest"}
        assert set(content["skills"]["disabled_defaults"]) == set(user_choices.HARNESSES)
        serialized = json.dumps(content)
        assert '"installs"' not in serialized
        assert '"setup_selection"' not in serialized
        assert "/machine/path" not in serialized
        assert len(store.writes) == 1


def test_unavailable_runtime_uses_file():
    store = InMemoryAppState()
    store.unreachable = True
    with user_state.using(store):
        assert not user_choices.push_skills(LOCAL)
        assert reconcile() == user_choices.Reconciled(LOCAL, "file", False)
        assert not store.documents


def test_unavailable_during_file_newer_write():
    store = InMemoryAppState()
    with user_state.using(store):
        assert user_choices.push_skills(REMOTE)
        store.fail_writes.add(2)
        assert reconcile() == user_choices.Reconciled(LOCAL, "file-newer", False)
        assert user_state.read(KEY).content == document(REMOTE, user_choices.disabled_digest(REMOTE))
        assert reconcile() == user_choices.Reconciled(LOCAL, "file-newer", True)


@pytest.mark.parametrize("content, key", [
    ({**document(LOCAL, None), "installs": {}}, "installs"),
    ({**document(LOCAL, None), "schema": "wrong"}, "schema"),
    ({"schema": user_choices.SCHEMA, "skills": {"setup_selection": "x"}}, "setup_selection"),
    ({"schema": user_choices.SCHEMA, "skills": []}, "skills"),
])
def test_validate_refuses_unknown_keys_and_schema(content, key):
    with pytest.raises(ValueError, match=key):
        user_choices.validate_document(content)
    store = InMemoryAppState()
    with user_state.using(store):
        with pytest.raises(ValueError, match=key):
            user_state.update(KEY, lambda doc: doc.update(content), validate=user_choices.validate_document)
        assert not store.writes


def test_size_guard_keeps_file_current():
    store = InMemoryAppState()
    oversized = {"claude": ["x" * 17000], "codex": [], "hermes": []}
    with user_state.using(store):
        assert not user_choices.push_skills(oversized)
        assert not store.writes
        assert reconcile(oversized) == user_choices.Reconciled(oversized, "file", False)
        assert not store.writes
        assert user_choices.push_skills(REMOTE)
        assert reconcile(oversized) == user_choices.Reconciled(oversized, "file-newer", False)
        assert len(store.writes) == 1
        assert user_state.read(KEY).content == document(REMOTE, user_choices.disabled_digest(REMOTE))
