"""Generic human-gate HTTP server — see STAGE.md for the full contract."""


from __future__ import annotations

from astrid.pack_api import AstridError, render_astrid_error
from astrid.pack_api import owned_by_current_user
from astrid.pack_api import guard_canonical_entrypoint

guard_canonical_entrypoint('editorial.human_review')
import argparse  # noqa: E402
import copy  # noqa: E402
import hashlib  # noqa: E402
import json  # noqa: E402
import logging  # noqa: E402
import mimetypes  # noqa: E402
import re  # noqa: E402
import secrets  # noqa: E402
import socket  # noqa: E402
import stat  # noqa: E402
import sys  # noqa: E402
import tempfile  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
import webbrowser  # noqa: E402
import zipfile  # noqa: E402
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Any, Mapping  # noqa: E402
from urllib.parse import parse_qs, unquote, urldefrag, urljoin, urlparse  # noqa: E402

from runtime_protocol import platform  # noqa: E402
from runtime_protocol.dirfd import filesystem as os  # noqa: E402

from astrid.pack_api import StaleStateConflict, apply_experiment_review_save, is_experiment_review_save, load_experiment_review_state
from astrid.pack_api import log_and_swallow  # noqa: E402

try:  # The receiver fails closed for explicit schema bundles if unavailable.
    import jsonschema  # type: ignore
    from referencing import Registry, Resource  # type: ignore
except ImportError:  # pragma: no cover - dependency is part of the runtime
    jsonschema = None
    Registry = Resource = None


_D18_OBJECT_LIMIT = 64 * 1024 * 1024
_ASSET_MEMBER_LIMIT = 4096
_ASSET_EXTRACTED_LIMIT = 64 * 1024 * 1024
_ASSET_MANIFEST_NAME = "human-review-assets.json"
_STATE_SCHEMA_FILES = frozenset(
    {"run-state.schema.json", "review-decision.schema.json", "filter-stats.schema.json"}
)
_STATE_RESULT_FILENAME = "state_result.json"


class HumanReviewInputError(ValueError):
    """Raised when a bounded Human Review input is unsafe or malformed."""


class ReviewStateError(ValueError):
    """Raised when a Human Review state document is invalid."""


class _StateValidator:
    def __init__(self, validator: Any | None = None) -> None:
        self.validator = validator

    def validate(self, state: Any) -> None:
        if self.validator is not None:
            errors = sorted(self.validator.iter_errors(state), key=lambda error: list(error.path))
            if errors:
                error = errors[0]
                path = ".".join(str(part) for part in error.path) or "<root>"
                raise ReviewStateError(f"review state invalid at {path}: {error.message}")
            return
        if not isinstance(state, dict):
            raise ReviewStateError("review state must be a JSON object")
        if state.get("kind") == "experiment_review_state":
            required = {"schema_version", "kind", "experiment_id", "state_version", "updated_at", "draft"}
            if not required <= state.keys() or type(state["schema_version"]) is not int:
                raise ReviewStateError("experiment review state has an invalid shape")
            if type(state["state_version"]) is not int or state["state_version"] < 0:
                raise ReviewStateError("experiment review state_version must be non-negative")
            if not isinstance(state["experiment_id"], str) or not state["experiment_id"]:
                raise ReviewStateError("experiment review experiment_id must be non-empty")
            if not isinstance(state["updated_at"], str) or not state["updated_at"]:
                raise ReviewStateError("experiment review updated_at must be non-empty")
            if not isinstance(state["draft"], dict):
                raise ReviewStateError("experiment review draft must be an object")
            return
        required = {"run_id", "writer_id", "state_version", "created_at", "updated_at", "status"}
        if not required <= state.keys():
            raise ReviewStateError("review state is missing required fields")
        if type(state["state_version"]) is not int or state["state_version"] < 0:
            raise ReviewStateError("review state_version must be a non-negative integer")
        if not all(isinstance(state[key], str) and state[key] for key in ("run_id", "writer_id", "created_at", "updated_at")):
            raise ReviewStateError("review state identity and timestamps must be non-empty strings")
        if state["status"] not in {
            "initializing", "acquiring", "filtering", "preview_ready",
            "captioning", "reviewing", "finalized", "failed",
        }:
            raise ReviewStateError("review state status is invalid")
        for key in ("review_decisions", "filter_stats"):
            if key in state and not isinstance(state[key], dict):
                raise ReviewStateError(f"review state {key} must be an object")


def _encode_state(state: Mapping[str, Any]) -> bytes:
    return (json.dumps(dict(state), ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _validate_state(state: Any, state_validator: _StateValidator | None = None) -> None:
    (state_validator or _StateValidator()).validate(state)


def _read_state(path: Path, state_validator: _StateValidator | None = None) -> dict[str, Any]:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ReviewStateError(f"cannot read review state {path}: {exc}") from exc
    _validate_state(state, state_validator)
    return state


def _commit_state(path: Path, state: Mapping[str, Any]) -> bytes:
    encoded = _encode_state(state)
    _atomic_write(path, encoded)
    return encoded


def make_initial_state(
    *,
    run_id: str,
    writer_id: str,
    config_hash: str | None = None,
    buckets: Mapping[str, int | Mapping[str, Any]] | None = None,
    schema_version_source: str | None = None,
    status: str = "initializing",
    now: str | None = None,
) -> dict[str, Any]:
    timestamp = now or _now_iso()
    state: dict[str, Any] = {
        "run_id": run_id,
        "writer_id": writer_id,
        "state_version": 0,
        "created_at": timestamp,
        "updated_at": timestamp,
        "status": status,
        "processed_source_ids": [],
        "review_decisions": {},
        "filter_stats": {},
        "top_up_rounds": 0,
        "submitted": False,
    }
    if config_hash is not None:
        state["config_hash"] = config_hash
    if schema_version_source is not None:
        if schema_version_source != "deprecated_inferred_v1":
            raise ReviewStateError(f"unsupported schema_version_source: {schema_version_source}")
        state["schema_version_source"] = schema_version_source
    if buckets is not None:
        state["buckets"] = {
            str(name): {
                "target_count": int(value.get("target_count", 0)) if isinstance(value, Mapping) else int(value),
                "accepted": int(value.get("accepted", 0)) if isinstance(value, Mapping) else 0,
                "rejected": int(value.get("rejected", 0)) if isinstance(value, Mapping) else 0,
                "pending": int(value.get("pending", 0)) if isinstance(value, Mapping) else 0,
                **({"item_ids": list(value.get("item_ids", []))} if isinstance(value, Mapping) and "item_ids" in value else {}),
            }
            for name, value in buckets.items()
        }
    _validate_state(state)
    return state


def read_review_state(path: str | Path, state_validator: _StateValidator | None = None) -> dict[str, Any]:
    return _read_state(Path(path), state_validator)


def write_review_state(
    path: str | Path,
    state: Mapping[str, Any],
    *,
    now: str | None = None,
    state_validator: _StateValidator | None = None,
) -> dict[str, Any]:
    next_state = copy.deepcopy(dict(state))
    next_state["state_version"] = int(next_state.get("state_version", 0)) + 1
    next_state["updated_at"] = now or _now_iso()
    _validate_state(next_state, state_validator)
    _commit_state(Path(path), next_state)
    return next_state



def _pick_free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _atomic_write(path: Path, body: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(body)
    os.replace(tmp, path)


def _safe_under(root: Path, candidate: Path) -> bool:
    try:
        candidate.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False
def _archive_relative(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value or value.startswith("/") or "\\" in value or "\x00" in value:
        raise HumanReviewInputError(f"{field} must be a relative POSIX path")
    parts = value.rstrip("/").split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise HumanReviewInputError(f"{field} contains an unsafe path component")
    return "/".join(parts)


def _mount_prefix(value: Any, *, field: str) -> str:
    if (
        not isinstance(value, str)
        or not re.fullmatch(r"/[A-Za-z0-9._~-]+(?:/[A-Za-z0-9._~-]+)*", value)
    ):
        raise HumanReviewInputError(f"{field} must be a valid absolute URL prefix")
    parts = value.split("/")[1:]
    if any(part in {"", ".", ".."} for part in parts):
        raise HumanReviewInputError(f"{field} contains an invalid URL path component")
    return "/" + "/".join(parts)


def _zip_mode(info: zipfile.ZipInfo) -> int:
    return (info.external_attr >> 16) & 0xFFFF


def _read_bounded_zip(path: Path) -> tuple[dict[str, bytes], set[str]]:
    try:
        archive_size = path.stat().st_size
    except OSError as exc:
        raise HumanReviewInputError(f"ZIP input is unreadable: {path}") from exc
    if archive_size > _D18_OBJECT_LIMIT:
        raise HumanReviewInputError("ZIP input exceeds the 64 MiB object limit")
    members: dict[str, bytes] = {}
    directories: set[str] = set()
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            if len(infos) > _ASSET_MEMBER_LIMIT:
                raise HumanReviewInputError("ZIP input exceeds the member-count limit")
            total = 0
            for info in infos:
                name = _archive_relative(info.filename, field="ZIP member")
                if name in members or name in directories:
                    raise HumanReviewInputError(f"ZIP contains duplicate destination {name!r}")
                mode = _zip_mode(info)
                file_type = stat.S_IFMT(mode)
                is_directory = info.is_dir() or file_type == stat.S_IFDIR
                if file_type not in {0, stat.S_IFREG, stat.S_IFDIR}:
                    raise HumanReviewInputError(f"ZIP member {name!r} is a symlink or special file")
                if is_directory:
                    directories.add(name.rstrip("/"))
                    continue
                total += int(info.file_size)
                if total > _ASSET_EXTRACTED_LIMIT:
                    raise HumanReviewInputError("ZIP extracted bytes exceed the 64 MiB limit")
                try:
                    data = archive.read(info)
                except (OSError, RuntimeError, ValueError, zipfile.BadZipFile) as exc:
                    raise HumanReviewInputError(f"cannot read ZIP member {name!r}") from exc
                if len(data) != info.file_size:
                    raise HumanReviewInputError(f"ZIP member {name!r} has an invalid size")
                members[name] = data
    except zipfile.BadZipFile as exc:
        raise HumanReviewInputError(f"invalid ZIP input: {path}") from exc
    return members, directories


def _asset_manifest(members: Mapping[str, bytes], directories: set[str]) -> tuple[str, dict[str, str]]:
    if _ASSET_MANIFEST_NAME not in members:
        raise HumanReviewInputError(f"ZIP must contain root {_ASSET_MANIFEST_NAME}")
    try:
        manifest = json.loads(members[_ASSET_MANIFEST_NAME].decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise HumanReviewInputError("human-review-assets.json must be UTF-8 JSON") from exc
    if not isinstance(manifest, dict) or set(manifest) - {"html_root", "mounts"}:
        raise HumanReviewInputError("human-review-assets.json has an invalid shape")
    html_root = _archive_relative(manifest.get("html_root"), field="html_root")
    raw_mounts = manifest.get("mounts", {})
    if not isinstance(raw_mounts, Mapping):
        raise HumanReviewInputError("human-review-assets.json mounts must be an object")
    mounts: dict[str, str] = {}
    destinations = {html_root}
    for raw_prefix, raw_root in raw_mounts.items():
        prefix = _mount_prefix(raw_prefix, field="mount prefix")
        root = _archive_relative(raw_root, field=f"mount {prefix}")
        if prefix in mounts:
            raise HumanReviewInputError(f"duplicate mount prefix {prefix!r}")
        if root in destinations or any(root.startswith(existing + "/") or existing.startswith(root + "/") for existing in destinations):
            raise HumanReviewInputError(f"duplicate or overlapping archive destination {root!r}")
        mounts[prefix] = root
        destinations.add(root)
    for root in destinations:
        if root not in directories and not any(name.startswith(root + "/") for name in members):
            raise HumanReviewInputError(f"archive directory {root!r} is missing")
    return html_root, mounts


def _extract_assets_bundle(path: Path, scratch_parent: Path) -> tuple[Path, dict[str, Path], tempfile.TemporaryDirectory[str]]:
    members, directories = _read_bounded_zip(path)
    html_root, raw_mounts = _asset_manifest(members, directories)
    scratch = tempfile.TemporaryDirectory(prefix=".human-review-assets-", dir=str(scratch_parent))
    root = Path(scratch.name).resolve()
    roots = [html_root, *raw_mounts.values()]
    selected = {
        name: data
        for name, data in members.items()
        if any(name == archive_root or name.startswith(archive_root + "/") for archive_root in roots)
    }
    try:
        for name, data in selected.items():
            destination = (root / name).resolve()
            if not _safe_under(root, destination):
                raise HumanReviewInputError("asset extraction escaped its scratch root")
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
    except OSError as exc:
        scratch.cleanup()
        raise HumanReviewInputError("asset extraction could not write confined scratch") from exc
    except HumanReviewInputError:
        scratch.cleanup()
        raise
    html_path = root / html_root
    mounts = {prefix: root / archive_root for prefix, archive_root in raw_mounts.items()}
    if not html_path.is_dir():
        scratch.cleanup()
        raise HumanReviewInputError("html_root must name an extracted directory")
    return html_path, mounts, scratch


def _schema_uri(name: str, schema: Mapping[str, Any]) -> str:
    schema_id = schema.get("$id")
    if schema_id is not None:
        if not isinstance(schema_id, str) or not schema_id:
            raise HumanReviewInputError(f"schema {name!r} has an invalid $id")
        return schema_id
    return name


def _walk_schema_refs(value: Any, *, base_uri: str, schemas: Mapping[str, Mapping[str, Any]], uris: Mapping[str, str]) -> None:
    if isinstance(value, list):
        for item in value:
            _walk_schema_refs(item, base_uri=base_uri, schemas=schemas, uris=uris)
        return
    if not isinstance(value, Mapping):
        return
    if "$ref" in value:
        ref = value["$ref"]
        if not isinstance(ref, str) or not ref:
            raise HumanReviewInputError("$ref must be a non-empty string")
        document_uri, fragment = urldefrag(urljoin(base_uri, ref))
        target_name = uris.get(document_uri)
        if target_name is None:
            raise HumanReviewInputError(f"schema reference is external or unresolved: {ref!r}")
        if fragment:
            target: Any = schemas[target_name]
            if not fragment.startswith("/"):
                raise HumanReviewInputError(f"schema reference anchor is unsupported: {ref!r}")
            for component in fragment[1:].split("/"):
                component = component.replace("~1", "/").replace("~0", "~")
                if not isinstance(target, Mapping) or component not in target:
                    raise HumanReviewInputError(f"schema reference is unresolved: {ref!r}")
                target = target[component]
    for key, nested in value.items():
        if key != "$ref":
            _walk_schema_refs(nested, base_uri=base_uri, schemas=schemas, uris=uris)


def _load_state_schema_bundle(path: Path, entry_name: str = "run-state.schema.json") -> _StateValidator:
    if jsonschema is None or Registry is None or Resource is None:
        raise HumanReviewInputError("state_schema_bundle requires jsonschema and referencing")
    members, directories = _read_bounded_zip(path)
    if directories or set(members) != _STATE_SCHEMA_FILES:
        raise HumanReviewInputError(
            "state_schema_bundle must contain exactly the three unchanged schema files"
        )
    entry_name = _archive_relative(entry_name, field="state schema entry")
    if entry_name not in members:
        raise HumanReviewInputError(f"state schema entry {entry_name!r} is missing")
    schemas: dict[str, Mapping[str, Any]] = {}
    uri_by_name: dict[str, str] = {}
    uri_to_name: dict[str, str] = {}
    for name, raw in members.items():
        try:
            schema = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise HumanReviewInputError(f"schema {name!r} is not UTF-8 JSON") from exc
        if not isinstance(schema, Mapping):
            raise HumanReviewInputError(f"schema {name!r} must be an object")
        try:
            jsonschema.Draft7Validator.check_schema(schema)
        except jsonschema.SchemaError as exc:
            raise HumanReviewInputError(f"schema {name!r} is invalid") from exc
        uri = _schema_uri(name, schema)
        if uri in uri_to_name:
            raise HumanReviewInputError(f"duplicate schema $id {uri!r}")
        schemas[name] = schema
        uri_by_name[name] = uri
        uri_to_name[uri] = name
    uris = {name: name for name in schemas}
    uris.update({uri: name for name, uri in uri_by_name.items()})
    for name, schema in schemas.items():
        _walk_schema_refs(schema, base_uri=uri_by_name[name], schemas=schemas, uris=uris)
    registry = Registry()
    for name, schema in schemas.items():
        resource = Resource.from_contents(schema)
        registry = registry.with_resource(name, resource).with_resource(uri_by_name[name], resource)
    return _StateValidator(jsonschema.Draft7Validator(schemas[entry_name], registry=registry))


def _state_validator_for(path: Path | None, entry_name: str = "run-state.schema.json") -> _StateValidator | None:
    if path is None:
        return None
    return _load_state_schema_bundle(path, entry_name)


def _validate_against_schema(body: dict, schema_path: Path) -> tuple[bool, str]:
    if jsonschema is None:
        return True, "jsonschema not installed; validation skipped"
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        if isinstance(schema, dict):
            schema = schema.get("schema", schema)
        jsonschema.Draft7Validator.check_schema(schema)
        jsonschema.validate(body, schema)
    except jsonschema.ValidationError as exc:
        return False, str(exc)
    except (OSError, ValueError, jsonschema.SchemaError) as exc:
        return False, str(exc)
    return True, ""


def _read_json_file(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _filter_paginated_data(data_path: Path, query: dict[str, list[str]]) -> dict[str, Any]:
    raw = _read_json_file(data_path)
    items = raw.get("items", raw) if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        items = []
    status = (query.get("status") or [""])[0]
    if status:
        items = [item for item in items if isinstance(item, dict) and item.get("review_status", item.get("status")) == status]
    sampled = (query.get("sampled") or [""])[0]
    if sampled:
        items = [item for item in items if isinstance(item, dict) and _matches_sampled_filter(item, sampled)]
    total = len(items)
    offset = _non_negative_int((query.get("offset") or ["0"])[0], default=0)
    limit = _non_negative_int((query.get("limit") or [str(total)])[0], default=total)
    if limit == 0:
        page_items: list[Any] = []
    else:
        page_items = items[offset:offset + limit]
    return {
        "items": page_items,
        "total": total,
        "offset": offset,
        "limit": limit,
        "status": status or None,
        "sampled": sampled or None,
    }


def _non_negative_int(value: str, *, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(parsed, 0)


def _is_dataset_diff_save(body: Any) -> bool:
    return isinstance(body, dict) and "base_state_version" in body and "revisions" in body


def _prepare_dataset_diff_save(
    state_path: Path,
    body: dict[str, Any],
    state_validator: _StateValidator | None = None,
) -> tuple[dict[str, Any], bytes]:
    state = read_review_state(state_path, state_validator)
    base_version = body.get("base_state_version")
    if type(base_version) is not int:
        raise ReviewStateError("base_state_version must be an integer")
    current_version = int(state.get("state_version", 0))
    if base_version != current_version:
        raise StaleStateConflict(
            f"base_state_version {base_version} does not match current state_version {current_version}"
        )
    decisions = dict(state.get("review_decisions") or {})
    for item_id, revision in _iter_revisions(body.get("revisions")):
        existing = dict(decisions.get(item_id) or {})
        decision = _normalize_decision(
            revision.get("decision", revision.get("review_status", existing.get("decision", "pending")))
        )
        decisions[item_id] = {
            "item_id": item_id,
            "decision": decision,
            "reject_reason": revision.get("reject_reason", existing.get("reject_reason")),
            "edited_caption": revision.get("edited_caption", existing.get("edited_caption")),
            "reviewed_at": revision.get("reviewed_at") or _now_iso(),
            "reviewer_id": revision.get("reviewer_id", existing.get("reviewer_id", "human_review")),
            "state_version": current_version + 1,
        }
    next_state = copy.deepcopy(state)
    next_state["review_decisions"] = decisions
    next_state["state_version"] = current_version + 1
    next_state["updated_at"] = _now_iso()
    _validate_state(next_state, state_validator)
    return next_state, _encode_state(next_state)


def _prepare_experiment_review_save(
    state_path: Path,
    body: dict[str, Any],
    state_validator: _StateValidator | None = None,
) -> tuple[dict[str, Any], bytes]:
    experiment_id = body.get("experiment_id")
    if not isinstance(experiment_id, str):
        raise ReviewStateError("experiment_id must be a string")
    state = load_experiment_review_state(state_path, expected_experiment_id=experiment_id)
    if state_validator is not None:
        _validate_state(state, state_validator)
    base_version = body.get("base_state_version")
    if type(base_version) is not int or base_version < 0:
        raise ReviewStateError("base_state_version must be a non-negative integer")
    if base_version != state["state_version"]:
        raise StaleStateConflict(
            f"base_state_version {base_version} does not match current state_version {state['state_version']}"
        )
    draft = body.get("draft")
    if not isinstance(draft, dict):
        raise ReviewStateError("draft must be an object")
    next_state = dict(state)
    next_state["draft"] = {str(key): value for key, value in draft.items()}
    next_state["state_version"] = state["state_version"] + 1
    next_state["updated_at"] = _now_iso()
    _validate_state(next_state, state_validator)
    return next_state, _encode_state(next_state)


def _apply_dataset_diff_save(
    state_path: Path,
    body: dict[str, Any],
    state_validator: _StateValidator | None = None,
) -> dict[str, Any]:
    state, _ = _prepare_dataset_diff_save(state_path, body, state_validator)
    _commit_state(state_path, state)
    return state


def _prepare_dataset_batch_save(
    state_path: Path,
    data_path: Path,
    body: dict[str, Any],
    state_validator: _StateValidator | None = None,
) -> tuple[dict[str, Any], bytes]:
    item_ids = _batch_item_ids(data_path, body)
    if not item_ids:
        raise AstridError(
            "batch save matched no item_ids",
            recovery_command="check filter criteria or provide explicit item_ids",
        )
    decision = _normalize_decision(body.get("decision", body.get("review_status", "pending")))
    revisions = [
        {
            "item_id": item_id,
            "decision": decision,
            "reject_reason": body.get("reject_reason"),
            "edited_caption": body.get("edited_caption"),
            "reviewed_at": body.get("reviewed_at"),
            "reviewer_id": body.get("reviewer_id", "human_review_batch"),
        }
        for item_id in item_ids
    ]
    return _prepare_dataset_diff_save(
        state_path,
        {"base_state_version": body.get("base_state_version"), "revisions": revisions},
        state_validator,
    )


def _apply_dataset_batch_save(
    state_path: Path,
    data_path: Path,
    body: dict[str, Any],
    state_validator: _StateValidator | None = None,
) -> dict[str, Any]:
    state, _ = _prepare_dataset_batch_save(state_path, data_path, body, state_validator)
    _commit_state(state_path, state)
    return state


def _batch_item_ids(data_path: Path, body: Mapping[str, Any]) -> list[str]:
    item_ids = body.get("item_ids")
    if isinstance(item_ids, list) and item_ids:
        return [str(item_id) for item_id in item_ids if item_id is not None]
    scope = str(body.get("scope", ""))
    if scope != "filtered":
        raise AstridError(
            "batch save requires item_ids or scope='filtered'",
            recovery_command="provide item_ids list or set scope='filtered' with optional status/sampled filters",
        )
    status = body.get("status")
    filter_config = body.get("filter") if isinstance(body.get("filter"), Mapping) else {}
    if status is None:
        status = filter_config.get("status")
    sampled = body.get("sampled")
    if sampled is None:
        sampled = filter_config.get("sampled")
    items = _items_from_data(data_path)
    if status:
        items = [
            item
            for item in items
            if str(item.get("review_status", item.get("status", ""))) == str(status)
        ]
    if sampled is not None and str(sampled) != "":
        items = [item for item in items if _matches_sampled_filter(item, str(sampled))]
    return [str(item["item_id"]) for item in items if item.get("item_id") is not None]


def _items_from_data(data_path: Path) -> list[dict[str, Any]]:
    raw = _read_json_file(data_path)
    items = raw.get("items", raw) if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        return []
    return [dict(item) for item in items if isinstance(item, dict)]


def _matches_sampled_filter(item: Mapping[str, Any], expected: str) -> bool:
    normalized = str(expected).strip().lower()
    if normalized not in {"true", "false", "1", "0", "yes", "no"}:
        return True
    marker = item.get("review_sampled")
    if isinstance(marker, Mapping):
        sampled = bool(marker.get("sampled", True))
    elif marker is None:
        sampled = True
    else:
        sampled = bool(marker)
    expected_bool = normalized in {"true", "1", "yes"}
    return sampled is expected_bool


def _iter_revisions(revisions: Any):
    if isinstance(revisions, dict):
        for item_id, revision in revisions.items():
            if isinstance(revision, dict):
                yield str(item_id), revision
        return
    if isinstance(revisions, list):
        for revision in revisions:
            if isinstance(revision, dict) and revision.get("item_id") is not None:
                yield str(revision["item_id"]), revision


def _normalize_decision(value: Any) -> str:
    if value in {"accepted", "accept", True}:
        return "accept"
    if value in {"rejected", "reject", False}:
        return "reject"
    return "pending"


def _now_iso() -> str:
    from astrid.pack_api import utc_now_iso

    return utc_now_iso()


def _inherited_bridge() -> Any | None:
    from astrid.sdk import _child_bridge

    return _child_bridge._bridge


_RECEIVER_SESSION_NAME = ".astrid-human-review-session.json"
_RECEIVER_SESSION_SCHEMA = "astrid.human-review-session/v2"
_REVIEW_LAUNCH_NONCE_ENV = "ASTRID_HUMAN_REVIEW_LAUNCH_NONCE"


def _managed_receiver_path(out_path: Path) -> Path | None:
    """Use only the inherited bridge and its exact attempt-local output layout."""
    bridge = _inherited_bridge()
    if bridge is None:
        return None
    from astrid.sdk._child_bridge import ChildBridge

    if not isinstance(bridge, ChildBridge) or bridge._closed or bridge._channel.fileno() < 0:
        raise HumanReviewInputError("managed review requires its live inherited bridge")
    raw = os.environ.get("ASTRID_PROGRESS_PATH", "")
    progress = Path(raw)
    if (not raw or not progress.is_absolute() or progress.name != ".astrid-progress.json"
            or progress != progress.resolve(strict=False)
            or not out_path.is_absolute() or out_path != out_path.resolve(strict=False)
            or out_path.parent != progress.parent / "outputs"):
        raise HumanReviewInputError("managed review paths do not match the admitted attempt")
    for directory in (progress.parent, out_path.parent):
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or not owned_by_current_user(directory, info, forbidden=0o022):
            raise HumanReviewInputError("managed review directory is not owner controlled")
    if progress.exists() or progress.is_symlink():
        info = progress.lstat()
        if not stat.S_ISREG(info.st_mode) or not owned_by_current_user(progress, info) or info.st_nlink != 1:
            raise HumanReviewInputError("managed review progress path is not a private regular file")
    return progress.parent / _RECEIVER_SESSION_NAME


def _publish_receiver_session(path: Path, url: str) -> tuple[Path, int, int, bytes]:
    """Atomically expose a private observation of this already-listening receiver."""
    from astrid.pack_api import process_birth_identity

    parsed = urlparse(url)
    if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or not parsed.port
            or parsed.path != "/" or parsed.fragment or parsed.username or parsed.password
            or re.fullmatch(r"token=[0-9a-f]{32}", parsed.query) is None):
        raise HumanReviewInputError("review receiver must be the selected loopback session")
    launch_nonce = os.environ.get(_REVIEW_LAUNCH_NONCE_ENV, "")
    if re.fullmatch(r"[0-9a-f]{64}", launch_nonce) is None:
        raise HumanReviewInputError("managed review launch observation is unavailable")
    birth = process_birth_identity()
    if not birth:
        raise HumanReviewInputError("review receiver process birth is unavailable")
    payload = json.dumps({"schema": _RECEIVER_SESSION_SCHEMA, "receiver_url": url,
                          "process_pid": os.getpid(), "process_birth_id": birth,
                          "launch_nonce": launch_nonce},
                         sort_keys=True, separators=(",", ":")).encode("utf-8")
    fd, temporary = tempfile.mkstemp(prefix=".review-session-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            platform.owner_only(stream.fileno())
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        # link provides exclusive publication of complete bytes; never replace
        # a stale or foreign observation left by another owner.
        os.link(temporary, path, follow_symlinks=False)
        os.unlink(temporary)
        info = os.lstat(path)
        if os.name != "nt":
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        return path, info.st_dev, info.st_ino, payload
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _retire_receiver_session(owner: tuple[Path, int, int, bytes]) -> None:
    """Called only after server shutdown; mismatch preserves the observation."""
    from astrid.pack_api import process_birth_identity

    path, device, inode, payload = owner
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        raise HumanReviewInputError("review receiver cleanup observation unavailable") from exc
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        raw = stream.read(8193)
    expected = json.loads(payload)
    if (not stat.S_ISREG(info.st_mode) or not owned_by_current_user(path, info, mode=0o600) or info.st_nlink != 1
            or (info.st_dev, info.st_ino) != (device, inode) or raw != payload
            or expected["process_pid"] != os.getpid()
            or expected["process_birth_id"] != process_birth_identity()
            or (os.lstat(path).st_dev, os.lstat(path).st_ino) != (device, inode)):
        raise HumanReviewInputError("review receiver cleanup owner mismatch")
    os.unlink(path)


def _publish_saved_state(
    snapshot_path: Path,
    saved_bytes: bytes,
    revision: int,
) -> None:
    bridge = _inherited_bridge()
    if bridge is None:
        return
    _atomic_write(snapshot_path, saved_bytes)
    try:
        receipt = bridge.publish_snapshot(
            filename=_STATE_RESULT_FILENAME,
            output_port="state_result",
            revision=revision,
        )
    except Exception as exc:  # bridge failures are never browser acknowledgments
        raise HumanReviewInputError(f"durable state publication failed: {exc}") from exc
    if (
        not isinstance(receipt, Mapping)
        or receipt.get("output_port") != "state_result"
        or receipt.get("revision") != revision
        or receipt.get("durability") != "durable"
        or receipt.get("size") != len(saved_bytes)
        or receipt.get("digest") != "sha256:" + hashlib.sha256(saved_bytes).hexdigest()
    ):
        raise HumanReviewInputError("durable state publication returned an invalid receipt")


def _restore_snapshot_path(path: Path, previous: bytes | None) -> None:
    if previous is None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    else:
        _atomic_write(path, previous)


def _prepare_review_save(
    state_path: Path,
    body: dict[str, Any],
    *,
    state_validator: _StateValidator | None,
    managed: bool,
) -> tuple[dict[str, Any], bytes]:
    if is_experiment_review_save(body):
        if managed:
            return _prepare_experiment_review_save(state_path, body, state_validator)
        updated = apply_experiment_review_save(state_path, body)
        return updated, state_path.read_bytes()
    if _is_dataset_diff_save(body):
        if managed:
            return _prepare_dataset_diff_save(state_path, body, state_validator)
        updated = _apply_dataset_diff_save(state_path, body, state_validator)
        return updated, state_path.read_bytes()
    raise ReviewStateError(
        "/save requires a JSON object with base_state_version and either "
        "revisions (dataset diff) or draft (experiment review)"
    )


def make_handler_class(*, html_path: Path, data_path: Path, state_path: Path | None,
                       out_path: Path, schema_path: Path | None, mounts: dict[str, Path],
                       token: str, shutdown_event: threading.Event,
                       state_validator: _StateValidator | None = None,
                       state_result_path: Path | None = None):
    """Closure-based request handler with all config baked in."""

    state_lock = threading.RLock()
    snapshot_path = (state_result_path or out_path.parent / _STATE_RESULT_FILENAME).resolve()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            # Silence default access log; keep stderr clean
            return

        # ── helpers ────────────────────────────────────────────────────
        def _send(self, status: int, body: bytes = b"", content_type: str = "text/plain", extra_headers: dict | None = None):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra_headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if body:
                self.wfile.write(body)

        def _send_json(self, status: int, payload: dict):
            self._send(status, json.dumps(payload).encode("utf-8"), "application/json")

        def _token_ok(self) -> bool:
            url = urlparse(self.path)
            qs = parse_qs(url.query)
            t = (qs.get("token", [""])[0]) or self.headers.get("X-Session-Token", "")
            return t == token

        def _serve_file(self, path: Path, content_type: str | None = None):
            if not path.is_file():
                self._send(404, b"Not found")
                return
            ctype = content_type or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            data = path.read_bytes()
            # Range request support (mp4 seek)
            range_hdr = self.headers.get("Range", "")
            m = re.match(r"bytes=(\d+)-(\d*)", range_hdr)
            if m:
                start = int(m.group(1))
                end = int(m.group(2)) if m.group(2) else len(data) - 1
                end = min(end, len(data) - 1)
                chunk = data[start:end + 1]
                self.send_response(206)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Range", f"bytes {start}-{end}/{len(data)}")
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", str(len(chunk)))
                self.end_headers()
                self.wfile.write(chunk)
                return
            self._send(200, data, ctype, {"Accept-Ranges": "bytes"})

        # ── GET ───────────────────────────────────────────────────────
        def do_GET(self):  # noqa: N802
            url = urlparse(self.path)
            p = url.path

            # / → html_path (file or dir/index.html)
            if p == "/" or p == "":
                target = html_path if html_path.is_file() else (html_path / "index.html")
                self._serve_file(target, "text/html; charset=utf-8")
                return

            # /data.json
            if p == "/data.json":
                if url.query:
                    self._send_json(200, _filter_paginated_data(data_path, parse_qs(url.query)))
                    return
                self._serve_file(data_path, "application/json")
                return

            # /state.json (token required)
            if p == "/state.json":
                if not self._token_ok():
                    self._send(403, b"Forbidden")
                    return
                if state_path and state_path.is_file():
                    self._serve_file(state_path, "application/json")
                else:
                    self._send(404, b"No state file")
                return

            # /<prefix>/... static mounts
            for prefix, root in mounts.items():
                if p == prefix or p.startswith(prefix + "/"):
                    relative = unquote(p[len(prefix):].lstrip("/"))
                    candidate = (root / relative).resolve()
                    if not _safe_under(root, candidate):
                        self._send(403, b"Forbidden (path escape)")
                        return
                    self._serve_file(candidate)
                    return

            # html_path is a directory → maybe serve from there
            if html_path.is_dir():
                candidate = (html_path / p.lstrip("/")).resolve()
                if _safe_under(html_path, candidate) and candidate.is_file():
                    self._serve_file(candidate)
                    return

            self._send(404, b"Not found")

        # ── POST ──────────────────────────────────────────────────────
        def do_POST(self):  # noqa: N802
            if not self._token_ok():
                self._send_json(403, {"error": "forbidden", "detail": "missing or invalid session token"})
                return

            url = urlparse(self.path)
            length = int(self.headers.get("Content-Length", "0") or 0)
            raw = self.rfile.read(length) if length > 0 else b""

            if url.path == "/save":
                if state_path is None:
                    self._send_json(400, {"error": "no_state", "detail": "--state not configured"})
                    return
                try:
                    body = json.loads(raw.decode("utf-8") or "{}")
                except Exception as exc:  # noqa: BLE001
                    self._send_json(400, {"error": "bad_json", "detail": str(exc)})
                    return
                if not _is_dataset_diff_save(body) and not is_experiment_review_save(body):
                    self._send_json(
                        400,
                        {
                            "error": "diff_required",
                            "detail": (
                                "/save requires a JSON object with base_state_version and "
                                "either revisions (dataset diff) or draft (experiment review)"
                            ),
                        },
                    )
                    return
                managed = _inherited_bridge() is not None
                previous_snapshot: bytes | None = None
                published = False
                try:
                    with state_lock:
                        if managed:
                            previous_snapshot = snapshot_path.read_bytes() if snapshot_path.is_file() else None
                        updated, saved_bytes = _prepare_review_save(
                            state_path,
                            body,
                            state_validator=state_validator,
                            managed=managed,
                        )
                        if managed:
                            _publish_saved_state(snapshot_path, saved_bytes, updated["state_version"])
                            published = True
                            _commit_state(state_path, updated)
                except StaleStateConflict as exc:
                    if managed and not published:
                        _restore_snapshot_path(snapshot_path, previous_snapshot)
                    self._send_json(409, {"error": "stale_state", "detail": str(exc)})
                    return
                except Exception as exc:  # noqa: BLE001 - return JSON instead of killing handler thread
                    if managed and not published:
                        _restore_snapshot_path(snapshot_path, previous_snapshot)
                    self._send_json(
                        503 if managed else 400,
                        {
                            "error": "save_unpublished" if managed else "save_failed",
                            "detail": str(exc),
                        },
                    )
                    return
                self._send_json(200, {"state_version": updated["state_version"], "updated_at": updated["updated_at"]})
                return

            if url.path == "/submit-batch":
                if state_path is None:
                    self._send_json(400, {"error": "no_state", "detail": "--state not configured"})
                    return
                try:
                    body = json.loads(raw.decode("utf-8") or "{}")
                except Exception as exc:  # noqa: BLE001
                    self._send_json(400, {"error": "bad_json", "detail": str(exc)})
                    return
                if not isinstance(body, dict) or "base_state_version" not in body:
                    self._send_json(
                        400,
                        {
                            "error": "base_state_version_required",
                            "detail": "/submit-batch requires base_state_version",
                        },
                    )
                    return
                managed = _inherited_bridge() is not None
                previous_snapshot: bytes | None = None
                published = False
                try:
                    with state_lock:
                        if managed:
                            previous_snapshot = snapshot_path.read_bytes() if snapshot_path.is_file() else None
                        updated, saved_bytes = _prepare_dataset_batch_save(
                            state_path, data_path, body, state_validator
                        )
                        if managed:
                            _publish_saved_state(snapshot_path, saved_bytes, updated["state_version"])
                            published = True
                        _commit_state(state_path, updated)
                except StaleStateConflict as exc:
                    if managed and not published:
                        _restore_snapshot_path(snapshot_path, previous_snapshot)
                    self._send_json(409, {"error": "stale_state", "detail": str(exc)})
                    return
                except Exception as exc:  # noqa: BLE001 - return JSON instead of killing handler thread
                    if managed and not published:
                        _restore_snapshot_path(snapshot_path, previous_snapshot)
                    self._send_json(
                        503 if managed else 400,
                        {
                            "error": "batch_unpublished" if managed else "batch_failed",
                            "detail": str(exc),
                        },
                    )
                    return
                self._send_json(200, {"state_version": updated["state_version"], "updated_at": updated["updated_at"]})
                return
            if url.path == "/submit":
                try:
                    body = json.loads(raw.decode("utf-8") or "{}")
                except Exception as exc:  # noqa: BLE001
                    self._send_json(400, {"error": "bad_json", "detail": str(exc)})
                    return
                if schema_path is not None:
                    ok, err = _validate_against_schema(body, schema_path)
                    if not ok:
                        self._send_json(400, {"error": "schema_violation", "detail": err})
                        return
                if state_path is not None and state_path.is_file():
                    _validate_state(_read_state(state_path, state_validator), state_validator)
                    _atomic_write(snapshot_path, state_path.read_bytes())
                _atomic_write(out_path, raw)
                self._send(204)
                shutdown_event.set()
                return

            self._send(404, b"Not found")

    return Handler


_OPTIONAL_SENTINELS = {"", "__none__", "none", "None", "null"}


def _optional_path(value: Path | None) -> Path | None:
    if value is None or str(value) in _OPTIONAL_SENTINELS:
        return None
    return value


def _parse_mounts(values: list[str]) -> dict[str, Path]:
    out: dict[str, Path] = {}
    for value in values or []:
        if value in _OPTIONAL_SENTINELS:
            continue
        if "=" not in value:
            raise SystemExit(f"--serve expects PREFIX=DIR, got: {value}")
        raw_prefix, raw_root = value.split("=", 1)
        if not raw_prefix.startswith("/"):
            raw_prefix = "/" + raw_prefix
        prefix = _mount_prefix(raw_prefix, field="--serve prefix")
        if prefix in out:
            raise SystemExit(f"--serve has duplicate prefix: {prefix}")
        out[prefix] = Path(raw_root).resolve()
    return out


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--html", type=Path)
    p.add_argument("--data", type=Path, required=True)
    p.add_argument("--serve", action="append", default=[])
    p.add_argument("--state", type=Path)
    p.add_argument("--assets-bundle", type=Path)
    p.add_argument("--state-schema-bundle", type=Path)
    p.add_argument("--state-schema-entry", default="run-state.schema.json")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--response-schema", type=Path)
    p.add_argument("--port", type=int, default=0)
    p.add_argument("--no-open", action="store_true")
    p.add_argument("--timeout", type=int, default=0)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    args.html = _optional_path(args.html)
    args.state = _optional_path(args.state)
    args.assets_bundle = _optional_path(args.assets_bundle)
    args.state_schema_bundle = _optional_path(args.state_schema_bundle)
    args.response_schema = _optional_path(args.response_schema)
    selected_out = args.out
    args.out = args.out.resolve()
    args.out.parent.mkdir(parents=True, exist_ok=True)

    if args.html is not None and not args.html.exists() and args.assets_bundle is None:
        raise AstridError(
            f"--html not found: {args.html}",
            recovery_command="verify the --html path points to an existing file or directory",
        )
    if not args.data.is_file():
        raise AstridError(
            f"--data not found: {args.data}",
            recovery_command="verify the --data path points to an existing JSON file",
        )
    if args.assets_bundle is None and args.html is None:
        raise AstridError(
            "one of --html or --assets-bundle is required",
            recovery_command="provide direct HTML or a bounded Human Review asset bundle",
        )
    if args.assets_bundle is not None and not args.assets_bundle.is_file():
        raise AstridError(
            f"--assets-bundle not found: {args.assets_bundle}",
            recovery_command="verify the managed asset archive path",
        )
    if args.state_schema_bundle is not None and not args.state_schema_bundle.is_file():
        raise AstridError(
            f"--state-schema-bundle not found: {args.state_schema_bundle}",
            recovery_command="verify the schema archive path",
        )

    asset_scratch: tempfile.TemporaryDirectory[str] | None = None
    server: ThreadingHTTPServer | None = None
    server_thread: threading.Thread | None = None
    receiver_owner: tuple[Path, int, int, bytes] | None = None
    receiver_path = _managed_receiver_path(selected_out)
    try:
        if args.assets_bundle is not None:
            try:
                html_path, bundle_mounts, asset_scratch = _extract_assets_bundle(
                    args.assets_bundle, args.out.parent
                )
            except HumanReviewInputError as exc:
                raise AstridError(
                    str(exc),
                    recovery_command="provide a bounded Human Review asset archive with a valid manifest",
                ) from exc
        else:
            html_path = args.html.resolve()
            bundle_mounts = {}
        direct_mounts = _parse_mounts(args.serve)
        if set(bundle_mounts) & set(direct_mounts):
            raise AstridError(
                "asset bundle and --serve declare the same URL prefix",
                recovery_command="use unique static mount prefixes",
            )
        mounts = {**bundle_mounts, **direct_mounts}
        try:
            state_validator = _state_validator_for(
                args.state_schema_bundle, args.state_schema_entry
            )
        except HumanReviewInputError as exc:
            raise AstridError(
                str(exc),
                recovery_command="provide the unchanged three-file schema bundle",
            ) from exc
        if args.state is not None and args.state.is_file():
            try:
                _read_state(args.state, state_validator)
            except (OSError, ValueError, ReviewStateError) as exc:
                raise AstridError(
                    str(exc),
                    recovery_command="provide a state file valid under the admitted schema bundle",
                ) from exc

        port = args.port if args.port else _pick_free_port()
        token = secrets.token_hex(16)
        shutdown_event = threading.Event()
        handler = make_handler_class(
            html_path=html_path.resolve(),
            data_path=args.data.resolve(),
            state_path=args.state.resolve() if args.state else None,
            out_path=args.out,
            schema_path=args.response_schema.resolve() if args.response_schema else None,
            mounts=mounts,
            token=token,
            shutdown_event=shutdown_event,
            state_validator=state_validator,
            state_result_path=args.out.parent / _STATE_RESULT_FILENAME,
        )
        server = ThreadingHTTPServer(("127.0.0.1", port), handler)
        url = f"http://127.0.0.1:{port}/?token={token}"

        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        if receiver_path is not None:
            receiver_owner = _publish_receiver_session(receiver_path, url)
        else:
            print(f"human_review: serving at {url}", flush=True)
            print(f"human_review: token={token}", flush=True)
        if not args.no_open:
            try:
                webbrowser.open(url)
            except Exception as exc:  # noqa: BLE001
                if receiver_path is None:
                    log_and_swallow(exc, context="human_review.webbrowser_open")
                else:
                    # exc_info would include a browser exception's secret URL.
                    logging.getLogger(__name__).debug("managed review browser launch failed")

        start_t = time.time()
        while not shutdown_event.is_set():
            if args.timeout and (time.time() - start_t) >= args.timeout:
                raise AstridError(
                    f"human_review: timeout after {args.timeout}s without /submit",
                    recovery_command="submit the review form or increase --timeout",
                )
            time.sleep(0.25)
        print(f"human_review: submit received, wrote {args.out}", flush=True)
        return 0
    finally:
        if server is not None:
            if server_thread is not None:
                server.shutdown()
                server_thread.join(timeout=2)
                if server_thread.is_alive():
                    raise HumanReviewInputError("review receiver shutdown is incomplete")
            server.server_close()
            if receiver_owner is not None:
                _retire_receiver_session(receiver_owner)
        if asset_scratch is not None:
            asset_scratch.cleanup()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AstridError as exc:
        sys.exit(render_astrid_error(exc))
