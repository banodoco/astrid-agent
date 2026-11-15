"""A restored environment receipt is never trusted on its own (P6 engine snapshot).

A snapshot restore copies the inert environment (venv, environment receipt and
requirements.lock) onto the container-disk root, never ``install.json``. Through the
real ``engine_runner`` environment step, a receipt that says validated but whose venv
content was altered must fail ``environment_probe``; the install then reinstalls that
environment cleanly, and the read-only status never reports it installed.
"""

from __future__ import annotations

from astrid.core.generation import engine_runner as runner
from astrid.core.generation.engine_contracts import FileFact
from tests.core.generation.test_engine_runner import ROOT, install, setup, status  # noqa: F401


def _package_file(context) -> str:
    environment = context.recipe.environments[0]
    return context.adapter.join(ROOT, environment.relative_path, "lib/python3/site-packages/pkg/__init__.py")


def _with_package_file(context):
    """The probe needs one installed package file; ``pip sync`` puts it back, reset removes it."""
    package = _package_file(context)
    original = context.run

    def run(request):
        argv = request.argv
        if argv[0].endswith("/uv") and "sync" in argv:
            context.files[package] = FileFact(True, True, False, 10, "d" * 64)
        if (argv[0].endswith("/bin/python") and "-c" in argv and "fake_environment" in argv[-1]
                and package not in context.files):
            context.events.append(("probe-refused", package, request.deadline))
            return context.result(code=1)
        return original(request)

    context.run = run
    return package


def _environment_receipt(context):
    name = context.recipe.environments[0].name
    return context.files[context.adapter.join(ROOT, ".astrid-engine/environments", name, "receipt.json")]


def _restored_tampered(setup):
    """Installed once, then as restored onto a fresh pod: no install.json, no server, a package file gone."""
    plan, context, _adapter, _deadline = setup
    package = _with_package_file(context)
    assert install(setup).status == "installed"
    assert package in context.files and _environment_receipt(context)["validated"] is True
    context.files.pop(ROOT + "/" + runner._RECEIPT)
    context.server = None
    del context.files[package]
    context.events.clear()
    return context, package


def _helper_operations(context):
    return [event[1][-2] for event in context.events
            if event[0] == "argv" and "engine_runner_target" in " ".join(event[1])]


def test_status_after_restore_before_install_is_not_installed_and_does_not_raise(setup):
    context, _package = _restored_tampered(setup)
    assert status(setup) is None
    assert not any(event[0] in {"write", "mkdir", "service:start"} for event in context.events)


def test_install_reinstalls_a_tampered_restored_environment(setup):
    context, package = _restored_tampered(setup)
    receipt_before = _environment_receipt(context)
    assert receipt_before["validated"] is True  # the restored receipt claims a validated environment

    installed = install(setup)  # no IntegrityError: a failed probe is a clean reinstall

    assert installed.status == "installed"
    assert any(event[0] == "probe-refused" for event in context.events)
    operations = _helper_operations(context)
    assert "reset-environment" in operations
    # Same lock reused (no re-resolution), then the environment is rebuilt and re-validated.
    commands = [event[1] for event in context.events if event[0] == "argv"]
    assert not any("compile" in command for command in commands)
    assert any(command[0].endswith("/uv") and "sync" in command for command in commands)
    receipt = _environment_receipt(context)
    assert receipt["validated"] is True
    assert receipt["requirements_lock_sha256"] == receipt_before["requirements_lock_sha256"]
    assert package in context.files
    assert status(setup).status == "installed"


def test_readonly_status_on_a_tampered_environment_never_reports_installed(setup):
    plan, context, _adapter, _deadline = setup
    package = _with_package_file(context)
    assert install(setup).status == "installed"
    del context.files[package]  # receipt and install.json intact; venv content altered
    context.events.clear()
    # The runner's readonly IntegrityError ("installed environment no longer matches its lock")
    # is caught by engine_status and reported as a failed status, which sends placement to install.
    result = status(setup)
    assert result is not None and result.status == "failed"
    assert (result.error_code, result.failed_step_id) == ("integrity_failed", "environment:comfy")
    assert "reset-environment" not in _helper_operations(context)
    assert package not in context.files
