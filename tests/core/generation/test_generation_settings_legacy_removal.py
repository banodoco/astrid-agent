"""Narrow, Runtime-independent proof of the one-time legacy TOML migration."""

import copy
import logging
from datetime import date

from astrid.core.generation import settings


class InMemoryAppState:
    """A versioned, idempotent in-memory app-state store."""

    def __init__(self):
        self.documents = {}
        self.receipts = {}

    def get_user_app_state(self, key):
        content, version = self.documents.get(key, (None, 0))
        return {"content": copy.deepcopy(content), "version": version}

    def update_user_app_state(self, key, content, *, expected_version, idempotency_key):
        if (key, idempotency_key) in self.receipts:
            return self.receipts[(key, idempotency_key)]
        _, version = self.documents.get(key, (None, 0))
        assert version == expected_version
        self.documents[key] = (copy.deepcopy(dict(content)), version + 1)
        result = {"content": copy.deepcopy(dict(content)), "version": version + 1}
        self.receipts[(key, idempotency_key)] = result
        return result


def test_legacy_import_once(tmp_path, caplog, monkeypatch):
    monkeypatch.delattr(settings.read, "_legacy_import_warning", raising=False)
    store = InMemoryAppState()
    legacy = tmp_path / "config.toml"
    original = b'[runpod]\ngpu_types = ["A40"]\n'
    legacy.write_bytes(original)
    with caplog.at_level(logging.WARNING, logger=settings.__name__):
        first = settings.read(store, legacy_path=legacy)
        assert first.imported
        assert first.version == 1
        assert first.content["machines"]["runpod"]["gpu_types"] == ["A40"]
        assert legacy.read_bytes() == original
        records = [r for r in caplog.records if r.name == settings.__name__]
        assert len(records) == 1
        assert records[0].levelno == logging.WARNING
        assert records[0].getMessage() == (
            "Imported your old VibeComfy [placement]/[runpod] settings into Astrid's Runtime "
            f"settings; this one-time import is removed after {settings.LEGACY_TOML_IMPORT_REMOVE_AFTER}."
        )

        caplog.clear()
        second = settings.read(store, legacy_path=legacy)
        assert not second.imported
        assert second.version == 1
        assert second.content == first.content
        assert legacy.read_bytes() == original

        changed = b'[runpod]\ngpu_types = ["H100"]\n'
        legacy.write_bytes(changed)
        third = settings.read(store, legacy_path=legacy)
        assert not third.imported
        assert third.version == 1
        assert third.content == first.content
        assert legacy.read_bytes() == changed
        assert len(store.receipts) == 1

        # Even another store importing its own document in this process is silent.
        other = settings.read(InMemoryAppState(), legacy_path=legacy)
        assert other.imported
        assert other.content["machines"]["runpod"]["gpu_types"] == ["H100"]
        assert legacy.read_bytes() == changed
        assert not [r for r in caplog.records if r.name == settings.__name__]


def test_removal_date():
    assert date.fromisoformat(settings.LEGACY_TOML_IMPORT_REMOVE_AFTER) > date(2026, 11, 26)


def test_missing_toml_does_not_import_or_warn(tmp_path, caplog, monkeypatch):
    monkeypatch.delattr(settings.read, "_legacy_import_warning", raising=False)
    store = InMemoryAppState()
    legacy = tmp_path / "missing.toml"
    with caplog.at_level(logging.WARNING, logger=settings.__name__):
        snapshot = settings.read(store, legacy_path=legacy)
    assert not snapshot.imported
    assert snapshot.version == 0
    assert snapshot.content == settings.empty_document()
    assert not store.documents
    assert not legacy.exists()
    assert not [r for r in caplog.records if r.name == settings.__name__]
