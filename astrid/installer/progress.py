"""A calm sign of life for the installer's slow phases, on interactive terminals only.

Each slow phase prints one line. On an interactive terminal a stream of mink
tracks in snow runs along the end of that line: a mink bounds left to right,
leaving a pair of prints per bound, each pair filling in with snow as it ages,
and the next mink sets off before the last trail has gone. When the phase ends
the line keeps a check mark. Piped, redirected or logged output, CI,
``NO_COLOR`` and ``TERM=dumb`` get the plain line alone: no frames and no
control characters. ``public/install`` in the App draws the same frames, from
the same rule, around its own slow steps and tells this module, through
``HINT``, when its relay to the terminal is interactive.
"""

from __future__ import annotations

import codecs
import os
import stat
import sys
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator, TextIO


@dataclass(frozen=True)
class Tracks:
    """Minks running one after another across ``slots`` bound slots.

    A run that sets off at tick ``s`` lays a fresh print in slot ``i`` at tick
    ``s + i``; it then ages one stage a tick through ``ages`` (freshest first)
    and is gone. A new run sets off every ``period`` ticks, so the stream never
    resets. All ages have the same width, one terminal cell per character.
    """

    ages: tuple[str, ...]
    slots: int
    period: int
    mark: str

    @property
    def width(self) -> int:
        return self.slots * (len(self.ages[0]) + 1) - 1

    def cell(self, tick: int, slot: int) -> str:
        # In the steady stream at most one run can be at a slot at a time
        # (period >= len(ages)), so this is that run's print, or snow.
        age = (tick - slot) % self.period
        return self.ages[age] if age < len(self.ages) else " " * len(self.ages[0])

    def frame(self, tick: int) -> str:
        return " ".join(self.cell(tick, slot) for slot in range(self.slots))


# Seen from above, a bound is two oval prints side by side, one a little
# ahead (Braille dots 1,2 and 5,6); snow then fills them in, dot by dot.
UNICODE = Tracks(ages=("\u2833", "\u2832", "\u2822", "\u2820"), slots=6, period=8, mark="\u2713")
ASCII = Tracks(ages=("'.", "' ", " ."), slots=4, period=5, mark="done")
INTERVAL = 0.19  # a tick: a quarter of the App mink's 770 ms stride
# public/install relays installer output to the terminal through a pipe. When its
# own stdout is an interactive terminal it names the tracks to draw here.
HINT = "ASTRID_INSTALL_PROGRESS"
HINTS = {"unicode": UNICODE, "ascii": ASCII}


def frames(tracks: Tracks) -> list[str]:
    """The steady stream's cycle, one frame a tick, starting with a run setting off."""
    return [tracks.frame(tick) for tick in range(tracks.period)]


def _utf8(stream) -> bool:
    try:
        return codecs.lookup(getattr(stream, "encoding", None) or "ascii").name == "utf-8"
    except LookupError:
        return False


def style(stream=None, environ=None, platform: str | None = None) -> Tracks | None:
    """The tracks to draw on ``stream``, or None when it must get plain lines."""
    stream = sys.stdout if stream is None else stream
    environ = os.environ if environ is None else environ
    platform = sys.platform if platform is None else platform
    if environ.get("NO_COLOR") or environ.get("CI") or environ.get("TERM") == "dumb":
        return None
    try:
        if stream.isatty():
            hinted = None
        else:
            hinted = HINTS.get(environ.get(HINT, ""))
            # Only the bootstrap's relay pipe qualifies, never a file or a log.
            if hinted is None or not stat.S_ISFIFO(os.fstat(stream.fileno()).st_mode):
                return None
    except (AttributeError, OSError, ValueError):
        return None
    if not _utf8(stream):
        return ASCII
    if hinted is not None:
        return hinted
    # Windows conhost fonts lack these glyphs; Windows Terminal has them.
    return UNICODE if platform != "win32" or environ.get("WT_SESSION") else ASCII


class Progress:
    """Prints installer lines and animates the current phase line on a terminal.

    Call it like ``print`` for an ordinary line. ``phase`` opens a line for one
    slow step; any other line printed meanwhile closes it first.

    Output is best effort: once the stream cannot be written (a closed terminal,
    a broken pipe), every later line and frame is dropped, so a display error
    never fails, interrupts or replaces an install step or its rollback.
    """

    def __init__(self, stream: TextIO | None = None, *, tracks: Tracks | None = None,
                 animate: bool | None = None, interval: float = INTERVAL):
        self._stream = stream
        if animate is False:
            tracks = None
        elif tracks is None:
            tracks = style(self.stream)
        self.tracks = tracks
        self.interval = interval
        self._lock = threading.Lock()
        self._open: object | None = None  # the phase whose line is open
        self._text = ""  # that line's text
        self._broken = False  # the stream refused a write; say nothing more

    @property
    def stream(self) -> TextIO:
        return sys.stdout if self._stream is None else self._stream

    def _write(self, text: str) -> None:
        if self._broken:
            return
        try:
            self.stream.write(text)
            self.stream.flush()
        except (OSError, ValueError):
            self._broken = True

    def _close(self, mark: str = "") -> None:
        """End the open phase line: rewrite it whole with ``mark`` instead of the tracks.

        Rewriting from the line's start also clears a ``^C`` the terminal echoed.
        """
        if self._open is not None:
            self._open = None
            self._write(f"\r{self._text} {mark}" + " " * (self.tracks.width + 2) + "\n")

    def __call__(self, text: str) -> None:
        with self._lock:
            self._close()
            self._write(f"{text}\n")

    @contextmanager
    def phase(self, text: str) -> Iterator[None]:
        tracks = self.tracks
        if tracks is None or self._broken:
            self(text)
            yield
            return
        stop, line = threading.Event(), object()

        def draw() -> None:
            tick = 0
            while not stop.wait(self.interval):
                with self._lock:
                    if self._open is not line or self._broken:
                        return
                    self._write("\b" * tracks.width + tracks.frame(tick))
                tick = (tick + 1) % tracks.period

        ticker = threading.Thread(target=draw, name="astrid-install-progress", daemon=True)
        finished = False
        # Cleanup covers opening the line and starting the ticker, so an
        # interrupt at any point still stops the drawing and ends the line.
        try:
            with self._lock:
                self._close()
                self._open, self._text = line, text
                self._write(text + " " + " " * tracks.width)
            ticker.start()
            yield
            finished = True
        finally:
            stop.set()
            if ticker.ident is not None:  # it started
                ticker.join()
            with self._lock:
                if self._open is line:  # a later line may already have closed it
                    self._close(tracks.mark if finished else "")


@contextmanager
def phase(say, text: str) -> Iterator[None]:
    """Run one slow phase under ``say``: animated by a Progress, a plain line otherwise."""
    if isinstance(say, Progress):
        with say.phase(text):
            yield
    else:
        say(text)
        yield
