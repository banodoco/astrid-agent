"""Read the V3 working-copy journal and verify its append-only hash chain."""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from astrid.core.cli.domain_output import DomainResult, print_result
from astrid.sdk.contracts import ErrorObject
from astrid.sdk.timeline import journal, store


def configure_log(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("ref", help="Timeline UUID, ULID, or slug.")
    parser.add_argument("--project", required=True, help="Owning project id or immutable slug.")
    parser.add_argument("--since", default=None, help="A sequence number or ISO timestamp.")
    parser.add_argument("--actor", default=None, help="Actor id or kind:id.")
    parser.add_argument("--op", default=None, help="Operation label or operation id.")
    parser.add_argument("--cut", default=None, help="Only records that touched this cut.")
    parser.add_argument("--clip", default=None, help="Only records that touched this clip.")
    parser.add_argument("--outcome", default=None, help="Only records with this outcome.")
    parser.add_argument("--json", action="store_true", help="Print the exact five-key product envelope.")
    parser.add_argument("--verify", action="store_true", help="Verify the journal hash chain.")
    parser.set_defaults(handler=run_log)


def _row(record: Any) -> str:
    actor = f"{record.actor.get('kind')}:{record.actor.get('id')}"
    cuts = f"  ({', '.join(record.cuts)})" if record.cuts else ""
    return (f"{record.seq:>5}  {record.at}  {actor:<22}  {record.source.get('via', ''):<8}  "
            f"{record.kind}/{record.outcome}  {record.label}{cuts}")


def run_log(parsed: Any) -> int:
    project_id, timeline_id, _head = store.resolve_ids(
        parsed.project, parsed.ref, client=parsed.client
    )
    path = journal.journal_path(project_id, timeline_id)
    if parsed.verify:
        problems = journal.verify(path)
        records = journal.read(path)
        data = {"records": len(records), "ok": not problems, "problems": problems}
        if parsed.json:
            if problems:
                result = DomainResult.failure(
                    ErrorObject(
                        code="validation_error",
                        message="timeline journal hash chain is broken",
                        details={"problem_count": len(problems)},
                    )
                )
            else:
                result = DomainResult.success(data)
            return print_result(result, as_json=True)
        if not path.is_file():
            print(f"no journal yet for {parsed.ref}")
        elif problems:
            print(f"journal of {parsed.ref}: BROKEN, {len(problems)} problem(s) in {len(records)} record(s)")
            for problem in problems:
                print(f"  {problem}")
        else:
            print(f"journal of {parsed.ref}: chain intact, {len(records)} record(s)")
        return 1 if problems else 0

    if not path.is_file():
        records = []
    else:
        try:
            records = journal.query(
                journal.read(path), since=parsed.since, actor=parsed.actor, op=parsed.op,
                cut=parsed.cut, clip=parsed.clip, outcome=parsed.outcome,
            )
        except ValueError as exc:
            print(f"error validation_error: {exc}", file=sys.stderr)
            return 2
    if parsed.json:
        return print_result(DomainResult.success({"records": [row.to_json() for row in records]}), as_json=True)
    if not records:
        print(f"no journal records match for {parsed.ref}")
        return 0
    print("\n".join(_row(record) for record in records))
    return 0
