"""Private, inherited-FD custody for an authenticated generation prompt object."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class _PromptSnapshot:
    path: str
    object_id: str
    content: bytes


class _PromptSnapshotInputs(dict):
    """Action inputs whose private custody data never enters the public mapping."""

    _prompt_snapshot: _PromptSnapshot | None = None


_installed_prompt_snapshot: _PromptSnapshot | None = None


def _prompt_snapshot_transport(inputs: Any):
    """Return an anonymous launch FD holding retained bytes, without a path read."""
    snapshot = inputs._prompt_snapshot if isinstance(inputs, _PromptSnapshotInputs) else None
    if snapshot is None:
        return None
    if (type(snapshot.content) is not bytes
            or "sha256:" + hashlib.sha256(snapshot.content).hexdigest() != snapshot.object_id
            or inputs.get("prompts_file") != snapshot.path):
        raise ValueError("retained generation prompt binding changed")
    stream = tempfile.TemporaryFile(mode="w+b")
    try:
        header = {"path": snapshot.path, "object_id": snapshot.object_id, "size": len(snapshot.content)}
        stream.write(json.dumps(header, sort_keys=True).encode("utf-8") + b"\n")
        stream.write(snapshot.content)
        stream.seek(0)
        return stream
    except BaseException:
        stream.close()
        raise


def _install_prompt_snapshot(fd: str | int) -> None:
    """Consume the inherited private handoff before any action code executes."""
    global _installed_prompt_snapshot
    descriptor = int(fd)
    os.set_inheritable(descriptor, False)
    with os.fdopen(descriptor, "rb") as stream:
        header_bytes = stream.readline(16385)
        if len(header_bytes) > 16384 or not header_bytes.endswith(b"\n"):
            raise ValueError("invalid inherited prompt snapshot header")
        header = json.loads(header_bytes)
        if (type(header) is not dict or set(header) != {"path", "object_id", "size"}
                or type(header["path"]) is not str or not os.path.isabs(header["path"])
                or type(header["object_id"]) is not str or type(header["size"]) is not int
                or not 0 <= header["size"] <= 5 * 1024 ** 3):
            raise ValueError("invalid inherited prompt snapshot binding")
        content = stream.read(header["size"] + 1)
        if (len(content) != header["size"]
                or "sha256:" + hashlib.sha256(content).hexdigest() != header["object_id"]):
            raise ValueError("inherited prompt snapshot failed digest verification")
    if _installed_prompt_snapshot is not None:
        raise ValueError("generation prompt snapshot already installed")
    _installed_prompt_snapshot = _PromptSnapshot(header["path"], header["object_id"], content)


def _retained_prompt_bytes(path: Path) -> bytes | None:
    """Use the lexical launch binding; never resolve or reopen a staged pathname."""
    snapshot = _installed_prompt_snapshot
    if snapshot is None:
        return None
    if os.path.abspath(path) != snapshot.path:
        raise ValueError("generation prompt path differs from inherited snapshot")
    return snapshot.content
