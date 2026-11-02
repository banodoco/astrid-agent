"""Integrate bounded Linux Pinokio mode actions with the Agent journey harness.

The route first executes the exact current-tuple R-24 seam, then invokes the
immutable R-25 mode provider while the same genuine busy task remains active.
It retains a non-qualifying observation record only.  It cannot mint an M4-6
receipt, authenticate custody, or turn not-run protected scenarios into pass.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping

try:
    from . import ci_journey_r24 as r24
    from . import pinokio_r24_journey_seam as seam
except ImportError:
    import ci_journey_r24 as r24
    import pinokio_r24_journey_seam as seam

SCHEMA = "astrid.r25.linux-mode-actions-seam.v1"
PROVIDER_SCHEMA = "astrid.r25.linux-mode-actions-request.v1"
RESULT_SCHEMA = "astrid.r25.linux-mode-actions-result.v1"
MODE_PRODUCER_REF = "0d28295a6adfdb08fd3adf56f85bae07c68b0c6e"
MODE_PRODUCER_FILES = {
    "pinokio/providers/r25-linux-mode-actions.py":
        "290ebcf703e3d2d662bc05a51d6f86a1268a1c1ad371e6d6dd49f14dd2ebdf26",
    "pinokio/providers/r25-mode-actions.cjs":
        "2a7558d724f41d4777638be47a1684df89cb5b0ff4a42dbbce63d6808a98db23",
}
MODES = {"backend": "pinokio-backend-linux",
         "fully-local": "pinokio-fully-local-linux"}
OPERATIONS = {"backend": ["update", "keep_data_uninstall"],
              "fully-local": ["keep_data_uninstall"]}
_HEX40 = re.compile(r"[0-9a-f]{40}\Z")
_HEX32 = re.compile(r"[0-9a-f]{32}\Z")
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")


class LinuxModeActionError(ValueError):
    """The integrated observation cannot be retained without promotion."""


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False).encode("ascii")


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in items:
        if key in result:
            raise LinuxModeActionError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _decode(value: bytes) -> Any:
    try:
        return json.loads(value, object_pairs_hook=_pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(
                              LinuxModeActionError("invalid JSON constant")))
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise LinuxModeActionError("invalid JSON") from exc


def _object(value: object, fields: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise LinuxModeActionError(f"{label} fields differ")
    return dict(value)


def _regular(path: Path, *, limit: int = 1024 * 1024) -> bytes:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
        raise LinuxModeActionError(f"missing bounded regular file: {path.name}")
    return path.read_bytes()


def _git(root: Path, *args: str) -> bytes:
    try:
        return subprocess.check_output(["git", "-C", str(root), *args],
                                       stderr=subprocess.DEVNULL, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        raise LinuxModeActionError("Git identity validation failed") from exc


def _checkout(path: Path, ref: str, sources: Mapping[str, str]) -> Path:
    if not path.is_absolute() or path.is_symlink() or not path.is_dir():
        raise LinuxModeActionError("absolute nonsymlink producer checkout required")
    root = path.resolve(strict=True)
    try:
        top = Path(os.fsdecode(_git(root, "rev-parse", "--show-toplevel").strip())).resolve()
        head = _git(root, "rev-parse", "HEAD").decode().strip()
        dirty = _git(root, "status", "--porcelain=v1", "--untracked-files=all")
    except (OSError, UnicodeError) as exc:
        raise LinuxModeActionError("producer checkout identity is unreadable") from exc
    if top != root or head != ref or dirty:
        raise LinuxModeActionError("producer checkout ref or cleanliness differs")
    for relative, expected in sources.items():
        working = _regular(root / relative)
        committed = _git(root, "show", f"{ref}:{relative}")
        if _sha(working) != expected or _sha(committed) != expected:
            raise LinuxModeActionError("mode producer source digest differs")
    return root


def _runner_identity() -> dict[str, Any]:
    source = Path(__file__)
    if source.is_symlink() or not source.is_file():
        raise LinuxModeActionError("mode-action runner source is unavailable")
    root = source.resolve().parent.parent
    top = Path(os.fsdecode(_git(root, "rev-parse", "--show-toplevel").strip())).resolve()
    head = _git(root, "rev-parse", "HEAD").decode().strip()
    dirty = _git(root, "status", "--porcelain=v1", "--untracked-files=all")
    if top != root or _HEX40.fullmatch(head) is None or dirty:
        raise LinuxModeActionError("mode-action runner checkout is not immutable and clean")
    sources = {}
    for relative in ("scripts/ci_journey.py", "scripts/pinokio_r24_journey_seam.py",
                     "scripts/pinokio_linux_mode_actions.py"):
        working = _regular(root / relative)
        committed = _git(root, "show", f"{head}:{relative}")
        if working != committed:
            raise LinuxModeActionError("mode-action runner source differs from its commit")
        sources[relative] = _sha(working)
    return {"head": head, "sources": sources}


def _output_directory(path: Path) -> Path:
    if not path.is_absolute() or path.is_symlink() or path.exists():
        raise LinuxModeActionError("output must be an absent absolute non-symlink directory")
    parent = path.parent
    if parent.is_symlink() or not parent.is_dir() or parent.resolve(strict=True) != parent:
        raise LinuxModeActionError("output parent must be an absolute resolved directory")
    os.mkdir(path, 0o700)
    return path


def _write_exclusive(path: Path, value: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as output:
        output.write(value)
        output.flush()
        os.fsync(output.fileno())


def _validate_result(value: object, request: Mapping[str, Any], mode: str) -> dict[str, Any]:
    result = _object(value, {"schema", "request_sha256", "r24_request_sha256",
                             "r24_seam_sha256", "run_id", "session", "slot_id",
                             "mode", "actions", "protected", "scenarios", "boundary"},
                     "mode provider result")
    if (result.get("schema") != RESULT_SCHEMA
            or result.get("request_sha256") != _sha(_canonical(request))
            or result.get("r24_request_sha256") != request["r24_request_sha256"]
            or result.get("r24_seam_sha256") != request["r24_seam_sha256"]
            or result.get("run_id") != request["run_id"]
            or result.get("slot_id") != MODES[mode]
            or result.get("mode") != mode
            or not isinstance(result.get("session"), str)
            or _HEX32.fullmatch(result["session"]) is None):
        raise LinuxModeActionError("mode provider result binding differs")
    actions = result.get("actions")
    if not isinstance(actions, list) or [row.get("operation") for row in actions
                                         if isinstance(row, dict)] != OPERATIONS[mode]:
        raise LinuxModeActionError("mode provider action inventory differs")
    for row in actions:
        action = _object(row, {"operation", "uri", "request_id", "refusal_sha256", "fenced"},
                         "mode action")
        if (not isinstance(action.get("request_id"), str)
                or not isinstance(action.get("uri"), str)
                or not isinstance(action.get("refusal_sha256"), str)
                or _HEX64.fullmatch(action["refusal_sha256"]) is None
                or action.get("fenced") is not True):
            raise LinuxModeActionError("mode action is not a fenced real refusal")
    protected = _object(result.get("protected"), {"before_sha256", "after_sha256", "unchanged"},
                        "protected snapshot")
    if (protected.get("unchanged") is not True
            or protected.get("before_sha256") != protected.get("after_sha256")
            or not isinstance(protected.get("before_sha256"), str)
            or _HEX64.fullmatch(protected["before_sha256"]) is None):
        raise LinuxModeActionError("protected snapshot changed")
    scenarios = _object(result.get("scenarios"), {"stop_safety", "interruption_recovery",
                                                   "failed_candidate_rollback",
                                                   "keep_data_uninstall"}, "scenario state")
    if (not isinstance(scenarios["interruption_recovery"], dict)
            or scenarios["interruption_recovery"].get("status") != "not-run"
            or not isinstance(scenarios["failed_candidate_rollback"], dict)
            or str(scenarios["failed_candidate_rollback"].get("status", "")).startswith("pass")
            or not isinstance(scenarios["keep_data_uninstall"], dict)
            or scenarios["keep_data_uninstall"].get("successful_uninstall") is not False
            or scenarios["stop_safety"].get("status") != "partial"):
        raise LinuxModeActionError("protected scenarios were promoted without execution")
    boundary = _object(result.get("boundary"), {"authenticated_custody", "qualification",
                                                 "m4_6_receipt", "missing"}, "boundary")
    if boundary != {
        "authenticated_custody": False,
        "qualification": "not-evaluated",
        "m4_6_receipt": False,
        "missing": "R-36 authenticated platform session/removal authorization/finalization interface",
    }:
        raise LinuxModeActionError("protected boundary differs")
    return result


def execute(*, mode: str, run_root: Path, candidate_file: Path, config_file: Path,
            r24_producer_root: Path, mode_producer_root: Path,
            checkouts: Mapping[str, Path], output: Path) -> dict[str, Any]:
    """Run R-24 once, then the safe real mode actions, retaining no receipt."""
    if sys.platform != "linux" or mode not in MODES:
        raise LinuxModeActionError("Linux backend or fully-local mode is required")
    selected_output = _output_directory(output)
    runner = _runner_identity()
    mode_producer = _checkout(mode_producer_root, MODE_PRODUCER_REF, MODE_PRODUCER_FILES)
    other_roots = {Path(path).resolve(strict=True) for path in checkouts.values()}
    other_roots.add(r24_producer_root.resolve(strict=True))
    if mode_producer in other_roots:
        raise LinuxModeActionError("mode producer must be distinct from all candidate inputs")
    r24_seam_file = selected_output / "r24-seam.json"
    r24_record = seam.execute(
        run_root=run_root, candidate_file=candidate_file, config_file=config_file,
        producer_root=r24_producer_root, checkouts=checkouts, output=r24_seam_file)
    r24_seam_raw = _regular(r24_seam_file)
    r24_request_path = run_root.resolve(strict=True) / "evidence/r24-live/request.json"
    r24_request_raw = _regular(r24_request_path)
    r24_request = _decode(r24_request_raw)
    config_raw = _regular(config_file)
    config = _decode(config_raw)
    run_id = ((r24_record.get("r24") or {}).get("run_id")
              if isinstance(r24_record, dict) else None)
    if not isinstance(run_id, str) or _HEX32.fullmatch(run_id) is None:
        raise LinuxModeActionError("R-24 run binding is unavailable")
    request = {
        "schema": PROVIDER_SCHEMA,
        "candidate": dict(r24.PUBLISHED_CANDIDATE),
        "mode": mode,
        "provider": {"ref": MODE_PRODUCER_REF, "sources": dict(MODE_PRODUCER_FILES)},
        "r24_request_sha256": _sha(_canonical(r24_request)),
        "r24_seam_sha256": _sha(r24_seam_raw),
        "config_sha256": _sha(_canonical(config)),
        "run_id": run_id,
    }
    request_path = selected_output / "request.json"
    bound_r24_path = selected_output / "r24-request.json"
    bound_config_path = selected_output / "config.json"
    _write_exclusive(request_path, _canonical(request) + b"\n")
    _write_exclusive(bound_r24_path, _canonical(r24_request) + b"\n")
    _write_exclusive(bound_config_path, _canonical(config) + b"\n")
    argv = [sys.executable, "-I", "-B",
            str(mode_producer / "pinokio/providers/r25-linux-mode-actions.py"),
            str(request_path), str(bound_r24_path), str(bound_config_path),
            str(r24_producer_root.resolve(strict=True))]
    try:
        process = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 timeout=180, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise LinuxModeActionError("mode provider invocation unavailable") from exc
    _write_exclusive(selected_output / "provider-stdout.json", process.stdout)
    _write_exclusive(selected_output / "provider-stderr.log", process.stderr)
    if process.returncode != 0:
        raise LinuxModeActionError("mode provider failed or refused execution")
    result = _validate_result(_decode(process.stdout), request, mode)
    record = {
        "schema": SCHEMA,
        "candidate": dict(r24.PUBLISHED_CANDIDATE),
        "platform": "linux",
        "slot_id": MODES[mode],
        "mode": mode,
        "runner": runner,
        "r24": r24_record["r24"],
        "mode_provider": {"ref": MODE_PRODUCER_REF,
                          "sources": dict(MODE_PRODUCER_FILES),
                          "result_sha256": _sha(process.stdout)},
        "observations": result,
        "boundary": {
            "authenticated_custody": False,
            "qualification": "not-evaluated",
            "m4_6_receipt": False,
            "missing_producer": "M-QUAL R-36 authenticated platform session/removal authorization/finalization",
        },
    }
    _write_exclusive(selected_output / "mode-action-seam.json", _canonical(record) + b"\n")
    return record
