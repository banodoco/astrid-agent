"""LE-12 entry collision recovery and preservation of authorized records."""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
import re

from .preflight import GenerationReadinessError, generation_error
from .workflow_additions import repair_workflow_argv


def entry_exists_error(
    error: FileExistsError, *, entry_id: str, retry_argv: Sequence[str],
) -> GenerationReadinessError:
    """Offer explicit replacement only for a known, replaceable saved entry.

    The caller establishes that the destination is a real user entry. A
    foreign directory or symlink is not a replaceable entry.
    """
    argv, placeholders = repair_workflow_argv(retry_argv, replace_entry=True)
    return generation_error(
        error, code="entry_exists", entry={"id": entry_id},
        details={"field": "as_entry", "entry_id": entry_id},
        fixes=[{
            "id": "replace-entry", "who": "human", "scope": "managed_write",
            "instruction": {
                "step": "If you intend to replace the saved entry, explicitly retry with "
                        "--replace. Earlier explicit pack declarations and old code approval "
                        "records are retained; changed commits need fresh consent.",
                "then_argv": argv,
            },
            "placeholders": placeholders,
        }],
        attempt_count=0,
    )


def retained_entry_approvals(previous, authorized):
    """Keep every prior record, adding only newly authorized exact records.

    Both inputs are records already obtained from the canonical validator or
    the authorization boundary. This function validates consistency, not
    consent; callers must authorize the current node set before publication.
    """
    from .node_approvals import validate_approval_record

    records = {}
    for record in [*previous, *authorized]:
        checked = validate_approval_record(record)
        records.setdefault(checked["approval_sha256"], checked)
    return list(records.values())


# All candidate construction uses the same VibeComfy import and selection
# implementation as final publication. No second entry format or resolver.
@dataclass
class PreparedEntry:
    entry: dict
    added_refs: list
    approvals: list
    previous: dict | None
    previous_records: list


def _previous_entry(entry_id: str, *, replace: bool, retry_argv):
    from vibecomfy.entries import entries_dir, load_entry

    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", entry_id) is None:
        raise generation_error(ValueError("invalid entry id"), code="input_rejected",
                               details={"field": "as_entry"})
    directory = entries_dir() / entry_id
    if not directory.exists() and not directory.is_symlink():
        return None
    manifest = directory / "entry.json"
    if directory.is_symlink() or not directory.is_dir() or manifest.is_symlink() or not manifest.is_file():
        raise generation_error(ValueError(f"not a replaceable entry: {directory}"),
                               code="input_rejected", details={"field": "replace"})
    previous = dict(load_entry(manifest), source="user-entry")
    if previous["id"] != entry_id:
        raise generation_error(ValueError("entry id does not match destination"),
                               code="input_rejected", details={"field": "as_entry"})
    if not replace:
        raise entry_exists_error(FileExistsError(f"entry already exists: {directory}"),
                                 entry_id=entry_id, retry_argv=retry_argv)
    return previous


def select_entry_mode(entry, mode):
    from astrid.sdk.generation import _explicit_workflow_needs

    mode = mode or (entry["modes"][0] if len(entry["modes"]) == 1 else "workflow")
    if mode not in entry["modes"]:
        entry = dict(entry, modes=[mode], needs=_explicit_workflow_needs(entry["needs"]))
    return mode, entry


def _source_nodes(entry):
    from astrid.core.generation import entries

    # Load the canonical pair emitted by the actual importer, not a mutable
    # authored metadata view. This includes every explicit added ref before
    # asking for authorization, and canonical recipe validation is shared.
    workflow = entries._load_entry_workflow(entry)
    return entries.engine_table()["comfyui"].workflow_requirements([workflow])["extra"]["nodes"]


def prepare_entry_candidate(source, entry_id, *, replace, additions, placement_class,
                            consent, retry_argv, scratch):
    from vibecomfy.custom_node_refs import CustomNodeRefConflict
    from vibecomfy.entries import entry_added_node_refs, import_as_entry, merge_entry_added_node_refs

    from .node_approvals import authorize_nodes, entry_approval_directory, read_entry_approvals
    from .workflow_additions import resolve_workflow_additions

    previous = _previous_entry(entry_id, replace=replace, retry_argv=retry_argv)
    previous_dir = entry_approval_directory(previous)
    previous_records = read_entry_approvals(previous_dir) if previous_dir is not None else []
    refs = resolve_workflow_additions(additions, retry_argv=retry_argv)
    retained = entry_added_node_refs(previous) if previous is not None else []
    selected = merge_entry_added_node_refs(retained, refs)
    try:
        entry = import_as_entry(source, entry_id, directory=scratch, added_node_refs=selected)
        nodes = _source_nodes(entry)
    except CustomNodeRefConflict as exc:
        argv, placeholders = repair_workflow_argv(retry_argv, correct_add_count=len(additions))
        raise generation_error(exc, code="pin_conflict", details={"field": "add"},
            fixes=[{"id": "choose-consistent-pin", "who": "human", "scope": "managed_write",
                    "instruction": {
                        "step": "Choose one consistent repository and commit. Correct conflicting authored "
                                "workflow pins or explicit additions before retrying.",
                        "then_argv": argv,
                    }, "placeholders": placeholders}]) from exc
    current = authorize_nodes(nodes, placement_class=placement_class, consent=consent,
                              entry=previous, retry_argv=retry_argv)
    return PreparedEntry(entry, refs, current, previous, previous_records)


def publish_entry_candidate(source, prepared, *, replace, placement_class, retry_argv):
    from vibecomfy.entries import import_as_entry

    from .node_approvals import (
        entry_approval_directory, read_entry_approvals, require_approved_nodes, write_entry_approvals,
    )

    # The saved entry may have changed during preview. Refuse rather than
    # silently replacing a different manifest or dropping newly saved records.
    entry_id = prepared.entry["id"]
    current = _previous_entry(entry_id, replace=replace, retry_argv=retry_argv)
    directory = entry_approval_directory(current)
    saved = read_entry_approvals(directory) if directory is not None else []
    if current != prepared.previous or saved != prepared.previous_records:
        raise generation_error(ValueError("saved entry or its approvals changed during preparation; retry"),
                               code="pin_conflict", details={"field": "replace"},
                               fixes=[{"id": "review-current-entry", "who": "human", "scope": "read_only",
                                       "instruction": {"step": "Review the current saved entry and its pins before replacing it.",
                                                       "then_argv": ["astrid", "generate", "describe", "--entry", entry_id, "--json"]}}])
    try:
        entry = import_as_entry(source, entry_id, replace=replace, added_node_refs=prepared.added_refs)
    except FileExistsError as exc:
        # Only an actual replaceable user entry gets the explicit replacement
        # fix. A symlink or foreign directory retains its ordinary refusal.
        _previous_entry(entry_id, replace=replace, retry_argv=retry_argv)
        raise exc
    entry = dict(entry, source="user-entry")
    actual_nodes = _source_nodes(entry)
    actual = require_approved_nodes(actual_nodes, prepared.approvals, placement_class=placement_class)
    # Do not silently grant a new reviewed/approved record for an import
    # that differs from the frozen preview. The canonical writer owns
    # publication, and the final node set must match what was authorized.
    if {row["approval_sha256"] for row in actual} != {row["approval_sha256"] for row in prepared.approvals}:
        raise generation_error(ValueError("published node declarations differ from the authorized candidate"),
            code="pin_conflict", details={"field": "add"},
            fixes=[{"id": "review-published-entry", "who": "human", "scope": "read_only",
                    "instruction": {"step": "Inspect the saved entry and resolve its changed pins before running it.",
                                    "then_argv": ["astrid", "generate", "describe", "--entry", entry_id, "--json"]}}])
    merged = retained_entry_approvals(prepared.previous_records, prepared.approvals)
    directory = entry_approval_directory(entry)
    if prepared.previous is None or merged != prepared.previous_records:
        write_entry_approvals(directory, merged)
    return entry


# Reuse the authorization boundary for the private candidate, not a token
# constructed by the CLI from the caller's requested refs.
from .node_approvals import preview_entry_authority as preview_entry_authority
