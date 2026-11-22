from __future__ import annotations

import argparse

import pytest

from astrid import mcp_server


def test_generation_timings_are_not_an_mcp_input_but_other_families_keep_them():
    from astrid.core.generation.cli import _parser as generation_parser

    generation_tools = mcp_server._tools_for_parsers({"generate": generation_parser()})
    assert generation_tools
    for tool in generation_tools.values():
        schema = tool["definition"]["inputSchema"]
        assert "timings" not in schema["properties"]
        assert all(argument["dest"] != "timings" for argument in tool["args"])
        with pytest.raises(ValueError, match="unknown arguments: timings"):
            mcp_server.argv_for(tool, {"timings": True})

    synthetic = argparse.ArgumentParser(prog="reports")
    synthetic.add_argument("--timings", action="store_true", help="Print command stage timings.")
    synthetic.add_argument("--json", action="store_true")
    report_tool = mcp_server._tools_for_parsers({"reports": synthetic})["reports"]

    assert report_tool["definition"]["inputSchema"]["properties"]["timings"] == {
        "type": "boolean", "description": "Print command stage timings."
    }
    assert mcp_server.argv_for(report_tool, {"timings": True}) == [
        "reports", "--timings", "--json"
    ]
