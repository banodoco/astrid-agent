"""Typed service observations remain bounded, private and non-authoritative."""
import json
import subprocess
from unittest.mock import Mock

import pytest

from astrid.installer import service

OBJECT = "/org/freedesktop/systemd1/unit/astrid_2eservice"


def replies(monkeypatch, entries, *, resolved=OBJECT):
    calls = []
    def query(args):
        calls.append(args)
        if args[0] == "call":
            return json.dumps({"type": "o", "data": [resolved]}).encode()
        return json.dumps({"type": "a(sb)", "data": entries}).encode()
    monkeypatch.setattr(service, "_bounded_busctl", query)
    return calls


@pytest.mark.parametrize("entries", [[], [["/one", False]],
    [["/with space/*", True], ["/with space/*", False], [r"/escaped\\path", True]]])
def test_typed_array_preserves_all_tuples(monkeypatch, entries):
    calls = replies(monkeypatch, entries)
    assert service._typed_environment_files() == tuple(map(tuple, entries))
    assert [call[0] for call in calls] == ["call", "get-property", "call"]
    assert calls[1][1:] == ["org.freedesktop.systemd1", OBJECT,
                             "org.freedesktop.systemd1.Service", "EnvironmentFiles"]


@pytest.mark.parametrize("raw", [b'', b'{}', b'{"type":"a(sb)","type":"a(sb)","data":[]}',
    b'{"type":"as","data":[]}', b'{"type":"a(sb)","data":null}',
    b'{"type":"a(sb)","data":[["/secret",1]]}',
    b'{"type":"a(sb)","data":[["/secret",false],]}',
    b'{"type":"a(sb)","data":[["/secret",false,1]]}',
    b'{"type":"a(sb)","data":[["",false]]}',
    b'{"type":"a(sb)","data":[],"extra":0}',
    b'{"type":"a(sb)","data":[]} trailing',
    b'{"type":"a(sb)","data":[["\\ud800",false]]}'])
def test_bad_typed_replies_refuse_without_disclosing_value(monkeypatch, raw, caplog):
    def query(args):
        if args[0] == "call":
            return json.dumps({"type": "o", "data": [OBJECT]}).encode()
        return raw
    monkeypatch.setattr(service, "_bounded_busctl", query)
    with pytest.raises(ValueError) as error:
        service._typed_environment_files()
    assert "/secret" not in str(error.value)
    assert "/secret" not in caplog.text


def test_wrong_or_changed_object_refuses(monkeypatch):
    replies(monkeypatch, [], resolved="/org/freedesktop/systemd1/unit/other_2eservice")
    with pytest.raises(ValueError, match="object"):
        service._typed_environment_files()
    count = 0
    def query(args):
        nonlocal count
        if args[0] == "get-property":
            return b'{"type":"a(sb)","data":[]}'
        count += 1
        return json.dumps({"type":"o", "data":[OBJECT if count == 1 else "/wrong"]}).encode()
    monkeypatch.setattr(service, "_bounded_busctl", query)
    with pytest.raises(ValueError, match="object"):
        service._typed_environment_files()


@pytest.mark.parametrize("failure", [OSError("unavailable"), ValueError("failed"),
                                      subprocess.TimeoutExpired("busctl", 15)])
def test_query_failures_never_default_to_empty(monkeypatch, failure):
    monkeypatch.setattr(service, "_bounded_busctl", Mock(side_effect=failure))
    with pytest.raises(type(failure)):
        service._typed_environment_files()


def test_text_parsers_keep_other_fields_strict():
    fields = dict.fromkeys(service._TRANSFER_UNIT_PROPERTIES, "")
    output = "\n".join(f"{key}={value}" for key, value in fields.items())
    for parser in (service._transfer_unit_fields, service._snapshot_unit_fields):
        assert parser(output) == fields
        with pytest.raises(ValueError):
            parser(output + "\nId=astrid.service")
        with pytest.raises(ValueError):
            parser("\n".join(line for line in output.splitlines() if not line.startswith("User=")))
        with pytest.raises(ValueError):
            parser(output + "\nEnvironmentFiles=secret")


def test_busctl_capture_is_bounded_and_suppresses_errors(tmp_path, monkeypatch, capfd):
    executable = tmp_path / "busctl"
    executable.write_text("#!/usr/bin/env python3\nimport sys\nsys.stderr.write('private error')\nsys.stdout.write('x' * 65537)\n")
    executable.chmod(0o700)
    monkeypatch.setenv("PATH", str(tmp_path) + ":" + __import__("os").environ["PATH"])
    with pytest.raises(ValueError, match="limit"):
        service._bounded_busctl(["get-property"])
    assert "private" not in capfd.readouterr().err
    executable.write_text("#!/usr/bin/env python3\nimport sys\nsys.stderr.write('private error')\nsys.exit(1)\n")
    with pytest.raises(ValueError, match="failed"):
        service._bounded_busctl(["get-property"])
    assert "private" not in capfd.readouterr().err


def test_multiline_execstart_remains_complete_and_duplicate_strict():
    fields = dict.fromkeys(service._TRANSFER_UNIT_PROPERTIES, "")
    fields["ExecStart"] = "{ path=/python ; argv[]=line1\nline2 ; }"
    text = "\n".join(f"{key}={value}" for key, value in fields.items())
    for parser in (service._transfer_unit_fields, service._snapshot_unit_fields):
        assert parser(text) == fields
        with pytest.raises(ValueError):
            parser(text.replace("line2 ; }", "EnvironmentFiles=private\nline2 ; }"))
        with pytest.raises(ValueError):
            parser(text + "\nMainPID=1")
        with pytest.raises(ValueError):
            parser("\n".join(line for line in text.splitlines() if not line.startswith("ControlPID=")))


def test_busctl_timeout_kills_child_and_keeps_output_private(tmp_path, monkeypatch, capfd):
    executable = tmp_path / "busctl"
    executable.write_text("#!/usr/bin/env python3\nimport time\ntime.sleep(30)\n")
    executable.chmod(0o700)
    monkeypatch.setenv("PATH", str(tmp_path) + ":" + __import__("os").environ["PATH"])
    class NoEvents:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def register(self, *args): pass
        def select(self, timeout): return []
    monkeypatch.setattr(service.selectors, "DefaultSelector", NoEvents)
    with pytest.raises(ValueError, match="timed out"):
        service._bounded_busctl(["get-property"])
    assert capfd.readouterr() == ("", "")


def test_overlarge_array_and_strings_refuse(monkeypatch):
    replies(monkeypatch, [["/x", False]] * 1025)
    with pytest.raises(ValueError):
        service._typed_environment_files()
    replies(monkeypatch, [["/" + "x" * 4096, False]])
    with pytest.raises(ValueError):
        service._typed_environment_files()
