"""Managed Runtime lifecycle composition for the selected Astrid installation.

The neutral Runtime may call this only with its already-validated source and
support roots. Returning None means neither root names a managed installation.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def managed_runtime_lifecycle(*, action: str, source_checkout: Path, data_root: Path,
                              wait: bool = False) -> dict | None:
    from runtime_protocol import platform
    from .app_update import locked
    from .backend import Backend
    from .core import absolute, current, pending_transaction, read_json
    from .installation import adopt_if_handed_off
    from .service import LABEL, install_selection, installer_lock_path

    if action not in {"up", "restart"}:
        raise ValueError("Unsupported managed Runtime lifecycle action.")
    source, data = absolute(source_checkout), absolute(data_root)
    home, system = Path.home(), sys.platform
    selection_path = install_selection(home, system)
    selection = read_json(selection_path)
    # Source-shaped installation paths are never treated as unmanaged just
    # because a current pointer or install record is broken.
    version_dir = next((part for part in (source, *source.parents) if part.parent.name == "versions"), None)
    inferred = version_dir.parent.parent if version_dir is not None else None
    selected_root = selection.get("program_root") if not selection.get("uninstalled") else None
    selected_data = selection.get("data_root") if selected_root else None
    candidate = absolute(selected_root) if isinstance(selected_root, str) and selected_data == str(data) else inferred
    if candidate is None:
        return None
    root = absolute(candidate)
    lock_path = installer_lock_path(home, system)
    with locked(lock_path, timeout=10):
        selection = read_json(selection_path)
        marker = read_json(root / "install.json")
        # Refuse incomplete, ambiguous or concurrently changed selection before
        # allowing C1 to stop the live owner.
        if (selection.get("uninstalled") or selection.get("owner") != LABEL
                or selection.get("program_root") != str(root)
                or selection.get("data_root") != str(data)
                or marker.get("owner") != LABEL or marker.get("data_root") != str(data)):
            raise ValueError("Managed Astrid selection is missing or inconsistent; no Runtime was stopped.")
        if marker.get("managed_by") == "pinokio" or selection.get("managed_by") == "pinokio":
            raise ValueError("Pinokio owns this Astrid installation; use its controls.")
        if marker.get("managed_by") not in (None, "") or selection.get("managed_by") not in (None, ""):
            raise ValueError("Unknown Astrid installation manager; no Runtime was stopped.")
        if pending_transaction(root):
            raise ValueError("An Astrid installation transaction needs recovery; no Runtime was stopped.")
        version = current(root)
        if (version is None or version.managed_by is not None
                or not version.python.is_file() or not version.profile.is_file()):
            raise ValueError("Managed Astrid version is incomplete or has changed; no Runtime was stopped.")
        profile = read_json(version.profile)
        if (profile.get("source_checkout") != str(source)
                or not source.is_relative_to(version.root)
                or not absolute(profile.get("runtime_environment", "")).is_relative_to(version.root)):
            raise ValueError("Managed Astrid source profile differs from its selected version; no Runtime was stopped.")
        catalog = read_json(data / "runtime" / "catalog.json")
        realm_id = catalog.get("selected_realm_id")
        realm = next((row for row in catalog.get("realms", []) if row.get("realm_id") == realm_id), None)
        if not isinstance(realm_id, str) or not realm_id or not isinstance(realm, dict) or not realm.get("data_root"):
            raise ValueError("Managed Astrid workspace selection is incomplete; no Runtime was stopped.")
        discovery = read_json(data / "runtime" / "discovery.json")
        old_id = discovery.get("runtime_instance_id")
        backend = Backend(root, platform.log_dir() / "installer.log", Path(sys.executable), say=lambda *_: None)
        backend.service.activation_lock_path = lock_path
        adopt_if_handed_off(backend, root)
        # Validate service settings and stop target before attempting shutdown.
        backend.service.settings()
        if (backend.service.system == "linux" and backend.service.autostart_enabled()
                and backend.service.login_service_exists() and not backend.service.systemd()):
            raise ValueError("The selected systemd manager is unavailable; no Runtime was stopped.")
        register = False
        if action == "restart":
            if not isinstance(old_id, str) or not old_id:
                raise ValueError("No selected Runtime owner to restart; no Runtime was stopped.")
            backend.preflight_stop(data)
            from astrid.services import prepare_pack_host_shutdown
            stop_host = prepare_pack_host_shutdown(support_root=data / "runtime", source_checkout=source,
                                                   runtime_instance_id=old_id)
            backend.stop(version, data, stop_manager=stop_host)
            register = True  # C1 may have unloaded the canonical service.
        elif discovery:
            pid, birth = discovery.get("pid"), discovery.get("process_birth_id")
            if type(pid) is not int or pid <= 0 or not isinstance(birth, str) or not birth:
                raise ValueError("Runtime discovery owner identity is incomplete; no Runtime was stopped.")
            observed = platform.process_identity(pid)
            if observed is not None and observed.get("birth_id") == birth:
                # Never replace a live owner as a side effect of `up`.
                backend.verify(version, data, realm_id)
                return {"ok": True, "status": "reconnected", "realm_id": realm_id,
                        "runtime_instance_id": old_id, "endpoint": discovery.get("endpoint"),
                        "door_endpoint": discovery.get("door_endpoint")}
            # C1 owns stale-publication cleanup and manager identity checks.
            backend.preflight_stop(data)
            backend.stop(version, data)
            register = True
        else:
            # A configured stopped service can be started without re-registering
            # it; validate its identity before using the existing OS manager.
            if backend.service.login_service_exists():
                existing = backend.service.validate_stop_target(version, data, owner_pid=None)
                if (backend.service.system == "darwin" and existing is None
                        and backend.service.autostart_enabled()):
                    register = True
            else:
                register = True
        if register:
            backend.register(version, data)
        backend.start(version, data)
        backend.verify(version, data, realm_id, pre_update_instance_id=old_id if action == "restart" else None)
        new = read_json(data / "runtime" / "discovery.json")
        if action == "restart" and (not new.get("runtime_instance_id") or new.get("runtime_instance_id") == old_id):
            raise ValueError("Managed Runtime restart did not establish a new owner.")
        return {"ok": True, "status": "restarted" if action == "restart" else "started",
                "realm_id": realm_id, "runtime_instance_id": new.get("runtime_instance_id"),
                "endpoint": new.get("endpoint"), "door_endpoint": new.get("door_endpoint")}
