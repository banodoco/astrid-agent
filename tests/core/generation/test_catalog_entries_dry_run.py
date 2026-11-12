"""Required catalog inputs are admitted through the public generation CLI."""

from astrid.core.model_catalog.registry import ModelRegistry
from scripts.catalog_dry_run import catalog_dry_run
from tests import runpod_guard


def test_catalog_entries_with_required_inputs_dry_run_to_admission(tmp_path, monkeypatch):
    from astrid.core.generation import preflight

    # Admission is the subject; readiness is an external host property.
    monkeypatch.setattr(preflight, "require_generation_target_readiness", lambda *a, **k: None)
    monkeypatch.delenv(runpod_guard.LIVE_OPT_IN_ENV, raising=False)
    registry = ModelRegistry.load_default()
    rows = [(entry, mode) for entry in registry.list_all()
            for mode, spec in entry.modes.items() if spec.requires]
    assert rows
    before = len(runpod_guard.blocked_attempts)
    failures = []
    for entry, mode in rows:
        probe = catalog_dry_run(entry, mode, tmp_path)
        if probe.failure:
            failures.append(f"{entry.id}/{mode}: {probe.failure}")
    assert runpod_guard.blocked_attempts[before:] == []
    assert not failures, "\n".join(failures)
