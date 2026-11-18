"""Producer output observations carried to Runtime's independent byte check.

These records save repeated hashes inside collection. They are not authority
to import a verified Runtime object; Runtime still checks every uploaded byte.
Source/evidence validation and caller delivery do not use this shortcut.
"""
from __future__ import annotations

import hashlib
import re
import shutil
import stat
from collections.abc import Mapping
from pathlib import Path


def file_identity(path: Path) -> list[int]:
    value = path.lstat()
    if not stat.S_ISREG(value.st_mode):
        raise ValueError("output receipt requires a regular file")
    return [value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns]


def make_output_receipt(path: Path, digest: str, size: int, *, prompt_id: str | None,
                        descriptor: Mapping | None = None, producer: str = "fetch") -> dict:
    identity = file_identity(path)
    digest = digest.removeprefix("sha256:")
    if not re.fullmatch(r"[0-9a-f]{64}", digest) or identity[2] != size:
        raise ValueError("output receipt identity does not match the produced file")
    return {"version": 1, "sha256": digest, "bytes": size, "file_identity": identity,
            "prompt_id": prompt_id, "descriptor": dict(descriptor or {}), "producer": producer}


def receipt_digest(path: Path, receipt: object) -> str | None:
    if not isinstance(receipt, Mapping) or receipt.get("version") != 1:
        return None
    digest = receipt.get("sha256")
    if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        return None
    try:
        identity = file_identity(path)
    except OSError:
        return None
    if receipt.get("file_identity") != identity or receipt.get("bytes") != identity[2]:
        return None
    return digest


def output_digest(path: Path, receipt: object = None) -> str:
    observed = receipt_digest(path, receipt)
    if observed is not None:
        return observed
    # A stale observation is never used for a changed file. The ordinary
    # verifier still compares this result with the declared output digest.
    from .hash import sha256_file
    return sha256_file(path)


def copy_output_receipt(source: Path, destination: Path, receipt: object) -> dict | None:
    digest = receipt_digest(source, receipt)
    before = file_identity(source)
    shutil.copy2(source, destination)
    if file_identity(source) != before:
        raise ValueError("producer output changed while copying")
    if digest is None:
        return None
    return make_output_receipt(destination, digest, before[2], prompt_id=receipt.get("prompt_id"),
                               descriptor=receipt.get("descriptor"), producer=receipt.get("producer", "fetch"))


def transformed_output_receipt(path: Path, previous: Mapping | None = None) -> dict:
    before = file_identity(path)
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    if file_identity(path) != before:
        raise ValueError("transformed output changed while hashing")
    receipt = make_output_receipt(path, digest.hexdigest(), before[2],
                                  prompt_id=(previous or {}).get("prompt_id"), producer="transform")
    if previous is not None:
        receipt["source_sha256"] = previous.get("sha256")
    return receipt
