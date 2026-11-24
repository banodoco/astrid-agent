"""P56 approval data and consent tokens, separate from workflow source refs.

An entry owns ``code-approvals.json`` beside ``entry.json``. A record binds one
repository/commit, recipe hash and resolved placement class. Computing a record
or storing a caller-supplied record is not evidence of a person's consent:
The CLI warning and target admission use the same VibeComfy contract. Tokens are a short confirmation
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

import json
import os
import tempfile
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from .consent import consent_scope

from vibecomfy.node_approvals import (
    NodeApproval as NodeApproval,
    NodeIdentity as NodeIdentity,
    PlacementClass as PlacementClass,
    PLACEMENT_CLASSES as PLACEMENT_CLASSES,
    approval_record as approval_record,
    approval_matches as approval_matches,
    consent_matches as consent_matches,
    consent_token as consent_token,
    require_approved_nodes as require_approved_nodes,
    require_approved_nodes as target_approvals,
    reviewed_approvals as reviewed_approvals,
    validate_approval_record as validate_approval_record,
    validate_approvals as _records,
)

APPROVALS_FILENAME = "code-approvals.json"

# Only the CLI entry transaction may bind already-authorized records to its
# private candidate for a read-only SDK preflight. Nothing in a workflow or
# entry manifest can populate this context, and it is cleared on exit.
_PREVIEW_AUTHORITY: ContextVar[tuple | None] = ContextVar("entry_preview_authority", default=None)


@contextmanager
def preview_entry_authority(entry, records, placement_class):
    """Carry exact prior authorization, never manufacture a consent token."""
    bound = (entry["id"], str(Path(entry["bundle"]).resolve()), placement_class, _records(records))
    token = _PREVIEW_AUTHORITY.set(bound)
    try:
        yield
    finally:
        _PREVIEW_AUTHORITY.reset(token)


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


def entry_approval_directory(entry):
    """Resolve the actual saved entry, never a workflow-authored sidecar path."""
    if not entry or entry.get("source") != "user-entry":
        return None
    from vibecomfy.entries import entries_dir, load_entry

    directory = entries_dir() / entry["id"]
    if directory.parent != entries_dir() or directory.is_symlink():
        raise ValueError("code approvals require a plain saved entry directory")
    saved = load_entry(directory)
    if saved["id"] != entry["id"] or saved["bundle"] != entry["bundle"]:
        raise ValueError("saved entry changed before code approval")
    return directory


def _consent_retry(argv, nodes):
    """Retain the invocation but freeze every offered --add to its displayed SHA."""
    result = []
    pinned = {row["repo"].rstrip("/").removesuffix(".git"): row for row in nodes}
    index = 0
    while index < len(argv):
        value = argv[index]
        index += 1
        if value == "--dry-run" or value.startswith("--dry-run="):
            continue
        if value.startswith("--consent="):
            if value == "--consent=user_env":
                result.append(value)
            continue
        if value == "--consent" and index < len(argv):
            scope = argv[index]
            index += 1
            if scope == "user_env":
                result.extend([value, scope])
            continue
        if value == "--add" or value.startswith("--add="):
            if value == "--add":
                if index == len(argv):
                    raise ValueError("--add requires a pinned repository")
                raw = argv[index]
                index += 1
            else:
                raw = value.split("=", 1)[1]
            repo = raw.rsplit("@", 1)[0] if "@" in raw else raw
            node = pinned.get(repo.rstrip("/").removesuffix(".git"))
            if node is None:
                raise ValueError("consent retry --add does not identify a declared repository")
            result.extend(["--add", f"{node['repo']}@{node['commit']}"])
        else:
            result.append(value)
    return result


def authorize_nodes(nodes, *, placement_class, consent=(), entry=None, retry_argv,
                    persist=False):
    """Obtain one informed yes per exact node; declarations alone approve nothing."""
    from .preflight import GenerationReadinessError, readiness_envelope

    current = [approval_record(node, placement_class=placement_class) for node in nodes]
    # Validate the node set's URL and directory constraints without mutating it.
    require_approved_nodes(nodes, current, placement_class=placement_class)
    directory = entry_approval_directory(entry)
    saved = read_entry_approvals(directory) if directory is not None else []
    preview = _PREVIEW_AUTHORITY.get()
    if (not persist and preview is not None and entry is not None and entry.get("bundle")
            and (entry.get("id"), str(Path(entry["bundle"]).resolve()), placement_class) == preview[:3]):
        saved = [*saved, *preview[3]]
    known = {row["approval_sha256"] for row in [*saved, *reviewed_approvals(nodes, placement_class=placement_class)]}
    choices = [scope for scope in consent if consent_scope(scope) != "user_env"]
    expected_tokens = {consent_token(record) for record in current}
    supplied = {scope.removeprefix("code=") for scope in choices if scope.startswith("code=")}
    invalid = any(scope == "code" for scope in choices) or not supplied <= expected_tokens
    confirmed = {record["approval_sha256"] for node, record in zip(nodes, current)
                 if any(consent_matches(token, node, placement_class=placement_class) for token in supplied)}
    pending = [row for row in current if row["approval_sha256"] not in known
               and row["approval_sha256"] not in confirmed]
    if invalid:
        pending = current
    if pending or invalid:
        then = _consent_retry(list(retry_argv), nodes)
        for record in current:
            if record["approval_sha256"] not in known or invalid:
                then.extend(["--consent", "code=" + consent_token(record)])
        shown = []
        for node, record in zip(nodes, current):
            shown.append(f"{node['repo']}@{node['commit']} (recipe SHA-256 {record['recipe_sha256']}, "
                         f"requirements_txt={node.get('requirements_txt')!r}, "
                         f"pip_no_deps={node.get('pip_no_deps', [])!r}, token {consent_token(record)})")
        warning = ("Show this warning to the person and obtain their approval before running the command. "
                   f"The following third-party code will run on the {placement_class} target with that target's "
                   "access to files, credentials and network. Dependencies are resolved at install time; "
                   "install.py is never run. Approve only these exact repositories, commits and recipes: "
                   + "; ".join(shown))
        raise GenerationReadinessError(readiness_envelope(
            entry={"id": (entry or {}).get("id"), "requirements": {"nodes": nodes}},
            target={"kind": placement_class}, attempt_count=0,
            issues=[{"code": "needs_consent", "severity": "error",
                     "message": "Node code requires approval of the displayed source, recipe and target.",
                     "details": {"approvals": current}, "fix_ids": ["approve-node-code"]}],
            fixes=[{"id": "approve-node-code", "who": "human", "scope": "managed_write",
                    "instruction": {"step": warning, "then_argv": then}}]))
    if persist and directory is not None and supplied:
        records = {row["approval_sha256"]: row for row in saved}
        records.update({row["approval_sha256"]: row for row in current})
        write_entry_approvals(directory, list(records.values()))
    return current
