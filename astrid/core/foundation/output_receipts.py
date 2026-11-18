"""Producer output observations carried to Runtime's independent byte check.

These records save repeated hashes inside collection. They are not authority
to import a verified Runtime object; Runtime still checks every uploaded byte.
Source/evidence validation and caller delivery do not use this shortcut.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import stat
from collections.abc import Mapping
from pathlib import Path


OBSERVATIONS_NAME = "managed-generation-result.receipts.json"


def content_receipt(receipt: Mapping, path: str) -> dict:
    """Keep filesystem observations out of the durable producer evidence."""
    return {**receipt, "file_identity": {
        "sha256": receipt["sha256"], "bytes": receipt["bytes"], "path": path,
    }}


def write_output_observations(root: Path, payload: Mapping, receipts: Mapping) -> None:
    """Persist local observations separately from the deterministic envelope."""
    from .atomic_io import write_json_atomic
    write_json_atomic(root / OBSERVATIONS_NAME, {
        "schema_version": 1,
        **{key: payload.get(key) for key in ("task_id", "attempt_id", "producer_run_id")},
        "output_receipts": dict(receipts),
    })


def bind_output_receipts(payload: Mapping, root: Path) -> dict:
    """Bind content receipts to unchanged files; otherwise use ordinary hashing.

    The adjacent observations are an optimization, never upload authority.
    Correlation, logical path, all receipt fields and the current stat tuple
    must agree before the independent Runtime upload check may be deferred to.
    """
    evidence = payload.get("evidence")
    producer = evidence.get("producer") if isinstance(evidence, Mapping) else None
    receipts = producer.get("output_receipts") if isinstance(producer, Mapping) else None
    if not isinstance(receipts, Mapping):
        return {}
    observed = {}
    sidecar = root / OBSERVATIONS_NAME
    try:
        if not sidecar.is_symlink() and stat.S_ISREG(sidecar.lstat().st_mode):
            data = json.loads(sidecar.read_bytes())
            if (isinstance(data, Mapping) and data.get("schema_version") == 1
                    and all(data.get(key) == payload.get(key)
                            for key in ("task_id", "attempt_id", "producer_run_id"))
                    and isinstance(data.get("output_receipts"), Mapping)):
                observed = data["output_receipts"]
    except (OSError, ValueError, TypeError):
        pass
    bound = {}
    for logical_path, public in receipts.items():
        if not isinstance(logical_path, str) or not isinstance(public, Mapping):
            continue
        relative = Path(logical_path)
        if relative.is_absolute() or ".." in relative.parts:
            continue
        path = root / relative
        if not path.resolve().is_relative_to(root.resolve()):
            continue
        # Existing in-process/legacy stat receipts remain supported. New
        # serialized evidence carries only the stable content projection.
        candidate = public if isinstance(public.get("file_identity"), list) else observed.get(logical_path)
        if not isinstance(candidate, Mapping):
            continue
        if candidate is not public:
            try:
                if content_receipt(candidate, logical_path) != public:
                    continue
            except (KeyError, TypeError):
                continue
        try:
            if receipt_digest(path, candidate) is not None:
                bound[logical_path] = dict(candidate)
        except (OSError, ValueError):
            continue
    return bound


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
    before = file_identity(source)
    digest = receipt_digest(source, receipt)
    if file_identity(source) != before:
        raise ValueError("producer output changed before copying")
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
