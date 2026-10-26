"""`python -m astrid.mcp_server`: Astrid's commands as an MCP server (stdio).

Any MCP client (Claude Code, Codex, Claude Desktop, ...) can drive Astrid
without its own shell: CLI commands become MCP tools, built from the same
argparse parsers that `scripts/gen_cli_surface.py` walks, so the tools follow
the CLI. Pack skills (SKILL.md) are served as MCP prompts.

Each product tool call runs `astrid-tools <family> <verb...> ... --json` and
returns its JSON envelope. Pack tool calls run the pack's Python command.
JSON-valued flags take real JSON; file arguments take the file's text (the
server writes and reads private temporary files), so a client that cannot see
Astrid's filesystem, for example one outside a sandbox, can use them. The
hand-written `invoke` tool admits a capability run, like Astrid's own agent
tool. Tool definitions omit read-only and mutating annotations. Preference
writes are not exposed: preferences are added to every agent's system prompt,
so an outside agent must not be able to set them.

Transport: newline-delimited JSON-RPC 2.0 on stdin/stdout (MCP stdio). Logs go
to stderr. Standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from astrid.core.cli.argtypes import (
    execution_request,
    input_file,
    json_array,
    json_object,
    json_object_or_path,
    output_file,
)
from astrid.core.cli.domain_product import family_parsers
from astrid.packs.vibecomfy import local as vibecomfy_local

PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
EXCLUDED = {
    ("preferences", "checkin"),
    ("preferences", "edit"),
    ("tasks", "follow"),
    ("comfy", "pack-images"),
}
TIMEOUT = float(os.environ.get("ASTRID_MCP_TIMEOUT", "900"))
TOOL_SOURCES = {
    "comfy": (
        vibecomfy_local.build_parser,
        (sys.executable, "-m", "astrid.packs.vibecomfy.local"),
    ),
}


def log(message: str) -> None:
    print(f"astrid-mcp: {message}", file=sys.stderr, flush=True)


# --- tools from the CLI parsers ---------------------------------------------

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
    kind = _value_kind(action)
    if kind == "json-object":
        return {"type": "object"}
    if kind == "json-array":
        return {"type": "array"}
    if kind == "json-object-or-path":
        return {"type": ["object", "array"]}
    if kind in {"input-file", "output-file"}:
        return {"type": "string"}
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


def _value_kind(action: argparse.Action) -> str:
    """How a value travels, based on the parser's declared argument type."""
    if action.type is json_array:
        return "json-array"
    if action.type is json_object or action.type is execution_request:
        return "json-object"
    if action.type is json_object_or_path:
        return "json-object-or-path"
    if action.type is input_file:
        return "input-file"
    if action.type is output_file:
        return "output-file"
    return "plain"


def _tool_name(path: tuple[str, ...]) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", "_".join(path))[:64]


def build_tools() -> dict[str, dict[str, Any]]:
    tools: dict[str, dict[str, Any]] = {}
    sources = {
        family: (lambda parser=parser: parser, ("astrid-tools", family))
        for family, parser in family_parsers().items()
    }
    sources.update(TOOL_SOURCES)
    for family, (build_parser, command) in sources.items():
        for path, leaf, summary in _leaves(build_parser(), (family,), ""):
            if path[:2] in EXCLUDED:
                continue
            properties: dict[str, Any] = {}
            required: list[str] = []
            args: list[dict[str, Any]] = []
            supports_json = False
            for action in leaf._actions:
                if isinstance(action, argparse._HelpAction):
                    continue
                if path == ("timelines", "render") and action.dest == "wait":
                    continue
                if "--json" in action.option_strings:
                    supports_json = True
                    continue
                value_kind = _value_kind(action)
                output_argument = value_kind == "output-file"
                args.append({"dest": action.dest, "option": action.option_strings[0] if action.option_strings else None,
                             "kind": type(action).__name__, "nargs": action.nargs, "value": value_kind,
                             "output": output_argument})
                if output_argument:
                    continue  # the server picks the path and returns the file's text
                schema = _schema(action)
                if action.help and action.help != argparse.SUPPRESS:
                    try:
                        schema["description"] = action.help % {"default": action.default, "prog": "astrid"}
                    except (KeyError, TypeError, ValueError):
                        schema["description"] = action.help
                if value_kind == "input-file":
                    schema["description"] = "The file's text (written to a private temporary file for Astrid)."
                # Paired flags share a dest (--wait/--detach): the first one names it.
                properties.setdefault(action.dest, schema)
                if action.required or (not action.option_strings and action.nargs not in ("?", "*")):
                    required.append(action.dest)
            description = summary or leaf.description or f"astrid-tools {' '.join(path)}"
            tools[_tool_name(path)] = {
                "definition": {
                    "name": _tool_name(path),
                    "title": "astrid " + " ".join(path),
                    "description": description.strip(),
                    "inputSchema": {"type": "object", "properties": properties, "required": required,
                                    "additionalProperties": False},
                },
                "kind": "cli", "path": path[1:], "family_path": path,
                "command": command, "args": args, "json": supports_json,
            }
    return tools


def argv_for(tool: dict[str, Any], arguments: dict[str, Any], workdir: Path | None = None,
             outputs: dict[str, Path] | None = None) -> list[str]:
    """CLI argv for a tool call. File inputs are written to, and outputs reserved in,
    ``workdir``; ``outputs`` collects files to read back."""
    unknown = set(arguments) - {a["dest"] for a in tool["args"] if not a.get("output")}
    if unknown:
        raise ValueError(f"unknown arguments: {', '.join(sorted(unknown))}")
    path = tool.get("family_path", tool["path"])
    argv = list(tool["path"])
    flags: list[str] = []
    for spec in tool["args"]:
        if path == ("timelines", "render") and spec["dest"] == "wait":
            continue
        if spec.get("output"):
            if workdir is None:
                raise ValueError("file output needs a working directory")
            target = workdir / f"{spec['dest']}.out"
            if outputs is not None:
                outputs[spec["dest"]] = target
            value = str(target)
        elif spec["dest"] not in arguments or arguments[spec["dest"]] is None:
            continue
        else:
            value = arguments[spec["dest"]]
        if spec.get("value") == "input-file":
            if workdir is None:
                raise ValueError("file argument needs a working directory")
            if not isinstance(value, str):
                raise TypeError(f"{spec['dest']} must be the file's text")
            target = workdir / f"{spec['dest']}.in"
            target.write_text(value, encoding="utf-8")
            value = str(target)
        elif spec.get("value") == "json-object-or-path":
            if isinstance(value, (dict, list)):
                value = json.dumps(value)
            elif isinstance(value, str):
                try:
                    parsed = json.loads(value)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"{spec['dest']} must be JSON; file paths are not accepted") from exc
                if not isinstance(parsed, (dict, list)):
                    raise ValueError(f"{spec['dest']} must be a JSON object or array")
            else:
                raise TypeError(f"{spec['dest']} must be a JSON object or array")
        elif spec.get("value", "").startswith("json") and not isinstance(value, str):
            value = json.dumps(value)
        option, kind = spec["option"], spec["kind"]
        if option is None:
            argv += [str(v) for v in value] if isinstance(value, list) else [str(value)]
        elif kind == "_StoreTrueAction":
            if value:
                flags.append(option)
        elif kind == "_StoreFalseAction":
            # The dest's value is what argparse stores: false selects this flag.
            if value is False:
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
    if path == ("timelines", "render"):
        flags.append("--detach")
    if path == ("timelines", "edit") and "--no-sweep" not in flags:
        flags.append("--no-sweep")
    return argv + flags + (["--json"] if tool["json"] else [])


def _astrid_env() -> dict[str, str]:
    env = dict(os.environ)
    if "ASTRID_RUNTIME_DATA_ROOT" not in env:
        # Installed layout: versions/<id>/py is this interpreter's prefix.
        record = Path(sys.prefix).parent / "version.json"
        if record.is_file():
            env.update({str(k): str(v) for k, v in json.loads(record.read_text(encoding="utf-8"))["env"].items()})
    return env


def _text(text: str, *, error: bool = False, structured: Any = None) -> dict[str, Any]:
    result: dict[str, Any] = {"content": [{"type": "text", "text": text}], "isError": error}
    if structured is not None:
        result["structuredContent"] = structured
    return result


def _run(argv: list[str], cwd: Path) -> subprocess.CompletedProcess | dict[str, Any]:
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=TIMEOUT, cwd=cwd,
                              stdin=subprocess.DEVNULL, env=_astrid_env())
    except subprocess.TimeoutExpired:
        return _text(f"{' '.join(argv[-4:])} timed out after {TIMEOUT:.0f}s", error=True)


def call_tool(tool: dict[str, Any], arguments: dict[str, Any], state: dict[str, Any] | None = None) -> dict[str, Any]:
    kind = tool.get("kind", "cli")
    if kind == "skills_list":
        skills = [{"name": p["definition"]["name"], "description": p["definition"]["description"]}
                  for p in (state or {}).get("prompts", {}).values()]
        return _text(json.dumps(skills, indent=1), structured={"skills": skills})
    if kind == "skill_get":
        prompt = (state or {}).get("prompts", {}).get(arguments.get("name"))
        if prompt is None:
            return _text(f"unknown skill {arguments.get('name')!r}; see skills_list", error=True)
        return _text(prompt["text"])
    with tempfile.TemporaryDirectory(prefix="astrid-mcp-") as scratch:
        workdir = Path(scratch)
        if kind == "invoke":
            return _invoke(arguments, workdir)
        outputs: dict[str, Path] = {}
        try:
            argv = argv_for(tool, arguments, workdir, outputs)
        except (TypeError, ValueError) as exc:
            return _text(f"invalid arguments: {exc}", error=True)
        command = list(tool["command"])
        if command[0] == "astrid-tools":
            executable = Path(sys.executable).with_name("astrid-tools")
            command = [str(executable)] if executable.exists() else [sys.executable, "-m", "astrid.core.gateway"]
            command.extend(tool["command"][1:])
        done = _run([*command, *argv], Path.cwd())
        if isinstance(done, dict):
            return done
        result = _cli_result(done)
        files = {dest: path.read_text(encoding="utf-8") for dest, path in outputs.items() if path.is_file()}
        for dest, text in files.items():
            result["content"].append({"type": "text", "text": f"{dest} contents:\n{text}"})
        if files:
            # Clients that show structuredContent (Claude Code) drop the extra text blocks.
            result["structuredContent"] = {**result.get("structuredContent", {}), "files": files}
        return result


def _invoke(arguments: dict[str, Any], workdir: Path) -> dict[str, Any]:
    """Admit one capability through astrid.omp_invoke, Astrid's own agent tool boundary."""
    request = {key: arguments[key] for key in ("capability_id", "kind", "project", "inputs", "execution_request")
               if arguments.get(key) is not None}
    request.setdefault("kind", "executor")
    done = _run([sys.executable, "-m", "astrid.omp_invoke", json.dumps(request)], Path.cwd())
    if isinstance(done, dict):
        return done
    try:
        reply = json.loads(done.stdout.strip().splitlines()[-1])
        payload = json.loads(reply["content"][0]["text"])
    except (IndexError, KeyError, TypeError, ValueError):
        return _cli_result(done)
    if not isinstance(payload, dict):
        payload = {"ok": False, "error": payload}
    structured = {**payload, "task_refs": reply.get("details", {}).get("astridTaskRefs", [])}
    return _text(json.dumps(structured, default=str)[:20000], error=done.returncode != 0 or payload.get("ok") is False,
                 structured=structured)


def _cli_result(done: subprocess.CompletedProcess) -> dict[str, Any]:
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


def _extra_tools() -> dict[str, dict[str, Any]]:
    def tool(name, description, properties, required, **extra):
        return name, {"definition": {
            "name": name, "title": "astrid " + name.replace("_", " "), "description": description,
            "inputSchema": {"type": "object", "properties": properties, "required": required,
                            "additionalProperties": False},
        }, **extra}
    return dict([
        tool("skills_list", "List Astrid's pack skills (name and when to use each).", {}, [],
             kind="skills_list"),
        tool("skill_get", "Read one Astrid skill (SKILL.md) before multi-step work in its area.",
             {"name": {"type": "string", "description": "Skill name from skills_list."}}, ["name"],
             kind="skill_get"),
        tool("invoke",
             "Admit one capability run (an executor or orchestrator, e.g. vibecomfy.run or "
             "generation.generate_video), the way Astrid's own agent does. Returns after admission "
             "with the task id; poll it with tasks_show. Render runs detached, so poll its task the same way. "
             "Read the capability's skill (skill_get) for its inputs first.",
             {"capability_id": {"type": "string", "description": "Capability id, e.g. vibecomfy.run."},
              "kind": {"type": "string", "enum": ["executor", "orchestrator"],
                       "description": "Capability kind (default executor)."},
              "project": {"type": "string", "description": "Project slug or ID."},
              "inputs": {"type": "object", "description": "The capability's inputs as a JSON object."},
              "execution_request": {"type": "object", "description": "Optional target-aware execution request."}},
             ["capability_id", "project"], kind="invoke"),
    ])


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
        return ok(call_tool(tool, arguments, state))
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
    state = {"tools": {**build_tools(), **_extra_tools()}, "prompts": build_prompts()}
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
            except Exception as exc:  # noqa: BLE001 - one bad request must not end the session
                log(f"error handling {message.get('method')}: {exc!r}")
                reply = {"jsonrpc": "2.0", "id": message.get("id"),
                         "error": {"code": -32603, "message": str(exc)}}
        if reply is not None:
            sys.stdout.write(json.dumps(reply) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
