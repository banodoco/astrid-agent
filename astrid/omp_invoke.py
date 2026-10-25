"""Small JSON boundary for the packaged OMP admission tool."""
from __future__ import annotations

import json
import sys
from contextlib import redirect_stdout
from collections.abc import Mapping
from typing import Any


def invoke_tool(request: Mapping[str, Any], *, invoker=None) -> dict[str, Any]:
    allowed = {"capability_id", "kind", "project", "inputs", "execution_request"}
    if set(request) - allowed:
        raise ValueError("Unsupported invocation fields")
    capability_id = request.get("capability_id")
    kind = request.get("kind")
    project = request.get("project")
    if not isinstance(capability_id, str) or not capability_id.strip():
        raise ValueError("capability_id is required")
    if kind not in {"executor", "orchestrator"}:
        raise ValueError("kind must be executor or orchestrator")
    if not isinstance(project, str) or not project.strip():
        raise ValueError("project is required")
    if not isinstance(request.get("inputs", {}), Mapping):
        raise ValueError("inputs must be an object")
    kwargs = {key: value for key, value in request.items() if key not in {"capability_id", "kind"}}
    if invoker is None:
        from astrid.sdk.client import AstridClient
        from astrid.version import ASTRID_VERSION

        # This CLI tool is an explicit launcher boundary. The supported
        # acquisition path also ensures the generic pack host is ready on a
        # fresh installation; the client method carries the authenticated
        # generated transport into admission.
        with AstridClient.open_from_launcher(
            client_name="astrid-omp", client_version=ASTRID_VERSION,
        ) as client:
            result = client.invoke_result(capability_id, kind=kind, wait=False, **kwargs)
    else:
        result = invoker(capability_id, kind=kind, wait=False, **kwargs)
    raw = result.raw_result
    task = raw.get("task") if isinstance(raw, Mapping) else None
    refs = []
    fields = ("project_id", "run_id", "task_id", "capability_id")
    if result.ok is True and isinstance(task, Mapping) and all(
        isinstance(task.get(field), str) and task[field].strip() for field in fields
    ):
        refs.append({field: task[field] for field in fields})
    # Preserve the SDK response as tool content. The compact details are the
    # only machine join; supplied project slugs and narrative are never joins.
    payload = {"ok": result.ok, "error": result.error, "result": dict(raw)}
    return {"content": [{"type": "text", "text": json.dumps(payload, default=str)}],
            "details": {"astridTaskRefs": refs}}


def main(argv=None) -> int:
    try:
        args = sys.argv[1:] if argv is None else argv
        request = json.loads(args[0])
        if not isinstance(request, dict):
            raise ValueError("Invocation must be an object")
        from astrid.core.execution.platform_helpers import configure_process_control_from_install

        configure_process_control_from_install()
        with redirect_stdout(sys.stderr):
            result = invoke_tool(request)
        print(json.dumps(result))
        return 0
    except Exception as exc:
        print(json.dumps({"content": [{"type": "text", "text": str(exc)}],
                          "details": {"astridTaskRefs": []}}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
