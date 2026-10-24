"""`python -m astrid.mcp_server`: Astrid's product commands as an MCP server (stdio).

Any MCP client (Claude Code, Codex, Claude Desktop, ...) can drive Astrid
without its own shell: each `astrid-tools` command becomes one MCP tool, built
from the same argparse parsers that `scripts/gen_cli_surface.py` walks, so the
tools follow the CLI. Pack skills (SKILL.md) are served as MCP prompts.

Each tool call runs `astrid-tools <family> <verb...> ... --json` and returns its
JSON envelope. Tools are annotated read-only or mutating so clients can ask
before changes. Preference writes are not exposed: preferences are added to
every agent's system prompt, so an outside agent must not be able to set them.

Transport: newline-delimited JSON-RPC 2.0 on stdin/stdout (MCP stdio). Logs go
to stderr. Standard library only.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
EXCLUDED = {("preferences", "checkin"), ("preferences", "edit")}
READ_ONLY_VERBS = {
    "assets", "check", "context", "current", "diff", "elements", "events", "find", "follow",
    "history", "inspect", "lines", "lint", "list", "log", "show", "status", "verify",
    "visualize", "words",
}
DESTRUCTIVE_VERBS = {"archive", "cancel", "discard", "remove"}
TIMEOUT = float(os.environ.get("ASTRID_MCP_TIMEOUT", "900"))


def log(message: str) -> None:
    print(f"astrid-mcp: {message}", file=sys.stderr, flush=True)


# --- tools from the CLI parsers ---------------------------------------------

def family_parsers() -> dict[str, argparse.ArgumentParser]:
    from astrid.core.cli.domain_product import FAMILY_PARSER_MODULES, PRODUCT_FAMILIES

    parsers = {}
    for family in sorted(PRODUCT_FAMILIES):
        module = importlib.import_module(FAMILY_PARSER_MODULES[family])
        kwargs = {}
        if family == "media":
            kwargs["reference_commands"] = importlib.import_module(FAMILY_PARSER_MODULES["references"]).COMMANDS
        parsers[family] = module.build_parser(None, **kwargs)
    return parsers


def _leaves(parser: argparse.ArgumentParser, path: tuple[str, ...], summary: str):
    subs = [a for a in parser._actions if isinstance(a, argparse._SubParsersAction)]
    if not subs:
        yield path, parser, summary
        return
    for action in subs:
        helps = {choice.dest: choice.help or "" for choice in action._choices_actions}
        for name, sub in action.choices.items():
            yield from _leaves(sub, (*path, name), helps.get(name, ""))


def _schema(action: argparse.Action) -> dict[str, Any]:
    if isinstance(action, (argparse._StoreTrueAction, argparse._StoreFalseAction)):
        return {"type": "boolean"}
    if isinstance(action, argparse._CountAction):
        return {"type": "integer", "minimum": 0}
    item: dict[str, Any] = {"type": "string"}
    if action.type is int:
        item = {"type": "integer"}
    elif action.type is float:
        item = {"type": "number"}
    if action.choices is not None:
        item["enum"] = [c for c in action.choices]
    if isinstance(action, argparse._AppendAction) or action.nargs in ("*", "+") or isinstance(action.nargs, int) and action.nargs > 1:
        return {"type": "array", "items": item}
    return item


def _tool_name(path: tuple[str, ...]) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", "_".join(path))[:64]


def build_tools() -> dict[str, dict[str, Any]]:
    tools: dict[str, dict[str, Any]] = {}
    for family, parser in family_parsers().items():
        for path, leaf, summary in _leaves(parser, (family,), ""):
            if path[:2] in EXCLUDED:
                continue
            properties: dict[str, Any] = {}
            required: list[str] = []
            args: list[dict[str, Any]] = []
            supports_json = False
            for action in leaf._actions:
                if isinstance(action, argparse._HelpAction):
                    continue
                if "--json" in action.option_strings:
                    supports_json = True
                    continue
                schema = _schema(action)
                if action.help and action.help != argparse.SUPPRESS:
                    try:
                        schema["description"] = action.help % {"default": action.default, "prog": "astrid"}
                    except (KeyError, TypeError, ValueError):
                        schema["description"] = action.help
                properties[action.dest] = schema
                if action.required or (not action.option_strings and action.nargs not in ("?", "*")):
                    required.append(action.dest)
                args.append({"dest": action.dest, "option": action.option_strings[0] if action.option_strings else None,
                             "kind": type(action).__name__, "nargs": action.nargs})
            verb = path[-1]
            read_only = verb in READ_ONLY_VERBS
            description = summary or leaf.description or f"astrid-tools {' '.join(path)}"
            tools[_tool_name(path)] = {
                "definition": {
                    "name": _tool_name(path),
                    "title": "astrid " + " ".join(path),
                    "description": description.strip(),
                    "inputSchema": {"type": "object", "properties": properties, "required": required,
                                    "additionalProperties": False},
                    "annotations": {"readOnlyHint": read_only,
                                    "destructiveHint": (not read_only) and verb in DESTRUCTIVE_VERBS,
                                    "openWorldHint": False},
                },
                "path": path, "args": args, "json": supports_json,
            }
    return tools


def argv_for(tool: dict[str, Any], arguments: dict[str, Any]) -> list[str]:
    unknown = set(arguments) - {a["dest"] for a in tool["args"]}
    if unknown:
        raise ValueError(f"unknown arguments: {', '.join(sorted(unknown))}")
    argv = list(tool["path"])
    flags: list[str] = []
    for spec in tool["args"]:
        if spec["dest"] not in arguments or arguments[spec["dest"]] is None:
            continue
        value = arguments[spec["dest"]]
        option, kind = spec["option"], spec["kind"]
        if option is None:
            argv += [str(v) for v in value] if isinstance(value, list) else [str(value)]
        elif kind in ("_StoreTrueAction", "_StoreFalseAction"):
            if value:
                flags.append(option)
        elif kind == "_CountAction":
            flags += [option] * int(value)
        elif kind == "_AppendAction":
            for item in value if isinstance(value, list) else [value]:
                flags += [option, str(item)]
        elif isinstance(value, list):
            flags += [option, *[str(v) for v in value]]
        else:
            flags += [option, str(value)]
    return argv + flags + (["--json"] if tool["json"] else [])


def _astrid_env() -> dict[str, str]:
    env = dict(os.environ)
    if "ASTRID_RUNTIME_DATA_ROOT" not in env:
        # Installed layout: versions/<id>/py is this interpreter's prefix.
        record = Path(sys.prefix).parent / "version.json"
        if record.is_file():
            env.update({str(k): str(v) for k, v in json.loads(record.read_text(encoding="utf-8"))["env"].items()})
    return env


def call_tool(tool: dict[str, Any], arguments: dict[str, Any]) -> dict[str, Any]:
    try:
        argv = argv_for(tool, arguments)
    except (TypeError, ValueError) as exc:
        return {"content": [{"type": "text", "text": f"invalid arguments: {exc}"}], "isError": True}
    executable = Path(sys.executable).with_name("astrid-tools")
    command = [str(executable)] if executable.exists() else [sys.executable, "-m", "astrid.core.gateway"]
    try:
        done = subprocess.run([*command, *argv], capture_output=True, text=True, timeout=TIMEOUT,
                              stdin=subprocess.DEVNULL, env=_astrid_env())
    except subprocess.TimeoutExpired:
        return {"content": [{"type": "text", "text": f"astrid {' '.join(argv)} timed out after {TIMEOUT:.0f}s"}],
                "isError": True}
    text = done.stdout.strip() or done.stderr.strip()
    result: dict[str, Any] = {"content": [{"type": "text", "text": text or f"exit status {done.returncode}"}],
                              "isError": done.returncode != 0}
    try:
        envelope = json.loads(done.stdout)
    except ValueError:
        envelope = None
    if isinstance(envelope, dict):
        result["structuredContent"] = envelope
        result["isError"] = result["isError"] or envelope.get("ok") is False
    if done.returncode != 0 and done.stderr.strip() and done.stdout.strip():
        result["content"].append({"type": "text", "text": done.stderr.strip()[-4000:]})
    return result


# --- prompts from pack skills -----------------------------------------------

def _frontmatter(text: str) -> tuple[dict[str, str], str]:
    if not text.startswith("---\n"):
        return {}, text
    head, _, body = text[4:].partition("\n---\n")
    meta: dict[str, str] = {}
    key = None
    for line in head.splitlines():
        match = re.match(r"^([A-Za-z_-]+):\s*(.*)$", line)
        if match:
            key, value = match[1], match[2].strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            meta[key] = "" if value in (">", "|", ">-", "|-") else value
        elif key and line.startswith((" ", "\t")):
            meta[key] = (meta[key] + " " + line.strip()).strip()
    return meta, body


def build_prompts() -> dict[str, dict[str, Any]]:
    import astrid

    packs = Path(astrid.__file__).resolve().parent / "packs"
    prompts: dict[str, dict[str, Any]] = {}
    for path in sorted(packs.rglob("SKILL.md")):
        if any(part in {"tests", "fixtures"} for part in path.parts):
            continue
        meta, body = _frontmatter(path.read_text(encoding="utf-8"))
        name = re.sub(r"[^A-Za-z0-9_-]+", "-", meta.get("name") or "").strip("-")
        if not name or name in prompts:
            name = re.sub(r"[^A-Za-z0-9_-]", "-", "-".join(path.relative_to(packs).parts[:-1]))
        prompts[name] = {"definition": {"name": name, "title": f"Astrid skill: {name}",
                                        "description": meta.get("description", "")[:1000], "arguments": []},
                         "text": body.strip()}
    return prompts


# --- MCP over stdio -----------------------------------------------------------

def handle(message: dict[str, Any], state: dict[str, Any]) -> dict[str, Any] | None:
    method, params, ident = message.get("method"), message.get("params") or {}, message.get("id")
    if ident is None:  # notifications (initialized, cancelled) need no reply
        return None
    def ok(result: dict[str, Any]) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": ident, "result": result}
    def error(code: int, text: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": ident, "error": {"code": code, "message": text}}
    if method == "initialize":
        asked = params.get("protocolVersion")
        return ok({
            "protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
            "capabilities": {"tools": {"listChanged": False}, "prompts": {"listChanged": False}},
            "serverInfo": {"name": "astrid", "title": "Astrid", "version": "0.1.0"},
            "instructions": ("Astrid's product commands (projects, documents, media, tasks, runs, timelines, "
                             "preferences) as tools; each returns Astrid's JSON envelope. Load the matching "
                             "Astrid skill prompt (e.g. creative-work, vibecomfy) before multi-step work."),
        })
    if method == "ping":
        return ok({})
    if method == "tools/list":
        return ok({"tools": [t["definition"] for t in state["tools"].values()]})
    if method == "tools/call":
        tool = state["tools"].get(params.get("name"))
        if tool is None:
            return error(-32602, f"unknown tool {params.get('name')!r}")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            return error(-32602, "arguments must be an object")
        return ok(call_tool(tool, arguments))
    if method == "prompts/list":
        return ok({"prompts": [p["definition"] for p in state["prompts"].values()]})
    if method == "prompts/get":
        prompt = state["prompts"].get(params.get("name"))
        if prompt is None:
            return error(-32602, f"unknown prompt {params.get('name')!r}")
        return ok({"description": prompt["definition"]["description"],
                   "messages": [{"role": "user", "content": {"type": "text", "text": prompt["text"]}}]})
    return error(-32601, f"method not found: {method}")


def main() -> int:
    state = {"tools": build_tools(), "prompts": build_prompts()}
    log(f"ready: {len(state['tools'])} tools, {len(state['prompts'])} prompts")
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            message = json.loads(line)
        except ValueError:
            reply: dict[str, Any] | None = {"jsonrpc": "2.0", "id": None,
                                            "error": {"code": -32700, "message": "parse error"}}
        else:
            try:
                reply = handle(message, state) if isinstance(message, dict) else None
            except Exception as exc:  # one bad request must not end the session
                log(f"error handling {message.get('method')}: {exc!r}")
                reply = {"jsonrpc": "2.0", "id": message.get("id"),
                         "error": {"code": -32603, "message": str(exc)}}
        if reply is not None:
            sys.stdout.write(json.dumps(reply) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
