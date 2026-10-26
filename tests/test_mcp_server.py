from __future__ import annotations

import argparse
import io
import json
import socketserver
import subprocess
import urllib.request
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

from astrid import mcp_server
from astrid.core.cli.argtypes import (
    execution_request,
    input_file,
    json_array,
    json_object,
    json_object_or_path,
    output_file,
)


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
    assert "annotations" not in show
    archive = tools["things_archive"]["definition"]
    assert "annotations" not in archive


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
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers()
    create = sub.add_parser("create", help="Create a task.")
    create.add_argument("--spec", type=json_object, required=True)
    create.add_argument("--inputs", type=json_array)
    checkin = sub.add_parser("checkin", help="Check a file in.")
    checkin.add_argument("file", type=input_file)
    checkout = sub.add_parser("checkout", help="Check a file out.")
    checkout.add_argument("--file", type=output_file, required=True)
    return parser


def test_json_flags_take_real_json(monkeypatch):
    monkeypatch.setattr(mcp_server, "family_parsers", lambda: {"tasks": _json_and_file_parser()})
    tools = mcp_server.build_tools()
    schema = tools["tasks_create"]["definition"]["inputSchema"]["properties"]
    assert schema["spec"]["type"] == "object" and schema["inputs"]["type"] == "array"
    argv = mcp_server.argv_for(tools["tasks_create"], {"spec": {"size": 1}, "inputs": ["a"]})
    assert argv == ["tasks", "create", "--spec", '{"size": 1}', "--inputs", '["a"]']


def test_value_kinds_use_argtype_identity():
    parser = argparse.ArgumentParser()
    for name, value_type in (
        ("object", json_object),
        ("array", json_array),
        ("request", execution_request),
        ("input", input_file),
        ("output", output_file),
        ("dual", json_object_or_path),
    ):
        parser.add_argument(f"--{name}", type=value_type)
    assert [mcp_server._value_kind(action) for action in parser._actions[1:]] == [
        "json-object", "json-array", "json-object", "input-file", "output-file", "json-object-or-path",
    ]

    def json_object_named_but_unrelated(value):
        return value

    json_object_named_but_unrelated.__name__ = "json_object"
    unrelated = argparse.ArgumentParser()
    unrelated.add_argument("--value", type=json_object_named_but_unrelated)
    assert mcp_server._value_kind(unrelated._actions[-1]) == "plain"


def test_json_object_or_path_is_json_only_for_mcp(monkeypatch):
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers()
    visualize = sub.add_parser("visualize")
    visualize.add_argument("--rules", type=json_object_or_path)
    monkeypatch.setattr(mcp_server, "family_parsers", lambda: {"timelines": parser})
    tool = mcp_server.build_tools()["timelines_visualize"]
    assert tool["definition"]["inputSchema"]["properties"]["rules"]["type"] == ["object", "array"]
    assert mcp_server.argv_for(tool, {"rules": {"params": {"motion": 0.4}}}) == [
        "timelines", "visualize", "--rules", '{"params": {"motion": 0.4}}',
    ]
    encoded = '{"params": {"motion": 0.4}}'
    assert mcp_server.argv_for(tool, {"rules": encoded})[-1] == encoded
    result = mcp_server.call_tool(tool, {"rules": "/etc/passwd"})
    assert result["isError"] is True
    assert "must be JSON" in result["content"][0]["text"]


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


def test_timeline_document_verbs_are_generated_and_checkout_returns_file(monkeypatch):
    tools = mcp_server.build_tools()
    names = {f"timelines_document_{verb}" for verb in ("checkout", "add_shot", "bind_script", "check", "publish")}
    assert names <= tools.keys()
    assert not any(name.startswith("timeline_document_") for name in mcp_server._extra_tools())
    for name in names:
        assert tools[name]["kind"] == "cli"
    checkout = tools["timelines_document_checkout"]
    assert checkout["definition"]["inputSchema"]["required"] == ["project", "timeline"]
    check = tools["timelines_document_check"]
    assert check["definition"]["inputSchema"]["properties"]["file"]["type"] == "string"
    assert "report" not in check["definition"]["inputSchema"]["properties"]

    def fake_run(argv, cwd):
        start = argv.index("timelines")
        assert argv[start:start + 3] == ["timelines", "document", "checkout"]
        assert argv[-1] == "--json"
        Path(argv[argv.index("--file") + 1]).write_text('{"placements": []}')
        return subprocess.CompletedProcess(argv, 0, stdout='{"ok": true, "data": {}}', stderr="")

    monkeypatch.setattr(mcp_server, "_run", fake_run)
    result = mcp_server.call_tool(checkout, {"project": "p", "timeline": "t"})
    assert result["isError"] is False
    assert result["structuredContent"]["files"] == {"file": '{"placements": []}'}


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


def test_output_file_round_trip(monkeypatch):
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers()
    export = sub.add_parser("export")
    export.add_argument("--file", type=output_file, required=True)
    monkeypatch.setattr(mcp_server, "family_parsers", lambda: {"documents": parser})
    tool = mcp_server.build_tools()["documents_export"]
    assert "file" not in tool["definition"]["inputSchema"]["properties"]

    def fake_run(argv, cwd):
        Path(argv[argv.index("--file") + 1]).write_text("document contents")
        return subprocess.CompletedProcess(argv, 0, stdout='{"ok": true}', stderr="")

    monkeypatch.setattr(mcp_server, "_run", fake_run)
    result = mcp_server.call_tool(tool, {})
    assert result["isError"] is False
    assert result["structuredContent"]["files"] == {"file": "document contents"}


def test_every_tool_returns_without_following_long_running_work():
    tools = {**mcp_server.build_tools(), **mcp_server._extra_tools()}
    for name, tool in tools.items():
        assert "annotations" not in tool["definition"], name
        path = tool.get("path")
        assert path != ("tasks", "follow"), name
        if path == ("timelines", "render"):
            assert "wait" not in tool["definition"]["inputSchema"]["properties"]
            argv = mcp_server.argv_for(tool, {})
            assert "--wait" not in argv
            assert argv.count("--detach") == 1


def test_mcp_edit_forces_no_sweep_but_check_can_request_one():
    tools = mcp_server.build_tools()
    edit = mcp_server.argv_for(tools["timelines_edit"], {"ref": "film", "sweep": True})
    assert edit[-1] == "--no-sweep"
    assert edit.count("--no-sweep") == 1
    assert edit.index("--sweep") < edit.index("--no-sweep")

    check = mcp_server.argv_for(tools["timelines_check"], {"ref": "film", "sweep": True})
    assert "--sweep" in check and "--no-sweep" not in check


def test_cli_subprocesses_use_server_cwd_for_relative_paths(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    calls = []
    file_contents = []

    def fake_run(argv, cwd):
        calls.append((argv, cwd))
        if "media" in argv and argv[argv.index("media") + 1] == "import":
            file_contents.append(Path(argv[argv.index("media") + 2]).read_text())
        elif "timelines" in argv and argv[argv.index("timelines") + 1] == "find":
            file_contents.append(Path(argv[argv.index("--file") + 1]).read_text())
        elif "timelines" in argv and argv[argv.index("timelines") + 1] == "words":
            file_contents.append(Path(argv[argv.index("--file") + 1]).read_text())
        return subprocess.CompletedProcess(argv, 0, stdout='{"ok": true}', stderr="")

    monkeypatch.setattr(mcp_server, "_run", fake_run)
    tools = mcp_server.build_tools()
    examples = [
        ("media_import", {"project": "demo", "path": "media/clip.mp4"}),
        ("timelines_visualize", {"timeline_ref": "film", "out": "frames"}),
        ("timelines_edit", {"project": "demo", "ref": "film", "clip": "c1", "nudge": 1}),
        ("timelines_find", {"project": "demo", "ref": "film", "query": "word", "file": "edit.json"}),
        ("timelines_words", {"project": "demo", "ref": "film", "word": "word", "file": "words.json"}),
        ("timelines_lint", {"project": "demo", "ref": "film"}),
    ]
    for name, arguments in examples:
        result = mcp_server.call_tool(tools[name], arguments)
        assert result["isError"] is False, name

    assert [cwd for _, cwd in calls] == [tmp_path] * len(examples)
    assert file_contents == ["media/clip.mp4", "edit.json", "words.json"]
    assert calls[0][0][calls[0][0].index("media") + 2] != "media/clip.mp4"
    assert calls[1][0][calls[1][0].index("--out") + 1] == "frames"
    assert calls[3][0][calls[3][0].index("--file") + 1] != "edit.json"
    assert calls[4][0][calls[4][0].index("--file") + 1] != "words.json"


def test_module_docstring_does_not_claim_tool_annotations():
    assert "annotated read-only or mutating" not in (mcp_server.__doc__ or "")


def test_shots_and_timeline_file_arguments_take_text(tmp_path):
    tools = mcp_server.build_tools()
    text_tool = tools["timelines_shots_text_set"]
    sheet_tool = tools["timelines_apply"]

    assert text_tool["definition"]["inputSchema"]["properties"]["text_file"]["type"] == "string"
    assert sheet_tool["definition"]["inputSchema"]["properties"]["sheet_file"]["type"] == "string"
    text_argv = mcp_server.argv_for(text_tool, {"text_file": "exact narration\n"}, tmp_path)
    sheet_argv = mcp_server.argv_for(sheet_tool, {"sheet_file": "00:00.000\t0\n"}, tmp_path)
    assert Path(text_argv[text_argv.index("--text-file") + 1]).read_text() == "exact narration\n"
    assert Path(sheet_argv[-1]).read_text() == "00:00.000\t0\n"
    assert Path(text_argv[text_argv.index("--text-file") + 1]).is_absolute()
    assert Path(sheet_argv[-1]).is_absolute()


def test_invoke_returns_admission_and_task_refs(monkeypatch):
    calls = []

    def fake_run(argv, cwd):
        calls.append(argv)
        reply = {"content": [{"text": json.dumps({"ok": True, "task_id": "T1"})}],
                 "details": {"astridTaskRefs": [{"id": "T1"}]}}
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(reply), stderr="")

    monkeypatch.setattr(mcp_server, "_run", fake_run)
    tool = mcp_server._extra_tools()["invoke"]
    result = mcp_server.call_tool(tool, {"capability_id": "vibecomfy.run", "project": "demo",
                                         "inputs": {"workflow": "abc"}})
    request = json.loads(calls[0][-1])
    assert calls[0][1:3] == ["-m", "astrid.omp_invoke"] and calls[0][-1] == json.dumps(request)
    assert request == {"capability_id": "vibecomfy.run", "kind": "executor", "project": "demo",
                       "inputs": {"workflow": "abc"}}
    assert result["structuredContent"]["task_refs"] == [{"id": "T1"}]


def test_comfy_scan_runs_with_requested_ports(monkeypatch):
    calls = []

    def fake_run(argv, cwd):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout='{"ok": true}', stderr="")

    monkeypatch.setattr(mcp_server, "_run", fake_run)
    tool = mcp_server._extra_tools()["comfy_scan"]
    result = mcp_server.call_tool(tool, {"ports": "8189,8190"})
    assert calls[0][-3:] == ["scan", "--ports", "8189,8190"]
    assert result["structuredContent"]["ok"] is True


@contextmanager
def _workflow_server(handler_type):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_type)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


@contextmanager
def _malformed_http_server():
    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            self.request.recv(4096)
            self.request.sendall(b"not an HTTP status line\r\n\r\n")

    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def _main_tool_call(monkeypatch, capsys, name, arguments):
    monkeypatch.setattr(mcp_server, "build_tools", lambda: {})
    monkeypatch.setattr(mcp_server, "build_prompts", lambda: {})
    request = {"jsonrpc": "2.0", "id": 41, "method": "tools/call",
               "params": {"name": name, "arguments": arguments}}
    monkeypatch.setattr(mcp_server.sys, "stdin", io.StringIO(json.dumps(request) + "\n"))
    assert mcp_server.main() == 0
    [line] = capsys.readouterr().out.splitlines()
    return json.loads(line)


def test_comfy_workflow_import_rejects_nul_name_as_tool_error(monkeypatch, capsys):
    Handler, hits = _workflow_handler()
    with _workflow_server(Handler) as url:
        nul = _main_tool_call(monkeypatch, capsys, "comfy_workflow_import",
                              {"project": "demo", "name": "bad\x00.json", "comfy_url": url})
    assert "result" in nul and "error" not in nul
    assert nul["result"]["isError"] is True
    assert hits == []


def test_comfy_workflow_import_handles_non_http_reply_as_tool_error(monkeypatch, capsys):
    with _malformed_http_server() as url:
        malformed = _main_tool_call(monkeypatch, capsys, "comfy_workflow_import",
                                    {"project": "demo", "name": "portrait.json", "comfy_url": url})
    assert "result" in malformed and "error" not in malformed
    assert malformed["result"]["isError"] is True


def test_comfy_workflow_import_ignores_http_proxy_for_loopback(monkeypatch):
    tool = mcp_server._extra_tools()["comfy_workflow_import"]
    Target, target_hits = _workflow_handler()
    Proxy, proxy_hits = _workflow_handler()
    imported = []

    def fake_run(argv, cwd):
        imported.append(Path(argv[argv.index("import") + 1]).read_text())
        return subprocess.CompletedProcess(argv, 0, stdout='{"ok": true}', stderr="")

    monkeypatch.setattr(mcp_server, "_run", fake_run)
    with _workflow_server(Target) as target_url, _workflow_server(Proxy) as proxy_url:
        monkeypatch.setattr(urllib.request, "getproxies", lambda: {"http": proxy_url})
        monkeypatch.setattr(urllib.request, "proxy_bypass", lambda host: False)
        result = mcp_server.call_tool(tool, {"project": "demo", "name": "portrait.json", "comfy_url": target_url})

    assert result["isError"] is False
    assert target_hits == ["/userdata/workflows%2Fportrait.json"]
    assert proxy_hits == []
    assert imported == ['{"nodes": []}']


def _workflow_handler(*, redirect=False):
    hits = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            if redirect and self.path.endswith("portrait.json"):
                self.send_response(302)
                self.send_header("Location", "/followed")
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"nodes": []}')

        def log_message(self, format, *args):
            return None

    return Handler, hits


def test_comfy_workflow_import_checks_project_loopback_and_name(monkeypatch):
    tool = mcp_server._extra_tools()["comfy_workflow_import"]
    Handler, hits = _workflow_handler()
    with _workflow_server(Handler) as url:
        with monkeypatch.context() as patch:
            patch.setattr(urllib.request, "build_opener",
                          lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("loopback check was skipped")))
            refused = mcp_server.call_tool(
                tool, {"project": "demo", "name": "portrait.json", "comfy_url": "http://example.com"})
        assert refused["isError"] is True
        assert "comfy_url must be loopback" in refused["content"][0]["text"]

        for arguments in (
            {"name": "portrait.json"},
            {"project": "demo", "name": "portrait.json", "comfy_url": "http://[::1"},
            {"project": "demo", "name": "../portrait.json", "comfy_url": url},
            {"project": "demo", "name": ".hidden.json", "comfy_url": url},
            {"project": "demo", "name": "folder\\portrait.json", "comfy_url": url},
        ):
            result = mcp_server.call_tool(tool, arguments)
            assert result["isError"] is True
    assert hits == []


def test_comfy_workflow_import_reads_loopback_workflow_as_json(monkeypatch):
    tool = mcp_server._extra_tools()["comfy_workflow_import"]
    Handler, hits = _workflow_handler()
    imported = []

    def fake_run(argv, cwd):
        imported.append((argv, Path(argv[argv.index("import") + 1]).read_text()))
        return subprocess.CompletedProcess(argv, 0, stdout='{"ok": true}', stderr="")

    monkeypatch.setattr(mcp_server, "_run", fake_run)
    with _workflow_server(Handler) as url:
        result = mcp_server.call_tool(tool, {"project": "demo", "name": "portrait.json", "comfy_url": url})
    assert result["isError"] is False
    assert hits == ["/userdata/workflows%2Fportrait.json"]
    assert imported[0][1] == '{"nodes": []}'
    assert imported[0][0][-3:] == ["--project", "demo", "--json"]


def test_comfy_workflow_import_refuses_redirects(monkeypatch):
    tool = mcp_server._extra_tools()["comfy_workflow_import"]
    Handler, hits = _workflow_handler(redirect=True)
    monkeypatch.setattr(mcp_server, "_run", lambda *args: (_ for _ in ()).throw(AssertionError("redirect imported")))
    with _workflow_server(Handler) as url:
        result = mcp_server.call_tool(tool, {"project": "demo", "name": "portrait.json", "comfy_url": url})
    assert result["isError"] is True
    assert "could not read" in result["content"][0]["text"]
    assert hits == ["/userdata/workflows%2Fportrait.json"]


def test_stdio_loop_reports_malformed_json_and_unknown_method(monkeypatch, capsys):
    monkeypatch.setattr(mcp_server, "build_tools", lambda: {})
    monkeypatch.setattr(mcp_server, "_extra_tools", lambda: {})
    monkeypatch.setattr(mcp_server, "build_prompts", lambda: {})
    monkeypatch.setattr(mcp_server.sys, "stdin", io.StringIO(
        'not-json\n{"jsonrpc":"2.0","id":7,"method":"not-a-method"}\n'))

    assert mcp_server.main() == 0
    replies = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert replies[0]["error"]["code"] == -32700
    assert replies[1]["id"] == 7 and replies[1]["error"]["code"] == -32601
