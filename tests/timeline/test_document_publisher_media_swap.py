"""Document publisher coverage for imported footage replacements."""
import copy

import pytest

from astrid.packs.rendering.docs.scripts.timeline_document import emit_document_publish_commands
from astrid.sdk.timeline._command_state import State
from astrid.sdk.timeline.commands import execute
from tests.timeline.test_occurrence_media_swap import (
    CLIP, OWNER, CatalogTransport, _candidate, _legacy_bundle,
)


@pytest.mark.parametrize("kind", ["image", "video"])
def test_document_publisher_imported_swap_preserves_authored_clip(kind):
    base = _legacy_bundle(kind)
    base["base_parent"]["publication_id"] = "publication-1"
    candidate = _candidate(base, kind)
    before = copy.deepcopy(State(base).entity(CLIP))
    verified = []

    commands = emit_document_publish_commands(
        base, candidate, {}, verified, transport=CatalogTransport(kind=kind)
    )
    result = execute(base, commands, verified_media=verified).bundle

    assert {command["op"] for command in commands} == {"doc.patch", "media.admit"}
    assert State(result).entity(CLIP) == {**before, "asset": "new-footage"}
    assert State(result).registry(OWNER)["assets"] == State(candidate).registry(OWNER)["assets"]
    assert State(result).entity("occurrence:occ-b/clip:clip-a") == State(base).entity(
        "occurrence:occ-b/clip:clip-a"
    )
