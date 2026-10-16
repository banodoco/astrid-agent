"""Local consumer contract fixtures only; these do not attest Runtime acceptance."""
from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest

from astrid.packs.timeline import cli
from astrid.sdk.contracts import DomainResult
from astrid.sdk.remote import RemoteTimelines
from astrid.sdk.workspace_client import WorkspaceClient, WorkspaceClientError


def target(timeline="dup"):
    return {"timeline_id": timeline, "expected_version": 3,
            "expected_revision_id": None, "expected_publication_id": None}


def plan():
    return {"project_id": "p", "realm_id": "realm", "targets": [
        {**target(), "archived": True, "provenance": {"timeline_id": "source"}}
    ], "plan_digest": "sha256:fixture", "eligible": False,
        "blockers": [{"code": "external_reference", "timeline_id": "dup", "table": "tasks", "row_id": "task"}],
        "deletion_counts": {"parent_revisions": 4}, "reclaimable_payload_bytes": 517}


class RecordingClient:
    def __init__(self):
        self.calls = []
        self.refusal = None

    def plan_timeline_purge(self, project, targets):
        self.calls.append(("plan", project, copy.deepcopy(targets)))
        return plan()

    def execute_timeline_purge(self, project, targets, **options):
        self.calls.append(("execute", project, copy.deepcopy(targets), options))
        if self.refusal:
            raise self.refusal
        # Adapter-only recording response. No fake deletion/eligibility decision.
        return {"status": "adapter_fixture_only"}

    def archive_timeline(self, timeline, **options):
        self.calls.append(("archive", timeline, options))
        return {"timeline_id": timeline}


def test_plan_only_passes_explicit_exact_set_without_listing():
    client = RecordingClient()
    targets = [target("dup-b"), target("dup-a")]
    result = RemoteTimelines(client).plan_purge("p", targets)
    assert result.ok and result.data == plan()
    assert client.calls == [("plan", "p", targets)]
    assert result.idempotency_key == ""  # planning is a read


@pytest.mark.parametrize("targets", [[], "dup", [target(), target()], [{"timeline_id": "dup"}],
    [{**target(), "slug": "trial"}], [{**target(), "expected_version": True}],
    [{**target(), "expected_revision_id": ""}]])
def test_malformed_or_implicit_target_sets_do_not_call_runtime(targets):
    client = RecordingClient()
    assert not RemoteTimelines(client).plan_purge("p", targets).ok
    assert client.calls == []


def test_execute_preserves_full_plan_target_order_digest_and_retry_identity():
    client = RecordingClient()
    remote = RemoteTimelines(client)
    saved = plan()
    saved["targets"].append({**target("dup-2"), "provenance": {"timeline_id": "source"}})
    before = copy.deepcopy(saved)
    for _ in range(2):
        result = remote.execute_purge("p", saved, idempotency_key="purge-attempt-1")
        assert result.ok and result.idempotency_key == "purge-attempt-1"
    assert saved == before
    assert client.calls == [("execute", "p", saved["targets"], {
        "plan_digest": saved["plan_digest"], "idempotency_key": "purge-attempt-1"})] * 2


@pytest.mark.parametrize("code", ["conflict", "forbidden"])
def test_stale_plan_or_authority_refusal_is_preserved_without_replanning(code):
    client = RecordingClient()
    details = {"blockers": [{"code": "stale_plan"}], "plan": plan()}
    client.refusal = WorkspaceClientError(409 if code == "conflict" else 403, code, "refused", details)
    result = RemoteTimelines(client).execute_purge("p", plan(), idempotency_key="same-key")
    assert not result.ok and result.error.code == code and result.error.details == details
    assert len(client.calls) == 1 and client.calls[0][0] == "execute"
    assert result.idempotency_key == "same-key"


@pytest.mark.parametrize("project,saved,key", [("other", plan(), "key"), ("p", {}, "key"),
    ("p", {**plan(), "plan_digest": ""}, "key"), ("p", plan(), "")])
def test_execute_requires_exact_project_plan_digest_and_explicit_key(project, saved, key):
    client = RecordingClient()
    assert not RemoteTimelines(client).execute_purge(project, saved, idempotency_key=key).ok
    assert client.calls == []


def test_archive_omits_withdrawal_by_default_and_forwards_explicit_pins():
    client = RecordingClient()
    remote = RemoteTimelines(client)
    assert remote.archive("p", "dup", expected_version=2, idempotency_key="a").ok
    assert client.calls == [("archive", "dup", {"expected_version": 2, "idempotency_key": "a"})]
    pins = {"expected_revision_id": "rev", "expected_publication_id": "pub"}
    assert remote.archive("p", "dup", expected_version=2, idempotency_key="b", withdraw_publication=pins).ok
    assert client.calls[-1][2] == {"expected_version": 2, "idempotency_key": "b", "withdraw_publication": pins}


@pytest.mark.parametrize("version,pins", [(None, {"expected_revision_id": "rev", "expected_publication_id": "pub"}),
    (2, {}), (2, {"expected_revision_id": "rev", "expected_publication_id": None})])
def test_withdrawal_never_infers_missing_head_or_version(version, pins):
    client = RecordingClient()
    assert not RemoteTimelines(client).archive("p", "dup", expected_version=version, withdraw_publication=pins).ok
    assert client.calls == []


def run_cli(client, args):
    parsed = cli.build_parser(SimpleNamespace(timelines=RemoteTimelines(client))).parse_args(args)
    return parsed.handler(parsed)


def test_cli_displays_structured_blockers_counts_and_saves_exact_plan(tmp_path, capsys):
    client = RecordingClient()
    inputs, output = tmp_path / "targets.json", tmp_path / "plan.json"
    inputs.write_text(json.dumps([target()]))
    assert run_cli(client, ["purge-plan", "--project", "p", "--targets", str(inputs), "--output", str(output)]) == 0
    displayed = json.loads(capsys.readouterr().out)["data"]
    assert displayed == json.loads(output.read_text()) == plan()
    assert displayed["blockers"][0]["table"] == "tasks" and displayed["deletion_counts"] == {"parent_revisions": 4}
    for _ in range(2):
        assert run_cli(client, ["purge-execute", "--project", "p", "--plan", str(output), "--idempotency-key", "fixed"]) == 0
    assert client.calls[-1] == client.calls[-2]
    assert client.calls[-1][2] == displayed["targets"]


def test_cli_missing_execution_key_and_withdrawal_pins_cannot_mutate(capsys):
    client = RecordingClient()
    with pytest.raises(SystemExit):
        run_cli(client, ["purge-execute", "--project", "p", "--plan", "unused"])
    assert run_cli(client, ["archive", "dup", "--project", "p", "--withdraw-publication", "--json"]) != 0
    assert client.calls == []


def test_cli_ordinary_archive_and_explicit_withdrawal(capsys):
    client = RecordingClient()
    assert run_cli(client, ["archive", "dup", "--project", "p", "--expected-version", "3", "--idempotency-key", "normal"]) == 0
    assert "withdraw_publication" not in client.calls[-1][2]
    assert run_cli(client, ["archive", "dup", "--project", "p", "--withdraw-publication", "--expected-version", "3",
                           "--expected-revision-id", "rev", "--expected-publication-id", "pub", "--idempotency-key", "withdraw"]) == 0
    assert client.calls[-1][2]["withdraw_publication"] == {"expected_revision_id": "rev", "expected_publication_id": "pub"}


class CreateClient:
    def __init__(self, revision="rev"):
        self.revision = revision
        self.calls = []

    def get_timeline(self, timeline, *, project_id):
        return {"project_id": project_id, "timeline_id": timeline, "head_revision_id": self.revision, "head_publication_id": "pub"}

    def create_timeline(self, project, timeline, **options):
        self.calls.append((project, timeline, options))
        return {"project_id": project, "timeline_id": timeline}

    def publish_parent_composition(self, project, timeline, publication, **options):
        return {"new_head": "empty"}


def test_duplicate_create_pins_checked_revision_and_current_publication_on_retry():
    client = CreateClient()
    remote = RemoteTimelines(client)
    for _ in range(2):
        assert remote.create_empty(project="p", timeline_id="dup", idempotency_key="stable",
            duplicate_source={"timeline_id": "source", "expected_revision_id": "rev"}).ok
    assert client.calls == [("p", "dup", {"idempotency_key": "stable-shell", "duplicate_source": {
        "timeline_id": "source", "expected_revision_id": "rev", "expected_publication_id": "pub"}})] * 2


def test_source_head_mismatch_refuses_before_create():
    client = CreateClient("changed")
    result = RemoteTimelines(client).create_empty(project="p", timeline_id="dup", duplicate_source={"timeline_id": "source", "expected_revision_id": "rev"})
    assert not result.ok and result.error.code == "conflict" and client.calls == []


def test_duplicate_flow_supplies_validated_source_identity_and_keeps_notes(monkeypatch):
    from astrid.sdk import timeline_duplicate as td

    src = SimpleNamespace(bundle={"project_id": "p", "timeline_id": "source-id", "base_parent": {"revision_id": "rev"}},
                          check=lambda: SimpleNamespace(valid=True))
    dst = SimpleNamespace(bundle={}, notes=[], check=lambda: SimpleNamespace(valid=True),
                          publish=lambda *a, **k: {"old_head": "empty", "new_head": "copied"})
    monkeypatch.setattr(td.Checkout, "open", lambda p, t, client: src if t == "source-slug" else dst)
    monkeypatch.setattr(td, "copy_content", lambda *a, **k: ({"shots": {"new": {}}}, ["narration rebound"]))
    calls = []
    def create(**kwargs):
        calls.append(kwargs)
        return DomainResult.success({"timeline_id": "dup"})
    result = td.duplicate_timeline("project-slug", "source-slug", slug="dup", client=SimpleNamespace(timelines=SimpleNamespace(create_empty=create)))
    assert calls[0]["project"] == "p"
    assert calls[0]["duplicate_source"] == {"timeline_id": "source-id", "expected_revision_id": "rev"}
    assert result["notes"] == ["narration rebound"] and result["new_head"] == "copied"


def test_workspace_facade_forwards_new_contract_and_out_of_band_receipt():
    from banodoco_workspace_client.generated import MutationResult

    calls = []
    class Generated:
        def __getattr__(self, name):
            def record(*args, **kwargs):
                calls.append((name, args, kwargs))
                return plan() if name == "plan_timeline_purge" else MutationResult({"status": "adapter_fixture_only"}, {"receipt_id": "receipt"})
            return record
    facade = object.__new__(WorkspaceClient)
    facade._generated = Generated()
    pins = {"timeline_id": "source", "expected_revision_id": "rev", "expected_publication_id": "pub"}
    assert facade.create_timeline("p", "dup", idempotency_key="c", duplicate_source=pins)["receipt"] == {"receipt_id": "receipt"}
    facade.archive_timeline("dup", expected_version=3, idempotency_key="a", withdraw_publication={k: v for k, v in pins.items() if k != "timeline_id"})
    assert facade.plan_timeline_purge("p", [target()]) == plan()
    result = facade.execute_timeline_purge("p", plan()["targets"], plan_digest="digest", idempotency_key="e")
    assert result == {"data": {"status": "adapter_fixture_only"}, "receipt": {"receipt_id": "receipt"}}
    assert calls[0] == ("create_timeline", ("p", "dup"), {"idempotency_key": "c", "duplicate_source": pins})
    assert calls[-1] == ("execute_timeline_purge", ("p", plan()["targets"]), {"plan_digest": "digest", "idempotency_key": "e"})


def test_changed_plan_is_sent_with_original_key_and_runtime_refusal_preserved():
    client = RecordingClient()
    remote = RemoteTimelines(client)
    saved = plan()
    remote.execute_purge("p", saved, idempotency_key="original")
    saved["targets"][0]["expected_version"] += 1
    client.refusal = WorkspaceClientError(409, "idempotency_conflict", "request changed", {"plan_digest": saved["plan_digest"]})
    result = remote.execute_purge("p", saved, idempotency_key="original")
    assert not result.ok and result.error.code == "idempotency_conflict"
    assert client.calls[-1][3]["idempotency_key"] == "original"
    assert client.calls[-1][2] == saved["targets"] and len(client.calls) == 2


def test_duplicate_runtime_race_refusal_does_not_publish_or_retry_with_new_identity():
    class Refused(CreateClient):
        def create_timeline(self, *args, **kwargs):
            self.calls.append((args, kwargs))
            raise WorkspaceClientError(409, "conflict", "source publication changed", {"actual_publication_id": "new-pub"})

        def publish_parent_composition(self, *args, **kwargs):
            pytest.fail("a refused create must not publish")

    client = Refused()
    result = RemoteTimelines(client).create_empty(project="p", timeline_id="dup", idempotency_key="same",
        duplicate_source={"timeline_id": "source", "expected_revision_id": "rev", "expected_publication_id": "pub"})
    assert not result.ok and result.error.details == {"actual_publication_id": "new-pub"}
    assert result.idempotency_key == "same-shell" and len(client.calls) == 1


def test_cli_invalid_json_refuses_without_a_runtime_call(tmp_path, capsys):
    client = RecordingClient()
    path = tmp_path / "bad.json"
    path.write_text("{broken")
    assert run_cli(client, ["purge-plan", "--project", "p", "--targets", str(path)]) == 2
    assert client.calls == [] and "could not read retention JSON" in capsys.readouterr().err
