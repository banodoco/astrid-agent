"""Contract tests for the redacted Desktop discovery fixtures."""

import ipaddress
import re
from urllib.error import HTTPError, URLError

import pytest

from tests.fixtures.comfyui_desktop import FIXTURE_NAMES, facts, get_json_for, load


KEYS = {"state", "source_report", "shape_basis", "registration", "files", "responders", "tells_apart"}
REDACTED_PREFIX = "C:\\Users\\<user>\\"
SOURCE = "banodoco/astrid-dev#1848"
SHAPE_BASIS = (
    "assumed from Desktop's public layout; #1848 records only version 0.7.2 "
    "and the missing paths; not yet verified on a real Windows machine"
)


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_fixture_schema_and_redaction(name):
    fixture = load(name)
    assert fixture.keys() == KEYS
    assert fixture["state"] == name
    assert fixture["source_report"] == SOURCE
    assert fixture["shape_basis"] == SHAPE_BASIS
    assert fixture["registration"] == {
        "source": "registry+config",
        "version": "0.7.2",
        "executable": REDACTED_PREFIX + "AppData\\Local\\Programs\\ComfyUI\\ComfyUI.exe",
        "data_dir": REDACTED_PREFIX + "Documents\\ComfyUI",
        "server_port": 8000,
    }
    assert fixture["files"].keys() == {"executable", "data_dir"}
    assert all(isinstance(value, bool) for value in fixture["files"].values())
    assert isinstance(fixture["tells_apart"], list) and fixture["tells_apart"]
    for value in _strings(fixture):
        assert "@" not in value
        assert not re.search(r"(?i)(?<![a-z])[a-bd-z]:", value)
        if re.search(r"(?i)[a-z]:[\\/]|\\\\", value):
            assert value.startswith(REDACTED_PREFIX)
        for candidate in re.findall(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])", value):
            assert ipaddress.ip_address(candidate) == ipaddress.ip_address("127.0.0.1")
        # Also reject IPv6 literals (including IPv4-mapped forms).
        for candidate in re.findall(r"(?i)[0-9a-f:.]*:[0-9a-f:.]*:[0-9a-f:.]*", value):
            try:
                address = ipaddress.ip_address(candidate)
            except ValueError:
                continue
            assert address == ipaddress.ip_address("127.0.0.1")


def test_facts_distinguish_all_four_states():
    expected = {
        "running": {"files": (True, True), "port_kind": "comfyui", "base_dir_matches": True},
        "closed": {"files": (True, True), "port_kind": "none", "base_dir_matches": None},
        "stale": {"files": (False, False), "port_kind": "none", "base_dir_matches": None},
        "wrong_responder": {"files": (True, True), "port_kind": "other", "base_dir_matches": None},
    }
    actual = {name: facts(load(name)) for name in FIXTURE_NAMES}
    assert actual == expected
    assert len({tuple(value.items()) for value in actual.values()}) == 4


def test_running_base_dir_mismatch():
    fixture = load("running")
    fixture["responders"]["8000"]["folder_paths"]["custom_nodes"][0] = (
        REDACTED_PREFIX + "Documents\\Other\\custom_nodes"
    )
    assert facts(fixture)["base_dir_matches"] is False


def test_stale_is_the_reported_missing_paths_state():
    fixture = load("stale")
    assert fixture["files"] == {"executable": False, "data_dir": False}
    assert fixture["responders"] == {}


@pytest.mark.parametrize(("name", "port"), [("closed", 8000), ("stale", 8000), ("running", 9999)])
def test_refused(name, port):
    with pytest.raises(URLError) as exc:
        get_json_for(load(name))(port, "/system_stats")
    assert type(exc.value) is URLError
    assert isinstance(exc.value.reason, ConnectionRefusedError)
    assert exc.value.reason.errno == 111


@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_denied_precedes_responder(name):
    with pytest.raises(URLError) as exc:
        get_json_for(load(name), denied_ports=(8000,))(8000, "/system_stats")
    assert type(exc.value) is URLError
    assert isinstance(exc.value.reason, PermissionError)
    assert exc.value.reason.errno == 1


@pytest.mark.parametrize("name", ["running", "wrong_responder"])
def test_http_payloads_and_404(name):
    fixture = load(name)
    probe = get_json_for(fixture)
    responder = fixture["responders"]["8000"]
    assert probe(8000, "/system_stats") == responder["system_stats"]
    paths = ["/unknown"]
    if name == "running":
        assert probe(8000, "/internal/folder_paths") == responder["folder_paths"]
    else:
        paths.append("/internal/folder_paths")
    for path in paths:
        with pytest.raises(HTTPError) as exc:
            probe(8000, path)
        assert type(exc.value) is HTTPError
        assert exc.value.code == 404
        assert exc.value.reason == "Not Found"
        assert exc.value.url == f"http://127.0.0.1:8000{path}"


@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_load_returns_independent_copies(name):
    first = load(name)
    original = load(name)
    first["registration"]["version"] = "changed"
    first["files"]["executable"] = not first["files"]["executable"]
    first["tells_apart"].clear()
    first["responders"].clear()
    assert load(name) == original
    assert first != original
