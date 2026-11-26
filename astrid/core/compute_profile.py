"""Secret-safe, user-scoped compute profile resolution.

Compute profiles are Runtime-owned documents, with legacy JSON files in
``~/.astrid`` imported once or used when the Runtime is unavailable. They
describe where an executor should run (GPU, image, storage, and paths), but
never contain credentials.  Credential fields contain environment-variable
*names* only; the executor resolves those names against its process
environment at execution time.

Resolution precedence is fixed and intentionally boring::

    explicit field override > env-selected profile > named/default profile
    > executor defaults

The result is JSON-safe and suitable for a run's ``compute_resolved.json``
snapshot.  It does not include values read from credential environment
variables.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from astrid.core import user_state, user_state_import

RUNTIME_KEY = "agent.compute-profiles"
_RUNTIME_SCHEMA = "astrid.compute-profiles.v1"
_AUTO = object()

SCHEMA_ID = "astrid.compute_profile.v1"
PROFILE_ENV_VAR = "ASTRID_COMPUTE_PROFILE"
PROFILE_DIRNAME = "compute-profiles"
DEFAULT_PROFILE_ID = "default"

# Fields understood by the RunPod adapter today.  Profiles may also carry a
# description and provider marker for future adapters, but unknown fields are
# rejected so a typo cannot silently select an executor setting.
PROFILE_FIELDS = frozenset(
    {
        "schema",
        "schema_version",
        "id",
        "description",
        "provider",
        "credentials",
        "gpu_type",
        "allowed_cuda_versions",
        "storage_name",
        "max_runtime_seconds",
        "name_prefix",
        "image",
        "container_disk_gb",
        "volume_in_gb",
        "volume_mount_path",
        "datacenter_id",
        "ports",
        "local_root",
        "remote_root",
        "remote_script",
        "timeout",
        "upload_mode",
        "excludes",
        "require_storage",
    }
)
_ENV_REF_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_SECRET_FIELD_RE = re.compile(r"(?:^|_)(?:api[_-]?key|token|secret|password)(?:$|_)", re.I)
_PROFILE_SELECTOR_KEYS = ("profile_id", "compute_profile", "profile")


def profile_dir(home: Path | str | None = None) -> Path:
    """Return the user-local profile directory without creating it."""

    base = Path.home() if home is None else Path(home).expanduser()
    return base / ".astrid" / PROFILE_DIRNAME


def profile_path(profile_id: str, home: Path | str | None = None) -> Path:
    """Return the path for a named profile, rejecting path traversal."""

    if not isinstance(profile_id, str) or not profile_id.strip():
        raise ValueError("compute profile id must be a non-empty string")
    clean = profile_id.strip()
    if clean in {".", ".."} or Path(clean).name != clean or "/" in clean or "\\" in clean:
        raise ValueError(f"invalid compute profile id {profile_id!r}")
    return profile_dir(home) / f"{clean}.json"


def _validate_env_ref(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not _ENV_REF_RE.fullmatch(value.strip()):
        raise ValueError(f"{field} must be an environment variable name, never a credential value")
    return value.strip()


def validate_profile(document: Mapping[str, Any], *, expected_id: str | None = None) -> dict[str, Any]:
    """Validate and copy one profile document.

    Validation is intentionally stdlib-only so profile loading remains usable
    by the lightweight executor entrypoints.  In particular, credential
    values are checked to be env-var references and are never returned from
    the process environment.
    """

    if not isinstance(document, Mapping):
        raise ValueError("compute profile must be a JSON object")
    data = dict(document)
    if data.get("schema") != SCHEMA_ID:
        raise ValueError(f"compute profile schema must be {SCHEMA_ID!r}")
    if data.get("schema_version") != 1:
        raise ValueError("compute profile schema_version must be 1")
    profile_id = data.get("id")
    if not isinstance(profile_id, str) or not profile_id.strip():
        raise ValueError("compute profile id must be a non-empty string")
    if expected_id is not None and profile_id != expected_id:
        raise ValueError(f"compute profile id {profile_id!r} does not match requested {expected_id!r}")
    unknown = sorted(set(data) - PROFILE_FIELDS)
    if unknown:
        raise ValueError(f"unknown compute profile fields: {', '.join(unknown)}")

    if "allowed_cuda_versions" in data:
        cuda_versions = data["allowed_cuda_versions"]
        if isinstance(cuda_versions, str):
            cuda_versions = [part.strip() for part in cuda_versions.split(",") if part.strip()]
        if not isinstance(cuda_versions, (list, tuple)) or not cuda_versions:
            raise ValueError("allowed_cuda_versions must contain at least one version")
        if any(not isinstance(version, str) or not re.fullmatch(r"\d+\.\d+", version.strip()) for version in cuda_versions):
            raise ValueError("allowed_cuda_versions entries must be non-empty CUDA versions like '12.4'")
        data["allowed_cuda_versions"] = list(dict.fromkeys(version.strip() for version in cuda_versions))

    credentials = data.get("credentials", {})
    if not isinstance(credentials, Mapping):
        raise ValueError("compute profile credentials must be an object of env-var references")
    data["credentials"] = {
        str(name): _validate_env_ref(value, field=f"credentials.{name}")
        for name, value in credentials.items()
    }

    # Catch accidental literal secrets in conventional fields while allowing
    # ordinary executor values such as image names and paths.
    for key, value in data.items():
        if _SECRET_FIELD_RE.search(str(key)) and key != "credentials":
            raise ValueError(f"secret-bearing field {key!r} is not allowed; use credentials env references")
        if isinstance(value, str) and (value.startswith("rpa_") or value.startswith("sk-")):
            raise ValueError("literal credential values are not allowed in compute profiles")
    return data


def _read_legacy_profiles() -> dict[str, Any] | None:
    profiles = {}
    for path in sorted(profile_dir().glob("*.json")):
        try:
            profile = validate_profile(
                json.loads(path.read_text(encoding="utf-8")), expected_id=path.stem
            )
            profile_path(path.stem)
        except (OSError, ValueError):
            continue
        profiles[path.stem] = profile
    return profiles or None


def _runtime_document(profiles: dict) -> dict:
    return {"schema": _RUNTIME_SCHEMA, "profiles": profiles}


def _profiles(document: dict | None) -> dict:
    if document is None:
        return {}
    if document.get("schema") != _RUNTIME_SCHEMA or not isinstance(document.get("profiles"), dict):
        raise ValueError("invalid Runtime compute-profiles document")
    return document["profiles"]


def _runtime_profiles(store: Any) -> dict:
    snapshot = user_state.read(RUNTIME_KEY, store=store)
    if snapshot.content is None:
        user_state_import.run_import(
            user_state_import.ImportSpec(
                import_id="compute-profiles",
                target_key=RUNTIME_KEY,
                source_label="~/.astrid/compute-profiles/*.json",
                read_source=_read_legacy_profiles,
                to_document=_runtime_document,
            ),
            store=store,
        )
        snapshot = user_state.read(RUNTIME_KEY, store=store)
    return _profiles(snapshot.content)


def _selected_store(store: Any, home: Path | str | None = None) -> Any:
    if home is not None:
        return None
    if store is _AUTO:
        try:
            return user_state.current_store()
        except OSError:
            return None
    return store


def load_profile(
    profile_id: str, *, home: Path | str | None = None, store: Any = _AUTO
) -> tuple[dict[str, Any], Path]:
    """Load and validate a named profile, preferring the Runtime when reachable."""

    path = profile_path(profile_id, home)
    runtime = _selected_store(store, home)
    if runtime is not None:
        try:
            profiles = _runtime_profiles(runtime)
        except user_state.UserStateError as exc:
            if exc.code != "unavailable":
                raise
        else:
            location = Path(f"runtime:{RUNTIME_KEY}/{profile_id}")
            if profile_id not in profiles:
                raise FileNotFoundError(f"compute profile {profile_id!r} not found at {location}")
            return validate_profile(profiles[profile_id], expected_id=profile_id), location

    return _load_file(profile_id, path)


def _load_file(profile_id: str, path: Path) -> tuple[dict[str, Any], Path]:
    if not path.is_file():
        raise FileNotFoundError(f"compute profile {profile_id!r} not found at {path}")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON in compute profile {profile_id!r}: {exc.msg}") from exc
    return validate_profile(document, expected_id=profile_id), path


def save_profile(document: Mapping[str, Any], *, store: Any = _AUTO) -> None:
    """Validate and save a profile without ever writing credentials or legacy files."""

    profile = validate_profile(document)
    profile_id = profile["id"]
    profile_path(profile_id)
    runtime = _selected_store(store)
    unavailable = "the Runtime is not reachable; the compute profile was not saved"
    if runtime is None:
        raise RuntimeError(unavailable)

    def change(content: dict) -> None:
        profiles = _profiles(content or None)
        profiles[profile_id] = profile
        content.update(_runtime_document(profiles))

    try:
        _runtime_profiles(runtime)
        user_state.update(RUNTIME_KEY, change, store=runtime)
    except user_state.UserStateError as exc:
        if exc.code == "unavailable":
            raise RuntimeError(unavailable) from exc
        if exc.code == "too_large" and exc.key == RUNTIME_KEY:
            raise ValueError(
                f"compute profile {profile_id!r} would exceed the 16 KiB "
                f"({user_state.APP_STATE_MAX_BYTES} byte) limit"
            ) from exc
        raise


def remove_profile(profile_id: str, *, store: Any = _AUTO) -> bool:
    """Remove a Runtime profile, retaining the legacy file for rollback."""

    profile_path(profile_id)
    runtime = _selected_store(store)
    unavailable = "the Runtime is not reachable; the compute profile was not removed"
    if runtime is None:
        raise RuntimeError(unavailable)

    removed = False

    def change(content: dict) -> None:
        nonlocal removed
        profiles = _profiles(content or None)
        removed = profile_id in profiles
        if removed:
            del profiles[profile_id]
            content.update(_runtime_document(profiles))

    try:
        _runtime_profiles(runtime)
        user_state.update(RUNTIME_KEY, change, store=runtime)
    except user_state.UserStateError as exc:
        if exc.code == "unavailable":
            raise RuntimeError(unavailable) from exc
        raise
    return removed


def list_profile_ids(*, store: Any = _AUTO) -> list[str]:
    """List profile ids from the Runtime or, if unavailable, valid legacy files."""

    runtime = _selected_store(store)
    if runtime is not None:
        try:
            return sorted(_runtime_profiles(runtime))
        except user_state.UserStateError as exc:
            if exc.code != "unavailable":
                raise
    return sorted(_read_legacy_profiles() or {})


def _deep_merge(base: Mapping[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in overlay.items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), Mapping):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def resolve_compute_profile(
    *,
    explicit: Mapping[str, Any] | None = None,
    env: Mapping[str, str] | None = None,
    profile_id: str | None = None,
    executor_defaults: Mapping[str, Any] | None = None,
    home: Path | str | None = None,
    store: Any = _AUTO,
) -> dict[str, Any]:
    """Resolve a profile into a safe, JSON-serialisable mapping.

    ``profile_id`` is the explicit named profile requested by the caller and
    therefore beats ``ASTRID_COMPUTE_PROFILE``.  The environment variable is
    the next profile-selection tier.  A missing ``default.json`` is normal and
    simply leaves executor defaults in place; an explicitly selected profile
    must exist and validate.
    """

    environment = os.environ if env is None else env
    explicit_mapping = explicit or {}
    explicit_selector = next(
        (str(explicit_mapping[key]).strip() for key in _PROFILE_SELECTOR_KEYS if explicit_mapping.get(key)),
        None,
    )
    explicit_values = {
        key: value
        for key, value in explicit_mapping.items()
        if key not in _PROFILE_SELECTOR_KEYS and value is not None
    }
    defaults = dict(executor_defaults or {})

    explicit_profile_id = str(profile_id or explicit_selector or "").strip() or None
    env_profile_id = str(environment.get(PROFILE_ENV_VAR, "")).strip() or None
    selected_id = explicit_profile_id or env_profile_id
    source = "executor-defaults"
    selected_path: Path | None = None
    selected: Mapping[str, Any] = {}
    runtime = _selected_store(store, home)
    if selected_id is not None:
        selected, selected_path = load_profile(selected_id, home=home, store=runtime)
        source = "explicit-profile" if explicit_profile_id else "env-selected-profile"
    else:
        named_id = str(profile_id or "").strip() or DEFAULT_PROFILE_ID
        if runtime is None:
            default_path = profile_path(named_id, home)
            if default_path.is_file():
                selected, selected_path = load_profile(named_id, home=home, store=None)
        else:
            try:
                selected, selected_path = load_profile(named_id, home=home, store=runtime)
            except FileNotFoundError:
                pass
        if selected_path is not None:
            source = "named-profile" if named_id != DEFAULT_PROFILE_ID else "default-profile"

    resolved = _deep_merge(defaults, {k: v for k, v in selected.items() if k not in {"schema", "schema_version", "id", "description", "provider"}})
    resolved = _deep_merge(resolved, explicit_values)
    resolved["schema"] = SCHEMA_ID
    resolved["schema_version"] = 1
    resolved["profile_id"] = selected.get("id") if selected else None
    resolved["profile_source"] = source
    resolved["profile_path"] = str(selected_path) if selected_path else None
    # Re-check merged credential references, including executor defaults.
    credentials = resolved.get("credentials", {})
    if not isinstance(credentials, Mapping):
        raise ValueError("resolved compute profile credentials must be an object")
    resolved["credentials"] = {
        str(name): _validate_env_ref(value, field=f"credentials.{name}")
        for name, value in credentials.items()
    }
    return resolved


def credential_env_ref(profile: Mapping[str, Any], name: str, default: str | None = None) -> str | None:
    """Get a credential environment-variable name from a resolved profile."""

    credentials = profile.get("credentials", {})
    if not isinstance(credentials, Mapping):
        raise ValueError("resolved compute profile credentials must be an object")
    value = credentials.get(name, default)
    return _validate_env_ref(value, field=f"credentials.{name}") if value is not None else None


def credential_value(
    profile: Mapping[str, Any], name: str, *, env: Mapping[str, str] | None = None, default: str | None = None
) -> str | None:
    """Resolve a credential reference at runtime, without mutating the profile."""

    reference = credential_env_ref(profile, name, default)
    if reference is None:
        return None
    return (os.environ if env is None else env).get(reference)


def write_resolved_snapshot(produces_dir: Path | str, resolved: Mapping[str, Any]) -> Path:
    """Write the safe resolved profile next to executor-produced artifacts."""

    path = Path(produces_dir) / "compute_resolved.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    # This function receives only resolver output, which contains references,
    # but validate the credentials once more at the persistence boundary.
    safe = dict(resolved)
    credentials = safe.get("credentials", {})
    if not isinstance(credentials, Mapping):
        raise ValueError("resolved compute profile credentials must be an object")
    safe["credentials"] = {
        str(name): _validate_env_ref(value, field=f"credentials.{name}")
        for name, value in credentials.items()
    }
    path.write_text(json.dumps(safe, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


__all__ = [
    "SCHEMA_ID",
    "RUNTIME_KEY",
    "save_profile",
    "remove_profile",
    "list_profile_ids",
    "PROFILE_ENV_VAR",
    "PROFILE_DIRNAME",
    "DEFAULT_PROFILE_ID",
    "profile_dir",
    "profile_path",
    "validate_profile",
    "load_profile",
    "resolve_compute_profile",
    "credential_env_ref",
    "credential_value",
    "write_resolved_snapshot",
]
