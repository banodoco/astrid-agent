"""Canonical shot-composition contract and Runtime preparation boundary.

The adapter accepts explicit immutable source records, produces the canonical
wire graph shared with Astrid's editor, and delegates persistence to an
injected Runtime resolution/CAS port. It never translates legacy
``pinnedShotGroups`` or ``clipType: "shot"`` data.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import asdict, dataclass, is_dataclass
from typing import Any, Protocol, Sequence, runtime_checkable
from urllib.parse import quote

SCHEMA_VERSION = 1
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


class StaleWriteError(RuntimeError):
    status = 409
    code = "stale_write"


class ShotCompositionValidationError(ValueError):
    pass


class MissingDependencyError(ShotCompositionValidationError):
    """A referenced immutable shot revision was not present in the graph."""

    status = 422
    code = "missing_dependency"

    def __init__(self, shot_id: str, revision_id: str, *, path: str = "dependencies") -> None:
        self.shot_id = shot_id
        self.revision_id = revision_id
        super().__init__(
            f"{path}: missing dependency revision {shot_id!r}/{revision_id!r}"
        )


@dataclass(frozen=True)
class ProjectRecord:
    """The project/document identity that owns the primary timeline."""

    project_id: str
    document_id: str
    role: str = "project"


@dataclass(frozen=True)
class PrimaryTimelineHeadRecord:
    """The immutable primary-timeline head used for Runtime CAS publication."""

    revision_id: str
    content_digest: str


@dataclass(frozen=True)
class InternalTimelineRevisionRecord:
    revision_id: str
    content_digest: str
    timeline: Mapping[str, Any]


@dataclass(frozen=True)
class DependencyRecord:
    shot_id: str
    revision_id: str
    required: bool = True


@dataclass(frozen=True)
class AssetRecord:
    asset_id: str
    object_id: str
    digest: str
    scope: Mapping[str, Any]
    role: str


@dataclass(frozen=True)
class GenerationInputRecord:
    input_id: str
    object_id: str
    digest: str
    ordinal: int
    role: str


@dataclass(frozen=True)
class AudioRecord:
    track_id: str
    object_id: str
    digest: str
    scope: Mapping[str, Any]


@dataclass(frozen=True)
class TimingRecord:
    duration_ms: int


@dataclass(frozen=True)
class ShotRevisionRecord:
    """A complete immutable shot revision and its referenced source records."""

    shot_id: str
    revision_id: str
    content_digest: str
    internal_timeline_revision: InternalTimelineRevisionRecord | Mapping[str, Any]
    audio: AudioRecord | Mapping[str, Any]
    timing: TimingRecord | Mapping[str, Any]
    provenance: Mapping[str, Any]
    document_role: str = "shot_revision"
    dependencies: Sequence[DependencyRecord | Mapping[str, Any]] = ()
    assets: Sequence[AssetRecord | Mapping[str, Any]] = ()
    generation_inputs: Sequence[GenerationInputRecord | Mapping[str, Any]] = ()


@dataclass(frozen=True)
class CompositionOccurrenceRecord:
    """One placement identity; multiple records may link to one shot revision."""

    occurrence_id: str
    parent_document_id: str
    shot_id: str
    revision_id: str
    ordinal: int
    at_ms: int
    duration_ms: int


@dataclass(frozen=True)
class ShotCompositionSource:
    """Typed source records accepted by :func:`prepare_shot_composition`."""

    project: ProjectRecord | Mapping[str, Any]
    primary_timeline_head: PrimaryTimelineHeadRecord | Mapping[str, Any]
    shot_revisions: Sequence[ShotRevisionRecord | Mapping[str, Any]]
    occurrences: Sequence[CompositionOccurrenceRecord | Mapping[str, Any]]
    cases: Mapping[str, Any] | None = None


@runtime_checkable
class RuntimeShotCompositionWriter(Protocol):
    """Runtime port for immutable revision resolution and primary-timeline CAS."""

    def resolve_immutable_revision(
        self,
        *,
        project_id: str,
        document_id: str,
        shot_id: str,
        revision_id: str,
    ) -> Any:
        """Return a present immutable revision handle, or ``None`` if absent."""

    def publish_primary_timeline_revision(
        self,
        *,
        project_id: str,
        document_id: str,
        expected_head_revision_id: str | None,
        graph: Mapping[str, Any],
    ) -> Any:
        """Atomically publish the complete graph with a Runtime CAS precondition."""


def _fail(path: str, message: str) -> None:
    raise ShotCompositionValidationError(f"{path}: {message}")


def _mapping(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _fail(path, "must be an object")
    return value


def _string(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value:
        _fail(path, "must be a non-empty string")
    return value


def _int(value: Any, path: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        _fail(path, "must be a non-negative integer")
    return value


def _digest(value: Any, path: str) -> str:
    value = _string(value, path)
    if not _DIGEST.fullmatch(value):
        _fail(path, "must be a sha256 digest")
    return value


def _copy_record(value: Any, path: str) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return deepcopy(dict(value))
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    _fail(path, "must be a canonical record object")


def _copy_records(values: Sequence[Any], path: str) -> list[dict[str, Any]]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        _fail(path, "must be a list of canonical records")
    return [_copy_record(value, f"{path}[{index}]") for index, value in enumerate(values)]


def _default_cases(head_revision_id: str) -> dict[str, Any]:
    # ``cases`` is retained as fixture metadata for Python/TypeScript parity.
    # It is never used to resolve or publish Runtime state.
    return {
        "missing_dependency": {
            "shot_id": "__missing__",
            "revision_id": "__missing__",
            "expected": "missing_dependency",
        },
        "stale_write_rejection": {
            "expected_head_revision_id": head_revision_id,
            "submitted_head_revision_id": head_revision_id,
            "expected_status": 409,
        },
    }


def _source_to_graph(
    source: ShotCompositionSource | Mapping[str, Any] | None,
    *,
    project: ProjectRecord | Mapping[str, Any] | None,
    primary_timeline_head: PrimaryTimelineHeadRecord | Mapping[str, Any] | None,
    shot_revisions: Sequence[ShotRevisionRecord | Mapping[str, Any]] | None,
    occurrences: Sequence[CompositionOccurrenceRecord | Mapping[str, Any]] | None,
    cases: Mapping[str, Any] | None,
) -> dict[str, Any]:
    explicit = any(
        value is not None
        for value in (project, primary_timeline_head, shot_revisions, occurrences, cases)
    )
    if source is not None and explicit:
        _fail("source", "use either source or explicit source records, not both")
    if source is None:
        if project is None or primary_timeline_head is None or shot_revisions is None or occurrences is None:
            _fail(
                "source",
                "project, primary_timeline_head, shot_revisions, and occurrences are required",
            )
        project_mapping = _copy_record(project, "project")
        head_mapping = _copy_record(primary_timeline_head, "primary_timeline_head")
        graph = {
            "schema_version": SCHEMA_VERSION,
            "project": project_mapping,
            "primary_timeline": {
                "document_id": project_mapping.get("document_id"),
                "role": "primary_timeline",
                "head": head_mapping,
            },
            "shot_revisions": _copy_records(shot_revisions, "shot_revisions"),
            "occurrences": _copy_records(occurrences, "occurrences"),
        }
        if cases is not None:
            graph["cases"] = deepcopy(dict(cases))
    elif isinstance(source, ShotCompositionSource):
        graph = {
            "schema_version": SCHEMA_VERSION,
            "project": _copy_record(source.project, "project"),
            "primary_timeline": {
                "document_id": _copy_record(source.project, "project").get("document_id"),
                "role": "primary_timeline",
                "head": _copy_record(source.primary_timeline_head, "primary_timeline_head"),
            },
            "shot_revisions": _copy_records(source.shot_revisions, "shot_revisions"),
            "occurrences": _copy_records(source.occurrences, "occurrences"),
        }
        if source.cases is not None:
            graph["cases"] = deepcopy(dict(source.cases))
    else:
        graph = _copy_record(source, "source")

    _walk_for_legacy(graph)
    if "primary_timeline" not in graph and "primary_timeline_head" in graph:
        project_mapping = _mapping(graph.get("project"), "project")
        graph["primary_timeline"] = {
            "document_id": project_mapping.get("document_id"),
            "role": "primary_timeline",
            "head": graph.pop("primary_timeline_head"),
        }
    if "cases" not in graph:
        primary = _mapping(graph.get("primary_timeline"), "primary_timeline")
        head = _mapping(primary.get("head"), "primary_timeline.head")
        graph["cases"] = _default_cases(_string(head.get("revision_id"), "primary_timeline.head.revision_id"))

    raw_revisions = graph.get("shot_revisions")
    if isinstance(raw_revisions, list):
        normalized_revisions: list[dict[str, Any]] = []
        for index, raw_revision in enumerate(raw_revisions):
            revision = dict(_mapping(raw_revision, f"shot_revisions[{index}]"))
            for field in ("dependencies", "assets", "generation_inputs"):
                values = revision.get(field)
                if isinstance(values, Sequence) and not isinstance(values, (str, bytes)):
                    revision[field] = [deepcopy(value) for value in values]
            normalized_revisions.append(revision)
        graph["shot_revisions"] = normalized_revisions

    project_mapping = _mapping(graph.get("project"), "project")
    project_id = _string(project_mapping.get("project_id"), "project.project_id")
    document_id = _string(project_mapping.get("document_id"), "project.document_id")
    raw_occurrences = graph.get("occurrences")
    if not isinstance(raw_occurrences, list):
        _fail("occurrences", "must be a list")
    normalized_occurrences: list[dict[str, Any]] = []
    for index, raw_occurrence in enumerate(raw_occurrences):
        occurrence = _mapping(raw_occurrence, f"occurrences[{index}]")
        normalized = dict(occurrence)
        shot_id = _string(occurrence.get("shot_id"), f"occurrences[{index}].shot_id")
        revision_id = _string(occurrence.get("revision_id"), f"occurrences[{index}].revision_id")
        occurrence_id = _string(
            occurrence.get("occurrence_id"), f"occurrences[{index}].occurrence_id"
        )
        normalized.setdefault(
            "stable_deep_link",
            stable_occurrence_deep_link(project_id, document_id, shot_id, revision_id, occurrence_id),
        )
        normalized.setdefault(
            "output_identity",
            stable_output_identity(project_id, document_id, occurrence_id),
        )
        normalized_occurrences.append(normalized)
    graph["occurrences"] = normalized_occurrences
    return graph


def _walk_for_legacy(value: Any, path: str = "contract") -> None:
    if isinstance(value, Mapping):
        if "pinnedShotGroups" in value:
            _fail(path, "pinnedShotGroups is migration-only")
        if value.get("clipType") == "shot":
            _fail(path, "clipType=shot is migration-only")
        for key, child in value.items():
            _walk_for_legacy(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _walk_for_legacy(child, f"{path}[{index}]")


def stable_occurrence_deep_link(
    project_id: str,
    parent_document_id: str,
    shot_id: str,
    revision_id: str,
    occurrence_id: str,
) -> str:
    parts = (project_id, parent_document_id, shot_id, revision_id, occurrence_id)
    # Match JavaScript encodeURIComponent used by Astrid's contract validator.
    encoded = [quote(_string(part, "deep_link"), safe="-_.!~*'()") for part in parts]
    return (
        f"project/{encoded[0]}/document/{encoded[1]}/shot/{encoded[2]}"
        f"/revision/{encoded[3]}/occurrence/{encoded[4]}"
    )


def stable_output_identity(project_id: str, parent_document_id: str, occurrence_id: str) -> str:
    parts = (project_id, parent_document_id, occurrence_id)
    encoded = [quote(_string(part, "output_identity"), safe="-_.!~*'()") for part in parts]
    return f"project/{encoded[0]}/document/{encoded[1]}/occurrence/{encoded[2]}/output/final-video"


def prepare_shot_composition(
    source: ShotCompositionSource | Mapping[str, Any] | None = None,
    *,
    project: ProjectRecord | Mapping[str, Any] | None = None,
    primary_timeline_head: PrimaryTimelineHeadRecord | Mapping[str, Any] | None = None,
    shot_revisions: Sequence[ShotRevisionRecord | Mapping[str, Any]] | None = None,
    occurrences: Sequence[CompositionOccurrenceRecord | Mapping[str, Any]] | None = None,
    cases: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Prepare explicit canonical records for Runtime publication.

    The returned value is a detached, JSON-shaped graph.  It is validated at
    the boundary and contains stable occurrence deep links and occurrence-
    qualified output identities.  No legacy timeline data is read or
    translated here; legacy shapes are rejected by the canonical validator.
    """

    graph = _source_to_graph(
        source,
        project=project,
        primary_timeline_head=primary_timeline_head,
        shot_revisions=shot_revisions,
        occurrences=occurrences,
        cases=cases,
    )
    return validate_shot_composition(graph)


def _raise_stale_if_needed(error: Exception) -> None:
    if isinstance(error, StaleWriteError):
        raise error
    if getattr(error, "status", None) == 409 or getattr(error, "code", None) == "stale_write":
        raise StaleWriteError(str(error)) from error


def _resolution_is_missing(result: Any) -> bool:
    if result is None:
        return True
    if isinstance(result, Mapping):
        if result.get("status") == 404 or result.get("code") in {"not_found", "missing_dependency"}:
            return True
        error = result.get("error")
        if result.get("ok") is False and isinstance(error, Mapping):
            if error.get("code") in {"not_found", "missing_dependency"}:
                return True
    return False


_EXPECTED_HEAD_UNSET = object()


def publish_shot_composition(
    source: ShotCompositionSource | Mapping[str, Any],
    writer: RuntimeShotCompositionWriter,
    *,
    expected_head_revision_id: str | None | object = _EXPECTED_HEAD_UNSET,
) -> Any:
    """Resolve immutable references, then delegate complete-graph CAS to Runtime.

    The writer is the only persistence boundary.  This function performs no
    local head mutation and does not call legacy timeline mutation APIs.
    """

    graph = prepare_shot_composition(source)
    project = graph["project"]
    primary = graph["primary_timeline"]
    head = primary["head"]
    graph_head_revision_id = head["revision_id"]
    if expected_head_revision_id is _EXPECTED_HEAD_UNSET:
        expected = graph_head_revision_id
    elif expected_head_revision_id is None:
        expected = None
    else:
        _string(expected_head_revision_id, "expected_head_revision_id")
        if expected_head_revision_id != graph_head_revision_id:
            raise ShotCompositionValidationError(
                "expected_head_revision_id must match primary_timeline.head.revision_id"
            )
        expected = expected_head_revision_id

    resolved: set[tuple[str, str]] = set()
    for index, revision in enumerate(graph["shot_revisions"]):
        key = (revision["shot_id"], revision["revision_id"])
        if key in resolved:
            continue
        resolved.add(key)
        try:
            result = writer.resolve_immutable_revision(
                project_id=project["project_id"],
                document_id=project["document_id"],
                shot_id=revision["shot_id"],
                revision_id=revision["revision_id"],
            )
        except Exception as error:
            _raise_stale_if_needed(error)
            if isinstance(error, (KeyError, LookupError)):
                raise MissingDependencyError(
                    revision["shot_id"], revision["revision_id"], path=f"shot_revisions[{index}]"
                ) from error
            raise
        if _resolution_is_missing(result):
            raise MissingDependencyError(
                revision["shot_id"], revision["revision_id"], path=f"shot_revisions[{index}]"
            )

    try:
        result = writer.publish_primary_timeline_revision(
            project_id=project["project_id"],
            document_id=project["document_id"],
            expected_head_revision_id=expected,
            graph=deepcopy(graph),
        )
    except Exception as error:
        _raise_stale_if_needed(error)
        raise
    if isinstance(result, Mapping) and result.get("status") == 409:
        raise StaleWriteError("Runtime rejected stale shot-composition head")
    return result


def assert_expected_head(expected_revision_id: str | None, actual_revision_id: str | None) -> None:
    """Apply the local contract guard; this is not Runtime CAS publication."""
    if expected_revision_id != actual_revision_id:
        raise StaleWriteError(
            f"stale shot-composition head: expected {expected_revision_id!r}, "
            f"actual {actual_revision_id!r}"
        )


def validate_shot_composition(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and return a canonical-wire-shaped shot composition mapping."""
    _walk_for_legacy(raw)
    root = _mapping(raw, "contract")
    if root.get("schema_version") != SCHEMA_VERSION:
        _fail("schema_version", f"must be {SCHEMA_VERSION}")

    project = _mapping(root.get("project"), "project")
    project_id = _string(project.get("project_id"), "project.project_id")
    document_id = _string(project.get("document_id"), "project.document_id")
    if project.get("role") != "project":
        _fail("project.role", "must be project")
    primary = _mapping(root.get("primary_timeline"), "primary_timeline")
    if primary.get("role") != "primary_timeline":
        _fail("primary_timeline.role", "must be primary_timeline")
    if primary.get("document_id") != document_id:
        _fail("primary_timeline.document_id", "must match project.document_id")
    head = _mapping(primary.get("head"), "primary_timeline.head")
    head_revision_id = _string(head.get("revision_id"), "primary_timeline.head.revision_id")
    _digest(head.get("content_digest"), "primary_timeline.head.content_digest")

    revisions = root.get("shot_revisions")
    if not isinstance(revisions, list) or not revisions:
        _fail("shot_revisions", "must be a non-empty list")
    revision_keys: set[tuple[str, str]] = set()
    revisions_by_key: dict[tuple[str, str], Mapping[str, Any]] = {}
    for index, raw_revision in enumerate(revisions):
        path = f"shot_revisions[{index}]"
        revision = _mapping(raw_revision, path)
        shot_id = _string(revision.get("shot_id"), f"{path}.shot_id")
        revision_id = _string(revision.get("revision_id"), f"{path}.revision_id")
        key = (shot_id, revision_id)
        if key in revision_keys:
            _fail(path, "duplicate shot/revision identity")
        revision_keys.add(key)
        revisions_by_key[key] = revision
        if revision.get("document_role") != "shot_revision":
            _fail(f"{path}.document_role", "must be shot_revision")
        _digest(revision.get("content_digest"), f"{path}.content_digest")
        internal = _mapping(revision.get("internal_timeline_revision"), f"{path}.internal_timeline_revision")
        _string(internal.get("revision_id"), f"{path}.internal_timeline_revision.revision_id")
        _digest(internal.get("content_digest"), f"{path}.internal_timeline_revision.content_digest")
        timeline = _mapping(internal.get("timeline"), f"{path}.internal_timeline_revision.timeline")
        if not isinstance(timeline.get("tracks"), list) or not isinstance(timeline.get("clips"), list):
            _fail(f"{path}.internal_timeline_revision.timeline", "tracks and clips must be lists")
        timing = _mapping(revision.get("timing"), f"{path}.timing")
        _int(timing.get("duration_ms"), f"{path}.timing.duration_ms")
        audio = _mapping(revision.get("audio"), f"{path}.audio")
        _string(audio.get("track_id"), f"{path}.audio.track_id")
        _string(audio.get("object_id"), f"{path}.audio.object_id")
        _digest(audio.get("digest"), f"{path}.audio.digest")
        audio_scope = _mapping(audio.get("scope"), f"{path}.audio.scope")
        if audio_scope.get("project_id") != project_id:
            _fail(f"{path}.audio.scope.project_id", "must match project.project_id")
        assets = revision.get("assets")
        if not isinstance(assets, list):
            _fail(f"{path}.assets", "must be a list")
        for asset_index, raw_asset in enumerate(assets):
            asset_path = f"{path}.assets[{asset_index}]"
            asset = _mapping(raw_asset, asset_path)
            for field in ("asset_id", "object_id"):
                _string(asset.get(field), f"{asset_path}.{field}")
            _digest(asset.get("digest"), f"{asset_path}.digest")
            _string(asset.get("role"), f"{asset_path}.role")
            scope = _mapping(asset.get("scope"), f"{asset_path}.scope")
            if scope.get("project_id") != project_id:
                _fail(f"{asset_path}.scope.project_id", "must match project.project_id")
        inputs = revision.get("generation_inputs")
        if not isinstance(inputs, list):
            _fail(f"{path}.generation_inputs", "must be a list")
        ordinals: list[int] = []
        for input_index, raw_input in enumerate(inputs):
            input_path = f"{path}.generation_inputs[{input_index}]"
            generation_input = _mapping(raw_input, input_path)
            for field in ("input_id", "object_id", "role"):
                _string(generation_input.get(field), f"{input_path}.{field}")
            _digest(generation_input.get("digest"), f"{input_path}.digest")
            ordinals.append(_int(generation_input.get("ordinal"), f"{input_path}.ordinal"))
        if ordinals != list(range(len(ordinals))):
            _fail(f"{path}.generation_inputs", "ordinals must be contiguous and ordered")
        if not isinstance(revision.get("provenance"), Mapping):
            _fail(f"{path}.provenance", "must be an object")
        dependencies = revision.get("dependencies")
        if not isinstance(dependencies, list):
            _fail(f"{path}.dependencies", "must be a list")
        for dependency_index, raw_dependency in enumerate(dependencies):
            dependency_path = f"{path}.dependencies[{dependency_index}]"
            dependency = _mapping(raw_dependency, dependency_path)
            dep_key = (_string(dependency.get("shot_id"), f"{dependency_path}.shot_id"), _string(dependency.get("revision_id"), f"{dependency_path}.revision_id"))
            if dep_key not in revisions_by_key:
                raise MissingDependencyError(*dep_key, path=dependency_path)
            if not isinstance(dependency.get("required"), bool):
                _fail(f"{dependency_path}.required", "must be boolean")

    occurrences = root.get("occurrences")
    if not isinstance(occurrences, list) or not occurrences:
        _fail("occurrences", "must be a non-empty list")
    occurrence_ids: set[str] = set()
    occurrence_ordinals: list[int] = []
    for index, raw_occurrence in enumerate(occurrences):
        path = f"occurrences[{index}]"
        occurrence = _mapping(raw_occurrence, path)
        occurrence_id = _string(occurrence.get("occurrence_id"), f"{path}.occurrence_id")
        if occurrence_id in occurrence_ids:
            _fail(path, "duplicate occurrence_id")
        occurrence_ids.add(occurrence_id)
        if occurrence.get("parent_document_id") != document_id:
            _fail(f"{path}.parent_document_id", "must match primary timeline")
        shot_id = _string(occurrence.get("shot_id"), f"{path}.shot_id")
        revision_id = _string(occurrence.get("revision_id"), f"{path}.revision_id")
        if (shot_id, revision_id) not in revisions_by_key:
            _fail(path, "occurrence must pin an available shot revision")
        occurrence_ordinals.append(_int(occurrence.get("ordinal"), f"{path}.ordinal"))
        _int(occurrence.get("at_ms"), f"{path}.at_ms")
        _int(occurrence.get("duration_ms"), f"{path}.duration_ms")
        expected_link = stable_occurrence_deep_link(project_id, document_id, shot_id, revision_id, occurrence_id)
        if occurrence.get("stable_deep_link") != expected_link:
            _fail(f"{path}.stable_deep_link", "does not match pinned identity")
        expected_output = stable_output_identity(project_id, document_id, occurrence_id)
        if occurrence.get("output_identity") != expected_output:
            _fail(f"{path}.output_identity", "must be occurrence-qualified")
    if occurrence_ordinals != list(range(len(occurrence_ordinals))):
        _fail("occurrences", "ordinals must be contiguous and ordered")

    cases = _mapping(root.get("cases"), "cases")
    missing = _mapping(cases.get("missing_dependency"), "cases.missing_dependency")
    _string(missing.get("shot_id"), "cases.missing_dependency.shot_id")
    _string(missing.get("revision_id"), "cases.missing_dependency.revision_id")
    if missing.get("expected") != "missing_dependency":
        _fail("cases.missing_dependency.expected", "must be missing_dependency")
    stale = _mapping(cases.get("stale_write_rejection"), "cases.stale_write_rejection")
    _string(stale.get("expected_head_revision_id"), "cases.stale_write_rejection.expected_head_revision_id")
    _string(stale.get("submitted_head_revision_id"), "cases.stale_write_rejection.submitted_head_revision_id")
    if stale.get("expected_status") != 409:
        _fail("cases.stale_write_rejection.expected_status", "must be 409")
    assert_expected_head(head_revision_id, head_revision_id)
    return dict(raw)


def parse_shot_composition(raw: Mapping[str, Any] | str | bytes) -> dict[str, Any]:
    if isinstance(raw, (str, bytes)):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ShotCompositionValidationError("contract: invalid JSON") from exc
    if not isinstance(raw, Mapping):
        _fail("contract", "must be an object")
    return validate_shot_composition(raw)


__all__ = [
    "AssetRecord",
    "AudioRecord",
    "CompositionOccurrenceRecord",
    "DependencyRecord",
    "GenerationInputRecord",
    "InternalTimelineRevisionRecord",
    "MissingDependencyError",
    "PrimaryTimelineHeadRecord",
    "ProjectRecord",
    "RuntimeShotCompositionWriter",
    "SCHEMA_VERSION",
    "ShotCompositionSource",
    "ShotCompositionValidationError",
    "ShotRevisionRecord",
    "StaleWriteError",
    "TimingRecord",
    "assert_expected_head",
    "parse_shot_composition",
    "prepare_shot_composition",
    "publish_shot_composition",
    "stable_occurrence_deep_link",
    "stable_output_identity",
    "validate_shot_composition",
]
