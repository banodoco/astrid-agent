"""Read-only observations of an existing ComfyUI Desktop installation.

Registration and responder identity are separate facts. Never infer Desktop
identity from a listening port, or launch/modify an installation here.
"""

from __future__ import annotations

import json
import ntpath
import os
import posixpath
import re
import sys
from pathlib import Path

DESKTOP_CANDIDATE_PORTS = (8000, 8188)


def _path_module(*paths):
    return ntpath if any("\\" in path or ntpath.splitdrive(path)[0] for path in paths) else posixpath


def _normalized(path, module):
    if module is ntpath:
        path = path.replace("/", "\\")
    return module.normcase(module.normpath(path))


def path_equal(a, b):
    if not isinstance(a, str) or not a or not isinstance(b, str) or not b:
        return False
    module = _path_module(a, b)
    return _normalized(a, module) == _normalized(b, module)


def parent_dir(path):
    module = _path_module(path)
    return module.dirname(module.normpath(path))


def _join(path, *parts):
    return _path_module(path).join(path, *parts)


def _string(value):
    return value if isinstance(value, str) and value.strip() else None


def _permission_denied(exc):
    # urllib wraps the OS error in .reason; other transports use chaining.
    pending = [exc]
    seen = set()
    while pending:
        current = pending.pop()
        if not isinstance(current, BaseException) or id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, PermissionError):
            return True
        pending.extend((getattr(current, "reason", None), current.__cause__, current.__context__))
    return False


def _windows_registry():
    """Enumerate HKCU uninstall entries using only the native read API."""
    if sys.platform != "win32":
        return []
    import winreg

    def value(key, name):
        try:
            return winreg.QueryValueEx(key, name)[0]
        except FileNotFoundError:
            return None

    try:
        uninstall = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Uninstall",
            0, winreg.KEY_READ,
        )
    except FileNotFoundError:
        return []
    except PermissionError:
        return [{"_denied": True}]

    entries = []
    with uninstall:
        index = 0
        while True:
            try:
                name = winreg.EnumKey(uninstall, index)
            except PermissionError:
                entries.append({"_denied": True})
                break
            except OSError as exc:
                if getattr(exc, "winerror", None) == 259:  # ERROR_NO_MORE_ITEMS
                    break
                raise
            index += 1
            try:
                with winreg.OpenKey(uninstall, name, 0, winreg.KEY_READ) as key:
                    if value(key, "DisplayName") == "ComfyUI":
                        entries.append({
                            field: value(key, field)
                            for field in ("DisplayVersion", "InstallLocation", "DisplayIcon")
                        })
            except FileNotFoundError:
                # An uninstall entry may disappear during enumeration.
                continue
            except PermissionError:
                # Preserve the observed entries and report incomplete access,
                # rather than turning a partial enumeration into absence.
                entries.append({"_denied": True})
    return entries


def read_registrations(environ=None, *, registry=None, read_text=None):
    environ = os.environ if environ is None else environ
    registry = _windows_registry if registry is None else registry
    if read_text is None:
        read_text = lambda path: Path(path).read_text(encoding="utf-8")

    try:
        entries = registry()
    except PermissionError:
        entries = [{"_denied": True}]
    # Retain the original single-entry injection seam alongside enumeration.
    if isinstance(entries, dict):
        entries = [entries]
    entries = entries or []

    config = None
    config_seen = False
    config_denied = False
    appdata = _string(environ.get("APPDATA"))
    if appdata:
        try:
            raw = read_text(_join(appdata, "ComfyUI", "config.json"))
        except PermissionError:
            config_denied = True
        except (FileNotFoundError, NotADirectoryError):
            pass
        except ValueError:
            # A present but undecodable config is not complete absence.
            config_seen = True
        else:
            config_seen = True
            try:
                parsed = json.loads(raw)
            except ValueError:
                pass
            else:
                if isinstance(parsed, dict):
                    config = parsed

    if not entries and not config_seen and not config_denied:
        return []

    registrations = []
    for entry in entries or [{}]:
        icon = _string(entry.get("DisplayIcon"))
        executable = None
        if icon:
            executable = _string(re.sub(r",\s*0$", "", icon.strip()).strip().strip('"'))
        install_location = _string(entry.get("InstallLocation"))
        localappdata = _string(environ.get("LOCALAPPDATA"))
        if executable is None:
            if install_location:
                executable = _join(install_location, "ComfyUI.exe")
            elif localappdata:
                executable = _join(localappdata, "Programs", "ComfyUI", "ComfyUI.exe")

        registration = {
            "source": "registry+config" if entries and config_seen else
                      "registry" if entries else "config",
            "version": _string(entry.get("DisplayVersion")),
            "executable": executable,
            "data_dir": _string((config or {}).get("basePath")),
            "server_port": 8000,
        }
        # An inaccessible source is not evidence of absent files.
        # This internal flag is never part of the public installation schema.
        if entry.get("_denied") or config_denied:
            registration["_denied"] = True
        registrations.append(registration)
    return registrations


def _folder_base(folders):
    if not isinstance(folders, dict):
        return None
    custom_nodes = folders.get("custom_nodes")
    if isinstance(custom_nodes, str):
        custom_nodes = [custom_nodes]
    if not isinstance(custom_nodes, list):
        return None
    path = next((item for item in custom_nodes if _string(item)), None)
    return (parent_dir(path) or None) if path else None


def _default_get_json(port, path):
    from astrid.packs.vibecomfy.local import _get_json

    return _get_json(f"http://127.0.0.1:{port}{path}", timeout=1.0)


def probe_ports(ports, *, get_json=None):
    get_json = _default_get_json if get_json is None else get_json
    responders = {}
    for port in dict.fromkeys(ports):
        responder = {"kind": "none", "base_dir": None, "version": None}
        responders[port] = responder
        try:
            stats = get_json(port, "/system_stats")
        except Exception as exc:
            if _permission_denied(exc):
                responder["kind"] = "denied"
            elif isinstance(getattr(exc, "code", None), int) or isinstance(exc, ValueError):
                responder["kind"] = "other"
            continue
        if not (
            isinstance(stats, dict)
            and isinstance(stats.get("system"), dict)
            and isinstance(stats.get("devices"), list)
        ):
            responder["kind"] = "other"
            continue
        responder["kind"] = "comfyui"
        responder["version"] = _string(stats["system"].get("comfyui_version"))
        try:
            responder["base_dir"] = _folder_base(get_json(port, "/internal/folder_paths"))
        except Exception as exc:
            if _permission_denied(exc):
                responder["kind"] = "denied"
            # Healthy ComfyUI without folder evidence remains unidentified.
    return responders


def classify(registration, *, files, responders):
    expected = registration["server_port"]
    data_dir = registration.get("data_dir")

    def observation(state, port=None, responder=None, matches=None):
        responder = responder or {}
        return {
            "layout": "desktop",
            "state": state,
            "version": registration.get("version"),
            "endpoint": f"http://127.0.0.1:{port if state == 'running' else expected}",
            "recorded": {"executable": registration.get("executable"), "data_dir": data_dir},
            "files": dict(files),
            "responder": {
                "kind": responder.get("kind", "none"),
                "port": port,
                "base_dir": responder.get("base_dir"),
                "matches_install": matches,
            },
        }

    if registration.get("_denied"):
        return observation("denied")
    # The public files booleans mean positively observed presence. An unknown
    # path is distinguished by recorded.<field> == None, not by stale.
    if not _string(registration.get("executable")) or not _string(data_dir):
        return observation("ambiguous")
    if not files["executable"] or not files["data_dir"]:
        return observation("stale")

    # Multiple ports reporting the same data root are one installation;
    # prefer the expected port, then the stable candidate order.
    ordered = dict.fromkeys((expected, *responders))
    for port in ordered:
        responder = responders.get(port, {})
        if responder.get("kind") == "comfyui" and path_equal(data_dir, responder.get("base_dir")):
            return observation("running", port, responder, True)

    responder = responders.get(expected, {})
    kind = responder.get("kind")
    if kind == "denied":
        return observation("denied", expected, responder)
    if kind == "other":
        return observation("wrong_responder", expected, responder)
    if kind == "comfyui":
        if not responder.get("base_dir"):
            return observation("ambiguous", expected, responder)
        return observation("wrong_responder", expected, responder, False)
    for port in ordered:
        responder = responders.get(port, {})
        if responder.get("kind") == "denied":
            return observation("denied", port, responder)
    return observation("closed")


def _exists(path):
    # os.path.exists can convert access denial into False. Only absence
    # counts as missing; a permission failure must reach the classifier.
    try:
        os.stat(path)
    except (FileNotFoundError, NotADirectoryError):
        return False
    return True


def _path_key(path):
    return _normalized(path, _path_module(path)) if path else None


def observe_installations(
    *, environ=None, registrations=None, get_json=None, exists=None,
    read_text=None, registry=None, ports=(),
):
    if registrations is None:
        registrations = read_registrations(environ, registry=registry, read_text=read_text)
    exists = _exists if exists is None else exists
    checked = []
    seen = set()
    candidates = {}
    for raw in registrations:
        registration = dict(raw)
        key = (
            _path_key(registration.get("executable")),
            _path_key(registration.get("data_dir")),
            bool(registration.get("_denied")),
        )
        # Deduplicate only when both paths identify the same registration.
        # Two incomplete entries are not proven duplicates.
        if key[0] is not None and key[1] is not None:
            if key in seen:
                continue
            seen.add(key)
        files = {}
        for field in ("executable", "data_dir"):
            path = registration.get(field)
            try:
                files[field] = bool(path and exists(path))
            except PermissionError:
                files[field] = False
                registration["_denied"] = True
        if not registration.get("_denied") and all(files.values()):
            candidates.setdefault(registration["server_port"], None)
        checked.append((registration, files))

    # Share one complete observation set across registrations. Missing or
    # inaccessible registrations cannot trigger Desktop network requests.
    if candidates:
        candidates.update(dict.fromkeys((*DESKTOP_CANDIDATE_PORTS, *ports)))
    responders = probe_ports(candidates, get_json=get_json) if candidates else {}
    observations = [
        classify(registration, files=files, responders=responders)
        for registration, files in checked
    ]

    # If distinct registered executables share the same observed data root,
    # folder evidence alone cannot distinguish which executable owns it.
    running = [item for item in observations if item["state"] == "running"]
    for item, (registration, _) in zip(observations, checked):
        if item["state"] != "running":
            continue
        recorded = item["recorded"]
        if any(
            other is not item
            and path_equal(recorded["data_dir"], other["recorded"]["data_dir"])
            and not path_equal(recorded["executable"], other["recorded"]["executable"])
            for other in running
        ):
            item["state"] = "ambiguous"
            item["endpoint"] = f"http://127.0.0.1:{registration['server_port']}"
            item["responder"]["matches_install"] = None
    return observations


def select_candidate(installations, servers):
    choices = []
    endpoints = set()
    running_roots = []
    not_running = []
    for installation in installations:
        if installation["state"] != "running":
            not_running.append(installation)
            continue
        endpoint = installation["endpoint"]
        root = installation["recorded"]["data_dir"]
        if endpoint in endpoints or any(path_equal(root, other) for other in running_roots):
            continue
        endpoints.add(endpoint)
        running_roots.append(root)
        choices.append({"layout": "desktop", "endpoint": endpoint, "label": "ComfyUI Desktop"})
    for server in servers:
        url = server["url"]
        # The scan's folder evidence, not a guessed interpreter or the old
        # registered port, establishes that this is the same installation.
        root = _folder_base(server.get("folder_paths"))
        if url in endpoints or any(path_equal(root, other) for other in running_roots):
            continue
        endpoints.add(url)
        label = f"ComfyUI at {url}"
        if server.get("pinokio_apps"):
            label += " (Pinokio)"
        choices.append({"layout": "other", "endpoint": url, "label": label})
    return {
        "outcome": "none" if not choices else "one" if len(choices) == 1 else "several",
        "choices": choices,
        "not_running": not_running,
    }
