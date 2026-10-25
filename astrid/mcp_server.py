"""`python -m astrid.mcp_server`: Astrid's product commands as an MCP server (stdio).

Any MCP client (Claude Code, Codex, Claude Desktop, ...) can drive Astrid
without its own shell: each `astrid-tools` command becomes one MCP tool, built
from the same argparse parsers that `scripts/gen_cli_surface.py` walks, so the
tools follow the CLI. Pack skills (SKILL.md) are served as MCP prompts.

Each tool call runs `astrid-tools <family> <verb...> ... --json` and returns its
JSON envelope. JSON-valued flags take real JSON; file arguments take the file's
text (the server writes and reads private temporary files), so a client that
cannot see Astrid's filesystem, for example one outside a sandbox, can use them.
More tools serve what the CLI lacks: `invoke` (admit a capability run, like
Astrid's own agent tool), `skills_list`/`skill_get` (the pack
skills, also offered as MCP prompts, which only users can invoke) and the
`timeline_document_*` tools (the rendering pack's JSON timeline route, needed to
put the first clips on an empty timeline). Tools are annotated read-only or mutating so clients can ask
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
import tempfile
import uuid
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
    kind = _value_kind(action)
    if kind == "json-object":
        return {"type": "object"}
    if kind == "json-array":
        return {"type": "array"}
    if kind == "file":
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
    """How a value travels: plain, JSON, or a file whose text the server handles."""
    if action.type is Path:
        return "file"
    name = getattr(action.type, "__name__", "")
    if "json_array" in name:
        return "json-array"
    if "json" in name or name == "_parse_execution_request":
        return "json-object"
    return "plain"


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
                value_kind = _value_kind(action)
                output_file = value_kind == "file" and path[-1] == "checkout"
                args.append({"dest": action.dest, "option": action.option_strings[0] if action.option_strings else None,
                             "kind": type(action).__name__, "nargs": action.nargs, "value": value_kind,
                             "output": output_file})
                if output_file:
                    continue  # the server picks the path and returns the file's text
                schema = _schema(action)
                if action.help and action.help != argparse.SUPPRESS:
                    try:
                        schema["description"] = action.help % {"default": action.default, "prog": "astrid"}
                    except (KeyError, TypeError, ValueError):
                        schema["description"] = action.help
                if value_kind == "file":
                    schema["description"] = "The file's text (written to a private temporary file for Astrid)."
                # Paired flags share a dest (--wait/--detach): the first one names it.
                properties.setdefault(action.dest, schema)
                if action.required or (not action.option_strings and action.nargs not in ("?", "*")):
                    required.append(action.dest)
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
                "kind": "cli", "path": path, "args": args, "json": supports_json,
            }
    return tools


def argv_for(tool: dict[str, Any], arguments: dict[str, Any], workdir: Path | None = None,
             outputs: dict[str, Path] | None = None) -> list[str]:
    """CLI argv for a tool call. File arguments are written to (or, for checkout
    outputs, reserved in) ``workdir``; ``outputs`` collects files to read back."""
    unknown = set(arguments) - {a["dest"] for a in tool["args"] if not a.get("output")}
    if unknown:
        raise ValueError(f"unknown arguments: {', '.join(sorted(unknown))}")
    argv = list(tool["path"])
    flags: list[str] = []
    for spec in tool["args"]:
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
        if spec.get("value") == "file" and not spec.get("output"):
            if workdir is None:
                raise ValueError("file argument needs a working directory")
            if not isinstance(value, str):
                raise TypeError(f"{spec['dest']} must be the file's text")
            target = workdir / f"{spec['dest']}.in"
            target.write_text(value, encoding="utf-8")
            value = str(target)
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
        if kind == "comfy_scan":
            done = _run([sys.executable, "-m", "astrid.packs.vibecomfy.local", "scan",
                         "--ports", str(arguments.get("ports") or "8188,8000")], workdir)
            return done if isinstance(done, dict) else _cli_result(done)
        if kind == "comfy_workflow_import":
            return _comfy_workflow_import(arguments, workdir)
        if kind == "timeline_document":
            if tool["command"] == "add_shot":
                return _timeline_document_add_shot(arguments)
            return _timeline_document(tool["command"], arguments, workdir)
        outputs: dict[str, Path] = {}
        try:
            argv = argv_for(tool, arguments, workdir, outputs)
        except (TypeError, ValueError) as exc:
            return _text(f"invalid arguments: {exc}", error=True)
        executable = Path(sys.executable).with_name("astrid-tools")
        command = [str(executable)] if executable.exists() else [sys.executable, "-m", "astrid.core.gateway"]
        done = _run([*command, *argv], workdir)
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


def _comfy_workflow_import(arguments: dict[str, Any], workdir: Path) -> dict[str, Any]:
    """Read one saved workflow from the user's ComfyUI (GET only) and import it into the project.

    The vibecomfy skill's "fetch it read-only, then astrid media import" step, for a
    client that cannot reach Astrid's filesystem."""
    import urllib.error
    import urllib.parse
    import urllib.request

    url = str(arguments.get("comfy_url") or "http://127.0.0.1:8188").rstrip("/")
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "http" or parts.hostname not in ("127.0.0.1", "localhost") or parts.path not in ("", "/"):
        return _text("comfy_url must be loopback, e.g. http://127.0.0.1:8188", error=True)
    name = str(arguments.get("name") or "")
    if not name.endswith(".json") or "/" in name or "\\" in name or name.startswith("."):
        return _text("name must be a saved workflow file name such as portrait.json (see comfy_scan)", error=True)
    source = f"{url}/userdata/{urllib.parse.quote('workflows/' + name, safe='')}"
    try:
        with urllib.request.urlopen(source, timeout=30) as response:
            raw = response.read(64 * 1024 * 1024 + 1)
    except (OSError, urllib.error.URLError) as exc:
        return _text(f"could not read {source}: {exc}", error=True)
    if len(raw) > 64 * 1024 * 1024:
        return _text("workflow is larger than 64 MiB", error=True)
    try:
        json.loads(raw)
    except ValueError:
        return _text(f"{name} is not JSON", error=True)
    target = workdir / name
    target.write_bytes(raw)
    executable = Path(sys.executable).with_name("astrid-tools")
    command = [str(executable)] if executable.exists() else [sys.executable, "-m", "astrid.core.gateway"]
    done = _run([*command, "media", "import", str(target), "--project", str(arguments["project"]), "--json"], workdir)
    return done if isinstance(done, dict) else _cli_result(done)


def _invoke(arguments: dict[str, Any], workdir: Path) -> dict[str, Any]:
    """Admit one capability through astrid.omp_invoke, Astrid's own agent tool boundary."""
    request = {key: arguments[key] for key in ("capability_id", "kind", "project", "inputs", "execution_request")
               if arguments.get(key) is not None}
    request.setdefault("kind", "executor")
    done = _run([sys.executable, "-m", "astrid.omp_invoke", json.dumps(request)], workdir)
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


# --- timeline documents (the rendering pack's JSON route) ---------------------

def _timeline_script() -> Path:
    import astrid

    return Path(astrid.__file__).resolve().parent / "packs" / "rendering" / "docs" / "scripts" / "timeline_document.py"


def _timeline_document(command: str, arguments: dict[str, Any], workdir: Path) -> dict[str, Any]:
    document = workdir / "timeline.json"
    argv = [sys.executable, str(_timeline_script()), command, "--file", str(document)]
    try:
        if command == "checkout":
            argv += ["--project", str(arguments["project"]), "--timeline", str(arguments["timeline"])]
        else:
            body = arguments["document"]
            document.write_text(body if isinstance(body, str) else json.dumps(body, indent=2), encoding="utf-8")
            if command == "publish":
                argv += ["--idempotency-key", str(arguments["idempotency_key"])]
    except KeyError as exc:
        return _text(f"invalid arguments: missing {exc.args[0]}", error=True)
    done = _run(argv, workdir)
    if isinstance(done, dict):
        return done
    result = _cli_result(done)
    extra = {"checkout": ("document", document), "check": ("report", document.with_suffix(".check.json"))}.get(command)
    if done.returncode == 0 and extra is not None and extra[1].is_file():
        text = extra[1].read_text(encoding="utf-8")
        result["content"].append({"type": "text", "text": text})
        # Clients that show structuredContent (Claude Code) drop the extra text blocks.
        result["structuredContent"] = {"summary": result.get("structuredContent"), extra[0]: json.loads(text)}
    return result


def _timeline_document_add_shot(arguments: dict[str, Any]) -> dict[str, Any]:
    """Seed one new, empty shot (optionally placed) with the SDK's own helper; no Runtime call."""
    from astrid.sdk.timeline_editing import TimelineEditError, add_authoring_shot

    try:
        body = arguments["document"]
        work = json.loads(body) if isinstance(body, str) else body
        before = {row.get("shot_id") for row in work.get("placements", [])}
        # An occurrence ID is what makes the helper place the shot on the timeline.
        occurrence = arguments.get("occurrence_id") or f"occurrence-{uuid.uuid4().hex[:12]}"
        shot = add_authoring_shot(work, name=arguments.get("name"), shot_id=arguments.get("shot_id"),
                                  occurrence_id=occurrence, start_ms=arguments.get("start_ms", 0))
        shot_id = next((key for key, value in work["shots"].items() if value is shot), arguments.get("shot_id"))
        if "duration_ms" in arguments:
            for row in work.get("placements", []):
                if row.get("shot_id") == shot_id and row.get("shot_id") not in before:
                    row["duration_ms"] = arguments["duration_ms"]
    except (KeyError, TypeError, ValueError, TimelineEditError) as exc:
        return _text(f"could not add a shot: {exc}", error=True)
    summary = {"shot_id": shot_id, "placements": len(work.get("placements", [])),
               "next": "add tracks and clips to document['shots'][shot_id]['internal_timeline'], then check and publish"}
    return _text(json.dumps(summary), structured={**summary, "document": work})


def _extra_tools() -> dict[str, dict[str, Any]]:
    def tool(name, description, properties, required, read_only, **extra):
        return name, {"definition": {
            "name": name, "title": "astrid " + name.replace("_", " "), "description": description,
            "inputSchema": {"type": "object", "properties": properties, "required": required,
                            "additionalProperties": False},
            "annotations": {"readOnlyHint": read_only, "destructiveHint": False, "openWorldHint": False},
        }, **extra}
    document = {"type": ["object", "string"],
                "description": "The timeline document from timeline_document_checkout, edited (JSON)."}
    return dict([
        tool("skills_list", "List Astrid's pack skills (name and when to use each).", {}, [], True,
             kind="skills_list"),
        tool("skill_get", "Read one Astrid skill (SKILL.md) before multi-step work in its area.",
             {"name": {"type": "string", "description": "Skill name from skills_list."}}, ["name"], True,
             kind="skill_get"),
        tool("invoke",
             "Admit one capability run (an executor or orchestrator, e.g. vibecomfy.run or "
             "generation.generate_video), the way Astrid's own agent does. Returns at admission "
             "with the task id; follow it with tasks_follow or tasks_show, stop it with tasks_cancel. "
             "Read the capability's skill (skill_get) for its inputs first.",
             {"capability_id": {"type": "string", "description": "Capability id, e.g. vibecomfy.run."},
              "kind": {"type": "string", "enum": ["executor", "orchestrator"],
                       "description": "Capability kind (default executor)."},
              "project": {"type": "string", "description": "Project slug or ID."},
              "inputs": {"type": "object", "description": "The capability's inputs as a JSON object."},
              "execution_request": {"type": "object", "description": "Optional target-aware execution request."}},
             ["capability_id", "project"], False, kind="invoke"),
        tool("comfy_scan",
             "Find the user's running ComfyUI (vibecomfy local scan): URL, output folder, saved "
             "workflows, Manager, its Python. Read-only.",
             {"ports": {"type": "string", "description": "Comma-separated ports (default 8188,8000)."}},
             [], True, kind="comfy_scan"),
        tool("comfy_workflow_import",
             "Read one saved workflow from the user's ComfyUI (read-only GET of /userdata/workflows/NAME) "
             "and import it into the project; the result's digest is vibecomfy.run's workflow input.",
             {"project": {"type": "string", "description": "Project slug or ID."},
              "name": {"type": "string", "description": "Saved workflow file name from comfy_scan, e.g. portrait.json."},
              "comfy_url": {"type": "string", "description": "Loopback ComfyUI URL (default http://127.0.0.1:8188)."}},
             ["project", "name"], False, kind="comfy_workflow_import"),
        tool("timeline_document_checkout",
             "Check out a timeline as one editable JSON document (placements, shots, clips). "
             "Use it to put the first clips on an empty timeline, then check and publish it.",
             {"project": {"type": "string", "description": "Project slug or ID."},
              "timeline": {"type": "string", "description": "Timeline slug or ID."}},
             ["project", "timeline"], True, kind="timeline_document", command="checkout"),
        tool("timeline_document_add_shot",
             "Add one new, empty shot to a timeline document (Astrid SDK add_authoring_shot) and place it; "
             "returns the updated document. For an empty timeline: checkout, add_shot, put a track and clips "
             "in document['shots'][shot_id]['internal_timeline'] (clip times in seconds: at, hold/from/to), "
             "then check and publish. The rendering skill's document-checkout reference has the clip fields.",
             {"document": document,
              "name": {"type": "string", "description": "Shot display name."},
              "shot_id": {"type": "string", "description": "Optional explicit shot ID."},
              "occurrence_id": {"type": "string", "description": "Optional explicit placement (occurrence) ID."},
              "start_ms": {"type": "number", "description": "Placement start in milliseconds (default 0)."},
              "duration_ms": {"type": "number", "description": "Placement window duration in milliseconds."}},
             ["document"], True, kind="timeline_document", command="add_shot"),
        tool("timeline_document_check",
             "Validate an edited timeline document and show its diff and media imports, without publishing.",
             {"document": document}, ["document"], True, kind="timeline_document", command="check"),
        tool("timeline_document_publish",
             "Publish an edited timeline document as one new timeline revision.",
             {"document": document,
              "idempotency_key": {"type": "string", "description": "A new unique key for this publication."}},
             ["document", "idempotency_key"], False, kind="timeline_document", command="publish"),
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
