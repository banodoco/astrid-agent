"""Configure the provisioned runtime for integration renders."""

from __future__ import annotations

import pytest

from tests.helpers.remotion_runtime import configure_remotion_runtime, remotion_install_problems
from tests.packs.rendering._helpers import _execution_env


@pytest.fixture
def renderer_execution_environment():
    """Keep nested backend commands on the test process runtime.

    Renderer manifests use ``python3`` and ``node`` from PATH, so relying on the shell's ambient PATH can
    select a different interpreter that lacks the canonical timeline-schema package.
    """

    with _execution_env():
        yield


@pytest.fixture
def remotion_runtime_environment(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    renderer_execution_environment: None,
) -> None:
    """Configure the runtime normally selected by host bootstrap."""

    problems = remotion_install_problems()
    if problems:
        pytest.fail(
            "a real render needs the provisioned Remotion install (run `make remotion-install`): " + "; ".join(problems),
            pytrace=False,
        )
    configure_remotion_runtime(monkeypatch)
    from tests.helpers.remotion_validation import issue_remotion_launch_validation

    issue_remotion_launch_validation(tmp_path, monkeypatch)
