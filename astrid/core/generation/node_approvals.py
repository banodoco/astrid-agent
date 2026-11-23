"""P56 approval data and consent tokens, separate from workflow source refs.

An entry owns ``code-approvals.json`` beside ``entry.json``. A record binds one
repository/commit, recipe hash and resolved placement class. Computing a record
or storing a caller-supplied record is not evidence of a person's consent:
LE-2b owns the CLI warning and target admission. Tokens are a short confirmation
of what was shown, not a secret, a signature or proof of who typed them.

Classes follow the resolved ``Placement.kind``, not the command's selector:
``local`` is Astrid's managed ComfyUI on the local machine (the ``managed``
selector resolves to this class); ``attached`` is the person's own ComfyUI,
including a loopback endpoint, which Astrid does not own; ``runpod`` is a rented
RunPod machine. An attached endpoint never becomes ``local`` merely because it
is on the same host. Resolve ``auto`` before constructing the approval; target
admission maps its managed/attached/pod transport to these placement classes.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal, TypedDict

PlacementClass = Literal["local", "attached", "runpod"]
PLACEMENT_CLASSES = frozenset({"local", "attached", "runpod"})
APPROVALS_FILENAME = "code-approvals.json"
_CONTEXT = b"astrid.node-code-approval.v1\0"
_SHA256 = re.compile(r"[0-9a-f]{64}")


class NodeIdentity(TypedDict):
    repo: str
    commit: str


class NodeApproval(TypedDict):
    identity: NodeIdentity
    recipe_sha256: str
    approval_sha256: str
    placement_class: PlacementClass


def _identity(value: Any) -> NodeIdentity:
    if not isinstance(value, Mapping) or set(value) != {"repo", "commit"}:
        raise ValueError("node approval identity must contain repo and commit")
    repo, commit = value["repo"], value["commit"]
    if (not isinstance(repo, str) or not repo.strip() or repo != repo.strip()
            or any(ord(char) < 32 or ord(char) == 127 for char in repo)):
        raise ValueError("node approval repo must be a nonempty source identity")
    if not isinstance(commit, str) or re.fullmatch(r"[0-9a-f]{40}", commit) is None:
        raise ValueError("node approval commit must be a full lowercase 40-hex commit")
    return {"repo": repo, "commit": commit}


def _approval_hash(identity: NodeIdentity, recipe_hash: str, placement_class: str) -> str:
    payload = {"identity": identity, "recipe_sha256": recipe_hash, "placement_class": placement_class}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(_CONTEXT + encoded).hexdigest()


def approval_record(node: Mapping[str, Any], *, placement_class: PlacementClass) -> NodeApproval:
    """Describe one manifest node; callers must obtain consent before saving it."""
    from vibecomfy.node_recipes import RECIPE_FIELDS, recipe_sha256

    if (not isinstance(node, Mapping) or not {"repo", "commit"} <= set(node)
            or set(node) - ({"repo", "commit"} | RECIPE_FIELDS)):
        raise ValueError("approval requires the closed extra.nodes shape")
    identity = _identity({key: node[key] for key in ("repo", "commit")})
    if not isinstance(placement_class, str) or placement_class not in PLACEMENT_CLASSES:
        raise ValueError("approval needs a resolved placement class: local, attached or runpod")
    recipe_hash = recipe_sha256({key: node[key] for key in RECIPE_FIELDS if key in node})
    return {"identity": identity, "recipe_sha256": recipe_hash,
            "approval_sha256": _approval_hash(identity, recipe_hash, placement_class),
            "placement_class": placement_class}


def validate_approval_record(record: Any) -> NodeApproval:
    """Validate/copy stored data; this proves consistency, never authenticity."""
    if not isinstance(record, Mapping) or set(record) != {"identity", "recipe_sha256", "approval_sha256", "placement_class"}:
        raise ValueError("invalid node approval record shape")
    identity = _identity(record["identity"])
    placement = record["placement_class"]
    if not isinstance(placement, str) or placement not in PLACEMENT_CLASSES:
        raise ValueError("invalid approval placement class")
    recipe_hash, approval_hash = record["recipe_sha256"], record["approval_sha256"]
    if any(not isinstance(value, str) or _SHA256.fullmatch(value) is None for value in (recipe_hash, approval_hash)):
        raise ValueError("approval hashes must be lowercase SHA-256 values")
    expected = _approval_hash(identity, recipe_hash, placement)
    if not hmac.compare_digest(approval_hash, expected):
        raise ValueError("node approval record does not match its identity, recipe and placement")
    return {"identity": identity, "recipe_sha256": recipe_hash, "approval_sha256": expected, "placement_class": placement}


def consent_token(record: Mapping[str, Any]) -> str:
    """The 12 lowercase hex characters shown with the warning."""
    return validate_approval_record(record)["approval_sha256"][:12]


def consent_matches(token: Any, node: Mapping[str, Any], *, placement_class: PlacementClass) -> bool:
    if not isinstance(token, str) or re.fullmatch(r"[0-9a-f]{12}", token) is None:
        return False
    return hmac.compare_digest(token, consent_token(approval_record(node, placement_class=placement_class)))


def approval_matches(record: Mapping[str, Any], node: Mapping[str, Any], *, placement_class: PlacementClass) -> bool:
    saved = validate_approval_record(record)
    current = approval_record(node, placement_class=placement_class)
    return hmac.compare_digest(saved["approval_sha256"], current["approval_sha256"])


def _records(value: Any) -> list[NodeApproval]:
    if not isinstance(value, (list, tuple)):
        raise ValueError("entry code approvals must be an array")
    records = [validate_approval_record(record) for record in value]
    if len({row["approval_sha256"] for row in records}) != len(records):
        raise ValueError("duplicate entry code approval")
    return records


def read_entry_approvals(entry_directory: str | Path) -> list[NodeApproval]:
    """Read only the entry's approval sidecar; source declarations imply none."""
    path = Path(entry_directory) / APPROVALS_FILENAME
    if path.is_symlink():
        raise ValueError("entry code approval sidecar must not be a symlink")
    try:
        return _records(json.loads(path.read_text(encoding="utf-8")))
    except FileNotFoundError:
        return []


def write_entry_approvals(entry_directory: str | Path, records: list[NodeApproval]) -> None:
    """Atomically replace a complete set after the caller has verified consent.

    This is not a read/append operation and does not coordinate concurrent
    entry replacement; the entry transaction owns that serialization.
    """
    payload = _records(records)
    directory = Path(entry_directory)
    if not (directory / "entry.json").is_file():
        raise ValueError("code approvals belong beside an existing entry.json")
    fd, name = tempfile.mkstemp(prefix=".code-approvals-", suffix=".json", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(payload, sort_keys=True, indent=2) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, directory / APPROVALS_FILENAME)
    finally:
        Path(name).unlink(missing_ok=True)
