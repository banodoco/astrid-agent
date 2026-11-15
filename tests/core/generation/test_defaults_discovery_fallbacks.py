from __future__ import annotations

import logging

from astrid.core.generation import defaults, entries, workflows


def _assert_debug_fallback(caplog, message, failure):
    records = [record for record in caplog.records if record.name == defaults._logger.name]
    assert len(records) == 1
    record = records[0]
    assert record.levelno == logging.DEBUG
    assert record.getMessage() == message
    assert record.exc_info[1] is failure
    assert f"RuntimeError: {failure}" in caplog.text


def test_model_catalog_discovery_failure_keeps_fallback_rows(monkeypatch, caplog, capsys):
    from astrid.core.model_catalog.registry import ModelRegistry

    failure = RuntimeError("catalog discovery unavailable")

    class Catalog:
        def list_all(self, *, include_closed):
            raise failure

    monkeypatch.setattr(defaults, "pack_candidates", lambda registry=None: [])
    monkeypatch.setattr(workflows, "workflow_entries", lambda registry: [])
    monkeypatch.setattr(entries, "user_entries", lambda: [])
    monkeypatch.setattr(entries, "ready_entries", lambda: [])
    monkeypatch.setattr(entries, "action_entries", lambda: [])
    monkeypatch.setattr(ModelRegistry, "load_default", classmethod(lambda cls: Catalog()))
    caplog.set_level(logging.DEBUG, logger=defaults._logger.name)

    assert defaults.known_entries() == []

    _assert_debug_fallback(
        caplog, "Could not discover model catalog entries for generation defaults", failure
    )
    assert "Traceback" not in capsys.readouterr().err


def test_workflow_and_action_discovery_failure_keeps_fallback_rows(monkeypatch, caplog, capsys):
    failure = RuntimeError("workflow entries unavailable")

    def fail():
        raise failure

    monkeypatch.setattr(defaults, "pack_candidates", lambda registry=None: [])
    monkeypatch.setattr(workflows, "workflow_entries", lambda registry: [])
    # Keep the separate catalog fallback independent of this test.
    from astrid.core.model_catalog.registry import ModelRegistry

    monkeypatch.setattr(ModelRegistry, "load_default", classmethod(lambda cls: type("Catalog", (), {"list_all": lambda self, **kw: []})()))
    monkeypatch.setattr(entries, "user_entries", fail)
    caplog.set_level(logging.DEBUG, logger=defaults._logger.name)

    assert defaults.known_entries() == []

    _assert_debug_fallback(
        caplog, "Could not discover workflow and action entries for generation defaults", failure
    )
    assert "Traceback" not in capsys.readouterr().err


def test_contributed_workflow_discovery_failure_keeps_fallback_rows(monkeypatch, caplog, capsys):
    failure = RuntimeError("contributed workflows unavailable")

    def fail(registry):
        raise failure

    monkeypatch.setattr(defaults, "pack_candidates", lambda registry=None: [])
    from astrid.core.model_catalog.registry import ModelRegistry

    monkeypatch.setattr(ModelRegistry, "load_default", classmethod(lambda cls: type("Catalog", (), {"list_all": lambda self, **kw: []})()))
    monkeypatch.setattr(entries, "user_entries", lambda: [])
    monkeypatch.setattr(entries, "ready_entries", lambda: [])
    monkeypatch.setattr(entries, "action_entries", lambda: [])
    monkeypatch.setattr(workflows, "workflow_entries", fail)
    caplog.set_level(logging.DEBUG, logger=defaults._logger.name)

    assert defaults.known_entries(registry=object()) == []

    _assert_debug_fallback(
        caplog, "Could not discover contributed workflow entries for generation defaults", failure
    )
    assert "Traceback" not in capsys.readouterr().err
