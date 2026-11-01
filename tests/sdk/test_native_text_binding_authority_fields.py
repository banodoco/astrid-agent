"""Native text binding keeps only already-authenticated Runtime authority."""
from __future__ import annotations

import copy
import hashlib

from astrid.sdk.contracts import DomainResult
from astrid.sdk.timeline._command_state import CommandError, State
from astrid.sdk.timeline.commands import execute
from astrid.sdk.timeline.native_text_binding import capture_text_binding_set
from astrid.sdk.timeline_script import read_composition_script
from tests.editing_conformance.test_recipe_m1_commands import bundle

OWNER = "occurrence:occ-1"
BINDING = {"kind": "voiceover_script", "slot": None}
AUTHORITY_FIELDS = {"binding_id", "head", "event_stream_id"}


def _descriptor(text):
    raw = text.encode("utf-8")
    digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    return {"object_id": digest, "digest": digest, "size": len(raw),
            "media_type": "text/plain; charset=utf-8"}


def _bundle_with_binding(text="Pinned wording"):
    value = bundle(shared=False)
    shot = State(value).occurrence(OWNER)["shot_id"]
    descriptor = _descriptor(text)
    value["shots"][shot]["payload"]["text_bindings"] = [{
        **BINDING, "media_id": descriptor["object_id"],
        "content_hash": descriptor["digest"], "byte_size": descriptor["size"],
        "mime_type": descriptor["media_type"], "binding_id": "runtime-binding", "head": 7,
        "event_stream_id": "runtime-binding:shot.text_binding",
    }]
    return value, descriptor


def test_native_output_feeds_script_reader_with_existing_runtime_authority():
    value, descriptor = _bundle_with_binding()
    command = capture_text_binding_set(value, OWNER, BINDING, text=descriptor,
                                       verified_text=[{**descriptor, "utf8": True}])
    plan = execute(value, [command], verified_media=[{**descriptor, "utf8": True}])
    row = State(plan.bundle).entity(OWNER + "/text-binding:voiceover_script")

    assert {key: row[key] for key in ("binding_id", "head", "event_stream_id")} == {
        "binding_id": "runtime-binding", "head": 7,
        "event_stream_id": "runtime-binding:shot.text_binding",
    }

    class Timelines:
        def inspect(self, *args, **kwargs):
            return DomainResult.success({
                "revision_id": "revision-7", "snapshot_digest": "snapshot-7",
                "selected": [{"role": "target", "occurrence": {
                    "occurrence_id": "occ-1", "text_bindings": [row],
                }}], "next_cursor": None,
            })

    class Transport:
        def get_object(self, object_id):
            assert object_id == descriptor["object_id"]
            return {"data": b"Pinned wording"}

    result = read_composition_script(Timelines(), Transport(), "project", "timeline")
    assert result.ok
    assert result.data["occurrences"][0]["text"] == "Pinned wording"
    assert result.data["occurrences"][0]["bindings"][0]["head"] == 7
    assert result.data["occurrences"][0]["bindings"][0]["provenance"] == "registered_pin"


def test_native_birth_does_not_fabricate_runtime_authority():
    value, descriptor = _bundle_with_binding()
    shot = State(value).occurrence(OWNER)["shot_id"]
    value["shots"][shot]["payload"]["text_bindings"] = []
    command = capture_text_binding_set(value, OWNER, BINDING, text=descriptor,
                                       verified_text=[{**descriptor, "utf8": True}])
    plan = execute(value, [command], verified_media=[{**descriptor, "utf8": True}])
    row = State(plan.bundle).entity(OWNER + "/text-binding:voiceover_script")

    assert not AUTHORITY_FIELDS & row.keys()
    assert "_runtime_document" not in command


def test_native_set_refuses_incomplete_or_wrong_authenticated_pin_without_write():
    value, descriptor = _bundle_with_binding()
    row = value["shots"][State(value).occurrence(OWNER)["shot_id"]]["payload"]["text_bindings"][0]
    row.pop("head")
    before = copy.deepcopy(value)

    try:
        capture_text_binding_set(value, OWNER, BINDING, text=descriptor,
                                 verified_text=[{**descriptor, "utf8": True}])
    except CommandError as error:
        assert error.kind == "unsupported_shape"
    else:
        raise AssertionError("incomplete Runtime authority must be refused")
    assert value == before


def test_native_rebind_drops_stale_authority_for_changed_media_without_write():
    value, _ = _bundle_with_binding()
    replacement = _descriptor("Replacement wording")
    before = copy.deepcopy(value)

    command = capture_text_binding_set(value, OWNER, BINDING, text=replacement,
                                       verified_text=[{**replacement, "utf8": True}])
    plan = execute(value, [command], verified_media=[{**replacement, "utf8": True}])
    row = State(plan.bundle).entity(OWNER + "/text-binding:voiceover_script")

    assert not AUTHORITY_FIELDS & row.keys()
    assert value == before
