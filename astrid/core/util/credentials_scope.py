"""CredentialsScope — scoped API key resolution (tier-3).

Provides a single in-process credentials resolver that delegates to
``astrid.core.util.secrets.load_api_key``. One ``SCOPE_REGISTRY`` entry is
registered per ``credentials.<provider>`` key; each resolver returns a
``CredentialsScope`` populated with that provider's resolved value (or
raises ``AstridError`` when the key is missing).

Local integrations use ``CredentialsScope.get_local`` or
``CredentialsScope.resolve_local`` to read Astrid's shared user-level
``astrid.env`` file. ``CredentialsScope.get`` follows the same source policy.

Resolution precedence:

1. explicit option (caller-supplied key, ``ScopeRequest.explicit`` or the
   ``explicit`` argument);
2. shared ``astrid.env`` file;
3. process environment when the shared file has no value;
4. optional explicitly named fallback file.

Broad cwd/repository/workspace/home env-file scavenging is absent.

Canonical provider → env-var table
----------------------------------
fal        → ``FAL_KEY``
wavespeed  → ``WAVESPEED_API_KEY``
openai     → ``OPENAI_API_KEY``
anthropic  → ``ANTHROPIC_API_KEY``
deepseek   → ``DEEPSEEK_API_KEY``
fireworks  → ``FIREWORKS_API_KEY``
gemini     → ``GEMINI_API_KEY``
giphy      → ``GIPHY_API_KEY``
huggingface → ``HF_TOKEN``
runpod     → ``RUNPOD_API_KEY``
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from astrid.core.contracts.errors import AstridError
from astrid.core.contracts.scoped_config import (
    SCOPE_REGISTRY,
    ScopedConfig,
    ScopeRequest,
)
from astrid.core.util.secrets import (
    load_api_key,
    load_local_api_key_with_source,
)

# ---------------------------------------------------------------------------
# Canonical provider → env-var table
# ---------------------------------------------------------------------------

_PROVIDER_ENV: dict[str, str] = {
    "fal": "FAL_KEY",
    "wavespeed": "WAVESPEED_API_KEY",
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "fireworks": "FIREWORKS_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "giphy": "GIPHY_API_KEY",
    "huggingface": "HF_TOKEN",
    "runpod": "RUNPOD_API_KEY",
}

_LOG = logging.getLogger("astrid.credentials")
_ACTION_ID_ENV = "ASTRID_ACTION_ID"
_ACTION_SECRETS_ENV = "ASTRID_ACTION_ENV_ALLOWLIST"


def _injected_worker_credential(
    reference: str, environ: object | None
) -> tuple[bool, str | None]:
    """Resolve a secret inside an action worker without consulting files.

    The host sets the action marker and its manifest declaration alongside
    the injected environment. Once present, this policy is authoritative for
    every CredentialsScope API, including the scope registry.
    """
    get = getattr(environ or {}, "get")
    action_id = get(_ACTION_ID_ENV)
    if not action_id:
        return False, None
    try:
        declared = json.loads(get(_ACTION_SECRETS_ENV, "[]"))
    except (TypeError, ValueError) as exc:
        raise AstridError(
            "action secret declaration is invalid",
            code="invalid_action_secrets",
            recovery_command="repair the action's secrets declaration and retry",
        ) from exc
    if not isinstance(declared, list) or any(not isinstance(name, str) for name in declared):
        raise AstridError(
            "action secret declaration is invalid",
            code="invalid_action_secrets",
            recovery_command="repair the action's secrets declaration and retry",
        )
    local_id = str(action_id).split(".", 1)[-1]
    if reference not in declared:
        raise AstridError(
            f"secret {reference} is not declared for action {action_id}",
            code="undeclared_secret",
            field=reference,
            recovery_command=f"declare {reference} in actions.{local_id}.secrets and retry",
        )
    value = get(reference)
    if not value:
        raise AstridError(
            f"declared secret {reference} was not injected for action {action_id}",
            code="missing_declared_secret",
            field=reference,
            recovery_command=f"configure {reference} for actions.{local_id}.secrets and retry",
        )
    return True, str(value)

# ---------------------------------------------------------------------------
# CredentialsScope
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CredentialsScope(ScopedConfig):
    """Resolved credentials for a single provider.

    Instances are returned by the per-provider resolvers registered in
    ``SCOPE_REGISTRY`` under ``credentials.<provider>`` keys.
    """

    provider: str
    """Provider key (e.g. ``"fal"``, ``"openai"``)."""

    value: str
    """The resolved API key value."""

    @classmethod
    def resolve_local(
        cls,
        provider: str,
        *,
        env_var: str | None = None,
        env_file: Path | None = None,
        explicit: str | None = None,
    ) -> "ResolvedCredential":
        """Resolve a credential for a local Astrid command with safe provenance.

        Local resolution uses the shared precedence: explicit value, Astrid's
        shared ``astrid.env`` file, process environment, then a specifically
        named fallback env file. The shared file path defaults to
        ``~/.astrid/astrid.env`` and can be overridden with ``ASTRID_ENV_FILE``.
        """

        canonical_env_var = _PROVIDER_ENV.get(provider)
        if canonical_env_var is None:
            # Reuse the canonical unknown-provider error and recovery text.
            cls.get(provider)
            raise AssertionError("unreachable")  # pragma: no cover

        reference = env_var or canonical_env_var
        in_worker, injected = _injected_worker_credential(reference, os.environ)
        if in_worker:
            _LOG.debug(
                "Resolved action credential provider=%s reference=%s source=environment",
                provider,
                reference,
            )
            return ResolvedCredential(
                provider=provider,
                reference=reference,
                source="environment",
                value=injected or "",
            )
        value, source = load_local_api_key_with_source(
            reference,
            env_file=env_file,
            explicit=explicit,
        )
        _LOG.debug(
            "Resolved local credential provider=%s reference=%s source=%s",
            provider,
            reference,
            source,
        )
        return ResolvedCredential(
            provider=provider,
            reference=reference,
            source=source,
            value=value,
        )

    @classmethod
    def get_local(
        cls,
        provider: str,
        *,
        env_var: str | None = None,
        env_file: Path | None = None,
        explicit: str | None = None,
    ) -> str:
        """Return the resolved value for a local Astrid command."""

        return cls.resolve_local(
            provider,
            env_var=env_var,
            env_file=env_file,
            explicit=explicit,
        ).value

    @classmethod
    def get(
        cls,
        provider: str,
        *,
        env_var: str | None = None,
        env_file: Path | None = None,
        explicit: str | None = None,
    ) -> str:
        """Resolve *provider*'s API key using Astrid's shared source policy.

        Args:
            provider: Canonical provider key (must be in ``_PROVIDER_ENV``).
            env_var: Optional override for the credential environment
                     reference, used by compute profiles.
            env_file: Optional explicitly named ``.env`` fallback file.
            explicit: Optional caller-supplied explicit value (highest
                      priority tier).

        Returns:
            The resolved API key string.

        Raises:
            AstridError: If the provider is unknown or the key is not found.
        """
        canonical_env_var = _PROVIDER_ENV.get(provider)
        if canonical_env_var is None:
            valid = ", ".join(sorted(_PROVIDER_ENV))
            raise AstridError(
                f"Unknown credentials provider: {provider!r} (valid: {valid})",
                recovery_command="use a canonical provider name (fal, openai, anthropic, deepseek, fireworks, gemini, giphy, huggingface, runpod)",
            )
        resolved_env_var = env_var or canonical_env_var
        in_worker, injected = _injected_worker_credential(resolved_env_var, os.environ)
        if in_worker:
            return injected or ""
        return load_api_key(
            resolved_env_var,
            env_file=env_file,
            explicit=explicit,
        )


@dataclass(frozen=True)
class ResolvedCredential:
    """Credential value plus safe, non-secret resolution provenance."""

    provider: str
    reference: str
    source: Literal["explicit", "astrid_env_file", "environment", "env_file"]
    value: str = field(repr=False)


# ---------------------------------------------------------------------------
# Per-provider scope resolvers
# ---------------------------------------------------------------------------

def _make_resolver(provider: str, env_var: str):
    """Factory: create a scope resolver for *provider*."""

    def _resolve(request: ScopeRequest) -> CredentialsScope:
        in_worker, injected = _injected_worker_credential(env_var, os.environ)
        if not in_worker:
            in_worker, injected = _injected_worker_credential(
                env_var, request.env if request.env is not None else os.environ
            )
        if in_worker:
            return CredentialsScope(provider=provider, value=injected or "")
        # Tier 1: explicit overrides take priority (caller-supplied key).
        # ScopeRequest.explicit keys are scope keys ("credentials.fal"); the
        # bare provider key ("fal") is also accepted for compatibility.
        explicit = request.explicit
        if explicit is not None:
            for key in (f"credentials.{provider}", provider):
                value = explicit.get(key)
                if value is not None:
                    return CredentialsScope(provider=provider, value=str(value))

        # The shared user-level file is the local source of truth; request.env
        # remains a fallback for CI and deployed environments without it.
        try:
            value, source = load_local_api_key_with_source(env_var, environ=request.env)
        except AstridError:
            raise  # re-raise — missing credentials are fatal
        _LOG.debug(
            "Resolved scoped credential provider=%s reference=%s source=%s",
            provider,
            env_var,
            source,
        )
        return CredentialsScope(provider=provider, value=value)

    return _resolve


# Register one resolver per provider at import time.
for _provider, _env_var in _PROVIDER_ENV.items():
    SCOPE_REGISTRY.register(
        f"credentials.{_provider}",
        _make_resolver(_provider, _env_var),
    )
