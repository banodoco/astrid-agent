"""Q-owned composition boundary for the terminal M4 platform adapters.

The class in this module has three deliberately separate responsibilities:

* prepare the M4-4a platform boundary while failing closed on any missing
  first-party Pinokio runner or live negative-control setup seam;
* authenticate the exact installed/browser adapter used by M4-5; and
* drive the M4-6 native adapter through a pinned product command, one live
  action at a time.

It does not authenticate hosted evidence. It exposes immutable command/artifact
bindings to the native adapter's source-fixed B/platform live session, which must
authorize removal and finalize from live re-observation before the driver returns.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Callable

if __package__:
    from . import journey_release2, native_service_adapter, pinokio_platform_adapter
else:
    import journey_release2
    import native_service_adapter
    import pinokio_platform_adapter

PRODUCT_PROTOCOL = "astrid-m4-6-product-driver/v1"
ADOPTION_STAGES = frozenset({"pre-execution", "after-first-action", "after-retry-action"})
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
_CANDIDATE_FIELDS = frozenset({"app", "agent", "runtime", "pinokio", "manifest_sha256"})
_M4_5_SCENARIO_IDS = (
    "release2-button-direct-macos",
    "release2-install-line-direct-macos",
    "release2-pinokio-update-pinokio-backend-macos",
    "release2-pinokio-update-pinokio-fully-local-macos",
    "release2-button-direct-linux",
    "release2-install-line-direct-linux",
    "release2-pinokio-update-pinokio-backend-linux",
    "release2-pinokio-update-pinokio-fully-local-linux",
    "release2-button-direct-windows",
    "release2-install-line-direct-windows",
    "release2-pinokio-update-pinokio-backend-windows",
    "release2-pinokio-update-pinokio-fully-local-windows",
)
_FORBIDDEN_PROMOTION_FIELDS = frozenset(
    {
        "admission",
        "claimed",
        "fixture",
        "fixture_mode",
        "hosted",
        "hosted_admissible",
        "observation",
        "observations",
        "receipt",
        "verdict",
        "verifier",
    }
)


class JourneyDriverError(RuntimeError):
    """A product or adapter boundary is absent, foreign, or unsafe."""


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise JourneyDriverError(f"{label} must be an object")
    return dict(value)


def _exact_fields(value: object, fields: set[str], label: str) -> dict[str, Any]:
    row = _mapping(value, label)
    if set(row) != fields:
        raise JourneyDriverError(f"{label} fields must be exactly {sorted(fields)}")
    return row


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise JourneyDriverError(f"{label} must be a non-empty string")
    return value


def _digest(value: object, label: str) -> str:
    text = _text(value, label)
    if _SHA256.fullmatch(text) is None:
        raise JourneyDriverError(f"{label} must be a lowercase SHA-256 digest")
    return text


def _regular_file(value: object, label: str) -> Path:
    if not isinstance(value, (str, os.PathLike)) or not os.fspath(value):
        raise JourneyDriverError(f"{label} must be a non-empty path")
    path = Path(value)
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise JourneyDriverError(f"{label} must be an absolute regular non-symlink file")
    resolved = path.resolve(strict=True)
    if resolved != path.absolute():
        raise JourneyDriverError(f"{label} must not traverse a symlink")
    return resolved


def _directory(value: object, label: str) -> Path:
    if not isinstance(value, (str, os.PathLike)) or not os.fspath(value):
        raise JourneyDriverError(f"{label} must be a non-empty path")
    path = Path(value)
    if not path.is_absolute() or path.is_symlink() or not path.is_dir():
        raise JourneyDriverError(f"{label} must be an absolute non-symlink directory")
    resolved = path.resolve(strict=True)
    if resolved != path.absolute():
        raise JourneyDriverError(f"{label} must not traverse a symlink")
    return resolved


def _candidate(value: object, label: str) -> dict[str, str]:
    row = _mapping(value, label)
    if set(row) != _CANDIDATE_FIELDS:
        raise JourneyDriverError(f"{label} must contain the exact candidate tuple")
    for name in ("app", "agent", "runtime", "pinokio"):
        if not isinstance(row[name], str) or _GIT_SHA.fullmatch(row[name]) is None:
            raise JourneyDriverError(f"{label}.{name} must be a full lowercase commit SHA")
    _digest(row["manifest_sha256"], f"{label}.manifest_sha256")
    return {name: str(row[name]) for name in _CANDIDATE_FIELDS}


def _no_promoted_evidence(value: object, label: str) -> None:
    if isinstance(value, Mapping):
        forbidden = set(value) & _FORBIDDEN_PROMOTION_FIELDS
        if forbidden:
            raise JourneyDriverError(
                f"{label} attempts to promote forbidden evidence fields: {sorted(forbidden)}"
            )
        for name, child in value.items():
            _no_promoted_evidence(child, f"{label}.{name}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _no_promoted_evidence(child, f"{label}[{index}]")


def _git_blob(checkout: Path, ref: str, relative: str) -> bytes:
    try:
        return subprocess.check_output(
            ["git", "-C", str(checkout), "show", f"{ref}:{relative}"],
            stderr=subprocess.PIPE,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise JourneyDriverError(
            f"platform_adapter.stop_action file {relative!r} is absent at {ref}"
        ) from exc


class _PinokioAdoptionProvider:
    """Pinned, shell-free provider for a live Pinokio-launched Runtime."""

    def __init__(self, value: object) -> None:
        adoption = _exact_fields(
            value,
            {"command", "artifacts"},
            "platform_adapter.adoption",
        )
        artifacts = _mapping(adoption["artifacts"], "platform_adapter.adoption.artifacts")
        if not artifacts:
            raise JourneyDriverError("platform_adapter.adoption.artifacts must not be empty")
        pinned: dict[Path, str] = {}
        for name, raw in artifacts.items():
            _text(name, "platform_adapter.adoption artifact name")
            artifact = _exact_fields(
                raw,
                {"path", "sha256"},
                f"platform_adapter.adoption.artifacts.{name}",
            )
            path = _regular_file(
                artifact["path"],
                f"platform_adapter.adoption.artifacts.{name}.path",
            )
            expected = _digest(
                artifact["sha256"],
                f"platform_adapter.adoption.artifacts.{name}.sha256",
            )
            if path in pinned:
                raise JourneyDriverError(
                    "platform_adapter.adoption.artifacts contain a duplicate path"
                )
            if _sha256_bytes(path.read_bytes()) != expected:
                raise JourneyDriverError(
                    f"platform_adapter.adoption artifact {name!r} hash differs"
                )
            pinned[path] = expected

        command = _exact_fields(
            adoption["command"],
            {"argv", "cwd", "timeout_seconds"},
            "platform_adapter.adoption.command",
        )
        argv = command["argv"]
        if (
            not isinstance(argv, list)
            or not argv
            or not all(isinstance(item, str) and item for item in argv)
        ):
            raise JourneyDriverError("platform_adapter.adoption.command.argv must be explicit argv")
        executable = _regular_file(argv[0], "platform_adapter.adoption.command.argv[0]")
        if executable not in pinned:
            raise JourneyDriverError(
                "platform_adapter.adoption provider executable must be a pinned artifact"
            )
        if not os.access(executable, os.X_OK):
            raise JourneyDriverError(
                "platform_adapter.adoption provider executable must be executable"
            )
        if "-c" in argv[1:] or "--command" in argv[1:]:
            raise JourneyDriverError(
                "platform_adapter.adoption provider cannot use inline commands"
            )
        if executable.name.lower().startswith(("python", "pypy")):
            script = next((item for item in argv[1:] if not item.startswith("-")), None)
            if (
                script is None
                or _regular_file(script, "platform_adapter.adoption Python provider") not in pinned
            ):
                raise JourneyDriverError(
                    "platform_adapter.adoption Python provider must be a pinned artifact"
                )
        timeout = command["timeout_seconds"]
        if type(timeout) is not int or timeout <= 0 or timeout > 300:
            raise JourneyDriverError(
                "platform_adapter.adoption.command.timeout_seconds must be an integer from 1 to 300"
            )
        _no_promoted_evidence(adoption, "platform_adapter.adoption")
        self._argv = (str(executable), *argv[1:])
        self._cwd = _directory(command["cwd"], "platform_adapter.adoption.command.cwd")
        self._timeout = timeout
        self._artifacts = pinned

    def _revalidate(self) -> None:
        if _directory(self._cwd, "platform_adapter.adoption.command.cwd") != self._cwd:
            raise JourneyDriverError("platform_adapter.adoption command cwd changed identity")
        for path, expected in self._artifacts.items():
            if (
                _regular_file(path, f"platform_adapter.adoption artifact {path.name}") != path
                or _sha256_bytes(path.read_bytes()) != expected
            ):
                raise JourneyDriverError(
                    f"platform_adapter.adoption artifact {path.name!r} no longer matches its pin"
                )

    def invoke(self, stage: str) -> dict[str, Any] | None:
        if stage not in ADOPTION_STAGES:
            raise JourneyDriverError(f"unsupported Pinokio adoption stage: {stage!r}")
        self._revalidate()
        request = {"stage": stage}
        _no_promoted_evidence(request, "platform_adapter.adoption request")
        try:
            process = subprocess.run(
                self._argv,
                input=_canonical(request).decode("utf-8") + "\n",
                capture_output=True,
                text=True,
                timeout=self._timeout,
                cwd=self._cwd,
                env={},
                shell=False,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise JourneyDriverError(
                f"platform_adapter.adoption provider could not run at {stage}: {exc}"
            ) from exc
        if process.returncode != 0:
            detail = process.stderr.strip()
            raise JourneyDriverError(
                "platform_adapter.adoption provider exited "
                f"{process.returncode} at {stage}" + (f": {detail}" if detail else "")
            )
        try:
            response_value = json.loads(process.stdout)
        except json.JSONDecodeError as exc:
            raise JourneyDriverError(
                f"platform_adapter.adoption provider returned invalid JSON at {stage}"
            ) from exc
        _no_promoted_evidence(response_value, f"platform_adapter.adoption response at {stage}")
        response = _exact_fields(
            response_value,
            {
                "status",
                "pid",
                "identity",
                "instance_id",
                "controller_token_digest",
                "start_count",
            },
            f"platform_adapter.adoption response at {stage}",
        )
        status = response["status"]
        fields = (
            "pid",
            "identity",
            "instance_id",
            "controller_token_digest",
            "start_count",
        )
        if status == "absent":
            if any(response[name] is not None for name in fields):
                raise JourneyDriverError(
                    "platform_adapter.adoption absent response must contain only null "
                    "process fields"
                )
            return None
        if status != "running":
            raise JourneyDriverError(
                "platform_adapter.adoption response status must be absent or running"
            )
        pid = response["pid"]
        start_count = response["start_count"]
        if type(pid) is not int or pid <= 1:
            raise JourneyDriverError(
                "platform_adapter.adoption running pid must be an integer greater than one"
            )
        if type(start_count) is not int or start_count < 1:
            raise JourneyDriverError(
                "platform_adapter.adoption running start_count must be a positive integer"
            )
        _text(response["identity"], "platform_adapter.adoption response identity")
        _text(response["instance_id"], "platform_adapter.adoption response instance_id")
        _digest(
            response["controller_token_digest"],
            "platform_adapter.adoption response controller_token_digest",
        )
        return response


class JourneyDriver:
    """Concrete shared driver and stateless M4-4a/M4-5 composition helpers."""

    def __init__(
        self,
        config_path: Path | str | None = None,
        context: Mapping[str, Any] | None = None,
    ) -> None:
        self._sequence = 0
        self._context: dict[str, Any] | None = None
        self._candidate: dict[str, str] | None = None
        self._command: tuple[str, ...] | None = None
        self._command_cwd: Path | None = None
        self._timeout = 0
        self._artifacts: dict[Path, str] = {}
        self._artifact_roles: dict[str, Path] = {}
        if config_path is None and context is None:
            return
        if config_path is None or context is None:
            raise JourneyDriverError("native JourneyDriver requires both config path and context")
        self._configure_native(config_path, context)

    @staticmethod
    def compose_pinokio_backend_config(config: Mapping[str, Any]) -> dict[str, Any]:
        """Authenticate the Stop candidate and fail closed on the missing engine seam."""
        if not isinstance(config, Mapping):
            raise JourneyDriverError("M4-4a backend config must be an object")
        platform = _exact_fields(
            config.get("platform_adapter"),
            {"spec", "stop_action", "adoption"},
            "platform_adapter",
        )
        _PinokioAdoptionProvider(platform["adoption"])
        spec_path = _regular_file(platform["spec"], "platform_adapter.spec")
        stop = _exact_fields(
            platform["stop_action"],
            {"checkout", "ref", "file", "sha256"},
            "platform_adapter.stop_action",
        )
        candidates = _mapping(config.get("candidate_checkouts"), "candidate_checkouts")
        pinokio = _mapping(candidates.get("pinokio"), "candidate_checkouts.pinokio")
        candidate_checkout = _directory(pinokio.get("path"), "candidate_checkouts.pinokio.path")
        candidate_ref = _text(pinokio.get("ref"), "candidate_checkouts.pinokio.ref")
        if _GIT_SHA.fullmatch(candidate_ref) is None:
            raise JourneyDriverError("candidate_checkouts.pinokio.ref must be a full commit SHA")
        try:
            candidate_head = subprocess.check_output(
                ["git", "-C", str(candidate_checkout), "rev-parse", "HEAD"],
                stderr=subprocess.PIPE,
                text=True,
            ).strip()
        except (OSError, subprocess.CalledProcessError) as exc:
            raise JourneyDriverError("candidate_checkouts.pinokio is not a Git checkout") from exc
        if candidate_head != candidate_ref:
            raise JourneyDriverError(
                "candidate_checkouts.pinokio HEAD differs from its immutable ref"
            )
        descriptor_checkout = _directory(stop["checkout"], "platform_adapter.stop_action.checkout")
        descriptor_ref = _text(stop["ref"], "platform_adapter.stop_action.ref")
        if descriptor_checkout != candidate_checkout or descriptor_ref != candidate_ref:
            raise JourneyDriverError(
                "platform_adapter.stop_action must use the exact candidate Pinokio checkout/ref"
            )
        relative_text = _text(stop["file"], "platform_adapter.stop_action.file")
        relative = Path(relative_text)
        if relative.is_absolute() or relative.parts in {(), (".",)} or ".." in relative.parts:
            raise JourneyDriverError(
                "platform_adapter.stop_action.file must be a safe relative path"
            )
        if relative.as_posix() != "pinokio/stop.js":
            raise JourneyDriverError(
                "platform_adapter.stop_action.file must be the exact candidate path pinokio/stop.js"
            )
        expected_sha = _digest(stop["sha256"], "platform_adapter.stop_action.sha256")
        candidate_files = _mapping(pinokio.get("files"), "candidate_checkouts.pinokio.files")
        if candidate_files.get(relative.as_posix()) != expected_sha:
            raise JourneyDriverError(
                "platform_adapter.stop_action must be included in the candidate Pinokio file pins"
            )
        source = _git_blob(candidate_checkout, candidate_ref, relative.as_posix())
        if _sha256_bytes(source) != expected_sha:
            raise JourneyDriverError(
                "platform_adapter.stop_action hash differs from the immutable Git blob"
            )
        source_path = _regular_file(
            candidate_checkout / relative,
            "platform_adapter.stop_action tracked source",
        )
        if _sha256_bytes(source_path.read_bytes()) != expected_sha:
            raise JourneyDriverError(
                "platform_adapter.stop_action working-tree bytes differ from the immutable Git blob"
            )
        try:
            source_text_exact = source.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise JourneyDriverError("candidate pinokio/stop.js must be UTF-8") from exc
        semantic_entrypoint = re.compile(
            r"^[ \t]*module\.exports[ \t]*=[ \t]*\{[ \t]*run[ \t]*:[ \t]*"
            r"\[[ \t]*app\.customStep\(app\.stopOwnedStart\)[ \t]*\][ \t]*"
            r"\}[ \t]*;?[ \t]*$",
            re.MULTILINE,
        )
        if semantic_entrypoint.search(source_text_exact) is None:
            raise JourneyDriverError(
                "candidate pinokio/stop.js must invoke the semantic entrypoint "
                "app.customStep(app.stopOwnedStart)"
            )
        try:
            spec = json.loads(spec_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise JourneyDriverError(f"platform_adapter.spec cannot be read: {exc}") from exc
        spec = _mapping(spec, "platform_adapter.spec")
        controls = _mapping(spec.get("stop_controls"), "platform_adapter.spec.stop_controls")
        parsed: dict[str, tuple[list[str], str]] = {}
        for reason in ("busy", "unknown"):
            control = _mapping(
                controls.get(reason), f"platform_adapter.spec.stop_controls.{reason}"
            )
            argv = control.get("argv")
            if (
                not isinstance(argv, list)
                or not argv
                or not all(isinstance(item, str) and item for item in argv)
            ):
                raise JourneyDriverError(
                    f"platform_adapter.spec.stop_controls.{reason}.argv must be explicit argv"
                )
            cwd = _text(control.get("cwd"), f"platform_adapter.spec.stop_controls.{reason}.cwd")
            if str(source_path) not in argv:
                raise JourneyDriverError(
                    f"platform_adapter.spec.stop_controls.{reason}.argv must include the "
                    "tracked Stop action source"
                )
            parsed[reason] = (list(argv), cwd)
        if parsed["busy"] != parsed["unknown"]:
            raise JourneyDriverError(
                "busy and unknown Stop controls must use the same immutable argv/cwd"
            )
        argv = parsed["busy"][0]
        executable = Path(argv[0]).name.lower()
        if executable in {"echo", "false", "printf", "print", "true"}:
            raise JourneyDriverError("Stop control is a disposable print/exit command")
        if executable.startswith(("python", "pypy")) and "-c" in argv[1:]:
            raise JourneyDriverError("Stop control cannot use disposable python -c")
        source_text = source.decode("utf-8", errors="replace").lower()
        source_lines = [
            line.strip()
            for line in source_text.splitlines()
            if line.strip() and not line.lstrip().startswith(("#", "//"))
        ]
        disposable_source = any(
            marker in source_text
            for marker in (
                "raise systemexit",
                "sys.exit(",
                "console.log(",
                "print(",
                "process.exit(",
            )
        ) or any(
            re.match(r"^(?:exit|echo|printf)(?:\s|$)", line) is not None for line in source_lines
        )
        if len(source_lines) <= 3 and disposable_source:
            raise JourneyDriverError(
                "tracked Stop action source is an obvious disposable print/exit"
            )
        if executable in {"bash", "cmd", "cmd.exe", "powershell", "powershell.exe", "pwsh", "sh"}:
            lowered = " ".join(argv[1:]).lower()
            if "-c" in argv[1:] and any(word in lowered for word in ("exit", "echo", "printf")):
                raise JourneyDriverError("Stop control is a disposable print/exit command")

        if executable in {"node", "node.exe"} and argv[1:2] == [str(source_path)]:
            raise JourneyDriverError(
                "NEEDS: direct Node execution of pinokio/stop.js only loads its exported "
                "action and does not invoke the Pinokio action engine"
            )
        raise JourneyDriverError(
            "NEEDS: no locally pinned approved Pinokio action runner is available, and the "
            "frozen M4-4a adapter/backend expose no interface that establishes and restores "
            "the live busy and unknown preconditions around the immutable Stop action"
        )

    @staticmethod
    def execute_pinokio_backend(
        config: Mapping[str, Any],
        output: Path,
        backend_executor: Any,
    ) -> dict[str, Any]:
        """Execute through the existing backend with adoption at its action seams."""
        if not isinstance(config, Mapping):
            raise JourneyDriverError("M4-4a backend config must be an object")
        platform = _exact_fields(
            config.get("platform_adapter"),
            {"spec", "stop_action", "adoption"},
            "platform_adapter",
        )
        spec_path = _regular_file(platform["spec"], "platform_adapter.spec")
        try:
            spec_value = json.loads(spec_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise JourneyDriverError(f"platform_adapter.spec cannot be read: {exc}") from exc
        original_platform_spec = _mapping(spec_value, "platform_adapter.spec")
        provider = _PinokioAdoptionProvider(platform["adoption"])
        base = getattr(backend_executor, "PinokioBackendExecutor", None)
        if not isinstance(base, type):
            raise JourneyDriverError("backend executor does not expose PinokioBackendExecutor")
        original_actions = _canonical(config.get("actions"))
        original_old_launcher = _canonical(config.get("old_launcher"))
        original_config = copy.deepcopy(dict(config))

        class _AdoptingPinokioBackendExecutor(base):
            def _adopt(self, stage: str, *, allow_absent: bool) -> None:
                process = provider.invoke(stage)
                if process is None:
                    if allow_absent:
                        return
                    raise JourneyDriverError(
                        "platform_adapter.adoption did not find the Runtime after "
                        f"the successful {stage}"
                    )
                pinokio_platform_adapter.adopt_process(
                    copy.deepcopy(original_platform_spec),
                    pid=process["pid"],
                    identity=process["identity"],
                    instance_id=process["instance_id"],
                    controller_token_digest=process["controller_token_digest"],
                    start_count=process["start_count"],
                )

            def _run(self, command: Mapping[str, Any], *args: Any, **kwargs: Any) -> Any:
                result = super()._run(command, *args, **kwargs)
                if result.returncode == 0:
                    if command is self.data["first"]["command"]:
                        self._adopt("after-first-action", allow_absent=False)
                    elif command is self.data["retry"]["command"]:
                        self._adopt("after-retry-action", allow_absent=False)
                return result

            def execute(self) -> dict[str, Any]:
                self._adopt("pre-execution", allow_absent=True)
                return super().execute()

        receipt = _AdoptingPinokioBackendExecutor(config, output).execute()
        if _canonical(config.get("actions")) != original_actions:
            raise JourneyDriverError("adoption wrapper changed Pinokio action bytes")
        if _canonical(config.get("old_launcher")) != original_old_launcher:
            raise JourneyDriverError("adoption wrapper changed old-launcher provenance bytes")
        if dict(config) != original_config:
            raise JourneyDriverError("adoption wrapper mutated its input config")
        return receipt

    @staticmethod
    def boundary_command(
        boundary: Path | str,
        boundary_args: Sequence[str],
        evidence_kind: str,
    ) -> tuple[str, ...]:
        """Return an argv-safe M4-5 command bound to the checked-in adapter."""
        if isinstance(boundary_args, (str, bytes)) or not isinstance(boundary_args, Sequence):
            raise JourneyDriverError("m4_5_boundary_args must be an argv sequence")
        if any(not isinstance(value, str) or not value for value in boundary_args):
            raise JourneyDriverError("m4_5_boundary_args must contain non-empty strings")
        executable = _regular_file(boundary, "m4_5_boundary")
        args = list(boundary_args)
        adapter = Path(__file__).with_name("installed_browser_adapter.py").resolve(strict=True)
        matrix_ids = tuple(row["scenario_id"] for row in journey_release2.RELEASE2_SCENARIOS)
        if (
            journey_release2.RELEASE2_SCENARIO_IDS != _M4_5_SCENARIO_IDS
            or matrix_ids != _M4_5_SCENARIO_IDS
        ):
            raise JourneyDriverError("checked-in M4-5 aggregate is not the exact twelve-row matrix")
        if executable == adapter:
            if not os.access(executable, os.X_OK):
                raise JourneyDriverError("checked-in installed/browser adapter is not executable")
        else:
            if not os.access(executable, os.X_OK):
                raise JourneyDriverError("m4_5_boundary must be executable")
            approved_python = Path(sys.executable).resolve(strict=True)
            if executable != approved_python:
                raise JourneyDriverError(
                    "m4_5_boundary Python executable must be this driver's exact interpreter"
                )
            if not args or Path(args[0]).resolve(strict=False) != adapter:
                raise JourneyDriverError(
                    "the first Python script argument must be the exact checked-in "
                    "installed_browser_adapter.py"
                )
        expected_adapter_kind = {
            "hosted-lifecycle": "installed-executable",
            "disposable-executable": "disposable-executable",
        }.get(evidence_kind)
        if expected_adapter_kind is None:
            raise JourneyDriverError(f"unsupported M4-5 evidence kind: {evidence_kind!r}")
        values: list[str] = []
        for index, value in enumerate(args):
            if value == "--evidence-kind":
                if index + 1 >= len(args):
                    raise JourneyDriverError("installed/browser --evidence-kind needs a value")
                values.append(args[index + 1])
            elif value.startswith("--evidence-kind="):
                values.append(value.split("=", 1)[1])
        if values != [expected_adapter_kind]:
            raise JourneyDriverError(
                "installed/browser --evidence-kind must exactly match the outer evidence class"
            )
        return (str(executable), *args)

    def _configure_native(self, config_path: Path | str, context: Mapping[str, Any]) -> None:
        path = _regular_file(config_path, "native journey-driver config")
        try:
            config_value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise JourneyDriverError(f"native journey-driver config cannot be read: {exc}") from exc
        config = _exact_fields(
            config_value,
            {"protocol", "candidate", "product_command", "artifacts"},
            "native journey-driver config",
        )
        if config["protocol"] != PRODUCT_PROTOCOL:
            raise JourneyDriverError(f"native product protocol must be {PRODUCT_PROTOCOL}")
        candidate = _candidate(config["candidate"], "native config candidate")
        context_candidate = _candidate(context.get("candidate"), "native context candidate")
        if candidate != context_candidate:
            raise JourneyDriverError(
                "native product command candidate differs from exact context tuple"
            )
        artifacts = _mapping(config["artifacts"], "native config artifacts")
        if not artifacts:
            raise JourneyDriverError("native config artifacts must not be empty")
        pinned: dict[Path, str] = {}
        roles: dict[str, Path] = {}
        for name, value in artifacts.items():
            _text(name, "native artifact name")
            artifact = _exact_fields(value, {"path", "sha256"}, f"native config artifacts.{name}")
            artifact_path = _regular_file(artifact["path"], f"native config artifacts.{name}.path")
            expected = _digest(artifact["sha256"], f"native config artifacts.{name}.sha256")
            if _sha256_bytes(artifact_path.read_bytes()) != expected:
                raise JourneyDriverError(f"native artifact {name!r} hash differs")
            if artifact_path in pinned:
                raise JourneyDriverError("native config artifacts contain a duplicate path")
            pinned[artifact_path] = expected
            roles[name] = artifact_path
        command = _exact_fields(
            config["product_command"],
            {"argv", "cwd", "timeout_seconds"},
            "native config product_command",
        )
        argv = command["argv"]
        if (
            not isinstance(argv, list)
            or not argv
            or not all(isinstance(value, str) and value for value in argv)
        ):
            raise JourneyDriverError("native product_command.argv must be explicit argv")
        command_path = _regular_file(argv[0], "native product_command.argv[0]")
        if command_path not in pinned:
            raise JourneyDriverError("native product command executable is not a pinned artifact")
        if not os.access(command_path, os.X_OK):
            raise JourneyDriverError("native product command executable is not executable")
        if "-c" in argv[1:] or "--command" in argv[1:]:
            raise JourneyDriverError(
                "native product command cannot use an inline shell/interpreter"
            )
        if command_path.name.lower().startswith(("python", "pypy")):
            script = next((value for value in argv[1:] if not value.startswith("-")), None)
            if (
                script is None
                or _regular_file(script, "native product Python script") not in pinned
            ):
                raise JourneyDriverError("native product Python script must be a pinned artifact")
        timeout = command["timeout_seconds"]
        if type(timeout) is not int or timeout <= 0 or timeout > 300:
            raise JourneyDriverError(
                "native product_command.timeout_seconds must be an integer from 1 to 300"
            )
        _no_promoted_evidence(config, "native config")
        initial_contract = _mapping(context.get("contract"), "native context contract")
        for name in ("os", "mode"):
            _text(initial_contract.get(name), f"native context contract.{name}")
        self._context = copy.deepcopy(dict(context))
        self._candidate = candidate
        self._command = (str(command_path), *argv[1:])
        self._command_cwd = _directory(command["cwd"], "native product_command.cwd")
        self._timeout = timeout
        self._artifacts = pinned
        self._artifact_roles = roles

    def _verify_artifact(self, path: Path, label: str) -> None:
        expected = self._artifacts.get(path)
        if expected is None or _sha256_bytes(path.read_bytes()) != expected:
            raise JourneyDriverError(f"{label} no longer matches its pinned artifact")

    def _require_native(self) -> None:
        if (
            self._context is None
            or self._candidate is None
            or self._command is None
            or self._command_cwd is None
        ):
            raise JourneyDriverError("NEEDS: pinned M4-6 product-driver config and context")

    def _check_context(self, context: Mapping[str, Any] | None) -> None:
        self._require_native()
        if context is None:
            return
        assert self._candidate is not None
        if _candidate(context.get("candidate"), "native call context candidate") != self._candidate:
            raise JourneyDriverError("native call context changed the exact candidate tuple")
        assert self._context is not None
        for name in ("slot_id", "canonical_definition_path", "principal"):
            if context.get(name) != self._context.get(name):
                raise JourneyDriverError(f"native call context changed {name}")
        expected_contract = _mapping(self._context.get("contract"), "native stored contract")
        actual_contract = _mapping(context.get("contract"), "native call contract")
        for name in ("os", "mode"):
            if actual_contract.get(name) != expected_contract.get(name):
                raise JourneyDriverError(f"native call context changed contract.{name}")

    def _invoke(
        self,
        action: str,
        *,
        payload: Mapping[str, Any] | None = None,
        fields: set[str],
    ) -> dict[str, Any]:
        self._require_native()
        assert self._context is not None
        assert self._candidate is not None
        assert self._command is not None
        assert self._command_cwd is not None
        for artifact_path in self._artifacts:
            self._verify_artifact(artifact_path, f"native artifact {artifact_path.name}")
        self._sequence += 1
        request = {
            "protocol": PRODUCT_PROTOCOL,
            "sequence": self._sequence,
            "action": action,
            "candidate": copy.deepcopy(self._candidate),
            "context": {
                "slot_id": self._context.get("slot_id"),
                "os": _mapping(self._context.get("contract"), "native context contract").get("os"),
                "mode": _mapping(self._context.get("contract"), "native context contract").get(
                    "mode"
                ),
            },
            "payload": copy.deepcopy(dict(payload or {})),
        }
        _no_promoted_evidence(request, f"product action {action!r} request")
        inherited_names = (
            "PATH",
            "SYSTEMROOT",
            "WINDIR",
            "TEMP",
            "TMP",
            "HOME",
            "USERPROFILE",
            "LANG",
        )
        product_environment = {
            name: os.environ[name] for name in inherited_names if name in os.environ
        }
        try:
            process = subprocess.run(
                self._command,
                input=_canonical(request).decode("utf-8") + "\n",
                capture_output=True,
                text=True,
                timeout=self._timeout,
                cwd=self._command_cwd,
                env=product_environment,
                shell=False,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise JourneyDriverError(f"product action {action!r} could not run: {exc}") from exc
        if process.returncode != 0:
            detail = process.stderr.strip()
            raise JourneyDriverError(
                f"product action {action!r} exited {process.returncode}"
                + (f": {detail}" if detail else "")
            )
        try:
            response_value = json.loads(process.stdout)
        except json.JSONDecodeError as exc:
            raise JourneyDriverError(f"product action {action!r} returned invalid JSON") from exc
        response = _exact_fields(
            response_value,
            {"protocol", "sequence", "action", "status", *fields},
            f"product action {action!r} response",
        )
        _no_promoted_evidence(response, f"product action {action!r} response")
        expected = {
            "protocol": PRODUCT_PROTOCOL,
            "sequence": self._sequence,
            "action": action,
            "status": "pass",
        }
        for name, value in expected.items():
            if response.get(name) != value:
                raise JourneyDriverError(
                    f"product action {action!r} response {name} differs from request"
                )
        return response

    def _discovery_action(
        self,
        action: str,
        *,
        payload: Mapping[str, Any] | None = None,
        scenario: str | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        fields = {"discovery"} | ({"scenario"} if scenario is not None else set())
        response = self._invoke(action, payload=payload, fields=fields)
        discovery = _mapping(response["discovery"], f"product action {action!r} discovery")
        if scenario is None:
            return discovery, None
        scenario_row = _exact_fields(
            response["scenario"], {"name", "value"}, f"product action {action!r} scenario"
        )
        if scenario_row["name"] != scenario:
            raise JourneyDriverError(
                f"product action {action!r} returned scenario {scenario_row['name']!r}; "
                f"expected {scenario!r}"
            )
        value = _mapping(scenario_row["value"], f"product action {action!r} scenario.value")
        return discovery, copy.deepcopy(value)

    def service_spec(
        self, context: Mapping[str, Any] | None = None
    ) -> native_service_adapter.ServiceSpec:
        self._check_context(context)
        response = self._invoke("service-spec", fields={"service_spec"})
        row = _exact_fields(
            response["service_spec"],
            {
                "definition_path",
                "definition_sha256",
                "action",
                "principal",
                "definition_owner",
                "working_directory",
                "removal_plan",
            },
            "product service_spec",
        )
        definition = _regular_file(row["definition_path"], "product service definition")
        definition_sha = _digest(row["definition_sha256"], "product service definition sha256")
        if self._artifacts.get(definition) != definition_sha:
            raise JourneyDriverError("product service definition is not an exact pinned artifact")
        self._verify_artifact(definition, "product service definition")
        action = row["action"]
        if (
            not isinstance(action, list)
            or not action
            or not all(isinstance(value, str) and value for value in action)
        ):
            raise JourneyDriverError("product service action must be explicit argv")
        action_executable = _regular_file(action[0], "product service action executable")
        if action_executable not in self._artifacts:
            raise JourneyDriverError("product service action executable is not pinned")
        self._verify_artifact(action_executable, "product service action executable")
        removal_value = _exact_fields(
            row["removal_plan"],
            {
                "operation",
                "keep_data",
                "remove_paths",
                "preserve_paths",
                "expected_postconditions",
            },
            "product removal_plan",
        )

        def path_tuple(name: str) -> tuple[str, ...]:
            values = removal_value[name]
            if (
                not isinstance(values, list)
                or not values
                or not all(isinstance(value, str) and value for value in values)
            ):
                raise JourneyDriverError(f"product removal_plan.{name} must be a string list")
            return tuple(values)

        postconditions = removal_value["expected_postconditions"]
        if not isinstance(postconditions, list) or any(
            not isinstance(value, str) or not value for value in postconditions
        ):
            raise JourneyDriverError(
                "product removal_plan.expected_postconditions must be a string list"
            )
        try:
            removal_plan = native_service_adapter.RemovalPlan(
                operation=_text(removal_value["operation"], "product removal operation"),
                keep_data=removal_value["keep_data"],
                remove_paths=path_tuple("remove_paths"),
                preserve_paths=path_tuple("preserve_paths"),
                expected_postconditions=tuple(postconditions),
            )
        except ValueError as exc:
            raise JourneyDriverError(f"product removal_plan is invalid: {exc}") from exc
        return native_service_adapter.ServiceSpec(
            definition_path=definition,
            definition_sha256=definition_sha,
            action=(str(action_executable), *action[1:]),
            principal=_text(row["principal"], "product service principal"),
            definition_owner=_text(row["definition_owner"], "product service definition owner"),
            working_directory=_text(row["working_directory"], "product service working directory")
            if row["working_directory"]
            else "",
            removal_plan=removal_plan,
        )

    def immutable_platform_artifacts(self, context: Mapping[str, Any]) -> Mapping[str, Any]:
        """Return the closed, revalidated phase-1 command/artifact binding."""
        self._check_context(context)
        assert self._command is not None
        assert self._command_cwd is not None
        for artifact_path in self._artifacts:
            self._verify_artifact(artifact_path, f"native artifact {artifact_path.name}")
        return {
            "protocol": PRODUCT_PROTOCOL,
            "product_command": {
                "argv": list(self._command),
                "cwd": str(self._command_cwd),
            },
            "artifacts": [
                {
                    "role": role,
                    "path": str(path),
                    "sha256": self._artifacts[path],
                }
                for role, path in sorted(self._artifact_roles.items())
            ],
        }

    def run(
        self,
        context: Mapping[str, Any],
        session: native_service_adapter.NativeSession,
    ) -> Mapping[str, Any]:
        """Run four native scenarios through live product actions and NativeSession."""
        self._check_context(context)
        values: dict[str, Any] = {}

        before_discovery, _ = self._discovery_action("interruption-recovery-before")
        before = session.observe("interruption_recovery", "before", before_discovery)
        interrupted_controller, _ = self._discovery_action("interruption-controller")
        interrupted_authorization = session.before_external_controller_interruption(
            interrupted_controller
        )
        self._invoke(
            "interrupt-controller",
            fields=set(),
        )
        session.observe_external_controller_interrupted(interrupted_authorization)
        session.terminate_owned_runtime(before, before_discovery)
        after_discovery, interruption = self._discovery_action(
            "interruption-recovery-after",
            scenario="interruption_recovery",
        )
        session.observe("interruption_recovery", "after", after_discovery)
        recovery_controller, _ = self._discovery_action("recovery-controller")
        recovery_authorization = session.before_external_recovery_controller(recovery_controller)
        self._invoke(
            "complete-recovery-controller",
            fields=set(),
        )
        session.observe_external_recovery_controller_complete(recovery_authorization)
        assert interruption is not None
        values["interruption_recovery"] = interruption

        rollback_discovery, rollback = self._discovery_action(
            "failed-candidate-rollback", scenario="failed_candidate_rollback"
        )
        session.observe("failed_candidate_rollback", "after", rollback_discovery)
        assert rollback is not None
        values["failed_candidate_rollback"] = rollback

        stop_cases: dict[str, Any] = {}
        for outcome in native_service_adapter.failure.STOP_OUTCOMES:
            scenario_name = f"stop_safety.{outcome}"
            stop_before_discovery, _ = self._discovery_action(f"stop-safety-{outcome}-before")
            session.observe(scenario_name, "before", stop_before_discovery)
            stop_after_discovery, case = self._discovery_action(
                f"stop-safety-{outcome}",
                scenario=scenario_name,
            )
            session.observe(scenario_name, "after", stop_after_discovery)
            assert case is not None
            stop_cases[outcome] = case
        values["stop_safety"] = {"status": "pass", "cases": stop_cases}

        removal_discovery, _ = self._discovery_action("keep-data-uninstall-before")
        current = session.observe("keep_data_uninstall", "before", removal_discovery)
        session.before_product_removal(current, removal_discovery)
        removal_response = self._invoke("remove-product", fields={"scenario"})
        removal_scenario = _exact_fields(
            removal_response["scenario"],
            {"name", "value"},
            "product remove-product scenario",
        )
        if removal_scenario["name"] != "keep_data_uninstall":
            raise JourneyDriverError("remove-product returned the wrong scenario")
        values["keep_data_uninstall"] = copy.deepcopy(
            _mapping(removal_scenario["value"], "remove-product scenario.value")
        )
        session.observe_product_removal()
        return values


def journey_driver_factory(
    config_path: Path | str,
) -> Callable[[Mapping[str, Any]], JourneyDriver]:
    """Return the closure expected by native_service_adapter registration."""
    frozen_path = _regular_file(config_path, "native journey-driver config")

    def factory(context: Mapping[str, Any]) -> JourneyDriver:
        return JourneyDriver(frozen_path, context)

    return factory
