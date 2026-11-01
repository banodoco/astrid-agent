from __future__ import annotations

import contextlib
import io
import subprocess
import sys
import textwrap
from unittest import mock

from astrid.core import gateway
from astrid.core.contracts.errors import AstridError
from astrid.core.project.runtime import ProjectRuntimeError
from astrid.core.project.schema import ProjectValidationError

pipeline = gateway


def _capture(argv: list[str]) -> tuple[int, str]:
    stderr = io.StringIO()
    with contextlib.redirect_stderr(stderr):
        rc = pipeline.main(argv)
    return rc, stderr.getvalue()


def test_main_renders_astrid_error_without_traceback() -> None:
    err = AstridError(
        "bad transition kind",
        valid_options=("cross-fade", "wipe"),
        recovery_command="astrid timelines transition set --kind cross-fade",
        state_snapshot={"project": "demo"},
    )

    with mock.patch.object(pipeline, "_dispatch", side_effect=err):
        rc, stderr = _capture(["doctor"])

    assert rc == 2
    assert "bad transition kind" in stderr
    assert "valid options: cross-fade, wipe" in stderr
    assert "recovery: astrid timelines transition set --kind cross-fade" in stderr
    assert 'state snapshot: {"project": "demo"}' in stderr
    assert "Traceback" not in stderr


def test_main_wraps_generic_exception_as_degraded_bug_envelope() -> None:
    with mock.patch.object(pipeline, "_dispatch", side_effect=ValueError("boom")):
        rc, stderr = _capture(["doctor"])

    assert rc == 1
    assert stderr.splitlines()[0] == "unstructured - this is a bug."
    assert "boom" in stderr
    assert '"entrypoint": "astrid.core.gateway.main"' in stderr
    assert "Traceback" not in stderr


def test_unknown_top_level_command_renders_structured_recovery() -> None:
    rc, stderr = _capture(["definitely-not-a-command"])

    assert rc == 2
    assert "unknown command 'definitely-not-a-command'" in stderr
    assert "recovery: astrid --help" in stderr
    assert "valid options:" in stderr
    assert "Traceback" not in stderr


# ---------------------------------------------------------------------------
# T17: pipeline renderer edge cases and module-entry proof
# ---------------------------------------------------------------------------


def test_module_entry_delegates_to_pipeline_main() -> None:
    """``python -m astrid`` reaches the same ``gateway.main()`` renderer."""
    from astrid.__main__ import main as module_main
    from astrid.core.gateway import main as gateway_main

    assert module_main is gateway_main


def test_renderer_degraded_minimal_no_valid_options_or_recovery() -> None:
    """Degraded envelope with only a cause still emits the bug flag and exits 1."""
    err = AstridError("something broke", degraded=True)
    with mock.patch.object(pipeline, "_dispatch", side_effect=err):
        rc, stderr = _capture(["doctor"])

    assert rc == 1
    lines = stderr.strip().splitlines()
    assert lines[0] == "unstructured - this is a bug."
    assert "something broke" in lines[1]


def test_renderer_non_degraded_minimal_no_valid_options_or_recovery() -> None:
    """Non-degraded envelope with only a cause prints cause only and exits 2."""
    err = AstridError("simple failure")
    with mock.patch.object(pipeline, "_dispatch", side_effect=err):
        rc, stderr = _capture(["doctor"])

    assert rc == 2
    lines = stderr.strip().splitlines()
    assert lines[0] == "simple failure"
    assert "valid options:" not in stderr
    assert "recovery:" not in stderr
    assert "state snapshot:" not in stderr


def test_subprocess_module_entry_to_renderer_no_traceback() -> None:
    """``python -m astrid`` error path reaches the renderer: no traceback."""

    code = textwrap.dedent("""\
        import sys
        from unittest import mock
        from astrid.core.gateway import main

        from astrid.core.contracts.errors import AstridError

        err = AstridError(
            "bad transition kind",
            valid_options=("cross-fade", "wipe"),
            recovery_command="astrid timelines transition set --kind cross-fade",
            state_snapshot={"project": "demo"},
        )
        with mock.patch("astrid.core.gateway._dispatch", side_effect=err):
            sys.exit(main(["doctor"]))
    """)
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "bad transition kind" in result.stderr
    assert "valid options: cross-fade, wipe" in result.stderr
    assert "recovery: astrid timelines transition set --kind cross-fade" in result.stderr
    assert 'state snapshot: {"project": "demo"}' in result.stderr
    assert "Traceback" not in result.stderr


def test_subprocess_degraded_bug_envelope_no_traceback() -> None:
    """Generic exception through ``pipeline.main()`` → degraded envelope, no traceback."""

    code = textwrap.dedent("""\
        import sys
        from unittest import mock
        from astrid.core.gateway import main

        with mock.patch("astrid.core.gateway._dispatch", side_effect=ValueError("boom")):
            sys.exit(main(["doctor"]))
    """)
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert result.stderr.splitlines()[0] == "unstructured - this is a bug."
    assert "boom" in result.stderr
    assert '"entrypoint": "astrid.core.gateway.main"' in result.stderr
    assert "Traceback" not in result.stderr


# ============================================================================
# T19: migrated core errors flowing through the universal renderer
# ============================================================================


def test_project_validation_error_renders_via_envelope_no_prefix() -> None:
    """ProjectValidationError routed through pipeline.main() → envelope, no bespoke prefix."""
    err = ProjectValidationError("no project file at /tmp/X.json")
    with mock.patch.object(pipeline, "_dispatch", side_effect=err):
        rc, stderr = _capture(["projects", "show"])
    assert rc == 2
    assert "no project file at /tmp/X.json" in stderr
    assert "project:" not in stderr  # no bespoke prefix
    assert "Traceback" not in stderr


def test_project_runtime_error_renders_via_envelope() -> None:
    """ProjectRuntimeError routed through pipeline.main() → envelope."""
    err = ProjectRuntimeError("project 'demo' already exists")
    with mock.patch.object(pipeline, "_dispatch", side_effect=err):
        rc, stderr = _capture(["projects", "create", "demo"])
    assert rc == 2
    assert "project 'demo' already exists" in stderr
    assert "Traceback" not in stderr


def test_project_validation_error_renders_via_envelope() -> None:
    """ProjectValidationError routed through pipeline.main() → envelope."""
    err = ProjectValidationError("source.kind must be one of {...}")
    with mock.patch.object(pipeline, "_dispatch", side_effect=err):
        rc, stderr = _capture(["projects", "show"])
    assert rc == 2
    assert "source.kind must be one of {...}" in stderr
    assert "Traceback" not in stderr
