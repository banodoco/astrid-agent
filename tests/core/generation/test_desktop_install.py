"""Desktop discovery tests use redacted fixtures and explicit observation seams."""

from copy import deepcopy
from urllib.error import HTTPError, URLError

import pytest

from astrid.core.generation import desktop_install as desktop
from tests.fixtures.comfyui_desktop import FIXTURE_NAMES, get_json_for, load


def _unexpected_request(*args, **kwargs):
    pytest.fail("missing or inaccessible registration must not be probed")


def _refused(*args, **kwargs):
    raise URLError(ConnectionRefusedError("refused"))


def test_read_registry_and_config_with_windows_paths():
    environ = {
        "APPDATA": r"C:\Users\redacted\AppData\Roaming",
        "LOCALAPPDATA": r"C:\Users\redacted\AppData\Local",
    }
    entry = {
        "DisplayVersion": "0.7.2",
        "InstallLocation": r"C:\fallback",
        "DisplayIcon": r'"C:\Program Files\ComfyUI\ComfyUI.exe",0',
    }

    def read_text(path):
        assert path == environ["APPDATA"] + r"\ComfyUI\config.json"
        return r'{"basePath": "C:\\Users\\redacted\\Documents\\ComfyUI"}'

    assert desktop.read_registrations(environ, registry=lambda: entry, read_text=read_text) == [{
        "source": "registry+config",
        "version": "0.7.2",
        "executable": r"C:\Program Files\ComfyUI\ComfyUI.exe",
        "data_dir": r"C:\Users\redacted\Documents\ComfyUI",
        "server_port": 8000,
    }]


def test_config_only_uses_localappdata_and_native_join(tmp_path):
    appdata = tmp_path / "roaming"
    config = appdata / "ComfyUI" / "config.json"
    config.parent.mkdir(parents=True)
    config.write_text('{"basePath": "/redacted/data"}', encoding="utf-8")
    before = config.read_bytes()
    result = desktop.read_registrations(
        {"APPDATA": str(appdata), "LOCALAPPDATA": str(tmp_path / "local")},
        registry=lambda: None,
    )
    assert result == [{
        "source": "config",
        "version": None,
        "executable": str(tmp_path / "local" / "Programs" / "ComfyUI" / "ComfyUI.exe"),
        "data_dir": "/redacted/data",
        "server_port": 8000,
    }]
    assert config.read_bytes() == before


def test_registry_only_uses_install_location():
    assert desktop.read_registrations(
        {}, registry=lambda: {"DisplayVersion": "0.7.2", "InstallLocation": r"C:\ComfyUI"}
    ) == [{
        "source": "registry",
        "version": "0.7.2",
        "executable": r"C:\ComfyUI\ComfyUI.exe",
        "data_dir": None,
        "server_port": 8000,
    }]


@pytest.mark.parametrize("raw", ["invalid json", "[]", "null"])
def test_invalid_config_without_registry(raw):
    assert desktop.read_registrations(
        {"APPDATA": "/redacted"}, registry=lambda: None, read_text=lambda path: raw
    ) == []


def test_no_registry_or_config(tmp_path):
    assert desktop.read_registrations(
        {"APPDATA": str(tmp_path)}, registry=lambda: None
    ) == []
    assert desktop.observe_installations(
        environ={}, registry=lambda: None, get_json=_unexpected_request
    ) == []


def test_normalized_paths_are_platform_specific():
    assert desktop.path_equal(r"C:\Users\Creator\ComfyUI\\", "c:/users/CREATOR/ComfyUI")
    assert desktop.path_equal("/home/creator/data/../ComfyUI/", "/home/creator/ComfyUI")
    assert not desktop.path_equal("/home/creator/ComfyUI", "/home/creator/comfyui")
    assert not desktop.path_equal(r"C:\ComfyUI", r"C:\Other")
    assert not desktop.path_equal(None, None)
    assert not desktop.path_equal("", "")
    assert desktop.parent_dir(r"C:\Users\Creator\ComfyUI\custom_nodes") == r"C:\Users\Creator\ComfyUI"
    assert desktop.parent_dir("/home/creator/ComfyUI/custom_nodes") == "/home/creator/ComfyUI"


@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_classifies_each_redacted_fixture(name):
    fixture = load(name)
    result = desktop.classify(
        fixture["registration"],
        files=fixture["files"],
        responders=desktop.probe_ports(
            desktop.DESKTOP_CANDIDATE_PORTS, get_json=get_json_for(fixture)
        ),
    )
    registration = fixture["registration"]
    assert result["layout"] == "desktop"
    assert result["state"] == name
    assert result["version"] == registration["version"]
    assert result["endpoint"] == "http://127.0.0.1:8000"
    assert result["recorded"] == {
        "executable": registration["executable"], "data_dir": registration["data_dir"]
    }
    assert result["files"] == fixture["files"]
    assert result["responder"] == {
        "kind": {"running": "comfyui", "closed": "none", "stale": "none",
                 "wrong_responder": "other"}[name],
        "port": 8000 if name in {"running", "wrong_responder"} else None,
        "base_dir": registration["data_dir"] if name == "running" else None,
        "matches_install": True if name == "running" else None,
    }


def _observe(fixture, *, get_json=None, **kwargs):
    registration = fixture["registration"]
    if get_json is None:
        get_json = get_json_for(fixture)
    files = {
        registration[field]: fixture["files"][field]
        for field in ("executable", "data_dir")
    }
    return desktop.observe_installations(
        registrations=[registration], get_json=get_json, exists=files.__getitem__, **kwargs
    )[0]


def test_stale_never_calls_http_even_if_responder_exists():
    fixture = load("stale")
    assert _observe(fixture, get_json=_unexpected_request)["state"] == "stale"
    fixture["files"]["data_dir"] = True
    assert _observe(fixture, get_json=_unexpected_request)["state"] == "stale"


def test_denied_http_is_not_closed_or_wrong():
    fixture = load("running")
    result = _observe(fixture, get_json=get_json_for(fixture, denied_ports=(8000,)))
    assert result["state"] == "denied"
    assert result["responder"] == {
        "kind": "denied", "port": 8000, "base_dir": None, "matches_install": None
    }
    closed = load("closed")
    result = _observe(closed, get_json=get_json_for(closed, denied_ports=(8188,)))
    assert result["state"] == "denied"
    assert result["responder"]["port"] == 8188


def test_permission_error_nested_in_cause_and_reason():
    def denied(port, path):
        try:
            raise PermissionError("blocked")
        except PermissionError as exc:
            raise URLError(RuntimeError("transport")) from exc

    assert desktop.probe_ports([8000], get_json=denied)[8000]["kind"] == "denied"


def test_folder_permission_error_remains_denied():
    fixture = load("running")
    underlying = get_json_for(fixture)

    def get_json(port, path):
        if port == 8000 and path == "/internal/folder_paths":
            raise URLError(PermissionError("blocked"))
        return underlying(port, path)

    assert _observe(fixture, get_json=get_json)["state"] == "denied"


@pytest.mark.parametrize("source", ["registry", "config"])
def test_inaccessible_registration_is_not_empty(source):
    def denied(*args):
        raise PermissionError("denied")

    results = desktop.observe_installations(
        environ={"APPDATA": "/redacted"},
        registry=denied if source == "registry" else lambda: None,
        read_text=denied if source == "config" else lambda path: '{"basePath": "/data"}',
        exists=lambda path: True,
        get_json=_unexpected_request,
    )
    assert len(results) == 1
    assert results[0]["state"] == "denied"
    assert results[0]["responder"]["matches_install"] is None


def test_file_stat_permission_is_not_missing(monkeypatch, tmp_path):
    executable = tmp_path / "ComfyUI.exe"
    executable.touch()
    original = desktop.os.stat

    def stat(path, *args, **kwargs):
        if str(path) == str(executable):
            raise PermissionError("blocked")
        return original(path, *args, **kwargs)

    registration = {
        "executable": str(executable), "data_dir": str(tmp_path), "server_port": 8000
    }
    with monkeypatch.context() as m:
        m.setattr(desktop.os, "stat", stat)
        result = desktop.observe_installations(
            registrations=[registration], get_json=_unexpected_request
        )[0]
    assert result["state"] == "denied"
    assert result["files"] == {"executable": False, "data_dir": True}


def test_ambiguous_when_folder_paths_are_unavailable():
    fixture = load("running")
    underlying = get_json_for(fixture)

    def get_json(port, path):
        if path == "/internal/folder_paths":
            raise HTTPError("http://127.0.0.1:8000" + path, 404, "Not Found", {}, None)
        return underlying(port, path)

    result = _observe(fixture, get_json=get_json)
    assert result["state"] == "ambiguous"
    assert result["responder"] == {
        "kind": "comfyui", "port": 8000, "base_dir": None, "matches_install": None
    }


def test_other_comfy_at_recorded_port_is_wrong_responder():
    fixture = load("running")
    other = r"C:\Users\redacted\Other"
    fixture["responders"]["8000"]["folder_paths"] = {"custom_nodes": [other + r"\custom_nodes"]}
    result = _observe(fixture)
    assert result["state"] == "wrong_responder"
    assert result["responder"] == {
        "kind": "comfyui", "port": 8000, "base_dir": other, "matches_install": False
    }


def test_other_comfy_at_different_port_leaves_desktop_closed():
    fixture = load("running")
    other = fixture["responders"].pop("8000")
    other["folder_paths"] = {"custom_nodes": [r"C:\Other\custom_nodes"]}
    fixture["responders"]["8188"] = other
    assert _observe(fixture)["state"] == "closed"


def test_changed_port_wins_over_old_wrong_responder():
    fixture = load("running")
    fixture["responders"]["8188"] = fixture["responders"]["8000"]
    fixture["responders"]["8000"] = load("wrong_responder")["responders"]["8000"]
    result = _observe(fixture)
    assert result["state"] == "running"
    assert result["endpoint"] == "http://127.0.0.1:8188"
    assert result["responder"]["matches_install"] is True


def test_matching_ports_prefer_recorded_port_and_do_not_duplicate_choice():
    fixture = load("running")
    fixture["responders"]["8188"] = deepcopy(fixture["responders"]["8000"])
    result = _observe(fixture)
    assert result["endpoint"] == "http://127.0.0.1:8000"
    servers = [
        {"url": f"http://127.0.0.1:{port}",
         "folder_paths": fixture["responders"][str(port)]["folder_paths"]}
        for port in (8188, 8000)
    ]
    assert desktop.select_candidate([result], servers) == {
        "outcome": "one",
        "choices": [{"layout": "desktop", "endpoint": result["endpoint"], "label": "ComfyUI Desktop"}],
        "not_running": [],
    }


def test_duplicate_registrations_and_ports_are_observed_once():
    fixture = load("running")
    registration = fixture["registration"]
    duplicate = {**registration, "data_dir": registration["data_dir"].lower().replace("\\", "/")}
    requests = []
    underlying = get_json_for(fixture)

    def get_json(port, path):
        requests.append((port, path))
        return underlying(port, path)

    observations = desktop.observe_installations(
        registrations=[registration, duplicate], exists=lambda path: True,
        get_json=get_json, ports=[8000, 8188, 8000],
    )
    assert len(observations) == 1
    assert observations[0]["state"] == "running"
    assert requests == [(8000, "/system_stats"), (8000, "/internal/folder_paths"),
                        (8188, "/system_stats")]


def test_select_none_one_and_mixed_layouts():
    closed = _observe(load("closed"))
    running = _observe(load("running"))
    assert desktop.select_candidate([closed], []) == {
        "outcome": "none", "choices": [], "not_running": [closed]
    }
    assert desktop.select_candidate([], []) == {
        "outcome": "none", "choices": [], "not_running": []
    }
    one = desktop.select_candidate([running], [])
    assert one["outcome"] == "one"
    portable = {"url": "http://127.0.0.1:8188",
                "folder_paths": {"custom_nodes": [r"C:\Portable\custom_nodes"]}}
    pinokio = {"url": "http://127.0.0.1:9000", "pinokio_apps": ["comfy.git"]}
    mixed = desktop.select_candidate([running, closed], [portable, pinokio, portable])
    assert mixed == {
        "outcome": "several",
        "choices": [
            *one["choices"],
            {"layout": "other", "endpoint": portable["url"], "label": "ComfyUI at " + portable["url"]},
            {"layout": "other", "endpoint": pinokio["url"], "label": "ComfyUI at " + pinokio["url"] + " (Pinokio)"},
        ],
        "not_running": [closed],
    }


@pytest.mark.parametrize("payload", [None, [], {"system": {}}, {"system": {}, "devices": {}},
                                    {"system": "fake", "devices": []}])
def test_shape_check_never_queries_folders_of_non_comfy(payload):
    def get_json(port, path):
        assert path == "/system_stats"
        return payload

    assert desktop.probe_ports([8000], get_json=get_json)[8000]["kind"] == "other"


def test_default_probe_uses_loopback_and_one_second_timeout(monkeypatch):
    from astrid.packs.vibecomfy import local

    calls = []

    def get_json(url, timeout):
        calls.append((url, timeout))
        if url.endswith("/system_stats"):
            return {"system": {"comfyui_version": "0.8.0"}, "devices": []}
        return {"custom_nodes": ["/data/custom_nodes"]}

    monkeypatch.setattr(local, "_get_json", get_json)
    assert desktop.probe_ports([8000, 8188]) == {
        port: {"kind": "comfyui", "version": "0.8.0", "base_dir": "/data"}
        for port in (8000, 8188)
    }
    assert calls == [
        (f"http://127.0.0.1:{port}{path}", 1.0)
        for port in (8000, 8188)
        for path in ("/system_stats", "/internal/folder_paths")
    ]
