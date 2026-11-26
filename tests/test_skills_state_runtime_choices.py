"""Default-path skill opt-outs share Runtime state without losing the file mirror."""

import json

import pytest

from astrid.core import user_choices, user_state, user_state_import
from astrid.skills import state
from tests.user_state_fakes import InMemoryAppState


@pytest.fixture
def profile(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTRID_STATE_HOME", str(tmp_path))
    store = InMemoryAppState()
    with user_state.using(store):
        yield store, state.state_path()


def runtime_choices(store):
    return user_state.read(user_choices.KEY, store=store).content["skills"]["disabled_defaults"]


def test_fresh_profile_reads_runtime_without_file(profile):
    store, path = profile
    assert user_choices.push_skills({"claude": ["paint"], "codex": ["audio"]})
    assert not path.exists()
    data = state.load()
    assert data["disabled_defaults"] == {"claude": ["paint"], "codex": ["audio"], "hermes": []}
    assert not path.exists()


def test_import_then_save_and_older_file_edit(profile):
    store, path = profile
    local = state.load(path)
    local["disabled_defaults"]["claude"] = ["paint"]
    state.record_install(local, "codex", "audio", target="/local/audio", mechanism="symlink")
    local["setup_selection"] = {"workspace_id": "local"}
    state.save(local, path)

    assert state.load() == local
    receipts = user_state.read(user_state_import.RECEIPT_KEY, store=store).content
    assert receipts["skills-disabled-defaults"]["outcome"] == "imported"
    assert runtime_choices(store) == local["disabled_defaults"]
    writes = list(store.writes)
    assert state.load() == local
    assert store.writes == writes

    local["disabled_defaults"]["codex"] = ["music"]
    state.save(local)
    assert json.loads(path.read_text()) == local
    assert runtime_choices(store) == local["disabled_defaults"]
    doc = user_state.read(user_choices.KEY, store=store).content
    assert set(doc) == {"schema", "skills"}
    assert set(doc["skills"]) == {"disabled_defaults", "mirror_digest"}

    local["disabled_defaults"]["claude"] = []
    path.write_text(json.dumps(local), encoding="utf-8")
    assert state.load() == local
    assert runtime_choices(store) == local["disabled_defaults"]
    assert user_state.read(user_state_import.RECEIPT_KEY, store=store).content == receipts


def test_unavailable_runtime_preserves_file_and_unchanged_save_retries(profile):
    store, path = profile
    store.unreachable = True
    assert state.load() == state.load(path)
    local = state.load(path)
    local["disabled_defaults"]["claude"] = ["z", "a", "z"]
    state.save(local)
    assert state.load() == state.load(path) == local
    original = path.read_bytes()
    store.unreachable = False
    state.save(local)
    assert path.read_bytes() == original
    assert runtime_choices(store)["claude"] == ["a", "z"]


def test_uninstall_and_reinstall_mirror_independent_harnesses(profile):
    store, path = profile
    data = state.load()
    state.record_uninstall(data, "claude", "paint", default=True)
    state.record_uninstall(data, "codex", "paint", default=True)
    state.save(data)
    assert runtime_choices(store) == {"claude": ["paint"], "codex": ["paint"], "hermes": []}
    assert state.load(path)["disabled_defaults"] == runtime_choices(store)
    data = state.load()
    state.record_install(data, "claude", "paint", target="/local/paint", mechanism="symlink")
    state.save(data)
    assert runtime_choices(store) == {"claude": [], "codex": ["paint"], "hermes": []}
    assert state.load(path) == data


def test_explicit_path_never_touches_runtime(profile):
    _, path = profile

    class ForbiddenStore:
        def get_user_app_state(self, *args, **kwargs):
            raise AssertionError("explicit path read Runtime")

        def update_user_app_state(self, *args, **kwargs):
            raise AssertionError("explicit path wrote Runtime")

    # Also spy on the boundary because default-path best-effort catches errors.
    calls = []

    class RecordingForbiddenStore(ForbiddenStore):
        def get_user_app_state(self, *args, **kwargs):
            calls.append("read")
            return super().get_user_app_state(*args, **kwargs)

        def update_user_app_state(self, *args, **kwargs):
            calls.append("write")
            return super().update_user_app_state(*args, **kwargs)

    with user_state.using(RecordingForbiddenStore()):
        data = state.load(path)
        state.record_uninstall(data, "hermes", "paint", default=True)
        state.save(data, path)
        state.save(data, path)
        assert state.load(path) == data
    assert calls == []
