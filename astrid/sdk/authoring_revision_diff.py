"""Read exact canonical closures for the existing timelines.diff operation."""

from collections.abc import Mapping

from astrid.core.timeline.authoring_bundle import AuthoringBundleError, open_authoring_bundle

from .contracts import DomainResult, ErrorObject


def read_authoring_revision(reader, project_id, timeline_id, revision_id):
    """Hydrate only the requested pins; validate scope and immutable bytes."""
    def read(operation, *args, expected):
        result = reader._typed(operation, *args)
        if not result.ok:
            return DomainResult.failure(ErrorObject(
                result.error.code, result.error.message,
                {**dict(result.error.details), "requested_parent_revision": revision_id,
                 "dependency": operation, "expected": expected},
            ))
        row = result.data
        if not isinstance(row, Mapping) or any(row.get(key) != value for key, value in expected.items()):
            return DomainResult.failure(ErrorObject(
                "integrity_error", "immutable revision response does not match requested scope or pin",
                {"requested_parent_revision": revision_id, "dependency": operation, "expected": expected},
            ))
        return result

    parent = read("get_project_parent_composition_revision", project_id, timeline_id, revision_id,
                  expected={"project_id": project_id, "timeline_id": timeline_id, "revision_id": revision_id})
    if not parent.ok:
        return parent
    shots, internals = {}, {}
    try:
        payload = parent.data.get("payload")
        if not isinstance(payload, Mapping) or not isinstance(payload.get("occurrences"), list):
            raise AuthoringBundleError("parent revision has no complete occurrences list")
        for occurrence in payload["occurrences"]:
            if not isinstance(occurrence, Mapping):
                raise AuthoringBundleError("invalid pinned occurrence")
            shot_id = occurrence.get("shot_id")
            shot_revision = occurrence.get("shot_revision_id")
            if not isinstance(shot_id, str) or not isinstance(shot_revision, str):
                raise AuthoringBundleError("occurrence is missing its exact shot pin")
            if shot_revision not in shots:
                result = read("get_project_shot_revision", project_id, shot_id, shot_revision,
                              expected={"project_id": project_id, "shot_id": shot_id, "revision_id": shot_revision})
                if not result.ok:
                    return result
                shots[shot_revision] = result.data
            elif shots[shot_revision].get("shot_id") != shot_id:
                raise AuthoringBundleError("shared revision pin has conflicting shot identity")
            shot = shots[shot_revision]
            shot_payload = shot.get("payload", {})
            if not isinstance(shot_payload, Mapping):
                raise AuthoringBundleError("shot revision has no payload object")
            internal_revision = shot.get("internal_timeline_revision_id") or shot_payload.get("internal_timeline_revision_id")
            if not isinstance(internal_revision, str) or not internal_revision:
                raise AuthoringBundleError("shot is missing its exact internal timeline pin")
            if internal_revision not in internals:
                result = read("get_project_timeline_revision", project_id, timeline_id, internal_revision,
                              expected={"project_id": project_id, "timeline_id": timeline_id, "revision_id": internal_revision})
                if not result.ok:
                    return result
                internals[internal_revision] = result.data
        bundle = open_authoring_bundle(parent.data, shot_revisions=shots,
                                       internal_timeline_revisions=internals)
        # Authoring normalization removes source-only fields and remaps shared
        # items. Keep the verified read records in memory for exact raw details.
        bundle["immutable_closure"] = {
            "parent": parent.data, "shots": shots, "internal_timelines": internals,
        }
    except AuthoringBundleError as exc:
        return DomainResult.failure(ErrorObject("integrity_error", str(exc), {
            "project_id": project_id, "timeline_id": timeline_id, "revision_id": revision_id,
        }))
    return DomainResult.success(bundle)
