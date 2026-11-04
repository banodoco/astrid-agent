"""Bounded current-attempt inputs for the public Training Human Review child.

The mapping is authenticated *content* of the admitted ZIP, not Runtime authority.
F05 must verify its binding to immutable admitted data and parent/child descriptors.
Historical acquisition identity and failed-child snapshot recovery are not proved here.
"""

from __future__ import annotations

import copy
import hashlib
import io
import json
import re
import stat
import zipfile
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote

from runtime_protocol.dirfd import filesystem as os

import astrid.sdk as sdk
from astrid.pack_api import write_json_atomic
from astrid.core.foundation.paths import REPO_ROOT

from .state import validate_review_state, write_review_state

ARCHIVE_MAX_BYTES = 64 * 1024**2
ARCHIVE_MAX_ENTRIES = 4096
SCHEMA_NAMES = ("run-state.schema.json", "review-decision.schema.json", "filter-stats.schema.json")
UI_NAMES = ("index.html", "app.js", "styles.css")
MAP_NAME = "review-media-map.json"


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      allow_nan=False).encode("utf-8")


def _digest(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _relative(value: str) -> str:
    path = PurePosixPath(value)
    if (not value or path.is_absolute() or value != path.as_posix()
            or any(part in {"", ".", ".."} for part in value.split("/"))
            or "\\" in value or "\x00" in value):
        raise ValueError("Human Review input path must be canonical and confined")
    return value


def _identity(info: os.stat_result) -> tuple[int, ...]:
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns)


def _capture(root: Path, relative: str, maximum: int) -> bytes:
    """Open every path component without following links, and read once.

    Compare the open inode before/after reading and the directory entry after
    reading, so replacement, unlinking and in-place modification fail closed.
    ZIP assembly only consumes the returned bytes, never this mutable path.
    """
    parts = _relative(relative).split("/")
    descriptors: list[int] = []
    directory_entries: list[tuple[int, str, int]] = []
    try:
        directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        descriptors.append(directory)
        for part in parts[:-1]:
            parent = directory
            directory = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            descriptors.append(directory)
            directory_entries.append((parent, part, directory))
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                     dir_fd=directory)
        descriptors.append(fd)
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or not 0 <= before.st_size <= maximum:
            raise ValueError("Human Review input must be a bounded regular file")
        chunks: list[bytes] = []
        size = 0
        while chunk := os.read(fd, min(1024 * 1024, maximum - size + 1)):
            size += len(chunk)
            if size > maximum:
                raise ValueError("Human Review input exceeds its byte limit")
            chunks.append(chunk)
        after = os.fstat(fd)
        entry = os.stat(parts[-1], dir_fd=directory, follow_symlinks=False)
        if (size != before.st_size or _identity(before) != _identity(after)
                or _identity(after) != _identity(entry)):
            raise ValueError("Human Review input changed or was replaced during capture")
        for parent, name, opened in directory_entries:
            entry_info = os.stat(name, dir_fd=parent, follow_symlinks=False)
            opened_info = os.fstat(opened)
            if (entry_info.st_dev, entry_info.st_ino, entry_info.st_mode) != (
                    opened_info.st_dev, opened_info.st_ino, opened_info.st_mode):
                raise ValueError("Human Review input directory was replaced during capture")
        root_info, opened_root = os.lstat(root), os.fstat(descriptors[0])
        if (root_info.st_dev, root_info.st_ino, root_info.st_mode) != (
                opened_root.st_dev, opened_root.st_ino, opened_root.st_mode):
            raise ValueError("Human Review capture root was replaced")
        return b"".join(chunks)
    except OSError as exc:
        raise ValueError("Human Review input is missing, symlinked or unavailable") from exc
    finally:
        for fd in reversed(descriptors):
            os.close(fd)


def _under(path: Path, root: Path) -> str:
    # Do not resolve: that would hide symlink traversal before _capture checks it.
    try:
        return _relative(path.relative_to(root).as_posix())
    except ValueError as exc:
        raise ValueError("Human Review input escapes the parent attempt output root") from exc


def _check_archive(compressed: int, extracted: int, entries: int) -> None:
    if (any(type(value) is not int or value < 0 for value in (compressed, extracted, entries))
            or compressed > ARCHIVE_MAX_BYTES or extracted > ARCHIVE_MAX_BYTES
            or entries > ARCHIVE_MAX_ENTRIES):
        raise ValueError("Human Review archive compressed/extracted byte or entry limit exceeded")


def _archive(members: Mapping[str, bytes], *, directories: tuple[str, ...] = ()) -> bytes:
    extracted = sum(len(payload) for payload in members.values())
    _check_archive(0, extracted, len(members) + len(directories))
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in (*directories, *sorted(members)):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = ((stat.S_IFDIR | 0o755) if name.endswith("/")
                                  else (stat.S_IFREG | 0o644)) << 16
            archive.writestr(info, b"" if name.endswith("/") else members[name])
            _check_archive(stream.tell(), extracted, len(members) + len(directories))
    payload = stream.getvalue()
    _check_archive(len(payload), extracted, len(members) + len(directories))
    return payload


def _source(item: Mapping[str, Any]) -> dict[str, Any]:
    fields = ("source_type", "source_id", "source_url", "acquired_at", "content_hash", "media_type")
    if any(type(item.get(field)) is not str or not item[field] for field in fields):
        raise ValueError("Human Review row requires exact source provenance")
    if re.fullmatch(r"[0-9a-f]{64}", item["content_hash"]) is None:
        raise ValueError("Human Review source content_hash is invalid")
    # content_hash is retained as source provenance, not reinterpreted as a
    # derived clip digest. The captured media has its own digest and size.
    return {field: copy.deepcopy(item[field]) for field in (
        *fields, "source_metadata", "derived_from", "scene_index", "clip_start_s", "clip_end_s"
    ) if field in item}


def _stage(root: Path, port: str, payload: bytes, suffix: str) -> dict[str, str]:
    directory = root / "review-inputs"
    if directory.is_symlink():
        raise ValueError("Human Review staging directory must not be a symlink")
    directory.mkdir(exist_ok=True)
    name = f"{port}-{hashlib.sha256(payload).hexdigest()}{suffix}"
    path = directory / name
    if path.is_symlink():
        raise ValueError("Human Review staged input must not be a symlink")
    if path.exists():
        if _capture(root, _under(path, root), len(payload)) != payload:
            raise ValueError("Human Review staged input changed")
    else:
        with path.open("xb") as stream:
            stream.write(payload)
    return {"filename": _under(path, root), "output_port": port,
            "media_type": "application/zip" if suffix == ".zip" else "application/json"}


def _input_bytes(root: Path, port: str, producer: Mapping[str, str], maximum: int) -> bytes:
    payload = _capture(root, producer["filename"], maximum)
    expected = Path(producer["filename"]).stem.removeprefix(port + "-")
    if hashlib.sha256(payload).hexdigest() != expected:
        raise ValueError("Human Review immutable staged input changed before submit/retry")
    return payload


def prepare_inputs(*, root: Path, run_dir: Path, data_path: Path, state_path: Path,
                   ui_root: Path, schema_root: Path, meter: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    if meter is None:
        raise ValueError("Human Review requires the run-scoped finite child_work_meter")
    # max_child_bytes limits the aggregate transported objects below; it must
    # not limit one uncompressed member that will be carried inside the ZIP.
    maximum = min(ARCHIVE_MAX_BYTES, meter.OBJECT_MAX_BYTES)
    original_bytes = _capture(root, _under(data_path, root), maximum)
    canonical = json.loads(original_bytes)
    if not isinstance(canonical, dict) or type(canonical.get("items")) is not list:
        raise ValueError("Human Review canonical data must contain an items list")
    projected = copy.deepcopy(canonical)
    _check_archive(0, 0, len(projected["items"]) + len(UI_NAMES) + 4)
    members = {"ui/" + name: _capture(ui_root, name, maximum) for name in UI_NAMES}
    mapping: list[dict[str, Any]] = []
    item_ids: set[str] = set()
    member_names: set[str] = set()
    clips = run_dir / "clips"
    for item in projected["items"]:
        if not isinstance(item, dict) or type(item.get("item_id")) is not str or not item["item_id"]:
            raise ValueError("Human Review row requires an item_id")
        item_id = item["item_id"]
        if item_id in item_ids:
            raise ValueError("Human Review has duplicate item IDs")
        item_ids.add(item_id)
        provenance = _source(item)
        raw = item.get("media_path")
        if type(raw) is not str or not raw or "://" in raw:
            raise ValueError("Human Review media must be a confined local source")
        path = Path(raw)
        if not path.is_absolute():
            _relative(raw)
            path = REPO_ROOT / path
        relative = _under(path, root)
        media_relative = _under(path, clips)
        # Encode URL components individually; the ZIP member stays the exact
        # canonical filesystem-relative name and is unique for every row.
        member = "media/" + media_relative
        if member in member_names:
            raise ValueError("Human Review has colliding media members")
        member_names.add(member)
        remaining = min(maximum, ARCHIVE_MAX_BYTES - sum(len(value) for value in members.values()))
        payload = _capture(root, relative, remaining)
        members[member] = payload
        url = "/media/" + "/".join(quote(part, safe="") for part in media_relative.split("/"))
        mapping.append({"item_id": item_id, "source": provenance,
                        "source_media_path": relative, "media_path": url,
                        "archive_member": member, "digest": _digest(payload), "size": len(payload)})
        item["media_path"] = url
    data_bytes = _json_bytes(projected)
    members["human-review-assets.json"] = _json_bytes({"html_root": "ui", "mounts": {"/media": "media"}})
    members[MAP_NAME] = _json_bytes({"schema": "training-review-media-map/v1",
        "data": {"digest": _digest(data_bytes), "size": len(data_bytes)},
        "canonical_data": {"digest": _digest(original_bytes), "size": len(original_bytes)},
        "items": mapping})
    bundle = _archive(members, directories=("ui/", "media/"))
    schemas = _archive({name: _capture(schema_root, name, maximum) for name in SCHEMA_NAMES})
    state_bytes = _capture(root, _under(state_path, root), maximum)
    initial_state = json.loads(state_bytes)
    validate_review_state(initial_state)
    payloads = {"assets_bundle": (bundle, ".zip"), "data": (data_bytes, ".json"),
                "state": (state_bytes, ".json"), "state_schema_bundle": (schemas, ".zip")}
    if (len(payloads) > meter.limits["max_child_inputs"]
            or sum(len(value[0]) for value in payloads.values()) > meter.limits["max_child_bytes"]
            or any(len(value[0]) > meter.OBJECT_MAX_BYTES for value in payloads.values())):
        raise RuntimeError("Human Review child object/count/aggregate input budget exceeded")
    inputs: dict[str, Any] = {"timeout": 0, "no_open": True}
    for port, (payload, suffix) in payloads.items():
        producer = _stage(root, port, payload, suffix)
        registered = meter.register_local_file(root / producer["filename"], producer, name=port)
        if registered["digest"] != _digest(payload) or registered["size"] != len(payload):
            raise ValueError("Human Review staged descriptor changed before admission")
        inputs[port] = producer
    return inputs, initial_state


def _output(child: Any, port: str, root: Path, meter: Any) -> Any:
    rows = child.outputs.get("managed_outputs")
    matches = [row for row in rows or [] if isinstance(row, Mapping) and row.get("output_port") == port]
    if len(matches) != 1:
        raise ValueError(f"Human Review must return exactly one {port} output")
    row = dict(matches[0])
    if ((row.get("task_id"), row.get("attempt_id"), row.get("run_id")) !=
            (child.kernel_task_id, child.kernel_attempt_id, child.kernel_run_id)
            or type(row.get("size")) is not int or row["size"] < 0
            or type(row.get("digest")) is not str
            or re.fullmatch(r"sha256:[0-9a-f]{64}", row["digest"]) is None
            or row.get("object_id") != row["digest"]
            or type(row.get("association_id")) is not str or not row["association_id"]
            or row.get("media_type") != "application/json"):
        raise ValueError("Human Review output identity or byte descriptor changed")
    meter.retain(row)
    local = child.materialize_output(row["association_id"])
    if dict(local.output) != row:
        raise ValueError("Human Review materialized output descriptor changed")
    payload = _capture(root, _relative(local.filename), min(meter.OBJECT_MAX_BYTES, row["size"]))
    if len(payload) != row["size"] or _digest(payload) != row["digest"]:
        raise ValueError("Human Review materialized output bytes changed")
    return json.loads(payload)


def run_review(*, root: Path, run_dir: Path, output_dir: Path, data_path: Path,
               state_path: Path, ui_root: Path, schema_root: Path, meter: Any,
               round_index: int) -> None:
    inputs, initial = prepare_inputs(root=root, run_dir=run_dir, data_path=data_path,
        state_path=state_path, ui_root=ui_root, schema_root=schema_root, meter=meter)
    projected = json.loads(_input_bytes(root, "data", inputs["data"], meter.OBJECT_MAX_BYTES))
    item_ids = {item["item_id"] for item in projected["items"]}
    identity = _json_bytes([round_index, inputs])
    key = "training-review-" + hashlib.sha256(identity).hexdigest()
    meter.admit(key, "editorial.human_review", inputs)
    waiting_identity: tuple[str, str] | None = None
    waiting_attempt: str | None = None
    while True:
        for port in ("assets_bundle", "data", "state", "state_schema_bundle"):
            _input_bytes(root, port, inputs[port], meter.OBJECT_MAX_BYTES)
        child = sdk.invoke("editorial.human_review", kind="action", inputs=inputs,
                           child_key=key, wait=True, timeout_seconds=3600.0, poll_seconds=1.0)
        pair = (child.kernel_task_id, child.kernel_run_id)
        if waiting_identity is not None and (pair != waiting_identity
                or waiting_attempt and child.kernel_attempt_id != waiting_attempt):
            raise ValueError("Human Review retry changed child task/attempt identity")
        if child.ok is True and child.raw_result.get("state") == "completed":
            if not all(type(value) is str and value for value in (*pair, child.kernel_attempt_id)):
                raise ValueError("Human Review completion requires exact task/attempt identity")
            break
        error = child.error
        if (not isinstance(error, Mapping) or error.get("code") != "task_wait_timeout"
                or not all(type(value) is str and value for value in pair)):
            # A failed child is not a public materialization source. Its newer
            # acknowledged snapshots require the separate F05 controller route.
            raise RuntimeError(f"editorial.human_review child did not complete: {error}")
        waiting_identity = pair
        waiting_attempt = child.kernel_attempt_id or None
    decisions = _output(child, "decisions", root, meter)
    final = _output(child, "state_result", root, meter)
    if not isinstance(decisions, Mapping):
        raise ValueError("Human Review submit body must be an object")
    validate_review_state(final)
    mutable = {"review_decisions", "state_version", "updated_at", "submitted"}
    if ({key: value for key, value in final.items() if key not in mutable} !=
            {key: value for key, value in initial.items() if key not in mutable}
            or final["state_version"] < initial["state_version"]):
        raise ValueError("Human Review final state ownership/config/version changed")
    current = json.loads(_capture(root, _under(state_path, root), meter.OBJECT_MAX_BYTES))
    validate_review_state(current)
    if current != initial:
        raise ValueError("Human Review parent checkpoint changed while awaiting child")
    prior = initial.get("review_decisions") or {}
    returned = final.get("review_decisions") or {}
    if (any(item_id not in item_ids and prior.get(item_id) != value for item_id, value in returned.items())
            or any(item_id not in item_ids and returned.get(item_id) != value for item_id, value in prior.items())
            or any(value.get("item_id") != item_id for item_id, value in returned.items())):
        raise ValueError("Human Review returned foreign or mismatched decisions")
    current["review_decisions"] = {**prior, **copy.deepcopy(returned)}
    current["submitted"] = True
    # Preserve the parent writer and monotonic version after child-side saves.
    current["state_version"] = max(current["state_version"], final["state_version"])
    validate_review_state(current)
    checkpoint = output_dir / "review_server" / "human_review.final.json"
    _under(checkpoint, root)
    for directory in (*output_dir.parents, output_dir, checkpoint.parent):
        if directory.is_relative_to(root) and directory.is_symlink():
            raise ValueError("Human Review checkpoint directory must not be a symlink")
    if checkpoint.is_symlink():
        raise ValueError("Human Review final checkpoint must not be a symlink")
    checkpoint.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(checkpoint, dict(decisions))
    write_review_state(state_path, current)
