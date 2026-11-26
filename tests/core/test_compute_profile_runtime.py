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
from tests.user_state_fakes import InMemoryAppState


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
