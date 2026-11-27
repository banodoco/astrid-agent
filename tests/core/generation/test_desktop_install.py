"""Desktop discovery tests use redacted fixtures and explicit observation seams."""

from copy import deepcopy
import sys
from types import SimpleNamespace
from urllib.error import HTTPError, URLError

import pytest

from astrid.core.generation import desktop_install as desktop
from tests.fixtures.comfyui_desktop import FIXTURE_NAMES, get_json_for, load


def _unexpected_request(*args, **kwargs):
    pytest.fail("missing or inaccessible registration must not be probed")


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


@pytest.mark.parametrize("raw", ["invalid json", "[]", "null", "{}", '{"basePath": null}',
                                 '{"basePath": ""}', '{"basePath": 123}'])
def test_invalid_config_without_registry_preserves_incomplete_observation(raw):
    options = {"environ": {"APPDATA": "/redacted"}, "registry": lambda: None,
               "read_text": lambda path: raw}
    assert desktop.read_registrations(**options) == [{
        "source": "config", "version": None, "executable": None,
        "data_dir": None, "server_port": 8000,
    }]
    result = desktop.observe_installations(
        **options, get_json=_unexpected_request, exists=_unexpected_request,
    )
    assert len(result) == 1
    assert result[0]["state"] == "ambiguous"
    assert result[0]["recorded"] == {"executable": None, "data_dir": None}
    assert result[0]["files"] == {"executable": False, "data_dir": False}
    assert result[0]["responder"]["matches_install"] is None
    assert desktop.select_candidate(result, [])["not_running"] == result


@pytest.mark.parametrize("config", [None, "{broken", "{}", '{"basePath": null}'])
def test_registered_executable_with_unknown_data_path_is_not_stale(config, tmp_path):
    executable = tmp_path / "ComfyUI.exe"
    executable.touch()
    seen = []

    def read_text(path):
        if config is None:
            raise FileNotFoundError(path)
        return config

    def exists(path):
        seen.append(path)
        return desktop._exists(path)

    result = desktop.observe_installations(
        environ={"APPDATA": str(tmp_path)},
        registry=lambda: {"DisplayIcon": str(executable)},
        read_text=read_text, exists=exists, get_json=_unexpected_request,
    )
    assert seen == [str(executable)]
    assert len(result) == 1
    assert result[0]["state"] == "ambiguous"
    assert result[0]["recorded"] == {"executable": str(executable), "data_dir": None}
    assert result[0]["files"] == {"executable": True, "data_dir": False}
    assert result[0]["responder"]["kind"] == "none"


def test_undecodable_config_is_not_absence(tmp_path):
    config = tmp_path / "ComfyUI" / "config.json"
    config.parent.mkdir()
    config.write_bytes(b"\xff")
    before = config.read_bytes()
    result = desktop.observe_installations(
        environ={"APPDATA": str(tmp_path)}, registry=lambda: [],
        get_json=_unexpected_request, exists=_unexpected_request,
    )
    assert [item["state"] for item in result] == ["ambiguous"]
    assert config.read_bytes() == before


def test_config_only_known_data_unknown_executable_is_ambiguous(tmp_path):
    result = desktop.observe_installations(
        environ={"APPDATA": str(tmp_path)}, registry=lambda: [],
        read_text=lambda path: '{"basePath": "/data"}',
        exists=lambda path: path == "/data", get_json=_unexpected_request,
    )
    assert len(result) == 1
    assert result[0]["state"] == "ambiguous"
    assert result[0]["recorded"] == {"executable": None, "data_dir": "/data"}
    assert result[0]["files"] == {"executable": False, "data_dir": True}


def test_known_recorded_file_absence_is_still_stale(tmp_path):
    result = desktop.observe_installations(
        environ={"APPDATA": str(tmp_path)},
        registry=lambda: {"DisplayIcon": str(tmp_path / "missing.exe")},
        read_text=lambda path: '{"basePath": "/data"}',
        exists=lambda path: path == "/data", get_json=_unexpected_request,
    )
    assert len(result) == 1
    assert result[0]["state"] == "stale"
    assert result[0]["recorded"]["executable"] == str(tmp_path / "missing.exe")
    assert result[0]["files"] == {"executable": False, "data_dir": True}


def _install_native_registry_seam(monkeypatch, entries, *, deny_keys=(), vanished_keys=(),
                                  deny_enumeration_at=None):
    """Replace only the OS API; execute the actual default Windows reader."""
    hkey = object()
    uninstall_path = r"Software\Microsoft\Windows\CurrentVersion\Uninstall"
    key_read = 0x20019
    enumerated = []
    closed = []

    class Key:
        def __init__(self, name):
            self.name = name

        def __enter__(self):
            return self

        def __exit__(self, *args):
            closed.append(self.name)

    def open_key(parent, name, reserved=0, access=key_read):
        assert reserved == 0 and access == key_read
        if parent is hkey:
            assert name == uninstall_path
            return Key("uninstall")
        assert parent.name == "uninstall"
        if name in vanished_keys:
            raise FileNotFoundError(name)
        if name in deny_keys:
            raise PermissionError(name)
        assert name in entries
        return Key(name)

    def enum_key(parent, index):
        assert parent.name == "uninstall"
        enumerated.append(index)
        if index == deny_enumeration_at:
            raise PermissionError("uninstall enumeration denied")
        names = list(entries)
        if index >= len(names):
            exc = OSError("no more items")
            exc.winerror = 259
            raise exc
        return names[index]

    def query_value(key, field):
        if field not in entries[key.name]:
            raise FileNotFoundError(field)
        return entries[key.name][field], 1

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "winreg", SimpleNamespace(
        HKEY_CURRENT_USER=hkey, KEY_READ=key_read, OpenKey=open_key,
        EnumKey=enum_key, QueryValueEx=query_value,
    ))
    return enumerated, closed


def test_native_registry_enumerates_every_matching_uninstall_entry(monkeypatch):
    entries = {
        "first": {"DisplayName": "ComfyUI", "DisplayVersion": "0.7.2",
                  "DisplayIcon": r'"C:\First\ComfyUI.exe",0'},
        "unrelated": {"DisplayName": "Different app"},
        "disappeared": {"DisplayName": "ComfyUI"},
        "last": {"DisplayName": "ComfyUI", "DisplayVersion": "0.8.0",
                 "InstallLocation": r"C:\Last"},
    }
    with monkeypatch.context() as m:
        enumerated, closed = _install_native_registry_seam(
            m, entries, vanished_keys=("disappeared",),
        )
        registrations = desktop.read_registrations(
            {"APPDATA": r"C:\Roaming"},
            read_text=lambda path: r'{"basePath": "C:\\Data"}',
        )
    assert enumerated == [0, 1, 2, 3, 4]
    assert closed == ["first", "unrelated", "last", "uninstall"]
    assert registrations == [
        {"source": "registry+config", "version": "0.7.2", "executable": r"C:\First\ComfyUI.exe",
         "data_dir": r"C:\Data", "server_port": 8000},
        {"source": "registry+config", "version": "0.8.0", "executable": r"C:\Last\ComfyUI.exe",
         "data_dir": r"C:\Data", "server_port": 8000},
    ]


def test_native_registry_keeps_all_incomplete_registrations(monkeypatch):
    entries = {
        "first": {"DisplayName": "ComfyUI", "DisplayVersion": "0.7.2"},
        "last": {"DisplayName": "ComfyUI", "DisplayVersion": "0.8.0"},
    }
    with monkeypatch.context() as m:
        enumerated, _ = _install_native_registry_seam(m, entries)
        result = desktop.observe_installations(
            environ={}, get_json=_unexpected_request, exists=_unexpected_request,
        )
    assert enumerated == [0, 1, 2]
    assert [item["version"] for item in result] == ["0.7.2", "0.8.0"]
    assert [item["state"] for item in result] == ["ambiguous", "ambiguous"]


@pytest.mark.parametrize("deny_enumeration", [False, True])
def test_native_registry_partial_denial_is_not_complete_absence(monkeypatch, deny_enumeration):
    entries = {
        "first": {"DisplayName": "ComfyUI", "DisplayVersion": "0.7.2"},
        "denied": {"DisplayName": "ComfyUI"},
        "last": {"DisplayName": "ComfyUI", "DisplayVersion": "0.8.0"},
    }
    with monkeypatch.context() as m:
        enumerated, _ = _install_native_registry_seam(
            m, entries, deny_keys=("denied",),
            deny_enumeration_at=1 if deny_enumeration else None,
        )
        result = desktop.observe_installations(
            environ={}, get_json=_unexpected_request, exists=_unexpected_request,
        )
    assert [item["state"] for item in result] == (
        ["ambiguous", "denied"] if deny_enumeration else ["ambiguous", "denied", "ambiguous"]
    )
    assert enumerated == ([0, 1] if deny_enumeration else [0, 1, 2, 3])


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
    assert desktop.parent_dir("/home/creator/ComfyUI/custom_nodes/") == "/home/creator/ComfyUI"


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


def test_shared_data_root_cannot_identify_distinct_registered_executables():
    fixture = load("running")
    registration = fixture["registration"]
    other = {**registration, "executable": r"C:\Other\ComfyUI.exe"}
    observations = desktop.observe_installations(
        registrations=[registration, other], exists=lambda path: True,
        get_json=get_json_for(fixture),
    )
    assert len(observations) == 2
    assert all(item["state"] == "ambiguous" for item in observations)
    assert all(item["responder"]["matches_install"] is None for item in observations)
    assert desktop.select_candidate(observations, [])["outcome"] == "none"


def test_two_distinct_data_roots_remain_two_choices():
    fixture = load("running")
    registration = fixture["registration"]
    other = {**registration, "executable": r"C:\Other\ComfyUI.exe",
             "data_dir": r"C:\Other\data", "server_port": 8188}
    fixture["responders"]["8188"] = deepcopy(fixture["responders"]["8000"])
    fixture["responders"]["8188"]["folder_paths"] = {
        "custom_nodes": [r"C:\Other\data\custom_nodes"]
    }
    observations = desktop.observe_installations(
        registrations=[registration, other], exists=lambda path: True,
        get_json=get_json_for(fixture),
    )
    assert [item["state"] for item in observations] == ["running", "running"]
    assert desktop.select_candidate(observations, [])["outcome"] == "several"


def test_all_complete_registrations_share_changed_port_evidence():
    fixture = load("running")
    registration = fixture["registration"]
    other = {**registration, "executable": r"C:\Other\ComfyUI.exe",
             "data_dir": r"C:\Other\data", "server_port": 9000}
    fixture["responders"]["9000"] = fixture["responders"].pop("8000")
    observations = desktop.observe_installations(
        registrations=[registration, other], exists=lambda path: True,
        get_json=get_json_for(fixture),
    )
    assert observations[0]["state"] == "running"
    assert observations[0]["endpoint"] == "http://127.0.0.1:9000"
    assert observations[1]["state"] == "wrong_responder"


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
