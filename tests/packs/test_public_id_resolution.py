"""Public canonical-id resolution parity.

Verifies that the shipped canonical ids resolve through the default executor /
orchestrator registries and that retired ids are not reintroduced.
"""

from __future__ import annotations

import pytest

from astrid.core.execution.executor.registry import load_default_registry as load_executor_registry
from astrid.core.execution.orchestrator.registry import (
    load_default_registry as load_orchestrator_registry,
)

# Representative canonical ids across the shipped external and built-in packs.
PRESERVED_EXECUTOR_IDS = [
    "runpod.provision",
    "runpod.exec",
    "runpod.teardown",
    "runpod.session",
    "vibecomfy.run",
    "vibecomfy.validate",
    # One canonical id per remaining pack — sanity checks for common cases.
    "training.pool_build",
]

# One canonical orchestrator per pack that ships orchestrators.
PRESERVED_ORCHESTRATOR_IDS = [
    "h3_av.transform",
]


@pytest.fixture(scope="module")
def executor_registry():
    # keeps discovery scoped to the repo's own packs:
    # the default scan also loads ~/.astrid/packs, which contains an external
    # hivemind pack whose executor.yaml fails validation (713-char description
    # > 500 max) — that failure is not this tree's to fix.
    return load_executor_registry()


@pytest.fixture(scope="module")
def orchestrator_registry():
    # Mirror the executor fixture: scope discovery to the repo's own packs.
    # The orchestrator registry validates child executor references, so it
    # needs the same scoped executor registry threaded through — otherwise
    # its validation falls back to the unscoped default, which trips over
    # the broken installed hivemind pack in ~/.astrid/packs.
    executor_registry = load_executor_registry()
    return load_orchestrator_registry(
        executor_registry=executor_registry,
    )


@pytest.mark.parametrize("public_id", PRESERVED_EXECUTOR_IDS)
def test_preserved_executor_id_resolves(public_id, executor_registry):
    executor = executor_registry.get(public_id)
    assert executor is not None, f"{public_id!r} did not resolve"
    assert executor.id == public_id
    # And the first segment still matches its owning pack, i.e. no silent
    # rename slipped through the migration.
    assert executor.metadata.get("source_pack") == public_id.split(".", 1)[0]


@pytest.mark.parametrize("public_id", PRESERVED_ORCHESTRATOR_IDS)
def test_preserved_orchestrator_id_resolves(public_id, orchestrator_registry):
    orchestrator = orchestrator_registry.get(public_id)
    assert orchestrator is not None, f"{public_id!r} did not resolve"
    assert orchestrator.id == public_id
    assert (
        orchestrator.metadata.get("source_pack") == public_id.split(".", 1)[0]
    )


def test_retired_iteration_prepare_is_not_registered(executor_registry):
    assert all(item.id != "iteration.prepare" for item in executor_registry.list())
    with pytest.raises(KeyError, match="unknown executor"):
        executor_registry.get("iteration.prepare")
