"""Prepare one explicit historical selection before ordinary root admission.

Only the frozen JSON is ingested here. F05 owns media custody, current-attempt
uploads, child authority and Rendering. Existing documents are required: a run
ID alone is not a request to reconstruct the legacy iteration graph.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any

import astrid
from astrid.packs.video_editing.shared.iteration_inputs import freeze_inputs, serialize_frozen_inputs
from astrid.sdk import validate_child_policy
from astrid.sdk.capability_selection import select_capability_row
from astrid.sdk.exceptions import CapabilityValidationError

ROOT = "video_editing.iteration_video"
RENDER = "rendering.render"


class IterationAdmissionError(ValueError):
    """Selection, metadata budget or frozen CAS identity failed pre-admission."""


def _identity(value: Any) -> str:
    if (type(value) is not str or not value or len(value) > 1024
            or "*" in value or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise IterationAdmissionError("selection requires exact canonical identities")
    return value


def _id(row: Mapping[str, Any], key: str) -> str:
    value = _identity(row.get(key, row.get("id")))
    if "id" in row and key in row and row["id"] != value:
        raise IterationAdmissionError("conflicting resource identity aliases")
    return value


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


class _SelectedMetadata:
    """Local finite policy over an isolated generated response-reader seam."""

    def __init__(self, generated: Any, limits: Mapping[str, int]):
        self.reader = copy.copy(generated)
        self.limits = dict(limits)
        self.bytes = self.rows = 0
        self.failed = False
        self.raw: dict[str, Any] | None = None
        request = self.reader._request

        def observed_request(*args: Any, **kwargs: Any) -> Any:
            self.active()
            remaining = self.limits["max_discovery_metadata_bytes"] - self.bytes
            if remaining <= 0:
                self.stop("exhausted discovery metadata byte budget")

            def acquire(stream: Any) -> bytes:
                chunks: list[bytes] = []
                acquired = 0
                while True:
                    chunk = stream.read(min(65536, remaining + 1 - acquired))
                    if type(chunk) is not bytes:
                        self.stop("invalid discovery response bytes")
                    acquired += len(chunk)
                    if acquired > remaining:
                        chunks.clear()
                        self.stop("discovery metadata byte budget exceeded")
                    if not chunk:
                        break
                    chunks.append(chunk)
                self.bytes += acquired
                body = b"".join(chunks)
                try:
                    def pairs(items):
                        result = {}
                        for key, item in items:
                            if key in result:
                                raise ValueError("duplicate metadata field")
                            result[key] = item
                        return result
                    value = json.loads(body, object_pairs_hook=pairs,
                        parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite metadata")))
                except (ValueError, UnicodeError, RecursionError):
                    self.stop("invalid discovery metadata JSON")
                if type(value) is not dict:
                    self.stop("discovery metadata must be an object")
                if "items" in value:
                    if (set(value) != {"items", "next_cursor"} or type(value["items"]) is not list
                            or value["next_cursor"] is not None
                            and (type(value["next_cursor"]) is not str or not value["next_cursor"])):
                        self.stop("invalid discovery metadata page")
                    rows = max(1, len(value["items"]))
                else:
                    rows = 1
                self.rows += rows
                if self.rows > self.limits["max_discovery_rows"]:
                    self.stop("discovery metadata row budget exceeded")
                self.raw = value
                return body

            kwargs["response_reader"] = acquire
            try:
                return request(*args, **kwargs)
            except Exception:
                self.failed = True
                raise

        self.reader._request = observed_request

    def active(self) -> None:
        if self.failed:
            raise IterationAdmissionError("selected metadata reader already ended")

    def stop(self, message: str) -> None:
        self.failed = True
        raise IterationAdmissionError(message)

    def read(self, operation: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self.active()
        self.raw = None
        try:
            getattr(self.reader, operation)(*args, **kwargs)
            if self.raw is None:
                self.stop("generated bounded reader did not observe metadata")
            return self.raw
        except Exception:
            self.failed = True
            raise


def admit_iteration_video(
    *, client: Any, selection: Mapping[str, Any], manifest: Mapping[str, Any],
    quality: Mapping[str, Any], discovery_grant: Mapping[str, Any],
    parent_capability: Mapping[str, str], rendering_capability: Mapping[str, str],
    rendering_policy: Mapping[str, Any], force: bool = False,
    extra_pack_roots: tuple[str, ...] = (),
    execution_request: Mapping[str, Any] | None = None,
):
    """Verify one selected association, freeze existing documents and invoke.

    ``selection`` requires project_id/run_id/task_id/attempt_id/association_id
    and the artifact_index in the supplied manifest. Capabilities are exact
    capability_id/capability_digest pins. The singular grant is separate from
    Rendering policy and action inputs. Return the ordinary InvocationResult.
    """
    if type(force) is not bool:
        raise IterationAdmissionError("force must be a boolean")
    fields = {"project_id", "run_id", "task_id", "attempt_id", "association_id", "artifact_index"}
    if not isinstance(selection, Mapping) or set(selection) != fields:
        raise IterationAdmissionError("requires explicit project/run/task/attempt/association and artifact_index")
    selected = dict(selection)
    for field in fields - {"artifact_index"}:
        _identity(selected[field])
    if type(selected["artifact_index"]) is not int or selected["artifact_index"] < 0:
        raise IterationAdmissionError("artifact_index must be a nonnegative integer")
    pins = []
    for supplied, expected in ((parent_capability, ROOT), (rendering_capability, RENDER)):
        if (not isinstance(supplied, Mapping) or set(supplied) != {"capability_id", "capability_digest"}
                or supplied["capability_id"] != expected or type(supplied["capability_digest"]) is not str
                or re.fullmatch(r"sha256:[0-9a-f]{64}", supplied["capability_digest"]) is None):
            raise IterationAdmissionError("requires exact parent and Rendering capability pins")
        pins.append(dict(supplied))
    if not isinstance(rendering_policy, Mapping) or "discovery_grant" in rendering_policy:
        raise IterationAdmissionError("discovery grant must be supplied separately")
    try:
        policy = validate_child_policy({**rendering_policy, "discovery_grant": dict(discovery_grant)})
    except (CapabilityValidationError, ValueError, TypeError) as exc:
        raise IterationAdmissionError("invalid discovery grant or Rendering policy") from exc
    if policy["capabilities"] != [pins[1]] or policy["input_object_ids"] != []:
        raise IterationAdmissionError("Rendering policy must select the singular pinned renderer")
    grant = policy["discovery_grant"]
    if (any(grant[key] != selected[key] for key in ("project_id", "run_id", "task_id", "attempt_id"))
            or any(grant[key] != pins[0][key] for key in pins[0])):
        raise IterationAdmissionError("discovery grant disagrees with selected identity or parent capability")
    transport = getattr(client, "_transport", client)
    generated = getattr(transport, "_generated", getattr(transport, "generated", None))
    if not callable(getattr(transport, "ingest_project_object", None)) or generated is None:
        raise IterationAdmissionError("requires an explicit authenticated Runtime client")
    # Compare the same normalized declaration hash used by the existing host.
    for pin in pins:
        capability = astrid.get_capability(pin["capability_id"], kind="action", extra_pack_roots=extra_pack_roots)
        actual = "sha256:" + hashlib.sha256(_canonical(capability.definition)).hexdigest()
        if capability.id != pin["capability_id"] or actual != pin["capability_digest"]:
            raise IterationAdmissionError("selected local capability disagrees with its pin")
    metadata = _SelectedMetadata(generated, grant["limits"])
    project = metadata.read("get_project", selected["project_id"])
    run = metadata.read("get_run", selected["run_id"])
    task = metadata.read("get_task", selected["task_id"])
    association = metadata.read("get_managed_output", selected["association_id"])
    if (_id(project, "project_id") != selected["project_id"]
            or _id(run, "run_id") != selected["run_id"]
            or run.get("project_id") != selected["project_id"]
            or type(run.get("task_ids")) is not list or selected["task_id"] not in run["task_ids"]
            or _id(task, "task_id") != selected["task_id"]
            or any(task.get(key) != selected[key] for key in ("project_id", "run_id"))
            or any(association.get(key) != selected[key] for key in fields - {"artifact_index"})):
        raise IterationAdmissionError("selected historical ownership or association mismatch")
    # The association's historical attempt is authoritative even after a retry.
    rows, cursor, seen = [], None, set()
    while True:
        page = metadata.read("list_capabilities", limit=50, cursor=cursor)
        rows.extend(page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
        if cursor in seen:
            raise IterationAdmissionError("capability pagination repeated a cursor")
        seen.add(cursor)
    for pin in pins:
        registered = select_capability_row(rows, pin["capability_id"])
        if registered is None or registered.get("definition_digest") != pin["capability_digest"]:
            raise IterationAdmissionError("registered capability disagrees with its pin")
    digest = association.get("digest")
    if (type(digest) is not str or re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None
            or association.get("object_id") != digest or type(association.get("size")) is not int
            or not 0 <= association["size"] <= grant["limits"]["max_selected_output_bytes"]
            or association["size"] > grant["limits"]["max_child_media_bytes"]):
        raise IterationAdmissionError("selected media identity or finite byte budget mismatch")
    reference = {"project": selected["project_id"], "run_id": selected["run_id"],
                 "task_id": selected["task_id"], "artifact_index": selected["artifact_index"],
                 "source_association_id": association["association_id"]}
    if "output_id" in association:
        reference["output_id"] = association["output_id"]
    binding = {"name": "media_dependency", "object_id": digest, "sha256": digest[7:],
               "size": association["size"], "media_type": association.get("media_type"),
               "filename": association.get("filename"), "associations": [reference]}
    frozen = freeze_inputs(manifest, quality, [binding], project=selected["project_id"], target_run_id=selected["run_id"])
    data = serialize_frozen_inputs(frozen, project=selected["project_id"], target_run_id=selected["run_id"])
    envelope_id = "sha256:" + hashlib.sha256(data).hexdigest()
    ingested = transport.ingest_project_object(selected["project_id"], data, media_type="application/json",
        filename="iteration-frozen.json", idempotency_key="iteration-frozen-" + hashlib.sha256(
            _canonical({"project_id": selected["project_id"], "object_id": envelope_id})).hexdigest())
    # The ordinary SDK facade preserves the committed receipt out of band in
    # its stable data/receipt envelope; generated clients return the mapping.
    if isinstance(ingested, Mapping) and set(ingested) == {"data", "receipt"}:
        ingested = ingested["data"]
    if (not isinstance(ingested, Mapping) or ingested.get("object_id") != envelope_id
            or ingested.get("digest") != envelope_id or type(ingested.get("size")) is not int
            or ingested["size"] != len(data)
            or "project" in ingested and ingested["project"] != selected["project_id"]):
        raise IterationAdmissionError("ingested frozen CAS identity disagrees with exact envelope bytes")
    descriptor = {"object_id": envelope_id, "digest": envelope_id, "size": len(data),
                  "filename": "iteration-frozen.json", "media_type": "application/json"}
    root_inputs = {"project_id": selected["project_id"], "target_run_id": selected["run_id"],
                   "frozen_inputs": descriptor}
    if force:
        root_inputs["force"] = True
    return astrid.invoke(ROOT, kind="action", project=selected["project_id"], client=client,
        extra_pack_roots=extra_pack_roots, execution_request=execution_request,
        inputs=root_inputs, child_delegation=policy)
