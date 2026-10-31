"""E14 acceptance against the locked Runtime."""
from __future__ import annotations

import copy
import io
import json
import subprocess
import time
import wave
from pathlib import Path

import pytest

from tests.helpers.runtime import runtime_checkout

RUNTIME_CHECKOUT = str(runtime_checkout())
RUNTIME_COMMIT = "9ef808711a5207f5be74dc5f267af6fdd7bc9550"


@pytest.fixture
def editing_case(tmp_path, monkeypatch, real_runtime):
    """A fresh Runtime realm with a real HTTP client and an empty timeline."""
    from tests.helpers.runtime import use_runtime

    monkeypatch.setenv("ASTRID_RUNTIME_CHECKOUT", RUNTIME_CHECKOUT)
    runtime = use_runtime()
    actual = subprocess.check_output(["git", "-C", str(runtime), "rev-parse", "HEAD"], text=True).strip()
    assert actual == RUNTIME_COMMIT
    from runtime_protocol import timeline_commit_store

    assert Path(timeline_commit_store.__file__).resolve() == runtime / "runtime_protocol/timeline_commit_store.py"

    daemon = real_runtime(authoring_profile="astrid")
    project = daemon.service.create_project({"slug": "e14-acceptance", "name": "E14 acceptance"})["id"]
    from tests.editing_conformance.test_commit_runtime_m1 import open_client

    client = open_client(daemon)
    created = client.timelines.create_empty(
        project=project,
        timeline_id="film",
        name="E14 film",
        idempotency_key="e14-create-film",
    )
    assert created.ok, created.as_dict()
    from astrid.sdk.timeline import Timeline

    timeline = Timeline.open(project, "film", client=client, outbox_path=tmp_path / "outbox.json")
    return daemon, client, project, timeline


def _history(client, project):
    return _timeline_history(client, project, "film")


def _timeline_history(client, project, timeline_id):
    result = client.timelines.history(project, timeline_id, limit=100)
    assert result.ok, result.as_dict()
    return result.data["items"]


def _snapshot(timeline):
    return copy.deepcopy(timeline._runtime_document)


def _media_file(path, *, color="red"):
    """Create a tiny deterministic PNG without an external fixture or install."""
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"color=c={color}:s=16x16:r=1",
         "-frames:v", "1", "-threads", "1", "-c:v", "png", str(path)],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return path


def _video_file(path):
    """Create a small moving source with a source window long enough for split and trim."""
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=blue:s=64x64:r=24",
         "-t", "12", "-an", "-threads", "1", "-c:v", "mpeg4", str(path)],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return path


def _voice_file(path, *, seconds=2):
    audio = io.BytesIO()
    with wave.open(audio, "wb") as output:
        output.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
        output.writeframes(b"\x00\x00" * (8000 * seconds))
    path.write_bytes(audio.getvalue())
    return path


def _sequential_births(bundle, specs):
    """Capture each birth on the bundle the previous births produce.

    A birth guards the ordered occurrence collection it observed, so a list of
    births is valid only when each is captured against its predecessor's result.
    """
    from astrid.sdk.timeline.commands import execute
    from astrid.sdk.timeline.native_occurrence_create import capture_occurrence_create

    commands = []
    for spec in specs:
        command = capture_occurrence_create(bundle, **spec)
        commands.append(command)
        bundle = execute(bundle, [command]).bundle
    return commands


@pytest.mark.parametrize("lost_ack", [False, True])
def test_e14_twenty_real_occurrences_arrange_once_and_exact_undo(
    editing_case, tmp_path, capsys, monkeypatch, lost_ack,
):
    _daemon, client, project, timeline = editing_case
    from astrid.sdk.timeline.commands import execute
    from astrid.sdk.timeline.commit import bundle_from_document
    from astrid.sdk.timeline.native_occurrence_create import capture_occurrence_create

    started = time.perf_counter()
    bundle = bundle_from_document(_snapshot(timeline))
    history_before = _history(client, project)
    births = []
    for index in range(20):
        command = capture_occurrence_create(
            bundle, f"occ-{index:02d}", f"shot-{index:02d}", name=f"Shot {index:02d}",
            track="V1", on=index * 3, duration=2,
        )
        births.append(command)
        bundle = execute(bundle, [command]).bundle
    timings = {"setup_capture_seconds": round(time.perf_counter() - started, 6)}
    started = time.perf_counter()
    setup = timeline.commit(births, label="Create twenty shot occurrences", key="e14-create-shots")
    timings["setup_commit_seconds"] = round(time.perf_counter() - started, 6)
    assert setup["receipt"]["receipt_id"]
    assert len(_history(client, project)) == len(history_before) + 1

    before = _snapshot(timeline)
    history_before = _history(client, project)
    order = [f"occ-{index:02d}" for index in reversed(range(20))]
    evaluations = []

    def arrange(t):
        evaluations.append(True)
        t.arrange(order, at=100, gap=0)

    started = time.perf_counter()
    if lost_ack:
        from astrid.sdk.timeline.outbox import request_bytes
        from astrid.sdk.timeline.sync import CommitOutcomeUnknown
        from astrid_runtime_client import ApiError

        generated = client._remote._transport._generated
        original = generated.commit_timeline
        sent = []

        def lose_first_response(*args, **kwargs):
            sent.append((request_bytes(args[2]), kwargs["idempotency_key"]))
            result = original(*args, **kwargs)
            if len(sent) == 1:
                raise ApiError(503, "not_ready", "simulated lost arrange response after acceptance")
            return result

        monkeypatch.setattr(generated, "commit_timeline", lose_first_response)
        with pytest.raises(CommitOutcomeUnknown):
            timeline.commit(arrange, label="Arrange twenty shots", key="e14-arrange-shots")
        outbox = timeline.session.outbox.path
        flight = json.loads(outbox.read_text())["flight"]
        assert flight is not None
        timeline = type(timeline).open(project, "film", client=client, outbox_path=outbox)
        arranged = timeline.commit(lambda _t: pytest.fail("retry must reuse the saved arrange request"))
        assert sent == [(flight["body"].encode(), flight["key"])] * 2
        assert flight["key"] == "e14-arrange-shots"
    else:
        arranged = timeline.commit(arrange, label="Arrange twenty shots", key="e14-arrange-shots")
    timings["arrange_seconds"] = round(time.perf_counter() - started, 6)
    assert evaluations == [True]
    assert arranged["receipt"]["receipt_id"]
    assert len(_history(client, project)) == len(history_before) + 1
    rows = arranged["data"]["canonical_document"]["parent_revision"]["payload"]["occurrences"]
    assert [row["occurrence_id"] for row in rows] == order
    assert [row["placement"]["start_ms"] for row in rows] == [100000 + i * 2000 for i in range(20)]
    old_rows = {row["occurrence_id"]: row for row in before["parent_revision"]["payload"]["occurrences"]}
    for row in rows:
        previous = old_rows[row["occurrence_id"]]
        assert row == {**previous, "placement": {**previous["placement"], "start_ms": row["placement"]["start_ms"]}}
    for field in ("shot_revisions", "internal_timeline_revisions", "dependency_manifest"):
        assert arranged["data"]["canonical_document"][field] == before[field]
    started = time.perf_counter()
    undone = timeline.undo(publications=[arranged["receipt"]["receipt_id"]])
    timings["undo_seconds"] = round(time.perf_counter() - started, 6)
    assert undone["data"]["canonical_document"] == before
    assert len(_history(client, project)) == len(history_before) + 2
    # These timings describe this run; they impose no performance threshold.
    evidence = {"runtime": RUNTIME_COMMIT, "occurrences": 20, "lost_ack": lost_ack, **timings}
    (tmp_path / "e14-arrange-timings.json").write_text(json.dumps(evidence, indent=2) + "\n")
    with capsys.disabled():
        print("E14 arrange timings: " + json.dumps(evidence, sort_keys=True))


def test_e14_picture_and_voice_on_new_tracks_one_receipt_exact_undo(editing_case, tmp_path):
    _daemon, client, project, timeline = editing_case
    picture = _media_file(tmp_path / "picture.png")
    voice = _voice_file(tmp_path / "voice.wav")
    before = _snapshot(timeline)
    history_before = _history(client, project)

    result = timeline.commit(
        lambda t: (
            t.track("V2", above="V1"),
            t.add(picture, at=0, hold=2, track="V2", id="new-picture"),
            t.add(voice, at=0, track="A2", id="new-voice"),
        ),
        label="Picture and voice on new tracks",
        key="e14-picture-voice",
    )
    after = result["data"]["canonical_document"]
    payload = after["parent_revision"]["payload"]
    clips = {row["id"]: row for row in payload["clips"]}
    assert result["receipt"]["receipt_id"]
    assert len(_history(client, project)) == len(history_before) + 1
    assert clips["new-picture"]["track"] == "V2"
    assert clips["new-voice"]["track"] == "A2"
    assert {row["id"] for row in payload["config"]["tracks"]} >= {"V1", "V2", "A2"}
    undone = timeline.undo(publications=[result["receipt"]["receipt_id"]])
    assert undone["data"]["canonical_document"] == before
    assert len(_history(client, project)) == len(history_before) + 2


def test_e14_twenty_ordered_media_one_receipt_exact_undo_and_timings(editing_case, tmp_path):
    _daemon, client, project, timeline = editing_case
    timeline_type = type(timeline)
    source = _media_file(tmp_path / "frame.png")
    before = _snapshot(timeline)
    history_before = _history(client, project)

    result = timeline.commit(
        lambda t: [t.add(source, at=index * 2, hold=1, track="V1", id=f"shot-{index + 1:02d}")
                   for index in range(20)],
        label="Place twenty files",
        key="e14-place-twenty",
    )

    assert result["data"]["status"] in {"accepted", "merged", "rebased"}
    assert result["receipt"]["receipt_id"]
    assert len(_history(client, project)) == len(history_before) + 1
    assert len(result["imports"]) == 1  # Same content under one identity is reused.
    after = copy.deepcopy(result["data"]["canonical_document"])
    clips = after["parent_revision"]["payload"]["clips"]
    assert [row["id"] for row in clips] == [f"shot-{index + 1:02d}" for index in range(20)]
    assert [row["at"] for row in clips] == [index * 2 for index in range(20)]

    undo = timeline.undo(publications=[result["receipt"]["receipt_id"]])
    assert undo["data"]["canonical_document"] == before
    assert len(_history(client, project)) == len(history_before) + 2

    # Timing is observational evidence only; it is deliberately not a performance gate.
    timings = {}
    local = timeline_type.open(project, "film", client=client,
                               outbox_path=tmp_path / "timing-20.json")
    prior = _snapshot(local)
    timing_history = _history(client, project)
    start = time.perf_counter()
    measured = local.commit(
        lambda t: [t.add(source, at=1000 + index * 2, hold=1,
                         track="V1", id=f"timing-20-{index:03d}")
                   for index in range(20)],
        label="Timing 20 items",
        key="e14-timing-20",
    )
    timings["20"] = round(time.perf_counter() - start, 6)
    assert measured["receipt"]["receipt_id"]
    assert len(_history(client, project)) == len(timing_history) + 1
    timed_after = copy.deepcopy(measured["data"]["canonical_document"])
    undone = local.undo(publications=[measured["receipt"]["receipt_id"]])
    assert undone["data"]["canonical_document"] == prior
    assert timed_after != prior
    assert len(_history(client, project)) == len(timing_history) + 2

    # Keep each setup request bounded. The measured edit below changes one clip
    # on a timeline that already contains 200 real items; it is not a 200-item
    # batch mislabeled as a timeline-size observation.
    timing_setup_commits = 0
    local = timeline_type.open(project, "film", client=client,
                               outbox_path=tmp_path / "timing-200.json")
    for batch in range(10):
        start_index = batch * 20
        seeded = local.commit(
            lambda t, start_index=start_index: [
                t.add(source, at=2000 + index * 2, hold=1, track="V1",
                      id=f"timing-200-{index:03d}")
                for index in range(start_index, start_index + 20)
            ],
            label=f"Seed 200-item timing fixture {batch + 1}",
            key=f"e14-timing-200-seed-{batch + 1}",
        )
        assert seeded["receipt"]["receipt_id"]
        timing_setup_commits += 1

    prior = _snapshot(local)
    assert len(prior["parent_revision"]["payload"]["clips"]) == 200
    timing_history = _history(client, project)
    start = time.perf_counter()
    measured = local.commit(
        lambda t: t.clip("timing-200-000").hold_for(2),
        label="Timing edit on 200 items",
        key="e14-timing-200-edit",
    )
    timings["200_item_timeline_edit"] = round(time.perf_counter() - start, 6)
    assert measured["receipt"]["receipt_id"]
    assert len(_history(client, project)) == len(timing_history) + 1
    timed_after = copy.deepcopy(measured["data"]["canonical_document"])
    assert timed_after != prior
    undone = local.undo(publications=[measured["receipt"]["receipt_id"]])
    assert undone["data"]["canonical_document"] == prior
    assert len(_history(client, project)) == len(timing_history) + 2

    evidence = Path(
        "/tmp/claude-0/-home-user-repo/7dc61be1-7066-52e0-a5d0-94f90e61430a"
        "/scratchpad/orch/workers/D-E14-qualification-r6/evidence"
    )
    evidence.mkdir(parents=True, exist_ok=True)
    (evidence / "e14-timings.json").write_text(
        json.dumps({"runtime": RUNTIME_COMMIT,
                    "commit_seconds": timings,
                    "200_item_setup_commits": timing_setup_commits,
                    "200_item_setup_count": 200}, indent=2) + "\n",
        encoding="utf-8",
    )


def test_e14_media_reuse_adds_existing_media_without_second_upload_and_undoes_exactly(
    editing_case, tmp_path, monkeypatch,
):
    _daemon, client, project, timeline = editing_case
    transport = client._remote._transport
    uploads = []
    original_import = transport.import_project_media

    def counting_import(*args, **kwargs):
        uploads.append(kwargs.get("expected_digest"))
        return original_import(*args, **kwargs)

    monkeypatch.setattr(transport, "import_project_media", counting_import)
    source = _media_file(tmp_path / "reused.png")
    history_before = _history(client, project)

    first = timeline.commit(
        lambda t: t.add(source, at=0, hold=1, track="V1", id="reuse-first"),
        label="Place media",
        key="e14-reuse-first",
    )
    assert first["receipt"]["receipt_id"]
    assert len(uploads) == 1
    before_second = _snapshot(timeline)
    history_after_first = _history(client, project)
    assert len(history_after_first) == len(history_before) + 1

    second = timeline.commit(
        lambda t: t.add(source, at=2, hold=1, track="V1", id="reuse-second"),
        label="Reuse placed media",
        key="e14-reuse-second",
    )
    assert second["receipt"]["receipt_id"]
    assert len(uploads) == 1  # The second clip reuses the already-imported media object.
    clips = {row["id"]: row for row in second["data"]["canonical_document"]["parent_revision"]["payload"]["clips"]}
    assert clips["reuse-first"]["at"] == 0
    assert clips["reuse-second"]["at"] == 2
    assert len(_history(client, project)) == len(history_after_first) + 1

    undone = timeline.undo(publications=[second["receipt"]["receipt_id"]])
    assert undone["data"]["canonical_document"] == before_second
    assert len(_history(client, project)) == len(history_after_first) + 2


def _seed_flat_editing(timeline, picture, voice, *, music_at=None):
    """Author a real flat parent fixture; this receipt is setup, not an acceptance edit."""
    client = getattr(timeline.session.client, "_remote", timeline.session.client)
    history_before = len(_history(client, timeline.session.project_id))

    def seed(t):
        t.track("V2", above="V1")
        t.track("music", kind="audio")
        t.add(picture, at=0, hold=0.5, track="V1", id="before")
        t.add(picture, at=4.5, hold=0.5, track="V2", id="inside")
        t.add(picture, at=3, hold=4, track="V1", id="across")
        t.add(picture, at=8, hold=1, track="V1", id="after")
        transcript = t.add(voice, at=0, hold=10, track="A1", id="transcript")
        transcript.data["app"] = {"astrid": {
            "segment": "voice", "text": "before after",
            "words": [[0.1, 1.2, "before"], [4.1, 4.4, "after"]],
        }}
        if music_at is not None:
            t.add(voice, at=music_at, hold=10, track="music", id="music-bed")

    setup = timeline.commit(seed, label="Seed flat editing fixture", key=f"e14-seed-{music_at}")
    assert setup["receipt"]["receipt_id"]
    assert len(_history(client, timeline.session.project_id)) == history_before + 1
    return setup


@pytest.mark.parametrize("operation", ["insert_time", "ripple_delete", "close_gap"])
def test_e14_flat_ripple_helpers_commit_once_and_undo_real_transcript_gap(
    editing_case, tmp_path, operation,
):
    _daemon, _client, project, timeline = editing_case
    picture = _media_file(tmp_path / "ripple-picture.png")
    voice = _voice_file(tmp_path / "ripple-voice.wav", seconds=20)
    _seed_flat_editing(timeline, picture, voice)
    before = _snapshot(timeline)
    history_before = _history(_client, project)

    def ripple(t):
        if operation == "insert_time":
            t.insert_time(4, 2)
        elif operation == "ripple_delete":
            t.ripple_delete(4, 6)
        else:
            observed = next(word for word in t.words() if word.text == "after")
            assert observed.id
            t.close_gap(before=observed.id)

    result = timeline.commit(ripple, label=f"E14 {operation}", key=f"e14-{operation}")
    assert result["receipt"]["receipt_id"]
    assert len(_history(_client, project)) == len(history_before) + 1
    rows = {row["id"]: row for row in result["data"]["canonical_document"]["parent_revision"]["payload"]["clips"]}
    assert rows["before"]["at"] == 0
    if operation == "insert_time":
        assert rows["inside"]["at"] == pytest.approx(6.5)
        assert rows["across"]["hold"] == pytest.approx(6)
        assert rows["after"]["at"] == pytest.approx(10)
    elif operation == "ripple_delete":
        assert "inside" not in rows
        assert rows["across"]["hold"] == pytest.approx(2)
        assert rows["after"]["at"] == pytest.approx(6)
    else:
        assert rows["inside"]["at"] == pytest.approx(1.6)
        assert rows["across"]["at"] == pytest.approx(1.2)
        assert rows["after"]["at"] == pytest.approx(5.1)
    undone = timeline.undo(publications=[result["receipt"]["receipt_id"]])
    assert undone["data"]["canonical_document"] == before
    assert len(_history(_client, project)) == len(history_before) + 2


@pytest.mark.parametrize("music_at", [4, 5])
@pytest.mark.parametrize("music_policy", ["keep_end", "plays_on"])
def test_e14_music_beds_at_window_start_and_inside_follow_policy_and_undo(
    editing_case, tmp_path, music_at, music_policy,
):
    _daemon, client, project, timeline = editing_case
    picture = _media_file(tmp_path / "music-picture.png")
    voice = _voice_file(tmp_path / "music-voice.wav", seconds=20)
    _seed_flat_editing(timeline, picture, voice, music_at=music_at)
    before = _snapshot(timeline)
    history_before = _history(client, project)
    result = timeline.commit(
        lambda t: t.ripple_delete(4, 6, music=music_policy),
        label=f"E14 music {music_policy} at {music_at}",
        key=f"e14-music-{music_policy}-{music_at}",
    )
    assert result["receipt"]["receipt_id"]
    assert len(_history(client, project)) == len(history_before) + 1
    after_clips = result["data"]["canonical_document"]["parent_revision"]["payload"]["clips"]
    edge_bed = next(row for row in after_clips if row["id"] == "music-bed")
    assert edge_bed["at"] == 4
    assert edge_bed["from"] == pytest.approx(6 - music_at)
    assert edge_bed["to"] == pytest.approx(10)
    undone = timeline.undo(publications=[result["receipt"]["receipt_id"]])
    assert undone["data"]["canonical_document"] == before
    assert len(_history(client, project)) == len(history_before) + 2


def test_e14_mixed_recut_previews_then_applies_exact_literal_list_once(editing_case, tmp_path):
    _daemon, client, project, timeline = editing_case
    picture = _video_file(tmp_path / "recut-picture.mp4")
    from astrid.sdk.timeline.commit import bundle_from_document
    from astrid.sdk.timeline.native_occurrence_create import capture_occurrence_create

    shots_track = timeline.commit(lambda t: t.track("Shots", kind="visual"),
                                  label="Seed recut occurrence track", key="e14-seed-recut-track")
    assert shots_track["receipt"]["receipt_id"]
    bundle = bundle_from_document(_snapshot(timeline))
    occurrences = _sequential_births(bundle, [dict(
        occurrence_id=f"recut-occ-{index}", shot_id=f"recut-shot-{index}", name=f"Recut shot {index}",
        track="Shots", on=index * 3, duration=2,
    ) for index in range(2)])
    history_before_occurrences = _history(client, project)
    occurrence_setup = timeline.commit(occurrences, label="Seed recut occurrences", key="e14-seed-recut-occurrences")
    assert occurrence_setup["receipt"]["receipt_id"]
    assert len(_history(client, project)) == len(history_before_occurrences) + 1

    def setup(t):
        t.track("Titles", kind="visual")
        t.add(picture, at=2, hold=4, track="V1", id="footage")
        t.add(picture, at=6, hold=4, track="V1", id="cutaway")
        t.add_text("Draft chapter", at=0, hold=2, track="Titles", id="title")
        t.add_text("Old intro", at=0, hold=2, track="Titles", id="old-intro")

    setup_history = _history(client, project)
    setup_result = timeline.commit(setup, label="Seed recut", key="e14-seed-recut")
    assert setup_result["receipt"]["receipt_id"]
    assert len(_history(client, project)) == len(setup_history) + 1
    before = _snapshot(timeline)
    history_before = _history(client, project)

    def recut(t):
        footage = t.clip("footage").trim(1, 4).on(1)
        t.clip("cutaway").split(8, id="cutaway-right")
        t.clip("title").set(text="Chapter one").hold_for(3).on(footage.end + 0.25)
        t.track("Titles").rename("Opening")
        t.arrange(["recut-occ-1", "recut-occ-0"], at=12, gap=0.5)
        t.clip("old-intro").remove()
        t.add_text("Chapter one", at=12, hold=3, track="Opening", id="chapter-one")

    preview = timeline.commit(recut, label="E14 mixed recut", dry_run=True)
    assert preview["receipt"] is None
    assert preview["data"]["commands"]
    assert preview["data"]["diff"]["changes"]
    assert _snapshot(timeline) == before
    assert len(_history(client, project)) == len(history_before)
    applied = timeline.commit(preview["data"]["commands"], label="E14 mixed recut", key="e14-recut")
    assert applied["receipt"]["receipt_id"]
    assert len(_history(client, project)) == len(history_before) + 1
    rows = applied["data"]["canonical_document"]["parent_revision"]["payload"]["clips"]
    assert {row["id"] for row in rows} >= {
        "footage", "cutaway", "cutaway-right", "title", "chapter-one",
    }
    undone = timeline.undo(publications=[applied["receipt"]["receipt_id"]])
    assert undone["data"]["canonical_document"] == before
    assert len(_history(client, project)) == len(history_before) + 2


def test_e14_friendly_split_preserves_source_windows_and_undoes_exactly(editing_case, tmp_path):
    _daemon, client, project, timeline = editing_case
    picture = _video_file(tmp_path / "split-picture.mp4")
    setup_history = _history(client, project)
    seeded = timeline.commit(
        lambda t: t.add(picture, at=4, hold=6, track="V1", id="split-parent"),
        label="Seed split source", key="e14-seed-split",
    )
    assert seeded["receipt"]["receipt_id"]
    assert len(_history(client, project)) == len(setup_history) + 1
    before = _snapshot(timeline)
    history_before = _history(client, project)
    result = timeline.commit(lambda t: t.clip("split-parent").split(7, id="split-right"),
                             label="E14 friendly split", key="e14-split")
    assert result["receipt"]["receipt_id"]
    assert len(_history(client, project)) == len(history_before) + 1
    rows = {row["id"]: row for row in result["data"]["canonical_document"]["parent_revision"]["payload"]["clips"]}
    assert {"split-parent", "split-right"} <= rows.keys()
    assert rows["split-parent"]["at"] == 4
    assert rows["split-right"]["at"] == 7
    undone = timeline.undo(publications=[result["receipt"]["receipt_id"]])
    assert undone["data"]["canonical_document"] == before
    assert len(_history(client, project)) == len(history_before) + 2


def test_e14_preview_does_not_reserve_a_key_and_keyed_replay_skips_evaluation(editing_case):
    _daemon, client, project, timeline = editing_case
    before = _snapshot(timeline)
    history_before = _history(client, project)
    preview = timeline.commit(lambda t: t.bundle["parent"].update({"name": "Preview only"}),
                              label="Keyed edit", dry_run=True, key="e14-keyed-edit")
    assert preview["receipt"] is None
    assert _snapshot(timeline) == before
    assert len(_history(client, project)) == len(history_before)
    outbox_state = json.loads(timeline.session.outbox.path.read_text(encoding="utf-8"))
    assert outbox_state.get("pending", []) == [] and outbox_state.get("flight") is None
    assert "e14-keyed-edit" not in outbox_state.get("acknowledged", {})
    evaluations = []

    def edit(t):
        evaluations.append(True)
        t.bundle["parent"]["name"] = "Keyed edit"

    accepted = timeline.commit(edit, label="Keyed edit", key="e14-keyed-edit")
    assert accepted["receipt"]["receipt_id"]
    replay = type(timeline).open(project, "film", client=client,
                                 outbox_path=timeline.session.outbox.path).commit(
        lambda _t: pytest.fail("an acknowledged keyed function must replay before evaluation"),
        label="Keyed edit", key="e14-keyed-edit",
    )
    assert replay["receipt"] == accepted["receipt"]
    assert replay["replayed"] is True
    assert evaluations == [True]
    assert len(_history(client, project)) == len(history_before) + 1
    undone = timeline.undo(publications=[accepted["receipt"]["receipt_id"]])
    assert undone["data"]["canonical_document"] == before


def test_e14_create_with_picture_audio_content_and_fps_undoes_to_bootstrap(editing_case, tmp_path):
    daemon, client, project, _timeline = editing_case
    picture = _media_file(tmp_path / "create-picture.png")
    voice = _voice_file(tmp_path / "create-voice.wav")
    picture_upload = client._remote._transport.ingest_project_object(
        project, picture.read_bytes(), media_type="image/png", filename=picture.name,
        idempotency_key="e14-create-picture",
    )
    voice_upload = client._remote._transport.ingest_project_object(
        project, voice.read_bytes(), media_type="audio/wav", filename=voice.name,
        idempotency_key="e14-create-voice",
    )
    # The workspace transport returns the stable {data, receipt} envelope.
    assert picture_upload["data"].get("object_id") and voice_upload["data"].get("object_id")
    picture_id = picture_upload["data"]["object_id"]
    voice_id = voice_upload["data"]["object_id"]
    config = {
        "tracks": [{"id": "picture", "kind": "visual"}, {"id": "voice", "kind": "audio"}],
        "theme_overrides": {"visual": {"canvas": {"width": 640, "height": 360, "fps": 24}}},
        "clips": [
            {"id": "created-picture", "track": "picture", "asset": "picture",
             "clipType": "image", "at": 0, "hold": 2, "params": {}},
            {"id": "created-voice", "track": "voice", "asset": "voice",
             "clipType": "audio", "at": 0, "hold": 2, "params": {}},
        ],
        "registry": {"assets": {
            "picture": {"media_id": picture_id, "type": "image/png"},
            "voice": {"media_id": voice_id, "type": "audio/wav"},
        }},
    }
    created = client.timelines.create_empty(
        project=project, timeline_id="create-with-content", config=config,
        idempotency_key="e14-create-content",
    )
    assert created.ok, created.as_dict()
    content = created.data["canonical_document"]
    payload = content["parent_revision"]["payload"]
    assert payload["config"]["output"]["fps"] == 24
    assert {row["id"] for row in payload["clips"]} == {"created-picture", "created-voice"}
    history = client.timelines.history(project, "create-with-content", limit=100)
    assert history.ok and len(history.data["items"]) == 2
    assert history.data["items"][0]["intent"] == "bootstrap"
    assert history.data["items"][1]["intent"] == "recorded"
    # The shell bootstrap is the pre-initialization state in the authenticated creation history.
    shell_revision = history.data["items"][0]["revision_id"]
    bootstrap = daemon.service._timeline_commits()[0].revision_closure(
        project, "create-with-content", shell_revision,
    )
    opened = type(_timeline).open(project, "create-with-content", client=client,
                                  outbox_path=tmp_path / "create-content-outbox.json")
    undone = opened.undo(publications=[created.receipt.receipt_id])
    assert undone["data"]["canonical_document"] == bootstrap
    restored = undone["data"]["canonical_document"]["parent_revision"]["payload"]
    assert "output" not in restored["config"]


def test_e14_duplicate_uses_complete_fresh_identity_maps_and_separate_setup_receipt(editing_case):
    _daemon, client, project, timeline = editing_case
    from astrid.sdk.timeline.commit import bundle_from_document
    from astrid.sdk.timeline.native_occurrence_create import capture_occurrence_create

    bundle = bundle_from_document(_snapshot(timeline))
    births = _sequential_births(bundle, [dict(
        occurrence_id=f"duplicate-occ-{index}", shot_id=f"duplicate-shot-{index}",
        name=f"Duplicate shot {index}", track="V1", on=index * 3, duration=2,
    ) for index in range(3)])
    setup_history = _history(client, project)
    setup = timeline.commit(births, label="Seed duplicate source", key="e14-seed-duplicate")
    assert setup["receipt"]["receipt_id"]
    assert len(_history(client, project)) == len(setup_history) + 1
    source_before = _snapshot(timeline)
    history_before = _history(client, project)
    duplicated = timeline.duplicate(as_="variant-e14")
    destination_id = duplicated["destination_setup"]["timeline_id"]
    assert duplicated["destination_setup"]["receipt"]
    assert duplicated["copy_receipt"]["receipt_id"]
    assert duplicated["copy_receipt"]["receipt_id"] != duplicated["destination_setup"]["receipt"].get("receipt_id")
    destination = type(timeline).open(project, destination_id, client=client)
    source_doc = _snapshot(timeline)
    destination_doc = _snapshot(destination)
    source_payload = source_doc["parent_revision"]["payload"]
    destination_payload = destination_doc["parent_revision"]["payload"]
    source_occurrences = source_payload["occurrences"]
    destination_occurrences = destination_payload["occurrences"]
    source_shots = {row["shot_id"] for row in source_occurrences}
    destination_shots = {row["shot_id"] for row in destination_occurrences}
    source_ids = {row["occurrence_id"] for row in source_occurrences}
    destination_ids = {row["occurrence_id"] for row in destination_occurrences}
    assert len(destination_occurrences) == len(source_occurrences) == 3
    assert len(destination_shots) == len(source_shots) == 3
    assert len(destination_ids) == len(source_ids) == 3
    assert destination_shots.isdisjoint(source_shots)
    assert destination_ids.isdisjoint(source_ids)
    source_ordered = sorted(source_occurrences, key=lambda row: row["placement"]["start_ms"])
    destination_ordered = sorted(destination_occurrences, key=lambda row: row["placement"]["start_ms"])
    assert [row["placement"] for row in destination_ordered] == [
        row["placement"] for row in source_ordered
    ]
    assert [row["duration_ms"] for row in destination_ordered] == [
        row["duration_ms"] for row in source_ordered
    ]
    assert source_doc == source_before
    destination_history = _timeline_history(client, project, destination_id)
    assert len(destination_history) == 3  # bootstrap, empty initialization, and the copy receipt.
    assert destination_history[-1]["publication_id"] == duplicated["copy_receipt"]["receipt_id"]
    destination_bootstrap = _daemon.service._timeline_commits()[0].revision_closure(
        project, destination_id, destination_history[-2]["revision_id"],
    )
    undone = destination.undo(publications=[duplicated["copy_receipt"]["receipt_id"]])
    assert undone["data"]["canonical_document"] == destination_bootstrap


def test_e14_duplicate_unknown_copy_retry_reuses_destination_and_receipt(
    editing_case, monkeypatch,
):
    _daemon, client, project, timeline = editing_case
    from astrid.sdk.timeline.commit import bundle_from_document
    from astrid.sdk.timeline.native_occurrence_create import capture_occurrence_create
    from astrid_runtime_client import ApiError

    bundle = bundle_from_document(_snapshot(timeline))
    births = _sequential_births(bundle, [dict(
        occurrence_id=f"retry-occ-{index}", shot_id=f"retry-shot-{index}",
        name=f"Retry shot {index}", track="V1", on=index * 2, duration=1,
    ) for index in range(2)])
    setup_history = _history(client, project)
    setup = timeline.commit(births, label="Seed duplicate retry", key="e14-seed-duplicate-retry")
    assert setup["receipt"]["receipt_id"]
    assert len(_history(client, project)) == len(setup_history) + 1
    generated = client._remote._transport._generated
    original = generated.commit_timeline
    sent = []

    def lose_copy_response(project_id, timeline_id, body, **kwargs):
        # Only the copy publication loses its reply; destination setup is a separate commit.
        if timeline_id == "variant-e14-retry" and any(
                command.get("op") == "timeline.duplicate" for command in body.get("commands", ())):
            sent.append((copy.deepcopy(body), kwargs["idempotency_key"]))
            result = original(project_id, timeline_id, body, **kwargs)
            if len(sent) == 1:
                raise ApiError(503, "not_ready", "simulated lost duplicate response")
            return result
        return original(project_id, timeline_id, body, **kwargs)

    monkeypatch.setattr(generated, "commit_timeline", lose_copy_response)
    from astrid.sdk.timeline_duplicate import _CopyOutcomeUnknown

    with pytest.raises(_CopyOutcomeUnknown):
        timeline.duplicate(as_="variant-e14-retry")
    recovered = timeline.duplicate(as_="variant-e14-retry")
    assert recovered["copy_receipt"]["receipt_id"]
    assert len(sent) == 2 and sent[0] == sent[1]
    destinations = client.timelines.list(project=project, limit=100)
    assert destinations.ok, destinations.as_dict()
    matching = [row for row in destinations.data[0]
                if row.get("timeline_id") == "variant-e14-retry"]
    assert len(matching) == 1
    history = _timeline_history(client, project, "variant-e14-retry")
    assert len(history) == 3  # destination bootstrap, destination setup, one copy publication.
    assert sum(item.get("publication_id") == recovered["copy_receipt"]["receipt_id"]
               for item in history) == 1
    before_copy = _daemon.service._timeline_commits()[0].revision_closure(
        project, "variant-e14-retry", history[-2]["revision_id"],
    )
    destination = type(timeline).open(project, "variant-e14-retry", client=client)
    undone = destination.undo(publications=[recovered["copy_receipt"]["receipt_id"]])
    assert undone["data"]["canonical_document"] == before_copy


def test_e14_function_conflict_reruns_but_literal_list_conflict_stays_fixed(
    editing_case, tmp_path, monkeypatch,
):
    daemon, client, project, timeline = editing_case
    from astrid.sdk.timeline.sync import CommitConflict
    from tests.editing_conformance.test_commit_runtime_m1 import open_client

    # The Runtime derives the handshake actor from the presented credential.
    _, peer_credential = daemon.credentials.provision("e14-peer", [
        "handshake", "projects:read", "projects:write", "objects:read", "objects:write",
        "tasks:read", "tasks:write",
    ])
    peer_client = open_client(daemon, actor="e14-peer", credential=peer_credential)
    peer = type(timeline).open(project, "film", client=peer_client, outbox_path=tmp_path / "peer.json")
    generated = client._remote._transport._generated
    original = generated.commit_timeline
    injected = False
    peer_state = []

    def advance_then_submit(project_id, timeline_id, body, **kwargs):
        nonlocal injected
        if timeline_id == "film" and not injected and body.get("label") == "E14 function conflict":
            injected = True
            peer.commit(lambda t: t.bundle["parent"].update({"name": "Concurrent editor change"}),
                        label="Concurrent editor change", key="e14-peer-change")
            peer_state.append(_snapshot(peer))
        return original(project_id, timeline_id, body, **kwargs)

    monkeypatch.setattr(generated, "commit_timeline", advance_then_submit)
    history_before = _history(client, project)
    evaluations = []
    prior_name = timeline.bundle["parent"].get("name")

    def function_edit(t):
        evaluations.append(t.bundle["parent"].get("name"))
        t.bundle["parent"]["custom"] = {"function_edit": True}

    result = timeline.commit(function_edit, label="E14 function conflict", key="e14-function-conflict")
    assert result["receipt"]["receipt_id"]
    assert evaluations == [prior_name, "Concurrent editor change"]
    assert len(_history(client, project)) == len(history_before) + 2
    undone = timeline.undo(publications=[result["receipt"]["receipt_id"]])
    assert undone["data"]["canonical_document"] == peer_state[0]

    fixed = timeline.commit(
        lambda t: t.bundle["parent"].update({"custom": {"literal_edit": True}}),
        label="Preview fixed list", dry_run=True,
    )["data"]["commands"]
    stale = type(timeline).open(project, "film", client=client, outbox_path=tmp_path / "literal.json")
    other = type(timeline).open(project, "film", client=peer_client, outbox_path=tmp_path / "literal-peer.json")
    before_list_conflict = _history(client, project)
    peer_edit = other.commit(lambda t: t.bundle["parent"].update({"custom": {"peer_edit": True}}),
                             label="Second concurrent change", key="e14-peer-change-2")
    assert peer_edit["receipt"]["receipt_id"]
    assert len(_history(client, project)) == len(before_list_conflict) + 1
    attempts = []

    def count_fixed_submission(*args, **kwargs):
        attempts.append((copy.deepcopy(args[2]), kwargs["idempotency_key"]))
        return original(*args, **kwargs)

    monkeypatch.setattr(generated, "commit_timeline", count_fixed_submission)
    with pytest.raises(CommitConflict):
        stale.commit(fixed, label="Apply reviewed list", key="e14-fixed-list")
    assert len(attempts) == 1
    assert len(_history(client, project)) == len(before_list_conflict) + 1


@pytest.mark.parametrize("retry_ref", ["error", "id"])
def test_function_rerun_saves_overlapping_user_edit_and_explicit_retry_rebases(
    editing_case, tmp_path, monkeypatch, retry_ref,
):
    daemon, client, project, timeline = editing_case
    from astrid.sdk.timeline import Timeline
    from astrid.sdk.timeline.sync import CommitConflict
    from tests.editing_conformance.test_commit_runtime_m1 import open_client

    source = _media_file(tmp_path / "rerun-anchor.png")
    timeline.commit(lambda t: [t.add(source, at=index * 5, hold=4, track="V1", id=f"clip-{index}")
                               for index in range(1, 4)], label="Seed rerun clips")
    _, credential = daemon.credentials.provision("rerun-peer", [
        "handshake", "projects:read", "projects:write", "objects:read", "objects:write",
        "tasks:read", "tasks:write",
    ])
    peer = Timeline.open(project, "film", client=open_client(daemon, actor="rerun-peer", credential=credential),
                         outbox_path=tmp_path / "rerun-peer.json")
    for root in (peer.bundle["parent"], peer.bundle["parent"].get("config", {})):
        for clip in root.get("clips", []):
            if clip["id"] == "clip-3":
                clip["hold"] = 3.0
    peer_edit = peer.commit(label="User clip 3 duration", key="rerun-peer-duration")
    assert peer_edit["receipt"]["receipt_id"]

    def set_all_durations(t):
        for index in range(1, 4):
            t.clip(f"clip-{index}").set_duration(2.0)

    with pytest.raises(CommitConflict) as caught:
        timeline.commit(set_all_durations, label="Function duration rerun", key="rerun-duration")
    assert caught.value.pending_id
    saved = timeline.pending_list()[0]
    assert saved["clash"]["schema"] == "astrid.clash/2"
    assert any(row["publication_id"] == peer_edit["receipt"]["receipt_id"]
               and row["touches_conflict"] for row in saved["clash"]["receipts"])
    assert any(conflict["path"][-1] == "hold"
               and any(part.endswith("clip-3") for part in conflict["path"])
               for conflict in saved["clash"]["conflicts"]), saved["clash"]["conflicts"]
    current = Timeline.open(project, "film", client=client, outbox_path=tmp_path / "rerun-current.json")
    assert current.clip("clip-3").duration == pytest.approx(3.0)

    if retry_ref == "error":
        rebased = timeline.retry(caught.value, overwrite=True)
    else:
        rebased = timeline.retry(caught.value.pending_id, fn=set_all_durations, overwrite=True)
    assert rebased["status"] in {"accepted", "merged", "rebased"}
    assert "Rebased" in rebased["summary"]
    assert "clip-3" in rebased["summary"] and "hold" in rebased["summary"]
    landed = Timeline.open(project, "film", client=client, outbox_path=tmp_path / "rerun-landed.json")
    assert landed.clip("clip-3").duration == pytest.approx(2.0)

    later = timeline.commit(
        lambda t: t.bundle["parent"].update({"name": "Commit after overwrite retry"}),
        label="Commit after overwrite retry",
        key=f"rerun-after-overwrite-{retry_ref}",
    )
    assert later["receipt"]["receipt_id"]
    assert timeline.bundle["parent"]["name"] == "Commit after overwrite retry"
    saved_after_retry = timeline.pending_show(caught.value.pending_id)
    assert saved_after_retry["state"] == "landed"
    assert saved_after_retry["closed_by"] == {
        "actor_id": client._remote._transport._last_handshake["actor_id"],
        "publication_id": rebased["receipt"]["receipt_id"], "how": "rerun",
    }


def test_function_rerun_preserves_disjoint_user_title_edit(editing_case, tmp_path, monkeypatch):
    daemon, client, project, timeline = editing_case
    from astrid.sdk.timeline import Timeline
    from tests.editing_conformance.test_commit_runtime_m1 import open_client

    source = _media_file(tmp_path / "rerun-disjoint.png")
    timeline.commit(lambda t: [t.add(source, at=index * 5, hold=4, track="V1", id=f"clip-{index}")
                               for index in range(1, 4)], label="Seed disjoint rerun clips")
    _, credential = daemon.credentials.provision("rerun-title-peer", [
        "handshake", "projects:read", "projects:write", "objects:read", "objects:write",
        "tasks:read", "tasks:write",
    ])
    peer = Timeline.open(project, "film", client=open_client(daemon, actor="rerun-title-peer", credential=credential),
                         outbox_path=tmp_path / "rerun-title-peer.json")
    history_before = _history(client, project)
    observed_titles = []
    initial_title = timeline.bundle["parent"].get("name")

    def edit_title_then_submit(project_id, timeline_id, body, **kwargs):
        if timeline_id == "film" and body.get("label") == "Function duration disjoint rerun":
            generated.commit_timeline = original
            peer.commit(lambda t: t.bundle["parent"].update({"name": "User title"}),
                        label="User title edit", key="rerun-peer-title")
        return original(project_id, timeline_id, body, **kwargs)

    generated = client._remote._transport._generated
    original = generated.commit_timeline
    monkeypatch.setattr(generated, "commit_timeline", edit_title_then_submit)

    def set_all_durations(t):
        observed_titles.append(t.bundle["parent"].get("name"))
        for index in range(1, 4):
            t.clip(f"clip-{index}").set_duration(2.0)

    result = timeline.commit(set_all_durations, label="Function duration disjoint rerun",
                             key="rerun-duration-disjoint")
    assert result["receipt"]["receipt_id"]
    landed = Timeline.open(project, "film", client=client, outbox_path=tmp_path / "rerun-disjoint-landed.json")
    assert observed_titles == [initial_title, "User title"]
    assert len(_history(client, project)) == len(history_before) + 2
    assert landed.bundle["parent"]["name"] == "User title"
    assert [landed.clip(f"clip-{index}").duration for index in range(1, 4)] == [2.0, 2.0, 2.0]


def test_e14_function_lost_fallback_refuses_overlapping_read(
    editing_case, tmp_path, monkeypatch,
):
    daemon, client, project, timeline = editing_case
    from astrid.sdk.timeline.sync import CommitConflict
    from astrid_runtime_client import ApiError
    from tests.editing_conformance.test_commit_runtime_m1 import open_client

    _, credential = daemon.credentials.provision("e14-fallback-peer", [
        "handshake", "projects:read", "projects:write", "objects:read", "objects:write",
        "tasks:read", "tasks:write",
    ])
    peer_client = open_client(daemon, actor="e14-fallback-peer", credential=credential)
    peer = type(timeline).open(
        project, "film", client=peer_client, outbox_path=tmp_path / "fallback-peer.json"
    )
    generated = client._remote._transport._generated
    original = generated.commit_timeline
    history_before = _history(client, project)
    peer_writes = []
    evaluations = []

    def advance_before_attempt(project_id, timeline_id, body, **kwargs):
        if body.get("label") == "E14 function fallback" and len(peer_writes) < 2:
            name = f"Peer edit {len(peer_writes) + 1}"
            peer_writes.append(peer.commit(
                lambda t, name=name: t.bundle["parent"].update({"name": name}),
                label=name,
                key=f"e14-fallback-peer-{len(peer_writes) + 1}",
            ))
        elif body.get("label") == "E14 function fallback":
            raise ApiError(409, "conflict", "injected conflict after disjoint peer edits", details={
                "status": "conflict", "reason": "value_conflict", "current": dict(peer.head),
            })
        return original(project_id, timeline_id, body, **kwargs)

    monkeypatch.setattr(generated, "commit_timeline", advance_before_attempt)

    dependency = {"kind": "scope", "ref": "parent", "path": []}

    def edit(t):
        evaluations.append(t.bundle["parent"].get("name"))
        t.bundle["parent"]["custom"] = {"function_edit": t.bundle["parent"].get("name")}

    with pytest.raises(CommitConflict) as caught:
        timeline.commit(edit, label="E14 function fallback", key="e14-function-fallback",
                        merge="fields", reads=[dependency])
    assert len(evaluations) == 3
    assert len(peer_writes) == 2
    assert caught.value.pending_id
    assert len(_history(client, project)) == len(history_before) + 2

    # Reopening discards the in-memory callable; retry must offer a list preview.
    reopened = type(timeline).open(
        project, "film", client=client, outbox_path=timeline.session.outbox.path
    )
    monkeypatch.setattr(generated, "commit_timeline", original)
    preview = reopened.retry(caught.value.pending_id)
    assert preview["status"] == "preview"
    assert preview["pending_id"] == caught.value.pending_id
    assert len(evaluations) == 3

    late_peer_edit = peer.commit(
        lambda t: t.bundle["parent"].update({"name": "Peer edit after preview"}),
        label="Peer edit after preview",
        key="e14-fallback-peer-after-preview",
    )
    assert late_peer_edit["receipt"]["receipt_id"]
    history_after_peer = _history(client, project)
    with pytest.raises(CommitConflict) as late_conflict:
        reopened.commit(preview)
    assert late_conflict.value.pending_id == caught.value.pending_id
    saved = reopened.pending_list()
    assert any(record["id"] == caught.value.pending_id and record["state"] == "pending"
               for record in saved)
    current = type(timeline).open(
        project, "film", client=client, outbox_path=tmp_path / "fallback-current.json"
    )
    assert current.bundle["parent"]["name"] == "Peer edit after preview"
    assert current.bundle["parent"].get("custom") is None
    assert len(_history(client, project)) == len(history_after_peer)
    assert len(evaluations) == 3


def test_e14_function_lost_fallback_merges_disjoint_edit(
    editing_case, tmp_path, monkeypatch,
):
    daemon, client, project, timeline = editing_case
    from astrid.sdk.timeline.sync import CommitConflict
    from astrid_runtime_client import ApiError
    from tests.editing_conformance.test_commit_runtime_m1 import open_client

    _, credential = daemon.credentials.provision("e14-disjoint-fallback-peer", [
        "handshake", "projects:read", "projects:write", "objects:read", "objects:write",
        "tasks:read", "tasks:write",
    ])
    peer = type(timeline).open(
        project,
        "film",
        client=open_client(daemon, actor="e14-disjoint-fallback-peer", credential=credential),
        outbox_path=tmp_path / "disjoint-fallback-peer.json",
    )
    generated = client._remote._transport._generated
    original = generated.commit_timeline
    history_before = _history(client, project)
    peer_writes = []

    def advance_before_attempt(project_id, timeline_id, body, **kwargs):
        if body.get("label") == "E14 disjoint function fallback" and len(peer_writes) < 2:
            name = f"Disjoint peer edit {len(peer_writes) + 1}"
            peer_writes.append(peer.commit(
                lambda t, name=name: t.bundle["parent"].update({"name": name}),
                label=name,
                key=f"e14-disjoint-fallback-peer-{len(peer_writes) + 1}",
            ))
        elif body.get("label") == "E14 disjoint function fallback":
            raise ApiError(409, "conflict", "injected retry exhaustion after disjoint peer edits", details={
                "status": "conflict", "reason": "value_conflict", "current": dict(peer.head),
            })
        return original(project_id, timeline_id, body, **kwargs)

    monkeypatch.setattr(generated, "commit_timeline", advance_before_attempt)

    def edit(t):
        t.bundle["parent"]["custom"] = {"function_edit": True}

    with pytest.raises(CommitConflict) as caught:
        timeline.commit(edit, label="E14 disjoint function fallback", key="e14-disjoint-function-fallback")
    assert len(peer_writes) == 2
    assert caught.value.pending_id
    assert len(_history(client, project)) == len(history_before) + 2

    reopened = type(timeline).open(
        project, "film", client=client, outbox_path=timeline.session.outbox.path
    )
    monkeypatch.setattr(generated, "commit_timeline", original)
    preview = reopened.retry(caught.value.pending_id)
    assert preview["status"] == "preview"
    assert preview["pending_id"] == caught.value.pending_id
    assert preview["data"]["diff"]

    landed = reopened.commit(preview)
    assert landed["status"] in {"accepted", "merged"}
    current = type(timeline).open(
        project, "film", client=client, outbox_path=tmp_path / "disjoint-fallback-current.json"
    )
    assert current.bundle["parent"]["name"] == "Disjoint peer edit 2"
    assert current.bundle["parent"]["custom"] == {"function_edit": True}
    saved = reopened.pending_show(caught.value.pending_id)
    assert saved["state"] == "landed"


def test_e14_explicit_atomic_list_refuses_disjoint_intervening_edit(
    editing_case, tmp_path,
):
    daemon, client, project, timeline = editing_case
    from astrid.sdk.timeline.sync import CommitConflict
    from tests.editing_conformance.test_commit_runtime_m1 import open_client

    _, credential = daemon.credentials.provision("e14-atomic-peer", [
        "handshake", "projects:read", "projects:write", "objects:read", "objects:write",
        "tasks:read", "tasks:write",
    ])
    peer_client = open_client(daemon, actor="e14-atomic-peer", credential=credential)
    peer = type(timeline).open(
        project, "film", client=peer_client, outbox_path=tmp_path / "atomic-peer.json"
    )
    atomic_commands = timeline.commit(
        lambda t: t.bundle["parent"].update({"custom": {"atomic_edit": True}}),
        label="E14 explicit atomic",
        dry_run=True,
    )["data"]["commands"]
    before = _history(client, project)
    peer_result = peer.commit(
        lambda t: t.bundle["parent"].update({"name": "Disjoint peer edit"}),
        label="Disjoint peer edit",
        key="e14-atomic-peer-edit",
    )
    assert peer_result["receipt"]["receipt_id"]

    with pytest.raises(CommitConflict) as caught:
        timeline.commit(
            atomic_commands,
            label="E14 explicit atomic",
            merge="atomic",
            key="e14-explicit-atomic",
        )
    assert caught.value.outcome == "not_applied"
    assert len(_history(client, project)) == len(before) + 1
    assert _snapshot(peer)["parent_revision"]["payload"].get("custom") is None


def test_e14_final_refusal_releases_outbox_for_new_intent_and_exact_undo(
    editing_case, tmp_path,
):
    _daemon, client, project, timeline = editing_case
    from astrid.sdk.timeline.sync import CommitConflict
    from tests.editing_conformance.test_doc_patch_v2 import change, patch

    setup_history = _history(client, project)
    seed = timeline.commit(lambda t: t.bundle["parent"].update({"custom": {"seed": True}}),
                           label="Seed refusal recovery", key="e14-seed-refusal")
    assert seed["receipt"]["receipt_id"]
    assert len(_history(client, project)) == len(setup_history) + 1
    before = _snapshot(timeline)
    history_before = _history(client, project)
    fps = before["parent_revision"]["payload"]["config"].get("output", {}).get("fps")
    if fps is None:
        setup = timeline.commit(lambda t: t.bundle["parent"]["config"].setdefault("output", {}).update({"fps": 30}),
                                label="Declare FPS for refusal case", key="e14-seed-fps")
        assert setup["receipt"]["receipt_id"]
        before = _snapshot(timeline)
        history_before = _history(client, project)
        fps = before["parent_revision"]["payload"]["config"]["output"]["fps"]
    removal = change(["config", "output", "fps"], {"state": "present", "value": fps}, op="remove")
    with pytest.raises(CommitConflict):
        timeline.commit([patch(removal)], label="Refused arbitrary FPS removal", key="e14-refused-fps")
    assert len(_history(client, project)) == len(history_before)
    after_refusal = _snapshot(timeline)
    result = timeline.commit(
        lambda t: t.bundle["parent"].update({"name": "New intent after refusal"}),
        label="New intent after refusal", key="e14-new-intent",
    )
    assert result["receipt"]["receipt_id"]
    assert len(_history(client, project)) == len(history_before) + 1
    undone = timeline.undo(publications=[result["receipt"]["receipt_id"]])
    assert undone["data"]["canonical_document"] == after_refusal


def test_e14_unknown_commit_reopens_and_retries_the_exact_request(editing_case, tmp_path, monkeypatch):
    _daemon, client, project, timeline = editing_case
    from astrid.sdk.timeline.sync import CommitOutcomeUnknown
    from astrid_runtime_client import ApiError

    before = _snapshot(timeline)
    history_before = _history(client, project)
    generated = client._remote._transport._generated
    original = generated.commit_timeline
    sent = []

    def lose_first_response(*args, **kwargs):
        sent.append((copy.deepcopy(args[2]), kwargs["idempotency_key"]))
        result = original(*args, **kwargs)
        if len(sent) == 1:
            raise ApiError(503, "not_ready", "simulated lost response after acceptance")
        return result

    monkeypatch.setattr(generated, "commit_timeline", lose_first_response)
    evaluations = []

    def edit(t):
        evaluations.append(True)
        t.bundle["parent"]["name"] = "Recovered exact retry"

    outbox = tmp_path / "lost-response.json"
    timeline = type(timeline).open(project, "film", client=client, outbox_path=outbox)
    with pytest.raises(CommitOutcomeUnknown):
        timeline.commit(edit, label="Recovered exact retry", key="e14-lost-response")
    flight = json.loads(outbox.read_text(encoding="utf-8"))["flight"]
    reopened = type(timeline).open(project, "film", client=client, outbox_path=outbox)
    retried = reopened.commit(lambda _t: pytest.fail("unknown outcomes retry stored bytes, not intent"))
    assert retried["receipt"]["receipt_id"]
    assert evaluations == [True]
    assert len(sent) == 2 and sent[0] == sent[1]
    assert sent[1][1] == flight["key"] == "e14-lost-response"
    assert len(_history(client, project)) == len(history_before) + 1
    undone = reopened.undo(publications=[retried["receipt"]["receipt_id"]])
    assert undone["data"]["canonical_document"] == before
    assert len(_history(client, project)) == len(history_before) + 2

