"""Describe the installed host for Runtime's existing verified local launcher."""

from __future__ import annotations

import hashlib
import importlib.metadata
import os
from pathlib import Path
import platform
import sys


def write_worker_profile(folder: Path, source_checkout: Path, data_root: Path,
                         capability_matrix: Path | None = None) -> Path:
    """Measure the candidate interpreter and installed release, without issuing authority."""
    from runtime_protocol.verified_host_launch import (
        CURRENT_ROLES_VERSION, raw_digest, source_checkout_closure,
    )
    from runtime_protocol import platform as runtime_platform
    from astrid.core.gateway.dispatch import compose_profile_handoff
    from astrid.installer.core import absolute, write_json

    folder, source_checkout, data_root = map(absolute, (folder, source_checkout, data_root))
    if capability_matrix is not None:
        capability_matrix = absolute(capability_matrix)

    def digest(value):
        return "sha256:" + raw_digest(value)

    records = {}
    for name in ("astrid", "astrid-runtime"):
        distribution = importlib.metadata.distribution(name)
        if Path(distribution.locate_file("")).resolve() != source_checkout.resolve():
            raise ValueError("Worker profile must describe the selected installed product tree.")
        record = distribution.read_text("RECORD")
        if not record:
            raise ValueError("Worker profile requires installed release RECORDs.")
        records[name] = hashlib.sha256(record.encode()).hexdigest()
    support = data_root / "runtime"
    runtime_platform.private_directory(support)
    boot = compose_profile_handoff(support / "astrid-host" / folder.name / "boot-manifest.json",
                                   support_root=support)
    launch = {
        "source_checkout": str(source_checkout), "pack_root": str(source_checkout / "astrid/packs"),
        "boot_manifest_path": boot["path"], "boot_manifest_hash": "sha256:" + boot["sha256"],
        "capability_matrix": str(capability_matrix) if capability_matrix else None,
        "environment": {key: os.environ[key] for key in (
            "PATH", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "TEMP", "TMP", "XDG_RUNTIME_DIR",
        ) if key in os.environ},
        "max_concurrency": 2,
    }
    executable = Path(os.path.abspath(sys.executable))
    kernel = executable.resolve(strict=True)
    value = {
        "execution_mode": "host_only_cpu", "profile_id": "astrid", "machine_id": platform.node(),
        "host_executable": str(executable),
        "host_artifact_digest": "sha256:" + hashlib.sha256(executable.read_bytes()).hexdigest(),
        "host_os_executable": str(kernel),
        "host_os_artifact_digest": "sha256:" + hashlib.sha256(kernel.read_bytes()).hexdigest(),
        "host_protocol": {"version": CURRENT_ROLES_VERSION, "module": "astrid.core.execution.generic_host",
                          "source_closure_digest": source_checkout_closure(source_checkout)["digest"]},
        "profile_revision": "installed-host/v1", "release_digest": digest(records),
        "session_config_digest": digest(launch), **launch,
    }
    value["profile_digest"] = digest(value)
    path = folder / "worker-profile.json"
    write_json(path, value)
    return path
