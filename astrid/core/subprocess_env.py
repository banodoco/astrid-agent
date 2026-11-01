"""Canonical child-process environment policy."""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Iterable, Mapping

from astrid.core.env_vars import (
    ASTRID_ACTOR,
    ASTRID_AUTHOR_TEST,
    ASTRID_ENV_FILE,
    ASTRID_INTERNAL_INVOCATION,
    ASTRID_NODE_EXECUTABLE,
    ASTRID_REMOTION_VALIDATION_PATH,
    ASTRID_REMOTION_VALIDATION_SHA256,
    ASTRID_REMOTION_VALIDATION_LAUNCH,
    ASTRID_REMOTION_VALIDATION_ROLE,
    ASTRID_REMOTION_PROJECT_DIR,
    ASTRID_VIBECOMFY_CHECKOUT,
)
from astrid.core.env_vars import (
    ASTRID_HOME as ASTRID_HOME_ENV,
)
from astrid.core.env_vars import (
    ASTRID_PACKS_PATH as PACKS_PATH_ENV,
)
from astrid.core.env_vars import (
    ASTRID_PROJECT_RUN as PROJECT_RUN_ENV,
)
from astrid.core.env_vars import (
    ASTRID_PROJECT_SLUG as PROJECT_SLUG_ENV,
)
from astrid.core.env_vars import (
    ASTRID_PROJECTS_ROOT as PROJECTS_ROOT_ENV,
)
from astrid.core.env_vars import (
    ASTRID_TASK_ITEM_ID as TASK_ITEM_ID_ENV,
)
from astrid.core.env_vars import (
    ASTRID_TASK_ITERATION as TASK_ITERATION_ENV,
)
from astrid.core.env_vars import (
    ASTRID_TASK_PROJECT as TASK_PROJECT_ENV,
)
from astrid.core.env_vars import (
    ASTRID_TASK_RUN_ID as TASK_RUN_ID_ENV,
)
from astrid.core.env_vars import (
    ASTRID_TASK_STEP_ID as TASK_STEP_ID_ENV,
)
from astrid.core.env_vars import (
    ASTRID_TIMELINE_SCHEMA_PYTHONPATH as TIMELINE_SCHEMA_PYTHONPATH_ENV,
)

_SAFE_BASE_ENV = frozenset(
    {
        "HOME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "LOGNAME",
        "PATH",
        "PWD",
        "SHELL",
        "TMPDIR",
        "USER",
        "USERNAME",
        "VIRTUAL_ENV",
        "PYENV_VERSION",
    }
)

_WINDOWS_BASE_ENV = frozenset(
    {
        "USERPROFILE",
        "HOMEDRIVE",
        "HOMEPATH",
        "APPDATA",
        "LOCALAPPDATA",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "PATHEXT",
        "COMSPEC",
    }
)

_ASTRID_PROPAGATED_ENV = frozenset(
    {
        ASTRID_HOME_ENV,
        ASTRID_ENV_FILE,
        "ASTRID_INSTALL_PROGRAM_ROOT",
        "ASTRID_HOST_EXECUTION_PROFILE",
        TIMELINE_SCHEMA_PYTHONPATH_ENV,
        ASTRID_VIBECOMFY_CHECKOUT,
        PROJECTS_ROOT_ENV,
        PROJECT_RUN_ENV,
        PROJECT_SLUG_ENV,
        ASTRID_AUTHOR_TEST,
        ASTRID_INTERNAL_INVOCATION,
        TASK_RUN_ID_ENV,
        TASK_PROJECT_ENV,
        TASK_STEP_ID_ENV,
        TASK_ITEM_ID_ENV,
        TASK_ITERATION_ENV,
        PACKS_PATH_ENV,
        ASTRID_NODE_EXECUTABLE,
        ASTRID_REMOTION_VALIDATION_PATH,
        ASTRID_REMOTION_VALIDATION_SHA256,
        ASTRID_REMOTION_VALIDATION_LAUNCH,
        ASTRID_REMOTION_VALIDATION_ROLE,


        ASTRID_REMOTION_PROJECT_DIR,
        "ASTRID_REMOTION_BROWSER_EXECUTABLE",
        "ASTRID_RENDER_INHERIT_PROCESS_GROUP",
        # RunPod executor manifests explicitly declare this host-owned path.
        # It must survive the supervised host -> executor -> SSH child
        # boundary; unlike a private key value, the path itself is not secret.
        "RUNPOD_SSH_IDENTITY_PATH",
        "RUNPOD_SSH_IDENTITY_PUBLIC_PATH",
    }
)

_SECRET_NAME_RE = re.compile(
    r"(^|_)(API[_-]?KEY|KEY|AUTH|CREDENTIAL|PASSWORD|SECRET|TOKEN)($|_)", re.IGNORECASE
)
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class SubprocessEnvPolicyError(ValueError):
    """Raised when a requested subprocess env shape violates policy."""


def build_child_subprocess_env(
    *,
    base: Mapping[str, str] | None = None,
    parent: Mapping[str, str] | None = None,
    explicit_env: Mapping[str, str] | None = None,
    passthrough: Iterable[str] = (),
    declared_passthrough: Iterable[str] = (),
    secret_values: Mapping[str, str] | None = None,
    declared_secrets: Iterable[str] = (),
) -> dict[str, str]:
    """Return the safe environment for an Astrid child process.

    ``base`` contributes only known-safe process variables unless a key is
    declared and requested through ``passthrough``. Astrid task/session/project
    invariants are copied from ``parent`` and take precedence over ``base``.
    """

    base_env = os.environ if base is None else base
    parent_env = os.environ if parent is None else parent
    requested = _normalize_names(passthrough, "passthrough")
    declared = _normalize_names(declared_passthrough, "declared_passthrough")
    undeclared = requested - declared
    if undeclared:
        names = ", ".join(sorted(undeclared))
        raise SubprocessEnvPolicyError(f"env passthrough requested without declaration: {names}")
    secret_names = _normalize_secret_names(declared_secrets, "declared_secrets")
    supplied_secrets = {str(key): str(value) for key, value in (secret_values or {}).items()}
    undeclared_secrets = set(supplied_secrets) - secret_names
    if undeclared_secrets:
        names = ", ".join(sorted(undeclared_secrets))
        raise SubprocessEnvPolicyError(f"secret env supplied without declaration: {names}")

    env: dict[str, str] = {}
    for key, value in base_env.items():
        if key in _SAFE_BASE_ENV or (sys.platform == "win32" and key in _WINDOWS_BASE_ENV) or key in requested:
            if not _is_secret_name(key):
                env[key] = str(value)

    for key, value in (explicit_env or {}).items():
        if key in {"ASTRID_HOST_EXECUTION_PROFILE", ASTRID_REMOTION_VALIDATION_PATH, ASTRID_REMOTION_VALIDATION_SHA256, ASTRID_REMOTION_VALIDATION_LAUNCH, ASTRID_REMOTION_VALIDATION_ROLE}:
            raise SubprocessEnvPolicyError("launch validation references are parent-owned")
        if _is_secret_name(str(key)) and str(key) not in secret_names:
            raise SubprocessEnvPolicyError(f"secret env {key!r} must be supplied through secret_values")
        env[str(key)] = str(value)

    # Values are copied only into the in-memory Popen environment; callers
    # must clear their mapping after process creation/cancellation.
    for key, value in supplied_secrets.items():
        env[key] = value

    if sys.platform == "darwin":
        # CoreFoundation can add this encoding selector before Python builds
        # os.environ. Pin the inherited owner value before a child is launched,
        # so the command-owner channel still compares the exact effective env.
        # No default or first-caller value is admitted, and this is not a
        # general loader/locale override exception.
        cf_key = "__CF_USER_TEXT_ENCODING"
        inherited = parent_env.get(cf_key)
        supplied = [mapping[cf_key] for mapping in (base_env, explicit_env or {}) if cf_key in mapping]
        if inherited is None:
            if supplied:
                raise SubprocessEnvPolicyError("Darwin CF encoding requires an inherited owner pin")
        else:
            if (not isinstance(inherited, str) or not inherited or "\0" in inherited
                    or any(value != inherited for value in supplied)):
                raise SubprocessEnvPolicyError("Darwin CF encoding conflicts with its inherited owner pin")
            env[cf_key] = inherited

    env.pop(ASTRID_ACTOR, None)
    for key in sorted(_ASTRID_PROPAGATED_ENV):
        parent_value = parent_env.get(key)
        if parent_value is not None:
            env[key] = str(parent_value)
    return env


def _normalize_names(values: Iterable[str], label: str) -> set[str]:
    names: set[str] = set()
    for value in values:
        if not isinstance(value, str) or not value:
            raise SubprocessEnvPolicyError(f"{label} entries must be non-empty strings")
        if not _ENV_NAME_RE.fullmatch(value):
            raise SubprocessEnvPolicyError(f"{label} entry {value!r} is not a valid environment variable name")
        if _is_secret_name(value):
            raise SubprocessEnvPolicyError(f"{label} entry {value!r} looks secret-like")
        names.add(value)
    return names


def _normalize_secret_names(values: Iterable[str], label: str) -> set[str]:
    names: set[str] = set()
    for value in values:
        if not isinstance(value, str) or not value or not _ENV_NAME_RE.fullmatch(value):
            raise SubprocessEnvPolicyError(f"{label} entries must be valid environment variable names")
        if not _is_secret_name(value):
            raise SubprocessEnvPolicyError(f"{label} entry {value!r} is not secret-like")
        names.add(value)
    return names


def _is_secret_name(name: str) -> bool:
    return bool(_SECRET_NAME_RE.search(name))


__all__ = [
    "ASTRID_ACTOR",
    "ASTRID_AUTHOR_TEST",
    "ASTRID_INTERNAL_INVOCATION",
    "ASTRID_NODE_EXECUTABLE",
    "ASTRID_REMOTION_PROJECT_DIR",
    "SubprocessEnvPolicyError",
    "TASK_ITEM_ID_ENV",
    "TASK_ITERATION_ENV",
    "TASK_PROJECT_ENV",
    "TASK_RUN_ID_ENV",
    "TASK_STEP_ID_ENV",
    "build_child_subprocess_env",
]
