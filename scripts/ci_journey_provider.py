"""R-24 typed provider wire contract. Parsing is not evidence authentication.

No provider is approved here, and this module does not authorize execution or
hosted admission. A source-backed provider and independently authenticated
observation custody must be agreed before the driver can consume these records.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

REQUEST_SCHEMA = "astrid.r24.provider-request.v1"
RESULT_SCHEMA = "astrid.r24.provider-result.v1"
_SHA = re.compile(r"[0-9a-f]{40}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_RUN = re.compile(r"[0-9a-f]{32}\Z")


class ProviderContractError(ValueError):
    """Malformed, mismatched, incomplete, or unsafe provider message."""


class ProviderInvocationError(ProviderContractError):
    """A failed invocation, retaining its separate untrusted diagnostics."""

    def __init__(self, message: str, stderr: bytes, *, stdout: bytes = b"",
                 returncode: int | None = None):
        super().__init__(message)
        self.stderr = stderr
        self.stdout = stdout
        self.returncode = returncode


def _object(value: Any, fields: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise ProviderContractError(f"{label} requires exactly {sorted(fields)}")
    return value


def _match(value: Any, pattern: re.Pattern[str], label: str) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ProviderContractError(f"invalid {label}")
    return value


def _path(value: Any) -> str:
    if (not isinstance(value, str) or not value or value.startswith("/")
            or "\\" in value or any(p in {"", ".", ".."} for p in value.split("/"))):
        raise ProviderContractError("invalid provider source path")
    return value


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ProviderContractError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _decode(raw: str | bytes) -> Any:
    if not isinstance(raw, (str, bytes)):
        raise ProviderContractError("message must be JSON bytes or text")
    try:
        return json.loads(raw, object_pairs_hook=_unique_object,
                          parse_constant=lambda _: (_ for _ in ()).throw(ProviderContractError("invalid JSON constant")))
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ProviderContractError(f"invalid JSON: {exc}") from exc


@dataclass(frozen=True)
class ProviderRequest:
    """Immutable canonical request, created from the independently selected inputs."""

    canonical: bytes
    sha256: str

    @classmethod
    def parse(cls, raw: str | bytes) -> "ProviderRequest":
        request = _object(_decode(raw), {"schema", "candidate", "provider", "stop", "config_sha256", "run_id"}, "request")
        if request["schema"] != REQUEST_SCHEMA:
            raise ProviderContractError("unsupported request schema")
        candidate = _object(request["candidate"], {"app", "agent", "runtime", "pinokio", "manifest_sha256"}, "candidate")
        for key in ("app", "agent", "runtime", "pinokio"):
            _match(candidate[key], _SHA, f"candidate.{key}")
        _match(candidate["manifest_sha256"], _DIGEST, "manifest digest")
        provider = _object(request["provider"], {"ref", "sources"}, "provider")
        _match(provider["ref"], _SHA, "provider ref")
        sources = provider["sources"]
        if not isinstance(sources, dict) or not sources:
            raise ProviderContractError("provider sources must be a non-empty object")
        for path, digest in sources.items():
            _path(path)
            _match(digest, _DIGEST, "provider source digest")
        stop = _object(request["stop"], {"ref", "path", "sha256"}, "stop")
        if stop["ref"] != candidate["pinokio"] or stop["path"] != "pinokio/stop.js":
            raise ProviderContractError("Stop identity differs from the candidate")
        _match(stop["sha256"], _DIGEST, "Stop digest")
        _match(request["config_sha256"], _DIGEST, "config digest")
        _match(request["run_id"], _RUN, "run ID")
        canonical = _canonical(request)
        return cls(canonical, hashlib.sha256(canonical).hexdigest())


@dataclass(frozen=True)
class Observation:
    kind: str
    source: str
    evidence_sha256: str


@dataclass(frozen=True)
class ProviderResult:
    """Structurally valid claims, not authenticated observations or a receipt."""

    request_sha256: str
    observations: tuple[Observation, ...]

    @classmethod
    def parse(cls, raw: str | bytes, request: ProviderRequest) -> "ProviderResult":
        if not isinstance(request, ProviderRequest):
            raise ProviderContractError("independently selected request required")
        result = _object(_decode(raw), {"schema", "request_sha256", "observations"}, "result")
        if result["schema"] != RESULT_SCHEMA or result["request_sha256"] != request.sha256:
            raise ProviderContractError("result schema or request binding differs")
        rows = result["observations"]
        if not isinstance(rows, list) or len(rows) != 3:
            raise ProviderContractError("exactly three observations are required")
        sources = json.loads(request.canonical)["provider"]["sources"]
        observations = []
        kinds = set()
        for raw_row in rows:
            row = _object(raw_row, {"kind", "source", "evidence_sha256", "safe"}, "observation")
            kind = row["kind"]
            if not isinstance(kind, str) or kind not in {"busy", "unknown", "restoration"} or kind in kinds:
                raise ProviderContractError("missing, duplicate, or unknown observation")
            kinds.add(kind)
            if not isinstance(row["source"], str) or row["source"] not in sources:
                raise ProviderContractError("observation source is not pinned")
            if row["safe"] is not True:
                raise ProviderContractError("unsafe observation")
            digest = _match(row["evidence_sha256"], _DIGEST, "evidence digest")
            observations.append(Observation(kind, row["source"], digest))
        if kinds != {"busy", "unknown", "restoration"}:
            raise ProviderContractError("incomplete observations")
        return cls(request.sha256, tuple(observations))

# This immutable producer implements only the diagnostic API below. It does not
# supply session identity, qualifying observations, or authenticated custody.
# Consequently this entry point must never be used to open the Stop route.
_PRODUCER_REF = "a51721d482117158ddf809f1449047c37a803c01"
_PRODUCER_FILES = {
    "pinokio/providers/r24-linux.py": "70bae007103ddbc43ae6d2f667c833497e11646daa7dd9a7b544ad7cb2ed0e5c",
    "pinokio/providers/r24-action.cjs": "679f07658574667fbcc6f92392fe01a85d127a9889569e3771143536bf52b1bb",
    "pinokio/providers/r24-linux-binding.json": "aa081980bb143ea67eac9e36ca567af8f54a3cdcf7c6593ee130ed81a4d86d87",
    "pinokio/providers/r24-schema.json": "6cd5dbc55a11f607f184c6e1ce281ea10633b01fb1a739df4b12673525d48421",
}
# The diagnostic producer above is retained as a separate, non-admission path.
# This live producer is an immutable invocation input, not a release pin or
# evidence authority.  Its binding selects the published release tuple below.
LIVE_PRODUCER_REF = "7bb0ae88721fdd6b2d7b948760d886eeda9df282"
LIVE_PRODUCER_FILES = {
    "pinokio/providers/r24-linux.py": "978b52548281250ed264d7f08d8c8179419a1aa11035ddb1d6450960839e0dc9",
    "pinokio/providers/r24-action.cjs": "2901c4e75b11a96547d2033e62118ee64247670b8f9531fc6bbad9fb1b34be01",
    "pinokio/providers/r24-live-action.js": "32778884de8fd1e615cfbbb8d56f79aa9b57e7ddb5eb6239621d01de2e00db92",
    "pinokio/providers/r24-linux-binding.json": "3cfde4fd777a6e285f07f96a97584966bd137c1d3ccfc1bd1f58f377b9b047ce",
    "pinokio/providers/r24-schema.json": "233f989b29f54d082e1287c200b70b14b0315a9d763d05bcff824e556c648ffc",
}
LIVE_INTERFACE_FILES = {
    "pinokio/providers/R24-LINUX.md": "4a5efde487359bdfbc557da475ef4a180ec0397bd038a743e38599fd126001a2",
}
# Preserve the internal names used by the R3 focused fixtures.
_SUCCESS_PRODUCER_REF = LIVE_PRODUCER_REF
_SUCCESS_PRODUCER_FILES = LIVE_PRODUCER_FILES
# The diagnostic producer remains intentionally bound to its own old candidate,
# while the live route has an independently pinned current release binding.
_CANDIDATE = {
    "app": "5c95abb00525e1a77d00c32042b6453d9f14f4e7",
    "agent": "e425903f7a9fa5b82973281d15054c0716b46e2d",
    "runtime": "5453366458f7b4ee874c94d15ae7952e3e82d3c3",
    "pinokio": "2f085c5fc0cde192ac4a022065883c85dcccd0de",
    "manifest_sha256": "a6eef2804e58e198d56899f1b36d97688d5cbc4a0847a82078ba144df4a207eb",
}
_STOP = {"ref": _CANDIDATE["pinokio"], "path": "pinokio/stop.js",
         "sha256": "f9d62e4e6a826fccd9791ee211dae933f46c8f99019e145abb3db3bf9595fd48"}
LIVE_CANDIDATE = {
    "app": "933c53a6fb1a11e5f8a01335d6252016d703beec",
    "agent": "ab43bad818ef65ca1e929b973a2f61cd9fc4bf61",
    "runtime": "2776ba973b6c8f3555b5629b0840d9691a98d3c2",
    "pinokio": "3a6a98ae251ca126505245286e2d3e760f4348e9",
    "manifest_sha256": "b2584f0bb5a949890364004572a0182f2b568adcdba061e73b739e1d3d2d623f",
}
LIVE_STOP = {"ref": LIVE_CANDIDATE["pinokio"], "path": "pinokio/stop.js",
             "sha256": "f9d62e4e6a826fccd9791ee211dae933f46c8f99019e145abb3db3bf9595fd48"}


@dataclass(frozen=True)
class DiagnosticResult:
    """A source-backed diagnostic, never an admission or custody assertion."""

    request_sha256: str
    status: str = "not-qualified"


def _invoke_linux(request: ProviderRequest, config_json: str | bytes,
                  producer_root: str, producer_ref: str, producer_files: dict[str, str],
                  candidate: dict[str, str], stop: dict[str, str],
                  *, live_config: bool = False, export_directory: str | None = None):
    """Validate immutable inputs and run one producer with separate streams."""
    import os
    from pathlib import Path
    import subprocess
    import sys
    import tempfile

    if not isinstance(request, ProviderRequest):
        raise ProviderContractError("typed request required")
    parsed = ProviderRequest.parse(request.canonical)
    if parsed != request:
        raise ProviderContractError("request integrity mismatch")
    data = _decode(request.canonical)
    if (data["candidate"] != candidate or data["stop"] != stop or
            data["provider"] != {"ref": producer_ref, "sources": producer_files}):
        raise ProviderContractError("producer or candidate binding mismatch")
    config = _decode(config_json)
    config_fields = {"checkouts", "pterm", "node", "node_sha256",
                     "pterm_entry_sha256", "unws_sha256"}
    if live_config:
        config_fields |= {"pinokiod", "engine", "scratch_root"}
    _object(config, config_fields, "config")
    _object(config["checkouts"], {"app", "agent", "runtime", "pinokio"}, "checkouts")
    if live_config:
        _object(config["engine"], {"pid", "boot_id", "start_ticks", "pinokio_home", "port"},
                "engine")
    if hashlib.sha256(_canonical(config)).hexdigest() != data["config_sha256"]:
        raise ProviderContractError("config digest mismatch")
    if not isinstance(producer_root, str) or not os.path.isabs(producer_root):
        raise ProviderContractError("absolute producer checkout required")
    root = Path(producer_root)
    try:
        if root.is_symlink() or not root.is_dir():
            raise ProviderContractError("invalid producer checkout")
        root = root.resolve(strict=True)
        def git(*args: str) -> bytes:
            return subprocess.check_output(["git", "-C", str(root), *args],
                                           stderr=subprocess.DEVNULL, timeout=30).strip()
        if (Path(os.fsdecode(git("rev-parse", "--show-toplevel"))).resolve() != root or
                git("rev-parse", "HEAD").decode() != producer_ref or
                git("status", "--porcelain=v1", "--untracked-files=all")):
            raise ProviderContractError("producer checkout identity or cleanliness mismatch")
        for rel, digest in producer_files.items():
            path = root / rel
            if path.is_symlink() or not path.is_file():
                raise ProviderContractError("producer source missing")
            blob = subprocess.check_output(["git", "-C", str(root), "show", f"{producer_ref}:{rel}"],
                                           stderr=subprocess.DEVNULL, timeout=30)
            if (hashlib.sha256(path.read_bytes()).hexdigest() != digest or
                    hashlib.sha256(blob).hexdigest() != digest):
                raise ProviderContractError("producer source digest mismatch")
        if export_directory is not None:
            if (not isinstance(export_directory, str) or not os.path.isabs(export_directory)
                    or Path(export_directory).exists()):
                raise ProviderContractError("absolute nonexistent export directory required")
        with tempfile.TemporaryDirectory(prefix="r24-provider-") as tmp:
            request_path, config_path = Path(tmp) / "request.json", Path(tmp) / "config.json"
            request_path.write_bytes(request.canonical)
            config_path.write_bytes(_canonical(config))
            argv = [sys.executable, "-I", "-B", str(root / "pinokio/providers/r24-linux.py"),
                    str(request_path), str(config_path)]
            if export_directory is not None:
                argv.append(export_directory)
            proc = subprocess.run(argv, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, timeout=150, check=False)
        return proc
    except subprocess.TimeoutExpired as exc:
        raise ProviderInvocationError("producer invocation timed out", exc.stderr or b"",
                                      stdout=exc.stdout or b"") from exc
    except (OSError, subprocess.SubprocessError, UnicodeError) as exc:
        raise ProviderContractError("producer invocation unavailable") from exc


def invoke_linux_diagnostic(request: ProviderRequest, config_json: str | bytes,
                            producer_root: str) -> DiagnosticResult:
    """Invoke the R1 diagnostic API; never qualify its output."""
    proc = _invoke_linux(request, config_json, producer_root, _PRODUCER_REF,
                         _PRODUCER_FILES, _CANDIDATE, _STOP)
    if proc.returncode != 0:
        raise ProviderContractError("producer failed or did not run")
    output = _object(_decode(proc.stdout), {"schema", "request_sha256", "action", "busy", "unknown",
                                            "restoration", "runtime_identity", "custody", "protected_state"},
                     "diagnostic")
    if (output["schema"] != "astrid.r24.producer-diagnostic.v1" or
            output["request_sha256"] != request.sha256 or
            output["busy"] != "not-run" or output["unknown"] != "not-run" or
            any(output[k] != "unknown" for k in ("restoration", "runtime_identity", "custody", "protected_state")) or
            not isinstance(output["action"], dict)):
        raise ProviderContractError("unexpected or contradictory diagnostic")
    return DiagnosticResult(request.sha256)


@dataclass(frozen=True)
class LinuxProviderOutput:
    """Untrusted structural claims and separate diagnostics, never admission."""

    result: ProviderResult
    stderr: bytes
    stdout: bytes = b""
    returncode: int = 0


def invoke_linux_provider(request: ProviderRequest, config_json: str | bytes,
                          producer_root: str) -> LinuxProviderOutput:
    """Invoke the immutable live collector and parse one result; do not admit it.

    The controlled caller supplies a materialized configuration. On a nonzero
    exit or invalid stdout, ProviderInvocationError retains stderr separately.
    No supported driver route or evidence admission is enabled by this function.
    """
    proc = _invoke_linux(request, config_json, producer_root,
                         LIVE_PRODUCER_REF, LIVE_PRODUCER_FILES,
                         LIVE_CANDIDATE, LIVE_STOP, live_config=True)
    if proc.returncode != 0:
        raise ProviderInvocationError("producer failed or did not run", proc.stderr,
                                      stdout=proc.stdout, returncode=proc.returncode)
    try:
        result = ProviderResult.parse(proc.stdout, request)
    except ProviderContractError as exc:
        raise ProviderInvocationError("invalid producer result", proc.stderr,
                                      stdout=proc.stdout, returncode=proc.returncode) from exc
    return LinuxProviderOutput(result, proc.stderr, proc.stdout, proc.returncode)


def invoke_linux_provider_export(request: ProviderRequest, config_json: str | bytes,
                                 producer_root: str, export_directory: str) -> LinuxProviderOutput:
    """Invoke the same typed provider while requesting its fixed raw export.

    The caller remains responsible for independently validating the export and
    hosted custody.  This function only preserves the typed R3 subprocess and
    stream boundary while adding the producer's immutable export argument.
    """
    proc = _invoke_linux(request, config_json, producer_root,
                         LIVE_PRODUCER_REF, LIVE_PRODUCER_FILES,
                         LIVE_CANDIDATE, LIVE_STOP, live_config=True,
                         export_directory=export_directory)
    if proc.returncode != 0:
        raise ProviderInvocationError("producer failed or did not run", proc.stderr,
                                      stdout=proc.stdout, returncode=proc.returncode)
    try:
        result = ProviderResult.parse(proc.stdout, request)
    except ProviderContractError as exc:
        raise ProviderInvocationError("invalid producer result", proc.stderr,
                                      stdout=proc.stdout, returncode=proc.returncode) from exc
    return LinuxProviderOutput(result, proc.stderr, proc.stdout, proc.returncode)
