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
to full hashing, the safe direction for integrity. A file without a matching witness
is fully hashed. One preparation snapshots the choice; its model_verification
step log counts full hashes and skips and gives a reason for each model check.

Records live with the models and use relative paths, size, mtime_ns and inode, so
new pods can reuse them on a warm volume. The key deliberately omits ctime and
st_dev inside the same trusted same-user boundary. If inode is unstable on a
filesystem, the fail-safe result is one re-hash per new pod.

A VibeComfy post-download receipt is accepted as the first witness when
trust_unchanged is on, then promoted to an Astrid record. Its key also omits ctime
and ignores st_dev by design for persistent-volume remounts. This is a slightly
weaker witness inside the same trusted same-user boundary, and a remount does not
force a re-hash wherever such a receipt exists. Both trade-offs were accepted by
the orchestrator under the owner's 14 Nov ruling.

Model verification records optimize re-verification for trusted writers on reliable storage
metadata. They are not tamper-proof evidence against code running as the engine's user (ComfyUI
and its custom nodes, which this design already trusts: engine_custody 'trusted engine'), nor
against changes that preserve (size, mtime_ns, inode). Before W3, a model-only
change was caught at the next preparation; now a same-user process that also writes a record is
not. A separate isolation boundary (another UID or a sandbox) would be needed to restore that.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from ._engine_target_files import _TargetFiles
from .engine_contracts import RecipeValidationError

_SCHEMA = "astrid.model-verification.v3"


def _signature(info: os.stat_result) -> list[int]:
    return [info.st_size, info.st_mtime_ns, info.st_ino]


@dataclass
class ModelVerification:
    trust_unchanged: bool = True
    hashed: int = 0
    skipped: int = 0
    files: list[dict[str, str]] = field(default_factory=list)

    def report(self) -> dict[str, bool | int]:
        return {"trust_unchanged": self.trust_unchanged, "hashed": self.hashed, "skipped": self.skipped}

    def as_stdout(self) -> str:
        return json.dumps({**self.report(), "files": self.files}, sort_keys=True) + "\n"


class _VerificationRecord:
    def __init__(self, root, models_root, path, declared_sha256, deadline, *, verification=None):
        self.verification = verification if verification is not None else ModelVerification()
        self.models_root = str(Path(models_root).resolve())
        self.path = str(Path(path).resolve())
        self.relative_path = Path(self.path).relative_to(self.models_root).as_posix()
        self.declared_sha256 = declared_sha256
        self.deadline = deadline
        self.files = _TargetFiles(self.models_root)
        self.directory = str(Path(self.models_root) / '.astrid/model-verification')
        # surrogatepass preserves support for POSIX non-UTF-8 filenames; normal
        # model paths use their ordinary UTF-8, POSIX relative-path encoding.
        name = hashlib.sha256(self.relative_path.encode('utf-8', errors='surrogatepass')).hexdigest() + '.json'
        self.record_path = str(Path(self.directory) / name)
        self.hash_outcome = 'hashed_new'

    def _value(self, info, sha256):
        return {"schema": _SCHEMA, "path": self.relative_path, "stat": _signature(info), "sha256": sha256,
                "declared_sha256": self.declared_sha256}

    def _read(self, path):
        """Return the optional witness and whether something existed at its path."""
        try:
            return self.files.read_json(path, deadline=self.deadline), True
        except (OSError, ValueError, TypeError, RecipeValidationError):
            try:
                exists = self.files.file_fact(path, hash_bytes=False, deadline=self.deadline).exists
            except (OSError, RecipeValidationError):
                exists = True  # Inaccessible witnesses count as changed, never trusted.
            return None, exists

    def _vibecomfy_matches(self, info):
        try:
            key = hashlib.sha256(self.path.encode('utf-8')).hexdigest()
        except UnicodeError:
            return False, False
        path = str(Path(self.models_root) / '.vibecomfy/model-verification' / f'{key}.json')
        value, existed = self._read(path)
        if value is None:
            return False, existed
        signature = value.get('stat')
        expected = value.get('expected_sha256')
        matches = (
            type(value.get('schema_version')) is int and value['schema_version'] == 1
            and value.get('path') == self.path
            and isinstance(expected, str) and expected.lower() == self.declared_sha256
            and value.get('actual_sha256') == self.declared_sha256
            and isinstance(signature, dict)
            and all(type(signature.get(key)) is int and signature[key] == current
                    for key, current in (('inode', info.st_ino), ('size', info.st_size),
                                         ('mtime_ns', info.st_mtime_ns)))
        )
        return matches, existed

    def _outcome(self, outcome):
        self.verification.files.append({'path': self.relative_path, 'outcome': outcome})

    def lookup(self, info):
        if self.verification.trust_unchanged is not True:
            self.hash_outcome = 'hashed_opt_in'
            return None
        if not re.fullmatch(r"[0-9a-f]{64}", self.declared_sha256):
            return None
        value, existed = self._read(self.record_path)
        signature = value.get('stat') if value is not None else None
        if (isinstance(signature, list) and len(signature) == 3
                and all(type(part) is int for part in signature)
                and value == self._value(info, self.declared_sha256)):
            self.verification.skipped += 1
            self._outcome('trusted_record')
            return self.declared_sha256
        matches, receipt_existed = self._vibecomfy_matches(info)
        if matches:
            self.verification.skipped += 1
            self._outcome('trusted_vibecomfy_receipt')
            self._write(info, self.declared_sha256)
            return self.declared_sha256
        if existed or receipt_existed:
            self.hash_outcome = 'hashed_changed'
        return None

    def remember(self, info, sha256):
        self.verification.hashed += 1
        self._outcome(self.hash_outcome)
        # Never write a witness for a full hash that failed its declaration.
        if sha256 != self.declared_sha256:
            return
        self._write(info, sha256)

    def _write(self, info, sha256):
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
        try:
            relative = Path(path).resolve().relative_to(Path(models_root).resolve()).as_posix()
        except (OSError, RuntimeError, ValueError):
            # The log row is diagnostic; it must never fail a verified preparation.
            relative = Path(path).name
        verification.files.append({'path': relative, 'outcome': (
            'hashed_new' if verification.trust_unchanged is True else 'hashed_opt_in')})
    return fact
