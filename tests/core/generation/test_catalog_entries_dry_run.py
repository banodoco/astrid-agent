"""Live-cell inputs are admitted through the public generation CLI."""

import pytest

from astrid.core.model_catalog.registry import ModelRegistry
from scripts.catalog_dry_run import catalog_dry_run, ready_request_entry
from scripts.preflight import live_cells
from tests import runpod_guard


# Restore all entries when #607 and the contract fixes land; --all-catalog reports them meanwhile.
@pytest.mark.parametrize("cell", live_cells(), ids=lambda cell: cell["cell"])
def test_catalog_entries_with_required_inputs_dry_run_to_admission(cell, tmp_path, monkeypatch):
    from astrid.core.generation import preflight

    # Admission is the subject; readiness is an external host property.
    monkeypatch.setattr(preflight, "require_generation_target_readiness", lambda *a, **k: None)
    monkeypatch.delenv(runpod_guard.LIVE_OPT_IN_ENV, raising=False)
    registry = ModelRegistry.load_default()
    try:
        entry = registry.get(cell["entry"])
    except KeyError:
        entry = ready_request_entry(cell["entry"], cell["mode"], cell["kind"], registry)
    before = len(runpod_guard.blocked_attempts)
    probe = catalog_dry_run(entry, cell["mode"], tmp_path)
    assert runpod_guard.blocked_attempts[before:] == []
    assert not probe.failure, probe.failure


@pytest.mark.parametrize("error", [
    "managed model has no declared digest: fixture.safetensors",
    "ready template render failed",
])
def test_catalog_dry_run_refuses_invalid_ready_template_declarations(tmp_path, monkeypatch, error):
    from astrid.core.generation import managed_preparation
    from astrid.core.generation.engine_contracts import IntegrityError

    def missing_declarations(*_args, **_kwargs):
        raise IntegrityError(error)

    monkeypatch.setattr(managed_preparation, "judge_declarations", missing_declarations)
    entry = ModelRegistry.load_default().get("z-image")

    probe = catalog_dry_run(entry, "t2i", tmp_path)

    assert probe.failure
    assert error in probe.failure
