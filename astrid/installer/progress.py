"""A calm sign of life for the installer's slow phases, on interactive terminals only.

Each slow phase prints one line. On an interactive terminal a small zigzag draws
itself at the end of that line (bottom, up, across, down), then erases itself the
same way back, until the phase ends and the line keeps a check mark. Piped,
redirected or logged output, CI, ``NO_COLOR`` and ``TERM=dumb`` get the plain line
alone: no frames and no control characters. ``public/install`` in the App draws the
same frames around its own slow steps and tells this module, through ``HINT``, when
its relay to the terminal is interactive.
"""

from __future__ import annotations

import codecs
import os
import stat
import sys
import threading
from contextlib import contextmanager
from typing import Iterator, TextIO

UNICODE = "▁╱▔╲▁╱▔╲"
ASCII = "_/-\\_/-\\"
MARKS = {UNICODE: "✓", ASCII: "done"}
INTERVAL = 0.11  # about nine frames a second
# public/install relays installer output to the terminal through a pipe. When its
# own stdout is an interactive terminal it names the frames to draw here.
HINT = "ASTRID_INSTALL_PROGRESS"
HINTS = {"unicode": UNICODE, "ascii": ASCII}


def _counts(width: int) -> list[int]:
    """How many cells one cycle shows, tick by tick: 0 up to ``width`` and back to 1."""
    return [*range(width + 1), *range(width - 1, 0, -1)]


def frames(path: str) -> list[str]:
    """One cycle: draw ``path`` a cell at a time, then erase it back the same way."""
    return [path[:count].ljust(len(path)) for count in _counts(len(path))]


def _utf8(stream) -> bool:
    try:
        return codecs.lookup(getattr(stream, "encoding", None) or "ascii").name == "utf-8"
    except LookupError:
        return False


def style(stream=None, environ=None, platform: str | None = None) -> str | None:
    """The zigzag to draw on ``stream``, or None when it must get plain lines."""
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

    def __init__(self, stream: TextIO | None = None, *, path: str | None = None,
                 animate: bool | None = None, interval: float = INTERVAL):
        self._stream = stream
        if animate is False:
            path = None
        elif path is None:
            path = style(self.stream)
        self.path = path
        self.interval = interval
        self._lock = threading.Lock()
        self._open: object | None = None  # the phase whose line is open
        self._drawn = 0  # cells of the zigzag on screen
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
        """End the open phase line: rewrite it whole with ``mark`` instead of the zigzag.

        Rewriting from the line's start also clears a ``^C`` the terminal echoed.
        """
        if self._open is not None:
            self._open = None
            self._write(f"\r{self._text} {mark}" + " " * (len(self.path) + 2) + "\n")
            self._drawn = 0

    def _tick(self, count: int) -> None:
        """Draw or erase cells at the zigzag's tip until ``count`` are shown."""
        if count > self._drawn:
            self._write(self.path[self._drawn:count])
        elif count < self._drawn:
            self._write("\b \b" * (self._drawn - count))
        self._drawn = count

    def __call__(self, text: str) -> None:
        with self._lock:
            self._close()
            self._write(f"{text}\n")

    @contextmanager
    def phase(self, text: str) -> Iterator[None]:
        if self.path is None or self._broken:
            self(text)
            yield
            return
        counts = _counts(len(self.path))
        stop, line = threading.Event(), object()

        def draw() -> None:
            tick = 1
            while not stop.wait(self.interval):
                with self._lock:
                    if self._open is not line or self._broken:
                        return
                    self._tick(counts[tick % len(counts)])
                tick += 1

        ticker = threading.Thread(target=draw, name="astrid-install-progress", daemon=True)
        finished = False
        # Cleanup covers opening the line and starting the ticker, so an
        # interrupt at any point still stops the drawing and ends the line.
        try:
            with self._lock:
                self._close()
                self._open, self._drawn, self._text = line, 0, text
                self._write(text + " ")
            ticker.start()
            yield
            finished = True
        finally:
            stop.set()
            if ticker.ident is not None:  # it started
                ticker.join()
            with self._lock:
                if self._open is line:  # a later line may already have closed it
                    self._close(MARKS.get(self.path, "done") if finished else "")


@contextmanager
def phase(say, text: str) -> Iterator[None]:
    """Run one slow phase under ``say``: animated by a Progress, a plain line otherwise."""
    if isinstance(say, Progress):
        with say.phase(text):
            yield
    else:
        say(text)
        yield
