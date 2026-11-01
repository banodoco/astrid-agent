"""Private Timeline revision operations used by the public remote facade."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any, Mapping

from .contracts import DomainResult, ErrorObject


class _RemoteTimelineRevisionMixin:
    """Keep canonical revision reads and ledger operations off ``remote.py``.

    The mixin deliberately relies on ``_RemoteFamily`` for the typed transport
    and on ``RemoteTimelines`` for timeline scope resolution.  It therefore
    moves no authority or transport policy; it only keeps the cohesive
    revision-operation facade together in a private neighboring module.
    """

    if TYPE_CHECKING:
        def _resolve_timeline(self, project: Any, ref: Any) -> DomainResult[Any]: ...

        def _typed(
            self, operation: str, *args: Any, key: str | None = None, **kwargs: Any,
        ) -> DomainResult[Any]: ...

    def open_bundle(self, project, ref, *, revision_id=None, publication_id=None):
        """Read one complete, read-only authoring bundle (parent → shots → internal timelines).

        The same closure ``timeline_document.py checkout`` writes, pinned to the
        current head or to ``revision_id``. ``timelines show`` builds its cut
        table from it and ``timelines diff`` compares two of them. Nothing is
        written. Returns ``{bundle, project_id, timeline_id, revision_id,
        is_current_head}``.
        """
        from astrid.core.timeline.authoring_bundle import (
            AuthoringBundleError,
            open_authoring_bundle,
        )
        from astrid.sdk.timeline.commit import (
            INTERNAL_IMAGE_FIELDS,
            PARENT_IMAGE_FIELDS,
            SHOT_IMAGE_FIELDS,
            _json,
            bundle_from_document,
        )

        opened = self.open_composition(
            project, ref, revision_id=revision_id, limit=1, _raw_projection=True,
        )
        if not opened.ok or not isinstance(opened.data, Mapping):
            return opened
        native = opened.data.get("native_inspection") if isinstance(opened.data.get("native_inspection"), Mapping) else {}
        summary = opened.data.get("summary") if isinstance(opened.data.get("summary"), Mapping) else {}
        project_id = native.get("project_id")
        timeline_id = native.get("timeline_id")
        head = summary.get("head_revision_id") or native.get("head_revision_id")
        revision = revision_id or summary.get("revision_id") or head
        if not all(isinstance(value, str) and value for value in (project_id, timeline_id, revision)):
            return DomainResult.failure(ErrorObject(
                "protocol_error",
                "timeline inspection did not return a project id, timeline id and revision",
                {"project": str(project), "timeline": str(ref)},
            ))
        if publication_id is not None and (not revision_id or not isinstance(publication_id, str) or not publication_id):
            return DomainResult.failure(ErrorObject(
                "protocol_error", "source hydration requires the exact original revision/publication pair", {}
            ))
        parent = self._typed("get_project_parent_composition_revision", project_id, timeline_id, revision)
        if not parent.ok or not isinstance(parent.data, Mapping):
            return parent
        if any(parent.data.get(key) != value for key, value in {
            "project_id": project_id, "timeline_id": timeline_id, "revision_id": revision,
        }.items()):
            return DomainResult.failure(ErrorObject(
                "integrity_error", "parent revision response does not match its exact scope or pin",
                {"project_id": project_id, "timeline_id": timeline_id, "revision_id": revision},
            ))
        payload = parent.data.get("payload") if isinstance(parent.data.get("payload"), Mapping) else {}
        owner_by_revision: dict[str, str] = {}
        manifest = parent.data.get("dependency_manifest")
        manifest_rows = manifest.get("internal_timelines") if isinstance(manifest, Mapping) else None
        if isinstance(manifest_rows, list):
            for row in manifest_rows:
                if not isinstance(row, Mapping):
                    return DomainResult.failure(ErrorObject(
                        "integrity_error", "canonical internal timeline pin is invalid", {}
                    ))
                child_revision, owner = row.get("revision_id"), row.get("timeline_id")
                if not isinstance(child_revision, str) or not isinstance(owner, str) or not owner:
                    return DomainResult.failure(ErrorObject(
                        "integrity_error", "canonical internal timeline pin has no exact owner", {}
                    ))
                previous = owner_by_revision.setdefault(child_revision, owner)
                if previous != owner:
                    return DomainResult.failure(ErrorObject(
                        "integrity_error", "canonical internal revision has conflicting owners",
                        {"revision_id": child_revision},
                    ))
        shots: dict[str, Any] = {}
        internals: dict[str, Any] = {}
        for occurrence in payload.get("occurrences") or []:
            if not isinstance(occurrence, Mapping):
                continue
            shot_revision = occurrence.get("shot_revision_id") or occurrence.get("revision_id")
            if shot_revision not in shots:
                shot = self._typed("get_project_shot_revision", project_id, occurrence.get("shot_id"), shot_revision)
                if not shot.ok or not isinstance(shot.data, Mapping):
                    return shot
                if (shot.data.get("project_id"), shot.data.get("shot_id"), shot.data.get("revision_id")) != (
                    project_id, occurrence.get("shot_id"), shot_revision,
                ):
                    return DomainResult.failure(ErrorObject(
                        "integrity_error",
                        "shot revision response does not match its exact scope or pin",
                        {"project_id": project_id, "shot_id": occurrence.get("shot_id"),
                         "revision_id": shot_revision},
                    ))
                shots[shot_revision] = shot.data
            shot_data = shots[shot_revision]
            shot_payload = shot_data.get("payload") if isinstance(shot_data.get("payload"), Mapping) else {}
            internal_revision = shot_data.get("internal_timeline_revision_id") or shot_payload.get("internal_timeline_revision_id")
            if internal_revision not in internals:
                internal_owner = owner_by_revision.get(internal_revision)
                if not isinstance(internal_owner, str) or not internal_owner:
                    return DomainResult.failure(ErrorObject(
                        "integrity_error",
                        "pinned internal revision has no exact owner timeline",
                        {"revision_id": internal_revision},
                    ))
                internal = self._typed("get_project_timeline_revision", project_id, internal_owner, internal_revision)
                if not internal.ok or not isinstance(internal.data, Mapping):
                    return internal
                if (internal.data.get("project_id"), internal.data.get("timeline_id"),
                        internal.data.get("revision_id")) != (project_id, internal_owner, internal_revision):
                    return DomainResult.failure(ErrorObject(
                        "integrity_error",
                        "internal revision response does not match its exact owner or pin",
                        {"project_id": project_id, "timeline_id": internal_owner, "revision_id": internal_revision},
                    ))
                internals[internal_revision] = internal.data
        try:
            # Runtime revision reads carry outer storage/read-envelope fields
            # that are not part of a canonical immutable image.  Keep those
            # raw rows available to callers, but project the source graph onto
            # the existing canonical fieldsets before opening the bundle.
            parent_image = {
                **{key: parent.data[key] for key in PARENT_IMAGE_FIELDS},
                "project_id": project_id,
                "timeline_id": timeline_id,
            }
            shot_images = [
                {
                    **{key: row[key] for key in SHOT_IMAGE_FIELDS},
                    "project_id": row.get("project_id", project_id),
                }
                for row in shots.values()
            ]
            internal_images = [
                {
                    **{key: row[key] for key in INTERNAL_IMAGE_FIELDS},
                    "project_id": row.get("project_id", project_id),
                }
                for row in internals.values()
            ]
            bundle = open_authoring_bundle(
                parent_image,
                shot_revisions=shot_images,
                internal_timeline_revisions=internal_images,
            )
            if publication_id is not None:
                # Bind the immutable read to the original publication, even if
                # another writer has advanced the head. Never infer retained pins.
                cursor, seen = None, set()
                while True:
                    page = self.history(project_id, timeline_id, cursor=cursor, limit=100)
                    if not page.ok:
                        return page
                    entry = next((row for row in page.data["items"]
                                  if row["publication_id"] == publication_id), None)
                    if entry is not None:
                        if entry["revision_id"] != revision or entry["receipt"]["receipt_id"] != publication_id:
                            raise AuthoringBundleError("original revision/publication pair changed")
                        break
                    cursor = page.data.get("next_cursor")
                    if cursor is None or cursor in seen:
                        raise AuthoringBundleError("original publication is unavailable")
                    seen.add(cursor)
                document = entry["receipt"]["result"].get("canonical_document")
                if document is None:
                    # Bootstrap/legacy receipts need the manifest retained by
                    # Runtime's verified immutable parent read, not a rebuilt one.
                    document = {
                        "schema_version": 1, "project_id": project_id, "timeline_id": timeline_id,
                        "parent_revision": {key: parent.data[key] for key in PARENT_IMAGE_FIELDS},
                        "shot_revisions": sorted(
                            ({key: row[key] for key in SHOT_IMAGE_FIELDS} for row in shots.values()),
                            key=lambda row: (row["shot_id"], row["revision_id"])),
                        "internal_timeline_revisions": sorted(
                            ({key: row[key] for key in INTERNAL_IMAGE_FIELDS} for row in internals.values()),
                            key=lambda row: (row["timeline_id"], row["revision_id"])),
                        "dependency_manifest": parent.data["dependency_manifest"],
                    }
                hydrated = bundle_from_document(document)
                source = hydrated["source_mapping"].pop("_runtime_document")
                if _json(hydrated) != _json(bundle) or ("dependency_manifest" in parent.data
                        and _json(parent.data["dependency_manifest"]) != _json(source["dependency_manifest"])):
                    raise AuthoringBundleError("canonical document differs from the original closure read")
                bundle["source_mapping"]["_runtime_document"] = source
                bundle["base_parent"]["publication_id"] = publication_id
        except (AuthoringBundleError, KeyError, TypeError) as exc:
            return DomainResult.failure(ErrorObject(
                "protocol_error", f"could not open the timeline closure: {exc}",
                {"timeline": timeline_id, "revision_id": revision},
            ))
        return DomainResult.success({
            "bundle": bundle,
            "project_id": project_id,
            "timeline_id": timeline_id,
            "revision_id": revision,
            "head_revision_id": head,
            "is_current_head": revision == head,
        })

    def commit(self, project, ref, body: Mapping[str, Any], *, idempotency_key=None):
        """Commit a scoped command batch without changing its request body."""
        key = idempotency_key or uuid.uuid4().hex
        resolved = self._resolve_timeline(project, ref)
        if not resolved.ok:
            return DomainResult.failure(resolved.error, idempotency_key=key)
        scope = resolved.data
        project_id = scope.get("project_id") if isinstance(scope, Mapping) else None
        timeline_id = scope.get("timeline_id") if isinstance(scope, Mapping) else None
        if not isinstance(project_id, str) or not project_id or not isinstance(timeline_id, str) or not timeline_id:
            return DomainResult.failure(
                ErrorObject("protocol_error", "timeline scope has no canonical project/timeline identity", {}),
                idempotency_key=key,
            )
        if not isinstance(body, Mapping) or body.get("project_id") != project_id or body.get("timeline_id") != timeline_id:
            return DomainResult.failure(
                ErrorObject(
                    "validation_error",
                    "commit body does not match the resolved timeline scope",
                    {"project_id": project_id, "timeline_id": timeline_id},
                ),
                idempotency_key=key,
            )
        return self._typed(
            "commit_timeline", project_id, timeline_id, body,
            key=key, idempotency_key=key,
        )

    def history(self, project, ref, *, cursor=None, limit=50):
        """Read scoped immutable commit history, including its exact head pair."""
        resolved = self._resolve_timeline(project, ref)
        if not resolved.ok:
            return resolved
        scope = resolved.data
        project_id = scope.get("project_id") if isinstance(scope, Mapping) else None
        timeline_id = scope.get("timeline_id") if isinstance(scope, Mapping) else None
        if not isinstance(project_id, str) or not project_id or not isinstance(timeline_id, str) or not timeline_id:
            return DomainResult.failure(
                ErrorObject("protocol_error", "timeline scope has no canonical project/timeline identity", {})
            )
        return self._typed(
            "list_timeline_commits", project_id, timeline_id,
            cursor=cursor, limit=limit,
        )

    def recover(self, project, ref, *, expected_version=None, version=None, idempotency_key=None):
        key = idempotency_key or uuid.uuid4().hex
        resolved = self._resolve_timeline(project, ref)
        if not resolved.ok or not isinstance(resolved.data, Mapping):
            return DomainResult.failure(resolved.error, idempotency_key=key)
        timeline_id = resolved.data.get("timeline_id")
        if not isinstance(timeline_id, str) or not timeline_id:
            return DomainResult.failure(
                ErrorObject("protocol_error", "timeline listing has no canonical identity", {}),
                idempotency_key=key,
            )
        try:
            value = resolved.data.get("version", resolved.data.get("config_version", 1))
            current_version = int(value)
            expected = int(expected_version) if expected_version is not None else current_version
            target = current_version if version is None else int(version)
        except (TypeError, ValueError):
            return DomainResult.failure(
                ErrorObject("protocol_error", "Runtime timeline listing has no valid version", {}),
                idempotency_key=key,
            )
        return self._typed(
            "recover_timeline",
            timeline_id,
            key=key,
            expected_version=expected,
            version=target,
            idempotency_key=key,
        )
