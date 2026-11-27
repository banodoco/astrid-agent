"""Isolated module-local listener adapter for an explicitly selected Vibe build.

This module is stdlib-only. It preserves the selected session module's readiness,
configuration and cleanup code. The private host supplies launch and signal
callbacks; neither numeric PID nor an environment routing hint grants custody.
"""
from __future__ import annotations

import hashlib
import inspect
import os
import re
import sys
import threading
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Sequence

from runtime_protocol import platform


class EngineAdapterError(RuntimeError):
    """A selected engine launch or custody interface cannot be proved."""


SOURCE_FACT_VERSION = "astrid.selected-source-fact/v1"
LISTENER_EXIT_V2 = "astrid.retained-listener-exit/v2"
_SOURCE_FACTS = {"current_source_revision", "current_source_content_digest", "_process_start_identity"}


def _file_pin(path: Path) -> dict[str, str]:
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise EngineAdapterError("selected observation source is unavailable")
    return {"path": str(path), "sha256": "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()}


def _function_pin(function: Any) -> str:
    try:
        body = inspect.getsource(function).encode("utf-8")
    except (OSError, TypeError) as exc:
        raise EngineAdapterError("selected observation function is unavailable") from exc
    return "sha256:" + hashlib.sha256(body).hexdigest()


class _ObservationSubprocess:
    """A module-local boundary; never changes the shared subprocess module."""

    def __init__(self, original: Any, helpers: Mapping[str, Any] | None = None):
        self.original, self.helpers = original, helpers

    def __getattr__(self, name: str) -> Any:
        if name in {"Popen", "call", "check_call", "check_output"}:
            raise EngineAdapterError("unselected observation helper launch is forbidden")
        return getattr(self.original, name)

    def run(self, argv: Any, *args: Any, **kwargs: Any) -> Any:
        if self.helpers is None:
            raise EngineAdapterError("engine observation helper launch is forbidden")
        if (args or not isinstance(argv, list) or not argv
                or not (argv == ["git", "rev-parse", "HEAD"]
                        or argv == ["git", "ls-files", "-z", "-c", "-o", "--exclude-standard"]
                        or (len(argv) == 5 and argv[0:2] == ["ps", "-p"]
                            and argv[3:] == ["-o", "lstart="] and argv[2].isdigit()))):
            raise EngineAdapterError("unselected observation helper command is forbidden")
        pin = self.helpers[argv[0]]
        if _file_pin(Path(pin["path"])) != pin:
            raise EngineAdapterError("selected observation executable differs")
        if argv[0] == "ps" and sys.platform == "darwin":
            # This answers the selected read-only lstart command. It grants
            # neither process ownership nor a terminal/custody disposition.
            return _native_lstart_result(argv, kwargs)
        # Git facts remain live; a pinned revision cannot replace a new
        # tracked/non-ignored content observation without losing drift checks.
        return self.original.run([pin["path"], *argv[1:]], **kwargs)


def _native_process_identity(pid: int) -> dict[str, int] | None:
    """Bracket a read-only process fact; it is never a custody capability."""
    from astrid.core.execution.custody_broker import _darwin_process_records
    if type(pid) is not int or pid < 1:
        raise EngineAdapterError("selected native process pid is invalid")
    try:
        before, after = _darwin_process_records(pid), _darwin_process_records(pid)
    except Exception as error:
        raise EngineAdapterError("selected native process observation is unavailable") from error
    if not before and not after:
        return None
    fields = ("pid", "uid", "parent_pid", "pgid", "start_sec", "start_usec")
    if (len(before) != 1 or len(after) != 1 or before[0].get("pid") != pid
            or any(type(row.get(k)) is not int for row in (before[0], after[0]) for k in fields)
            or any(before[0].get(k) != after[0].get(k) for k in fields)
            or any(row.get("status") not in {1, 2, 3, 4, 5, 6} for row in (before[0], after[0]))
            or before[0]["start_sec"] < 1 or not 0 <= before[0]["start_usec"] < 1000000
            or any(before[0][k] < 0 for k in ("uid", "parent_pid", "pgid"))):
        raise EngineAdapterError("selected native process identity changed during observation")
    return dict(before[0])


def _native_process_start_identity(pid: int) -> str | None:
    from astrid.core.execution.custody_broker import _ps_lstart_birth
    row = _native_process_identity(pid)
    return None if row is None else _ps_lstart_birth(row["start_sec"]).removeprefix("ps-lstart:")


def _native_process_parent_pid(pid: int) -> int | None:
    row = _native_process_identity(pid)
    return None if row is None else row["parent_pid"]


class _ListenerObservationSubprocess:
    """Keep the selected listener predicate; avoid Darwin lsof helper forks.

    -O can block in kernel inspection. The original two-second caller timeout
    and fail-closed result remain mandatory; this is no custody capability.
    """

    def __init__(self, original: Any):
        self.original = original

    def __getattr__(self, name: str) -> Any:
        return getattr(self.original, name)

    def run(self, argv: Any, *args: Any, **kwargs: Any) -> Any:
        if (args or not isinstance(argv, list) or len(argv) != 6
                or argv[:4] != ["lsof", "-nP", "-t", "-a"]
                or not isinstance(argv[4], str) or not argv[4].startswith("-iTCP:")
                or not argv[4][6:].isdigit() or not 0 < int(argv[4][6:]) < 65536
                or argv[5] != "-sTCP:LISTEN"):
            raise EngineAdapterError("unselected listener observation command is forbidden")
        command = [argv[0], "-O", *argv[1:]] if sys.platform == "darwin" else list(argv)
        return self.original.run(command, **kwargs)


class SelectedCompositeObserver:
    """Original selected ownership predicates, with private Darwin OS facts.

    The caller's existing checkout attestation remains mandatory. Source and
    function pins bracket this call; no external module or shared subprocess
    object is changed, and no process fact grants signal/cleanup authority.
    """

    _NAMES = ("_session_ownership_verified", "_session_composite_ownership_verified", "_listener_owner_pid")

    def __init__(self, module: Any):
        import types
        if getattr(module, "__name__", None) != "vibecomfy.runtime.session":
            raise EngineAdapterError("selected ownership module origin differs")
        self.module = module
        self.path = Path(module.__file__)
        if str(self.path.resolve(strict=True)) != str(self.path):
            raise EngineAdapterError("selected ownership source is aliased")
        self.source_pin = _file_pin(self.path)
        self.globals_pin = dict(vars(module))
        self.originals = {name: getattr(module, name, None) for name in self._NAMES}
        for function in self.originals.values():
            if (not isinstance(function, types.FunctionType)
                    or function.__module__ != module.__name__
                    or function.__globals__ is not vars(module)
                    or inspect.getsourcefile(function) != str(self.path)
                    or function.__code__.co_filename != str(self.path)):
                raise EngineAdapterError("selected ownership function origin differs")
        self.function_pins = {name: _function_pin(fn) for name, fn in self.originals.items()}
        namespace = {**vars(module), "_process_start_identity": _native_process_start_identity,
                     "_process_parent_pid": _native_process_parent_pid,
                     "subprocess": _ListenerObservationSubprocess(module.subprocess)}
        self.functions = {}
        for name, original in self.originals.items():
            cloned = types.FunctionType(original.__code__, namespace, original.__name__,
                                        original.__defaults__, original.__closure__)
            cloned.__kwdefaults__ = original.__kwdefaults__
            namespace[name] = self.functions[name] = cloned
        self._validate()

    def _validate(self) -> None:
        if (set(vars(self.module)) != set(self.globals_pin)
                or any(vars(self.module)[name] is not value for name, value in self.globals_pin.items())):
            raise EngineAdapterError("selected ownership globals differ")
        if _file_pin(self.path) != self.source_pin:
            raise EngineAdapterError("selected ownership source differs")
        for name, original in self.originals.items():
            if (getattr(self.module, name, None) is not original
                    or original.__globals__ is not vars(self.module)
                    or _function_pin(original) != self.function_pins[name]):
                raise EngineAdapterError("selected ownership function differs")

    def verify(self, session_dir: Path, pid: int) -> bool:
        if type(pid) is not int or pid < 1:
            raise EngineAdapterError("selected ownership pid is invalid")
        self._validate()
        try:
            return self.functions["_session_composite_ownership_verified"](session_dir, pid)
        finally:
            self._validate()


def _native_lstart_result(argv: list[str], options: Mapping[str, Any]) -> Any:
    import subprocess
    from astrid.core.execution.custody_broker import _darwin_process_records, _ps_lstart_birth
    allowed = {"check", "capture_output", "text", "universal_newlines", "encoding", "errors", "timeout", "stdout", "stderr"}
    if (set(options) - allowed or int(argv[2]) < 1
            or options.get("stdout") not in (None, subprocess.PIPE, subprocess.DEVNULL)
            or options.get("stderr") not in (None, subprocess.PIPE, subprocess.DEVNULL)
            or (options.get("capture_output") and (options.get("stdout") is not None or options.get("stderr") is not None))):
        raise EngineAdapterError("selected native observation options are unsupported")
    pid = int(argv[2])
    value = _native_process_start_identity(pid)
    code, value = (1, "") if value is None else (0, value + "\n")
    text = bool(options.get("text") or options.get("universal_newlines") or options.get("encoding") or options.get("errors"))
    captured = options.get("capture_output") or options.get("stdout") == subprocess.PIPE
    captured_error = options.get("capture_output") or options.get("stderr") == subprocess.PIPE
    stdout = (value if text else value.encode(options.get("encoding") or "utf-8", options.get("errors") or "strict")) if captured else None
    stderr = ("" if text else b"") if captured_error else None
    result = subprocess.CompletedProcess(list(argv), code, stdout, stderr)
    if options.get("check"):
        result.check_returncode()
    return result


def validate_selected_observation_sources(module: Any, pins: Mapping[str, Any]) -> None:
    import importlib.util
    import vibecomfy.utils as utils
    required = {"version", "source_root", "source_revision", "source_content_digest", "sources", "functions", "helpers"}
    if (not isinstance(pins, dict) or set(pins) != required or pins.get("version") != SOURCE_FACT_VERSION
            or set(pins.get("sources", {})) != {"runtime.session", "commands.session", "utils"}
            or set(pins.get("functions", {})) != _SOURCE_FACTS
            or set(pins.get("helpers", {})) != {"git", "ps"}):
        raise EngineAdapterError("selected observation pins are incomplete")
    root = Path(pins["source_root"])
    if not root.is_absolute() or str(root.resolve(strict=True)) != str(root) or utils.find_repo_root() != root:
        raise EngineAdapterError("selected observation checkout origin differs")
    spec = importlib.util.find_spec("vibecomfy.commands.session")
    paths = {"runtime.session": Path(module.__file__), "utils": Path(utils.__file__),
             "commands.session": Path(spec.origin) if spec is not None and spec.origin else Path("")}
    for name, path in paths.items():
        expected = root / "vibecomfy" / ("utils.py" if name == "utils" else name.replace(".", "/") + ".py")
        if path != expected or _file_pin(path) != pins["sources"][name]:
            raise EngineAdapterError("selected observation import mapping differs")
    if (getattr(module, "find_repo_root", None) is not utils.find_repo_root
            or getattr(utils.find_repo_root, "__module__", None) != utils.__name__
            or inspect.getsourcefile(inspect.unwrap(utils.find_repo_root)) != str(paths["utils"])):
        raise EngineAdapterError("selected observation checkout resolver differs")
    for name in _SOURCE_FACTS:
        function = getattr(module, name, None)
        if (getattr(function, "__module__", None) != module.__name__
                or inspect.getsourcefile(function) != str(paths["runtime.session"])
                or _function_pin(function) != pins["functions"][name]):
            raise EngineAdapterError("selected observation function origin differs")
    for pin in pins["helpers"].values():
        if not isinstance(pin, dict) or set(pin) != {"path", "sha256"} or _file_pin(Path(pin["path"])) != pin:
            raise EngineAdapterError("selected observation executable differs")


class SelectedSourceObserver:
    """Host-only original functions, executed at the engine's actual call time."""

    def __init__(self, module: Any, pins: Mapping[str, Any]):
        import types
        validate_selected_observation_sources(module, pins)
        self.module, self.pins = module, dict(pins)
        self.functions = {}
        for name in _SOURCE_FACTS:
            original = getattr(module, name)
            namespace = {**original.__globals__, "subprocess": _ObservationSubprocess(module.subprocess, pins["helpers"])}
            self.functions[name] = types.FunctionType(original.__code__, namespace, original.__name__, original.__defaults__, original.__closure__)

    @classmethod
    def from_selected_launch(cls, launch: Mapping[str, Any]) -> "SelectedSourceObserver":
        import importlib.util
        import shutil
        import vibecomfy.runtime.session as module
        import vibecomfy.utils as utils
        root = utils.find_repo_root().resolve(strict=True)
        spec = importlib.util.find_spec("vibecomfy.commands.session")
        if spec is None or not spec.origin:
            raise EngineAdapterError("selected command source is unavailable")
        pins = {"version": SOURCE_FACT_VERSION, "source_root": str(root),
                "source_revision": launch["source_revision"], "source_content_digest": launch["source_content_digest"],
                "sources": {"runtime.session": _file_pin(Path(module.__file__)), "utils": _file_pin(Path(utils.__file__)),
                            "commands.session": _file_pin(Path(spec.origin))},
                "functions": {name: _function_pin(getattr(module, name)) for name in _SOURCE_FACTS},
                "helpers": {name: _file_pin(Path(shutil.which(name) or "").resolve(strict=True)) for name in ("git", "ps")}}
        if pins["sources"]["runtime.session"]["sha256"] != launch["adapter_pins"]["session_source_sha256"]:
            raise EngineAdapterError("selected observation session source differs")
        observer = cls(module, pins)
        for fact, expected in (("current_source_revision", launch["source_revision"]),
                               ("current_source_content_digest", launch["source_content_digest"])):
            if observer.observe(fact) != expected:
                raise EngineAdapterError("selected observation checkout identity differs")
        return observer

    def observe(self, fact: str, pid: int | None = None) -> str | None:
        validate_selected_observation_sources(self.module, self.pins)
        if fact not in _SOURCE_FACTS or (fact == "_process_start_identity" and (type(pid) is not int or pid < 1)) or (fact != "_process_start_identity" and pid is not None):
            raise EngineAdapterError("selected source fact is invalid")
        if fact == "_process_start_identity" and sys.platform.startswith("linux"):
            value = linux_registry_birth(pid)
        else:
            value = self.functions[fact](pid) if fact == "_process_start_identity" else self.functions[fact]()
        validate_selected_observation_sources(self.module, self.pins)
        if value is not None and (not isinstance(value, str) or not value or len(value.encode("utf-8")) > 1024):
            raise EngineAdapterError("selected source fact is not compact")
        return value


def linux_registry_birth(pid: int) -> str | None:
    """VibeComfy registry compatibility only; never a custody birth."""
    from runtime_protocol import process_binding
    try:
        return process_binding.birth_ticks(pid)
    except (OSError, AttributeError) as exc:
        raise EngineAdapterError("Linux registry birth observation unavailable") from exc


def install_selected_observation_boundary(module: Any, pins: Mapping[str, Any], control: Any) -> None:
    validate_selected_observation_sources(module, pins)
    module.current_source_revision = lambda: control.observe_source_fact("current_source_revision")
    module.current_source_content_digest = lambda: control.observe_source_fact("current_source_content_digest")
    module._process_start_identity = lambda pid: control.observe_source_fact("_process_start_identity", pid)
    module.subprocess = _ObservationSubprocess(module.subprocess)


class EngineControlClient:
    """One serialized private channel; uncertain delivery is never retried.

    The registered identity is pinned before exposing the proxy. The first
    use allows the host's readiness token binding; later expectations come
    only from successful v2 ACKs, never from another designation disk read.
    """

    def __init__(self, *, channel: Any, custody: Any, reference_reader: Callable[[], Mapping[str, Any]],
                 custody_scope: str, operation_id: str, channel_id: str, owner_epoch: str,
                 source_pins_digest: str | None = None, enable_exit_v2: bool = False):
        self.validate_scope(custody_scope)
        self.channel, self.custody, self.reference_reader = channel, custody, reference_reader
        self.scope = custody_scope
        self.binding = {"operation_id": operation_id, "channel_id": channel_id, "owner_epoch": owner_epoch}
        self._lock = threading.Lock()
        self._pin: dict[str, Any] | None = None
        self._current: dict[str, Any] | None = None
        self._error: BaseException | None = None
        self.failure_phase: str | None = None
        self._exit_ack: dict[str, Any] | None = None
        if type(enable_exit_v2) is not bool:
            raise EngineAdapterError("listener exit selection must be explicit")
        self._enable_exit_v2, self._source_pins_digest = enable_exit_v2, source_pins_digest

    @staticmethod
    def validate_scope(scope: str) -> None:
        if not isinstance(scope, str) or not scope or scope != os.path.abspath(scope):
            raise EngineAdapterError("private custody scope must be an exact normalized absolute path")

    def _copy(self, value: Any) -> Any:
        import json
        return json.loads(self.custody._canonical(value))

    def _validate_reference(self, value: Any) -> dict[str, Any]:
        if (not isinstance(value, dict)
                or set(value) != {"version", "scope_root", "role", "generation", "target"}
                or value.get("version") != self.custody.ROLE_REFERENCE_VERSION
                or value.get("scope_root") != self.scope or value.get("role") != "engine_listener"
                or type(value.get("generation")) is not int or value["generation"] < 1):
            raise EngineAdapterError("listener custody reference shape or scope is invalid")
        target = value["target"]
        if (not isinstance(target, dict)
                or set(target) != {"pid", "uid", "birth_id", "audit_token_sha256", "audit_token_pidversion"}
                or type(target.get("pid")) is not int or target["pid"] < 1
                or type(target.get("uid")) is not int or target["uid"] < 0
                or not isinstance(target.get("birth_id"), str) or not target["birth_id"]
                or type(target.get("audit_token_pidversion")) is not int or target["audit_token_pidversion"] < 1
                or not isinstance(target.get("audit_token_sha256"), str)
                or re.fullmatch(r"sha256:[0-9a-f]{64}", target["audit_token_sha256"]) is None):
            raise EngineAdapterError("listener custody reference incarnation is invalid")
        selected = self._copy(value)
        if self._pin is not None:
            fixed = {k: selected[k] for k in ("version", "scope_root", "role", "generation")}
            fixed["target"] = {k: target[k] for k in ("pid", "uid", "birth_id")}
            pinned = {k: self._pin[k] for k in ("version", "scope_root", "role", "generation")}
            pinned["target"] = {k: self._pin["target"][k] for k in ("pid", "uid", "birth_id")}
            if self.custody._canonical(fixed) != self.custody._canonical(pinned):
                raise EngineAdapterError("listener custody reference differs from registered pin")
        return selected

    def pin_reference(self, reference: Mapping[str, Any]) -> None:
        with self._lock:
            if self._pin is not None:
                raise EngineAdapterError("listener registration pin is already bound")
            self._pin = self._validate_reference(reference)

    def _selected(self, pid: int) -> dict[str, Any]:
        if self._pin is None:
            raise EngineAdapterError("listener registration pin is unavailable")
        if self._current is None:
            self._current = self._validate_reference(self.reference_reader())
        if type(pid) is not int or pid != self._current["target"]["pid"]:
            raise EngineAdapterError("listener request differs from retained process")
        return self._current

    def _validate_signal_ack(self, response: Any, request: dict[str, Any]) -> dict[str, Any]:
        echo = {k: v for k, v in request.items() if k != "command"}
        if (not isinstance(response, dict) or set(response) != set(echo) | {"status", "current_reference"}
                or response.get("status") != "signaled"
                or self.custody._canonical({k: response[k] for k in echo}) != self.custody._canonical(echo)):
            raise EngineAdapterError("listener signal acknowledgement is uncertain")
        current = self._validate_reference(response["current_reference"])
        old, new = request["target"], current["target"]
        if (current["generation"] != request["generation"]
                or self.custody._canonical({k: new[k] for k in ("pid", "uid", "birth_id")})
                != self.custody._canonical({k: old[k] for k in ("pid", "uid", "birth_id")})
                or (new["audit_token_sha256"] == old["audit_token_sha256"]
                    and new["audit_token_pidversion"] != old["audit_token_pidversion"])):
            raise EngineAdapterError("listener signal acknowledgement changes retained custody")
        return current

    def observe_source_fact(self, fact: str, pid: int | None = None) -> str | None:
        with self._lock:
            if self._error is not None or self._exit_ack is not None:
                raise EngineAdapterError("listener control is permanently fenced") from self._error
            phase = "not_sent"
            try:
                if (fact not in _SOURCE_FACTS
                        or not isinstance(self._source_pins_digest, str)
                        or re.fullmatch(r"sha256:[0-9a-f]{64}", self._source_pins_digest) is None
                        or (fact == "_process_start_identity" and (type(pid) is not int or pid < 1))
                        or (fact != "_process_start_identity" and pid is not None)):
                    raise EngineAdapterError("selected source observation is unavailable")
                request = {"version": SOURCE_FACT_VERSION, "command": "observe_selected_source_fact",
                           **self.binding, "engine_pid": os.getpid(), "source_pins_digest": self._source_pins_digest,
                           "fact": fact, "pid": pid}
                phase = "outcome_unknown"
                self.custody._send_frame(self.channel, request)
                timeout = self.channel.gettimeout() if hasattr(self.channel, "gettimeout") else None
                try:
                    if hasattr(self.channel, "settimeout"):
                        # The original content walk allows 60s. Keep that
                        # bound rather than imposing the signal ACK's 10s.
                        self.channel.settimeout(65 if fact == "current_source_content_digest" else 10)
                    response = self.custody._read_frame(self.channel)
                finally:
                    if hasattr(self.channel, "settimeout"):
                        self.channel.settimeout(timeout)
                echo = {k: v for k, v in request.items() if k != "command"}
                if (not isinstance(response, dict) or set(response) != set(echo) | {"status", "value"}
                        or response.get("status") != "observed"
                        or self.custody._canonical({k: response[k] for k in echo}) != self.custody._canonical(echo)):
                    raise EngineAdapterError("selected source observation acknowledgement is uncertain")
                value = response["value"]
                if (value is not None and (not isinstance(value, str) or not value or len(value.encode("utf-8")) > 1024)
                        or (fact == "current_source_content_digest" and value is not None
                            and re.fullmatch(r"sha256:[0-9a-f]{64}", value) is None)):
                    raise EngineAdapterError("selected source observation is not compact")
                return value
            except BaseException as exc:
                self._error, self.failure_phase = exc, phase
                raise EngineAdapterError("selected source observation failed; obligation retained") from exc

    def _validate_exit_v2_ack(self, response: Any, request: dict[str, Any]) -> dict[str, Any]:
        echo = {k: v for k, v in request.items() if k != "command"}
        if (not isinstance(response, dict)
                or set(response) != set(echo) | {"status", "current_reference", "revision", "refresh_proof_digest"}
                or response.get("status") != "exit_recorded"
                or self.custody._canonical({k: response[k] for k in echo}) != self.custody._canonical(echo)
                or type(response.get("revision")) is not int or response["revision"] < 0
                or not isinstance(response.get("refresh_proof_digest"), str)
                or re.fullmatch(r"sha256:[0-9a-f]{64}", response["refresh_proof_digest"]) is None):
            raise EngineAdapterError("retained listener exit acknowledgement is uncertain")
        current = self._validate_reference(response["current_reference"])
        if (current["generation"] != request["generation"]
                or any(current["target"][k] != request["target"][k] for k in ("pid", "uid", "birth_id"))
                or (current["target"]["audit_token_sha256"] == request["target"]["audit_token_sha256"]
                    and current["target"]["audit_token_pidversion"] != request["target"]["audit_token_pidversion"])):
            raise EngineAdapterError("retained listener exit acknowledgement changes custody")
        return current

    def _exchange(self, pid: int, *, signal_value: int | None = None, exit_code: int | None = None) -> None:
        with self._lock:
            if self._error is not None:
                raise EngineAdapterError("listener control is permanently fenced") from self._error
            if self._exit_ack is not None:
                raise EngineAdapterError("retained listener exit was already recorded")
            phase = "not_sent"
            try:
                selected = self._selected(pid)
                request = {**self.binding, "role": "engine_listener", "generation": selected["generation"],
                           "target": self._copy(selected["target"])}
                if signal_value is not None:
                    if isinstance(signal_value, bool) or not isinstance(signal_value, int) or signal_value not in {15, 9}:
                        raise EngineAdapterError("listener signal is outside the admitted set")
                    request.update(version=self.custody.ENGINE_CONTROL_SIGNAL_V2,
                                   command="signal_owned_listener", signal=int(signal_value))
                else:
                    if type(exit_code) is not int:
                        raise EngineAdapterError("retained listener exit code is unavailable")
                    request.update(version=LISTENER_EXIT_V2 if self._enable_exit_v2 else self.custody.ENGINE_CONTROL_VERSION,
                                   command="observe_owned_listener_exit", exit_code=exit_code)
                # sendall can fail after any prefix; conservatively fence even
                # failures that happen before a byte is reported as written.
                phase = "outcome_unknown"
                self.custody._send_frame(self.channel, request)
                response = self.custody._read_frame(self.channel)
                if signal_value is not None:
                    self._current = self._validate_signal_ack(response, request)
                else:
                    if self._enable_exit_v2:
                        self._current = self._validate_exit_v2_ack(response, request)
                    else:
                        expected = {**{k: v for k, v in request.items() if k != "command"}, "status": "exit_recorded"}
                        if self.custody._canonical(response) != self.custody._canonical(expected):
                            raise EngineAdapterError("retained listener exit acknowledgement is uncertain")
                    self._exit_ack = self._copy(response)
            except BaseException as exc:
                self._error, self.failure_phase = exc, phase
                raise EngineAdapterError("listener control failed; obligation retained") from exc

    def signal_owner(self, pid: int, signum: int) -> None:
        self._exchange(pid, signal_value=signum)

    def observe_exit(self, process: Any, code: int) -> None:
        self._exchange(process.pid, exit_code=code)


class CustodyProcessProxy:
    """Keep async observations/waits while routing every signal to the host."""

    def __init__(self, process: Any, signal_owner: Callable[[int, int], None], observe_exit: Callable[[Any, int], None] | None = None):
        self._process = process
        self._signal_owner = signal_owner
        self._observe_exit = observe_exit
        self._exit_sent = False

    @property
    def pid(self) -> int:
        return self._process.pid

    @property
    def returncode(self) -> int | None:
        return self._process.returncode

    @property
    def stdin(self):
        return self._process.stdin

    @property
    def stdout(self):
        return self._process.stdout

    @property
    def stderr(self):
        return self._process.stderr

    def _record_exit(self, code: int) -> None:
        if isinstance(code, bool) or not isinstance(code, int):
            raise EngineAdapterError("retained child exit code is unavailable")
        if self._observe_exit is not None and not self._exit_sent:
            self._observe_exit(self._process, code)
            self._exit_sent = True

    async def wait(self):
        code = await self._process.wait()
        self._record_exit(code)
        return code

    async def communicate(self, input=None):
        result = await self._process.communicate(input)
        code = await self._process.wait()
        self._record_exit(code)
        return result

    def send_signal(self, signum: int) -> None:
        self._signal_owner(self.pid, signum)

    def terminate(self) -> None:
        self.send_signal(platform.TERM)

    def kill(self) -> None:
        self.send_signal(platform.KILL)


class SelectedListenerLauncher:
    """One exact inherited-session listener launch with retained obligations."""

    def __init__(self, *, argv: Sequence[str], cwd: str,
                 launch: Callable[..., Awaitable[Any]],
                 register: Callable[[Any], Awaitable[None]],
                 signal_owner: Callable[[int, int], None],
                 observe_exit: Callable[[Any, int], None] | None = None):
        if not argv or any(not isinstance(part, str) or not part or "\0" in part for part in argv):
            raise EngineAdapterError("selected listener argv is invalid")
        if not Path(argv[0]).is_absolute() or not Path(cwd).is_absolute():
            raise EngineAdapterError("selected listener executable and cwd must be absolute")
        self.argv, self.cwd = tuple(argv), cwd
        self.launch, self.register, self.signal_owner = launch, register, signal_owner
        self.observe_exit = observe_exit
        self.process: Any = None
        self.error: BaseException | None = None
        self._launch_started = False

    async def __call__(self, *argv: str, **kwargs: Any):
        if self._launch_started:
            raise EngineAdapterError("selected listener launch already consumed")
        if tuple(argv) != self.argv or kwargs.get("cwd") != self.cwd:
            raise EngineAdapterError("listener launch differs from selected argv or cwd")
        if kwargs.get("start_new_session", False) or kwargs.get("process_group") is not None:
            raise EngineAdapterError("listener must preserve the inherited engine session")
        if set(kwargs) - {"stdout", "stderr", "env", "cwd", "start_new_session"}:
            raise EngineAdapterError("listener launch contains unselected options")
        self._launch_started = True  # Before await: concurrent/replayed calls cannot duplicate.
        try:
            self.process = await self.launch(*argv, **kwargs)
            # Retain the exact process before registration can fail; the host's
            # pending ledger and initiating error survive dropped Vibe refs.
            await self.register(self.process)
            return CustodyProcessProxy(self.process, self.signal_owner, self.observe_exit)
        except BaseException as exc:
            self.error = exc
            raise


class _ModuleAsyncioProxy:
    def __init__(self, original: Any, launcher: SelectedListenerLauncher):
        self._original, self.create_subprocess_exec = original, launcher

    def __getattr__(self, name: str):
        return getattr(self._original, name)


def install_selected_listener_boundary(module: Any, launcher: SelectedListenerLauncher,
                                       *, expected_source_hash: str,
                                       expected_spawn_hash: str,
                                       expected_cleanup_hashes: Mapping[str, str]) -> None:
    """Replace only the selected module's async-launch alias after exact hashes.

    Missing pins or a different supported seam fail before any engine launch.
    This never changes the process-global asyncio or subprocess modules.
    """
    source_path = Path(getattr(module, "__file__", ""))
    if not source_path.is_absolute() or source_path.is_symlink() or not source_path.is_file():
        raise EngineAdapterError("selected Vibe session source is unavailable")
    actual = "sha256:" + hashlib.sha256(source_path.read_bytes()).hexdigest()
    if actual != expected_source_hash:
        raise EngineAdapterError("selected Vibe session source hash differs")
    required_cleanup = {"_cleanup_spawn_failure", "_stop_managed_process"}
    if set(expected_cleanup_hashes) != required_cleanup:
        raise EngineAdapterError("selected Vibe cleanup pins are incomplete")
    for name, expected in {"_spawn_comfy_server": expected_spawn_hash, **expected_cleanup_hashes}.items():
        function = getattr(module, name, None)
        try:
            body = inspect.getsource(function).encode("utf-8")
        except (TypeError, OSError) as exc:
            raise EngineAdapterError("selected Vibe listener seam is unavailable") from exc
        if "sha256:" + hashlib.sha256(body).hexdigest() != expected:
            raise EngineAdapterError("selected Vibe launch/cleanup seam hash differs")
    spawn = getattr(module, "_spawn_comfy_server")
    if spawn.__globals__.get("asyncio") is not module.asyncio:
        raise EngineAdapterError("selected Vibe listener launch has no bounded module-local seam")
    module.asyncio = _ModuleAsyncioProxy(module.asyncio, launcher)


def engine_main(payload: Mapping[str, Any]) -> int:
    """Run the pinned daemon in its dedicated process and one local seam."""
    import asyncio
    import importlib.util
    import json
    import os
    import runpy
    import socket
    import sys
    import time

    EngineControlClient.validate_scope(payload.get("custody_scope"))
    broker_path = Path(__file__).with_name("custody_broker.py")
    spec = importlib.util.spec_from_file_location("_astrid_engine_custody", broker_path)
    if spec is None or spec.loader is None:
        raise EngineAdapterError("selected host custody consumer is unavailable")
    custody = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(custody)
    launch = payload["engine_launch"]
    pins = launch["adapter_pins"]
    if "sha256:" + hashlib.sha256(Path(__file__).read_bytes()).hexdigest() != pins["adapter_source_sha256"]:
        raise EngineAdapterError("selected host adapter artifact differs")
    descriptor = int(os.environ.pop("ASTRID_ENGINE_CONTROL_FD"))
    if descriptor < 3:
        raise EngineAdapterError("private engine control channel is invalid")
    channel = socket.socket(fileno=descriptor)
    channel.settimeout(10)
    original_async_launch = asyncio.create_subprocess_exec
    control = EngineControlClient(channel=channel, custody=custody,
                                  reference_reader=lambda: custody.RoleCustodyAuthority(
                                      Path(payload["custody_scope"]), "engine_listener").reference(),
                                  custody_scope=payload["custody_scope"],
                                  source_pins_digest="sha256:" + hashlib.sha256(custody._canonical(payload["source_observation_pins"])).hexdigest(),
                                  **{k: payload[k] for k in ("operation_id", "channel_id", "owner_epoch")})

    async def registered_launch(*argv: str, **kwargs: Any):
        registration = dict(payload["listener_registration"])
        # The exact selected argv is wrapped only for admitted pre/post-exec
        # registration. The listener keeps the engine's inherited session.
        env = {**kwargs.pop("env", os.environ), **registration}
        executable = payload["listener_wrapper_executable"]
        return await original_async_launch(*custody.custody_wrapper_argv(executable), env=env, **kwargs)

    async def registered(process: Any) -> None:
        authority = custody.RoleCustodyAuthority(Path(payload["custody_scope"]), "engine_listener")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            selected = authority.reference()
            if selected["target"].get("pid") == process.pid and "audit_token_sha256" in selected["target"]:
                # Pending pre-exec is insufficient: await the active post-exec
                # record before exposing a signal-capable proxy to Vibe.
                with authority._guard(exclusive=False):
                    if authority._read()["state"] == "active":
                        control.pin_reference(selected)
                        return
            await asyncio.sleep(0.005)
        raise EngineAdapterError("listener registration did not seal; obligation retained")

    import vibecomfy.runtime.session as session_module
    install_selected_observation_boundary(session_module, payload["source_observation_pins"], control)
    selected = SelectedListenerLauncher(argv=launch["listener_argv"], cwd=str(launch["config"]["cwd"]),
                                        launch=registered_launch, register=registered,
                                        signal_owner=control.signal_owner, observe_exit=control.observe_exit)
    install_selected_listener_boundary(session_module, selected,
                                       expected_source_hash=pins["session_source_sha256"],
                                       expected_spawn_hash=pins["spawn_sha256"],
                                       expected_cleanup_hashes={"_cleanup_spawn_failure": pins["cleanup_sha256"],
                                                                "_stop_managed_process": pins["stop_sha256"]})
    sys.argv = ["vibecomfy.commands.session", "--daemon", "--id", Path(launch["session_root"]).name,
                "--require-source-attestation", "--launch-token", payload["launch_token"],
                "--config", json.dumps(launch["config"], sort_keys=True, separators=(",", ":"))]
    runpy.run_module("vibecomfy.commands.session", run_name="__main__")
    return 0


if __name__ == "__main__":
    import json
    import sys
    if len(sys.argv) != 2:
        raise SystemExit("private engine adapter requires one selected payload")
    raise SystemExit(engine_main(json.loads(sys.argv[1])))
