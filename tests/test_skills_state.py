"""Install records retain exact source evidence without changing legacy records."""
from astrid.skills import state


def test_source_identity_roundtrips_and_changes_only_when_evidence_changes(tmp_path):
    path = tmp_path / "skills.json"
    data = state.load(path)
    state.record_install(data, "claude", "demo", target="/harness/skills/astrid-demo", mechanism="symlink", source="/declared/skill")
    state.save(data, path)
    first = path.read_bytes()
    loaded = state.load(path)
    state.record_install(loaded, "claude", "demo", target="/harness/skills/astrid-demo", mechanism="symlink", source="/declared/skill")
    state.save(loaded, path)
    assert path.read_bytes() == first
    assert loaded["installs"]["claude"]["demo"]["source"] == "/declared/skill"
    state.record_install(loaded, "claude", "demo", target="/harness/skills/astrid-demo", mechanism="symlink", source="/new/skill")
    assert loaded["installs"]["claude"]["demo"]["source"] == "/new/skill"


def test_legacy_destination_only_record_is_not_promoted_to_source_evidence(tmp_path):
    path = tmp_path / "skills.json"
    data = state.load(path)
    state.record_install(data, "claude", "demo", target="/harness/skills/astrid-demo", mechanism="symlink")
    state.save(data, path)
    assert "source" not in state.load(path)["installs"]["claude"]["demo"]
