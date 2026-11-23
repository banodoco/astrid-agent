from __future__ import annotations

import contextlib
import io
import json
import sys
import threading
from types import SimpleNamespace

from astrid.core.generation import cli
from tests.sdk.test_timeline_sync import connected  # noqa: F401


class _OrderedSink:
    """A shared command destination that pauses the second result mid-write."""

    def __init__(self) -> None:
        self.buffer = io.StringIO()
        self.second_written = threading.Event()
        self.first_returned = threading.Event()

    def write(self, text: str) -> int:
        self.buffer.write(text)
        if '"marker":"second"' in text or '"marker": "second"' in text:
            self.second_written.set()
            assert self.first_returned.wait(5)
        return len(text)

    def flush(self) -> None:
        self.buffer.flush()

    def read_results(self) -> list[dict]:
        text = self.buffer.getvalue()
        decoder = json.JSONDecoder()
        results = []
        cursor = 0
        while cursor < len(text):
            while cursor < len(text) and text[cursor].isspace():
                cursor += 1
            if cursor == len(text):
                break
            result, cursor = decoder.raw_decode(text, cursor)
            results.append(result)
        return results


def test_overlapping_generation_results_keep_their_command_sink(monkeypatch) -> None:
    entered_first = threading.Event()
    second_entered = threading.Event()
    sink = _OrderedSink()
    original_stdout = cli.sys.stdout
    errors: list[BaseException] = []

    def generate(*, prompt: str, **_kwargs):
        if prompt == "first":
            entered_first.set()
            assert second_entered.wait(5)
            assert sink.second_written.wait(5)
        else:
            second_entered.set()
        return SimpleNamespace(ok=True, to_dict=lambda: {"marker": prompt})

    monkeypatch.setattr(cli, "_generate_facade", lambda: SimpleNamespace(image=generate))

    def invoke(prompt: str) -> None:
        try:
            assert cli.main(["image", prompt, "--dry-run", "--json", "--timings"]) == 0
        except BaseException as exc:  # noqa: BLE001 - propagate worker failures to the test thread.
            errors.append(exc)
        finally:
            if prompt == "first":
                sink.first_returned.set()

    with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(io.StringIO()):
        first = threading.Thread(target=invoke, args=("first",))
        second = threading.Thread(target=invoke, args=("second",))
        first.start()
        if not entered_first.wait(5):
            first.join(5)
            assert not errors, repr(errors)
            raise AssertionError("first generation did not reach the facade")
        second.start()
        first.join(5)
        second.join(5)
        assert not first.is_alive() and not second.is_alive()
        assert cli.sys.stdout is sink
        assert cli.main(["image", "third", "--dry-run", "--json"]) == 0

    assert not errors
    results = {row["marker"]: row for row in sink.read_results()}
    assert set(results) == {"first", "second", "third"}
    assert results["first"]["timings"]
    assert results["second"]["timings"]
    assert "timings" not in results["third"]
    assert cli.sys.stdout is original_stdout


def test_overlapping_timeline_results_keep_their_gateway_sink(connected, monkeypatch) -> None:
    from contextlib import nullcontext

    from astrid.core.gateway import dispatch
    from astrid.sdk.client import AstridClient
    from astrid.sdk.contracts import DomainResult

    client, _generated, _path = connected
    entered_first = threading.Event()
    second_entered = threading.Event()
    sink = _OrderedSink()
    original_stdout = sys.stdout
    errors: list[BaseException] = []

    def open_composition(_project: str, timeline: str, **_kwargs):
        if timeline == "first":
            entered_first.set()
            assert second_entered.wait(5)
            assert sink.second_written.wait(5)
        elif timeline == "second":
            second_entered.set()
        return DomainResult.success({"marker": timeline})

    client.timelines.open_composition = open_composition
    monkeypatch.setattr(
        AstridClient, "open_from_launcher", lambda **_kwargs: nullcontext(client)
    )

    def invoke(timeline: str) -> None:
        try:
            assert dispatch._dispatch_product(
                ["timelines", "show", timeline, "--project", "p", "--json", "--timings"]
            ) == 0
        except BaseException as exc:  # Propagate worker assertion failures in the test thread.
            errors.append(exc)
        finally:
            if timeline == "first":
                sink.first_returned.set()

    with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(io.StringIO()):
        first = threading.Thread(target=invoke, args=("first",))
        second = threading.Thread(target=invoke, args=("second",))
        first.start()
        if not entered_first.wait(5):
            first.join(5)
            assert not errors, repr(errors)
            raise AssertionError("first timeline command did not reach its reader")
        second.start()
        first.join(5)
        second.join(5)
        assert not first.is_alive() and not second.is_alive()
        assert sys.stdout is sink
        assert dispatch._dispatch_product(
            ["timelines", "show", "third", "--project", "p", "--json"]
        ) == 0

    assert not errors
    results = {row["data"]["marker"]: row for row in sink.read_results()}
    assert set(results) == {"first", "second", "third"}
    assert results["first"]["timings"]
    assert results["second"]["timings"]
    assert "timings" not in results["third"]
    assert sys.stdout is original_stdout
