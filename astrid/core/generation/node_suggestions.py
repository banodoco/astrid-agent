"""Read-only missing-node suggestions for Plan 56 (LE-5).

The resolver owns discovery and evidence. This module only derives queries and
projects its result; a suggestion is not an install plan or consent.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

_WORD = re.compile(r"[A-Z]+(?=[A-Z][a-z]|\d|$)|[A-Z]?[a-z]+|[A-Z]+|\d+")


def _name_query(classes: tuple[str, ...], workflow: Any) -> str | None:
    report = getattr(workflow, "_astrid_import_report", None)
    if isinstance(report, Mapping):
        provenance = report.get("provenance", {}).get("source_provenance", {})
        hints = {
            row.get("cnr_id") or row.get("aux_id")
            for row in provenance.get("records", ())
            if row.get("class_type") in classes and row.get("execution_looking")
            and (row.get("cnr_id") or row.get("aux_id"))
        }
        hints.discard("comfy-core")
        if len(hints) == 1:
            return hints.pop()
        if len(hints) > 1:
            return None  # LE-16 groups multiple packs; do not choose one here.
    words = [_WORD.findall(name) for name in classes]
    if not words:
        return None
    if len(words) == 1:
        # Drop the final class-specific word, if there is a prefix.
        return " ".join(words[0][:-1]) or None
    shared = []
    for column in zip(*words):
        if len({word.casefold() for word in column}) != 1:
            break
        shared.append(column[0])
    return " ".join(shared) or None


def missing_node_suggestions(workflow: Any, missing_classes: Iterable[str]) -> dict[str, Any]:
    """Look up each distinct missing class once and one evidence-derived name.

    Results retain the resolver's warnings and per-tier status, including
    unavailable tiers. Neither an exact-class match nor a name match is
    runnable. In particular, the latter must not become a command.
    """
    from vibecomfy.registry.pack_resolver import resolve_missing_nodes

    classes = tuple(sorted(set(missing_classes)))
    queries = [(name, "class_name") for name in classes]
    name_query = _name_query(classes, workflow)
    if name_query:
        queries.append((name_query, "capability"))

    lookups = []
    candidates: dict[str, dict[str, Any]] = {}
    for query, intent in queries:
        result = resolve_missing_nodes(query, query_intent=intent).to_dict()
        lookups.append(result)
        for candidate in result["candidates"]:
            url = candidate["pack"].get("url")
            if not url:
                continue  # an archive or pack name alone is not an --add source
            row = candidates.setdefault(url, {
                "url": url, "matched_classes": [], "evidence": [],
                "runnable": False, "add_option": ["--add", url],
            })
            row["matched_classes"] = sorted(
                set(row["matched_classes"]) | (set(candidate["expected_classes"]) & set(classes))
            )
            row["evidence"].append({"query": query, "query_intent": intent, "candidate": candidate})

    for row in candidates.values():
        row["label"] = "class match" if row["matched_classes"] else "name match only"
    return {
        "classes": list(classes),
        "lookups": lookups,
        "candidates": list(candidates.values()),
        "instruction": (
            "Choose and verify the repository that provides the missing classes; "
            "then supply its URL with --add. Lookup does not install or approve code."
            if candidates else
            "Find the repository that provides the missing classes manually; "
            "then supply its URL with --add. If none can be found, stop."
        ),
        "runnable": False,
    }
