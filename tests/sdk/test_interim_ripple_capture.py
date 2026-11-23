"""The interim callback lowers existing occurrence timing to guarded patches."""
import pytest

from astrid.sdk.timeline.emitter import DocumentPatchError
from tests.sdk.test_timeline_arrange_commit import arranged_session, runtime_abi  # noqa: F401


@pytest.mark.parametrize("reads", [None, []])
def test_public_callback_captures_placement_only(arranged_session, reads):  # noqa: F811
    timeline, _, calls = arranged_session
    original = timeline.bundle["placements"][0]

    def resize(working):
        working.bundle["placements"][0]["duration_ms"] = 2000

    result = timeline.commit(resize, reads=reads)
    parent = result["data"]["canonical_document"]["parent_revision"]["payload"]
    assert parent["occurrences"][0]["duration_ms"] == 2000
    request, = calls
    command, = request["commands"]
    change, = command["args"]["changes"]
    assert change == {
        "op": "set",
        "ref": "parent",
        "path": ["occurrences", "occurrence_id:" + original["occurrence_id"], "duration_ms"],
        "before": {"state": "present", "value": original["duration_ms"]},
        "value": 2000,
    }
    assert request["merge"] == "atomic"
    assert request["steps"][0]["reads"] == [{"kind": "scope", "ref": "timeline", "path": []}]


def test_interim_timing_preserves_original_refusal_for_managed_payload_links(arranged_session):  # noqa: F811
    timeline, _, calls = arranged_session

    def forge(working):
        placement = working.bundle["placements"][0]
        placement["duration_ms"] += 333
        working.bundle["shots"][placement["shot_id"]]["payload"]["internal_timeline_revision_id"] = "forged"

    with pytest.raises(DocumentPatchError) as caught:
        timeline.commit(forge, dry_run=True)
    assert str(caught.value) == "closure membership or provenance changed; use a semantic command"
    assert not calls
