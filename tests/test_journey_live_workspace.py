"""Direct journey observations and separately retained legacy R0 data."""
import hashlib
import json
import shutil
import sqlite3
import sys
import threading
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from scripts import journey_live_workspace as live
from scripts import journey_version_jumps as jumps
from scripts import journey_workspace as offline


FIXTURE = Path(__file__).parent / "fixtures/upgrade/r0-legacy-timeline"


def test_offline_snapshot_failure_retains_only_allowlisted_field_and_frames(tmp_path, monkeypatch):
    store = types.ModuleType("runtime_protocol.store")
    store.SCHEMA_VERSION = 1
    monkeypatch.setitem(sys.modules, "runtime_protocol", types.ModuleType("runtime_protocol"))
    monkeypatch.setitem(sys.modules, "runtime_protocol.store", store)

    class Service:
        def get_project(self, *_args):
            return {}

    with pytest.raises(KeyError) as caught:
        offline.snapshot(Service(), {})
    diagnostic = offline.failure_diagnostic(caught.value)
    assert diagnostic["schema"] == "astrid.live-workspace-failure.v2"
    assert diagnostic["error_type"] == "KeyError"
    assert diagnostic["missing_field"] == "project"
    assert any(frame["function"] == "snapshot" for frame in diagnostic["frames"])
    assert live.valid_failure_diagnostic(diagnostic)
    assert str(tmp_path) not in json.dumps(diagnostic)

    secret = "/private/runner/.credential/token"
    diagnostic = offline.failure_diagnostic(KeyError(secret))
    assert diagnostic["missing_field"] is None
    assert secret not in json.dumps(diagnostic)
    assert not live.valid_failure_diagnostic({**diagnostic, "missing_field": [secret]})

    run_root = tmp_path / "run"
    evidence = run_root / "evidence" / "release9-then-install"
    source = evidence / "release1-api-observe" / "live-snapshot-failure.json"
    source.parent.mkdir(parents=True)
    source.write_text(json.dumps(offline.failure_diagnostic(caught.value)), encoding="utf-8")
    (evidence / "journey-result.json").write_text(json.dumps({"scenario": "release9-then-install"}),
                                                   encoding="utf-8")
    monkeypatch.setattr(jumps.journey, "capture_runtime_service_logs", lambda *_: None)
    monkeypatch.setattr(jumps.journey, "capture_manager_state", lambda *_: None)
    jumps.journey.snapshot_failure_evidence(evidence, run_root, run_root / "data", "before-cleanup")
    staged = run_root / "evidence-upload" / "before-cleanup" / "live-release1-snapshot-failure.json"
    assert json.loads(staged.read_text(encoding="utf-8")) == offline.failure_diagnostic(caught.value)


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


def test_preservation_checks_public_schema_digest():
    before = {k: {"value": 1} for k in ("project", "timeline", "preferences", "task", "cas_sha256")}
    before.update(schema_digest="sha256:expected", target_schema_digest="sha256:expected")
    after = dict(before)
    jumps.assert_preserved(before, after)
    with pytest.raises(RuntimeError, match="Runtime schema"):
        jumps.assert_preserved(before, {**after, "schema_digest": "sha256:other"})


def test_snapshot_checks_user_data_and_public_schema_without_doctor(monkeypatch):
    payload = b"C1 object release0\n"
    keys = {"project": "p", "timeline": "t", "head": None, "generation": "release0",
            "object_digest": hashlib.sha256(payload).hexdigest(), "task_id": "task",
            "expected_schema_digest": "sha256:" + "9" * 64}
    # The live R0 Runtime contract differs from the R1 checkout's generated contract.
    r0_schema_digest = "sha256:" + "9" * 64
    class Client:
        def request(self, method, path, **_kwargs):
            assert method == "GET"
            if path == "/v1/projects/p":
                return {"project_id": "p", "slug": "p", "name": "project", "metadata": {}}
            if path == "/v1/projects/p/timelines/t":
                return {"name": "User edit release0", "head_revision_id": None}
            if path.startswith("/v1/preferences/project?"):
                return {"content": "Keep user changes release0", "version": 1}
            if path.startswith("/v1/objects/"):
                return payload
            if path == "/v1/tasks/task":
                return {"task_id": "task", "state": "succeeded", "spec": {}, "result": {}}
            if path == "/v1/health":
                return {"protocol": "workspace.v1", "schema_digest": r0_schema_digest}
            raise AssertionError(f"unexpected request: {path}")

    result = live.snapshot(Client(), keys)
    assert result["timeline"] == {"name": "User edit release0", "head": None}
    assert result["preferences"]["content"] == "Keep user changes release0"
    assert result["task"]["state"] == "succeeded"
    assert result["cas_sha256"] == keys["object_digest"]
    assert result["schema_digest"] == result["target_schema_digest"] == r0_schema_digest


def test_snapshot_rejects_runtime_schema_digest_different_from_selected_install():
    keys = {"project": "p", "timeline": "t", "head": None, "generation": "release0",
            "object_digest": hashlib.sha256(b"C1 object release0\n").hexdigest(), "task_id": "task",
            "expected_schema_digest": "sha256:" + "9" * 64}

    class Client:
        def request(self, method, path, **_kwargs):
            if path == "/v1/projects/p":
                return {"project_id": "p", "slug": "p", "name": "project", "metadata": {}}
            if path == "/v1/projects/p/timelines/t":
                return {"name": "User edit release0", "head_revision_id": None}
            if path.startswith("/v1/preferences/project?"):
                return {"content": "Keep user changes release0", "version": 1}
            if path.startswith("/v1/objects/"):
                return b"C1 object release0\n"
            if path == "/v1/tasks/task":
                return {"task_id": "task", "state": "succeeded", "spec": {}, "result": {}}
            if path == "/v1/health":
                return {"protocol": "workspace.v1", "schema_digest": "sha256:" + "8" * 64}
            raise AssertionError(f"unexpected request: {path}")

    with pytest.raises(ValueError, match="Runtime health does not match"):
        live.snapshot(Client(), keys)


def test_live_client_refuses_non_loopback():
    with pytest.raises(ValueError, match="loopback"):
        live.LiveWorkspace("https://example.com", "secret")


def test_failure_diagnostic_rejects_untrusted_fields():
    good = {"schema": "astrid.live-workspace-failure.v1", "error_type": "KeyError",
            "frames": [{"function": "seed", "line": 42}], "http_status": None, "runtime_code": None}
    assert live.valid_failure_diagnostic(good)
    for bad in ({**good, "detail": "secret"}, {**good, "error_type": ["secret"]},
                {**good, "frames": [{"function": "secret", "line": 42}]},
                {**good, "http_status": True}, {**good, "runtime_code": {"secret": 1}}):
        assert not live.valid_failure_diagnostic(bad)


def test_snapshot_failure_diagnostic_retains_only_known_missing_field():
    class Client:
        def request(self, _method, _path):
            return {}

    with pytest.raises(KeyError) as caught:
        live.snapshot(Client(), {"project": "project", "timeline": "timeline", "generation": "release0"})
    diagnostic = live.failure_diagnostic(caught.value)
    assert diagnostic["schema"] == "astrid.live-workspace-failure.v3"
    assert diagnostic["error_type"] == "KeyError"
    assert diagnostic["missing_field"] == "name"
    assert diagnostic["snapshot_lookup"] == {
        "timeline_type": "object",
        "timeline_head_revision_id": {"present": False, "type": None},
        "keys_type": "object",
        "keys_head": {"present": False, "type": None}}
    assert live.valid_failure_diagnostic(diagnostic)

    secret = "/private/runner/.credential/token"
    diagnostic = live.failure_diagnostic(KeyError(secret))
    assert diagnostic["missing_field"] is None
    assert secret not in json.dumps(diagnostic)
    assert not live.valid_failure_diagnostic({**diagnostic, "missing_field": secret})


@pytest.mark.parametrize(
    ("timeline", "keys", "expected_missing", "expected_lookup"),
    [
        ({"head_revision_id": "revision"}, {"project": "p", "timeline": "t", "generation": "release0"}, "name",
         {"timeline_type": "object", "timeline_head_revision_id": {"present": True, "type": "string"},
          "keys_type": "object", "keys_head": {"present": False, "type": None}}),
        ("private timeline response", {"project": "p", "timeline": "t", "head": "private key", "generation": "release0"}, None,
         {"timeline_type": "string", "timeline_head_revision_id": {"present": False, "type": None},
          "keys_type": "object", "keys_head": {"present": True, "type": "string"}}),
    ],
)
def test_snapshot_failure_diagnostic_identifies_each_lookup_without_values(
        timeline, keys, expected_missing, expected_lookup):
    class Client:
        def request(self, _method, path):
            if path == "/v1/projects/p":
                return {}
            if path == "/v1/projects/p/timelines/t":
                return timeline
            raise AssertionError(f"unexpected request: {path}")

    with pytest.raises((KeyError, TypeError)) as caught:
        live.snapshot(Client(), keys)
    diagnostic = live.failure_diagnostic(caught.value)
    assert diagnostic["missing_field"] == expected_missing
    assert diagnostic["snapshot_lookup"] == expected_lookup
    assert "private timeline response" not in json.dumps(diagnostic)
    assert "private key" not in json.dumps(diagnostic)
    assert live.valid_failure_diagnostic(diagnostic)

    unsafe = {**diagnostic, "snapshot_lookup": {**expected_lookup,
              "keys_head": {"present": [True], "type": "string"}}}
    assert not live.valid_failure_diagnostic(unsafe)


def test_snapshot_failure_diagnostic_is_staged_only_when_sanitized(tmp_path):
    from scripts import ci_journey as journey

    evidence = tmp_path / "evidence" / "release9-then-install"
    source = evidence / "release0-api-seed" / "live-snapshot-failure.json"
    source.parent.mkdir(parents=True)
    safe = {"schema": "astrid.live-workspace-failure.v2", "error_type": "KeyError",
            "frames": [{"function": "snapshot", "line": 123}],
            "http_status": None, "runtime_code": None, "missing_field": "head_revision_id"}
    source.write_text(json.dumps(safe))

    journey.retain_named_evidence(evidence, "before-cleanup")
    staged = tmp_path / "evidence-upload" / "before-cleanup" / "live-snapshot-failure.json"
    assert json.loads(staged.read_text()) == safe

    source.write_text(json.dumps({**safe, "missing_field": "credential-secret"}))
    journey.retain_named_evidence(evidence, "before-cleanup")
    assert not staged.exists()


def test_release1_snapshot_failure_diagnostic_is_staged_only_when_sanitized(tmp_path):
    from scripts import ci_journey as journey

    evidence = tmp_path / "evidence" / "release9-then-install"
    source = evidence / "release1-api-observe" / "live-snapshot-failure.json"
    source.parent.mkdir(parents=True)
    safe = {"schema": "astrid.live-workspace-failure.v2", "error_type": "KeyError",
            "frames": [{"function": "snapshot", "line": 123}],
            "http_status": None, "runtime_code": None, "missing_field": "head_revision_id"}
    source.write_text(json.dumps(safe))

    journey.retain_named_evidence(evidence, "before-cleanup")
    staged = tmp_path / "evidence-upload" / "before-cleanup" / "live-release1-snapshot-failure.json"
    assert json.loads(staged.read_text()) == safe

    source.write_text(json.dumps({**safe, "missing_field": "credential-secret"}))
    journey.retain_named_evidence(evidence, "before-cleanup")
    assert not staged.exists()


def test_live_workspace_checks_owner_identity_without_stopping(tmp_path, monkeypatch):
    from scripts import ci_journey as journey

    events = []
    selected = {"realm_root": str(tmp_path / "realm"), "runtime_instance_id": "r0"}
    monkeypatch.setattr(journey, "assert_direct_service_ownership", lambda *_, release: events.append(("owner", release)))
    monkeypatch.setattr(journey, "healthy", lambda *_: events.append("health") or selected)
    monkeypatch.setattr(journey, "workspace_root", lambda *_: tmp_path / "realm")
    manifest_root = tmp_path / "version"
    manifest_root.mkdir()
    (manifest_root / "manifest.json").write_text(json.dumps({"runtime": {"schema_digest": "sha256:" + "9" * 64}}))
    monkeypatch.setattr(journey, "installation", lambda *_: (None, tmp_path, {"root": str(manifest_root)}))
    monkeypatch.setattr(live, "selection_for", lambda *_: selected)
    monkeypatch.setattr(live, "check_selection", lambda *_: events.append("check"))
    def run(argv, *_args, **_kwargs):
        events.append("api")
        assert "journey_live_workspace.py" in str(argv[1])
        assert json.loads(Path(argv[argv.index("--selection") + 1]).read_text()) == selected
        request = json.loads(Path(argv[argv.index("--request") + 1]).read_text())
        assert request["expected_schema_digest"] == "sha256:" + "9" * 64
        assert Path(argv[argv.index("--diagnostic") + 1]).name == "live-snapshot-failure.json"
        return '{"actual": true}'
    monkeypatch.setattr(journey, "run", run)
    assert jumps.live_workspace(tmp_path, tmp_path, tmp_path / "evidence", "snapshot", {}) == {"actual": True}
    assert events == [("owner", 0), "health", "check", "api", "check", ("owner", 0)]


def test_live_workspace_rejects_owner_turnover(tmp_path, monkeypatch):
    from scripts import ci_journey as journey

    selected = {"realm_root": str(tmp_path / "realm")}
    monkeypatch.setattr(journey, "assert_direct_service_ownership", lambda *_, release: None)
    monkeypatch.setattr(journey, "healthy", lambda *_: selected)
    monkeypatch.setattr(journey, "workspace_root", lambda *_: tmp_path / "realm")
    manifest_root = tmp_path / "version"
    manifest_root.mkdir()
    (manifest_root / "manifest.json").write_text(json.dumps({"runtime": {"schema_digest": "sha256:" + "9" * 64}}))
    monkeypatch.setattr(journey, "installation", lambda *_: (None, tmp_path, {"root": str(manifest_root)}))
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
    root, data = tmp_path / "program", tmp_path / "data"
    realm = data / "realm"
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


def test_r1_copy_selection_uses_active_root_and_keeps_r0_owner_binding(tmp_path):
    root = tmp_path / "program"
    r0, r1 = tmp_path / "data", tmp_path / "data-r1"
    realm = r1 / "runtime" / "realms" / "realm"
    root.mkdir()
    (r1 / "runtime").mkdir(parents=True)
    realm.mkdir(parents=True)
    (r0).mkdir()
    (root / "install.json").write_text(json.dumps({
        "data_root": str(r0), "r1_data_root": str(r1)}))
    (tmp_path / "tst-b1-owner.json").write_text(json.dumps({
        "schema_version": 1, "run_id": "b" * 32, "root": str(root), "data_root": str(r0)}))
    credential = r1 / "runtime" / "credentials" / "owner.token"
    credential.parent.mkdir()
    credential.write_text("secret")
    discovery = {"endpoint": "http://127.0.0.1:4321", "realm_root": str(realm),
                 "active_realm": "realm", "runtime_instance_id": "r1", "pid": 12,
                 "process_birth_id": "birth", "credential_file": str(credential)}

    selected = live.selection_for(r1, root, discovery)

    assert selected["realm_root"] == str(realm)
    assert selected["run_id"] == "b" * 32


def test_selected_data_root_prefers_recorded_r1_copy_and_preserves_legacy(tmp_path):
    r0, r1 = tmp_path / "data", tmp_path / "data-r1"
    root = tmp_path / "program"
    root.mkdir()
    marker = root / "install.json"
    marker.write_text(json.dumps({"data_root": str(r0), "r1_data_root": str(r1)}))

    assert jumps.selected_data_root(root, r0) == r1
    assert jumps.selected_data_root(root, r1) == r1
    assert jumps.selected_data_root(root, r1, allow_r1=False) == r0

    marker.write_text(json.dumps({"data_root": str(r0)}))
    assert jumps.selected_data_root(root, r0) == r0
    with pytest.raises(RuntimeError, match="outside this journey"):
        jumps.selected_data_root(root, r1)


def test_existing_project_preferences_are_not_overwritten():
    class Client:
        def request(self, method, path, body=None, **kwargs):
            if method == "POST" and path == "/v1/projects":
                return {"data": {"project_id": "project"}}
            if method == "POST" and path.endswith("/timelines"):
                return {"data": {"timeline_id": "timeline"}}
            if method == "GET" and "/timelines/" in path:
                return {"name": "User edit generation", "head_revision_id": None}
            if method == "GET" and path.startswith("/v1/preferences/project?"):
                return {"content": "existing user content", "version": 1}
            assert method != "PUT"
            return {}
    with pytest.raises(ValueError, match="already has preferences"):
        live.seed(Client(), "project", "generation", create_project=True)


@pytest.mark.parametrize("contents", ["not json", "[]", "{}", '{"token": null}',
                                      '{"token": ""}', '{"token": "bad\\nsecret"}'])
def test_invalid_astrid_credential_is_rejected_without_disclosing_contents(tmp_path, contents):
    credential = tmp_path / "astrid.json"
    credential.write_text(contents)
    with pytest.raises(ValueError, match="^Invalid Astrid credential$") as error:
        live.credential_token(credential)
    assert contents not in str(error.value)


def test_live_seed_failure_is_staged_without_credentials_or_response_body(tmp_path, monkeypatch):
    from scripts import ci_journey as journey

    secret = "a" * 64
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"status":"ok","runtime_instance_id":"instance"}')
        def do_POST(self):
            assert self.headers["Authorization"] == "Bearer " + secret
            self.send_response(422)
            self.end_headers()
            self.wfile.write(json.dumps({"code": "validation_error", "message": secret}).encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        run_root = tmp_path / "run"
        root, data = run_root / "program", run_root / "data"
        realm = data / "realm"
        for directory in (root, data / "runtime", realm):
            directory.mkdir(parents=True)
        credential = data / "token"
        credential.write_text(json.dumps({"version": 1, "scope": "astrid", "actor_id": "astrid-test",
                                          "token": secret}, sort_keys=True, indent=2) + "\n")
        (run_root / "tst-b1-owner.json").write_text(json.dumps({
            "schema_version": 1, "run_id": "a" * 32, "root": str(root), "data_root": str(data)}))
        discovery = {"endpoint": f"http://127.0.0.1:{server.server_port}", "realm_root": str(realm),
                     "active_realm": "realm", "runtime_instance_id": "instance", "pid": 1,
                     "process_birth_id": "birth", "credential_file": str(credential)}
        (data / "runtime/discovery.json").write_text(json.dumps(discovery))
        evidence = run_root / "evidence" / "release9-then-install"
        monkeypatch.setattr(journey, "assert_direct_service_ownership", lambda *_, release: None)
        monkeypatch.setattr(journey, "healthy", lambda *_: discovery)
        monkeypatch.setattr(journey, "workspace_root", lambda *_: realm)
        with pytest.raises(RuntimeError, match="seed.json: exit 1"):
            jumps.live_workspace(root, data, evidence / "release0-api-seed", "seed",
                                 {"project": "p", "generation": "release0", "create_project": True})
        for phase in ("before-cleanup", "after-cleanup"):
            journey.retain_named_evidence(evidence, phase)
            staged = run_root / "evidence-upload" / phase
            diagnostic = json.loads((staged / "live-seed-failure.json").read_text())
            assert diagnostic["error_type"] == "HTTPError"
            assert diagnostic["http_status"] == 422
            assert diagnostic["runtime_code"] == "validation_error"
            assert any(frame["function"] == "seed" for frame in diagnostic["frames"])
            assert all(secret.encode() not in file.read_bytes() for file in staged.iterdir())
            assert not (staged / "seed.json").exists()
        # Reject an unexpected or malformed diagnostic rather than uploading it.
        source = evidence / "release0-api-seed" / "live-seed-failure.json"
        source.write_text(json.dumps({**diagnostic, "runtime_code": secret}))
        journey.retain_named_evidence(evidence, "after-cleanup")
        assert not (run_root / "evidence-upload/after-cleanup/live-seed-failure.json").exists()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
