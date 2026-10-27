"""F1 input preparation on the existing download path; never executes updates.

fetch_update_inputs is the single future verified-fetch integration hook. Local
byte digests bind selection to consumption, and make no upstream trust claim.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path
import sys

from runtime_protocol import platform
from . import tools

MAX_MANIFEST_BYTES = 1024 * 1024
MAX_BOOTSTRAP_BYTES = 2 * 1024 * 1024


def _selected_bytes(path: Path, limit: int) -> bytes:
    if path.is_symlink() or not path.is_file() or not platform.is_owner_only(path):
        raise ValueError("Selected update inputs must be private regular files.")
    with path.open("rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("Selected update input is not regular.")
        raw = stream.read(limit + 1)
    if not raw or len(raw) > limit:
        raise ValueError("Selected update input is empty or too large.")
    return raw


def validate_update_inputs(directory: Path, metadata: dict, installed_release: int) -> dict:
    values = {}
    for name, limit in (("manifest", MAX_MANIFEST_BYTES), ("bootstrap", MAX_BOOTSTRAP_BYTES)):
        raw = _selected_bytes(directory / name, limit)
        if not raw or len(raw) > limit or hashlib.sha256(raw).hexdigest() != metadata[name + "_sha256"]:
            raise ValueError("Selected update input changed or is invalid.")
        values[name] = raw
    manifest = tools.validate_manifest(json.loads(values["manifest"]))
    release = manifest.get("release", 0)
    if (type(installed_release) is not int or installed_release < 0
            or type(release) is not int or release <= installed_release or metadata["release"] != release):
        raise ValueError("The target release must be newer than the installed release.")
    return metadata


def fetch_update_inputs(directory: Path, *, manifest_url: str, bootstrap_url: str,
                        installed_release: int) -> dict:
    """Download once and validate with today's optional-hash policy.

    URLs are supplied only by Runtime's fixed server policy. No environment URL
    override, signature implementation, or verification bypass is introduced.
    Callers own this private directory for the full selection lifetime.
    """
    platform.private_directory(directory)
    metadata = {}
    for name, url in (("manifest", manifest_url), ("bootstrap", bootstrap_url)):
        path = directory / name
        # tools.download opens/truncates this owner-only file in place.
        descriptor, temporary = platform.private_temporary(directory, name + "-")
        os.close(descriptor)
        Path(temporary).replace(path)
        tools.download(url, path)
        raw = _selected_bytes(path, MAX_MANIFEST_BYTES if name == "manifest" else MAX_BOOTSTRAP_BYTES)
        metadata[name + "_path"] = str(path)
        metadata[name + "_sha256"] = hashlib.sha256(raw).hexdigest()
        if name == "manifest":
            manifest = tools.validate_manifest(json.loads(raw))
            metadata["release"] = manifest.get("release", 0)
            if type(installed_release) is not int or installed_release < 0 or metadata["release"] <= installed_release:
                raise ValueError("The target release must be newer than the installed release.")
    return validate_update_inputs(directory, metadata, installed_release)


def main():
    # Internal selected-Python helper, not a browser-controlled command.
    value = json.loads(sys.stdin.read(16384))
    metadata = fetch_update_inputs(Path(value["directory"]), manifest_url=value["manifest_url"],
                                   bootstrap_url=value["bootstrap_url"],
                                   installed_release=value["installed_release"])
    print(json.dumps(metadata))


if __name__ == "__main__":
    main()
