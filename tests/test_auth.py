from __future__ import annotations

from pathlib import Path

import pytest

from astrid.core import auth


def test_auth_system_notice_is_non_blocking_when_logged_out(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.delenv("HIVEMIND_CONTRIBUTOR_KEY", raising=False)

    notice = auth.contributor_auth_system_notice()

    assert "not logged in" in notice
    assert "ordinary Astrid work remain available" in notice
    assert "astrid login" in notice
    assert "do not block unrelated work" in notice


def test_auth_system_notice_does_not_use_local_key_file(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.delenv("HIVEMIND_CONTRIBUTOR_KEY", raising=False)
    key = "hm_" + "a" * 64
    key_path = tmp_path / ".hivemind" / "key"
    key_path.parent.mkdir()
    key_path.write_text(key, encoding="utf-8")

    notice = auth.contributor_auth_system_notice()

    assert "not logged in" in notice
    assert key not in notice


def test_auth_system_notice_uses_injected_key_without_exposing_it(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    key = "hm_" + "b" * 64
    monkeypatch.setenv("HIVEMIND_CONTRIBUTOR_KEY", key)

    notice = auth.contributor_auth_system_notice()

    assert "logged in" in notice
    assert key not in notice


@pytest.mark.parametrize("system", ["linux", "win32"])
def test_missing_auth_helper_shows_quoted_installed_launcher(monkeypatch, tmp_path, system):
    root = tmp_path / "Astrid's program files"
    monkeypatch.setenv("ASTRID_INSTALL_PROGRAM_ROOT", str(root))
    monkeypatch.setattr(auth.sys, "platform", system)
    monkeypatch.setattr(auth, "_auth_cli_candidates", lambda: [])

    with pytest.raises(auth.AuthCommandError) as error:
        auth._resolve_auth_cli(provision_if_missing=False)

    if system == "win32":
        command = f"& '{str(root / 'run-astrid.cmd').replace(chr(39), chr(39) * 2)}' login"
    else:
        command = f"{auth.shlex.quote(str(root / 'run-astrid'))} login"
    assert f"retry `{command}`" in str(error.value)


def test_run_auth_forwards_to_managed_hivemind_cli(monkeypatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(auth, "_resolve_auth_cli", lambda provision_if_missing: ["python", "cli.py"])
    monkeypatch.setattr(
        auth.subprocess,
        "run",
        lambda argv, check: calls.append(list(argv)) or type("Result", (), {"returncode": 0})(),
    )

    assert auth.run_auth(["login", "--machine", "studio", "--timeout", "42"]) == 0
    assert calls == [[
        "python", "cli.py", "auth", "login", "--machine", "studio", "--ttl", "600",
        "--timeout", "42.0", "--interval", "2.0",
    ]]


def test_gateway_login_aliases_forward_identical_argv_and_exit_code(monkeypatch) -> None:
    from astrid.core.gateway import main as gateway_main

    resolution_requests: list[bool] = []
    subprocess_calls: list[list[str]] = []

    def resolve_auth_cli(*, provision_if_missing: bool) -> list[str]:
        resolution_requests.append(provision_if_missing)
        return ["managed-hivemind-test"]

    def run_managed_cli(argv: list[str], *, check: bool):
        assert check is False
        subprocess_calls.append(list(argv))
        return type("Result", (), {"returncode": 17})()

    monkeypatch.setattr(auth, "_resolve_auth_cli", resolve_auth_cli)
    monkeypatch.setattr(auth.subprocess, "run", run_managed_cli)

    login_args = ["--no-browser", "--machine", "studio", "--ttl", "300", "--timeout", "42", "--interval", "1.5"]
    assert gateway_main(["auth", "login", *login_args]) == 17
    assert gateway_main(["login", *login_args]) == 17

    expected = [
        "managed-hivemind-test", "auth", "login", "--no-browser", "--machine", "studio",
        "--ttl", "300", "--timeout", "42.0", "--interval", "1.5",
    ]
    assert resolution_requests == [True, True]
    assert subprocess_calls == [expected, expected]


def test_gateway_auth_lifecycle_routes_do_not_provision_and_propagate_exit_code(monkeypatch) -> None:
    from astrid.core.gateway import main as gateway_main

    resolution_requests: list[bool] = []
    subprocess_calls: list[list[str]] = []

    def resolve_auth_cli(*, provision_if_missing: bool) -> list[str]:
        resolution_requests.append(provision_if_missing)
        return ["managed-hivemind-test"]

    def run_managed_cli(argv: list[str], *, check: bool):
        assert check is False
        subprocess_calls.append(list(argv))
        return type("Result", (), {"returncode": 29})()

    monkeypatch.setattr(auth, "_resolve_auth_cli", resolve_auth_cli)
    monkeypatch.setattr(auth.subprocess, "run", run_managed_cli)

    # Top-level status belongs to the gateway diagnostics route, so auth status
    # is available only under the auth namespace. Logout and revoke also expose
    # top-level aliases.
    routes = [
        (["auth", "status"], ["managed-hivemind-test", "auth", "status"]),
        (["auth", "logout"], ["managed-hivemind-test", "auth", "logout"]),
        (["logout"], ["managed-hivemind-test", "auth", "logout"]),
        (["auth", "revoke"], ["managed-hivemind-test", "auth", "revoke"]),
        (["revoke"], ["managed-hivemind-test", "auth", "revoke"]),
    ]
    for route, _ in routes:
        assert gateway_main(route) == 29

    assert resolution_requests == [False] * len(routes)
    assert subprocess_calls == [expected for _, expected in routes]
