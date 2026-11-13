"""Trusted dynamic provider-route resolvers.

Resolvers in this module run in Astrid's host process, before the child is
started.  They may use provider credentials held by the host to turn an
admitted handle into an exact, short-lived route.  Child-supplied endpoint
strings are never accepted as authority.
"""

from __future__ import annotations

import json
import re
from pathlib import Path


class ProviderRouteResolutionError(RuntimeError):
    """A dynamic provider endpoint could not be verified."""


_SSH_HANDLE_RE = re.compile(r"^root@(?P<host>[^\s]+)\s+-p\s+(?P<port>\d+)$")


def resolve_runpod_pod_handle_ssh_route(handle_value: object) -> str:
    """Resolve a verified RunPod pod handle to one exact TCP route.

    The persisted handle supplies the pod identity and the expected endpoint;
    the provider status supplies the current endpoint.  Both must agree before
    the route is admitted to Astrid's host-owned broker.
    """

    handle_path = Path(str(handle_value)).expanduser()
    try:
        handle = json.loads(handle_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ProviderRouteResolutionError(
            f"RunPod pod handle cannot be read: {handle_path}"
        ) from exc
    if not isinstance(handle, dict):
        raise ProviderRouteResolutionError("RunPod pod handle must contain an object")

    pod_id = str(handle.get("pod_id") or "").strip()
    if not pod_id:
        raise ProviderRouteResolutionError("RunPod pod handle is missing pod_id")
    expected = _SSH_HANDLE_RE.match(str(handle.get("ssh") or "").strip())
    if expected is None:
        raise ProviderRouteResolutionError(
            "RunPod pod handle is missing a valid root@host -p port ssh endpoint"
        )
    expected_host = expected.group("host").strip("[]").lower()
    expected_port = int(expected.group("port"))

    snapshot = handle.get("config_snapshot")
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    api_key_ref = str(
        handle.get("api_key_ref")
        or snapshot.get("api_key_ref")
        or "RUNPOD_API_KEY"
    )
    from astrid.core.util.credentials_scope import CredentialsScope

    credential = CredentialsScope.resolve_local("runpod", env_var=api_key_ref)
    from runpod_lifecycle.api import get_pod_status

    status = get_pod_status(pod_id, credential.value)
    if not isinstance(status, dict):
        raise ProviderRouteResolutionError(f"RunPod pod {pod_id} was not found")
    if status.get("desired_status") not in {"RUNNING", "PROVISIONING", "STARTING"}:
        raise ProviderRouteResolutionError(
            f"RunPod pod {pod_id} is not attachable: {status.get('desired_status')!r}"
        )

    live_ssh = None
    for port in status.get("ports") or ():
        if not isinstance(port, dict) or str(port.get("privatePort")) != "22":
            continue
        try:
            live_port = int(port.get("publicPort"))
        except (TypeError, ValueError):
            continue
        live_ssh = (str(port.get("ip") or "").strip("[]").lower(), live_port)
        break
    if live_ssh is None or not live_ssh[0] or live_ssh[1] <= 0:
        raise ProviderRouteResolutionError(
            f"RunPod pod {pod_id} has no live SSH endpoint"
        )
    if live_ssh != (expected_host, expected_port):
        raise ProviderRouteResolutionError(
            f"RunPod pod {pod_id} SSH endpoint changed from "
            f"{expected_host}:{expected_port} to {live_ssh[0]}:{live_ssh[1]}; "
            "a fresh handle is required"
        )
    return f"tcp://{live_ssh[0]}:{live_ssh[1]}"


__all__ = ["ProviderRouteResolutionError", "resolve_runpod_pod_handle_ssh_route"]
