"""Custody-local witnesses for successful model digest verification.

Like the VibeComfy witness memo, a witness is valid only while the file's stat
identity is unchanged. These records persist that result between preparations;
callers still enforce the declared size, file kind and digest themselves.

Trusting unchanged files after a full verification is the default. Opt in to fresh
checksums on every job with ``models.trust_unchanged: false`` in the Runtime's user
``generation`` settings document,
editable with ``astrid generate settings checkout --file FILE`` and
``astrid generate settings checkin FILE``. The default trusts file metadata, not a
fresh checksum. Absent settings use the default (trust unchanged); an explicit false
forces full hashing. Settings that are present but invalid or unreadable fail closed
to full hashing, the safe direction for integrity. A file without a matching record
is always fully hashed. One preparation snapshots the choice; its model_verification
step log counts full hashes and skips across all model checks.

Model verification records optimize re-verification for trusted writers on reliable storage
metadata. They are not tamper-proof evidence against code running as the engine's user (ComfyUI
and its custom nodes, which this design already trusts: engine_custody 'trusted engine'), nor
against changes that preserve (st_dev, size, mtime_ns, inode, ctime_ns). Before W3, a model-only
change was caught at the next preparation; now a same-user process that also writes a record is
not. A separate isolation boundary (another UID or a sandbox) would be needed to restore that.
"""
from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path

from ._engine_target_files import _TargetFiles
from .engine_contracts import RecipeValidationError

_SCHEMA = "astrid.model-verification.v2"


def _signature(info: os.stat_result) -> list[int]:
    return [info.st_dev, info.st_size, info.st_mtime_ns, info.st_ino, info.st_ctime_ns]


@dataclass
class ModelVerification:
    trust_unchanged: bool = True
    hashed: int = 0
    skipped: int = 0

    def report(self) -> dict[str, bool | int]:
        return {"trust_unchanged": self.trust_unchanged, "hashed": self.hashed, "skipped": self.skipped}


class _VerificationRecord:
    def __init__(self, root, models_root, path, declared_sha256, deadline, *, verification=None):
        self.verification = verification if verification is not None else ModelVerification()
        self.root = str(Path(root).resolve())
        self.models_root = str(Path(models_root).resolve())
        self.path = str(Path(path).resolve())
        self.declared_sha256 = declared_sha256
        self.deadline = deadline
        self.files = _TargetFiles(root)
        self.directory = str(Path(root) / '.astrid-engine/model-verification')
        name = hashlib.sha256(os.fsencode(self.path)).hexdigest() + '.json'
        self.record_path = str(Path(self.directory) / name)

    def _value(self, info, sha256):
        return {"schema": _SCHEMA, "root": self.root, "models_root": self.models_root,
                "path": self.path, "stat": _signature(info), "sha256": sha256,
                "declared_sha256": self.declared_sha256}

    def lookup(self, info):
        if self.verification.trust_unchanged is not True:
            return None
        if not re.fullmatch(r"[0-9a-f]{64}", self.declared_sha256):
            return None
        try:
            value = self.files.read_json(self.record_path, deadline=self.deadline)
        except (OSError, RecipeValidationError):
            return None
        signature = value.get("stat")
        if (not isinstance(signature, list) or len(signature) != 5
                or any(type(part) is not int for part in signature)):
            return None
        if value == self._value(info, self.declared_sha256):
            self.verification.skipped += 1
            return self.declared_sha256
        return None

    def remember(self, info, sha256):
        self.verification.hashed += 1
        # Never write a witness for a full hash that failed its declaration.
        if sha256 != self.declared_sha256:
            return
        try:
            self.files.ensure_dir(self.directory, deadline=self.deadline)
            self.files.write_json_atomic(self.record_path, self._value(info, sha256),
                                         deadline=self.deadline, ensure_ascii=True)
        except (OSError, RecipeValidationError):
            # An optional cache must not change verification or storage admission.
            pass


def model_file_fact(path, *, root, models_root, declared_sha256, deadline, check, verification=None):
    """Hash or reuse one model, through the same guarded descriptor inspection."""
    verification = verification if verification is not None else ModelVerification()
    try:
        record = _VerificationRecord(root, models_root, path, declared_sha256, deadline,
                                     verification=verification)
    except (OSError, RuntimeError, ValueError, TypeError, RecipeValidationError):
        # Unresolvable paths still go through the usual file-kind/path refusal.
        record = None
    fact = _TargetFiles(models_root).file_fact(path, hash_bytes=True, deadline=deadline,
                                              check=check, verification=record)
    if record is None and fact.sha256 is not None:
        verification.hashed += 1
    return fact
