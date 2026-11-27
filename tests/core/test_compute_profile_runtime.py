"""Runtime compute-profile resolution, migration and secret safety."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from astrid.core import user_state
from astrid.core.compute_profile import (
    RUNTIME_KEY,
    SCHEMA_ID,
    list_profile_ids,
    load_profile,
    remove_profile,
    resolve_compute_profile,
    save_profile,
    write_resolved_snapshot,
)
from astrid.core.user_state_import import RECEIPT_KEY
from tests.user_state_fakes import FakeApiError, InMemoryAppState


def profile(profile_id="default", **values):
    return {
        "schema": SCHEMA_ID,
        "schema_version": 1,
        "id": profile_id,
        "credentials": {"runpod_api_key": "RUNPOD_API_KEY"},
        **values,
    }


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    return tmp_path


def write_legacy(home, profile_id, document):
    path = home / ".astrid" / "compute-profiles" / f"{profile_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def seed(store, *profiles):
    user_state.write(
        RUNTIME_KEY,
        {"schema": "astrid.compute-profiles.v1", "profiles": {p["id"]: p for p in profiles}},
        expected_version=0,
        idempotency_key="seed",
        store=store,
    )


def test_fresh_runtime_document_wins_without_files(home):
    store = InMemoryAppState()
    seed(store, profile(gpu_type="runtime-gpu"))
    loaded, location = load_profile("default", store=store)
    assert loaded["gpu_type"] == "runtime-gpu"
    assert str(location) == f"runtime:{RUNTIME_KEY}/default"
    resolved = resolve_compute_profile(env={}, store=store)
    assert resolved["gpu_type"] == "runtime-gpu"
    assert resolved["profile_path"] == str(location)
    assert resolved["profile_source"] == "default-profile"
    assert not (home / ".astrid").exists()
    with pytest.raises(FileNotFoundError, match=f"compute profile 'missing' not found at runtime:{RUNTIME_KEY}/missing"):
        load_profile("missing", store=store)


def test_legacy_import_once_skips_invalid_and_ignores_later_edits(home):
    store = InMemoryAppState()
    path = write_legacy(home, "default", profile(gpu_type="original"))
    write_legacy(home, "wrong-id", profile("different"))
    write_legacy(home, "secret", profile("secret", credentials={"key": "rpa_literal"}))
    write_legacy(home, "invalid", {}).write_text("{", encoding="utf-8")
    original = path.read_bytes()
    assert load_profile("default", store=store)[0]["gpu_type"] == "original"
    assert path.read_bytes() == original
    receipt = user_state.read(RECEIPT_KEY, store=store).content["compute-profiles"]
    assert receipt["target_key"] == RUNTIME_KEY
    assert receipt["outcome"] == "imported"
    assert list_profile_ids(store=store) == ["default"]
    writes = store.writes.copy()
    write_legacy(home, "default", profile(gpu_type="changed"))
    assert load_profile("default", store=store)[0]["gpu_type"] == "original"
    assert store.writes == writes


def test_runtime_precedence(home):
    store = InMemoryAppState()
    seed(store, profile(gpu_type="default"), profile("env", gpu_type="env"),
         profile("named", gpu_type="named", storage_name="named-volume"))
    defaults = {"gpu_type": "executor", "container_disk_gb": 50}
    resolved = resolve_compute_profile(
        explicit={"gpu_type": "explicit"}, env={"ASTRID_COMPUTE_PROFILE": "env"},
        profile_id="named", executor_defaults=defaults, store=store,
    )
    assert resolved["gpu_type"] == "explicit"
    assert resolved["storage_name"] == "named-volume"
    assert resolved["profile_id"] == "named"
    assert resolved["profile_source"] == "explicit-profile"
    assert resolved["container_disk_gb"] == 50
    resolved = resolve_compute_profile(env={"ASTRID_COMPUTE_PROFILE": "env"},
                                       executor_defaults=defaults, store=store)
    assert resolved["gpu_type"] == "env"
    assert resolved["profile_id"] == "env"
    assert resolved["profile_source"] == "env-selected-profile"
    assert resolved["profile_path"] == f"runtime:{RUNTIME_KEY}/env"
    assert resolve_compute_profile(env={}, executor_defaults=defaults, store=store)["gpu_type"] == "default"
    assert resolve_compute_profile(
        explicit={"gpu_type": "explicit"}, env={"ASTRID_COMPUTE_PROFILE": "env"}, store=store,
    )["gpu_type"] == "explicit"


def test_auto_store_and_environment_selection(home, monkeypatch):
    store = InMemoryAppState()
    seed(store, profile("selected", gpu_type="runtime"))
    monkeypatch.setenv("ASTRID_COMPUTE_PROFILE", "selected")
    with user_state.using(store):
        assert resolve_compute_profile()["profile_id"] == "selected"
        assert load_profile("selected")[0]["gpu_type"] == "runtime"
        with pytest.raises(FileNotFoundError):
            load_profile("selected", store=None)


def test_unreachable_and_explicit_home_use_legacy_files(home):
    path = write_legacy(home, "default", profile(gpu_type="legacy"))
    store = InMemoryAppState()
    seed(store, profile(gpu_type="runtime"))
    assert load_profile("default", home=home, store=store)[1] == path
    assert resolve_compute_profile(env={}, home=home, store=store)["gpu_type"] == "legacy"
    assert load_profile("default", store=None)[1] == path
    store.unreachable = True
    assert load_profile("default", store=store)[1] == path
    assert resolve_compute_profile(env={}, store=store)["gpu_type"] == "legacy"
    assert list_profile_ids(store=store) == ["default"]
    with pytest.raises(RuntimeError, match="the Runtime is not reachable; the compute profile was not saved"):
        save_profile(profile("new"), store=store)
    with pytest.raises(RuntimeError, match="not saved"):
        save_profile(profile("new"), store=None)


def test_save_imports_first_and_list_remove_round_trip(home):
    path = write_legacy(home, "legacy", profile("legacy"))
    store = InMemoryAppState()
    save_profile(profile("new"), store=store)
    assert list_profile_ids(store=store) == ["legacy", "new"]
    assert user_state.read(RECEIPT_KEY, store=store).content["compute-profiles"]["outcome"] == "imported"
    save_profile(profile("new", gpu_type="updated"), store=store)
    assert load_profile("new", store=store)[0]["gpu_type"] == "updated"
    assert remove_profile("legacy", store=store) is True
    assert remove_profile("legacy", store=store) is False
    assert remove_profile("new", store=store) is True
    assert list_profile_ids(store=store) == []
    assert path.is_file()
    with pytest.raises(FileNotFoundError):
        load_profile("legacy", store=store)


def test_reject_literal_secrets_and_revalidate_reads(home):
    store = InMemoryAppState()
    with pytest.raises(ValueError, match="environment variable name"):
        save_profile(profile(credentials={"key": "sk-literal"}), store=store)
    assert store.writes == []
    seed(store, profile(credentials={"key": "rpa_literal"}))
    with pytest.raises(ValueError, match="environment variable name"):
        load_profile("default", store=store)


def test_size_limit_names_profile_and_preserves_previous_document(home):
    store = InMemoryAppState()
    save_profile(profile("first", description="x" * 9000), store=store)
    before = user_state.read(RUNTIME_KEY, store=store)
    with pytest.raises(ValueError, match="second.*16 KiB"):
        save_profile(profile("second", description="y" * 9000), store=store)
    assert user_state.read(RUNTIME_KEY, store=store) == before


def test_snapshot_contains_only_credential_references(home, monkeypatch):
    store = InMemoryAppState()
    monkeypatch.setenv("RUNPOD_API_KEY", "rpa_never_persist_this")
    save_profile(profile(), store=store)
    resolved = resolve_compute_profile(env={}, store=store)
    output = write_resolved_snapshot(home / "produces", resolved).read_text(encoding="utf-8")
    assert json.loads(output)["credentials"]["runpod_api_key"] == "RUNPOD_API_KEY"
    assert "rpa_never_persist_this" not in output
    assert "rpa_never_persist_this" not in json.dumps(store.documents)


@pytest.mark.parametrize("values", [
    {"image": {"api_key": "opaque-credential"}},
    {"description": {"nested": [{"token": "opaque-credential"}]}},
    {"excludes": ["sk-literal-secret"]},
    {"excludes": ("rpa_literal_secret",)},
    {"description": "  sk-literal-secret"},
    {"image": {"sk-literal-secret": "value"}},
    {"credentials": {"sk-literal-secret": "RUNPOD_API_KEY"}},
    {"credentials": {"rpa_literal_secret": "RUNPOD_API_KEY"}},
])
def test_nested_secrets_are_refused_on_save_and_skipped_on_import(home, values):
    unsafe = profile("unsafe", **values)
    store = InMemoryAppState()
    with pytest.raises(ValueError, match="secret-bearing field|literal credential"):
        save_profile(unsafe, store=store)
    assert not store.documents
    assert not store.writes

    unsafe_path = write_legacy(home, "unsafe", unsafe)
    original = unsafe_path.read_bytes()
    write_legacy(home, "default", profile(gpu_type="safe"))
    assert load_profile("default", store=store)[0]["gpu_type"] == "safe"
    assert list_profile_ids(store=store) == ["default"]
    assert unsafe_path.read_bytes() == original
    saved = json.dumps(store.documents)
    assert "literal-secret" not in saved
    assert "literal_secret" not in saved
    assert "opaque-credential" not in saved


@pytest.mark.parametrize("source", ["explicit", "executor_defaults"])
@pytest.mark.parametrize("values", [
    {"image": {"api_key": "opaque-credential"}},
    {"image": "sk-literal-secret"},
    {"excludes": ("rpa_literal_secret",)},
    {"credentials": {"sk-literal-secret": "RUNPOD_API_KEY"}},
])
def test_resolver_refuses_secrets_in_overrides_and_executor_defaults(home, source, values):
    with pytest.raises(ValueError, match="secret-bearing field|literal credential"):
        resolve_compute_profile(env={}, home=home, **{source: values})


@pytest.mark.parametrize("values", [
    {"image": {"nested": [{"password": "opaque-credential"}]}},
    {"image": "sk-literal-secret"},
    {"excludes": ["rpa_literal_secret"]},
    {"credentials": {"rpa_literal_secret": "RUNPOD_API_KEY"}},
])
def test_snapshot_refuses_secrets_before_any_file_write(home, values):
    safe = resolve_compute_profile(env={}, home=home)
    output = write_resolved_snapshot(home / "produces", safe)
    original = output.read_bytes()
    unsafe = {**safe, **values}
    with pytest.raises(ValueError, match="secret-bearing field|literal credential"):
        write_resolved_snapshot(output.parent, unsafe)
    assert output.read_bytes() == original

    absent = home / "not-created"
    with pytest.raises(ValueError, match="secret-bearing field|literal credential"):
        write_resolved_snapshot(absent, unsafe)
    assert not absent.exists()


def test_oversized_initial_import_preserves_legacy_reads_and_rejects_save(home):
    paths = [write_legacy(home, name, profile(name, description="x" * 8500))
             for name in ("default", "other")]
    original = [path.read_bytes() for path in paths]
    store = InMemoryAppState()

    assert load_profile("default", store=store)[1] == paths[0]
    resolved = resolve_compute_profile(env={"ASTRID_COMPUTE_PROFILE": "other"}, store=store)
    assert resolved["profile_id"] == "other"
    assert resolved["profile_path"] == str(paths[1])
    assert list_profile_ids(store=store) == ["default", "other"]
    with pytest.raises(ValueError, match="new.*16 KiB"):
        save_profile(profile("new"), store=store)
    assert not store.documents
    assert not store.writes
    assert [path.read_bytes() for path in paths] == original


def test_oversized_import_does_not_override_concurrently_emptied_runtime(home):
    paths = [write_legacy(home, name, profile(name, description="x" * 8500))
             for name in ("default", "other")]
    original = [path.read_bytes() for path in paths]

    class ConcurrentStore(InMemoryAppState):
        target_reads = 0

        def get_user_app_state(self, key):
            if key == RUNTIME_KEY:
                self.target_reads += 1
                if self.target_reads == 3:
                    seed(self)
            return super().get_user_app_state(key)

    store = ConcurrentStore()
    with pytest.raises(FileNotFoundError, match=f"runtime:{RUNTIME_KEY}/default"):
        load_profile("default", store=store)
    assert list_profile_ids(store=store) == []
    assert user_state.read(RUNTIME_KEY, store=store).content["profiles"] == {}
    assert [path.read_bytes() for path in paths] == original


@pytest.mark.parametrize("code", ["transport_error", "transport_timeout"])
def test_transport_failure_falls_back_to_files_and_refuses_writes(home, code):
    path = write_legacy(home, "default", profile(gpu_type="legacy"))

    class UnreachableStore(InMemoryAppState):
        def get_user_app_state(self, key):
            raise FakeApiError(0, code)

    store = UnreachableStore()
    assert load_profile("default", store=store)[1] == path
    assert resolve_compute_profile(env={}, store=store)["gpu_type"] == "legacy"
    assert list_profile_ids(store=store) == ["default"]
    with pytest.raises(RuntimeError, match="not saved"):
        save_profile(profile("new"), store=store)
    with pytest.raises(RuntimeError, match="not removed"):
        remove_profile("default", store=store)
    assert not store.documents
    assert not store.writes
