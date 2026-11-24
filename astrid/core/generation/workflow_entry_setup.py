"""LE-12 entry collision recovery and preservation of authorized records."""
from __future__ import annotations

from collections.abc import Sequence

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
