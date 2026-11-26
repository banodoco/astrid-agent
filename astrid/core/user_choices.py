"""Reconcile portable skill opt-outs with the local compatibility mirror."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from astrid.core import user_state, user_state_import

KEY = "agent.choices"
SCHEMA = "astrid.agent-choices.v1"
HARNESSES = ("claude", "codex", "hermes")


def normalize_disabled(value) -> dict[str, list[str]]:
    """Keep only recognized harnesses and unique, non-empty skill names."""
    source = value if isinstance(value, Mapping) else {}
    result = {}
    for harness in HARNESSES:
        entries = source.get(harness)
        result[harness] = (
            sorted({item for item in entries if isinstance(item, str) and item})
            if isinstance(entries, list) else []
        )
    return result


def disabled_digest(value) -> str:
    return user_state.content_digest(normalize_disabled(value))


def _document(value) -> dict:
    normalized = normalize_disabled(value)
    return {
        "schema": SCHEMA,
        "skills": {"disabled_defaults": normalized, "mirror_digest": disabled_digest(normalized)},
    }


def validate_document(content) -> dict:
    if not isinstance(content, Mapping):
        raise ValueError("agent.choices must be a mapping")
    for key in content:
        if key not in ("schema", "skills"):
            raise ValueError(f"Unknown agent.choices key: {key}")
    if content.get("schema") != SCHEMA:
        raise ValueError("Invalid schema for agent.choices")
    skills = content.get("skills")
    if not isinstance(skills, Mapping):
        raise ValueError("skills must be a mapping")
    for key in skills:
        if key not in ("disabled_defaults", "mirror_digest"):
            raise ValueError(f"Unknown skills key: {key}")
    digest = skills.get("mirror_digest")
    if digest is not None and not isinstance(digest, str):
        raise ValueError("mirror_digest must be a string or null")
    return {
        "schema": SCHEMA,
        "skills": {
            "disabled_defaults": normalize_disabled(skills.get("disabled_defaults")),
            "mirror_digest": digest,
        },
    }


@dataclass(frozen=True)
class Reconciled:
    value: dict
    source: str
    wrote_runtime: bool


def _store_or_none(store):
    if store is not None:
        return store
    try:
        return user_state.current_store()
    except OSError:
        return None


def push_skills(value, *, store=None) -> bool:
    """Publish the file's opt-outs after the caller has saved its local mirror."""
    store = _store_or_none(store)
    if store is None:
        return False
    desired = _document(value)

    def change(doc: dict) -> None:
        if doc:
            validate_document(doc)
        doc.update(desired)

    try:
        user_state.update(KEY, change, validate=validate_document, store=store)
    except user_state.UserStateError as exc:
        if exc.code in ("unavailable", "too_large"):
            return False
        raise
    return True


def reconcile_skills(*, file_present: bool, file_value, store=None) -> Reconciled:
    """Use the mirror digest to distinguish a restored Runtime from a newer file."""
    local = normalize_disabled(file_value)
    fallback = Reconciled(local, "file", False)
    store = _store_or_none(store)
    if store is None:
        return fallback
    try:
        snapshot = user_state.read(KEY, store=store)
        if snapshot.content is None:
            if not any(local.values()):
                return fallback
            result = user_state_import.run_import(
                user_state_import.ImportSpec(
                    import_id="skills-disabled-defaults",
                    target_key=KEY,
                    source_label="skills.json",
                    read_source=lambda: local if any(local.values()) else None,
                    to_document=_document,
                ),
                store=store,
            )
            if result.status == "imported":
                return Reconciled(local, "imported", True)
            # A competing import or an existing receipt may have kept a target.
            snapshot = user_state.read(KEY, store=store)
            if snapshot.content is None:
                return fallback
            skills = validate_document(snapshot.content)["skills"]
            return Reconciled(skills["disabled_defaults"], "runtime", False)
        skills = validate_document(snapshot.content)["skills"]
    except user_state.UserStateError as exc:
        if exc.code in ("unavailable", "too_large"):
            return fallback
        raise

    if not file_present or disabled_digest(local) == skills["mirror_digest"]:
        return Reconciled(skills["disabled_defaults"], "runtime", False)
    return Reconciled(local, "file-newer", push_skills(local, store=store))
