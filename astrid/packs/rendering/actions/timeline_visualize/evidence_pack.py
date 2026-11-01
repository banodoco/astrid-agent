"""R13 — assemble and hash the self-contained evidence pack.

:func:`write_evidence_pack` materializes every R9/R10/R11/R12 artifact into one
run-owned directory, builds the v1 result manifest (when R14 does not supply a
prebuilt one), rewrites the action graph's ``--from-view`` argv to the
pack-relative manifest path, hashes every file, and returns the
:class:`PackLayout` contract R14's executor wiring consumes.

Contract notes (read before changing):

* **Determinism.** Identical inputs produce identical bytes for every JSON and
  markdown file.  ``created`` and ``ground-truth.json.timestamps.frozen_at``
  default to the fixed sentinel :data:`FROZEN_AT_SENTINEL` — never the wall
  clock.  R14 supplies the real run instant via ``frozen_at``/``manifest``,
  outside every content-identity preimage.  PNG/SVG bytes pass through
  unchanged from R11.
* **Hash placement decision.**  ``diagnostics.json`` and the manifest
  ``entrypoints``/``optional_formats``/``companions`` objects are closed
  (``additionalProperties: false``), so the pack hash map lives in a sibling
  ``pack-hashes.json``.  The manifest's ``outputs`` array (the one open
  entrypoint list) documents every artifact's ``sha256``/``content_hash``/
  ``bytes`` **except** ``manifest.json`` and ``pack-hashes.json``: the manifest
  cannot contain its own hash, and ``pack-hashes.json`` hashes ``manifest.json``
  (written after it), so a manifest entry for ``pack-hashes.json`` would create
  an unresolvable hash cycle.  ``pack-hashes.json`` therefore covers every file
  except itself — including the manifest — in declared reading order, and its
  own digest is returned in :attr:`PackLayout.file_hashes`.
* **Action-index argv rewrite.**  R9 embeds the caller's manifest path after
  ``--from-view``.  This module rewrites that exact argv element to
  ``manifest.json`` (pack-relative) so a copied pack stays self-contained.
* **Containment.** Every write resolves inside ``out_root``; no source path is
  emitted for any pack-internal reference. Asset entries carry runtime-managed
  identity and admission state only.
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from astrid.packs.rendering.actions.timeline_visualize.emit import (
    FROZEN_AT_SENTINEL,
    METRIC_DEFINITIONS_NAME,
    _sanitize_diagnostic_message,
    _transition_default_fingerprint,
)
from astrid.packs.rendering.actions.timeline_visualize.layout import (
    LayoutPage,
    serialize_view_map,
)
from astrid.core.timeline.snapshot_digest import (
    canonical_json_bytes,
)

MANIFEST_NAME = "manifest.json"
PACK_HASHES_NAME = "pack-hashes.json"
GROUND_TRUTH_NAME = "ground-truth.json"
VIEW_MAP_NAME = "view-map.json"
ACTION_INDEX_NAME = "action-index.json"
ASSET_INDEX_NAME = "asset-index.json"
TRANSCRIPT_INDEX_NAME = "transcript-index.json"
DIAGNOSTICS_NAME = "diagnostics.json"
READING_GUIDE_NAME = "reading-guide.md"
STRUCTURE_NAME = "structure.md"
FILMSTRIP_DIR = "filmstrip"

#: Relative repo path of the pinned compositor source snapshot (R2).  This is
#: provenance only — it is never resolved inside the pack.
COMPOSITOR_SOURCE_SNAPSHOT_PATH = "docs/reference/timeline-composition-v0.0.6/"
COMPOSITOR_PACKAGE = "timeline-composition"

#: Deterministic reason recorded for the retired SVG output format.
_SVG_NULL_REASON = "SVG output was removed; request PNG or Markdown instead"

#: Deterministic reason used when structure.md is not emitted.
_STRUCTURE_NULL_REASON = (
    "structure.md was not emitted for this pack; the reading guide and "
    "action-index.json carry the navigation contract instead"
)

_PACK_INTEGRITY_SECTION = (
    "\n## Pack integrity\n\n"
    "`pack-hashes.json` lists the sha256 of every file in this pack (except "
    "itself) in declared reading order, so a copied pack can be verified "
    "without its parent run or live project state.\n"
)

#: Crockford base32 alphabet used by ULIDs (no I, L, O, U).
_ULID_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


@dataclass(frozen=True)
class PackLayout:
    """Materialized pack contract consumed by R14's executor wiring."""

    root: Path  # pack root dir (e.g. agent-view/<run_id> or out dir)
    manifest_path: Path
    pages: tuple[Path, ...]  # page PNG files in reading order
    svg_paths: tuple[Path, ...]
    structure_path: Path | None
    reading_guide_path: Path
    json_paths: dict[
        str, Path
    ]  # ground-truth, view-map, action-index, asset-index, transcript-index, diagnostics, manifest, pack-hashes
    file_hashes: dict[str, str]  # relative path -> sha256 (canonical bytes)
    total_bytes: int


# ---------------------------------------------------------------------------
# Small deterministic helpers.
# ---------------------------------------------------------------------------


def _canonical_json_bytes(obj: Any) -> bytes:
    """Byte-stable JSON exactly like the SNS canonicalization."""
    return json.dumps(
        obj,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")


def _ordered_json_bytes(obj: Any) -> bytes:
    """Byte-stable JSON preserving insertion order (pack-hashes reading order)."""
    return json.dumps(
        obj,
        sort_keys=False,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _deterministic_run_id(
    snapshot_sns: str, timeline_uuid: str, timeline_ulid: str, project_slug: str
) -> str:
    """Deterministic ULID-format run id derived from frozen identity.

    R14 replaces this with the real run ULID through the prebuilt ``manifest``
    parameter; the default keeps identical inputs -> identical bytes.
    """
    payload = canonical_json_bytes(
        {
            "snapshot_sns": snapshot_sns,
            "timeline_uuid": timeline_uuid,
            "timeline_ulid": timeline_ulid,
            "project_slug": project_slug,
        }
    )
    digest = hashlib.sha256(payload).digest()
    bits = int.from_bytes(digest, "big") >> (256 - 26 * 5)
    chars: list[str] = []
    for _ in range(26):
        chars.append(_ULID_ALPHABET[bits & 31])
        bits >>= 5
    return "".join(reversed(chars))


def _rewrite_action_index_argv(
    action_index: dict[str, Any], pack_manifest_rel: str = MANIFEST_NAME
) -> dict[str, Any]:
    """Rewrite every ``--from-view`` argv element to the pack-relative path.

    Mutates (and returns) the caller's deep copy; the original dict is never
    touched.  The element immediately after ``--from-view`` is the manifest
    path R9 baked in (often absolute); it becomes ``manifest.json``.
    """
    entries = action_index.get("entries", {})
    if not isinstance(entries, Mapping):
        raise TypeError("action_index['entries'] must be an object")
    for entry in entries.values():
        if not isinstance(entry, Mapping):
            continue
        actions = entry.get("actions", {})
        if not isinstance(actions, Mapping):
            continue
        for action_name, action in actions.items():
            if not isinstance(action, Mapping):
                continue
            # A child view's parent action deliberately names the exact prior
            # manifest.  Rewriting it to this pack would turn "up" into a
            # self-loop and destroy the lineage proof.
            if action_name == "parent_view":
                continue
            argv = action.get("argv")
            if not isinstance(argv, list):
                continue
            try:
                marker = argv.index("--from-view")
            except ValueError:
                continue
            if marker + 1 < len(argv):
                argv[marker + 1] = pack_manifest_rel
    return action_index


# ---------------------------------------------------------------------------
# Manifest construction (R13 default; R14 may pass a prebuilt manifest).
# ---------------------------------------------------------------------------


def _build_manifest(
    *,
    gt: dict[str, Any],
    ai: dict[str, Any],
    diagnostics: dict[str, Any],
    model: Any,
    pages: tuple[LayoutPage, ...],
    page_ids: tuple[str, ...],
    png_page_ids: tuple[str, ...],
    svg_page_ids: tuple[str, ...],
    structure_present: bool,
    frozen_at: str | None,
    file_records: list[dict[str, Any]],
    from_view: str | None,
    focus: str | None,
    resolved_project: Mapping[str, Any] | None,
    source_mode: str,
) -> dict[str, Any]:
    snapshots = gt["snapshots"]
    if not snapshots:
        raise ValueError("ground-truth.json must carry at least one snapshot block")
    snapshot_block = snapshots[0]
    timeline = snapshot_block["timeline"]
    project_slug = gt.get("project_slug", "")
    if not isinstance(project_slug, str) or not project_slug:
        raise ValueError("ground-truth.json project_slug must be a non-empty string")

    scope_block = gt.get("scope", {})
    scope_kind = (
        scope_block.get("kind", "timeline") if isinstance(scope_block, Mapping) else "timeline"
    )
    scope_ref = scope_block.get("ref") if isinstance(scope_block, Mapping) else None

    layouts: list[str] = []
    for page in pages:
        if page.layout not in layouts:
            layouts.append(page.layout)
    if not layouts:
        layouts = ["time-scaled"]
    input_layout = "both" if len(layouts) > 1 else layouts[0]

    breadcrumb: list[str] = [scope_ref] if isinstance(scope_ref, str) and scope_ref else []
    suggested: list[dict[str, str]] = []
    ai_entries = ai.get("entries", {})
    if isinstance(ai_entries, Mapping):
        for ref, entry in ai_entries.items():
            if not isinstance(entry, Mapping):
                continue
            actions = entry.get("actions", {})
            if not isinstance(actions, Mapping):
                continue
            for action_name in actions:
                suggested.append({"object_ref": ref, "action": action_name})

    warnings = [
        f"{item.get('code', 'DIAGNOSTIC')}: {_sanitize_diagnostic_message(item.get('message', ''))}"
        for item in diagnostics.get("diagnostics", [])
        if isinstance(item, Mapping)
    ]

    created = frozen_at if frozen_at else FROZEN_AT_SENTINEL
    run_id = _deterministic_run_id(
        snapshot_sns=snapshot_block["digest"],
        timeline_uuid=timeline["uuid"],
        timeline_ulid=timeline["ulid"],
        project_slug=project_slug,
    )

    primary_png = f"{png_page_ids[0]}.png" if png_page_ids else None
    structure_path = STRUCTURE_NAME if structure_present else None
    input_formats: list[str] = []
    if png_page_ids:
        input_formats.append("png")
    if structure_present:
        input_formats.append("md")

    return {
        "schema_version": 1,
        "kind": "timeline_visualize",
        "inputs": {
            "source_mode": source_mode,
            "resolved_project": dict(resolved_project or {"slug": project_slug}),
            "resolved_timelines": [deepcopy(timeline)],
            # Timeline-visualize v1 schemas freeze ULIDs in uppercase. The
            # v10 kernel and its public CLI use lowercase ULIDs. Keep the
            # frozen identity untouched and publish an additive comparison-
            # safe projection for agents crossing those two surfaces.
            "canonical_timeline_identities": [
                {
                    **deepcopy(timeline),
                    "ulid": str(timeline["ulid"]).lower(),
                }
            ],
            "from_view": from_view,
            "focus": focus,
            "scope": scope_kind,
            "layout": input_layout,
            "formats": input_formats,
        },
        "outputs": file_records,
        "created": created,
        "warnings": warnings,
        "run_id": run_id,
        "run_root": ".",
        "snapshots": snapshots,
        "compositor": {
            "package": COMPOSITOR_PACKAGE,
            "version": str(getattr(model, "compositor_version", "")),
            "source_snapshot_path": COMPOSITOR_SOURCE_SNAPSHOT_PATH,
            "registry_default_fingerprint": _transition_default_fingerprint(),
        },
        "scope": scope_block,
        "layouts": layouts,
        "page_count": len(page_ids),
        "reading_order": list(page_ids),
        "entrypoints": {
            "manifest": MANIFEST_NAME,
            "ground_truth": GROUND_TRUTH_NAME,
            "view_map": VIEW_MAP_NAME,
            "action_index": ACTION_INDEX_NAME,
            "asset_index": ASSET_INDEX_NAME,
            "transcript_index": TRANSCRIPT_INDEX_NAME,
            "diagnostics": DIAGNOSTICS_NAME,
            "reading_guide": READING_GUIDE_NAME,
            "structure": structure_path,
            "primary_image": primary_png,
        },
        "optional_formats": {
            "png": {
                "path": primary_png,
                "reason": None if primary_png else "no pages were laid out for this scope",
            },
            # The closed manifest schema still requires the key; SVG output
            # is not produced, so it is always null with a stable reason.
            "svg": {"path": None, "reason": _SVG_NULL_REASON},
            "structure": {
                "path": structure_path,
                "reason": None if structure_present else _STRUCTURE_NULL_REASON,
            },
        },
        "companions": {
            "reading_guide": {
                "path": READING_GUIDE_NAME,
                "content_kind": "prose",
                "schema": None,
            },
            "structure": {
                "path": structure_path,
                "reason": None if structure_present else _STRUCTURE_NULL_REASON,
                "content_kind": "factual_markdown",
                "breadcrumb": breadcrumb,
                "suggested_next_actions": suggested,
            },
        },
    }


def _declared_reading_order(
    png_page_ids: tuple[str, ...],
    svg_page_ids: tuple[str, ...],
    filmstrip_rel: tuple[str, ...],
    structure_present: bool,
) -> list[str]:
    """Entrypoint order, then page order, then svgs, then filmstrip frames.

    ``manifest.json`` leads (the root); ``pack-hashes.json`` is an appendix
    that cannot list itself and is therefore not part of this order.
    """
    order = [
        MANIFEST_NAME,
        GROUND_TRUTH_NAME,
        VIEW_MAP_NAME,
        ACTION_INDEX_NAME,
        ASSET_INDEX_NAME,
        TRANSCRIPT_INDEX_NAME,
        DIAGNOSTICS_NAME,
        METRIC_DEFINITIONS_NAME,
        READING_GUIDE_NAME,
    ]
    if structure_present:
        order.append(STRUCTURE_NAME)
    order.extend(f"{page_id}.png" for page_id in png_page_ids)
    order.extend(f"{page_id}.svg" for page_id in svg_page_ids)
    order.extend(filmstrip_rel)
    return order


# ---------------------------------------------------------------------------
# Pack materialization.
# ---------------------------------------------------------------------------


def _write_json(path: Path, obj: Any, *, ordered: bool = False) -> None:
    payload = _ordered_json_bytes(obj) if ordered else _canonical_json_bytes(obj)
    path.write_bytes(payload)


def _copy_filmstrip_frames(
    out_root: Path,
    filmstrips: dict[str, list[Path]],
) -> tuple[str, ...]:
    """Copy external sampled frames into ``filmstrip/`` with deterministic names.

    Frames that already live inside ``out_root`` (R12 may have sampled straight
    into the pack) are left in place and recorded as-is.  External frames are
    copied as ``filmstrip/{display_id}_film_{NN:02d}.png`` so names are
    deterministic and collision-free.  Audio filmstrips (empty lists) contribute
    nothing.
    """
    if not filmstrips:
        return ()
    filmstrip_dir = out_root / FILMSTRIP_DIR
    filmstrip_dir.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    for display_id in sorted(filmstrips):
        frames = filmstrips[display_id]
        if not frames:
            continue
        # Composite keys are "{page_id}::{asset_ref}" (per-page strips);
        # derive a filesystem-safe stem: page_ref form (dots -> underscores).
        stem = display_id.replace("::", "_").replace(".", "_")
        for index, frame in enumerate(frames):
            source = Path(frame)
            if not source.is_file():
                raise FileNotFoundError(f"filmstrip frame not found: {source}")
            try:
                inside = source.resolve().is_relative_to(out_root.resolve())
            except OSError:
                inside = False
            if inside:
                rel = source.resolve().relative_to(out_root.resolve()).as_posix()
                if rel not in written:
                    written.append(rel)
                continue
            target = filmstrip_dir / f"{stem}_film_{index:02d}.png"
            target.write_bytes(source.read_bytes())
            written.append(target.relative_to(out_root).as_posix())
    return tuple(written)


def write_evidence_pack(
    *,
    out_root: Path,  # where the pack lands (run-owned dir)
    page_id_prefix: str,  # e.g. "PG"
    model,
    identity_map,
    snapshot,
    scope,
    ground_truth: dict,
    action_index: dict,
    asset_index: dict,
    transcript_index: dict,
    diagnostics: dict,
    reading_guide: str,
    structure_md: str | None,
    pages: tuple[LayoutPage, ...],  # from R10
    svg_bytes: dict[str, bytes],  # page_id -> svg bytes (R11)
    png_bytes: dict[str, bytes],  # page_id -> png bytes (R11)
    filmstrips: dict[str, list[Path]] | None = None,  # asset display id -> sampled frames (R12)
    metric_definitions: dict | None = None,  # versioned metric-definitions.json content (R13)
    manifest: dict | None = None,  # prebuilt manifest if R14 supplies one
    frozen_at: str | None = None,  # deterministic timestamp; never wall-clock
    from_view: str | None = None,  # project-relative exact parent manifest
    focus: str | None = None,  # qualified parent-resolved focus
    resolved_project: Mapping[str, Any] | None = None,
    source_mode: str = "kernel",
) -> PackLayout:
    """Assemble the complete self-contained evidence pack on disk."""

    # The manifest schema's entrypoints object is closed
    # (additionalProperties: false), so metric-definitions.json cannot be
    # referenced there; it ships as a sibling, is hashed in pack-hashes.json
    # (files + coverage) and in the manifest outputs array (it is part of the
    # declared reading order), and the reading guide points at it.
    if metric_definitions is None:
        raise ValueError("metric_definitions content is required (R13)")
    if not isinstance(out_root, Path):
        out_root = Path(out_root)
    if not isinstance(page_id_prefix, str) or not page_id_prefix:
        raise ValueError("page_id_prefix must be a non-empty string")
    if not isinstance(pages, tuple) or not all(isinstance(page, LayoutPage) for page in pages):
        raise TypeError("pages must be a tuple of LayoutPage values")
    for page in pages:
        if not page.page_id.startswith(page_id_prefix):
            raise ValueError(
                f"page_id {page.page_id!r} does not start with prefix {page_id_prefix!r}"
            )

    out_root.mkdir(parents=True, exist_ok=True)
    filmstrips = dict(filmstrips) if filmstrips else {}

    page_ids = tuple(page.page_id for page in pages)
    unknown_png_ids = sorted(set(png_bytes) - set(page_ids))
    if unknown_png_ids:
        raise ValueError(f"png_bytes contains unknown page ids: {unknown_png_ids!r}")
    if svg_bytes:
        raise ValueError("SVG output was removed; request PNG or Markdown instead")
    # Keep the internal argument for historical callers while ensuring no new
    # pack can publish or hash an SVG page.
    svg_bytes = {}
    unknown_svg_ids: list[str] = []
    png_page_ids = tuple(page_id for page_id in page_ids if page_id in png_bytes)
    svg_page_ids = tuple(page_id for page_id in page_ids if page_id in svg_bytes)

    # 1. Deep-copied, pack-normalized artifact content.  Caller dicts never
    #    mutate; the action graph's --from-view argv becomes pack-relative.
    gt = deepcopy(dict(ground_truth))
    if frozen_at is not None:
        if not isinstance(frozen_at, str) or not frozen_at:
            raise ValueError("frozen_at must be a non-empty timestamp string")
        timestamps = gt.get("timestamps")
        if isinstance(timestamps, Mapping):
            gt["timestamps"] = {**timestamps, "frozen_at": frozen_at}
    ai = _rewrite_action_index_argv(deepcopy(dict(action_index)))
    asx = deepcopy(dict(asset_index))
    ti = deepcopy(dict(transcript_index))
    diag = deepcopy(dict(diagnostics))

    if pages and isinstance(pages[0].scope_ref, str) and pages[0].scope_ref:
        scope_ref = pages[0].scope_ref
    else:
        gt_scope = gt.get("scope")
        scope_ref = gt_scope.get("ref") if isinstance(gt_scope, Mapping) else None
        if not isinstance(scope_ref, str) or not scope_ref:
            scope_ref = "TL01"
    vm = serialize_view_map(
        pages,
        identity_map=identity_map,
        scope_ref=scope_ref,
        snapshot=snapshot,
    )
    if list(vm.get("reading_order", ())) != list(page_ids):
        raise ValueError("view-map reading_order must equal the page order")

    guide_text = reading_guide if isinstance(reading_guide, str) else str(reading_guide)
    if not guide_text.endswith("\n"):
        guide_text += "\n"
    guide_text += _PACK_INTEGRITY_SECTION

    # 2. Write every artifact except manifest.json and pack-hashes.json.
    json_targets = {
        GROUND_TRUTH_NAME: gt,
        VIEW_MAP_NAME: vm,
        ACTION_INDEX_NAME: ai,
        ASSET_INDEX_NAME: asx,
        TRANSCRIPT_INDEX_NAME: ti,
        DIAGNOSTICS_NAME: diag,
        METRIC_DEFINITIONS_NAME: deepcopy(dict(metric_definitions)),
    }
    for name, content in json_targets.items():
        _write_json(out_root / name, content)

    (out_root / READING_GUIDE_NAME).write_text(guide_text, encoding="utf-8")
    if structure_md is not None:
        (out_root / STRUCTURE_NAME).write_text(structure_md, encoding="utf-8")

    for page_id in png_page_ids:
        (out_root / f"{page_id}.png").write_bytes(png_bytes[page_id])
    for page_id in svg_page_ids:
        (out_root / f"{page_id}.svg").write_bytes(svg_bytes[page_id])

    filmstrip_rel = _copy_filmstrip_frames(out_root, filmstrips)

    # 3. Manifest: prebuilt (R14) or built here with truthful file hashes.
    #    manifest.json and pack-hashes.json are the two roots that cannot carry
    #    their own hashes; every other file is recorded in outputs.
    reading_order = _declared_reading_order(
        png_page_ids, svg_page_ids, filmstrip_rel, structure_md is not None
    )
    file_records: list[dict[str, Any]] = []
    for rel in reading_order:
        if rel in (MANIFEST_NAME, PACK_HASHES_NAME):
            continue
        path = out_root / rel
        if not path.is_file():
            raise FileNotFoundError(f"pack artifact missing before hashing: {rel}")
        digest = _sha256_file(path)
        file_records.append(
            {
                "name": rel,
                "path": rel,
                "type": "file",
                "content_hash": f"sha256:{digest}",
                "bytes": path.stat().st_size,
                "sha256": digest,
            }
        )

    if manifest is None:
        manifest = _build_manifest(
            gt=gt,
            ai=ai,
            diagnostics=diag,
            model=model,
            pages=pages,
            page_ids=page_ids,
            png_page_ids=png_page_ids,
            svg_page_ids=svg_page_ids,
            structure_present=structure_md is not None,
            frozen_at=frozen_at,
            file_records=file_records,
            from_view=from_view,
            focus=focus,
            resolved_project=resolved_project,
            source_mode=source_mode,
        )
    else:
        manifest = deepcopy(dict(manifest))
    _write_json(out_root / MANIFEST_NAME, manifest)

    # 4. pack-hashes.json: every file except itself, in declared reading order,
    #    written last so it can carry the manifest's final digest.
    hashes: dict[str, dict[str, Any]] = {}
    for rel in reading_order:
        if rel == PACK_HASHES_NAME:
            continue
        path = out_root / rel
        if not path.is_file():
            raise FileNotFoundError(f"pack artifact missing before hashing: {rel}")
        digest = _sha256_file(path)
        hashes[rel] = {"sha256": digest, "bytes": path.stat().st_size}
    pack_hashes = {
        "schema_version": 1,
        "kind": "timeline_visualize_pack_hashes",
        "coverage": {
            "manifest": MANIFEST_NAME,
            "ground_truth": GROUND_TRUTH_NAME,
            "view_map": VIEW_MAP_NAME,
            "action_index": ACTION_INDEX_NAME,
            "asset_index": ASSET_INDEX_NAME,
            "transcript_index": TRANSCRIPT_INDEX_NAME,
            "diagnostics": DIAGNOSTICS_NAME,
            "metric_definitions": METRIC_DEFINITIONS_NAME,
            "reading_guide": READING_GUIDE_NAME,
        },
        "files": hashes,
    }
    _write_json(out_root / PACK_HASHES_NAME, pack_hashes, ordered=True)

    # 5. Final hash map over every file actually on disk (no orphans), plus
    #    the aggregate byte total.
    file_hashes: dict[str, str] = {}
    total_bytes = 0
    for path in sorted(out_root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(out_root).as_posix()
        digest = _sha256_file(path)
        file_hashes[rel] = digest
        total_bytes += path.stat().st_size

    json_paths = {
        "manifest": out_root / MANIFEST_NAME,
        "ground-truth": out_root / GROUND_TRUTH_NAME,
        "view-map": out_root / VIEW_MAP_NAME,
        "action-index": out_root / ACTION_INDEX_NAME,
        "asset-index": out_root / ASSET_INDEX_NAME,
        "transcript-index": out_root / TRANSCRIPT_INDEX_NAME,
        "diagnostics": out_root / DIAGNOSTICS_NAME,
        "metric-definitions": out_root / METRIC_DEFINITIONS_NAME,
        "pack-hashes": out_root / PACK_HASHES_NAME,
    }
    return PackLayout(
        root=out_root,
        manifest_path=out_root / MANIFEST_NAME,
        pages=tuple(out_root / f"{page_id}.png" for page_id in png_page_ids),
        svg_paths=tuple(out_root / f"{page_id}.svg" for page_id in svg_page_ids),
        structure_path=(out_root / STRUCTURE_NAME) if structure_md is not None else None,
        reading_guide_path=out_root / READING_GUIDE_NAME,
        json_paths=json_paths,
        file_hashes=file_hashes,
        total_bytes=total_bytes,
    )


__all__ = [
    "ACTION_INDEX_NAME",
    "ASSET_INDEX_NAME",
    "DIAGNOSTICS_NAME",
    "FILMSTRIP_DIR",
    "GROUND_TRUTH_NAME",
    "MANIFEST_NAME",
    "PACK_HASHES_NAME",
    "PackLayout",
    "READING_GUIDE_NAME",
    "STRUCTURE_NAME",
    "TRANSCRIPT_INDEX_NAME",
    "VIEW_MAP_NAME",
    "write_evidence_pack",
]
