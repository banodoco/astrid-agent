"""Read-only missing-node suggestions for Plan 56 (LE-5).

The resolver owns discovery and evidence. This module only derives queries and
projects its result; a suggestion is not an install plan or consent.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

_WORD = re.compile(r"[A-Z]+(?=[A-Z][a-z]|\d|$)|[A-Z]?[a-z]+|[A-Z]+|\d+")


def _provenance_hints(classes: tuple[str, ...], import_report: Mapping[str, Any] | None) -> set[str]:
    # None is an explicit observation from a loader that did not import JSON.
    if import_report is None:
        return set()
    provenance = import_report["provenance"]["source_provenance"]
    hints = {
        row.get("cnr_id") or row.get("aux_id")
        for row in provenance["records"]
        if row.get("class_type") in classes and row.get("execution_looking")
        and (row.get("cnr_id") or row.get("aux_id"))
    }
    hints.discard("comfy-core")
    return hints


def _name_query(classes: tuple[str, ...], import_report: Mapping[str, Any] | None) -> str | None:
    hints = _provenance_hints(classes, import_report)
    if len(hints) == 1:
        return hints.pop()
    if len(hints) > 1:
        return None  # Conflicting provenance is not evidence of one pack.
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


def missing_node_suggestions(
    missing_classes: Iterable[str], *, import_report: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Look up each distinct missing class once and one evidence-derived name.

    Results retain the resolver's warnings and per-tier status, including
    unavailable tiers. Neither an exact-class match nor a name match is
    runnable. In particular, the latter must not become a command.
    """
    from vibecomfy.registry.pack_resolver import resolve_missing_nodes

    classes = tuple(sorted(set(missing_classes)))
    queries = [(name, "class_name") for name in classes]
    name_query = _name_query(classes, import_report)
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


def missing_node_pack_suggestions(
    missing_classes: Iterable[str], *, import_report: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    """Partition unresolved classes by authored locator or provisional name prefix.

    No name-based group proves common pack membership. Do not merge conflicting
    authored locators or unrelated classes merely because both lack evidence.
    Every class is looked up once; name lookup happens once per group.
    """
    groups: dict[tuple[str, str], list[str]] = {}
    for name in sorted(set(missing_classes)):
        hints = _provenance_hints((name,), import_report)
        words = _WORD.findall(name)
        if len(hints) == 1:
            key = ("authored", next(iter(hints)))
        elif not hints and len(words) >= 3:
            key = ("provisional", " ".join(words[:2]).casefold())
        else:
            key = ("unresolved", name)
        groups.setdefault(key, []).append(name)

    return [
        {"group_kind": kind, "group_key": key,
         **missing_node_suggestions(classes, import_report=import_report)}
        for (kind, key), classes in groups.items()
    ]


def combined_pack_choice(groups: Iterable[Mapping[str, Any]], *, retry_argv: Iterable[str]) -> dict[str, Any]:
    """One human choice for all missing groups; never a guessed executable fix.

    The caller supplies the actual entry-creation invocation. The ordinary
    CLI pin resolver and consent path own all subsequent authorization.
    """
    from .workflow_additions import repair_workflow_argv

    rows = list(groups)
    if not rows:
        raise ValueError("combined pack choice requires unresolved groups")
    argv, placeholders = repair_workflow_argv(list(retry_argv))
    if not any(arg == "--as-entry" or arg.startswith("--as-entry=") for arg in argv):
        raise ValueError("combined pack choice requires an entry creation command")
    for index, row in enumerate(rows, start=1):
        key = f"MISSING_PACK_{index}"
        argv.extend(["--add", f"<{key}>"])
        placeholders[key] = (
            "Verified HTTPS repository URL providing " + ", ".join(row["classes"])
            + "; if no candidate is verified, identify the source manually before retrying"
        )
    return {
        "id": "choose-missing-packs", "who": "human", "scope": "managed_write",
        "details": {"groups": rows},
        "instruction": {
            "step": "Choose and verify the repository for each missing pack or provisional "
                    "class group. Name-only matches are not verified class evidence. "
                    "If no source is verified, find it manually or stop. "
                    "Supply all verified repositories together; new code still requires consent.",
            "then_argv": argv,
        },
        "placeholders": placeholders,
    }
