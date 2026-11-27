"""Integration of read-only Desktop observations with the existing scan."""

from contextlib import contextmanager
from urllib.error import URLError

import pytest

from astrid.core.generation import desktop_install as desktop
from astrid.packs.vibecomfy import local
from tests.packs.test_vibecomfy_local import _non_comfy_server, _server

_read_registrations = desktop.read_registrations


@pytest.fixture(autouse=True)
def _no_uncontrolled_discovery(monkeypatch):
    monkeypatch.setattr(local, "_PINOKIO_INFO_URL", "http://127.0.0.1:1/pinokio/info")
    monkeypatch.setattr(desktop, "read_registrations", lambda *args, **kwargs: [])


@contextmanager
def _listening(factory, *args):
    server, thread = factory(*args)
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _registered(monkeypatch, root, port, *, present=True, candidates=None):
    root.mkdir(parents=True, exist_ok=True)
    executable = root / "ComfyUI.exe"
    if present:
        executable.touch()
    registration = {
        "source": "registry+config",
        "version": "0.7.2",
        "executable": str(executable),
        "data_dir": str(root),
        "server_port": port,
    }
    monkeypatch.setattr(desktop, "read_registrations", lambda *args, **kwargs: [registration])
    monkeypatch.setattr(
        desktop, "DESKTOP_CANDIDATE_PORTS", (port,) if candidates is None else candidates
    )
    return registration


def test_running_desktop_is_also_a_scan_server(tmp_path, monkeypatch):
    with _listening(_server, tmp_path) as port:
        registration = _registered(monkeypatch, tmp_path, port)
        result = local.scan([port])
    endpoint = f"http://127.0.0.1:{port}"
    assert result["errors"] == []
    assert [server["url"] for server in result["servers"]] == [endpoint]
    assert result["installations"] == [{
        "layout": "desktop",
        "state": "running",
        "version": "0.7.2",
        "endpoint": endpoint,
        "recorded": {"executable": registration["executable"], "data_dir": str(tmp_path)},
        "files": {"executable": True, "data_dir": True},
        "responder": {"kind": "comfyui", "port": port, "base_dir": str(tmp_path),
                      "matches_install": True},
    }]
    assert desktop.select_candidate(result["installations"], result["servers"])["outcome"] == "one"


def test_closed_registration_is_not_a_server(tmp_path, monkeypatch):
    _registered(monkeypatch, tmp_path, 1)
    result = local.scan([1])
    assert result["servers"] == []
    assert result["errors"] == []
    assert result["installations"][0]["state"] == "closed"
    assert result["installations"][0]["endpoint"] == "http://127.0.0.1:1"


def test_stale_does_not_probe_desktop_ports(tmp_path, monkeypatch):
    _registered(monkeypatch, tmp_path, 8000, present=False)
    original = local._get_json

    def get_json(url, timeout=2):
        assert url == local._PINOKIO_INFO_URL, "no Desktop HTTP for missing files"
        return original(url, timeout=timeout)

    monkeypatch.setattr(local, "_get_json", get_json)
    result = local.scan([])
    assert result["servers"] == []
    assert result["errors"] == []
    assert result["installations"][0]["state"] == "stale"
    assert result["installations"][0]["files"] == {"executable": False, "data_dir": True}


def test_non_comfy_at_desktop_port_is_rejected_and_reported(tmp_path, monkeypatch):
    with _listening(_non_comfy_server) as port:
        _registered(monkeypatch, tmp_path, port)
        result = local.scan([port])
    assert result["servers"] == []
    assert result["errors"] == [
        {"port": port, "endpoint": "/system_stats", "error": "responder is not ComfyUI"}
    ]
    assert result["installations"][0]["state"] == "wrong_responder"
    assert result["installations"][0]["responder"]["kind"] == "other"


def test_other_comfy_at_desktop_port_is_not_this_install(tmp_path, monkeypatch):
    with _listening(_server, tmp_path / "other") as port:
        _registered(monkeypatch, tmp_path / "desktop", port)
        result = local.scan([port])
    assert result["errors"] == []
    assert len(result["servers"]) == 1  # Healthy, but not the recorded Desktop.
    assert result["installations"][0]["state"] == "wrong_responder"
    assert result["installations"][0]["responder"]["matches_install"] is False
    assert desktop.select_candidate(result["installations"], result["servers"])["choices"] == [{
        "layout": "other", "endpoint": result["servers"][0]["url"],
        "label": "ComfyUI at " + result["servers"][0]["url"],
    }]


def test_changed_port_in_ordinary_scan_is_bound_by_folder(tmp_path, monkeypatch):
    with _listening(_server, tmp_path) as port:
        _registered(monkeypatch, tmp_path, 1, candidates=(1,))
        result = local.scan([port])
    endpoint = f"http://127.0.0.1:{port}"
    assert result["errors"] == []
    assert result["installations"][0]["state"] == "running"
    assert result["installations"][0]["endpoint"] == endpoint
    assert desktop.select_candidate(result["installations"], result["servers"])["choices"] == [
        {"layout": "desktop", "endpoint": endpoint, "label": "ComfyUI Desktop"}
    ]


def test_plain_non_comfy_on_ordinary_port_is_error_once():
    with _listening(_non_comfy_server) as port:
        result = local.scan([port, port])
    assert result["installations"] == []
    assert result["servers"] == []
    assert result["errors"] == [
        {"port": port, "endpoint": "/system_stats", "error": "responder is not ComfyUI"}
    ]


def test_missing_devices_is_not_comfy(monkeypatch):
    def get_json(url, timeout=2):
        if url == local._PINOKIO_INFO_URL:
            raise URLError(ConnectionRefusedError("refused"))
        assert url == "http://127.0.0.1:8188/system_stats"
        return {"system": {}}

    monkeypatch.setattr(local, "_get_json", get_json)
    assert local.scan([8188]) == {
        "servers": [],
        "errors": [{"port": 8188, "endpoint": "/system_stats", "error": "responder is not ComfyUI"}],
        "pinokio": {"available": False, "home": None},
        "installations": [],
    }


def test_no_registration_leaves_ordinary_and_pinokio_meanings(tmp_path):
    with _listening(_server, tmp_path) as port:
        result = local.scan([port])
    assert result["errors"] == []
    assert result["installations"] == []
    assert len(result["servers"]) == 1
    assert result["pinokio"] == {"available": False, "home": None}


def test_permission_failure_is_not_suppressed_by_scan(tmp_path, monkeypatch):
    _registered(monkeypatch, tmp_path, 8000)

    def get_json(url, timeout=2):
        if url == local._PINOKIO_INFO_URL:
            raise URLError(ConnectionRefusedError("refused"))
        assert url == "http://127.0.0.1:8000/system_stats"
        raise URLError(PermissionError("access denied"))

    monkeypatch.setattr(local, "_get_json", get_json)
    result = local.scan([8000])
    assert result["servers"] == []
    assert len(result["errors"]) == 1
    assert result["errors"][0]["port"] == 8000
    assert result["errors"][0]["endpoint"] == "/system_stats"
    assert "access denied" in result["errors"][0]["error"]
    assert result["installations"][0]["state"] == "denied"


def test_healthy_server_without_folder_evidence_is_ambiguous(tmp_path, monkeypatch):
    _registered(monkeypatch, tmp_path, 8000)

    def get_json(url, timeout=2):
        if url == local._PINOKIO_INFO_URL:
            raise URLError(ConnectionRefusedError("refused"))
        if url.endswith("/system_stats"):
            return {"system": {}, "devices": []}
        if url.endswith("/internal/folder_paths"):
            return {}
        if "/userdata?" in url:
            return []
        pytest.fail(f"unexpected request: {url}")

    monkeypatch.setattr(local, "_get_json", get_json)
    result = local.scan([8000])
    assert result["errors"] == []
    assert len(result["servers"]) == 1
    assert result["installations"][0]["state"] == "ambiguous"
    assert result["installations"][0]["responder"]["matches_install"] is None


@pytest.mark.parametrize("registry_present", [False, True])
def test_scan_incomplete_config_is_neither_stale_nor_empty(tmp_path, monkeypatch, registry_present):
    config = tmp_path / "ComfyUI" / "config.json"
    config.parent.mkdir()
    config.write_text("{broken", encoding="utf-8")
    executable = tmp_path / "ComfyUI.exe"
    executable.touch()
    entry = {"DisplayIcon": str(executable)} if registry_present else None

    def registrations(*args, **kwargs):
        return _read_registrations(
            {"APPDATA": str(tmp_path)}, registry=lambda: entry,
        )

    def get_json(url, timeout=2):
        assert url == local._PINOKIO_INFO_URL, "incomplete Desktop registration must not be probed"
        raise URLError(ConnectionRefusedError("refused"))

    monkeypatch.setattr(desktop, "read_registrations", registrations)
    monkeypatch.setattr(local, "_get_json", get_json)
    result = local.scan([])
    assert result["servers"] == []
    assert result["errors"] == []
    assert result["pinokio"] == {"available": False, "home": None}
    assert len(result["installations"]) == 1
    observation = result["installations"][0]
    assert observation["state"] == "ambiguous"
    assert observation["recorded"] == {
        "executable": str(executable) if registry_present else None,
        "data_dir": None,
    }
    assert observation["files"] == {"executable": registry_present, "data_dir": False}
    assert desktop.select_candidate(result["installations"], result["servers"]) == {
        "outcome": "none", "choices": [], "not_running": [observation],
    }
