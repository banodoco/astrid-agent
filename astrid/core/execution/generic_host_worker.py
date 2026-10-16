"""Internal child used by :class:`GenericPackHost` for one capability run.

This module is intentionally tiny: admission, leases, output publication, and
settlement remain in the parent host/runtime.  Only pack runtime code executes
here, under ``ASTRID_INTERNAL_INVOCATION=1``.
"""

from __future__ import annotations

import json
import os
import runpy
import sys
from dataclasses import asdict, is_dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, Mapping

def _install_pipe_bridge(descriptors):
    from ._child_pipe import install_bridge
    return install_bridge(descriptors)


def _run_child_command(encoded: str, descriptors: str = "", *, renderer: bool = False) -> int:
    """Install before Python command code, preserving its argv and entry point."""
    args = json.loads(encoded)
    if not isinstance(args, list) or not args or any(not isinstance(arg, str) for arg in args):
        raise ValueError("invalid private Python command")
    while args and args[0] in {"-u", "-B"}:
        if args.pop(0) == "-B":
            sys.dont_write_bytecode = True
    if (not args or args[0].startswith("-") and args[0] not in {"-c", "-m"}
            or args[0] in {"-c", "-m"} and len(args) < 2):
        raise ValueError("unsupported delegating Python command flags")
    if renderer:
        from .remotion_pipe import install_executor_client
        bridge = install_executor_client()
    else:
        bridge = _install_pipe_bridge(descriptors)
    try:
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


def run(payload_path: str | Path) -> int:
    from .generic_host import _capability_digest, _source_digest_for_roots, _verify_action_admission

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
    if payload.get("remotion_host_fd") is not None:
        raise ValueError("renderer socket descriptors are not admitted")
    if payload.get("remotion_pipe") is not None and type(payload["remotion_pipe"]) is not bool:
        raise ValueError("invalid renderer pipe selection")
    if payload.get("remotion_pipe") is True:
        if kind != "executor" or capability_id != "rendering.timeline_visualize":
            raise ValueError("invalid renderer pipe capability")
        from .remotion_pipe import install_executor_client
        install_executor_client()
    if kind == "executor":
        from astrid.core.execution.executor.registry import ExecutorRegistry
        from astrid.core.execution.executor.runner import run_executor

        from astrid.core.execution.executor.schema import validate_executor_definition

        registry = ExecutorRegistry([validate_executor_definition(definition_payload)])
        result = run_executor(_request_object(request_payload, kind=kind, capability_id=capability_id), registry, _admission=admission)
    else:
        from astrid.core.execution.orchestrator.registry import OrchestratorRegistry
        from astrid.core.execution.orchestrator.schema import validate_orchestrator_definition
        from astrid.core.execution.orchestrator.runner import run_orchestrator

        result = run_orchestrator(
            _request_object(request_payload, kind=kind, capability_id=capability_id),
            OrchestratorRegistry([validate_orchestrator_definition(definition_payload)]),
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
        if len(sys.argv) == 3 and sys.argv[1] == "--renderer-command-stdio":
            raise SystemExit(_run_child_command(sys.argv[2], renderer=True))
        if len(sys.argv) == 4 and sys.argv[1] == "--child-command":
            raise SystemExit(_run_child_command(sys.argv[3], sys.argv[2]))
        bridge = None
        if len(sys.argv) == 4 and sys.argv[2] == "--child-bridge":
            bridge = _install_pipe_bridge(sys.argv[3])
        elif len(sys.argv) != 2 or sys.argv[1].startswith("--"):
            raise ValueError("invalid private worker launch controls")
        try:
            raise SystemExit(run(sys.argv[1]))
        finally:
            if bridge is not None:
                bridge.close()
    except Exception as exc:
        from .provider_failure import ProviderFailure
        if isinstance(exc, ProviderFailure):
            payload = json.loads(Path(sys.argv[1]).read_text())
            Path(payload["result_path"]).write_text(json.dumps({
                "ok": False, "returncode": 1, "error": exc.to_envelope(),
            }))
        # Keep traceback on stderr for the host's bounded diagnostic.  The
        # parent never treats a missing/partial result as successful.
        import traceback

        traceback.print_exc()
        raise SystemExit(1) from exc
