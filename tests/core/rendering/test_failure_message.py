"""Render failure headlines reflect the root cause, not npm advice."""
from __future__ import annotations

import pytest

from astrid.core.rendering import _remotion_package
from astrid.core.rendering.failure_message import render_failure_message


@pytest.mark.parametrize(
    "cause",
    [
        ("installed Remotion package manifest has unexpected version: "
         f"'{_remotion_package._COMPOSITION_PACKAGE_VERSION}-stale'; "
         f"expected '{_remotion_package._COMPOSITION_PACKAGE_VERSION}'"),
        "installed Remotion package manifest has unexpected name: 'other-package'",
    ],
)
def test_composition_mismatch_has_package_headline_without_node_advice(cause: str):
    advice = "; run `npm ci` in /source/remotion with Node v20.19.4 on PATH"
    message = render_failure_message(
        f"render refused: {cause}{advice}",
        remotion_reason=f"{cause}{advice}",
        environment={"ASTRID_INSTALL_PROGRAM_ROOT": "/managed/astrid"},
    )

    assert message.startswith(
        "Headline: Remotion's installed packages don't match this version of Astrid\n"
    )
    assert f"Detail: render refused: {cause}\n" in message
    assert "Node v20.19.4 on PATH" not in message
    assert "Rerun the Astrid installer to repair this installation" in message


def test_composition_mismatch_keeps_source_checkout_recovery():
    version = _remotion_package._COMPOSITION_PACKAGE_VERSION
    reason = (
        f"installed Remotion package manifest has unexpected version: 'stale'; expected {version}"
    )
    message = render_failure_message(
        reason,
        remotion_reason=reason,
        environment={"ASTRID_REMOTION_PROJECT_DIR": "/source/remotion"},
    )

    assert message.startswith(
        "Headline: Remotion's installed packages don't match this version of Astrid\n"
    )
    assert "npm ci" in message
    assert "/source/remotion" in message
