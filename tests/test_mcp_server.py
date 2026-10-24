from __future__ import annotations

import argparse
import json

from astrid import mcp_server


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="astrid things")
    sub = parser.add_subparsers(dest="verb")
    show = sub.add_parser("show", help="Show one thing.")
    show.add_argument("thing")
    show.add_argument("--project", required=True, help="Owning project.")
    show.add_argument("--limit", type=int, help="At most %(default)s rows.", default=5)
    show.add_argument("--tag", action="append")
    show.add_argument("--all", action="store_true")
    show.add_argument("--json", action="store_true")
    archive = sub.add_parser("archive", help="Archive things.")
    archive.add_argument("ids", nargs="+")
    return parser


def _tools(monkeypatch):
    monkeypatch.setattr(mcp_server, "family_parsers", lambda: {"things": _parser()})
    return mcp_server.build_tools()


def test_tools_mirror_the_parser(monkeypatch):
    tools = _tools(monkeypatch)
    assert sorted(tools) == ["things_archive", "things_show"]
    show = tools["things_show"]["definition"]
    assert show["description"] == "Show one thing."
    schema = show["inputSchema"]
    assert schema["required"] == ["thing", "project"]
    assert schema["properties"]["limit"] == {"type": "integer", "description": "At most 5 rows."}
    assert schema["properties"]["tag"]["type"] == "array"
    assert schema["properties"]["all"]["type"] == "boolean"
    assert "json" not in schema["properties"]
    assert show["annotations"] == {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False}
    archive = tools["things_archive"]["definition"]
    assert archive["annotations"]["readOnlyHint"] is False
    assert archive["annotations"]["destructiveHint"] is True


def test_argv_maps_arguments_and_rejects_unknown(monkeypatch):
    tools = _tools(monkeypatch)
    argv = mcp_server.argv_for(tools["things_show"], {"thing": "a", "project": "p", "tag": ["x", "y"], "all": True})
    assert argv == ["things", "show", "a", "--project", "p", "--tag", "x", "--tag", "y", "--all", "--json"]
    assert mcp_server.argv_for(tools["things_archive"], {"ids": ["1", "2"]}) == ["things", "archive", "1", "2"]
    try:
        mcp_server.argv_for(tools["things_show"], {"thing": "a", "rm": "-rf"})
    except ValueError as exc:
        assert "rm" in str(exc)
    else:
        raise AssertionError("unknown argument accepted")


def test_preference_writes_are_not_exposed(monkeypatch):
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers()
    for verb in ("checkin", "edit", "context"):
        sub.add_parser(verb)
    monkeypatch.setattr(mcp_server, "family_parsers", lambda: {"preferences": parser})
    assert sorted(mcp_server.build_tools()) == ["preferences_context"]


def test_protocol_handshake_and_errors(monkeypatch):
    state = {"tools": _tools(monkeypatch), "prompts": {}}
    init = mcp_server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                              "params": {"protocolVersion": "2025-06-18"}}, state)
    assert init["result"]["protocolVersion"] == "2025-06-18"
    assert mcp_server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}, state) is None
    listed = mcp_server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, state)
    assert len(listed["result"]["tools"]) == 2
    unknown = mcp_server.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "nope"}}, state)
    assert unknown["error"]["code"] == -32602
    assert mcp_server.handle({"jsonrpc": "2.0", "id": 4, "method": "nope"}, state)["error"]["code"] == -32601
    json.dumps(listed)


def test_frontmatter_handles_quotes_and_folded_values():
    meta, body = mcp_server._frontmatter('---\nname: "astrid"\ndescription: >\n  Two\n  lines.\n---\nBody\n')
    assert meta == {"name": "astrid", "description": "Two lines."}
    assert body == "Body\n"
