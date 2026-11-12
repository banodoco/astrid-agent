from __future__ import annotations

import io
import os
import re
import threading
from pathlib import Path

import pytest

from astrid.installer import progress
from astrid.installer.progress import ASCII, HINT, UNICODE, Progress, frames, phase, style

CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


class Terminal(io.StringIO):
    """A UTF-8 interactive terminal stand-in that records every write."""

    encoding = "utf-8"

    def isatty(self):
        return True


def test_frames_draw_the_zigzag_then_erase_it_back():
    assert frames(UNICODE) == [
        "        ", "▁       ", "▁╱      ", "▁╱▔     ", "▁╱▔╲    ", "▁╱▔╲▁   ",
        "▁╱▔╲▁╱  ", "▁╱▔╲▁╱▔ ", "▁╱▔╲▁╱▔╲", "▁╱▔╲▁╱▔ ", "▁╱▔╲▁╱  ", "▁╱▔╲▁   ",
        "▁╱▔╲    ", "▁╱▔     ", "▁╱      ", "▁       ",
    ]
    assert frames(UNICODE) == frames(UNICODE)
    assert [frame.rstrip() for frame in frames(ASCII)][:5] == ["", "_", "_/", "_/-", "_/-\\"]
    assert all(len(frame) == 8 for frame in frames(UNICODE) + frames(ASCII))
    assert ASCII.isascii()


def test_frames_match_the_bootstrap_shell_copy():
    # public/install (App) draws the same cycle; keep the two in step.
    assert UNICODE == "▁╱▔╲▁╱▔╲"
    assert ASCII == "_/-\\_/-\\"


@pytest.mark.parametrize("environ", [
    {"NO_COLOR": "1"}, {"TERM": "dumb"}, {"CI": "true"},
])
def test_terminal_opt_outs_get_plain_lines(environ):
    assert style(Terminal(), environ, "darwin") is None


def test_terminal_style_follows_encoding_and_platform():
    assert style(Terminal(), {}, "darwin") == UNICODE
    assert style(Terminal(), {}, "win32") == ASCII  # conhost
    assert style(Terminal(), {"WT_SESSION": "x"}, "win32") == UNICODE

    class Latin1(Terminal):
        encoding = "latin-1"

    assert style(Latin1(), {}, "linux") == ASCII


def test_bootstrap_hint_applies_only_to_its_relay_pipe(tmp_path):
    read, write = os.pipe()
    try:
        with os.fdopen(write, "w", encoding="utf-8", closefd=False) as pipe:
            assert style(pipe, {}, "darwin") is None
            assert style(pipe, {HINT: "unicode"}, "darwin") == UNICODE
            assert style(pipe, {HINT: "ascii"}, "darwin") == ASCII
            assert style(pipe, {HINT: "unicode", "NO_COLOR": "1"}, "darwin") is None
    finally:
        os.close(read)
        os.close(write)
    with (tmp_path / "install.log").open("w", encoding="utf-8") as log:
        assert style(log, {HINT: "unicode"}, "darwin") is None
    assert style(io.StringIO(), {HINT: "unicode"}, "darwin") is None


def test_non_terminal_output_has_plain_lines_without_frames_or_controls():
    output = io.StringIO()
    say = Progress(output, interval=0.001)
    assert say.path is None
    say("Using your workspace at /tmp/w.")
    with say.phase("Downloading Release 12"):
        threading.Event().wait(0.05)
    with say.phase("Checking it"):
        pass
    text = output.getvalue()
    assert text == "Using your workspace at /tmp/w.\nDownloading Release 12\nChecking it\n"
    assert not CONTROL.search(text)
    assert not any(cell in text for cell in UNICODE + "✓")


def _rendered(text: str) -> list[str]:
    """What a terminal shows: apply backspaces and carriage returns cell by cell."""
    lines, cells, cursor = [], [], 0
    for char in text:
        if char == "\n":
            lines.append("".join(cells).rstrip())
            cells, cursor = [], 0
        elif char == "\b":
            cursor = max(0, cursor - 1)
        elif char == "\r":
            cursor = 0
        else:
            if cursor < len(cells):
                cells[cursor] = char
            else:
                cells.append(char)
            cursor += 1
    if cells:
        lines.append("".join(cells).rstrip())
    return lines


def test_terminal_phase_animates_and_ends_in_a_clean_checked_line():
    output = Terminal()
    say = Progress(output, interval=0.001)
    assert say.path == UNICODE
    with say.phase("Downloading Release 12"):
        threading.Event().wait(0.1)
    say("Open https://astrid.haus")
    text = output.getvalue()
    assert "▁╱▔╲▁╱▔╲" in text  # it drew the whole zigzag at least once
    assert _rendered(text) == ["Downloading Release 12 ✓", "Open https://astrid.haus"]
    assert text.endswith("\rDownloading Release 12 ✓          \nOpen https://astrid.haus\n")


def test_closing_rewrites_the_line_over_an_echoed_interrupt():
    output = Terminal()
    say = Progress(output, interval=0.001)
    with pytest.raises(KeyboardInterrupt):
        with say.phase("Downloading Release 12"):
            threading.Event().wait(0.02)
            output.write("^C")  # the terminal's echo of Ctrl+C lands mid-zigzag
            raise KeyboardInterrupt
    assert _rendered(output.getvalue()) == ["Downloading Release 12"]


def test_ascii_terminal_ends_with_done():
    output = Terminal()
    say = Progress(output, path=ASCII, interval=0.001)
    with say.phase("Stopping Release 1"):
        threading.Event().wait(0.02)
    assert _rendered(output.getvalue()) == ["Stopping Release 1 done"]
    assert output.getvalue().isascii()


def test_indicator_stops_on_exception_and_leaves_an_unmarked_line():
    output = Terminal()
    say = Progress(output, interval=0.001)
    with pytest.raises(KeyboardInterrupt):
        with say.phase("Starting Release 12"):
            threading.Event().wait(0.02)
            raise KeyboardInterrupt
    assert not any(thread.name == "astrid-install-progress" for thread in threading.enumerate())
    settled = output.getvalue()
    threading.Event().wait(0.02)
    assert output.getvalue() == settled  # nothing draws after the phase ended
    assert _rendered(settled) == ["Starting Release 12"]
    assert settled.endswith("\n")


def test_a_line_printed_during_a_phase_closes_it_first():
    output = Terminal()
    say = Progress(output, interval=0.001)
    with say.phase("Downloading Release 12"):
        threading.Event().wait(0.02)
        say("Rendering unavailable: see the log.")
        threading.Event().wait(0.02)
    assert _rendered(output.getvalue()) == ["Downloading Release 12", "Rendering unavailable: see the log."]


def test_a_phase_inside_a_phase_never_draws_twice_or_closes_the_wrong_line():
    output = Terminal()
    say = Progress(output, interval=0.001)
    with say.phase("Starting Release 12"):
        with say.phase("Checking Release 12 started"):
            threading.Event().wait(0.02)
        threading.Event().wait(0.02)
    assert _rendered(output.getvalue()) == ["Starting Release 12", "Checking Release 12 started ✓"]


def test_phase_helper_keeps_plain_say_callables():
    lines = []
    with phase(lines.append, "Checking it"):
        lines.append("inside")
    assert lines == ["Checking it", "inside"]
    assert progress.MARKS[UNICODE] == "✓"


def test_cli_routes_backend_lines_through_its_progress_but_keeps_injected_sinks(tmp_path):
    from astrid.installer.backend import Backend
    from astrid.installer.cli import _speaking

    say = Progress(io.StringIO())
    backend = _speaking(Backend(tmp_path / "program", tmp_path / "install.log", tmp_path / "uv"), say)
    assert backend.say is say and backend.service.say is say
    sink = []
    kept = _speaking(Backend(tmp_path / "program", tmp_path / "install.log", tmp_path / "uv", say=sink.append), say)
    assert kept.say == sink.append and kept.service.say == sink.append


def test_repaired_chat_command_note_goes_to_the_log_not_the_terminal(tmp_path):
    import json
    import sys

    from astrid.installer.backend import Backend
    from astrid.installer.core import Version, write_json

    declaration = tmp_path / "services.json"
    write_json(declaration, {"services": [{"name": "chat", "env": {"ASTRID_ACP_COMMAND": str(tmp_path / "gone")}}]})
    profile = tmp_path / "profile.json"
    write_json(profile, {"service_declaration": str(declaration)})
    version = Version(tmp_path / "version", Path(sys.executable), profile, {})
    log, said = tmp_path / "install.log", []
    backend = Backend(tmp_path / "program", log, tmp_path / "uv", say=said.append)

    assert backend.repair_chat_command(version) is True
    assert said == []
    assert ("Previous chat command is unavailable or belongs to a versioned install; "
            "using the selected version's default.") in log.read_text(encoding="utf-8")
    assert json.loads(declaration.read_text())["services"][0]["env"]["ASTRID_ACP_COMMAND"] != str(tmp_path / "gone")


class Failing(Terminal):
    """A terminal that goes away (EIO) after ``survives`` writes."""

    def __init__(self, survives: int):
        super().__init__()
        self.survives = survives

    def write(self, text):
        if self.survives <= 0:
            raise OSError(5, "Input/output error")
        self.survives -= 1
        return super().write(text)


@pytest.mark.parametrize("survives", range(6))
def test_a_terminal_that_goes_away_never_fails_a_step(survives):
    say = Progress(Failing(survives), interval=0.001)
    ran = []
    with say.phase("Downloading Release 12"):
        threading.Event().wait(0.02)
        ran.append("downloaded")
    say("Open https://astrid.haus")
    with say.phase("Checking it"):
        ran.append("checked")
    assert ran == ["downloaded", "checked"]
    assert not any(thread.name == "astrid-install-progress" for thread in threading.enumerate())


@pytest.mark.parametrize("survives", range(4))
def test_a_terminal_that_goes_away_keeps_the_original_exception(survives):
    say = Progress(Failing(survives), interval=0.001)
    with pytest.raises(RuntimeError, match="the new release did not start"):
        with say.phase("Starting Release 12"):
            threading.Event().wait(0.02)
            raise RuntimeError("the new release did not start")


def test_a_broken_plain_stream_drops_lines_instead_of_raising():
    class Broken(io.StringIO):
        def write(self, _text):
            raise BrokenPipeError(32, "Broken pipe")

    say = Progress(Broken())
    assert say.path is None
    say("Using your workspace at /tmp/w.")
    with say.phase("Downloading Release 12"):
        pass


@pytest.mark.parametrize("started", [True, False])
def test_an_interrupt_while_the_ticker_starts_stops_the_drawing(monkeypatch, started):
    real = threading.Thread

    class Interrupted(real):
        def start(self):
            if started:
                super().start()
            raise KeyboardInterrupt

    monkeypatch.setattr(progress.threading, "Thread", Interrupted)
    output = Terminal()
    say = Progress(output, interval=0.001)
    with pytest.raises(KeyboardInterrupt):
        with say.phase("Downloading Release 12"):
            pytest.fail("the step must not run after the interrupt")
    monkeypatch.setattr(progress.threading, "Thread", real)
    settled = output.getvalue()
    threading.Event().wait(0.03)
    assert output.getvalue() == settled  # nothing draws after the interrupt
    assert not any(thread.name == "astrid-install-progress" for thread in threading.enumerate())
    assert _rendered(settled) == ["Downloading Release 12"]
