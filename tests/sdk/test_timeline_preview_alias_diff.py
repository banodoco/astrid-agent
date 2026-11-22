"""Preview diffs tolerate Runtime's canonical local-shot aliases."""
import pytest

from astrid.sdk.timeline.commit_session import Timeline


@pytest.mark.parametrize("after_shots", [
    {"alias-a": {}, "alias-b": {}}, {}, {"physical": {}, "alias": {}},
], ids=["replacement", "removed", "added"])
def test_preview_uses_commands_when_local_shot_keys_change(monkeypatch, after_shots):
    from astrid.sdk.timeline import commit

    before = {"parent": {}, "shots": {"physical": {"payload": {}, "internal_timeline": {}}}}
    after = {"parent": {}, "shots": after_shots}
    command = {"op": "occurrence.place-shared", "select": "occurrence:first",
               "args": {"occurrence_id": "second", "on": 1}}
    result = {"data": {"canonical_document": {"revision": "preview"}, "commands": [command]}}
    monkeypatch.setattr(commit, "bundle_from_document", lambda document: after)

    Timeline._add_preview_diff(result, before)

    assert result["data"]["diff"]["changes"] == [
        {"op": command["op"], "ref": command["select"], "args": command["args"]}
    ]
    result["data"]["diff"]["changes"][0]["args"]["on"] = 99
    assert command["args"]["on"] == 1


def test_preview_does_not_hide_emitter_errors_for_matching_shot_keys(monkeypatch):
    from astrid.sdk.timeline import commit, emitter

    bundle = {"parent": {}, "shots": {"physical": {}}}
    monkeypatch.setattr(commit, "bundle_from_document", lambda document: bundle)

    def fail(*args):
        raise ValueError("invalid physical root")

    monkeypatch.setattr(emitter, "_root_pairs", fail)
    with pytest.raises(ValueError, match="invalid physical root"):
        Timeline._add_preview_diff({"data": {"canonical_document": {}}}, bundle)
