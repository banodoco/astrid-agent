"""Hash-first loading and scope resolution for frozen visualization packs.

This module is the R16 security boundary.  A drill-down may read the selected
evidence pack and its owning run record; it must not inspect a managed timeline
directory.  Current timeline state is reachable only through the executor's
explicit ``refresh_root`` branch.
"""

from __future__ import annotations

import atexit
import hashlib
import hmac
import json
import math
import shutil
import stat
import tempfile
import threading
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, Mapping

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource
from runtime_protocol.dirfd import filesystem as os

from astrid.core.timeline.resolution import AssetIntegrity
from astrid.core.timeline.snapshot import TimelineSnapshot
from astrid.packs.rendering.actions.timeline_visualize.evidence_pack import (
    ACTION_INDEX_NAME,
    ASSET_INDEX_NAME,
    DIAGNOSTICS_NAME,
    GROUND_TRUTH_NAME,
    MANIFEST_NAME,
    PACK_HASHES_NAME,
    TRANSCRIPT_INDEX_NAME,
    VIEW_MAP_NAME,
)
from astrid.packs.rendering.actions.timeline_visualize.ids import (
    RootIdMap,
    parse_qualified_ref,
)
from astrid.packs.rendering.actions.timeline_visualize.model import (
    TRANSITION_FALLBACK_FRAMES,
    ClipModel,
    IntervalFrames,
    IntervalSeconds,
    ModelExtents,
    ShotModel,
    TimelineInspectionModel,
    TrackModel,
)
from astrid.packs.rendering.actions.timeline_visualize.navigation import (
    IdentityMap,
    assign_range_ids,
    assign_transcript_ids,
    build_identity_map,
)
from astrid.packs.rendering.actions.timeline_visualize.schemas import (
    DEFS_PATH,
    SCHEMAS,
)
from astrid.packs.rendering.actions.timeline_visualize.scope import Scope, select_scope
from astrid.packs.rendering.actions.timeline_visualize.transcripts import (
    SpeechOccurrence,
    TranscriptSegment,
)
from astrid.sdk.pagination import paged_rows


class FrozenViewError(ValueError):
    """Base error for a rejected frozen-view operation."""


class ContainmentError(FrozenViewError):
    """The manifest or one of its declared files escapes project ownership."""


class FrozenIntegrityError(FrozenViewError):
    """The pack hash ledger or its cross-artifact chain does not verify."""


class FrozenSchemaError(FrozenViewError):
    """A hashed machine artifact violates its versioned schema."""


class FocusResolutionError(FrozenViewError):
    """A focus reference cannot be resolved within the frozen lineage."""


_REHYDRATED_PACKS_LOCK = threading.Lock()
_REHYDRATED_PACKS: dict[Path, tuple[int, int, int, int]] = {}
_RUNTIME_PAGE_LIMIT = 50
_RUNTIME_MAX_PAGES = 10_000


def _paged_rows(reader: Any, *args: Any) -> list[Any] | None:
    """Read all canonical pages from a cursor-bearing generated method."""

    return paged_rows(
        reader,
        *args,
        limit=_RUNTIME_PAGE_LIMIT,
        max_pages=_RUNTIME_MAX_PAGES,
    )


def _register_rehydrated_pack(root: Path) -> None:
    """Record one exact temp root created by this process."""

    stat = os.lstat(root)
    with _REHYDRATED_PACKS_LOCK:
        _REHYDRATED_PACKS[root] = (
            os.getpid(),
            threading.get_ident(),
            stat.st_dev,
            stat.st_ino,
        )


def _discard_rehydrated_root(root: Path) -> None:
    """Delete *root* only when it is an exact process-owned reconstruction."""

    with _REHYDRATED_PACKS_LOCK:
        ownership = _REHYDRATED_PACKS.pop(root, None)
    if ownership is None:
        return
    creator_pid, _thread_id, expected_device, expected_inode = ownership
    if creator_pid != os.getpid():
        return
    _remove_owned_directory(
        root,
        expected_device=expected_device,
        expected_inode=expected_inode,
    )


def _remove_owned_directory(
    root: Path,
    *,
    expected_device: int,
    expected_inode: int,
) -> None:
    """Remove one exact directory through no-follow descriptors.

    The root pathname is used only to open and finally unlink the directory.
    All recursive deletion stays attached to the verified inode. If another
    process swaps the pathname, a non-empty replacement is never traversed or
    deleted; the original may be left empty at its moved location instead.
    """

    open_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    open_flags |= getattr(os, "O_CLOEXEC", 0)
    parent_fd: int | None = None
    root_fd: int | None = None

    def clear(directory_fd: int) -> None:
        for name in os.listdir(directory_fd):
            try:
                before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISDIR(before.st_mode):
                child_fd: int | None = None
                try:
                    child_fd = os.open(name, open_flags, dir_fd=directory_fd)
                    opened = os.fstat(child_fd)
                    if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                        continue
                    clear(child_fd)
                    current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                    if (current.st_dev, current.st_ino) == (opened.st_dev, opened.st_ino):
                        os.rmdir(name, dir_fd=directory_fd)
                except OSError:
                    continue
                finally:
                    if child_fd is not None:
                        os.close(child_fd)
            else:
                try:
                    current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                    if (current.st_dev, current.st_ino) == (before.st_dev, before.st_ino):
                        os.unlink(name, dir_fd=directory_fd)
                except OSError:
                    continue

    try:
        parent_fd = os.open(root.parent, open_flags)
        root_fd = os.open(root.name, open_flags, dir_fd=parent_fd)
        opened_root = os.fstat(root_fd)
        if (opened_root.st_dev, opened_root.st_ino) != (
            expected_device,
            expected_inode,
        ):
            return
        clear(root_fd)
        current_root = os.stat(root.name, dir_fd=parent_fd, follow_symlinks=False)
        if (current_root.st_dev, current_root.st_ino) == (
            expected_device,
            expected_inode,
        ):
            os.rmdir(root.name, dir_fd=parent_fd)
    except OSError:
        return
    finally:
        if root_fd is not None:
            os.close(root_fd)
        if parent_fd is not None:
            os.close(parent_fd)


def _rehydrate_managed_pack(
    manifest_path: Path,
    *,
    project_root: Path,
) -> Path | None:
    """Reassemble a completed visualization pack from kernel-owned CAS files."""

    projects_root = project_root.parent
    runtime_client = _workspace_runtime_client()
    if runtime_client is None:
        return None
    try:
        project_rows = _paged_rows(runtime_client.list_projects)
        if project_rows is None:
            return None
        project = next((item for item in project_rows if isinstance(item, Mapping) and item.get("slug") == project_root.name), None)
        project_id = project.get("project_id") if isinstance(project, dict) else None
        if not project_id:
            return None
        run_rows = _paged_rows(runtime_client.list_project_runs, str(project_id))
        if run_rows is None:
            return None
    except Exception:
        return None

    # Runtime settlement events carry the immutable output object references.
    # Resolve the owner by the durable project run path; the path itself is a
    # navigation handle, never a source of output bytes.
    owner: tuple[str, list[dict[str, Any]]] | None = None
    resolved_manifest = Path(manifest_path).resolve(strict=False)
    for raw_run in run_rows or []:
        if not isinstance(raw_run, Mapping):
            continue
        run_id = str(raw_run.get("run_id", raw_run.get("id", "")))
        if not run_id or str(raw_run.get("capability", raw_run.get("capability_id", ""))) != "rendering.timeline_visualize":
            continue
        if str(raw_run.get("status", raw_run.get("state", ""))) not in {"completed", "succeeded"}:
            continue
        try:
            run_events = _paged_rows(runtime_client.list_run_events, run_id)
        except Exception:
            continue
        if run_events is None:
            continue
        outputs: list[dict[str, Any]] = []
        for event in run_events or []:
            if not isinstance(event, Mapping) or event.get("event_type", event.get("kind")) not in {"task.completed", "completed", "task.settled"}:
                continue
            payload = event.get("payload", {})
            result = payload.get("result", {}) if isinstance(payload, dict) else {}
            values = result.get("outputs", []) if isinstance(result, dict) else []
            if isinstance(values, list):
                outputs.extend(item for item in values if isinstance(item, dict))
        run_root = (project_root / "runs" / run_id).resolve(strict=False)
        try:
            relative = resolved_manifest.relative_to(run_root)
        except ValueError:
            continue
        output_labels = {
            str(
                _runtime_output_field(item, "path", "label", "filename", "name")
                or ""
            )
            for item in outputs
        }
        if (
            relative.as_posix() not in output_labels
            and relative.name not in output_labels
            and "manifest_path" not in output_labels
        ):
            continue
        owner = (run_id, outputs)
        break
    if owner is None:
        return None
    _owner_run_id, rows = owner

    pack_root = Path(tempfile.mkdtemp(prefix="astrid-frozen-view-")).resolve()
    _register_rehydrated_pack(pack_root)
    atexit.register(_discard_rehydrated_root, pack_root)
    seen: set[str] = set()
    try:
        requested_label = None
        for row in rows:
            label = _runtime_output_field(row, "path", "label", "filename", "name")
            digest = str(
                _runtime_output_field(
                    row, "digest", "content_hash", "sha256", "object_id"
                )
                or ""
            ).removeprefix("sha256:")
            if label == "manifest_path":
                requested_label = MANIFEST_NAME
                label = MANIFEST_NAME
            elif isinstance(label, str) and label.endswith("/" + MANIFEST_NAME):
                requested_label = label.removeprefix("agent-view/")
            if isinstance(label, str) and label.startswith("agent-view/"):
                label = label[len("agent-view/") :]
            if not digest or not isinstance(label, str):
                continue
            try:
                response = runtime_client.get_object("sha256:" + digest)
                data = response if isinstance(response, bytes) else response.get("data") if isinstance(response, Mapping) else getattr(response, "data", None)
                if not isinstance(data, bytes) or hashlib.sha256(data).hexdigest() != digest:
                    raise FrozenIntegrityError("managed visualization output hash mismatch")
            except Exception as exc:
                raise FrozenIntegrityError("managed visualization output object is unavailable") from exc
            label_path = Path(label) if isinstance(label, str) else None
            if (
                label_path is None
                or not label
                or label == "."
                or "\\" in label
                or label_path.is_absolute()
                or ".." in label_path.parts
                or label_path.as_posix() != label
                or label in seen
            ):
                raise ContainmentError("managed visualization output set is malformed")
            seen.add(label)
            destination = (pack_root / label_path).resolve()
            if not destination.is_relative_to(pack_root):
                raise ContainmentError("managed visualization output set is malformed")
            destination.parent.mkdir(parents=True, exist_ok=True)
            # A caller-visible frozen view must never share an inode with
            # managed CAS. An accidental write to the view must not mutate
            # the authoritative object.
            destination.write_bytes(data)
    except FrozenViewError:
        _discard_rehydrated_root(pack_root)
        raise
    except (OSError, TypeError, ValueError) as exc:
        _discard_rehydrated_root(pack_root)
        raise FrozenIntegrityError(
            f"managed visualization output set cannot be reconstructed: {exc}"
        ) from exc
    requested_path = Path(requested_label) if isinstance(requested_label, str) else None
    if (
        requested_path is None
        or requested_path.is_absolute()
        or ".." in requested_path.parts
        or requested_path.name != MANIFEST_NAME
        or requested_path.as_posix() != requested_label
    ):
        _discard_rehydrated_root(pack_root)
        raise ContainmentError("selected managed output is not a visualization manifest")
    root_candidate = pack_root / MANIFEST_NAME
    candidate = pack_root / requested_path
    try:
        root_manifest = json.loads(root_candidate.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        root_manifest = None
    has_navigation_ledger = PACK_HASHES_NAME in seen
    if (
        isinstance(root_manifest, dict)
        and root_manifest.get("kind") == "timeline_visualize_project"
    ):
        reading_order = root_manifest.get("reading_order")
        has_navigation_ledger = bool(reading_order) and isinstance(reading_order, list)
        if has_navigation_ledger:
            for raw_child in reading_order:
                child = Path(raw_child) if isinstance(raw_child, str) else None
                if (
                    child is None
                    or child.is_absolute()
                    or ".." in child.parts
                    or child.name != MANIFEST_NAME
                    or child.as_posix() != raw_child
                    or raw_child not in seen
                    or (child.parent / PACK_HASHES_NAME).as_posix() not in seen
                ):
                    has_navigation_ledger = False
                    break
    if not candidate.is_file() or not has_navigation_ledger:
        _discard_rehydrated_root(pack_root)
        raise FrozenIntegrityError(
            "managed visualization pack predates durable navigation support; rerun visualization"
        )
    return candidate


def discard_rehydrated_pack(path: Path) -> None:
    """Remove a temporary managed-pack reconstruction, if *path* belongs to one."""

    candidate = Path(path).expanduser().resolve(strict=False)
    with _REHYDRATED_PACKS_LOCK:
        roots = tuple(_REHYDRATED_PACKS)
    for root in roots:
        if candidate == root or candidate.is_relative_to(root):
            _discard_rehydrated_root(root)
            return


def rehydrate_managed_pack(
    manifest_path: Path,
    *,
    project_root: Path,
) -> Path:
    """Reassemble a completed kernel visualization output as a logical pack.

    Unlike :func:`load_frozen_view`, this accepts both a single-timeline leaf
    manifest and a multi-timeline project manifest. Ownership and every CAS
    member digest are verified by the kernel task-output reconstruction; the
    returned directory is a copied, disposable view and never shares inodes
    with managed media.
    """

    durable_manifest = Path(manifest_path).expanduser().resolve(strict=False)
    project = Path(project_root).expanduser().resolve(strict=True)
    manifest = _rehydrate_managed_pack(
        durable_manifest,
        project_root=project,
    )
    if manifest is None:
        raise ContainmentError(
            "manifest is not the durable output of a completed project visualization"
        )
    try:
        document = _load_json_file(
            manifest,
            label="managed visualization manifest",
            error_type=FrozenSchemaError,
        )
        kind = document.get("kind")
        if kind == "timeline_visualize":
            # Re-enter through the complete leaf verifier: schema, full pack
            # ledger, cross-artifact identity, and project ownership all run
            # before this directory is exposed to the SDK.
            verified = load_frozen_view(
                durable_manifest,
                project_root=project,
            )
            discard_rehydrated_pack(manifest)
            return verified.pack_root
        if kind != "timeline_visualize_project":
            raise FrozenSchemaError(f"managed visualization manifest has unsupported kind {kind!r}")

        inputs = document.get("inputs")
        if not isinstance(inputs, dict) or inputs.get("project_slug") != project.name:
            raise ContainmentError(
                "managed project visualization identity disagrees with its owning project"
            )
        reading_order = document.get("reading_order")
        timeline_ids = document.get("timeline_ids")
        outputs = document.get("outputs")
        entrypoints = document.get("entrypoints")
        if (
            document.get("schema_version") != 1
            or not isinstance(reading_order, list)
            or not reading_order
            or not isinstance(timeline_ids, list)
            or not all(isinstance(item, str) and item for item in timeline_ids)
            or len(set(timeline_ids)) != len(timeline_ids)
            or len(timeline_ids) != len(reading_order)
            or not isinstance(outputs, list)
            or len(outputs) != len(reading_order)
            or not isinstance(entrypoints, dict)
        ):
            raise FrozenSchemaError("managed project visualization has an invalid project index")
        root = manifest.parent
        managed_root = (project.parent / ".astrid" / "media" / "sha256").resolve()
        child_ids: list[str] = []
        declared_roots: set[str] = set()
        for index, raw_child in enumerate(reading_order, start=1):
            root_name = f"TL{index:02d}"
            declared_output = outputs[index - 1]
            child = Path(raw_child) if isinstance(raw_child, str) else None
            if (
                not isinstance(declared_output, dict)
                or declared_output.get("name") != root_name
                or declared_output.get("path") != root_name
                or declared_output.get("type") != "directory"
                or (
                    "manifest_sha256" in declared_output
                    and not isinstance(declared_output.get("manifest_sha256"), str)
                )
                or entrypoints.get(root_name) != raw_child
                or child is None
                or child.is_absolute()
                or ".." in child.parts
                or child.parent.as_posix() != root_name
                or child.name != MANIFEST_NAME
                or child.as_posix() != raw_child
            ):
                raise FrozenSchemaError(
                    "managed project visualization has an invalid child manifest path"
                )
            child_path = (root / child).resolve(strict=True)
            if not child_path.is_relative_to(root):
                raise ContainmentError("managed project visualization child escapes the pack")
            child_digest = hashlib.sha256(child_path.read_bytes()).hexdigest()
            if (
                "manifest_sha256" in declared_output
                and declared_output["manifest_sha256"] != child_digest
            ):
                raise FrozenIntegrityError(
                    "project manifest child digest disagrees with its declared output"
                )
            child_cas = managed_root / child_digest[:2] / child_digest[2:4] / child_digest
            child_view = load_frozen_view(child_cas, project_root=project)
            child_ids.append(child_view.timeline_ulid)
            discard_rehydrated_pack(child_view.pack_root)
            declared_roots.add(child.parts[0])
        if len(set(child_ids)) != len(child_ids):
            raise FrozenIntegrityError(
                "project manifest resolves more than one root to the same timeline"
            )
        if timeline_ids != child_ids:
            raise FrozenIntegrityError(
                "project manifest timeline_ids disagree with verified child packs"
            )
        actual_roots = {path.name for path in root.iterdir() if path.is_dir()}
        actual_files = {path.name for path in root.iterdir() if path.is_file()}
        if actual_roots != declared_roots or actual_files != {MANIFEST_NAME}:
            raise FrozenIntegrityError(
                "managed project visualization contains undeclared root artifacts"
            )
        return root
    except FrozenViewError:
        discard_rehydrated_pack(manifest)
        raise
    except (OSError, TypeError, ValueError) as exc:
        discard_rehydrated_pack(manifest)
        raise FrozenIntegrityError(
            f"managed project visualization cannot be verified: {exc}"
        ) from exc


class _DuplicateJsonKey(ValueError):
    pass


@dataclass(frozen=True)
class FrozenView:
    pack_root: Path
    manifest: dict
    ground_truth: dict
    action_index: dict
    identity_map: IdentityMap
    snapshot_sns: str
    timeline_uuid: str
    timeline_ulid: str
    # Hashed companions retained in memory so rendering never reopens mutable
    # pack paths after preflight (a local TOCTOU boundary).
    asset_index: dict
    transcript_index: dict
    diagnostics: dict
    source_manifest: Path | None = None
    # Optional in-memory producer metadata.  Frozen evidence remains the
    # immutable lineage source; this field only carries the already-admitted
    # host snapshot handoff into the emitter adapter when supplied.
    metadata: dict | None = None


def _pairs_no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _reject_constant(token: str) -> None:
    raise ValueError(f"non-standard JSON constant {token!r}")


def _load_json_bytes(payload: bytes, *, label: str, error_type: type[FrozenViewError]) -> dict:
    try:
        value = json.loads(
            payload,
            object_pairs_hook=_pairs_no_duplicates,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise error_type(f"{label} is not strict JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise error_type(f"{label} must contain a JSON object")
    return value


def _load_json_file(path: Path, *, label: str, error_type: type[FrozenViewError]) -> dict:
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise error_type(f"cannot read {label}: {exc}") from exc
    return _load_json_bytes(payload, label=label, error_type=error_type)


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _normalized_pack_path(pack_root: Path, raw: object) -> tuple[str, Path]:
    if not isinstance(raw, str) or not raw:
        raise FrozenIntegrityError("pack-hashes.json file names must be non-empty strings")
    pure = PurePosixPath(raw)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise ContainmentError(f"pack hash path is not normalized and relative: {raw!r}")
    normalized = pure.as_posix()
    if normalized != raw or "\\" in raw:
        raise ContainmentError(f"pack hash path is not canonical POSIX form: {raw!r}")
    candidate = pack_root.joinpath(*pure.parts)
    if candidate.is_symlink():
        raise ContainmentError(f"pack artifact is a symlink: {raw}")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise FrozenIntegrityError(f"pack artifact is missing: {raw}") from exc
    if not resolved.is_relative_to(pack_root) or not resolved.is_file():
        raise ContainmentError(f"pack artifact escapes its pack root: {raw}")
    return normalized, resolved


def _actual_pack_files(pack_root: Path) -> set[str]:
    result: set[str] = set()
    for current, directories, files in os.walk(pack_root, followlinks=False):
        current_path = Path(current)
        for name in directories:
            directory = current_path / name
            if directory.is_symlink():
                raise ContainmentError(
                    f"evidence pack contains a symlinked directory: "
                    f"{directory.relative_to(pack_root).as_posix()}"
                )
        for name in files:
            path = current_path / name
            rel = path.relative_to(pack_root).as_posix()
            if path.is_symlink():
                raise ContainmentError(f"evidence pack contains a symlinked file: {rel}")
            if not path.is_file():
                raise FrozenIntegrityError(f"evidence pack entry is not a regular file: {rel}")
            result.add(rel)
    return result


def _verify_pack(pack_root: Path) -> tuple[dict, dict[str, bytes]]:
    """Verify every byte before any artifact is trusted as structured data."""

    ledger_path = pack_root / PACK_HASHES_NAME
    if ledger_path.is_symlink():
        raise ContainmentError("pack-hashes.json must not be a symlink")
    ledger = _load_json_file(
        ledger_path,
        label=PACK_HASHES_NAME,
        error_type=FrozenIntegrityError,
    )
    if set(ledger) != {"schema_version", "kind", "coverage", "files"}:
        raise FrozenIntegrityError("pack-hashes.json has an invalid top-level shape")
    if ledger.get("schema_version") != 1 or ledger.get("kind") != "timeline_visualize_pack_hashes":
        raise FrozenIntegrityError("pack-hashes.json has the wrong kind or schema_version")
    coverage = ledger.get("coverage")
    expected_coverage = {
        "manifest": MANIFEST_NAME,
        "ground_truth": GROUND_TRUTH_NAME,
        "view_map": VIEW_MAP_NAME,
        "action_index": ACTION_INDEX_NAME,
        "asset_index": ASSET_INDEX_NAME,
        "transcript_index": TRANSCRIPT_INDEX_NAME,
        "diagnostics": DIAGNOSTICS_NAME,
        "metric_definitions": "metric-definitions.json",
        "reading_guide": "reading-guide.md",
    }
    if coverage != expected_coverage:
        raise FrozenIntegrityError(
            "pack-hashes.json coverage does not name every mandatory core artifact"
        )
    files = ledger.get("files")
    if not isinstance(files, dict) or not files:
        raise FrozenIntegrityError("pack-hashes.json files must be a non-empty object")
    declared = list(files)
    mandatory_order = list(expected_coverage.values())
    if declared[: len(mandatory_order)] != mandatory_order:
        raise FrozenIntegrityError("pack-hashes.json core reading order is invalid")

    actual = _actual_pack_files(pack_root)
    expected = set(declared) | {PACK_HASHES_NAME}
    missing = sorted(expected - actual)
    orphans = sorted(actual - expected)
    if missing or orphans:
        details: list[str] = []
        if missing:
            details.append(f"missing={missing!r}")
        if orphans:
            details.append(f"orphans={orphans!r}")
        raise FrozenIntegrityError("pack hash coverage mismatch: " + "; ".join(details))

    payloads: dict[str, bytes] = {}
    for raw in declared:
        rel, path = _normalized_pack_path(pack_root, raw)
        record = files[raw]
        if not isinstance(record, dict) or set(record) != {"sha256", "bytes"}:
            raise FrozenIntegrityError(f"invalid hash record for {raw}")
        expected_hash = record.get("sha256")
        expected_bytes = record.get("bytes")
        if (
            not isinstance(expected_hash, str)
            or len(expected_hash) != 64
            or any(ch not in "0123456789abcdef" for ch in expected_hash)
            or isinstance(expected_bytes, bool)
            or not isinstance(expected_bytes, int)
            or expected_bytes < 0
        ):
            raise FrozenIntegrityError(f"invalid digest or byte count for {raw}")
        try:
            payload = path.read_bytes()
        except OSError as exc:
            raise FrozenIntegrityError(f"cannot read hashed artifact {raw}: {exc}") from exc
        if len(payload) != expected_bytes or not hmac.compare_digest(
            _sha256(payload), expected_hash
        ):
            raise FrozenIntegrityError(f"sha256/byte-count mismatch for {raw}")
        payloads[rel] = payload

    # Hashes are now trusted.  The manifest output order must match the ledger
    # exactly (manifest first, then every declared output).  This also rejects
    # reordered reading ledgers that happen to carry valid individual hashes.
    manifest = _load_json_bytes(
        payloads[MANIFEST_NAME],
        label=MANIFEST_NAME,
        error_type=FrozenIntegrityError,
    )
    outputs = manifest.get("outputs")
    if not isinstance(outputs, list):
        raise FrozenIntegrityError("manifest outputs must be an array")
    output_paths: list[str] = []
    for index, record in enumerate(outputs):
        if not isinstance(record, dict):
            raise FrozenIntegrityError(f"manifest outputs[{index}] must be an object")
        raw_path = record.get("path")
        if not isinstance(raw_path, str):
            raise FrozenIntegrityError(f"manifest outputs[{index}].path must be a string")
        output_paths.append(raw_path)
        ledger_record = files.get(raw_path)
        if not isinstance(ledger_record, dict):
            raise FrozenIntegrityError(f"manifest output is not covered by pack hashes: {raw_path}")
        if record.get("bytes") != ledger_record.get("bytes"):
            raise FrozenIntegrityError(
                f"manifest byte count disagrees with pack hashes: {raw_path}"
            )
        raw_digest = record.get("sha256")
        content_hash = record.get("content_hash")
        expected_digest = ledger_record.get("sha256")
        if raw_digest is not None and raw_digest != expected_digest:
            raise FrozenIntegrityError(f"manifest sha256 disagrees with pack hashes: {raw_path}")
        if content_hash != f"sha256:{expected_digest}":
            raise FrozenIntegrityError(
                f"manifest content_hash disagrees with pack hashes: {raw_path}"
            )
    if declared != [MANIFEST_NAME, *output_paths]:
        raise FrozenIntegrityError("pack-hashes.json reading order disagrees with manifest outputs")
    return manifest, payloads


def _schema_registry() -> tuple[dict[str, dict], Registry]:
    documents: dict[str, dict] = {
        "_defs": _load_json_file(DEFS_PATH, label="schema _defs", error_type=FrozenSchemaError)
    }
    for name, descriptor in SCHEMAS.items():
        documents[name] = descriptor.load()
    resources = [
        (document["$id"], Resource.from_contents(document)) for document in documents.values()
    ]
    return documents, Registry().with_resources(resources)


def _validate_schema(
    name: str, value: dict, documents: dict[str, dict], registry: Registry
) -> None:
    validator = Draft202012Validator(
        documents[name],
        registry=registry,
        format_checker=FormatChecker(),
    )
    errors = sorted(
        validator.iter_errors(value),
        key=lambda error: tuple(str(part) for part in error.absolute_path),
    )
    if errors:
        first = errors[0]
        location = "/".join(str(part) for part in first.absolute_path) or "$"
        raise FrozenSchemaError(f"{name}.json violates schema at {location}: {first.message}")


def _canonical_ref(identity: tuple[str, str, str]) -> dict[str, str]:
    return {
        "timeline_uuid": identity[0],
        "kind": identity[1],
        "authored_id": identity[2],
    }


def _reconstruct_identity_map(ground_truth: dict, snapshot: dict) -> IdentityMap:
    rows = ground_truth.get("frozen_objects")
    if not isinstance(rows, list) or not rows:
        raise FrozenIntegrityError("ground-truth.json has no frozen identity map")
    timeline = snapshot.get("timeline")
    if not isinstance(timeline, dict):
        raise FrozenIntegrityError("snapshot timeline identity is missing")
    timeline_uuid = timeline.get("uuid")
    timeline_ulid = timeline.get("ulid")
    root_sns = snapshot.get("digest")
    if not all(isinstance(item, str) and item for item in (timeline_uuid, timeline_ulid, root_sns)):
        raise FrozenIntegrityError("snapshot identity fields are invalid")

    allocator = RootIdMap()
    ordinary_rows = ground_truth.get("objects")
    ordinary_by_ref = (
        {
            row.get("qualified_ref"): row
            for row in ordinary_rows
            if isinstance(row, dict) and isinstance(row.get("qualified_ref"), str)
        }
        if isinstance(ordinary_rows, list)
        else {}
    )
    seen_rows: dict[str, dict] = {}
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise FrozenIntegrityError(f"frozen_objects[{index}] must be an object")
        display_id = row.get("qualified_ref")
        stable_id = row.get("stable_id")
        canonical = row.get("canonical_ref")
        if not isinstance(display_id, str) or not isinstance(canonical, dict):
            raise FrozenIntegrityError(f"frozen_objects[{index}] is missing identity fields")
        parsed = parse_qualified_ref(display_id)
        if parsed.is_timestamp or parsed.stable_id != stable_id:
            raise FrozenIntegrityError(f"frozen object stable id disagrees with {display_id}")
        identity = (
            canonical.get("timeline_uuid"),
            canonical.get("kind"),
            canonical.get("authored_id"),
        )
        if not all(isinstance(item, str) and item for item in identity):
            raise FrozenIntegrityError(f"frozen_objects[{index}] has an invalid canonical_ref")
        if identity[0] != timeline_uuid:
            raise FrozenIntegrityError(f"frozen object {display_id} belongs to another timeline")
        try:
            allocator.add(identity, display_id)  # validates grammar/kind/ordinals
        except (TypeError, ValueError) as exc:
            raise FrozenIntegrityError(
                f"invalid frozen identity allocation {display_id}: {exc}"
            ) from exc
        seen_rows[display_id] = row
    for ref, row in ordinary_by_ref.items():
        if ref not in seen_rows or seen_rows[ref] != row:
            raise FrozenIntegrityError(f"scoped object {ref!r} disagrees with the frozen root map")

    semantic = dict(allocator.entries)
    display = {display_id: identity for identity, display_id in semantic.items()}
    identity_map = IdentityMap(
        semantic_to_display=MappingProxyType(semantic),
        display_to_semantic=MappingProxyType(display),
        root_sns=root_sns,
        timeline_uuid=timeline_uuid,
        timeline_ulid=timeline_ulid,
    ).child_copy()
    timeline_display = timeline.get("qualified_ref")
    expected_timeline_identity = (timeline_uuid, "timeline", timeline_uuid)
    if (
        timeline_display != timeline.get("stable_id")
        or identity_map.lookup_display(timeline_display) != expected_timeline_identity
    ):
        raise FrozenIntegrityError("snapshot timeline identity disagrees with the frozen ID map")
    return identity_map


def _verify_model_refs(ground_truth: dict, identity_map: IdentityMap) -> None:
    timeline = ground_truth.get("frozen_timeline")
    if not isinstance(timeline, dict):
        raise FrozenIntegrityError("ground-truth.json has no frozen timeline model")
    timeline_ref = timeline.get("timeline_ref")
    timeline_identity = (
        identity_map.lookup_display(timeline_ref) if isinstance(timeline_ref, str) else None
    )
    if timeline_identity is None or timeline_identity[1] != "timeline":
        raise FrozenIntegrityError("frozen timeline_ref does not resolve through the ID map")

    track_rows = timeline.get("tracks")
    if not isinstance(track_rows, list):
        raise FrozenIntegrityError("frozen timeline tracks must be an array")
    track_ids: set[str] = set()
    for row in track_rows:
        track_id = row.get("authored_id") if isinstance(row, dict) else None
        if not isinstance(track_id, str) or not track_id:
            raise FrozenIntegrityError("frozen timeline tracks contains an invalid authored_id")
        if track_id in track_ids:
            raise FrozenIntegrityError(
                f"frozen track authored_id {track_id!r} does not resolve uniquely in "
                "frozen_timeline.tracks"
            )
        track_ids.add(track_id)

    model_rows: dict[str, list[dict]] = {}
    model_refs: dict[str, set[str]] = {}
    clip_ids: set[str] = set()
    for collection, expected_kind in (("clips", "clip"), ("assets", "asset")):
        rows = timeline.get(collection)
        if not isinstance(rows, list):
            raise FrozenIntegrityError(f"frozen timeline {collection} must be an array")
        typed_rows: list[dict] = []
        refs: set[str] = set()
        for row in rows:
            if not isinstance(row, dict):
                raise FrozenIntegrityError(f"frozen timeline {collection} contains a non-object")
            ref = row.get("qualified_ref")
            identity = identity_map.lookup_display(ref) if isinstance(ref, str) else None
            if (
                identity is None
                or identity[1] != expected_kind
                or row.get("canonical_ref") != _canonical_ref(identity)
            ):
                raise FrozenIntegrityError(
                    f"frozen timeline ref does not match the ID map: {ref!r}"
                )
            typed_rows.append(row)
            refs.add(ref)
            if collection == "clips":
                clip_ids.add(identity[2])
        model_rows[collection] = typed_rows
        model_refs[collection] = refs

    for row in model_rows["clips"]:
        clip_ref = row["qualified_ref"]
        if not isinstance(row.get("mounted_interval"), dict):
            raise FrozenIntegrityError(
                f"frozen clip {clip_ref!r} has no compositor-mounted interval"
            )
        track_id = row.get("track_authored_id")
        if track_id not in track_ids:
            raise FrozenIntegrityError(
                f"frozen clip {clip_ref!r} track_authored_id {track_id!r} does not "
                "resolve to frozen_timeline.tracks[].authored_id"
            )
        for asset_ref in row.get("asset_refs", []):
            if asset_ref not in model_refs["assets"]:
                raise FrozenIntegrityError(
                    f"frozen clip {clip_ref!r} asset ref {asset_ref!r} does not "
                    "resolve to frozen_timeline.assets[].qualified_ref"
                )
        for speech_ref in row.get("mapped_speech", []):
            speech_identity = identity_map.lookup_display(speech_ref)
            if speech_identity is None or speech_identity[1] != "speech_occurrence":
                raise FrozenIntegrityError(
                    f"frozen clip {clip_ref!r} speech ref {speech_ref!r} does not "
                    "resolve to a frozen speech occurrence"
                )

    for key, expected_kind in (("frozen_shots", "shot"), ("frozen_ranges", "range")):
        rows = ground_truth.get(key, [])
        if not isinstance(rows, list):
            raise FrozenIntegrityError(f"ground-truth.json {key} must be an array")
        for row in rows:
            ref = row.get("qualified_ref") if isinstance(row, dict) else None
            identity = identity_map.lookup_display(ref) if isinstance(ref, str) else None
            if (
                identity is None
                or identity[1] != expected_kind
                or row.get("canonical_ref") != _canonical_ref(identity)
            ):
                raise FrozenIntegrityError(f"{key} ref does not match the ID map: {ref!r}")
            if key == "frozen_shots":
                for clip_id in row.get("member_clip_ids", []):
                    if clip_id not in clip_ids:
                        raise FrozenIntegrityError(
                            f"frozen shot {ref!r} member clip ref {clip_id!r} does not "
                            "resolve to frozen_timeline.clips[].canonical_ref.authored_id"
                        )


def _verify_action_refs(action_index: dict, identity_map: IdentityMap) -> None:
    entries = action_index.get("entries")
    if not isinstance(entries, dict):
        raise FrozenIntegrityError("action-index.json entries must be an object")
    for ref, entry in entries.items():
        identity = identity_map.lookup_display(ref)
        if identity is None:
            raise FrozenIntegrityError(
                f"action-index entry does not resolve through ground truth: {ref}"
            )
        if not isinstance(entry, dict) or entry.get("canonical_ref") != _canonical_ref(identity):
            raise FrozenIntegrityError(f"action-index canonical_ref disagrees for {ref}")
        relations = entry.get("relations")
        if not isinstance(relations, dict):
            raise FrozenIntegrityError(f"action-index relations are invalid for {ref}")
        targets = [relations.get("parent"), relations.get("previous"), relations.get("next")]
        children = relations.get("children")
        if not isinstance(children, list):
            raise FrozenIntegrityError(f"action-index children are invalid for {ref}")
        targets.extend(children)
        for target in targets:
            if target is not None and identity_map.lookup_display(target) is None:
                raise FrozenIntegrityError(f"action-index relation {ref} -> {target!r} is dangling")
        actions = entry.get("actions")
        if not isinstance(actions, dict):
            raise FrozenIntegrityError(f"action-index actions are invalid for {ref}")
        for action_name, action in actions.items():
            focus = action.get("focus") if isinstance(action, dict) else None
            if focus is None:
                continue
            try:
                parsed = parse_qualified_ref(focus)
            except ValueError as exc:
                raise FrozenIntegrityError(
                    f"action {ref}.{action_name} has malformed focus"
                ) from exc
            if not parsed.is_timestamp and identity_map.lookup_display(str(parsed)) is None:
                raise FrozenIntegrityError(
                    f"action {ref}.{action_name} focus is absent from ground truth"
                )
            timeline_identity = identity_map.lookup_display(parsed.timeline_id)
            if timeline_identity is None or timeline_identity[1] != "timeline":
                raise FrozenIntegrityError(f"action {ref}.{action_name} targets another timeline")


def _verify_run_ownership(
    manifest_path: Path, project_root: Path, manifest: dict, timeline_ulid: str
) -> None:
    try:
        relative = manifest_path.relative_to(project_root)
    except ValueError as exc:
        raise ContainmentError("manifest is not project-owned") from exc
    parts = relative.parts
    if (
        len(parts) < 4
        or parts[0] != "runs"
        or parts[2] != "agent-view"
        or parts[-1] != MANIFEST_NAME
    ):
        raise ContainmentError("manifest is not inside a project-owned visualization run")
    run_id = parts[1]
    runs_root = (project_root / "runs").resolve(strict=True)
    declared_run_root = project_root / "runs" / run_id
    if declared_run_root.is_symlink():
        raise ContainmentError("owning visualization run directory must not be a symlink")
    run_root = declared_run_root.resolve(strict=True)
    if (
        run_root.parent != runs_root
        or not run_root.is_dir()
        or not manifest_path.is_relative_to(run_root)
    ):
        raise ContainmentError("manifest run path escapes the owning run")
    project_slug = project_root.name
    # Runtime ownership is mandatory. A filesystem run.json is a legacy local
    # authority and cannot prove ownership after the runtime cutover.
    kernel_info = _kernel_frozen_run_info(project_slug, run_id, project_root.parent)
    if kernel_info is None:
        raise ContainmentError("runtime run ownership is unavailable; filesystem run.json is not authoritative")
    run_project_id = kernel_info.get("project_id")
    current_project_id = kernel_info.get("current_project_id")
    if not run_project_id or not current_project_id or str(run_project_id) != str(current_project_id):
        raise ContainmentError("kernel run does not belong to the current project")
    if kernel_info.get("status") not in ("succeeded", "completed"):
        raise ContainmentError(
            "kernel run does not own this timeline visualization pack (not completed)"
        )
    if kernel_info.get("capability") not in (None, "rendering.timeline_visualize"):
        raise ContainmentError("kernel run does not own this timeline visualization pack")
    if isinstance(kernel_info.get("timeline_ids"), list):
        if timeline_ulid not in kernel_info["timeline_ids"]:
            raise ContainmentError("kernel run does not own this timeline visualization pack")
    inputs = manifest.get("inputs")
    resolved_project = inputs.get("resolved_project") if isinstance(inputs, dict) else None
    if not isinstance(resolved_project, dict) or resolved_project.get("slug") != project_slug:
        raise ContainmentError("manifest project identity disagrees with its owning run")
    _verify_runtime_output_binding(
        manifest_path,
        manifest,
        runtime_outputs=kernel_info.get("outputs"),
    )


def _runtime_event_value(event: Any, *keys: str) -> Any:
    """Read an event field from either a generated DTO or a JSON mapping."""

    if isinstance(event, Mapping):
        for key in keys:
            if key in event:
                return event[key]
        return None
    for key in keys:
        try:
            return getattr(event, key)
        except AttributeError:
            continue
    return None


def _runtime_settled_outputs(runtime_client: Any, run_id: str, run_info: Mapping[str, Any]) -> list[dict[str, Any]] | None:
    """Return the output evidence from the exact run's terminal settlement.

    Output ownership cannot be inferred from project/capability/timeline
    identity alone: a copied pack under another completed run would otherwise
    pass those checks.  Prefer the immutable output list on the run DTO, then
    read the terminal settlement event for runtimes that expose outputs only
    there.  The caller deliberately receives ``None`` when no evidence exists;
    an absent evidence stream is not equivalent to an empty output set.
    """

    candidates: list[Any] = []
    for container in (run_info, run_info.get("result"), run_info.get("metadata")):
        if not isinstance(container, Mapping):
            continue
        for key in ("outputs", "output_objects"):
            value = container.get(key)
            if isinstance(value, list):
                candidates = value
                break
        if candidates:
            break
    if candidates:
        return [item for item in candidates if isinstance(item, Mapping)]

    reader = getattr(runtime_client, "list_run_events", None)
    if not callable(reader):
        return None
    try:
        events = _paged_rows(reader, run_id)
    except Exception:
        return None
    if events is None:
        return None
    # The event stream is ordered.  Use the last terminal settlement so a
    # retry cannot make an older attempt's outputs appear to belong to the
    # winning run.
    for event in reversed(events):
        kind = _runtime_event_value(event, "event_type", "kind", "type")
        if kind not in {"task.completed", "completed", "task.settled"}:
            continue
        payload = _runtime_event_value(event, "payload", "data")
        if not isinstance(payload, Mapping):
            continue
        result = payload.get("result")
        if isinstance(result, Mapping) and isinstance(result.get("outputs"), list):
            return [item for item in result["outputs"] if isinstance(item, Mapping)]
        if isinstance(payload.get("outputs"), list):
            return [item for item in payload["outputs"] if isinstance(item, Mapping)]
        if isinstance(payload.get("objects"), list):
            return [item for item in payload["objects"] if isinstance(item, Mapping)]
    return None


def _runtime_output_field(output: Mapping[str, Any], *keys: str) -> Any:
    """Read an output field, including the repository's params envelope."""

    for key in keys:
        if key in output:
            return output[key]
    params = output.get("params")
    if isinstance(params, Mapping):
        for key in keys:
            if key in params:
                return params[key]
    return None


def _verify_runtime_output_binding(
    manifest_path: Path,
    manifest: Mapping[str, Any],
    *,
    runtime_outputs: Any,
) -> None:
    """Require the selected pack's exact files and digests in run evidence."""

    if not isinstance(runtime_outputs, list) or not runtime_outputs:
        raise ContainmentError(
            "runtime settlement output evidence is unavailable; filesystem bytes are not authoritative"
        )
    declared = manifest.get("outputs")
    if not isinstance(declared, list) or not declared:
        raise FrozenIntegrityError("visualization manifest has no declared outputs")
    expected: dict[str, tuple[str, int]] = {}
    for index, raw in enumerate(declared):
        if not isinstance(raw, Mapping):
            raise FrozenIntegrityError(f"visualization manifest output {index} is malformed")
        path = raw.get("path")
        digest = raw.get("content_hash", raw.get("sha256"))
        size = raw.get("bytes", raw.get("size"))
        if not isinstance(path, str) or not path or not isinstance(digest, str):
            raise FrozenIntegrityError(f"visualization manifest output {index} lacks identity")
        digest = digest.removeprefix("sha256:")
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise FrozenIntegrityError(f"visualization manifest output {path!r} has an invalid digest")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise FrozenIntegrityError(f"visualization manifest output {path!r} has an invalid byte count")
        if path in expected:
            raise FrozenIntegrityError(f"visualization manifest output {path!r} is duplicated")
        expected[path] = (digest, size)

    # ``pack-hashes.json`` cannot list itself in the inner manifest, but it is
    # still a settled concrete output.  Include it when the verified pack has
    # one so copied packs cannot replace the integrity ledger under an
    # otherwise matching manifest.
    ledger_path = manifest_path.parent / PACK_HASHES_NAME
    if ledger_path.is_file():
        ledger_bytes = ledger_path.read_bytes()
        expected[PACK_HASHES_NAME] = (
            hashlib.sha256(ledger_bytes).hexdigest(),
            len(ledger_bytes),
        )

    # Runtime output paths are commonly rooted at the run's ``agent-view``
    # directory while the inner manifest is pack-relative.  Strip that one
    # path-derived prefix before matching.  The resulting path must be an
    # exact manifest-relative path: a basename match would let
    # ``other/manifest.json`` masquerade as this pack's ``manifest.json``.
    try:
        run_relative = manifest_path.resolve(strict=False).relative_to(
            manifest_path.resolve(strict=False).parent.parent
        )
        pack_prefix = run_relative.parent.as_posix()
    except (ValueError, IndexError):
        pack_prefix = "agent-view"
    observed: dict[str, tuple[str, int]] = {}
    for index, raw in enumerate(runtime_outputs):
        if not isinstance(raw, Mapping):
            raise ContainmentError(f"runtime settlement output {index} is malformed")
        raw_path = _runtime_output_field(raw, "path", "label", "filename", "name")
        digest = _runtime_output_field(raw, "digest", "content_hash", "sha256", "object_id")
        size = _runtime_output_field(raw, "bytes", "size")
        if not isinstance(raw_path, str) or not raw_path or not isinstance(digest, str):
            raise ContainmentError("runtime settlement output evidence lacks path or digest")
        normalized = raw_path.removeprefix("./")
        if normalized == "manifest_path":
            normalized = MANIFEST_NAME
        if (
            normalized.startswith("/")
            or "\\" in normalized
            or ".." in PurePosixPath(normalized).parts
        ):
            raise ContainmentError(f"runtime settlement output {raw_path!r} has an invalid path")
        if normalized.startswith(pack_prefix + "/"):
            normalized = normalized[len(pack_prefix) + 1 :]
        if normalized == MANIFEST_NAME and MANIFEST_NAME not in expected:
            try:
                manifest_bytes = manifest_path.read_bytes()
            except OSError as exc:
                raise FrozenIntegrityError(
                    "visualization manifest cannot be read for runtime binding"
                ) from exc
            expected[MANIFEST_NAME] = (
                hashlib.sha256(manifest_bytes).hexdigest(),
                len(manifest_bytes),
            )
        if normalized not in expected:
            raise ContainmentError(
                f"runtime settlement output {raw_path!r} is not in the selected visualization pack"
            )
        digest = digest.removeprefix("sha256:")
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise ContainmentError(f"runtime settlement output {raw_path!r} has an invalid digest")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise ContainmentError(f"runtime settlement output {raw_path!r} has an invalid byte count")
        if normalized in observed:
            raise ContainmentError(f"runtime settlement output {normalized!r} is duplicated")
        observed[normalized] = (digest, size)

    if set(observed) != set(expected):
        missing = sorted(set(expected) - set(observed))
        extra = sorted(set(observed) - set(expected))
        raise ContainmentError(
            f"runtime settlement outputs do not exactly bind this visualization pack: missing={missing!r}, extra={extra!r}"
        )
    for path, (digest, size) in observed.items():
        expected_digest, expected_size = expected[path]
        if not hmac.compare_digest(digest, expected_digest) or size != expected_size:
            raise FrozenIntegrityError(
                f"runtime settlement output evidence disagrees with visualization pack: {path}"
            )


def _kernel_frozen_run_info(
    project_slug: str, run_id: str, projects_root: Path
) -> dict[str, Any] | None:
    try:
        runtime_client = _workspace_runtime_client()
        if runtime_client is None:
            return None
        info = runtime_client.get_run(run_id)
        if not isinstance(info, dict):
            return None
        result = {
            "status": str(info.get("status", info.get("state", ""))),
            "capability": str(info.get("capability", info.get("capability_id"))) if info.get("capability", info.get("capability_id")) is not None else None,
            "timeline_ids": info.get("timeline_ids") or (info.get("metadata", {}) if isinstance(info.get("metadata"), dict) else {}).get("timeline_ids"),
        }
        # Keep settlement evidence attached to the exact path-derived run id.
        # It is intentionally omitted for narrow legacy test doubles that do
        # not expose an event reader; real runtime ownership checks below then
        # fail closed rather than accepting filesystem run.json as authority.
        outputs = _runtime_settled_outputs(runtime_client, run_id, info)
        if outputs is not None:
            result["outputs"] = outputs
        run_project_id = info.get("project_id") or info.get("project")
        projects_reader = getattr(runtime_client, "list_projects", None)
        if callable(projects_reader):
            rows = _paged_rows(projects_reader)
            if rows is None:
                return None
            current = next((item for item in rows or [] if isinstance(item, Mapping) and item.get("slug") == project_slug), None)
            current_project_id = current.get("project_id") or current.get("id") if isinstance(current, Mapping) else None
            result["project_id"] = run_project_id
            result["current_project_id"] = current_project_id
        return result
    except Exception:
        return None


def _workspace_runtime_client() -> Any | None:
    """Create the generated runtime client, without importing local authority."""
    try:
        from astrid.sdk.workspace_client import WorkspaceClient, resolve_runtime_connection

        endpoint, token = resolve_runtime_connection()
        return WorkspaceClient(endpoint, token)
    except Exception:
        return None


def _load_frozen_view_impl(manifest_path: Path, *, project_root: Path) -> FrozenView:
    """Verify and load a frozen evidence pack for drill-down.

    Preflight order is deliberate: project containment, complete pack hashes,
    schema, cross-artifact chain, then run ownership.  Nothing from the pack is
    used to select timeline state, and no managed-timeline reader is imported.
    """

    project = Path(project_root).expanduser().resolve(strict=True)
    raw_manifest = Path(manifest_path).expanduser()
    absolute_manifest = raw_manifest if raw_manifest.is_absolute() else Path.cwd() / raw_manifest
    if absolute_manifest.is_symlink():
        raise ContainmentError("--from-view manifest must not be a symlink")
    resolved_manifest = absolute_manifest.resolve(strict=False)
    source_manifest = resolved_manifest
    managed_output = False
    if not resolved_manifest.is_relative_to(project):
        rehydrated = _rehydrate_managed_pack(
            resolved_manifest,
            project_root=project,
        )
        if rehydrated is None:
            raise ContainmentError(
                "--from-view must be inside the project or the durable manifest "
                "returned by a completed project visualization"
            )
        resolved_manifest = rehydrated
        absolute_manifest = rehydrated
        managed_output = True
    if not resolved_manifest.is_file() or resolved_manifest.name != MANIFEST_NAME:
        raise FrozenIntegrityError("--from-view must name an existing manifest.json")
    pack_root = resolved_manifest.parent.resolve(strict=True)

    # 2. Full hash/orphan/read-order preflight.
    manifest, payloads = _verify_pack(pack_root)

    # 3. Versioned schemas and mandatory SNS.
    documents, registry = _schema_registry()
    _validate_schema("manifest", manifest, documents, registry)
    snapshots = manifest.get("snapshots")
    if not isinstance(snapshots, list) or len(snapshots) != 1:
        raise FrozenSchemaError("a drill-down leaf manifest must carry exactly one snapshot")
    snapshot = snapshots[0]
    snapshot_sns = snapshot.get("digest") if isinstance(snapshot, dict) else None
    if not isinstance(snapshot_sns, str) or not snapshot_sns.startswith("SNS:"):
        raise FrozenSchemaError("manifest snapshot SNS is missing")

    core_names = {
        "ground-truth": GROUND_TRUTH_NAME,
        "action-index": ACTION_INDEX_NAME,
        "asset-index": ASSET_INDEX_NAME,
        "transcript-index": TRANSCRIPT_INDEX_NAME,
        "diagnostics": DIAGNOSTICS_NAME,
        "view-map": VIEW_MAP_NAME,
    }
    core: dict[str, dict] = {}
    for schema_name, filename in core_names.items():
        document = _load_json_bytes(
            payloads[filename],
            label=filename,
            error_type=FrozenSchemaError,
        )
        _validate_schema(schema_name, document, documents, registry)
        core[schema_name] = document

    # 4. Cross-artifact chain of trust and deterministic ID reconstruction.
    ground_truth = core["ground-truth"]
    if ground_truth.get("project_slug") != project.name:
        raise ContainmentError("ground-truth project identity disagrees with its owning project")
    action_index = core["action-index"]
    for key, expected_type in (
        ("frozen_objects", list),
        ("frozen_timeline", dict),
        ("frozen_shots", list),
        ("frozen_ranges", list),
    ):
        if not isinstance(ground_truth.get(key), expected_type):
            raise FrozenIntegrityError(
                f"ground-truth.json is missing required R16 lineage facts: {key}"
            )
    if ground_truth.get("snapshots") != snapshots:
        raise FrozenIntegrityError("ground-truth snapshot block disagrees with manifest")
    for name in ("action-index", "asset-index", "transcript-index", "diagnostics", "view-map"):
        if core[name].get("snapshots") != snapshots:
            raise FrozenIntegrityError(f"{name} snapshot block disagrees with manifest")
    view_map = core["view-map"]
    if manifest.get("reading_order") != view_map.get("reading_order"):
        raise FrozenIntegrityError("manifest and view-map reading order disagree")
    pages = view_map.get("pages")
    if not isinstance(pages, list) or manifest.get("page_count") != len(pages):
        raise FrozenIntegrityError("manifest page_count disagrees with view-map")

    identity_map = _reconstruct_identity_map(ground_truth, snapshot)
    _verify_model_refs(ground_truth, identity_map)
    _verify_action_refs(action_index, identity_map)

    asset_index = core["asset-index"]
    for asset in asset_index.get("assets", []):
        ref = asset.get("qualified_ref") if isinstance(asset, dict) else None
        identity = identity_map.lookup_display(ref) if isinstance(ref, str) else None
        if (
            identity is None
            or identity[1] != "asset"
            or asset.get("canonical_ref") != _canonical_ref(identity)
        ):
            raise FrozenIntegrityError(
                f"asset-index ref does not resolve through ground truth: {ref!r}"
            )

    transcript_index = core["transcript-index"]
    source_refs: set[str] = set()
    for source in transcript_index.get("sources", []):
        ref = source.get("qualified_ref") if isinstance(source, dict) else None
        identity = identity_map.lookup_display(ref) if isinstance(ref, str) else None
        asset_identity = (
            identity_map.lookup_display(source.get("asset_ref"))
            if isinstance(source, dict)
            else None
        )
        if (
            identity is None
            or identity[1] != "transcript_source_segment"
            or source.get("canonical_ref") != _canonical_ref(identity)
            or asset_identity is None
            or asset_identity[1] != "asset"
        ):
            raise FrozenIntegrityError(f"transcript source ref does not resolve: {ref!r}")
        source_refs.add(ref)
    for occurrence in transcript_index.get("speech_occurrences", []):
        ref = occurrence.get("qualified_ref") if isinstance(occurrence, dict) else None
        identity = identity_map.lookup_display(ref) if isinstance(ref, str) else None
        clip_identity = (
            identity_map.lookup_display(occurrence.get("clip_ref"))
            if isinstance(occurrence, dict)
            else None
        )
        asset_identity = (
            identity_map.lookup_display(occurrence.get("asset_ref"))
            if isinstance(occurrence, dict)
            else None
        )
        if (
            identity is None
            or identity[1] != "speech_occurrence"
            or occurrence.get("canonical_ref") != _canonical_ref(identity)
            or occurrence.get("source_ref") not in source_refs
            or clip_identity is None
            or clip_identity[1] != "clip"
            or asset_identity is None
            or asset_identity[1] != "asset"
        ):
            raise FrozenIntegrityError(f"speech occurrence ref does not resolve: {ref!r}")

    timeline = snapshot["timeline"]
    timeline_uuid = timeline["uuid"]
    timeline_ulid = timeline["ulid"]

    frozen = FrozenView(
        pack_root=pack_root,
        manifest=manifest,
        ground_truth=ground_truth,
        action_index=action_index,
        identity_map=identity_map,
        snapshot_sns=snapshot_sns,
        timeline_uuid=timeline_uuid,
        timeline_ulid=timeline_ulid,
        asset_index=asset_index,
        transcript_index=transcript_index,
        diagnostics=core["diagnostics"],
        source_manifest=source_manifest,
    )
    _verify_deterministic_identity_map(frozen)

    # 5. The pack must be owned by the exact completed project run.
    if managed_output:
        inputs = manifest.get("inputs")
        resolved_project = inputs.get("resolved_project") if isinstance(inputs, dict) else None
        if not isinstance(resolved_project, dict) or resolved_project.get("slug") != project.name:
            raise ContainmentError(
                "managed visualization identity disagrees with its owning project"
            )
    else:
        _verify_run_ownership(resolved_manifest, project, manifest, timeline_ulid)
    return frozen


def load_frozen_view(manifest_path: Path, *, project_root: Path) -> FrozenView:
    """Load a view and reclaim any managed reconstruction when verification fails."""

    owner = threading.get_ident()
    with _REHYDRATED_PACKS_LOCK:
        before = {
            root
            for root, ownership in _REHYDRATED_PACKS.items()
            if ownership[0] == os.getpid() and ownership[1] == owner
        }
    try:
        return _load_frozen_view_impl(manifest_path, project_root=project_root)
    except Exception:
        with _REHYDRATED_PACKS_LOCK:
            after = {
                root
                for root, ownership in _REHYDRATED_PACKS.items()
                if ownership[0] == os.getpid() and ownership[1] == owner
            }
        for root in after - before:
            _discard_rehydrated_root(root)
        raise


def _frozen_timeline(frozen: FrozenView) -> dict:
    value = frozen.ground_truth.get("frozen_timeline")
    if isinstance(value, dict):
        return value
    raise FrozenIntegrityError("ground truth does not contain a reconstructable frozen timeline")


def _admitted_snapshot_metadata(frozen: FrozenView) -> Mapping[str, Any] | None:
    """Find the native snapshot metadata without deriving it from frozen rows."""

    candidates: list[Any] = [getattr(frozen, "metadata", None)]
    ground_truth = getattr(frozen, "ground_truth", None)
    if isinstance(ground_truth, Mapping):
        candidates.append(ground_truth.get("metadata"))
        timeline = ground_truth.get("frozen_timeline")
        if isinstance(timeline, Mapping):
            candidates.append(timeline.get("metadata"))
    for candidate in candidates:
        if isinstance(candidate, Mapping) and (
            "element_resources" in candidate or "element_resource_binding_hash" in candidate
        ):
            return candidate
    return None


def _interval_seconds(value: object, *, label: str) -> IntervalSeconds | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise FrozenIntegrityError(f"{label} must be an object or null")
    return IntervalSeconds(float(value["start_seconds"]), float(value["end_seconds"]))


def _interval_frames(value: object, *, fps: int, label: str) -> IntervalFrames | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise FrozenIntegrityError(f"{label} must be an object or null")
    return IntervalFrames(int(value["start_frame"]), int(value["end_frame"]), fps)


def model_from_frozen(frozen: FrozenView) -> TimelineInspectionModel:
    """Reconstruct the normalized inspection model from hashed pack facts."""

    timeline = _frozen_timeline(frozen)
    snapshot = frozen.manifest["snapshots"][0]
    fps = int(snapshot["fps"])
    tracks: list[TrackModel] = []
    track_kinds: dict[str, str] = {}
    track_muted: dict[str, bool] = {}
    for row in timeline.get("tracks", []):
        track_id = row["authored_id"]
        kind = row["kind"]
        paint = row["paint_order"] if row["paint_order"] is not None else row["config_order"]
        tracks.append(
            TrackModel(
                track_id=track_id,
                kind=kind,
                config_order=int(row["config_order"]),
                paint_index=int(paint),
                label=row["label"],
            )
        )
        track_kinds[track_id] = kind
        track_muted[track_id] = bool(row["muted"])

    clips: list[ClipModel] = []
    for row in timeline.get("clips", []):
        identity = frozen.identity_map.lookup_display(row["qualified_ref"])
        if identity is None or identity[1] != "clip":
            raise FrozenIntegrityError(f"cannot reconstruct clip {row.get('qualified_ref')!r}")
        source_bounds = row["source_bounds"]
        authored = IntervalSeconds(
            float(row["at_seconds"]),
            float(row["at_seconds"]) + float(source_bounds["duration_seconds"]),
        )
        frames = IntervalFrames(int(row["start_frame"]), int(row["end_frame"]), fps)
        mounted = _interval_frames(row["mounted_interval"], fps=fps, label="mounted_interval")
        if mounted is None:  # schema plus preflight make this unreachable
            raise FrozenIntegrityError("mounted_interval must not be null")
        transition_document = row.get("transition")
        transition: dict[str, Any] | None = None
        effective = frames.as_seconds()
        if isinstance(transition_document, dict):
            transition = {"id": transition_document["id"]}
            if transition_document.get("requested_duration_frames") is not None:
                transition["durationFrames"] = transition_document["requested_duration_frames"]
            if transition_document.get("requested_duration_seconds") is not None:
                transition["duration"] = transition_document["requested_duration_seconds"]
            interval = transition_document.get("effective_interval")
            if isinstance(interval, dict):
                effective = IntervalSeconds(
                    float(interval["start_seconds"]),
                    float(interval["end_seconds"]),
                )
        asset_keys: list[str] = []
        for asset_ref in row.get("asset_refs", []):
            asset_identity = frozen.identity_map.lookup_display(asset_ref)
            if asset_identity is None or asset_identity[1] != "asset":
                raise FrozenIntegrityError(f"cannot reconstruct asset ref {asset_ref!r}")
            asset_keys.append(asset_identity[2])
        source: dict[str, Any] = {}
        if source_bounds.get("from_seconds") is not None:
            source["from"] = source_bounds["from_seconds"]
        if source_bounds.get("to_seconds") is not None:
            source["to"] = source_bounds["to_seconds"]
        clips.append(
            ClipModel(
                clip_id=identity[2],
                track_id=row["track_authored_id"],
                authored=authored,
                frames=frames,
                effective=effective,
                speed=float(row["speed"]),
                transition=transition,
                source=source or None,
                kind=row["clip_type"],
                asset_keys=tuple(asset_keys),
                mounted=mounted,
                authored_text=row.get("authored_text"),
                pixel_text_state=row.get("pixel_text") or "not_inspected",
            )
        )

    shots: list[ShotModel] = []
    for row in frozen.ground_truth.get("frozen_shots", []):
        identity = frozen.identity_map.lookup_display(row["qualified_ref"])
        if identity is None or identity[1] != "shot":
            raise FrozenIntegrityError(f"cannot reconstruct shot {row.get('qualified_ref')!r}")
        shots.append(
            ShotModel(
                shot_id=identity[2],
                member_clip_ids=tuple(row["member_clip_ids"]),
                authored=_interval_seconds(row["authored_interval"], label="authored_interval"),
                frames=_interval_frames(row["frame_interval"], fps=fps, label="frame_interval"),
                warnings=tuple(row["warnings"]),
            )
        )

    media_integrity: dict[str, AssetIntegrity] = {}
    for row in frozen.asset_index.get("assets", []):
        identity = frozen.identity_map.lookup_display(row["qualified_ref"])
        if identity is None or identity[1] != "asset":
            raise FrozenIntegrityError(f"cannot reconstruct asset {row.get('qualified_ref')!r}")
        state = row["integrity_state"]
        media_integrity[identity[2]] = AssetIntegrity(
            asset_key=identity[2],
            role=row["role"],
            state=state,
            expected_sha256=row["expected_sha256"],
            observed_sha256=row["observed_sha256"],
            reason=f"frozen asset integrity state: {state}",
            source_id=row["source_id"],
            source_version=row["source_version"],
        )
    frozen_asset_ids = {
        frozen.identity_map.lookup_display(row["qualified_ref"])[2]
        for row in timeline.get("assets", [])
    }
    if set(media_integrity) != frozen_asset_ids:
        raise FrozenIntegrityError("asset-index and frozen timeline asset sets disagree")

    durations = timeline["durations"]
    composition = durations["all_track_composition"]
    visual = durations["frame_quantized_visual_end"]
    audible_frames = max(
        (clip.frames.end_frame for clip in clips if track_kinds.get(clip.track_id) == "audio"),
        default=0,
    )
    extents = ModelExtents(
        composition_frames=int(composition["frames"]),
        composition_seconds=float(composition["seconds"]),
        visual_frames=int(visual["frames"]),
        visual_seconds=float(visual["seconds"]),
        audible_frames=audible_frames,
        fps=fps,
    )
    return TimelineInspectionModel(
        timeline_uuid=frozen.timeline_uuid,
        timeline_ulid=frozen.timeline_ulid,
        slug=snapshot["timeline"]["slug"],
        fps=fps,
        tracks=tuple(tracks),
        clips=tuple(clips),
        extents=extents,
        compositor_version=frozen.manifest["compositor"]["version"],
        transition_default_frames=TRANSITION_FALLBACK_FRAMES,
        registry_keys=frozenset(media_integrity),
        media_integrity=media_integrity,
        snapshot_sns=frozen.snapshot_sns,
        shots=tuple(shots),
    )


def _verify_deterministic_identity_map(frozen: FrozenView) -> None:
    """Re-run the canonical allocator and require byte-order-equivalent IDs."""

    model = model_from_frozen(frozen)
    rebuilt = build_identity_map(
        model,
        root_sns=frozen.snapshot_sns,
        timeline_uuid=frozen.timeline_uuid,
        timeline_ulid=frozen.timeline_ulid,
    )
    ranges: list[tuple[str, float, float]] = []
    for row in frozen.ground_truth.get("frozen_ranges", []):
        canonical = row["canonical_ref"]
        ranges.append(
            (
                canonical["authored_id"],
                float(row["start_seconds"]),
                float(row["end_seconds"]),
            )
        )
    if ranges:
        rebuilt = assign_range_ids(rebuilt, ranges)
    transcript = frozen.transcript_index
    segment_rows = transcript.get("sources", [])
    occurrence_rows = transcript.get("speech_occurrences", [])
    if segment_rows:
        timeline = frozen.ground_truth.get("frozen_timeline", {})
        attachment = timeline.get("transcript_attachment", {}) if isinstance(timeline, dict) else {}
        transcript_hash = attachment.get("transcript_sha256")
        if not isinstance(transcript_hash, str):
            raise FrozenIntegrityError("frozen transcript identities lack transcript hash scope")
        segments = [
            TranscriptSegment(
                row["source_segment_id"],
                float(row["source_interval"]["start_seconds"]),
                float(row["source_interval"]["end_seconds"]),
                row["text"],
                row["speaker"],
                None,
                row["speaker_state"],
            )
            for row in segment_rows
        ]
        occurrences: list[SpeechOccurrence] = []
        for row in occurrence_rows:
            source = next(
                item for item in segment_rows if item["qualified_ref"] == row["source_ref"]
            )
            clip_identity = frozen.identity_map.lookup_display(row["clip_ref"])
            if clip_identity is None:
                raise FrozenIntegrityError("frozen speech occurrence clip ref is unresolved")
            interval = row["authored_mapping"]["interval"]
            if interval is None:
                continue
            occurrences.append(
                SpeechOccurrence(
                    row["qualified_ref"],
                    source["source_segment_id"],
                    clip_identity[2],
                    float(interval["start_seconds"]),
                    float(interval["end_seconds"]),
                    0.0,
                    float(interval["end_seconds"]) - float(interval["start_seconds"]),
                )
            )
        rebuilt = assign_transcript_ids(
            rebuilt,
            segments,
            occurrences,
            transcript_sha256=transcript_hash,
        )

    actual_semantic = dict(frozen.identity_map.semantic_to_display)
    expected_semantic = dict(rebuilt.semantic_to_display)
    actual_order = [row["qualified_ref"] for row in frozen.ground_truth["frozen_objects"]]
    expected_order = list(rebuilt.display_to_semantic)
    if actual_semantic != expected_semantic or actual_order != expected_order:
        raise FrozenIntegrityError(
            "frozen identity ordinals/order disagree with deterministic root allocation"
        )


def snapshot_from_frozen(frozen: FrozenView, model: TimelineInspectionModel) -> TimelineSnapshot:
    """Build an in-memory emitter adapter without touching timeline files."""

    frozen_timeline = _frozen_timeline(frozen)
    snapshot_metadata = _admitted_snapshot_metadata(frozen)
    from astrid.packs.rendering.actions.timeline_visualize.motion.element_motion import (
        admitted_element_bindings,
    )

    admitted_bindings = admitted_element_bindings(snapshot_metadata)
    track_rows = frozen_timeline["tracks"]
    muted = {row["authored_id"]: row["muted"] for row in track_rows}
    clip_rows: dict[str, dict] = {}
    for row in frozen_timeline["clips"]:
        identity = frozen.identity_map.lookup_display(row["qualified_ref"])
        if identity is not None:
            clip_rows[identity[2]] = row
    assembly_tracks = [
        {
            "id": track.track_id,
            "kind": track.kind,
            "label": track.label or "",
            "muted": bool(muted.get(track.track_id, False)),
        }
        for track in model.tracks
    ]
    assembly_clips: list[dict[str, Any]] = []
    for clip in model.clips:
        frozen_row = clip_rows[clip.clip_id]
        source_bounds = frozen_row["source_bounds"]
        raw: dict[str, Any] = {
            "id": clip.clip_id,
            "track": clip.track_id,
            "at": clip.authored.start,
            "clipType": clip.kind,
            "speed": clip.speed,
        }
        for frozen_key, raw_key in (
            ("from_seconds", "from"),
            ("to_seconds", "to"),
            ("hold_seconds", "hold"),
        ):
            value = source_bounds.get(frozen_key)
            if value is not None:
                raw[raw_key] = value
        # Legacy clips can carry only a resolved duration.  A synthetic hold is
        # then an emitter adapter, never a re-resolution of source state.
        if not any(key in raw for key in ("from", "to", "hold")):
            raw["hold"] = source_bounds["duration_seconds"]
        if clip.transition is not None:
            raw["transition"] = dict(clip.transition)
        if clip.authored_text is not None:
            raw["text"] = {"content": clip.authored_text}
        if len(clip.asset_keys) == 1:
            raw["asset"] = clip.asset_keys[0]
        # Preserve only the exact native-admitted V3 element projection when
        # the snapshot owner has supplied it.  Do not derive a pack, resource,
        # or descriptor from clipType/basename; absent rows intentionally leave
        # motion discovery on its truthful mirror fallback.
        for admitted_key in ("elementRef", "elementDescriptor", "elementResources"):
            admitted_value = frozen_row.get(admitted_key)
            if isinstance(admitted_value, Mapping) or (
                admitted_key == "elementResources" and isinstance(admitted_value, list)
            ):
                raw[admitted_key] = deepcopy(admitted_value)
        binding = admitted_bindings.get(clip.clip_id)
        if binding is not None:
            # The producer metadata is the admitted authority; a stale/raw
            # row cannot override its immutable clip-id join.
            raw["elementRef"] = deepcopy(binding["element_ref"])
            raw["elementDescriptor"] = deepcopy(binding["declaration"])
            raw["elementResources"] = deepcopy(binding["declaration"].get("resources", []))
        assembly_clips.append(raw)
    assembly = {
        "theme_overrides": {"visual": {"canvas": {"fps": model.fps}}},
        "tracks": assembly_tracks,
        "clips": assembly_clips,
        "pinnedShotGroups": [
            {"shotId": shot.shot_id, "clipIds": list(shot.member_clip_ids)} for shot in model.shots
        ],
    }
    registry_assets: dict[str, dict[str, Any]] = {}
    frozen_media_types: dict[str, str] = {}
    for row in frozen.asset_index.get("assets", []):
        identity = frozen.identity_map.lookup_display(row.get("qualified_ref"))
        media_type = row.get("media_type")
        if (
            identity is not None
            and identity[1] == "asset"
            and media_type in {"image", "video", "audio"}
        ):
            frozen_media_types[identity[2]] = media_type
    for key, integrity in model.media_integrity.items():
        entry: dict[str, Any] = {"role": integrity.role}
        if integrity.expected_sha256 is not None:
            entry["content_sha256"] = integrity.expected_sha256
        if integrity.source_id is not None:
            entry["sourceId"] = integrity.source_id
        if integrity.source_version is not None:
            entry["sourceVersion"] = integrity.source_version
        if key in frozen_media_types:
            entry["type"] = frozen_media_types[key]
        registry_assets[key] = entry
    head = frozen.manifest["snapshots"][0]["event_head"]
    return TimelineSnapshot(
        timeline_id=frozen.timeline_uuid,
        timeline_ulid=frozen.timeline_ulid,
        slug=model.slug,
        project_slug=frozen.ground_truth["project_slug"],
        head_version=int(head["version"]),
        last_event_id=head["last_event_id"],
        last_hash=head["last_hash"],
        assembly=assembly,
        registry={"assets": registry_assets},
        display=(
            {"metadata": deepcopy(dict(snapshot_metadata))}
            if isinstance(snapshot_metadata, Mapping)
            else None
        ),
        events=[],
        media_hashes={},
        assembly_sha256="0" * 64,
        registry_sha256="0" * 64,
        transcript_sha256=(
            frozen.ground_truth.get("frozen_timeline", {})
            .get("transcript_attachment", {})
            .get("transcript_sha256")
        ),
        diagnostics=(),
    )


def _timestamp_seconds(raw: str) -> float:
    parts = raw.split(":")
    if len(parts) == 2:
        hours = 0
        minutes, seconds = parts
    elif len(parts) == 3:
        hours, minutes, seconds = parts
    else:  # parse_qualified_ref already guards this; retain a closed boundary
        raise FocusResolutionError(f"malformed timestamp locator: {raw!r}")
    return int(hours) * 3600.0 + int(minutes) * 60.0 + float(seconds)


def resolve_focus(
    frozen: FrozenView,
    focus_ref: str,
    *,
    context_seconds: float = 3.0,
    neighbors: int | None = None,
) -> Scope:
    """Resolve a focus exclusively through the frozen ID map and truth model."""

    try:
        parsed = parse_qualified_ref(focus_ref)
    except ValueError as exc:
        raise FocusResolutionError(str(exc)) from exc
    timeline_identity = frozen.identity_map.lookup_display(parsed.timeline_id)
    if timeline_identity is None or timeline_identity[1] != "timeline":
        raise FocusResolutionError(
            f"timeline display id {parsed.timeline_id!r} is not in this snapshot"
        )
    if neighbors is not None and (
        isinstance(neighbors, bool) or not isinstance(neighbors, int) or neighbors < 0
    ):
        raise FocusResolutionError("neighbors must be a non-negative integer")
    if isinstance(context_seconds, bool) or not isinstance(context_seconds, (int, float)):
        raise FocusResolutionError("context_seconds must be a number")
    context = float(context_seconds)
    if not math.isfinite(context) or context < 0:
        raise FocusResolutionError("context_seconds must be finite and non-negative")

    model = model_from_frozen(frozen)
    if parsed.kind == "timestamp":
        if neighbors:
            raise FocusResolutionError("neighbors applies only to clip focus")
        return select_scope(
            model,
            kind="timestamp",
            ref=str(parsed),
            at_seconds=_timestamp_seconds(parsed.timestamp or ""),
            context_seconds=context,
            neighbors=0,
        )
    if parsed.kind == "TL":
        if neighbors:
            raise FocusResolutionError("neighbors applies only to clip focus")
        return select_scope(model, kind="timeline", ref=str(parsed), context_seconds=0, neighbors=0)
    if parsed.kind in {"TS", "SP"}:
        identity = frozen.identity_map.lookup_display(str(parsed))
        expected = "transcript_source_segment" if parsed.kind == "TS" else "speech_occurrence"
        if identity is None or identity[1] != expected:
            noun = "transcript segment" if parsed.kind == "TS" else "mapped speech occurrence"
            raise FocusResolutionError(f"{noun} {focus_ref!r} is not available in this snapshot")
        rows = frozen.transcript_index.get("speech_occurrences", [])
        if parsed.kind == "SP":
            rows = [row for row in rows if row.get("qualified_ref") == str(parsed)]
        else:
            rows = [row for row in rows if row.get("source_ref") == str(parsed)]
        clip_ids: list[str] = []
        for row in rows:
            clip_identity = frozen.identity_map.lookup_display(row.get("clip_ref"))
            if clip_identity is not None and clip_identity[2] not in clip_ids:
                clip_ids.append(clip_identity[2])
        if not clip_ids:
            return Scope(
                "text" if parsed.kind == "TS" else "speech",
                str(parsed),
                None,
                None,
                (),
                (),
                0,
                ("transcript evidence has no timeline occurrence",),
            )
        selected = [clip for clip in model.clips if clip.clip_id in set(clip_ids)]
        context_frames = int(math.floor(context * model.fps + 0.5))
        start = max(0, min(clip.mounted.start_frame for clip in selected) - context_frames)
        end = min(
            model.extents.composition_frames,
            max(clip.mounted.end_frame for clip in selected) + context_frames,
        )
        return Scope(
            "text" if parsed.kind == "TS" else "speech",
            str(parsed),
            start,
            end,
            tuple(clip.clip_id for clip in model.clips if clip.clip_id in set(clip_ids)),
            tuple(clip_ids),
            context_frames,
        )

    identity = frozen.identity_map.lookup_display(str(parsed))
    if identity is None:
        raise FocusResolutionError(
            f"display id {focus_ref!r} is not present in the frozen identity map"
        )
    expected_kind = {"CL": "clip", "SH": "shot", "RG": "range", "AS": "asset"}.get(parsed.kind)
    if identity[1] != expected_kind:
        raise FocusResolutionError(f"display id {focus_ref!r} has the wrong semantic kind")
    if neighbors and parsed.kind != "CL":
        raise FocusResolutionError("neighbors applies only to clip focus")
    authored_id = identity[2]
    if parsed.kind == "CL":
        return select_scope(
            model,
            kind="clip",
            ref=str(parsed),
            clip_id=authored_id,
            context_seconds=context,
            # A bare clip drill-down without same-track neighbors is a weird
            # crop (Grok UX feedback): default to one neighbor each side.
            neighbors=neighbors if neighbors is not None else 1,
        )
    if parsed.kind == "SH":
        return select_scope(model, kind="shot", ref=authored_id, context_seconds=0, neighbors=0)
    if parsed.kind == "AS":
        return select_scope(
            model,
            kind="asset",
            ref=str(parsed),
            asset_key=authored_id,
            context_seconds=0,
            neighbors=0,
        )
    ranges = frozen.ground_truth.get("frozen_ranges", [])
    row = next(
        (
            item
            for item in ranges
            if isinstance(item, dict) and item.get("qualified_ref") == str(parsed)
        ),
        None,
    )
    if row is None:
        raise FocusResolutionError(f"range {focus_ref!r} is not available in this snapshot")
    return select_scope(
        model,
        kind="range",
        ref=authored_id,
        start=float(row["start_seconds"]),
        end=float(row["end_seconds"]),
        context_seconds=0,
        neighbors=0,
    )


__all__ = [
    "ContainmentError",
    "FocusResolutionError",
    "FrozenIntegrityError",
    "FrozenSchemaError",
    "FrozenView",
    "FrozenViewError",
    "load_frozen_view",
    "model_from_frozen",
    "resolve_focus",
    "snapshot_from_frozen",
]
