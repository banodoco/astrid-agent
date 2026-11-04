"""Internal child used by :class:`GenericPackHost` for one capability run.

This module is intentionally tiny: admission, leases, output publication, and
settlement remain in the parent host/runtime.  Only pack runtime code executes
here, under ``ASTRID_INTERNAL_INVOCATION=1``.
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
import runpy
import socket
import sys
from dataclasses import asdict, is_dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, Mapping

from .generic_host import _capability_digest, _source_digest_for_roots, _verify_action_admission


def _read_captured_source(fd: int):
    from runtime_protocol.verified_host_launch import canonical

    with os.fdopen(fd, "rb") as channel:
        raw = channel.read(12 * 1024 * 1024 + 1)
    if len(raw) > 12 * 1024 * 1024:
        raise ValueError("external source descriptor exceeds its bound")
    selected = json.loads(raw)
    if canonical(selected) != raw or set(selected) != {"scope", "selected_profile_digest", "source", "definition"}:
        raise ValueError("invalid private external source descriptor")
    source = selected["source"]
    if selected["definition"] not in source["definitions"] or set(source["contents"]) != set(source["files"]):
        raise ValueError("external source descriptor selection differs")
    contents = {name: base64.b64decode(data, validate=True) for name, data in source["contents"].items()}
    if any(hashlib.sha256(data).hexdigest() != source["files"][name] for name, data in contents.items()):
        raise ValueError("external source descriptor bytes differ")
    return selected, contents


def _run_captured_command(encoded: str, selected, contents) -> int:
    from .executor.runner import _PackSourceFinder

    args = json.loads(encoded)
    while args:
        if args[0] in {"-u", "-B"}:
            if args.pop(0) == "-B":
                sys.dont_write_bytecode = True
        elif args[:2] == ["-X", "utf8"]:
            del args[:2]
        else:
            break
    if (not args or args[0].startswith("-") and args[0] not in {"-m", "-c"}
            or args[0] in {"-m", "-c"} and len(args) < 2):
        raise ValueError("unsupported captured Python command")
    root = Path(selected["source"]["root"])
    names = {Path(name).parts[0].split(".")[0] for name in contents if name.endswith(".py")}
    names.difference_update({"__init__", "__main__"})
    if root.name.isidentifier():
        names.add(root.name)
    finders = []
    for name in sorted(names):
        if not name.isidentifier() or name in {"astrid", "runtime_protocol", "astrid_runtime_client"}:
            raise ValueError("external source namespace is invalid")
        if name in sys.modules or importlib.util.find_spec(name) is not None:
            raise ValueError("external source collides with an installed package")
        finders.append(_PackSourceFinder(name, root, {}, contents, include_prefix=name != root.name))
    sys.meta_path[:0] = finders
    try:
        if args[0] == "-m":
            if args[1].split(".")[0] not in names:
                raise ValueError("external command module is outside captured source")
            sys.argv = [args[1], *args[2:]]
            runpy.run_module(args[1], run_name="__main__", alter_sys=True)
        elif args[0] == "-c":
            sys.argv = ["-c", *args[2:]]
            exec(compile(args[1], "<string>", "exec"), {"__name__": "__main__"})
        else:
            source = Path(args[0]).absolute()
            relative = source.relative_to(root).as_posix()
            if relative not in contents or source.suffix != ".py":
                raise ValueError("external command script is outside captured source")
            sys.argv = args
            exec(compile(contents[relative], str(source), "exec"),
                 {"__name__": "__main__", "__file__": str(source)})
        return 0
    finally:
        for finder in finders:
            sys.meta_path.remove(finder)
        for name in tuple(sys.modules):
            if name.split(".")[0] in names:
                del sys.modules[name]


def _install_inherited_bridge(fd: str):
    from astrid.sdk._child_bridge import _install_child_bridge

    channel = socket.socket(fileno=int(fd))
    channel.set_inheritable(False)
    return _install_child_bridge(channel)


def _run_child_command(fd: str | None, encoded: str, *, renderer: bool = False, captured=None) -> int:
    """Run Python command code with its host channel and optional captured source."""
    args = json.loads(encoded)
    if not isinstance(args, list) or not args or any(not isinstance(arg, str) for arg in args):
        raise ValueError("invalid private Python command")
    while args:
        if args[0] in {"-u", "-B"}:
            if args.pop(0) == "-B":
                sys.dont_write_bytecode = True
        elif args[:2] == ["-X", "utf8"]:
            del args[:2]
        else:
            break
    if (not args or args[0].startswith("-") and args[0] not in {"-c", "-m"}
            or args[0] in {"-c", "-m"} and len(args) < 2):
        raise ValueError("unsupported delegating Python command flags")
    if renderer:
        from .remotion_host_session import install_host_remotion_client
        bridge = install_host_remotion_client(fd)
    elif fd is not None:
        bridge = _install_inherited_bridge(fd)
    else:
        bridge = None
    try:
        from ._child_import_receipt import observe_action_entry

        observe_action_entry("worker_entry")
        if captured is not None:
            return _run_captured_command(encoded, *captured)
        if args[0] == "-c":
            sys.argv = ["-c", *args[2:]]
            sys.path[0] = ""
            original_main = sys.modules["__main__"]
            command_main = ModuleType("__main__")
            sys.modules["__main__"] = command_main
            try:
                exec(compile(args[1], "<string>", "exec"), command_main.__dict__)
            finally:
                sys.modules["__main__"] = original_main
        elif args[0] == "-m":
            sys.argv = [args[1], *args[2:]]
            sys.path[0] = os.getcwd()
            runpy.run_module(args[1], run_name="__main__", alter_sys=True)
        else:
            sys.argv = args
            sys.path[0] = str(Path(args[0]).resolve().parent)
            runpy.run_path(args[0], run_name="__main__")
        return 0
    finally:
        if bridge is not None:
            bridge.close()


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_value(item) for item in value]
    if is_dataclass(value):
        return _json_value(asdict(value))
    return str(value)


def _request_object(raw: Mapping[str, Any], *, kind: str, capability_id: str) -> Any:
    request = dict(raw)
    if kind == "executor":
        from astrid.core.execution.executor.runner import ExecutorRunRequest

        request.setdefault("executor_id", capability_id)
        return ExecutorRunRequest(**request)
    from astrid.core.execution.orchestrator.runner import OrchestratorRunRequest

    request.setdefault("orchestrator_id", capability_id)
    return OrchestratorRunRequest(**request)


def run(payload_path: str | Path, *, captured=None) -> int:
    payload = json.loads(Path(payload_path).read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError("capability worker payload must be an object")
    kind = str(payload.get("capability_kind") or "")
    capability_id = str(payload.get("capability_id") or "")
    request_payload = payload.get("request")
    if kind not in {"executor", "orchestrator"} or not capability_id:
        raise ValueError("capability worker payload has invalid capability identity")
    if not isinstance(request_payload, Mapping):
        raise ValueError("capability worker payload request must be an object")

    definition_payload = payload.get("definition")
    admission = payload.get("admission")
    if not isinstance(definition_payload, Mapping) or not isinstance(admission, Mapping):
        raise ValueError("capability worker requires an admitted definition and admission fence")
    if str(definition_payload.get("id") or "") != capability_id:
        raise ValueError("admitted definition does not match capability identity")
    expected_digest = str(admission.get("capability_digest") or "")
    if expected_digest and _capability_digest(definition_payload) != expected_digest:
        raise ValueError("admitted capability definition digest changed")
    expected_version = admission.get("version")
    if expected_version is not None and str(definition_payload.get("version")) != str(expected_version):
        raise ValueError("admitted capability version changed")
    if captured is not None:
        selected, contents = captured
        chosen = selected["definition"]
        source_root = Path(admission["source_root"])
        if (chosen["capability_kind"] != kind or chosen["capability_id"] != capability_id
                or chosen["definition_digest"] != _capability_digest(definition_payload)
                or (source_root != Path(selected["source"]["root"])
                    if kind == "executor" else
                    source_root != (Path(selected["source"]["root"]) / chosen["path"]).parent)):
            raise ValueError("external worker definition differs from Runtime selection")
    _verify_action_admission(definition_payload, admission)
    expected_source = str(admission.get("source_digest") or "")
    source_roots = admission.get("source_roots")
    if isinstance(source_roots, (list, tuple)) and source_roots:
        roots = tuple(Path(str(value)).expanduser().resolve() for value in source_roots)
    elif admission.get("source_root"):
        roots = (Path(str(admission["source_root"])).expanduser().resolve(),)
    else:
        roots = ()
    if roots and expected_source and _source_digest_for_roots(roots) != expected_source:
        raise ValueError("admitted capability source digest changed")
    remotion_fd = payload.get("remotion_host_fd")
    if remotion_fd is not None:
        if kind != "executor" or capability_id != "rendering.timeline_visualize" or type(remotion_fd) is not int:
            raise ValueError("invalid inherited renderer lease")
        from .remotion_host_session import install_host_remotion_client
        install_host_remotion_client(remotion_fd)
    if kind == "executor":
        from astrid.core.execution.executor.registry import ExecutorRegistry
        from astrid.core.execution.executor.runner import run_executor

        from astrid.core.execution.executor.schema import validate_executor_definition

        registry = ExecutorRegistry([validate_executor_definition(definition_payload)])
        result = run_executor(_request_object(request_payload, kind=kind, capability_id=capability_id), registry, _admission=admission,
                              _captured_source=captured[1] if captured is not None else None)
    else:
        from astrid.core.execution.orchestrator.registry import OrchestratorRegistry
        from astrid.core.execution.orchestrator.schema import validate_orchestrator_definition
        from astrid.core.execution.orchestrator.runner import run_orchestrator

        result = run_orchestrator(
            _request_object(request_payload, kind=kind, capability_id=capability_id),
            OrchestratorRegistry([validate_orchestrator_definition(definition_payload)]),
            _captured_source=captured,
        )

    result_path = Path(str(payload["result_path"]))
    result_path.write_text(
        json.dumps(
            {
                "ok": bool(getattr(result, "ok", False)),
                "returncode": getattr(result, "returncode", None),
                "outputs": _json_value(getattr(result, "outputs", {}) or {}),
                "payload": _json_value(getattr(result, "payload", {}) or {}),
                "error": _json_value(getattr(result, "error", None)),
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return 0 if bool(getattr(result, "ok", False)) else 1


if __name__ == "__main__":
    try:
        captured = None
        if len(sys.argv) >= 3 and sys.argv[1] == "--external-source-fd":
            captured = _read_captured_source(int(sys.argv[2]))
            del sys.argv[1:3]
        if len(sys.argv) == 3 and sys.argv[1] == "--python-command" and captured is None:
            raise SystemExit(_run_child_command(None, sys.argv[2]))
        if len(sys.argv) == 3 and sys.argv[1] == "--captured-command" and captured is not None:
            raise SystemExit(_run_captured_command(sys.argv[2], *captured))
        if len(sys.argv) == 4 and sys.argv[1] in {"--child-command", "--renderer-command"}:
            raise SystemExit(_run_child_command(sys.argv[2], sys.argv[3], renderer=sys.argv[1] == "--renderer-command", captured=captured))
        bridge = None
        if len(sys.argv) == 4 and sys.argv[2] == "--child-bridge":
            bridge = _install_inherited_bridge(sys.argv[3])
        elif len(sys.argv) != 2 or sys.argv[1].startswith("--"):
            raise ValueError("invalid private worker launch controls")
        try:
            raise SystemExit(run(sys.argv[1], captured=captured))
        finally:
            if bridge is not None:
                bridge.close()
    except Exception as exc:
        # Keep traceback on stderr for the host's bounded diagnostic.  The
        # parent never treats a missing/partial result as successful.
        import traceback

        traceback.print_exc()
        raise SystemExit(1) from exc
