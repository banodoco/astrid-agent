"""The per-shot plan (``payload.metadata.plan``): validation, set/merge/show/list, visualize labels, archive."""
from __future__ import annotations

import json
import re
import zipfile
from types import SimpleNamespace

import pytest

from astrid.sdk.contracts import DomainResult
from astrid.sdk.shot_plan import (
    ShotPlanError,
    apply_assignments,
    merge_plan,
    plan_label,
    plan_rows,
    resolve_shot,
    validate_plan,
)
from tests.sdk.test_timeline_restructure import restructure_runtime  # noqa: F401

FULL = {
    "version": 1, "on_screen": ["Morpheus"], "line": "You have two options.", "intent": "new line, real face",
    "method": ["LM", "R"], "check": "mouth follows the voice track",
    "inputs": {"references": ["sha256:" + "a" * 64], "voice": {"media": "sha256:" + "b" * 64, "range": [5.0, 8.0]},
               "masks": ["sha256:" + "c" * 64]},
    "status": "planned", "takes": [{"generation": "run-1", "media": "sha256:" + "d" * 64, "note": "first"}],
}


# -- validation ------------------------------------------------------------------


def test_full_plan_validates_and_version_is_filled_in():
    assert validate_plan(FULL) == FULL
    assert validate_plan({"method": ["K"]}) == {"version": 1, "method": ["K"]}
    # A kept shot is approved without a take.
    assert validate_plan({"method": ["K"], "status": "approved"})["status"] == "approved"


@pytest.mark.parametrize("plan,message", [
    ({"methods": ["LM"]}, "unknown key(s) under plan: methods"),
    ({"method": ["LX"]}, "unknown code(s) LX"),
    ({"method": ["V", "V"]}, "lists a code twice"),
    ({"status": "done"}, "plan.status must be one of planned, in_progress, take_ready, approved"),
    ({"version": 2}, "plan.version must be 1"),
    ({"inputs": {"voice": {"media": "m", "range": [8, 5]}}}, "seconds into inputs.voice.media (not timeline time)"),
    ({"inputs": {"voice": {"range": [1, 2]}}}, "range needs plan.inputs.voice.media"),
    ({"method": []}, "plan.method must name at least one code"),
    ({"method": ["K", "R"]}, "K (keep) stands alone"),
    ({"method": ["LM"], "status": "take_ready"}, "plan.status take_ready needs at least one take"),
    ({"method": ["LM"], "status": "approved", "takes": []}, "plan.status approved needs at least one take"),
    ({"method": ["K"], "status": "take_ready"}, "plan.status take_ready needs at least one take"),
    ({"inputs": {"refs": []}}, "unknown key(s) under plan.inputs: refs"),
    ({"inputs": {"voice": {"clip": "x"}}}, "unknown key(s) under plan.inputs.voice: clip"),
    ({"takes": [{"note": "no media"}]}, "needs a generation or a media id"),
    ({"takes": [{"media": "m", "score": 3}]}, "unknown key(s) under plan.takes[0]: score"),
    ({"line": 3}, "plan.line must be a string"),
    ([], "plan must be a JSON object"),
])
def test_invalid_plans_are_refused_with_a_clear_message(plan, message):
    with pytest.raises(ShotPlanError, match=re.escape(message)):
        validate_plan(plan)


def test_merge_keeps_other_fields_merges_inputs_unsets_and_appends_takes():
    current = {"version": 1, "method": ["LM"], "inputs": {"references": ["r1"]}, "status": "planned"}
    merged = merge_plan(current, {"inputs": {"voice": {"media": "v", "range": [1, 2]}}, "status": "in_progress"})
    assert merged == {"version": 1, "method": ["LM"], "status": "in_progress",
                      "inputs": {"references": ["r1"], "voice": {"media": "v", "range": [1, 2]}}}
    assert merge_plan(merged, {"inputs": {"voice": {"range": [1, 3]}}})["inputs"]["voice"] == {"media": "v", "range": [1, 3]}
    assert "voice" not in merge_plan(merged, unset=["inputs.voice"])["inputs"]
    from astrid.sdk.shot_plan import combine_patches
    assert combine_patches({"inputs": {"voice": {"media": "v"}, "references": ["r"]}},
                           {"inputs": {"voice": {"range": [1, 2]}}}) == {
        "inputs": {"voice": {"media": "v", "range": [1, 2]}, "references": ["r"]}}
    taken = merge_plan(merged, {"status": "take_ready"}, add_takes=[{"generation": "g1"}])
    assert merge_plan(taken, add_takes=[{"media": "m2"}])["takes"] == [{"generation": "g1"}, {"media": "m2"}]
    assert merge_plan(merged, {"method": ["K"]}, replace=True) == {"version": 1, "method": ["K"]}
    assert current == {"version": 1, "method": ["LM"], "inputs": {"references": ["r1"]}, "status": "planned"}
    with pytest.raises(ShotPlanError, match="unknown key"):
        merge_plan(current, {"owner": "x"})
    with pytest.raises(ShotPlanError, match="cannot unset 'version'"):
        merge_plan(current, unset=["version"])


def test_set_assignments_parse_by_field():
    assert apply_assignments(["method=lm, r", "status=planned", "line=Hello, you.", "on_screen=A,B",
                              "inputs.voice.range=12.2,16.5", "inputs.voice.media=v"]) == {
        "method": ["LM", "R"], "status": "planned", "line": "Hello, you.", "on_screen": ["A", "B"],
        "inputs": {"voice": {"range": [12.2, 16.5], "media": "v"}}}
    with pytest.raises(ShotPlanError, match="PATH one of"):
        apply_assignments(["owner=me"])
    with pytest.raises(ShotPlanError, match="START,END"):
        apply_assignments(["inputs.voice.range=a,b"])


def test_rows_resolve_and_label_read_the_shot_payload_only():
    bundle = {"placements": [
        {"occurrence_id": "o2", "shot_id": "s2", "placement": {"start_ms": 5000}, "duration_ms": 3000},
        {"occurrence_id": "o1", "shot_id": "s1", "placement": {"start_ms": 0}, "duration_ms": 5000},
        {"occurrence_id": "o3", "shot_id": "s2", "placement": {"start_ms": 8000}, "duration_ms": 1000}],
        "shots": {"s1": {"payload": {"name": "Logo", "metadata": {"other": 1}}},
                  "s2": {"payload": {"name": "Morpheus MS", "metadata": {"plan": FULL, "other": 2},
                                     "text_bindings": [{"kind": "voiceover_script", "binding_id": "b"}]}}}}
    rows = plan_rows(bundle)
    assert [(r["occurrence_id"], r["shared"], r["voiceover_script"]) for r in rows] == [
        ("o1", False, False), ("o2", False, True), ("o3", True, True)]
    assert rows[0]["plan"] is None and rows[1]["plan"] == FULL
    assert resolve_shot(bundle, "s1") == "s1"
    assert resolve_shot(bundle, "o3") == "s2"
    assert resolve_shot(bundle, "Morpheus MS") == "s2"
    with pytest.raises(ShotPlanError, match="no shot 'nope'.*s1 \\(Logo\\)"):
        resolve_shot(bundle, "nope")
    assert plan_label(FULL) == 'LM+R · planned · 1 take · "You have two options."'
    assert plan_label(None) == "no plan"


def test_exact_id_wins_and_ambiguous_names_are_refused():
    bundle = {"placements": [], "shots": {
        "a": {"payload": {"name": "Same"}}, "b": {"payload": {"name": "Same"}},
        "Same": {"payload": {"name": "Other"}}, "c": {"payload": {"name": "a"}}}}
    assert resolve_shot(bundle, "Same") == "Same"      # an exact shot id beats a name
    assert resolve_shot(bundle, "a") == "a"            # ... even when another shot is named "a"
    bundle["shots"].pop("Same")
    with pytest.raises(ShotPlanError, match="shot name 'Same' is ambiguous; use a shot id: a, b"):
        resolve_shot(bundle, "Same")


def test_malformed_stored_plan_is_reported_not_crashed_on():
    from astrid.sdk.shot_plan import plan_problem
    bundle = {"placements": [
        {"occurrence_id": "o1", "shot_id": "s1", "placement": {"start_ms": 0}, "duration_ms": 1000},
        {"occurrence_id": "o2", "shot_id": "s2", "placement": {"start_ms": 1000}, "duration_ms": 1000}],
        "shots": {"s1": {"payload": {"metadata": {"plan": {"method": "LM", "status": 3}}}},
                  "s2": {"payload": {"metadata": {"plan": "not an object"}}}}}
    rows = plan_rows(bundle)
    assert [bool(row["invalid"]) for row in rows] == [True, True]
    assert all(plan_label(row["plan"]).startswith("invalid plan (") for row in rows)
    assert plan_problem(rows[1]["plan"]) == "plan must be a JSON object"
    with pytest.raises(ShotPlanError, match="stored plan is invalid.*--replace"):
        merge_plan(rows[1]["plan"], {"method": ["K"]})
    with pytest.raises(ShotPlanError, match="already invalid.*--replace"):
        merge_plan(rows[0]["plan"], {"intent": "x"})
    assert merge_plan(rows[1]["plan"], {"method": ["K"]}, replace=True) == {"version": 1, "method": ["K"]}


# -- set / merge / show / list on a real Runtime commit path -----------------------


def _run(parser, argv, capsys):
    parsed = parser.parse_args(argv)
    code = parsed.handler(parsed)
    return code, json.loads(capsys.readouterr().out)


def test_cli_sets_merges_shows_and_lists_plans_through_timeline_revisions(restructure_runtime, capsys, tmp_path,
                                                                          monkeypatch):
    from astrid.core.cli.domain_shots import build_parser
    from astrid.sdk import shot_plan
    from astrid.sdk.timeline.commit import bundle_from_document

    env = restructure_runtime
    original = shot_plan.set_plan
    monkeypatch.setattr(shot_plan, "set_plan",
                        lambda *a, **k: original(*a, **{**k, "outbox_path": tmp_path / "outbox.json"}))
    parser = build_parser(env.client)
    base = ["--project", env.project, "--timeline", "t"]
    before = bundle_from_document(env.document())
    metadata_before = before["shots"]["film"]["payload"].get("metadata")

    code, out = _run(parser, ["plan", "list", *base], capsys)
    assert code == 0 and out["data"]["unplanned"] == ["film"]

    plan_file = tmp_path / "plan.json"
    plan_file.write_text(json.dumps({"on_screen": ["Morpheus"], "intent": "re-voice"}), encoding="utf-8")
    code, out = _run(parser, ["plan", "set", "FILM", *base, "--json-file", str(plan_file),
                              "--set", "method=LM,R", "--set", "status=planned",
                              "--set", "inputs.references=sha256:" + "a" * 64], capsys)
    assert code == 0, out
    first_revision = out["data"]["revision_id"]
    assert out["data"]["plan"] == {"version": 1, "on_screen": ["Morpheus"], "intent": "re-voice",
                                   "method": ["LM", "R"], "status": "planned",
                                   "inputs": {"references": ["sha256:" + "a" * 64]}}

    # Stage 4 then 5: the voice slice, then a take; earlier fields stay. A --json-file voice media
    # and a --set range combine (inputs.voice merges key by key).
    voice_file = tmp_path / "voice.json"
    voice_file.write_text(json.dumps({"inputs": {"voice": {"media": "sha256:" + "b" * 64}}}), encoding="utf-8")
    code, out = _run(parser, ["plan", "set", "film", *base, "--json-file", str(voice_file),
                              "--set", "inputs.voice.range=5,8"], capsys)
    assert code == 0, out
    assert out["data"]["plan"]["inputs"]["voice"] == {"media": "sha256:" + "b" * 64, "range": [5.0, 8.0]}
    code, out = _run(parser, ["plan", "set", "occ-film", *base, "--set", "status=take_ready",
                              "--add-take", '{"generation": "run-1", "note": "first"}'], capsys)
    assert code == 0
    assert out["data"]["plan"]["inputs"] == {"references": ["sha256:" + "a" * 64],
                                             "voice": {"media": "sha256:" + "b" * 64, "range": [5.0, 8.0]}}
    assert out["data"]["plan"]["takes"] == [{"generation": "run-1", "note": "first"}]

    # Refused before anything is committed.
    head = env.client.timelines.history(env.project, "t", limit=1).data["head"]
    code, out = _run(parser, ["plan", "set", "film", *base, "--set", "status=done"], capsys)
    assert code != 0 and "plan.status must be one of" in out["error"]["message"]
    code, out = _run(parser, ["plan", "set", "film", *base, "--json-file", str(_write(tmp_path, {"owner": 1}))], capsys)
    assert code != 0 and "unknown key(s) under plan: owner" in out["error"]["message"]
    assert env.client.timelines.history(env.project, "t", limit=1).data["head"] == head

    # The stored shot carries the plan beside its untouched metadata; earlier revisions keep theirs.
    after = bundle_from_document(env.document())
    metadata = after["shots"]["film"]["payload"]["metadata"]
    assert {k: v for k, v in metadata.items() if k != "plan"} == (metadata_before or {})
    assert metadata["plan"]["status"] == "take_ready"
    assert after["shots"]["film"]["internal_timeline"] == before["shots"]["film"]["internal_timeline"]

    code, out = _run(parser, ["plan", "show", "film", *base], capsys)
    assert code == 0 and out["data"]["plan"] == metadata["plan"]
    assert out["data"]["occurrences"] == [{"occurrence_id": "occ-film", "start": 0.0, "end": 12.0}]
    code, out = _run(parser, ["plan", "show", "film", *base, "--revision-id", first_revision], capsys)
    assert code == 0 and out["data"]["plan"]["status"] == "planned" and "takes" not in out["data"]["plan"]
    code, out = _run(parser, ["plan", "list", *base], capsys)
    assert code == 0 and out["data"]["unplanned"] == [] and out["data"]["not_approved"] == ["film"]

    assert _run(parser, ["plan", "set", "film", *base, "--set", "status=approved"], capsys)[0] == 0
    code, out = _run(parser, ["plan", "set", "film", *base, "--set", "status=approved"], capsys)
    assert code == 0 and out["data"]["unchanged"] is True  # no empty revision for a no-op
    assert _run(parser, ["plan", "list", *base], capsys)[1]["data"]["not_approved"] == []


def _write(tmp_path, value):
    path = tmp_path / "patch.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


# -- visualize labelling (structural) ----------------------------------------------


def test_visualize_shot_plans_flag_labels_each_shot_of_the_drawn_revision():
    from astrid.core.cli.domain_timelines import _visualize_add_visualize_flags
    from astrid.packs.timeline.cli_visualize import _visualized_plan_rows, shot_plan_lines
    import argparse

    parser = argparse.ArgumentParser()
    _visualize_add_visualize_flags(parser)
    assert parser.parse_args(["--shot-plans"]).shot_plans is True
    assert parser.parse_args([]).shot_plans is False

    bundle = {"placements": [
        {"occurrence_id": "o1", "shot_id": "s1", "placement": {"start_ms": 0}, "duration_ms": 5000},
        {"occurrence_id": "o2", "shot_id": "s2", "placement": {"start_ms": 5000}, "duration_ms": 3000}],
        "shots": {"s1": {"payload": {"name": "Logo"}},
                  "s2": {"payload": {"name": "Morpheus MS", "metadata": {"plan": FULL}}}}}
    calls = []

    def open_bundle(project, timeline, **kw):
        calls.append((project, timeline, kw))
        return DomainResult.success({"bundle": bundle})

    parsed = SimpleNamespace(project="p", client=SimpleNamespace(timelines=SimpleNamespace(open_bundle=open_bundle)))
    rows = _visualized_plan_rows(parsed, {"timeline_slug": "t", "revision_id": "rev-9"}, None)
    assert calls == [("p", "t", {"revision_id": "rev-9"})]
    assert [row["label"] for row in rows] == ["no plan", 'LM+R · planned · 1 take · "You have two options."']
    assert shot_plan_lines(rows) == [
        "shot plans (1 of 2 placed shots planned):",
        "    0.00–5.00s  Logo  no plan",
        '    5.00–8.00s  Morpheus MS  LM+R · planned · 1 take · "You have two options."',
    ]


# -- archive round trip ----------------------------------------------------------------


def test_shot_plan_survives_astrid_export_and_import(tmp_path, real_runtime, capsys, monkeypatch):
    from astrid.packs.timeline import cli as timeline_cli
    from astrid.sdk import shot_plan
    from astrid.sdk.timeline import Timeline
    from astrid.sdk.timeline.capture import capture_track_create
    from astrid.sdk.timeline.commit import bundle_from_document
    from astrid.sdk.timeline.native_occurrence_create import capture_occurrence_create
    from astrid.sdk.timeline_checkout import Checkout
    from tests.editing_conformance.test_commit_runtime_m1 import open_client
    from tests.helpers.runtime import runtime_checkout, use_runtime

    monkeypatch.setenv("ASTRID_RUNTIME_CHECKOUT", str(runtime_checkout()))
    use_runtime()
    daemons = [real_runtime(tmp_path / name, support_root=tmp_path / f"{name}-support", authoring_profile="astrid")
               for name in ("source-realm", "target-realm")]
    source, target = (open_client(daemon) for daemon in daemons)
    source_id = daemons[0].service.create_project({"slug": "plan-source", "name": "Plan source"})["id"]
    target_id = daemons[1].service.create_project({"slug": "plan-target", "name": "Plan target"})["id"]
    assert source.timelines.create_empty(project=source_id, timeline_id="main", name="Planned film",
                                         idempotency_key="plan-create").ok

    def opened():
        return Timeline.open(source_id, "main", client=source, outbox_path=tmp_path / "outbox.json")

    def snapshot(tl):
        return bundle_from_document(tl._runtime_document) if tl._runtime_document else tl.bundle

    tl = opened()
    tl.commit([capture_track_create(snapshot(tl), "parent", {"id": "picture", "kind": "visual"})],
              label="Picture track", key="plan-track")
    tl = opened()
    tl.commit([capture_occurrence_create(snapshot(tl), "occ-ms", "morpheus-ms", name="Morpheus MS",
                                         track="picture", on=0, duration=3)], label="Shot", key="plan-shot")
    plan = {**FULL, "takes": [{"generation": "run-1", "note": "first"}]}
    written = shot_plan.set_plan(source, source_id, "main", "Morpheus MS", patch=plan,
                                 outbox_path=tmp_path / "outbox.json")
    assert written.ok, written.as_dict()

    archive_path = tmp_path / "film.astrid"
    parsed = timeline_cli.build_parser(source).parse_args(
        ["export", "main", "--project", source_id, "--out", str(archive_path), "--json"])
    assert parsed.handler(parsed) == 0
    capsys.readouterr()
    with zipfile.ZipFile(archive_path) as archive:
        stored = [archive.read(name).decode("utf-8") for name in archive.namelist() if name.endswith(".json")]
    assert any("mouth follows the voice track" in text for text in stored)

    parsed = timeline_cli.build_parser(target).parse_args(
        ["import", str(archive_path), "--project", target_id, "--json"])
    assert parsed.handler(parsed) == 0
    imported = json.loads(capsys.readouterr().out)["data"]["timeline_id"]
    source_bundle = Checkout.open(source_id, "main", client=source).bundle
    target_bundle = Checkout.open(target_id, imported, client=target).bundle
    source_metadata = source_bundle["shots"]["morpheus-ms"]["payload"]["metadata"]
    assert source_metadata["plan"] == plan
    rows = plan_rows(target_bundle)
    assert len(rows) == 1 and rows[0]["name"] == "Morpheus MS"
    assert rows[0]["plan"] == plan
    assert target_bundle["shots"][rows[0]["shot_id"]]["payload"]["metadata"] == source_metadata


def _other_writer(env, value):
    """Commit an unrelated parent change as a second writer would."""
    head = env.service.get_project_timeline(env.project, "t")
    current = bundle_from_document_(env)["parent"].get("opaque")
    command = {"schema_version": 1, "op": "doc.patch", "version": "1", "select": None, "args": {"changes": [
        {"op": "set", "ref": "parent", "path": ["opaque"], "before": {"state": "present", "value": current},
         "value": {"writer": value}}]}}
    body = {"api_version": 1, "project_id": env.project, "timeline_id": "t",
            "base_revision_id": head["head_revision_id"], "base_publication_id": head["head_publication_id"],
            "commands": [command], "label": f"other {value}", "merge": "fields", "reads": [],
            "steps": [{"id": "other", "label": f"other {value}", "intent": "", "tags": [], "command_ordinals": [0],
                       "patch": command["args"]["changes"]}]}
    env.send(env.project, "t", body, idempotency_key=f"other-writer-{value}")


def bundle_from_document_(env):
    from astrid.sdk.timeline.commit import bundle_from_document
    return bundle_from_document(env.document())


def test_conflict_is_reported_as_conflict_and_leaves_no_saved_edit(restructure_runtime, tmp_path, monkeypatch):
    from astrid.sdk import shot_plan
    from astrid.sdk.timeline import Timeline

    env = restructure_runtime
    outbox = tmp_path / "outbox.json"
    original_merge = shot_plan.merge_plan
    calls = {"n": 0}

    def racing_merge(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] > 1:  # inside every commit attempt: someone else commits first
            _other_writer(env, calls["n"])
        return original_merge(*args, **kwargs)

    monkeypatch.setattr(shot_plan, "merge_plan", racing_merge)
    result = shot_plan.set_plan(env.client, env.project, "t", "film", patch={"method": ["V"]}, outbox_path=outbox)
    assert not result.ok and result.error.code == "conflict", result.as_dict()
    assert "run the same `timelines shots plan set` command again" in result.error.message
    assert "tl.retry" not in result.error.message
    assert result.error.details["dropped_saved_edits"]  # the saved retry copy was dropped, not left behind
    assert shot_plan.read_plan(bundle_from_document_(env)["shots"]["film"]) is None
    tl = Timeline.open(env.project, "t", client=env.client, outbox_path=outbox)
    assert tl.pending_list() == []

    # The same command, run again without a racing writer, lands.
    monkeypatch.setattr(shot_plan, "merge_plan", original_merge)
    again = shot_plan.set_plan(env.client, env.project, "t", "film", patch={"method": ["V"]}, outbox_path=outbox)
    assert again.ok, again.as_dict()
    assert shot_plan.read_plan(bundle_from_document_(env)["shots"]["film"])["method"] == ["V"]


def test_shot_removed_between_open_and_commit_is_a_clear_failure(restructure_runtime, tmp_path, monkeypatch):
    from astrid.sdk import shot_plan

    env = restructure_runtime
    original_open = shot_plan._open
    monkeypatch.setattr(shot_plan, "_open", lambda *a, **k: {
        **original_open(*a, **k), "shots": {**original_open(*a, **k)["shots"], "gone": {"payload": {"name": "Gone"}}}})
    result = shot_plan.set_plan(env.client, env.project, "t", "gone", patch={"method": ["V"]},
                                outbox_path=tmp_path / "outbox.json")
    assert not result.ok and result.error.code == "conflict"
    assert "shot gone is no longer on timeline 't'" in result.error.message
    assert "run the same `timelines shots plan set` command again" in result.error.message


def test_outcome_unknown_is_not_reported_as_nothing_committed(monkeypatch):
    from astrid.sdk import shot_plan
    from astrid.sdk.timeline import Timeline
    from astrid.sdk.timeline.sync import CommitOutcomeUnknown

    bundle = {"base_parent": {"revision_id": "r1"}, "placements": [
        {"occurrence_id": "o1", "shot_id": "s1", "placement": {"start_ms": 0}, "duration_ms": 1000}],
        "shots": {"s1": {"payload": {"name": "One"}}}}
    client = SimpleNamespace(timelines=SimpleNamespace(
        open_bundle=lambda *a, **k: DomainResult.success({"bundle": bundle})))
    seen = {}

    class Session:
        def commit(self, fn, *, label, key):
            seen["key"] = key
            raise CommitOutcomeUnknown(ConnectionError("connection reset"), key=key)

    monkeypatch.setattr(Timeline, "open", classmethod(lambda cls, *a, **k: Session()))
    result = shot_plan.set_plan(client, "p", "t", "s1", patch={"method": ["V"]}, idempotency_key="plan-key-1")
    assert not result.ok and result.error.code == "outcome_unknown"
    assert "may or may not have landed" in result.error.message
    assert "--idempotency-key plan-key-1" in result.error.message
    assert "Nothing was committed" not in result.error.message
    assert seen["key"] == "plan-key-1"

    # Without a caller key one is generated, sent, and named so the user can confirm with it.
    result = shot_plan.set_plan(client, "p", "t", "s1", patch={"method": ["V"]})
    assert seen["key"].startswith("shot-plan-") and f"--idempotency-key {seen['key']}" in result.error.message
    assert result.error.details["idempotency_key"] == seen["key"]


def test_unexpected_key_error_is_not_reported_as_conflict(monkeypatch):
    from astrid.sdk import shot_plan
    from astrid.sdk.timeline import Timeline

    bundle = {"placements": [], "shots": {"s1": {"payload": {"name": "One"}}}}
    client = SimpleNamespace(timelines=SimpleNamespace(
        open_bundle=lambda *a, **k: DomainResult.success({"bundle": bundle})))

    class Session:
        def commit(self, fn, *, label, key):
            raise KeyError("unrelated")

    monkeypatch.setattr(Timeline, "open", classmethod(lambda cls, *a, **k: Session()))
    with pytest.raises(KeyError, match="unrelated"):
        shot_plan.set_plan(client, "p", "t", "s1", patch={"method": ["V"]})
