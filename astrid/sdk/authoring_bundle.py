"""Lazy SDK-facing adapter for the canonical timeline authoring bundle.

The bundle contract and compiler remain owned by ``astrid.core.timeline``;
this module only keeps the public SDK surface transport-friendly and avoids a
second representation or persistence path.
"""

from astrid.core.timeline.authoring_bundle import (
    approve_authoring_candidate,
    authoring_media_inventory,
    compile_authoring_candidate,
    diff_authoring_candidate,
    format_authoring_inspection,
    inspect_authoring_candidate,
    open_authoring_bundle,
    preview_authoring_candidate,
    publish_authoring_candidate,
    promote_approved_authoring_candidate,
    validate_authoring_candidate,
)

from astrid.core.timeline.authoring_diff_format import format_authoring_revision_diff
from .authoring_media import import_authoring_media, plan_authoring_media

from .authoring_render_preview import render_authoring_candidate_preview
from .authoring_remote import AuthoringRemoteError, TargetBoundAuthoringBundle

__all__ = [
    "authoring_media_inventory",
    "AuthoringRemoteError",
    "approve_authoring_candidate",
    "compile_authoring_candidate",
    "diff_authoring_candidate",
    "format_authoring_inspection",
    "format_authoring_revision_diff",
    "plan_authoring_media",
    "import_authoring_media",
    "inspect_authoring_candidate",
    "open_authoring_bundle",
    "preview_authoring_candidate",
    "render_authoring_candidate_preview",
    "publish_authoring_candidate",
    "promote_approved_authoring_candidate",
    "validate_authoring_candidate",
    "TargetBoundAuthoringBundle",
]
