"""Windows pack setup must enter the public installed launcher's pack parser."""

from contextlib import redirect_stderr, redirect_stdout
from importlib import import_module
import io
import json

import pytest

from astrid.core.gateway import main as gateway_main
from astrid.core.pack import cli_basic, cli_manage
from scripts import ci_windows_journey as journey


@pytest.mark.parametrize("present", [False, True], ids=["install-missing", "already-installed"])
def test_windows_pack_setup_reaches_public_pack_parser(monkeypatch, present):
    """Use the real entry point and argument parser, with pack I/O at its boundary."""
    observed = []

    def list_packs(args):
        assert args.json is True
        observed.append("list")
        print(json.dumps({"packs": [{"id": "chiptune"}] if present else []}))
        return 0

    def add_pack(args):
        observed.append("add")
        assert args.source == "https://github.com/banodoco/poms_packs"
        assert args.revision == "2379ab54aca522019ff4f869cfdf9375bdb56dfc"
        assert args.pack_id == args.subpath == "chiptune"
        assert args.yes is True
        return 0

    monkeypatch.setattr(cli_basic, "_handle_list", list_packs)
    monkeypatch.setattr(cli_manage, "_handle_add", add_pack)

    def cli(module, argv, name, *, timeout=300):
        # The old adapter reaches the genuine gateway refusal here. The
        # corrected adapter reaches the same module the installer launches.
        main = gateway_main if module == "astrid" else import_module(module).main
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(argv)
        if code:
            raise RuntimeError(f"{name}: exit {code}: {stderr.getvalue()}")
        return stdout.getvalue()

    monkeypatch.setattr(journey, "cli", cli)
    journey.install_journey_packs()
    assert observed == (["list"] if present else ["list", "add"])
