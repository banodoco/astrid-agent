"""Tests for the canonical Astrid credential source policy."""

from __future__ import annotations

from pathlib import Path

import pytest

from astrid.core.contracts.errors import AstridError
from astrid.core.contracts.scoped_config import SCOPE_REGISTRY, ScopeRequest
from astrid.core.util.credentials_scope import CredentialsScope

PROVIDER_ENV = {
    "fal": "FAL_KEY",
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "fireworks": "FIREWORKS_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "giphy": "GIPHY_API_KEY",
    "huggingface": "HF_TOKEN",
    "runpod": "RUNPOD_API_KEY",
    "wavespeed": "WAVESPEED_API_KEY",
}


@pytest.mark.parametrize("provider,env_var", PROVIDER_ENV.items())
def test_local_credentials_resolve_from_shared_file(
    provider: str, env_var: str, monkeypatch, tmp_path: Path, caplog
) -> None:
    secret = f"local-test-{provider}-secret"
    shared_file = tmp_path / "astrid.env"
    shared_file.write_text(f"{env_var}={secret}\n", encoding="utf-8")
    monkeypatch.setenv("ASTRID_ENV_FILE", str(shared_file))
    monkeypatch.setenv(env_var, f"stale-{provider}-key")
    caplog.set_level("DEBUG", logger="astrid.credentials")

    credential = CredentialsScope.resolve_local(provider)

    assert credential.provider == provider
    assert credential.reference == env_var
    assert credential.source == "astrid_env_file"
    assert credential.value == secret
    assert secret not in repr(credential)
    assert env_var in caplog.text
    assert "source=astrid_env_file" in caplog.text
    assert secret not in caplog.text


@pytest.mark.parametrize("provider,env_var", PROVIDER_ENV.items())
def test_get_reads_the_same_shared_file(provider: str, env_var: str, monkeypatch, tmp_path: Path) -> None:
    shared_file = tmp_path / "astrid.env"
    shared_file.write_text(f"{env_var}=shared-{provider}\n", encoding="utf-8")
    monkeypatch.setenv("ASTRID_ENV_FILE", str(shared_file))
    monkeypatch.setenv(env_var, f"stale-{provider}")

    assert CredentialsScope.get(provider) == f"shared-{provider}"


def test_explicit_value_wins_over_shared_file(monkeypatch, tmp_path: Path) -> None:
    shared_file = tmp_path / "astrid.env"
    shared_file.write_text("FAL_KEY=from-shared-file\n", encoding="utf-8")
    monkeypatch.setenv("ASTRID_ENV_FILE", str(shared_file))

    assert CredentialsScope.get("fal", explicit="from-explicit") == "from-explicit"


def test_named_env_file_is_only_a_fallback(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ASTRID_ENV_FILE", str(tmp_path / "absent-shared.env"))
    monkeypatch.delenv("FAL_KEY", raising=False)
    fallback = tmp_path / "project.env"
    fallback.write_text("FAL_KEY=from-project-file\n", encoding="utf-8")

    assert CredentialsScope.get("fal", env_file=fallback) == "from-project-file"


def test_missing_key_raises_actionable_astrid_error(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ASTRID_ENV_FILE", str(tmp_path / "absent-shared.env"))
    monkeypatch.delenv("FAL_KEY", raising=False)

    with pytest.raises(AstridError) as exc_info:
        CredentialsScope.get("fal")
    assert "FAL_KEY not found" in str(exc_info.value.cause)
    assert "shared astrid.env" in exc_info.value.recovery_command


def test_action_worker_reads_only_declared_injected_secret(monkeypatch, tmp_path: Path) -> None:
    # Both values are synthetic. A worker must ignore the credential file and
    # consume only the value the host injected for this action.
    secret_file = tmp_path / "fake-astrid.env"
    secret_file.write_text("RUNPOD_API_KEY=fake-file-value\n", encoding="utf-8")
    monkeypatch.setenv("ASTRID_ENV_FILE", str(secret_file))
    monkeypatch.setenv("ASTRID_ACTION_ID", "runpod.provision")
    monkeypatch.setenv("ASTRID_ACTION_ENV_ALLOWLIST", '["RUNPOD_API_KEY"]')
    monkeypatch.setenv("RUNPOD_API_KEY", "fake-injected-value")

    assert CredentialsScope.get("runpod") == "fake-injected-value"


def test_action_worker_undeclared_secret_fails_without_file_fallback(
    monkeypatch, tmp_path: Path
) -> None:
    secret_file = tmp_path / "fake-astrid.env"
    secret_file.write_text("OPENAI_API_KEY=fake-file-value\n", encoding="utf-8")
    monkeypatch.setenv("ASTRID_ENV_FILE", str(secret_file))
    monkeypatch.setenv("ASTRID_ACTION_ID", "h3_av.transform")
    monkeypatch.setenv("ASTRID_ACTION_ENV_ALLOWLIST", "[]")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    with pytest.raises(AstridError) as exc_info:
        CredentialsScope.get("openai")

    assert exc_info.value.code == "undeclared_secret"
    assert "h3_av.transform" in str(exc_info.value)
    assert "actions.transform.secrets" in exc_info.value.recovery_command


def test_unknown_provider_raises_astrid_error() -> None:
    with pytest.raises(AstridError) as exc_info:
        CredentialsScope.get("nonexistent")
    assert "Unknown credentials provider" in str(exc_info.value.cause)
    assert "nonexistent" in str(exc_info.value.cause)


def test_secret_scrubbing_still_works() -> None:
    from astrid.core.util.secrets import scrub_secret

    result = scrub_secret("secret123", "prefix secret123 suffix")
    assert "secret123" not in result
    assert "***" in result


@pytest.mark.parametrize("provider", PROVIDER_ENV)
def test_provider_registered_in_scope_registry(provider: str) -> None:
    assert SCOPE_REGISTRY.is_registered(f"credentials.{provider}")


def test_credentials_scope_is_scoped_config_and_frozen() -> None:
    from astrid.core.contracts.scoped_config import ScopedConfig

    assert issubclass(CredentialsScope, ScopedConfig)
    scope = CredentialsScope(provider="fal", value="key123")
    with pytest.raises(Exception):
        scope.value = "hacked"  # type: ignore[misc]


def test_registry_explicit_and_request_environment(monkeypatch, tmp_path: Path) -> None:
    isolated_file = tmp_path / "missing.env"
    scope = SCOPE_REGISTRY.resolve(
        "credentials.fal",
        ScopeRequest(
            explicit={"credentials.fal": "from-explicit"},
            env={"ASTRID_ENV_FILE": str(isolated_file)},
        ),
    )
    assert scope is not None and scope.value == "from-explicit"

    scope = SCOPE_REGISTRY.resolve(
        "credentials.fal",
        ScopeRequest(env={"FAL_KEY": "from-request-env", "ASTRID_ENV_FILE": str(isolated_file)}),
    )
    assert scope is not None and scope.value == "from-request-env"

    # The request's environment mapping is authoritative: process env is not
    # consulted when the supplied mapping misses a reference.
    monkeypatch.setenv("FAL_KEY", "ambient-process-value")
    with pytest.raises(AstridError):
        SCOPE_REGISTRY.resolve(
            "credentials.fal",
            ScopeRequest(env={"OTHER": "x", "ASTRID_ENV_FILE": str(isolated_file)}),
        )
