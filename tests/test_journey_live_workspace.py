"""Direct journey observations and separately retained legacy R0 data."""
import hashlib
import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from scripts import journey_live_workspace as live
from scripts import journey_version_jumps as jumps


FIXTURE = Path(__file__).parent / "fixtures/upgrade/r0-legacy-timeline"


def test_authentic_r0_legacy_document_survives_offline_migration(tmp_path):
    from runtime_protocol import upgrade
    expected = json.loads((FIXTURE / "expected.json").read_text())
    source = FIXTURE / "realm.sqlite3"
    assert hashlib.sha256(source.read_bytes()).hexdigest() == expected["database_sha256"]
    realm = tmp_path / "realm"
    realm.mkdir()
    shutil.copyfile(source, realm / "realm.sqlite3")
    identity = upgrade.inspect_canonical_schema(realm)
    assert identity["version"] == expected["source_schema"] == 31
    result = upgrade.migrate_canonical_to_current(
        realm, confirmation="MIGRATE CANONICAL " + identity["realm_id"])
    assert result["source_schema_version"] == 31
    assert result["target_schema_version"] > 31
    with sqlite3.connect(realm / "realm.sqlite3") as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT id, project_id, kind, content_json, version FROM project_documents WHERE id=?",
            ("timeline:legacy-film",)).fetchone()
        assert dict(row) == expected["document"]
        assert connection.execute("SELECT version FROM runtime_schema WHERE id=1").fetchone()[0] == result["target_schema_version"]


def test_preservation_rejects_changed_data_and_wrong_schema():
    before = {k: {"value": 1} for k in ("project", "timeline", "preferences", "task", "cas_sha256")}
    before.update(schema_version=31, target_schema_version=31)
    after = {**before, "schema_version": 33, "target_schema_version": 33}
    jumps.assert_preserved(before, after)
    for key in ("project", "timeline", "preferences", "task", "cas_sha256"):
        with pytest.raises(RuntimeError, match="changed seeded workspace"):
            jumps.assert_preserved(before, {**after, key: {"value": 2}})
    with pytest.raises(RuntimeError, match="Runtime schema"):
        jumps.assert_preserved(before, {**after, "schema_version": 31})


def test_live_client_refuses_non_loopback():
    with pytest.raises(ValueError, match="loopback"):
        live.LiveWorkspace("https://example.com", "secret")


def test_live_workspace_checks_owner_identity_without_stopping(tmp_path, monkeypatch):
    from scripts import ci_journey as journey

    events = []
    selected = {"realm_root": str(tmp_path / "realm"), "runtime_instance_id": "r0"}
    monkeypatch.setattr(journey, "assert_direct_service_ownership", lambda *_: events.append("owner"))
    monkeypatch.setattr(journey, "healthy", lambda *_: events.append("health") or selected)
    monkeypatch.setattr(journey, "workspace_root", lambda *_: tmp_path / "realm")
    monkeypatch.setattr(live, "selection_for", lambda *_: selected)
    monkeypatch.setattr(live, "check_selection", lambda *_: events.append("check"))
    def run(argv, *_args, **_kwargs):
        events.append("api")
        assert "journey_live_workspace.py" in str(argv[1])
        assert json.loads(Path(argv[argv.index("--selection") + 1]).read_text()) == selected
        return '{"actual": true}'
    monkeypatch.setattr(journey, "run", run)
    assert jumps.live_workspace(tmp_path, tmp_path, tmp_path / "evidence", "snapshot", {}) == {"actual": True}
    assert events == ["owner", "health", "check", "api", "check", "owner"]


def test_live_workspace_rejects_owner_turnover(tmp_path, monkeypatch):
    from scripts import ci_journey as journey

    selected = {"realm_root": str(tmp_path / "realm")}
    monkeypatch.setattr(journey, "assert_direct_service_ownership", lambda *_: None)
    monkeypatch.setattr(journey, "healthy", lambda *_: selected)
    monkeypatch.setattr(journey, "workspace_root", lambda *_: tmp_path / "realm")
    monkeypatch.setattr(live, "selection_for", lambda *_: selected)
    calls = iter((None, RuntimeError("Runtime owner changed")))
    def check(*_):
        result = next(calls)
        if result:
            raise result
    monkeypatch.setattr(live, "check_selection", check)
    monkeypatch.setattr(journey, "run", lambda *_args, **_kwargs: '{}')
    with pytest.raises(RuntimeError, match="owner changed"):
        jumps.live_workspace(tmp_path, tmp_path, tmp_path / "evidence", "snapshot", {})


def test_selection_rejects_changed_discovery_before_mutation(tmp_path, monkeypatch):
    root, data, realm = (tmp_path / name for name in ("program", "data", "realm"))
    for directory in (root, data / "runtime", realm):
        directory.mkdir(parents=True)
    (tmp_path / "tst-b1-owner.json").write_text(json.dumps({
        "schema_version": 1, "run_id": "a" * 32, "root": str(root), "data_root": str(data)}))
    credential = data / "token"
    credential.write_text("secret")
    discovery = {"endpoint": "http://127.0.0.1:1234", "realm_root": str(realm),
                 "active_realm": "realm", "runtime_instance_id": "instance", "pid": 1,
                 "process_birth_id": "birth", "credential_file": str(credential)}
    selected = live.selection_for(data, root, discovery)
    for field in live.SELECTION_FIELDS:
        changed = {**discovery, field: "replacement"}
        (data / "runtime/discovery.json").write_text(json.dumps(changed))
        with pytest.raises(RuntimeError, match="owner changed"):
            live.check_selection(data, root, selected)
    (data / "runtime/discovery.json").write_text(json.dumps(discovery))
    credential.write_text("replacement")
    with pytest.raises(RuntimeError, match="owner changed"):
        live.check_selection(data, root, selected)


def test_existing_project_preferences_are_not_overwritten():
    class Client:
        def request(self, method, path, body=None, **kwargs):
            if method == "POST" and path == "/v1/projects":
                return {"data": {"project_id": "project"}}
            if method == "GET" and path.startswith("/v1/preferences/project?"):
                return {"content": "existing user content", "version": 1}
            assert method != "PUT"
            return {}
    with pytest.raises(ValueError, match="already has preferences"):
        live.seed(Client(), "project", "generation", create_project=True)
