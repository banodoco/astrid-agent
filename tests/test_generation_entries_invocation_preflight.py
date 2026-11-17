"""Every shipped generation entry passes its own invocation preflight, offline.

For each shipped entry (ready templates and contributed declarations) with a concrete
example or default input set, compile the entry and run the same invocation preflight
the dry run and the paid submission run. An entry that its own preflight would refuse
fails here, before a machine is ever rented.

Entries that cannot be checked are excluded and counted: a mode-less entry, one whose
required inputs have no concrete value, or one that needs a prompt the entry does not
supply. Known pre-existing refusals are listed by name and pinned to their defect, so the
suite stays green while each stays visible and a fix must update the list.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from astrid.core.generation import entries as generation_entries
from astrid.sdk import generation as sdk_generation

_PROMPT_INPUTS = frozenset({"prompt", "text"})

# Concrete example values for entries whose declaration carries only a placeholder.
_EXAMPLE_INPUTS = {
    "transcription": {"audio_ref": "input.wav"},  # the bundle's own DEFAULT_AUDIO_REF
}

# Shipped entries whose own preflight refuses them today, with the defect named. They are
# excluded from the pass test and pinned by test_known_shipped_defects_are_still_the_defect,
# so CI stays green while each one stays visible. A fix removes its entry from this list.
_KNOWN_DEFECTS = {
    "image/qwen_image_2_1_turbo": (
        "VIBECOMFY_PROMPT_EMPTY",
        "required prompt defaults to '' and the entry's own preflight refuses it",
    ),
    "edit/qwen_image_2_1_turbo_edit": (
        "VIBECOMFY_PROMPT_EMPTY",
        "required prompt defaults to '' and the entry's own preflight refuses it",
    ),
    "image/z_image_turbo": (
        "could not be read",
        "canonical member ready_templates/image/z_image_turbo.vib is missing from the VibeComfy checkout",
    ),
}


def _shipped_rows() -> dict[str, dict]:
    rows = {row["id"]: row for row in generation_entries.ready_entries()}
    for row in generation_entries.contributed_entries():
        rows.setdefault(row["id"], row)
    return rows


_MEDIA_SUFFIXES = (".wav", ".mp3", ".flac", ".png", ".jpg", ".jpeg", ".webp", ".mp4", ".mov", ".webm")


def _example_values(row: dict) -> dict | None:
    """The concrete input set this entry ships with, or None when it has none.

    Every required input needs a declared default, and a prompt input needs one too: an
    empty optional prompt is not an example. Optional aliases of the same field are left
    out so the set never binds one field twice.
    """
    if row["id"] in _EXAMPLE_INPUTS:
        return dict(_EXAMPLE_INPUTS[row["id"]])
    values: dict = {}
    alias_names = {alias for spec in row.get("inputs") or [] for alias in spec.get("aliases") or []}
    for spec in row.get("inputs") or []:
        name = spec.get("name")
        if name in alias_names:
            continue
        default = spec.get("default")
        if default is None:
            if spec.get("required") or name in _PROMPT_INPUTS:
                return None
            continue
        if spec.get("required") or name in _PROMPT_INPUTS:
            values[name] = default
    return values


def _checkable_ids() -> tuple[list[str], int]:
    rows = _shipped_rows()
    in_scope = []
    for entry_id in sorted(rows):
        row = rows[entry_id]
        if not row.get("modes") or _example_values(row) is None:
            continue
        in_scope.append(entry_id)
    return in_scope, len(rows) - len(in_scope)


_IN_SCOPE, _EXCLUDED = _checkable_ids()


_MEDIA_EXTENSION = {"image": ".png", "mask": ".png", "video": ".mp4", "audio": ".wav"}
_ALL_MEDIA_SUFFIXES = (".wav", ".mp3", ".flac", ".png", ".jpg", ".jpeg", ".webp", ".mp4", ".mov", ".webm")


def _with_media_files(values: dict, directory, row: dict) -> dict:
    """Give each media input a placeholder file so staging can read it.

    A media default names a file, and an empty media default means no file was given, so
    both get an empty placeholder of the input's declared kind. The preflight checks the
    workflow and prompt, not media content, so a placeholder is enough for this check.
    """
    semantics = {spec.get("name"): str(spec.get("media_semantics") or "") for spec in row.get("inputs") or []}
    staged = {}
    for name, value in values.items():
        kind = semantics.get(name, "")
        named_media = isinstance(value, str) and value.lower().endswith(_ALL_MEDIA_SUFFIXES)
        if (kind in _MEDIA_EXTENSION and value in ("", None)) or named_media:
            extension = _MEDIA_EXTENSION.get(kind, Path(str(value)).suffix or ".bin")
            name_on_disk = str(value) or f"{name}{extension}"
            path = directory / name_on_disk
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"placeholder")
            staged[name] = str(path)
        else:
            staged[name] = value
    return staged


def test_the_prevention_scope_is_not_vacuous():
    assert len(_IN_SCOPE) >= 15, f"only {len(_IN_SCOPE)} shipped entries have an example input set"
    assert "transcription" in _IN_SCOPE


_PASSING = [entry_id for entry_id in _IN_SCOPE if entry_id not in _KNOWN_DEFECTS]


@pytest.mark.parametrize("entry_id", _PASSING)
def test_shipped_entry_passes_its_own_invocation_preflight_offline(entry_id, tmp_path):
    row = _shipped_rows()[entry_id]
    mode = row["modes"][0]
    values = _with_media_files(_example_values(row), tmp_path, row)

    workflow, plan, assets = generation_entries.prepare(
        generation_entries.bundle_path(row), target_client=None, mode=mode,
        binds=row.get("binds"), defaults=row.get("defaults"), values=values,
    )

    # Same helper the dry run uses. H3 measures its prepared source on the live path;
    # an offline check cannot reproduce that source, so H3 skips only the AV measurement.
    sdk_generation._offline_invocation_preflight(
        workflow, plan=plan, assets=assets or {}, comfy_url=None,
        lineage=None, source_video=entry_id != "h3",
    )


@pytest.mark.parametrize("entry_id", sorted(_KNOWN_DEFECTS))
def test_known_shipped_defects_are_still_the_defect(entry_id, tmp_path):
    """Pin each known defect to its named failure, so a silent change in either direction is seen."""
    expected, _reason = _KNOWN_DEFECTS[entry_id]
    row = _shipped_rows()[entry_id]
    values = _with_media_files(_example_values(row), tmp_path, row)

    with pytest.raises(Exception, match=expected):
        workflow, plan, assets = generation_entries.prepare(
            generation_entries.bundle_path(row), target_client=None, mode=row["modes"][0],
            binds=row.get("binds"), defaults=row.get("defaults"), values=values,
        )
        sdk_generation._offline_invocation_preflight(
            workflow, plan=plan, assets=assets or {}, comfy_url=None,
            lineage=None, source_video=True,
        )
