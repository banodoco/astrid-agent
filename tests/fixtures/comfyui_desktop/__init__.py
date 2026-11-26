"""Redacted, assumed ComfyUI Desktop discovery states for narrow tests."""

import json
from copy import deepcopy
from pathlib import Path, PureWindowsPath
from urllib.error import HTTPError, URLError

FIXTURE_NAMES = ("running", "closed", "stale", "wrong_responder")


def load(name):
    """Read a fresh independent fixture; never cache mutable payloads."""
    if name not in FIXTURE_NAMES:
        raise ValueError(f"Unknown Desktop fixture: {name!r}")
    return json.loads(Path(__file__).with_name(f"{name}.json").read_text(encoding="utf-8"))


def get_json_for(fixture, *, denied_ports=()):
    """Return an HTTP-shaped fake without making a network request."""
    denied = frozenset(denied_ports)

    def get_json(port: int, path: str):
        if port in denied:
            raise URLError(PermissionError(1, "Operation not permitted"))
        responder = fixture["responders"].get(str(port), {"kind": "none"})
        kind = responder["kind"]
        if kind == "none":
            raise URLError(ConnectionRefusedError(111, "refused"))
        if path == "/system_stats":
            return deepcopy(responder["system_stats"])
        if kind == "comfyui" and path == "/internal/folder_paths":
            return deepcopy(responder["folder_paths"])
        raise HTTPError(f"http://127.0.0.1:{port}{path}", 404, "Not Found", {}, None)

    return get_json


def facts(fixture):
    """Describe the distinguishing facts independently of the host OS."""
    registration = fixture["registration"]
    files = fixture["files"]
    responder = fixture["responders"].get(str(registration["server_port"]), {"kind": "none"})
    kind = responder["kind"]
    matches = None
    if kind == "comfyui":
        custom_nodes = PureWindowsPath(responder["folder_paths"]["custom_nodes"][0])
        matches = custom_nodes.parent == PureWindowsPath(registration["data_dir"])
    return {
        "files": (files["executable"], files["data_dir"]),
        "port_kind": kind,
        "base_dir_matches": matches,
    }
