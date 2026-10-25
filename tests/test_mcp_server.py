from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

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


def _json_and_file_parser() -> argparse.ArgumentParser:
    def _parse_json_object(text):
        return json.loads(text)

    def _parse_json_array(text):
        return json.loads(text)

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers()
    create = sub.add_parser("create", help="Create a task.")
    create.add_argument("--spec", type=_parse_json_object, required=True)
    create.add_argument("--inputs", type=_parse_json_array)
    checkin = sub.add_parser("checkin", help="Check a file in.")
    checkin.add_argument("file", type=Path)
    checkout = sub.add_parser("checkout", help="Check a file out.")
    checkout.add_argument("--file", type=Path, required=True)
    return parser


def test_json_flags_take_real_json(monkeypatch):
    monkeypatch.setattr(mcp_server, "family_parsers", lambda: {"tasks": _json_and_file_parser()})
    tools = mcp_server.build_tools()
    schema = tools["tasks_create"]["definition"]["inputSchema"]["properties"]
    assert schema["spec"]["type"] == "object" and schema["inputs"]["type"] == "array"
    argv = mcp_server.argv_for(tools["tasks_create"], {"spec": {"size": 1}, "inputs": ["a"]})
    assert argv == ["tasks", "create", "--spec", '{"size": 1}', "--inputs", '["a"]']


def test_file_arguments_travel_as_text(monkeypatch, tmp_path):
    monkeypatch.setattr(mcp_server, "family_parsers", lambda: {"documents": _json_and_file_parser()})
    tools = mcp_server.build_tools()
    argv = mcp_server.argv_for(tools["documents_checkin"], {"file": "# notes\n"}, tmp_path)
    assert argv[:2] == ["documents", "checkin"] and Path(argv[2]).read_text() == "# notes\n"
    checkout = tools["documents_checkout"]
    assert "file" not in checkout["definition"]["inputSchema"]["properties"]
    outputs = {}
    argv = mcp_server.argv_for(checkout, {}, tmp_path, outputs)
    assert argv == ["documents", "checkout", "--file", str(tmp_path / "file.out")]
    assert outputs == {"file": tmp_path / "file.out"} and not outputs["file"].exists()


def test_skills_are_tools_too():
    state = {"prompts": {"vibecomfy": {"definition": {"name": "vibecomfy", "description": "ComfyUI"}, "text": "Body"}}}
    tools = mcp_server._extra_tools()
    listed = mcp_server.call_tool(tools["skills_list"], {}, state)
    assert listed["structuredContent"]["skills"] == [{"name": "vibecomfy", "description": "ComfyUI"}]
    assert mcp_server.call_tool(tools["skill_get"], {"name": "vibecomfy"}, state)["content"][0]["text"] == "Body"
    assert mcp_server.call_tool(tools["skill_get"], {"name": "nope"}, state)["isError"] is True


def test_timeline_document_tools_wrap_the_rendering_script(monkeypatch, tmp_path):
    calls = []

    def fake_run(argv, cwd):
        calls.append(argv)
        document = Path(argv[argv.index("--file") + 1])
        if argv[2] == "checkout":
            document.write_text('{"placements": []}')
        elif argv[2] == "check":
            document.with_suffix(".check.json").write_text('{"validation": {"ok": true}}')
        return subprocess.CompletedProcess(argv, 0, stdout='{"ok": true}', stderr="")

    monkeypatch.setattr(mcp_server, "_run", fake_run)
    monkeypatch.setattr(mcp_server, "_timeline_script", lambda: Path("/astrid/timeline_document.py"))
    tools = mcp_server._extra_tools()
    out = mcp_server.call_tool(tools["timeline_document_checkout"], {"project": "p", "timeline": "t"})
    assert calls[-1][2:4] == ["checkout", "--file"] and calls[-1][-4:] == ["--project", "p", "--timeline", "t"]
    # Clients such as Claude Code show only structuredContent when it is present.
    assert out["structuredContent"]["document"] == {"placements": []}
    checked = mcp_server.call_tool(tools["timeline_document_check"], {"document": {"placements": []}})
    assert checked["structuredContent"]["report"] == {"validation": {"ok": True}}
    mcp_server.call_tool(tools["timeline_document_publish"], {"document": "{}", "idempotency_key": "k1"})
    assert calls[-1][-2:] == ["--idempotency-key", "k1"]
    assert tools["timeline_document_publish"]["definition"]["annotations"]["readOnlyHint"] is False


def test_add_shot_places_a_new_empty_shot():
    from astrid.sdk.timeline_editing import add_authoring_shot  # noqa: F401  (real SDK helper)

    document = {"project_id": "p", "timeline_id": "t", "shots": {}, "placements": [],
                "source_mapping": {"shots": {}, "placements": {}}}
    result = mcp_server.call_tool(mcp_server._extra_tools()["timeline_document_add_shot"],
                                  {"document": document, "name": "Title", "duration_ms": 3000})
    assert result["isError"] is False
    work = result["structuredContent"]["document"]
    shot_id = result["structuredContent"]["shot_id"]
    assert work["shots"][shot_id]["payload"]["name"] == "Title"
    [row] = work["placements"]
    assert (row["shot_id"], row["duration_ms"], row["placement"]["start_ms"]) == (shot_id, 3000, 0)


def test_file_outputs_are_in_structured_content(monkeypatch, tmp_path):
    monkeypatch.setattr(mcp_server, "family_parsers", lambda: {"documents": _json_and_file_parser()})
    tool = mcp_server.build_tools()["documents_checkout"]

    def fake_run(argv, cwd):
        Path(argv[argv.index("--file") + 1]).write_text("# working copy")
        return subprocess.CompletedProcess(argv, 0, stdout='{"ok": true, "data": {}}', stderr="")

    monkeypatch.setattr(mcp_server, "_run", fake_run)
    result = mcp_server.call_tool(tool, {})
    assert result["structuredContent"]["files"] == {"file": "# working copy"}
    assert result["structuredContent"]["ok"] is True
