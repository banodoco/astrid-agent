"""Exact, CPU-observed custody for a managed VibeComfy session on RunPod.

The injected transport runs VibeComfy's managed ``session start/status/stop``
contract on the selected pod. Its status observation must include the managed
session's config.json and positive daemon/Comfy ownership evidence (the same
launch marker, birth identities and listener ownership checked by VibeComfy).
This owner never probes CUDA or qualifies a GPU.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Mapping, Protocol


class ManagedSessionError(RuntimeError):
    """Selected session or live process custody cannot be proved exactly."""


_SESSION_ID = re.compile(r"[A-Za-z0-9._-]{1,64}\Z")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")


def _digest(value: Mapping[str, Any]) -> str:
    try:
        encoded = json.dumps(
            dict(value), sort_keys=True, separators=(",", ":"),
            ensure_ascii=False, allow_nan=False,
        ).encode()
    except (TypeError, ValueError) as exc:
        raise ManagedSessionError("managed session configuration is not canonical") from exc
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _path(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.startswith("/") or any(
        character in value for character in "\x00\r\n"
    ):
        raise ManagedSessionError(f"{label} must be a canonical absolute path")
    path = PurePosixPath(value)
    if str(path) != value or ".." in path.parts or "." in value.split("/"):
        raise ManagedSessionError(f"{label} contains a lexical alias")
    return value


def _process(value: object, label: str) -> tuple[int, str]:
    if (not isinstance(value, Mapping) or type(value.get("pid")) is not int
            or value["pid"] <= 0 or not isinstance(value.get("birth_id"), str)
            or not value["birth_id"]):
        raise ManagedSessionError(f"{label} process identity is incomplete")
    return value["pid"], value["birth_id"]


@dataclass(frozen=True)
class ManagedSessionSelection:
    account_ref: str
    pod_id: str
    session_ref: str
    python_executable: str
    runtime_root: str
    vibecomfy_root: str
    comfy_root: str
    model_root: str
    output_root: str
    port: int
    config: Mapping[str, Any]
    config_digest: str


@dataclass(frozen=True)
class ManagedSessionReceipt:
    account_ref: str
    pod_id: str
    session_ref: str
    config_digest: str
    output_root: str
    url: str
    daemon_pid: int
    daemon_birth_id: str
    comfy_pid: int
    comfy_birth_id: str
    launch_token_digest: str

    def as_dict(self) -> dict[str, Any]:
        return vars(self).copy()

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ManagedSessionReceipt:
        if not isinstance(value, Mapping) or set(value) != set(cls.__dataclass_fields__):
            raise ManagedSessionError("managed session receipt has invalid fields")
        return cls(**dict(value))


class ManagedSessionTransport(Protocol):
    """Target-bound adapter for VibeComfy managed start/status/stop.

    ``status`` must read config.json and obtain positive VibeComfy composite
    ownership evidence on the target, not merely parse CLI status text.
    ``stop`` must address the exact receipt and use VibeComfy's managed stop;
    it must not signal an arbitrary PID supplied by a caller.
    ``confirm_stopped`` must positively observe that both receipt-bound birth
    identities are gone. An unavailable observation is an error, not absence.
    """

    def start(self, selection: ManagedSessionSelection) -> None: ...
    def status(self, selection: ManagedSessionSelection) -> Mapping[str, Any] | None: ...
    def stop(self, selection: ManagedSessionSelection, receipt: ManagedSessionReceipt) -> None: ...
    def confirm_stopped(self, selection: ManagedSessionSelection,
                        receipt: ManagedSessionReceipt) -> bool: ...


_REMOTE_SESSION_PROGRAM = r'''import json,os,sys
from pathlib import Path
from types import SimpleNamespace
selection=json.loads(sys.argv[1]); action=selection.pop("action")
runtime_root=Path(selection["runtime_root"]); os.chdir(runtime_root)
sys.path.insert(0,selection["vibecomfy_root"])
os.environ["PYTHONPATH"]=selection["vibecomfy_root"]+(os.pathsep+os.environ["PYTHONPATH"] if os.environ.get("PYTHONPATH") else "")
from vibecomfy.commands import session as commands
from vibecomfy.runtime import session as runtime
session_id=selection["session_ref"]
session_dir=Path("out/sessions")/session_id
if action=="start":
 commands._config_from_args=lambda _args: dict(selection["config"])
 code=commands._cmd_session_start(SimpleNamespace(id=session_id))
 if code: raise RuntimeError("VibeComfy managed session start failed")
 result={"started":True}
elif action=="status":
 if not session_dir.exists(): result={"absent":True}
 else:
  if not runtime._session_ready(session_dir):
   if any((session_dir/name).exists() for name in ("pid","url","launch.json","config.json")):
    raise RuntimeError("selected VibeComfy session has incomplete or unhealthy ownership evidence")
   result={"absent":True}
  else:
   daemon_pid=int((session_dir/"pid").read_text().strip()); marker=runtime._read_launch_marker(session_dir)
   if marker is None or not runtime._session_composite_ownership_verified(session_dir,daemon_pid):
    raise RuntimeError("VibeComfy daemon, Comfy child, or listener ownership is unverified")
   comfy_pid=int((session_dir/"comfy_pid").read_text().strip())
   config=json.loads((session_dir/"config.json").read_text())
   result={"account_ref":selection["account_ref"],"pod_id":selection["pod_id"],
    "session_ref":session_id,"runtime_root":str(runtime_root),"config":config,
    "output_root":config.get("output_directory"),"ownership_verified":True,
    "gpu_qualified":False,"daemon":{"pid":daemon_pid,"birth_id":runtime._process_start_identity(daemon_pid)},
    "comfy":{"pid":comfy_pid,"birth_id":(session_dir/"comfy_process_start_identity").read_text().strip()},
    "url":(session_dir/"url").read_text().strip(),"launch_token_digest":"sha256:"+__import__("hashlib").sha256(str(marker["launch_token"]).encode()).hexdigest()}
elif action=="stop":
 code=commands._cmd_session_stop(SimpleNamespace(id=session_id))
 if code: raise RuntimeError("VibeComfy managed session stop failed")
 result={"stopped":True}
elif action=="confirm_stopped":
 receipt=selection["receipt"]
 observations=[]
 for key in ("daemon","comfy"):
  current=runtime._process_start_identity(receipt[key+"_pid"])
  observations.append(current != receipt[key+"_birth_id"])
 result={"stopped":all(observations)}
else: raise RuntimeError("unsupported managed VibeComfy session operation")
print(json.dumps(result,sort_keys=True,separators=(",",":")))
'''


class RunPodVibeComfySessionTransport:
    """Run VibeComfy's existing managed-session commands over P1's SSH seam.

    The transport uses the selected remote interpreter and checkout. Status
    reuses VibeComfy's own process, launch-token, child-parent and listener
    ownership checks; it never starts, stops, or inspects GPU state.
    """

    def __init__(self, pod: Any, *, preparation_transport: Any | None = None) -> None:
        if preparation_transport is None:
            from .worker_staging import RunPodPreparationTransport

            preparation_transport = RunPodPreparationTransport(pod)
        self.preparation_transport = preparation_transport

    @staticmethod
    def _request(selection: ManagedSessionSelection, action: str) -> dict[str, Any]:
        return {
            "action": action,
            "account_ref": selection.account_ref,
            "pod_id": selection.pod_id,
            "session_ref": selection.session_ref,
            "runtime_root": selection.runtime_root,
            "vibecomfy_root": selection.vibecomfy_root,
            "config": dict(selection.config),
        }

    def _run(self, selection: ManagedSessionSelection, action: str,
             receipt: ManagedSessionReceipt | None = None) -> Mapping[str, Any]:
        from .worker_owner import _run_sync

        request = self._request(selection, action)
        if receipt is not None:
            request["receipt"] = {
                "daemon_pid": receipt.daemon_pid,
                "daemon_birth_id": receipt.daemon_birth_id,
                "comfy_pid": receipt.comfy_pid,
                "comfy_birth_id": receipt.comfy_birth_id,
            }
        encoded = base64.b64encode(
            json.dumps(request, sort_keys=True, separators=(",", ":")).encode()
        ).decode("ascii")
        try:
            return _run_sync(self.preparation_transport._run_json_program(
                _REMOTE_SESSION_PROGRAM, [encoded], python=selection.python_executable,
            ))
        except Exception as exc:
            raise ManagedSessionError(
                f"remote VibeComfy managed-session {action} could not be confirmed"
            ) from exc

    def start(self, selection: ManagedSessionSelection) -> None:
        self._run(selection, "start")

    def status(self, selection: ManagedSessionSelection) -> Mapping[str, Any] | None:
        value = self._run(selection, "status")
        return None if value.get("absent") is True else value

    def stop(self, selection: ManagedSessionSelection,
             receipt: ManagedSessionReceipt) -> None:
        self._run(selection, "stop", receipt)

    def confirm_stopped(self, selection: ManagedSessionSelection,
                        receipt: ManagedSessionReceipt) -> bool:
        return self._run(selection, "confirm_stopped", receipt).get("stopped") is True


class RunPodManagedVibeComfySessionOwner:
    """Materialize selected config and guard start, restart resume, and stop."""

    def __init__(self, transport: ManagedSessionTransport, *, session_config_type: Any = None) -> None:
        self.transport = transport
        self.session_config_type = session_config_type

    def _session_config(self, values: dict[str, Any]) -> Any:
        config_type = self.session_config_type
        if config_type is None:
            from vibecomfy.runtime.session import SessionConfig

            config_type = SessionConfig
        return config_type.from_dict(values)

    def select(self, reference: Any, cpu_readiness: Mapping[str, Any], *, port: int) -> ManagedSessionSelection:
        """Bind the managed session to P2's selected CPU paths and Runtime reference."""
        target = getattr(reference, "effective_target", None)
        paths = cpu_readiness.get("effective_paths") if isinstance(cpu_readiness, Mapping) else None
        if (not isinstance(target, Mapping) or target.get("kind") != "runpod"
                or not isinstance(target.get("provider_account_ref"), str)
                or not isinstance(target.get("pod_id"), str)
                or not isinstance(paths, Mapping)
                or cpu_readiness.get("status") != "cpu_ready"
                or cpu_readiness.get("gpu_qualified") is not False):
            raise ManagedSessionError("selected RunPod CPU preparation evidence is required")
        session_ref = getattr(reference, "session_ref", None)
        if not isinstance(session_ref, str) or not _SESSION_ID.fullmatch(session_ref):
            raise ManagedSessionError("managed session reference is unsafe")
        if type(port) is not int or not 1 <= port <= 65535:
            raise ManagedSessionError("managed session port is invalid")
        # P2 emits the interpreter beside ``effective_paths`` because it is
        # the interpreter that ran the child readiness check.  Keep accepting
        # an in-map value only for old persisted fixtures; it must agree when
        # both witnesses are present.
        python_value = cpu_readiness.get("python_executable")
        legacy_python = paths.get("python_executable")
        if legacy_python is not None and legacy_python != python_value:
            raise ManagedSessionError("P2 interpreter witnesses disagree")
        python = _path(python_value, "selected Python")
        comfy = _path(paths.get("comfy_root"), "selected Comfy root")
        model = _path(paths.get("model_root"), "selected model root")
        output = _path(paths.get("output_root"), "selected output root")
        vibecomfy = _path(paths.get("vibecomfy_root"), "selected VibeComfy root")
        candidate = _path(paths.get("candidate_namespace"), "selected release candidate")
        runtime_root = str(PurePosixPath(comfy).parent)
        if (python != str(reference.executable.path)
                or model != str(reference.model_root)
                or output != str(reference.output_root)
                or PurePosixPath(runtime_root).name != "runtime"
                or PurePosixPath(runtime_root).parent != PurePosixPath(candidate)
                or comfy != str(PurePosixPath(runtime_root) / "ComfyUI")):
            raise ManagedSessionError("managed session paths differ from Runtime selection")
        values = {
            "runtime_root": runtime_root,
            "cwd": runtime_root,
            "port": port,
            "warm_policy": "never",
            "base_directory": comfy,
            "models_root": model,
            "models_root_normalized": model,
            "output_directory": output,
            "locality": "managed_local_server",
            "server_log_path": str(PurePosixPath(runtime_root) / "out" / "sessions" / session_ref / "comfy.log"),
        }
        config = self._session_config(values)
        normalized = {
            **values,
            "runtime_root": str(config.runtime_root),
            "cwd": str(config.cwd),
            "port": config.port,
            "warm_policy": config.warm_policy,
            **{key: config.extra[key] for key in (
                "base_directory", "models_root", "models_root_normalized",
                "output_directory", "locality", "server_log_path",
            )},
        }
        if normalized != values:
            raise ManagedSessionError("SessionConfig changed selected managed-session paths")
        digest = _digest(normalized)
        if digest != getattr(reference, "session_config_digest", None):
            raise ManagedSessionError("materialized SessionConfig differs from Runtime selection")
        return ManagedSessionSelection(
            target["provider_account_ref"], target["pod_id"], session_ref,
            python, runtime_root, vibecomfy, comfy, model, output, port, normalized, digest,
        )

    def _observe(self, selection: ManagedSessionSelection) -> ManagedSessionReceipt | None:
        value = self.transport.status(selection)
        if value is None:
            return None
        if not isinstance(value, Mapping):
            raise ManagedSessionError("managed session status is invalid")
        if (value.get("account_ref") != selection.account_ref
                or value.get("pod_id") != selection.pod_id
                or value.get("session_ref") != selection.session_ref
                or value.get("runtime_root") != selection.runtime_root
                or value.get("config") != selection.config
                or value.get("output_root") != selection.output_root
                or value.get("ownership_verified") is not True
                or value.get("gpu_qualified") is not False):
            raise ManagedSessionError("live managed session config, output or ownership changed")
        daemon_pid, daemon_birth = _process(value.get("daemon"), "managed daemon")
        comfy_pid, comfy_birth = _process(value.get("comfy"), "managed Comfy child")
        if daemon_pid == comfy_pid:
            raise ManagedSessionError("managed daemon and Comfy child cannot share a PID")
        url = value.get("url")
        token_digest = value.get("launch_token_digest")
        if (not isinstance(url, str) or url != f"http://127.0.0.1:{selection.port}"
                or not isinstance(token_digest, str) or not _DIGEST.fullmatch(token_digest)):
            raise ManagedSessionError("managed session URL or launch ownership token is invalid")
        return ManagedSessionReceipt(
            selection.account_ref, selection.pod_id, selection.session_ref,
            selection.config_digest, selection.output_root, url,
            daemon_pid, daemon_birth, comfy_pid, comfy_birth, token_digest,
        )

    def start(self, selection: ManagedSessionSelection) -> ManagedSessionReceipt:
        existing = self._observe(selection)
        if existing is not None:
            # A prior start may have succeeded while its SSH response was
            # lost. Reuse only the same selected config and owned process pair.
            return existing
        try:
            self.transport.start(selection)
        except Exception:
            # Start is reconciled by the selected VibeComfy session id and
            # positive process/listener ownership evidence, never by starting
            # a second daemon after an ambiguous response.
            recovered = self._observe(selection)
            if recovered is not None:
                return recovered
            raise
        receipt = self._observe(selection)
        if receipt is None:
            raise ManagedSessionError("managed session start has no owned process evidence")
        return receipt

    def resume(self, selection: ManagedSessionSelection,
               receipt: ManagedSessionReceipt | Mapping[str, Any]) -> ManagedSessionReceipt:
        expected = receipt if isinstance(receipt, ManagedSessionReceipt) else ManagedSessionReceipt.from_dict(receipt)
        observed = self._observe(selection)
        if observed is None or observed != expected:
            raise ManagedSessionError("managed session process or config changed before resume")
        return observed

    def stop(self, selection: ManagedSessionSelection,
             receipt: ManagedSessionReceipt | Mapping[str, Any]) -> dict[str, Any]:
        expected = receipt if isinstance(receipt, ManagedSessionReceipt) else ManagedSessionReceipt.from_dict(receipt)
        observed = self._observe(selection)
        if observed is None:
            if self.transport.confirm_stopped(selection, expected) is True:
                return {"state": "stopped", "session_ref": selection.session_ref,
                        "config_digest": selection.config_digest, "gpu_qualified": False}
            raise ManagedSessionError("managed session absence is not positively confirmed")
        if observed != expected:
            raise ManagedSessionError("managed session process or config changed before stop")
        try:
            self.transport.stop(selection, expected)
        except Exception:
            if (self._observe(selection) is not None
                    or self.transport.confirm_stopped(selection, expected) is not True):
                raise
        if (self.transport.status(selection) is not None
                or self.transport.confirm_stopped(selection, expected) is not True):
            raise ManagedSessionError("managed session stop is unconfirmed")
        return {"state": "stopped", "session_ref": selection.session_ref,
                "config_digest": selection.config_digest, "gpu_qualified": False}

    def observe_config(self, selection: ManagedSessionSelection,
                       receipt: ManagedSessionReceipt | Mapping[str, Any]) -> dict[str, Any]:
        self.resume(selection, receipt)
        return {"session_ref": selection.session_ref,
                "session_config_digest": selection.config_digest,
                "output_root": selection.output_root,
                "gpu_qualified": False}


__all__ = [
    "ManagedSessionError", "ManagedSessionReceipt", "ManagedSessionSelection",
    "ManagedSessionTransport", "RunPodManagedVibeComfySessionOwner",
    "RunPodVibeComfySessionTransport",
]
