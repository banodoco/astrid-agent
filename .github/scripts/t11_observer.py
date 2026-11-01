#!/usr/bin/env python3
"""Fail closed T-11 same-process observation gate.

This program is deliberately *not* a profiler, preload, wrapper, or debugger.
Those mechanisms would alter the installed product's process launch.  Instead it
records the exact source seam which prevents an in-process observation and exits
2 (not qualified).  A future product-owned seam must emit the two direct events
validated by :func:`validate_direct_events`; an arbitrary file-open, package
hash, probe result, or process census is never treated as consumption evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
from typing import Any, Iterable, Mapping


EXIT_OBSERVATION_UNAVAILABLE = 2
EXIT_CONTRACT_FAILURE = 1
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
COMMIT_RE = re.compile(r"[0-9a-f]{40}\Z")
PROC_ROOT = Path("/proc")


class ObservationContractError(ValueError):
    """The direct-event contract is missing, ambiguous, or inconsistent."""


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def _inventory_digest(entries: Iterable[Mapping[str, Any]]) -> str:
    """Hash a deterministic package-file inventory, never a package version."""
    encoded = json.dumps(list(entries), sort_keys=True, separators=(",", ":")).encode("utf-8")
    return sha256_bytes(encoded)


def _regular_file_record(path: Path, relative: str) -> dict[str, Any]:
    stat = path.lstat()
    if path.is_symlink() or not path.is_file():
        raise ObservationContractError("Composition package contains a non-regular file")
    return {"path": relative, "size": stat.st_size, "sha256": sha256_file(path)}


def composition_archive_inventory(archive: Path) -> dict[str, Any]:
    """Return the exact regular-file inventory recorded in a Composition tgz.

    npm packages use a ``package/`` archive prefix.  Links and special entries
    are refused so their target cannot become an unrecorded executable input.
    """
    records: list[dict[str, Any]] = []
    try:
        with tarfile.open(archive, "r:gz") as package:
            for member in sorted(package.getmembers(), key=lambda item: item.name):
                if member.isdir():
                    continue
                if not member.isfile() or not member.name.startswith("package/"):
                    raise ObservationContractError("Composition archive has an unsupported member")
                relative = member.name.removeprefix("package/")
                if not relative or relative.startswith("/") or ".." in Path(relative).parts:
                    raise ObservationContractError("Composition archive member has an unsafe path")
                handle = package.extractfile(member)
                if handle is None:
                    raise ObservationContractError("Composition archive member is unreadable")
                payload = handle.read()
                records.append({"path": relative, "size": len(payload), "sha256": sha256_bytes(payload)})
    except (OSError, tarfile.TarError) as exc:
        raise ObservationContractError("Composition archive cannot be inventoried") from exc
    if not records:
        raise ObservationContractError("Composition archive has no regular files")
    return {
        "archive_sha256": sha256_file(archive),
        "file_count": len(records),
        "inventory_sha256": _inventory_digest(records),
        "files": records,
    }


def composition_installed_inventory(root: Path) -> dict[str, Any]:
    """Return the exact installed package tree; npm metadata belongs outside it."""
    package = root.resolve(strict=True)
    if package.is_symlink() or not package.is_dir():
        raise ObservationContractError("installed Composition package is not a real directory")
    records: list[dict[str, Any]] = []
    try:
        paths = sorted(package.rglob("*"), key=lambda item: item.relative_to(package).as_posix())
    except OSError as exc:
        raise ObservationContractError("installed Composition package cannot be inventoried") from exc
    for path in paths:
        relative = path.relative_to(package).as_posix()
        if path.is_symlink():
            raise ObservationContractError("installed Composition package contains a symlink")
        if path.is_dir():
            continue
        records.append(_regular_file_record(path, relative))
    if not records:
        raise ObservationContractError("installed Composition package has no regular files")
    return {
        "path": str(package),
        "file_count": len(records),
        "inventory_sha256": _inventory_digest(records),
        "files": records,
    }


def verify_composition_inputs(archive: Path, installed_package: Path) -> dict[str, Any]:
    """Require the installed inputs to be exactly the tuple-recorded archive."""
    recorded = composition_archive_inventory(archive)
    installed = composition_installed_inventory(installed_package)
    if recorded["files"] != installed["files"]:
        raise ObservationContractError("installed Composition inputs differ from the recorded archive")
    return {
        "archive_sha256": recorded["archive_sha256"],
        "archive_inventory_sha256": recorded["inventory_sha256"],
        "resolved_package_path": installed["path"],
        "resolved_input_inventory_sha256": installed["inventory_sha256"],
        "file_count": recorded["file_count"],
        "equal": True,
    }


def _line_number(text: str, marker: str) -> int:
    position = text.find(marker)
    if position < 0:
        raise ObservationContractError("required source marker is absent")
    return text.count("\n", 0, position) + 1


def _source_record(root: Path, relative: str, markers: Iterable[str]) -> dict[str, Any]:
    path = root / relative
    if path.is_symlink() or not path.is_file():
        raise ObservationContractError("required source evidence is not a regular file")
    raw = path.read_bytes()
    text = raw.decode("utf-8")
    return {
        "path": relative,
        "sha256": sha256_bytes(raw),
        "markers": {marker: _line_number(text, marker) for marker in markers},
    }


def inspect_missing_product_seam(agent_source: Path) -> dict[str, Any]:
    """Return source-hashed evidence for the current, non-observable topology.

    The markers are intentionally about topology, not an inference that a
    successful probe or render consumed a particular value.  Any drift makes the
    observer fail rather than silently applying this refusal to different code.
    """
    root = agent_source.resolve(strict=True)
    schema = _source_record(
        root,
        "astrid/core/rendering/remotion_runtime.py",
        (
            "schema validation requires an isolated installed interpreter",
            "contract = _local_schema.load_verified_schema(root, sys.argv[1])",
        ),
    )
    host = _source_record(
        root,
        "astrid/core/execution/remotion_host_session.py",
        (
            "arbitrary NODE_OPTIONS or preloaders",
            "[str(lease.node), str(lease.project / \"src/astrid-frame-worker.mjs\")]",
        ),
    )
    worker = _source_record(
        root,
        "remotion/src/astrid-frame-worker.mjs",
        (
            "async function bundleProject(request)",
            "selectComposition({",
            "await renderFrames(",
        ),
    )
    worker_text = (root / worker["path"]).read_text(encoding="utf-8")
    host_text = (root / host["path"]).read_text(encoding="utf-8")
    runtime_text = (root / schema["path"]).read_text(encoding="utf-8")
    if any("ASTRID_T11" in text or "t11_observer" in text
           for text in (runtime_text, host_text, worker_text)):
        raise ObservationContractError("source contains an unreviewed T-11 observer marker")
    return {
        "agent_source": str(root),
        "schema_probe": schema,
        "host_worker_launch": host,
        "frame_worker": worker,
        "missing_seams": {
            "schema_consumption": {
                "status": "unobserved",
                "reason": (
                    "load_verified_schema is called by the isolated validation probe; "
                    "the product exposes no hook in the actual host/render consumer to "
                    "record its returned object with process and run identity"
                ),
                "required_product_seam": (
                    "an in-process event emitted by the actual schema consumer after "
                    "load_verified_schema returns, including canonical document digest, "
                    "PID/start identity, and render run identity"
                ),
            },
            "composition_execution": {
                "status": "unobserved",
                "reason": (
                    "the host launches the Node worker with a fixed, preload-free "
                    "environment, and the worker exposes no module-resolution or "
                    "browser-loaded-bundle event tied to the render run"
                ),
                "required_product_seam": (
                    "a bundler/browser event from the actual frame worker that binds the "
                    "resolved Composition inputs and loaded bundle identity to the render "
                    "run and worker PID/start identity"
                ),
            },
        },
    }


def _proc_start_ticks(stat_bytes: bytes) -> int:
    # Field 2 may contain spaces and parentheses, so split only after its final
    # closing parenthesis.  starttime is field 22, or index 19 after state.
    right = stat_bytes.rfind(b")")
    fields = stat_bytes[right + 2:].split()
    if right < 0 or len(fields) <= 19:
        raise ValueError("unparseable proc stat")
    return int(fields[19])


def _read_proc_environment(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for item in path.read_bytes().split(b"\0"):
        if b"=" not in item:
            continue
        key, value = item.split(b"=", 1)
        values[key.decode("utf-8", "replace")] = value.decode("utf-8", "replace")
    return values


def _option_value(argv: list[str], option: str) -> str | None:
    positions = [index for index, value in enumerate(argv) if value == option]
    if len(positions) != 1 or positions[0] + 1 >= len(argv):
        return None
    return argv[positions[0] + 1]


def capture_pack_host_identity(
    *, installed_source_root: Path, data_root: Path, proc_root: Path = PROC_ROOT,
) -> dict[str, Any]:
    """Snapshot only a uniquely bound host; zero/multiple candidates are not proof."""
    expected_source = str(installed_source_root.resolve(strict=True))
    expected_data = str(data_root.resolve(strict=True))
    candidates: list[dict[str, Any]] = []
    try:
        boot_id = (proc_root / "sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
    except OSError:
        boot_id = None
    try:
        process_entries = sorted((entry for entry in proc_root.iterdir() if entry.name.isdigit()),
                                 key=lambda entry: int(entry.name))
    except OSError:
        return {"status": "unobserved", "reason": "process census is unavailable", "candidates": []}
    for entry in process_entries:
        try:
            argv = [item.decode("utf-8", "replace") for item in
                    (entry / "cmdline").read_bytes().split(b"\0") if item]
            if "astrid.core.execution.generic_host" not in argv:
                continue
            source = _option_value(argv, "--source-checkout")
            if source is None or str(Path(source).resolve(strict=True)) != expected_source:
                continue
            environment = _read_proc_environment(entry / "environ")
            executable = (entry / "exe").resolve(strict=True)
            start_ticks = _proc_start_ticks((entry / "stat").read_bytes())
            selected_roots = {
                "ASTRID_RUNTIME_DATA_ROOT": environment.get("ASTRID_RUNTIME_DATA_ROOT") == expected_data,
                "ASTRID_SUPPORT_ROOT": environment.get("ASTRID_SUPPORT_ROOT") == expected_data,
                "ASTRID_WORKSPACE_DISCOVERY": environment.get("ASTRID_WORKSPACE_DISCOVERY")
                == str(data_root.resolve(strict=True) / "runtime/discovery.json"),
            }
            candidates.append({
                "pid": int(entry.name),
                "start_ticks": start_ticks,
                "boot_id": boot_id,
                "executable": str(executable),
                "executable_sha256": sha256_file(executable),
                "argv_sha256": sha256_bytes(json.dumps(argv, separators=(",", ":")).encode("utf-8")),
                "argv_shape": ["python", "-X", "utf8", "-m", "astrid.core.execution.generic_host",
                               "--service", "--source-checkout", "<selected-agent>", "--support-root",
                               "<runtime-discovery-parent>"],
                "environment_name_sha256": sha256_bytes(
                    json.dumps(sorted(environment), separators=(",", ":")).encode("utf-8")
                ),
                "selected_root_matches": selected_roots,
            })
        except (OSError, ValueError):
            # A process can exit during inspection.  Do not retain a partial
            # identity or convert that race into a successful binding.
            continue
    if len(candidates) == 1 and all(candidates[0]["selected_root_matches"].values()):
        return {
            "status": "observed-not-run-correlated",
            "reason": "one live pack-host matches the selected Agent and data root",
            "candidates": candidates,
            "correlated_run_id": None,
        }
    reason = "no selected pack-host was observable" if not candidates else "ambiguous pack-host identity"
    return {"status": "unobserved", "reason": reason, "candidates": candidates,
            "correlated_run_id": None}


def _read_run_identity(path: Path) -> str | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    render = value.get("render") if isinstance(value, dict) else None
    run_id = render.get("run_id") if isinstance(render, dict) else None
    return run_id if isinstance(run_id, str) and run_id else None


def validate_direct_events(
    events: Iterable[Mapping[str, Any]], *, run_id: str, schema_sha256: str,
    composition_archive_sha256: str, composition_input_inventory_sha256: str,
    known_processes: Iterable[Mapping[str, Any]],
) -> dict[str, str]:
    """Validate future product-emitted events; reject gaps and ambiguity.

    This validator is intentionally not enabled by the current harness: current
    product code has no authenticated, inherited event endpoint.  It exists so
    a reviewed product seam has an exact fail-closed contract to target.
    """
    known = {(item.get("pid"), item.get("start_ticks"), item.get("boot_id"))
             for item in known_processes}
    expected = {"timeline_schema_consumed", "composition_executed"}
    seen: dict[str, Mapping[str, Any]] = {}
    for event in events:
        kind = event.get("event")
        if kind not in expected or kind in seen:
            raise ObservationContractError("event kind is missing, unsupported, or duplicated")
        if event.get("run_id") != run_id or not isinstance(event.get("observed_at_monotonic_ns"), int):
            raise ObservationContractError("event has no unambiguous render run identity")
        process = event.get("process")
        if not isinstance(process, Mapping) or (
            process.get("pid"), process.get("start_ticks"), process.get("boot_id")
        ) not in known:
            raise ObservationContractError("event process identity is absent or was not observed")
        seen[kind] = event
    if set(seen) != expected:
        raise ObservationContractError("both direct consumption events are required")
    schema = seen["timeline_schema_consumed"].get("schema")
    composition = seen["composition_executed"].get("composition")
    if (not isinstance(schema, Mapping)
            or schema.get("canonical_json_sha256") != schema_sha256
            or not isinstance(schema.get("reader_origin_sha256"), str)
            or not SHA256_RE.fullmatch(schema["reader_origin_sha256"])):
        raise ObservationContractError("schema event does not name the verified document")
    if (not isinstance(composition, Mapping)
            or composition.get("archive_sha256") != composition_archive_sha256
            or composition.get("resolved_input_inventory_sha256") != composition_input_inventory_sha256):
        raise ObservationContractError("composition event does not name the recorded archive")
    if not isinstance(composition.get("loaded_bundle_sha256"), str) or not SHA256_RE.fullmatch(
        composition["loaded_bundle_sha256"]
    ):
        raise ObservationContractError("composition event has no loaded-bundle identity")
    return {"schema_consumption": "observed", "composition_execution": "observed"}


def _git_head(agent_source: Path) -> str:
    completed = subprocess.run(
        ["git", "-C", str(agent_source), "rev-parse", "HEAD^{commit}"],
        text=True, capture_output=True, check=False,
    )
    if completed.returncode:
        raise ObservationContractError("selected Agent source has no readable commit identity")
    value = completed.stdout.strip()
    if not COMMIT_RE.fullmatch(value):
        raise ObservationContractError("selected Agent source commit identity is invalid")
    return value


def _write_new_json(path: Path, value: Mapping[str, Any]) -> None:
    encoded = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        offset = 0
        while offset < len(encoded):
            offset += os.write(descriptor, encoded[offset:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _parse_commit(value: str, name: str) -> str:
    if not COMMIT_RE.fullmatch(value):
        raise ObservationContractError(name + " must be a 40-character lowercase commit SHA")
    return value


def _parse_digest(value: str, name: str) -> str:
    if not SHA256_RE.fullmatch(value):
        raise ObservationContractError(name + " must be a SHA-256 hex digest")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent-source", required=True, type=Path)
    parser.add_argument("--installed-source-root", type=Path)
    parser.add_argument("--data-root", required=True, type=Path)
    parser.add_argument("--profile", required=True, type=Path)
    parser.add_argument("--example-receipt", required=True, type=Path)
    parser.add_argument("--receipt", required=True, type=Path)
    parser.add_argument("--agent-sha", required=True)
    parser.add_argument("--runtime-sha", required=True)
    parser.add_argument("--app-sha", required=True)
    parser.add_argument("--pinokio-sha", required=True)
    parser.add_argument("--schema-sha256", required=True)
    parser.add_argument("--composition-archive-sha256", required=True)
    parser.add_argument("--composition-archive", required=True, type=Path)
    parser.add_argument("--installed-composition-package", type=Path)
    args = parser.parse_args(argv)
    receipt: dict[str, Any] = {
        "schema_version": 1,
        "status": "observer-failed",
        "qualification": False,
    }
    try:
        tuple_identity = {
            "agent_sha": _parse_commit(args.agent_sha, "agent_sha"),
            "runtime_sha": _parse_commit(args.runtime_sha, "runtime_sha"),
            "app_sha": _parse_commit(args.app_sha, "app_sha"),
            "pinokio_sha": _parse_commit(args.pinokio_sha, "pinokio_sha"),
        }
        schema_sha = _parse_digest(args.schema_sha256, "schema_sha256")
        archive_sha = _parse_digest(args.composition_archive_sha256, "composition_archive_sha256")
        source = args.agent_source.resolve(strict=True)
        if _git_head(source) != tuple_identity["agent_sha"]:
            raise ObservationContractError("selected Agent source differs from requested tuple")
        installed_source_root = (args.installed_source_root.resolve(strict=True)
                                 if args.installed_source_root is not None else None)
        profile_sha = sha256_file(args.profile.resolve(strict=True))
        composition_archive = args.composition_archive.resolve(strict=True)
        if sha256_file(composition_archive) != archive_sha:
            raise ObservationContractError("Composition archive differs from the requested tuple digest")
        composition_inputs: dict[str, Any]
        if args.installed_composition_package is None:
            composition_inputs = {
                "status": "unobserved-no-installed-package",
                "archive_sha256": archive_sha,
                "reason": "this observer invocation has no installed Composition package root",
            }
        else:
            composition_inputs = verify_composition_inputs(
                composition_archive, args.installed_composition_package,
            )
        seam = inspect_missing_product_seam(source)
        host = (capture_pack_host_identity(
            installed_source_root=installed_source_root, data_root=args.data_root,
        ) if installed_source_root is not None else {
            "status": "unobserved",
            "reason": "no installed source root was available to this refusal invocation",
            "candidates": [],
            "correlated_run_id": None,
        })
        run_id = _read_run_identity(args.example_receipt)
        receipt = {
            "schema_version": 1,
            "status": "refused-no-product-seam",
            "qualification": False,
            "tuple": tuple_identity,
            "expected_static_digests": {
                "timeline_schema_sha256": schema_sha,
                "composition_archive_sha256": archive_sha,
                "source_profile_sha256": profile_sha,
            },
            "composition_input_identity": composition_inputs,
            "render_run_identity": {"status": "recorded" if run_id else "unobserved", "run_id": run_id},
            "host_process_identity": host,
            "process_run_correlation": {
                "status": "unobserved",
                "run_id": run_id,
                "reason": (
                    "the current product does not emit a same-render-run event from the "
                    "schema consumer or frame worker with a stable process-start identity"
                ),
            },
            "same_process_observation": seam["missing_seams"],
            "direct_event_contract": {
                "status": "unavailable-no-product-endpoint",
                "required_events": {
                    "timeline_schema_consumed": [
                        "run_id", "observed_at_monotonic_ns", "process(pid,start_ticks,boot_id)",
                        "schema.canonical_json_sha256", "schema.reader_origin_sha256",
                    ],
                    "composition_executed": [
                        "run_id", "observed_at_monotonic_ns", "process(pid,start_ticks,boot_id)",
                        "composition.archive_sha256", "composition.resolved_input_inventory_sha256",
                        "composition.loaded_bundle_sha256",
                    ],
                },
            },
            "source_evidence": {
                "schema_probe": seam["schema_probe"],
                "host_worker_launch": seam["host_worker_launch"],
                "frame_worker": seam["frame_worker"],
            },
            "refusal": {
                "code": "missing-product-instrumentation",
                "detail": (
                    "No approved same-process schema-return or Composition-resolution/"
                    "browser-bundle observation seam exists in this selected Agent source. "
                    "The receipt is not qualified even if installation and render succeeded."
                ),
            },
        }
        _write_new_json(args.receipt.resolve(), receipt)
        return EXIT_OBSERVATION_UNAVAILABLE
    except (OSError, ValueError, ObservationContractError):
        receipt["error_type"] = "observer-contract-failure"
        try:
            _write_new_json(args.receipt.resolve(), receipt)
        except OSError:
            pass
        return EXIT_CONTRACT_FAILURE


if __name__ == "__main__":
    raise SystemExit(main())
