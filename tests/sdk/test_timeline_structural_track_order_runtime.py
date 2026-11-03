"""Runtime rejects inverses that reorder E14 structural track history."""
from __future__ import annotations

import copy

import pytest

from astrid.sdk.timeline.sync import CommitConflict
from tests.sdk.test_timeline_editing_acceptance_runtime import (
    _history,
    _media_file,
    _snapshot,
    _voice_file,
)
from tests.sdk.test_timeline_editing_acceptance_runtime import (
    editing_case as editing_case,  # noqa: F401 - pytest fixture registration
)


@pytest.mark.parametrize("mutation", ["native-order", "stage-range"])
def test_e14_track_order_inverse_forgery_is_refused_without_advancing_head(
        editing_case, tmp_path, monkeypatch, mutation):  # noqa: F811 - pytest fixture injection
    from runtime_protocol.timeline_commit_port import RequestCommitPort

    daemon, client, project, timeline = editing_case
    picture = _media_file(tmp_path / "picture.png")
    voice = _voice_file(tmp_path / "voice.wav")
    before = _snapshot(timeline)
    history_before = _history(client, project)

    accepted = timeline.commit(
        lambda t: (
            t.track("V2", above="V1"),
            t.add(picture, at=0, hold=2, track="V2", id="new-picture"),
            t.add(voice, at=0, track="A2", id="new-voice"),
        ),
        label="Picture and voice on new tracks",
        key="e14-picture-voice-" + mutation,
    )
    publication = accepted["receipt"]["receipt_id"]
    assert publication
    assert len(_history(client, project)) == len(history_before) + 1
    accepted_history = _history(client, project)
    accepted_head = daemon.service.get_project_timeline(project, "film")
    accepted_snapshot = _snapshot(timeline)
    assert accepted_snapshot != before

    original_accept = RequestCommitPort.accept
    intercepted = []

    def tampered_accept(port, prepared):
        prepared = copy.deepcopy(prepared)
        records = prepared["records"]
        record = next(command for command in records["commands"]
                      if command["command"]["op"] == "timeline.inverse")
        stages = [binding for binding in record["bindings"]
                  if binding.get("key") == "inverse.stage"]
        assert stages
        intercepted.append(True)

        if mutation == "native-order":
            writes = record["writes"]
            order_index = next(index for index, write in enumerate(writes)
                               if write["ref"] == "parent" and write["path"] == ["config", "tracks"])
            adjacent_roots = [index for index, write in enumerate(writes)
                              if abs(index - order_index) == 1
                              and write["ref"].startswith("parent/track:") and not write["path"]]
            assert adjacent_roots
            root_index = adjacent_roots[0]
            writes[order_index], writes[root_index] = writes[root_index], writes[order_index]
        else:
            boundary = stages[0]["boundary"]
            start, end = boundary["native_write_range"]
            assert start < end
            boundary["native_write_range"] = [start, end - 1]

        return original_accept(port, prepared)

    monkeypatch.setattr(RequestCommitPort, "accept", tampered_accept)
    with pytest.raises(CommitConflict) as error:
        timeline.undo(publications=[publication])

    assert intercepted
    assert error.value.failure["details"]["reason"] == "invalid_records"
    assert _history(client, project) == accepted_history
    assert daemon.service.get_project_timeline(project, "film") == accepted_head
    assert _snapshot(timeline) == accepted_snapshot
