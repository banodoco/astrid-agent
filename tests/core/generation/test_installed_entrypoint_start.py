"""Distinguish a generated console script from embedded calls."""
import runpy
import sys

from astrid import console


def test_console_script_and_embedded_calls_choose_their_own_start(tmp_path, monkeypatch):
    observed = []
    monkeypatch.setattr(
        console, "_run_console",
        lambda argv=None, *, start_provenance: observed.append((argv, start_provenance)) or 0,
    )
    script = tmp_path / "astrid"
    script.write_text("from astrid.console import main\nmain()\n", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", [str(script), "generate"])
    runpy.run_path(str(script), run_name="__main__")
    console.main()
    console.main(["generate"])
    assert observed == [
        (None, "process start"),
        (None, "command entry"),
        (["generate"], "command entry"),
    ]
