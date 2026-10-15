"""Consume the host-compiled immutable timeline revision-diff contract.

The SDK/native owner prepares the two detached authoring closures.  This
adapter is deliberately a consumer only: it never opens Runtime, follows a
current head, reads a project directory, or accepts a second authority
object.  The existing motion ``diffview`` implementation remains the
semantic diff; this module only admits its exact inputs and publishes the
durable visualization result.
"""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Mapping

from astrid.core._shared.result_manifest import build_manifest, write_manifest

from .motion.diffview import compare, compose_page

_SCHEMA_VERSION = 1
_KIND = "astrid.timeline_revision_diff.v1"
_SHA256 = re.compile(r"^(?:sha256:)?[0-9a-f]{64}$")


class RevisionDiffAdmissionError(ValueError):
    """A compiled host contract is absent, malformed, or fails integrity checks."""


def _canonical_digest(value: Any, *, field: str) -> str:
    try:
        encoded = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise RevisionDiffAdmissionError(f"revision diff {field} is not canonical JSON") from exc
    return hashlib.sha256(encoded).hexdigest()


def _checked_digest(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise RevisionDiffAdmissionError(
            f"revision diff {field} must be a lowercase canonical SHA-256 digest"
        )
    return value.removeprefix("sha256:")


def _required_id(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RevisionDiffAdmissionError(f"revision diff requires explicit {field}")
    return value


def _read_compiled(value: Any) -> Mapping[str, Any]:
    """Read the host-injected JSON value without treating a path as authority."""

    if isinstance(value, Mapping):
        return value
    if not isinstance(value, str) or not value.strip():
        raise RevisionDiffAdmissionError("view=diff requires the host-compiled revision_diff input")
    raw = value
    candidate = Path(value).expanduser()
    if len(value) < 4096 and candidate.is_file():
        try:
            raw = candidate.read_text(encoding="utf-8")
        except OSError as exc:
            raise RevisionDiffAdmissionError("compiled revision_diff input is unreadable") from exc
    try:
        parsed = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise RevisionDiffAdmissionError("compiled revision_diff input is invalid JSON") from exc
    if not isinstance(parsed, Mapping):
        raise RevisionDiffAdmissionError("compiled revision_diff input must be an object")
    return parsed


def _validate_side(
    side: Any,
    *,
    label: str,
    project_id: str,
    timeline_id: str,
    revision_id: str,
) -> Mapping[str, Any]:
    if not isinstance(side, Mapping):
        raise RevisionDiffAdmissionError(f"revision diff {label} side must be an object")
    if side.get("revision_id") != revision_id:
        raise RevisionDiffAdmissionError(
            f"revision diff {label} revision selector does not match the explicit action selector"
        )
    parent = side.get("parent_content_digest")
    parent_digest = _checked_digest(parent, field=f"{label}.parent_content_digest")
    bundle = side.get("bundle")
    config = side.get("config")
    registry = side.get("registry")
    expansion = side.get("expansion")
    if not isinstance(bundle, Mapping):
        raise RevisionDiffAdmissionError(f"revision diff {label}.bundle must be an object")
    if not isinstance(config, Mapping):
        raise RevisionDiffAdmissionError(f"revision diff {label}.config must be an object")
    if not isinstance(registry, Mapping):
        raise RevisionDiffAdmissionError(f"revision diff {label}.registry must be an object")
    if not isinstance(expansion, Mapping):
        raise RevisionDiffAdmissionError(f"revision diff {label}.expansion must be an object")
    if bundle.get("project_id") != project_id or bundle.get("timeline_id") != timeline_id:
        raise RevisionDiffAdmissionError(
            f"revision diff {label}.bundle is bound to a different project/timeline"
        )
    base_parent = bundle.get("base_parent")
    if not isinstance(base_parent, Mapping) or base_parent.get("revision_id") != revision_id:
        raise RevisionDiffAdmissionError(
            f"revision diff {label}.bundle is not pinned to its explicit parent revision"
        )
    if _checked_digest(base_parent.get("content_digest"), field=f"{label}.bundle.base_parent.content_digest") != parent_digest:
        raise RevisionDiffAdmissionError(
            f"revision diff {label}.parent_content_digest does not match its admitted bundle parent"
        )
    if _checked_digest(side.get("bundle_hash"), field=f"{label}.bundle_hash") != _canonical_digest(bundle, field=f"{label}.bundle"):
        raise RevisionDiffAdmissionError(f"revision diff {label}.bundle_hash does not match its immutable bundle")
    if _checked_digest(side.get("config_hash"), field=f"{label}.config_hash") != _canonical_digest(config, field=f"{label}.config"):
        raise RevisionDiffAdmissionError(f"revision diff {label}.config_hash does not match its immutable config")
    if _checked_digest(side.get("registry_hash"), field=f"{label}.registry_hash") != _canonical_digest(registry, field=f"{label}.registry"):
        raise RevisionDiffAdmissionError(f"revision diff {label}.registry_hash does not match its immutable registry")
    return side


def validate_compiled_revision_diff(
    value: Any,
    *,
    before_revision_id: Any,
    after_revision_id: Any,
    timeline_selector: Any,
) -> Mapping[str, Any]:
    """Validate the exact native shape and return only admitted immutable sides."""

    before_id = _required_id(before_revision_id, field="before_revision_id")
    after_id = _required_id(after_revision_id, field="after_revision_id")
    _required_id(timeline_selector, field="timeline_slug or timeline_ref")
    compiled = _read_compiled(value)
    if compiled.get("schema_version") != _SCHEMA_VERSION or compiled.get("kind") != _KIND:
        raise RevisionDiffAdmissionError(
            f"revision diff compiled input must have schema_version={_SCHEMA_VERSION} and kind={_KIND!r}"
        )
    project_id = _required_id(compiled.get("project_id"), field="compiled project_id")
    timeline_id = _required_id(compiled.get("timeline_id"), field="compiled timeline_id")
    before = _validate_side(
        compiled.get("before"), label="before", project_id=project_id,
        timeline_id=timeline_id, revision_id=before_id,
    )
    after = _validate_side(
        compiled.get("after"), label="after", project_id=project_id,
        timeline_id=timeline_id, revision_id=after_id,
    )
    return {
        "schema_version": _SCHEMA_VERSION,
        "kind": _KIND,
        "project_id": project_id,
        "timeline_id": timeline_id,
        "before": before,
        "after": after,
    }


def _zip_atomic(destination: Path, source_root: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent, delete=False) as stream:
        temporary = Path(stream.name)
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(source_root.rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(source_root).as_posix())
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def _receipt_entry(path: Path, root: Path, *, role: str, primary: bool = False) -> dict[str, Any]:
    return {
        "path": path.relative_to(root).as_posix(),
        "type": "file",
        "role": role,
        "is_primary": primary,
        "content_hash": "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest(),
        "bytes": path.stat().st_size,
        "label": path.name,
    }


def _markdown(report: Mapping[str, Any], *, project_id: str, timeline_id: str, before: str, after: str) -> str:
    lines = [
        "# Timeline revision diff",
        "",
        f"- project: `{project_id}`",
        f"- timeline: `{timeline_id}`",
        f"- before: `{before}`",
        f"- after: `{after}`",
        f"- cuts: {report['before_cuts']} → {report['after_cuts']}",
        "",
        "## Changed cuts",
        "",
    ]
    rows = [row for row in report["rows"] if row["status"] != "ripple"]
    if not rows:
        lines.append("No authored cut changes.")
    else:
        for row in rows:
            cut = row.get("after") or row.get("before") or {}
            lines.append(
                f"- **{row['status']}** cut `{cut.get('index', '?')}`: "
                + ("; ".join(row.get("notes") or []) or "placement changed")
            )
    from astrid.core.timeline.change_intervals import interval_lines
    lines.extend(('', *interval_lines(report['intervals'])))
    lines.extend(("", "## Findings", ""))
    findings = report.get("findings") or []
    if not findings:
        lines.append("No findings.")
    else:
        lines.extend(f"- {finding.line()}" for finding in findings)
    lines.extend(("", "Pixels are not captured by this action; the page is composed from the two admitted immutable bundles only."))
    return "\n".join(lines) + "\n"


def _edited_cut_numbers(value: Any) -> tuple[int, ...]:
    if value in (None, ""):
        return ()
    if not isinstance(value, str):
        raise RevisionDiffAdmissionError("revision diff --edited must be comma-separated cut numbers")
    numbers: list[int] = []
    for token in value.split(","):
        token = token.strip()
        if not token:
            continue
        try:
            number = int(token)
        except ValueError as exc:
            raise RevisionDiffAdmissionError("revision diff --edited must be comma-separated cut numbers") from exc
        if number < 1:
            raise RevisionDiffAdmissionError("revision diff --edited cut numbers must be positive")
        numbers.append(number)
    return tuple(numbers)


def execute_revision_diff(args: Any) -> dict[str, Any]:
    """Produce the structural donor diff page from the admitted two-side input."""

    compiled = validate_compiled_revision_diff(
        getattr(args, "revision_diff", None),
        before_revision_id=getattr(args, "before_revision_id", None),
        after_revision_id=getattr(args, "after_revision_id", None),
        timeline_selector=getattr(args, "timeline_ref", None) or getattr(args, "timeline_slug", None),
    )
    edited = _edited_cut_numbers(getattr(args, "edited", None))
    report = compare(compiled["before"]["bundle"], compiled["after"]["bundle"], edited=edited,
                     interval_inputs=(compiled['before'], compiled['after']))
    report_json = {
        "schema_version": 1,
        "kind": _KIND,
        "project_id": compiled["project_id"],
        "timeline_id": compiled["timeline_id"],
        "before_revision_id": compiled["before"]["revision_id"],
        "after_revision_id": compiled["after"]["revision_id"],
        "edited": list(edited),
        "bundle_hashes": {
            "before": compiled["before"]["bundle_hash"],
            "after": compiled["after"]["bundle_hash"],
        },
        "report": {
            **{key: value for key, value in report.items() if key != "findings"},
            "findings": [finding.as_dict() for finding in report["findings"]],
        },
        "navigation": {
            "view": "diff",
            "before_revision_id": compiled["before"]["revision_id"],
            "after_revision_id": compiled["after"]["revision_id"],
            "timeline_id": compiled["timeline_id"],
        },
    }
    out_root = Path(args.out).expanduser().resolve()
    pack_root = out_root / "revision-diff-view"
    if pack_root.exists() and any(pack_root.iterdir()):
        raise RevisionDiffAdmissionError(f"evidence pack output is not empty: {pack_root}")
    pack_root.mkdir(parents=True, exist_ok=True)
    png_path = pack_root / "revision-diff.png"
    # The donor helper's before/after layout remains the action surface.  The
    # host contract carries authoring bundles, not rendered frames, so an
    # empty frame map is intentional and never substituted with current media.
    compose_page(
        report, {}, {}, png_path,
        title=f"{compiled['timeline_id']} · {compiled['before']['revision_id']} → {compiled['after']['revision_id']}",
    )
    markdown_path = pack_root / "revision-diff.md"
    markdown_path.write_text(
        _markdown(
            report,
            project_id=compiled["project_id"],
            timeline_id=compiled["timeline_id"],
            before=compiled["before"]["revision_id"],
            after=compiled["after"]["revision_id"],
        ),
        encoding="utf-8",
    )
    json_path = pack_root / "revision-diff.json"
    json_path.write_text(json.dumps(report_json, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    entrypoints = {"png": "revision-diff.png", "markdown": "revision-diff.md", "report": "revision-diff.json"}
    nested_manifest = build_manifest(
        kind="timeline_revision_diff",
        created="1970-01-01T00:00:00Z",
        inputs={
            "project_id": compiled["project_id"],
            "timeline_id": compiled["timeline_id"],
            "before_revision_id": compiled["before"]["revision_id"],
            "after_revision_id": compiled["after"]["revision_id"],
            "bundle_hashes": report_json["bundle_hashes"],
            "render_requested": False,
            "authority": "admitted_revision_bundles",
        },
        outputs=[
            {"path": path.name, "type": "file", "role": "result", "is_primary": path == png_path}
            for path in (png_path, markdown_path, json_path)
        ],
        entrypoints=entrypoints,
        timeline_ids=[compiled["timeline_id"]],
        request={"view": "diff", "before_revision_id": compiled["before"]["revision_id"], "after_revision_id": compiled["after"]["revision_id"], "edited": list(edited)},
    )
    nested_manifest_path = pack_root / "manifest.json"
    write_manifest(nested_manifest_path, nested_manifest)
    bundle_path = out_root / "revision-diff-bundle.zip"
    _zip_atomic(bundle_path, pack_root)
    outer_manifest_path = out_root / "manifest.json"
    write_manifest(
        outer_manifest_path,
        build_manifest(
            kind="timeline_revision_diff_result",
            created="1970-01-01T00:00:00Z",
            inputs={
                "project_id": compiled["project_id"],
                "timeline_id": compiled["timeline_id"],
                "before_revision_id": compiled["before"]["revision_id"],
                "after_revision_id": compiled["after"]["revision_id"],
                "edited": list(edited),
                "render_requested": False,
            },
            outputs=[
                _receipt_entry(nested_manifest_path, out_root, role="manifest"),
                _receipt_entry(bundle_path, out_root, role="result", primary=True),
            ],
            entrypoints={
                "manifest": "revision-diff-view/manifest.json",
                "png": "revision-diff-view/revision-diff.png",
                "markdown": "revision-diff-view/revision-diff.md",
                "report": "revision-diff-view/revision-diff.json",
                "bundle": "revision-diff-bundle.zip",
            },
            timeline_ids=[compiled["timeline_id"]],
            request={"view": "diff", "before_revision_id": compiled["before"]["revision_id"], "after_revision_id": compiled["after"]["revision_id"], "edited": list(edited)},
        ),
    )
    timing = {"view": "diff", "render_requested": False, "evidence_source": "admitted_revision_bundles", "capture_s": 0.0}
    manifest_digest = "sha256:" + hashlib.sha256(nested_manifest_path.read_bytes()).hexdigest()
    bundle_digest = "sha256:" + hashlib.sha256(bundle_path.read_bytes()).hexdigest()
    host_manifest_digest = "sha256:" + hashlib.sha256(outer_manifest_path.read_bytes()).hexdigest()
    identity = {
        "revision_diff": {"project_id": compiled["project_id"], "timeline_id": compiled["timeline_id"], "before_revision_id": compiled["before"]["revision_id"], "after_revision_id": compiled["after"]["revision_id"]},
        "manifest": {"kind": "timeline_revision_diff", "content_hash": manifest_digest},
        "host_receipt": {"kind": "timeline_revision_diff_result", "content_hash": host_manifest_digest},
        "bundle": {"content_hash": bundle_digest},
    }
    return {
        "returncode": 0,
        "run_root": str(out_root),
        "manifest_path": str(nested_manifest_path),
        "timeline_ids": [compiled["timeline_id"]],
        "identity": identity,
        "cas": {"revision_diff_manifest": manifest_digest, "revision_diff_bundle": bundle_digest},
        "entrypoints": {
            "manifest": "revision-diff-view/manifest.json",
            "png": "revision-diff-view/revision-diff.png",
            "markdown": "revision-diff-view/revision-diff.md",
            "report": "revision-diff-view/revision-diff.json",
            "bundle": "revision-diff-bundle.zip",
        },
        "request": {"view": "diff", "before_revision_id": compiled["before"]["revision_id"], "after_revision_id": compiled["after"]["revision_id"], "edited": list(edited)},
        "timing": timing,
        "outputs": {
            "pack_root": str(pack_root),
            "manifest_path": str(nested_manifest_path),
            "pages": [str(png_path)],
            "primary_page": str(png_path),
            "markdown": str(markdown_path),
            "report": str(json_path),
            "revision_diff_bundle": str(bundle_path),
            "identity": identity,
            "cas": {"revision_diff_manifest": manifest_digest, "revision_diff_bundle": bundle_digest},
            "entrypoints": {
                "manifest": "revision-diff-view/manifest.json",
                "png": "revision-diff-view/revision-diff.png",
                "markdown": "revision-diff-view/revision-diff.md",
                "report": "revision-diff-view/revision-diff.json",
                "bundle": "revision-diff-bundle.zip",
            },
            "navigation": report_json["navigation"],
            "findings": report_json["report"]["findings"],
            "timing": timing,
            "render_requested": False,
        },
    }
