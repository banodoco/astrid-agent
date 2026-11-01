"""Resolve Astrid's installation-owned neutral-runtime data root.

The runtime remains the authority for the database and object store.  This
module only chooses the support-root argument passed to ``astrid-runtime``;
it never opens or creates runtime state.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Mapping

DATA_ROOT_ENV = "ASTRID_RUNTIME_DATA_ROOT"
UPGRADE_ACTION = "run `astrid-upgrade` before launching Astrid"
CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "astrid-runtime.json"
STATE_HOME_ENV = "ASTRID_STATE_HOME"
XDG_STATE_HOME_ENV = "XDG_STATE_HOME"


def _safe_absolute(value: str | Path, *, base: Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base / path
    path = Path(os.path.abspath(path))
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if current.is_symlink():
            raise ValueError(f"Astrid runtime data root contains a symlink component: {path}")
    return path


def _persistent_setup_selection_path() -> Path:
    """Return the existing Astrid skills/setup selection document path."""

    override = os.environ.get(STATE_HOME_ENV, "").strip()
    if override:
        base = Path(override).expanduser()
    else:
        xdg = os.environ.get(XDG_STATE_HOME_ENV, "").strip()
        base = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "state"
    return base / "astrid" / "skills.json"


def _read_persisted_support_root() -> Path | None:
    """Read the support root already persisted by Astrid setup, if present.

    ``skills.json`` is an existing user-facing selection surface.  This helper
    only reads its ``setup_selection`` binding; it does not create or update
    that document and it never treats the selection as a workspace registry.
    """

    path = _persistent_setup_selection_path()
    if not path.is_file() or path.is_symlink():
        return None
    try:
        value: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Astrid persisted setup selection is invalid: {path}") from exc
    if not isinstance(value, Mapping):
        raise ValueError(f"Astrid persisted setup selection is invalid: {path}")
    selection = value.get("setup_selection")
    if selection is None:
        return None
    if not isinstance(selection, Mapping):
        raise ValueError(f"Astrid persisted setup selection is invalid: {path}")
    raw = selection.get("support_root")
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError(f"Astrid persisted setup selection has no support_root: {path}")
    if not Path(raw).expanduser().is_absolute():
        raise ValueError(f"Astrid persisted setup support_root must be absolute: {path}")
    return _safe_absolute(raw, base=path.parent)


def _read_install_config_root() -> Path | None:
    """Read the checked-in installation config without writing state."""

    if not CONFIG_PATH.is_file() or CONFIG_PATH.is_symlink():
        return None
    try:
        value: Any = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Astrid runtime data-root config is invalid: {CONFIG_PATH}") from exc
    if not isinstance(value, Mapping) or value.get("version") != 1:
        raise ValueError(f"Astrid runtime data-root config is invalid: {CONFIG_PATH}")
    raw = value.get("data_root", ".astrid-data")
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError(f"Astrid runtime data-root config has no data_root: {CONFIG_PATH}")
    return _safe_absolute(raw, base=CONFIG_PATH.parent.parent)


def _read_installed_support_root() -> Path | None:
    """Read the support root recorded by the Astrid login-service install."""

    from astrid.installer.core import read_json
    from astrid.installer.service import LABEL, install_selection

    # A linked selection must not look like an absent installation: falling
    # through could choose a blank root during repair or kept-data reinstall.
    selection = _safe_absolute(install_selection(Path.home(), sys.platform), base=Path.home())
    if not selection.is_file():
        return None
    try:
        value = read_json(selection)
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise ValueError(
            f"Astrid install record is invalid: {selection}; run the Astrid install line again"
        ) from exc
    if value.get("owner") != LABEL:
        return None
    raw = value.get("data_root")
    if not isinstance(raw, str) or not raw.strip() or not Path(raw).expanduser().is_absolute():
        raise ValueError(f"Astrid install record has no absolute data_root: {selection}")
    return _safe_absolute(raw, base=selection.parent)


def _selected_realm(root: Path) -> str | None:
    """Return a catalog's selected realm id, without opening runtime state."""

    catalog = root / "runtime" / "catalog.json"
    if not catalog.is_file() or catalog.is_symlink():
        return None
    try:
        value: Any = json.loads(catalog.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(value, Mapping):
        return None
    selected = value.get("selected_realm_id")
    return str(selected) if selected not in (None, "") else None


def _ambiguous_roots_error(
    left: Path,
    right: Path,
    *,
    left_realm: str | None,
    right_realm: str | None,
) -> ValueError:
    return ValueError(
        "Astrid runtime support-root selection is ambiguous: existing realms are "
        f"configured at {left} ({left_realm or 'unknown realm'}) and {right} "
        f"({right_realm or 'unknown realm'}). Choose one explicitly with "
        f"{DATA_ROOT_ENV} or the runtime CLI's --data-root; no workspace was "
        "created, moved, or selected automatically."
    )


def resolve_runtime_data_root() -> Path:
    """Return one stable support root selected by existing config surfaces.

    An explicit ``ASTRID_RUNTIME_DATA_ROOT`` override wins. Otherwise a
    recorded installation root wins, including a kept-data uninstall record.
    Next a persisted Astrid setup selection is reused when available, followed
    by the checked-in install config. A wheel without these surfaces uses
    ``~/.astrid-data``. Existing selections are read only; this function never
    creates a workspace or moves data.
    """

    configured = os.environ.get(DATA_ROOT_ENV, "").strip()
    if configured:
        if not Path(configured).expanduser().is_absolute():
            raise ValueError(f"{DATA_ROOT_ENV} must be an absolute path")
        return _safe_absolute(configured, base=Path(__file__).resolve().parents[2])

    installed = _read_installed_support_root()
    if installed is not None:
        return installed

    persisted = _read_persisted_support_root()
    configured_root = _read_install_config_root()
    if persisted is not None:
        if configured_root is not None and persisted != configured_root:
            persisted_realm = _selected_realm(persisted)
            configured_realm = _selected_realm(configured_root)
            if persisted_realm or configured_realm:
                raise _ambiguous_roots_error(
                    persisted,
                    configured_root,
                    left_realm=persisted_realm,
                    right_realm=configured_realm,
                )
        return persisted
    if configured_root is not None:
        return configured_root

    # A wheel does not ship the source checkout's top-level config. Keep its
    # fallback persistent and cwd-independent rather than silently falling
    # back to the neutral launcher's old external support tree.
    return _safe_absolute(Path.home() / ".astrid-data", base=Path.home())


def ensure_no_unmigrated_runtime(data_root: Path) -> None:
    """Refuse a blank default when an older selected realm still exists.

    This guard only reads the old neutral catalog. It prevents an upgrade from
    silently creating a second empty realm before an operator has reviewed a
    backup/restore relocation plan. Explicit ``ASTRID_RUNTIME_DATA_ROOT``
    overrides remain available for deliberate recovery or test composition.
    """

    if os.environ.get(DATA_ROOT_ENV, "").strip():
        return
    selected = _selected_realm(data_root)
    legacy_root = Path.home() / "Library" / "Application Support" / "Astrid Runtime"
    legacy_selected = _selected_realm(legacy_root)
    if data_root == legacy_root:
        return
    if data_root != legacy_root and selected and legacy_selected:
        raise _ambiguous_roots_error(
            data_root,
            legacy_root,
            left_realm=selected,
            right_realm=legacy_selected,
        )
    if selected:
        return
    legacy = legacy_root / "runtime" / "catalog.json"
    if not legacy.is_file() or legacy.is_symlink():
        return
    try:
        value = json.loads(legacy.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return
    if isinstance(value, Mapping) and value.get("selected_realm_id"):
        realm = value["selected_realm_id"]
        raise ValueError(
            f"an existing neutral runtime realm ({realm}) is configured at {legacy.parent}, "
            f"and this checkout's support root {data_root} has no workspace yet. Choose one: "
            f"(1) move it: {UPGRADE_ACTION}. That MOVES the existing realm from {legacy.parent} "
            f"into {data_root}, so the old location stops being used. "
            "(2) keep it where it is and start a separate workspace: set ASTRID_RUNTIME_DATA_ROOT "
            "to a new absolute folder (for example `export ASTRID_RUNTIME_DATA_ROOT=<new folder>`) "
            f"before running Astrid. That folder gets its own empty workspace and {legacy.parent} "
            "is not touched."
        )


__all__ = [
    "CONFIG_PATH",
    "DATA_ROOT_ENV",
    "UPGRADE_ACTION",
    "ensure_no_unmigrated_runtime",
    "resolve_runtime_data_root",
]
