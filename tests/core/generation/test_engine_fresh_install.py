"""Regression coverage for custody startup during the first engine install."""
from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest

from astrid.core.generation import engine_custody as custody
from astrid.core.generation import engine_runner
from astrid.core.generation.engine_execution import LocalLinuxExecutionContext
from astrid.core.generation.engine_platforms import platform_adapter
from tests.core.generation import test_engine_custody as cpu_fixtures

pytestmark = pytest.mark.linux_native
session = cpu_fixtures.session


@pytest.fixture
def fresh_cpu_install(session, monkeypatch):
    """Reuse the real CPU inventory but include a models-root launch argument."""
    fixture_generator = cpu_fixtures.cpu_h3_install.__wrapped__(
        session, monkeypatch,
        SimpleNamespace(extra_launch_flags=("--models-directory", "{models_root}")),
    )
    fixture = next(fixture_generator)
    try:
        yield fixture
    finally:
        try:
            next(fixture_generator)
        except StopIteration:
            pass


@pytest.mark.parametrize("external_models", [False, True], ids=["default-models", "external-model-volume"])
def test_fresh_install_custody_uses_receipt_models_root(tmp_path, monkeypatch, fresh_cpu_install, external_models):
    """A receipt-less supervisor must admit the launch bound by install_engine."""
    client, _payload, recipe, root, started, _program = fresh_cpu_install
    receipt_path = root / ".astrid-engine/install.json"
    # The CPU scaffold's first install has already proved the loopback child.
    # Retire both processes and remove its synthetic receipt to exercise a
    # fresh runner install and a supervisor born at helper identity.
    assert receipt_path.is_file()
    assert started.is_file()
    client.stop_owned(deadline=time.monotonic() + 15)
    receipt_path.unlink()
    default_models = root / "runtime/ComfyUI/models"
    if default_models.exists():
        default_models.rmdir()

    models_path = (tmp_path / "volume/astrid/engines/comfyui/models" if external_models
                   else default_models)
    models_root = str(models_path) if external_models else None
    receipts_at_boot = []
    supervisor_argv = custody._supervisor_argv

    def observe_boot(*args):
        receipts_at_boot.append(receipt_path.exists())
        return supervisor_argv(*args)

    monkeypatch.setattr(custody, "_supervisor_argv", observe_boot)

    adapter = platform_adapter(recipe.platform)
    context = LocalLinuxExecutionContext(engine=recipe.engine, platform=recipe.platform,
                                         root=str(root), adapter=adapter, service_client=client)
    plan = engine_runner.plan_install(recipe, root=str(root), placement="local", models_root=models_root)
    installed = engine_runner.install_engine(plan, context=context, adapter=adapter,
                                             deadline=time.monotonic() + 90)

    assert installed.status == "installed"
    assert receipts_at_boot == [False]
    assert models_path.is_dir()
    committed = json.loads(receipt_path.read_text())
    assert committed["models_root"] == models_root
    registry = root / "runtime/out/sessions/comfyui"
    config = json.loads((registry / "config.json").read_text())
    expected_models = models_root or str(root / "runtime/ComfyUI/models")
    assert config["models_root"] == expected_models
    assert engine_runner.engine_status(recipe.engine, platform=recipe.platform, placement="local",
        root=str(root), context=context, deadline=time.monotonic() + 30).status == "installed"
    assert len(started.read_text().splitlines()) == 2
