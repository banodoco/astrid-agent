"""Generation dispatch preserves the callable contract and startup timings."""

from contextvars import copy_context

import pytest

from astrid.core.gateway import dispatch
from astrid.core.generation import cli
from astrid.sdk import command_timing


@pytest.mark.parametrize("form", ["image", "workflow"])
def test_gateway_calls_generation_with_one_positional_argument(monkeypatch, form):
    calls = []

    def entry(args):
        calls.append(args)
        return 17

    monkeypatch.setattr(cli, "main", entry)
    assert dispatch._dispatch_generate([form, "input"]) == 17
    assert calls == [[form, "input"]]


def test_gateway_does_not_retry_entry_type_error(monkeypatch):
    calls = []

    def entry(args):
        calls.append(args)
        raise TypeError("entry failed")

    monkeypatch.setattr(cli, "main", entry)
    with pytest.raises(TypeError, match="entry failed"):
        dispatch._dispatch_generate(["image", "input"])
    assert calls == [["image", "input"]]


def test_dispatch_timing_is_scoped_to_gateway_call(monkeypatch):
    children = []
    original_init = command_timing.TimingCollector.__init__

    def capture(self, *args, **kwargs):
        children.append(kwargs.get("startup_child"))
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(command_timing.TimingCollector, "__init__", capture)
    monkeypatch.setattr(cli, "_main_with_errors", lambda args: 0)
    assert dispatch._dispatch_generate(["image", "input"]) == 0
    assert cli.main(["image", "input"]) == 0
    assert children[0][0] == "dispatch"
    assert children[0][1] <= children[0][2]
    assert children[1] is None


def test_console_reports_dispatch_timing(monkeypatch, capsys):
    from astrid import console

    monkeypatch.setattr(cli, "_main_with_errors", lambda args: 0)
    assert console.main(["generate", "image", "input", "--timings"]) == 0
    assert "dispatch:" in capsys.readouterr().err


def test_copied_dispatch_context_expires_after_nested_exception(monkeypatch):
    children = []
    original_init = command_timing.TimingCollector.__init__

    def capture(self, *args, **kwargs):
        children.append(kwargs.get("startup_child"))
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(command_timing.TimingCollector, "__init__", capture)
    monkeypatch.setattr(cli, "_main_with_errors", lambda args: 0)
    with cli.dispatch_timing((1.0, 2.0)):
        outer = copy_context()
        with pytest.raises(ValueError, match="inner"):
            with cli.dispatch_timing((3.0, 4.0)):
                inner = copy_context()
                cli.main(["image"])
                raise ValueError("inner")
        cli.main(["image"])
        inner.run(cli.main, ["image"])
    outer.run(cli.main, ["image"])
    assert children == [("dispatch", 3.0, 4.0), ("dispatch", 1.0, 2.0), None, None]
