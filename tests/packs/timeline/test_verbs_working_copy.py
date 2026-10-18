"""The non-edit working-copy verbs: lint, lines, words, discard, duplicate (restored from f840ec1c)."""
from __future__ import annotations

import copy
import json
import re
from pathlib import Path

import pytest

from astrid.packs.timeline import cli
from astrid.sdk import timeline_checkout as tc
from astrid.sdk.timeline_checkout import Checkout, TimelineEditError
from astrid.sdk.timeline_duplicate import copy_content, id_map

FPS = 30


def _shot(clips, tracks=None):
    internal = {
            "tracks": tracks or [
                {"id": "type", "kind": "visual"}, {"id": "sprite", "kind": "visual"}, {"id": "plate", "kind": "visual"},
                {"id": "vo", "kind": "audio"}, {"id": "music", "kind": "audio"},
            ],
            "clips": clips,
            "registry": {"assets": {"P": {"media_id": "sha256:" + "1" * 64}, "R": {"media_id": "sha256:" + "2" * 64}, "Q": {"media_id": "sha256:" + "3" * 64}}},
    }
    payload = {"name": "SHOT", "items": [], "pools": [], "audio_bindings": [], "text_bindings": []}
    return {
        "payload": payload, "base_payload": copy.deepcopy(payload),
        "internal_timeline": internal, "base_internal_timeline": copy.deepcopy(internal),
    }


def _placement_rows():
    return {
        "occ-A": {"shot_id": "A", "occurrence_id": "occ-A", "placement": {"start_ms": 0}, "duration_ms": 4000},
        "occ-B": {"shot_id": "B", "occurrence_id": "occ-B", "placement": {"start_ms": 4000}, "duration_ms": 4000},
    }


def _shot_identity(sid):
    digest = "sha256:" + "0" * 64
    return {"shot_id": sid, "revision_id": f"rev-{sid}", "content_digest": digest, "internal_timeline_id": "t",
            "internal_timeline_revision_id": f"itr-{sid}", "internal_timeline_content_digest": digest, "item_id_map": {}}


def bundle():
    """Two shots: A (0–4 s) and B (4–8 s). Shot A's VO says "it went viral"; B says "Live now"."""
    a = [
        {"id": "a-plate", "clipType": "am-snap-plate", "track": "plate", "asset": "P", "at": 0.0, "hold": 4.0, "params": {}},
        {"id": "a-rocket", "clipType": "am-sprite", "track": "sprite", "asset": "R", "at": 1.0, "hold": 1.0, "params": {"x": 10}},
        {"id": "a-vo", "clipType": "media", "track": "vo", "asset": "P", "at": 0.5, "from": 0.0, "to": 2.0,
         "app": {"segment": "s1", "words": [[0.1, 0.3, "it"], [0.4, 0.6, "went"], [0.8, 1.2, "viral"]]}},
        {"id": "a-music", "clipType": "media", "track": "music", "asset": "Q", "at": 0.0, "from": 0.0, "to": 4.0},
    ]
    b = [
        {"id": "b-plate", "clipType": "am-snap-plate", "track": "plate", "asset": "Q", "at": 0.0, "hold": 4.0, "params": {}},
        {"id": "b-type", "clipType": "am-type", "track": "type", "at": 2.0, "hold": 1.0, "params": {"text": "Live."}},
        {"id": "b-vo", "clipType": "media", "track": "vo", "asset": "P", "at": 1.9, "from": 0.0, "to": 1.0,
         "app": {"segment": "s2", "words": [[0.1, 0.4, "Live"], [0.5, 0.8, "now"]]}},
    ]
    return {
        "project_id": "p", "timeline_id": "t", "schema_version": 1,
        "base_parent": {"revision_id": "rev-0", "content_digest": "sha256:" + "0" * 64},
        "base_parent_payload": {"config": {"theme_overrides": {"visual": {"canvas": {"fps": FPS, "width": 1920, "height": 1080}}}}, "clips": [], "registry": {}},
        "base_placements": {"placements": _placement_rows()},
        "parent": {"config": {"theme_overrides": {"visual": {"canvas": {"fps": FPS, "width": 1920, "height": 1080}}}}, "clips": []},
        "placements": [
            {"shot_id": "A", "occurrence_id": "occ-A", "placement": {"start_ms": 0}, "duration_ms": 4000},
            {"shot_id": "B", "occurrence_id": "occ-B", "placement": {"start_ms": 4000}, "duration_ms": 4000},
        ],
        "shots": {sid: {**_shot(clips), "shot_id": sid} for sid, clips in (("A", a), ("B", b))},
        "source_mapping": {"placements": _placement_rows(), "shots": {sid: _shot_identity(sid) for sid in ("A", "B")}},
    }


def _parse(*argv):
    parser = cli.build_parser(None)
    return parser.parse_args(["timelines", *argv][1:] if argv and argv[0] == "timelines" else list(argv))


def _run(*argv) -> int:
    parsed = _parse(*argv)
    return parsed.handler(parsed)


@pytest.fixture
def local(tmp_path):
    path = tmp_path / "t.checkout.json"
    Checkout(bundle()).save(path)
    return path


# ---- argument parsing ------------------------------------------------------------------

@pytest.mark.parametrize("argv, name", [
    (["words", "tl1", "--project", "P", "--find", "viral", "--range", "1:02..1:10"], "_cmd_words"),
    (["lines", "tl1", "--project", "P", "--line", "s1"], "_cmd_lines"),
    (["lint", "tl1", "--project", "P", "--cut", "3", "--strict"], "_cmd_lint"),
    (["discard", "tl1", "--project", "P"], "_cmd_discard"),
    (["duplicate", "tl1", "--project", "P", "--slug", "dup", "--name", "Dup"], "_cmd_duplicate"),
])
def test_each_verb_parses_to_its_handler(argv, name):
    from astrid.packs.timeline import cli_working

    assert _parse(*argv).handler is getattr(cli_working, name)


def test_the_freeze_command_list_is_all_registered():
    names = {spec.name for spec in cli.COMMANDS}
    freeze = ("create list show replace-parent-media archive recover history log diff visualize lint inspect render "
              "checkout edit apply lines find undo redo words status check publish discard duplicate").split()
    assert set(freeze) <= names


# ---- words -----------------------------------------------------------------------------

def test_words_find_format_is_one_line_per_match(local, capsys):
    assert _run("words", "--file", str(local), "--find", "viral") == 0
    lines = capsys.readouterr().out.splitlines()
    assert re.fullmatch(r"  \d+\.\d{3}–\d+\.\d{3}  viral  \[s1:2\]  (cut \d+|c\d+[a-z]?)", lines[0]), lines[0]
    assert len(lines) == 2  # one match plus the next-step line
    assert lines[-1].startswith("next: ")


def test_words_range_limits_the_listing(local, capsys):
    assert _run("words", "--file", str(local), "--range", "0..2") == 0
    texts = [ln.split()[1] for ln in capsys.readouterr().out.splitlines() if ln.startswith("  ")]
    assert texts == ["it", "went", "viral"]


# ---- the duplicate id mapping (pure part) ----------------------------------------------

def test_id_map_keeps_suffixes_and_is_one_to_one():
    assert id_map(["almost-ready-90-v7-ch01-tomorrow", "almost-ready-90-v7-ch02-vision"], "dup") == {
        "almost-ready-90-v7-ch01-tomorrow": "dup-ch01-tomorrow",
        "almost-ready-90-v7-ch02-vision": "dup-ch02-vision",
    }
    assert id_map(["A", "B"], "dup") == {"A": "dup-A", "B": "dup-B"}
    with pytest.raises(tc.TimelineEditError):
        id_map(["A", "A"], "dup")


def test_copy_content_gives_new_shot_and_occurrence_ids_and_keeps_identity():
    source = bundle()
    target = {
        "project_id": "p", "timeline_id": "dup-tl", "base_parent": {"revision_id": "rev-empty"},
        "parent": {"config": {}, "clips": []}, "placements": [], "shots": {},
        "source_mapping": {"placements": {}, "shots": {}},
    }
    out, notes = copy_content(source, target, new_slug="dup")
    assert out["base_parent"] == {"revision_id": "rev-empty"} and out["timeline_id"] == "dup-tl"
    assert sorted(out["shots"]) == ["dup-A", "dup-B"]
    assert [row["occurrence_id"] for row in out["placements"]] == ["occ-dup-A", "occ-dup-B"]
    assert out["placements"][0]["duration_ms"] == 4000
    # the internal clips are copied with their own ids (they are per shot)
    assert [c["id"] for c in out["shots"]["dup-A"]["internal_timeline"]["clips"]] == \
        [c["id"] for c in source["shots"]["A"]["internal_timeline"]["clips"]]
    assert notes == []


def test_copy_content_reports_dropped_text_bindings():
    source = bundle()
    source["shots"]["A"]["payload"]["text_bindings"] = [{"binding_id": "b1"}]
    target = {"project_id": "p", "timeline_id": "t2", "base_parent": {}, "parent": {}, "placements": [], "shots": {},
              "source_mapping": {"placements": {}, "shots": {}}}
    out, notes = copy_content(source, target, new_slug="dup")
    assert out["shots"]["dup-A"]["payload"]["text_bindings"] == []
    assert notes and "1 narration text binding" in notes[0]


# ---- drafts, offline: data root in tmp, resolve_ids and fetch_bundle replaced ----------------

def test_draft_flow_checkout_edit_status_discard(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("BANODOCO_LOCAL_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setattr(tc, "resolve_ids", lambda project, timeline, client=None: ("p", "t", "rev-0"))
    monkeypatch.setattr(tc, "fetch_bundle", lambda project, timeline, revision_id=None, client=None: copy.deepcopy(bundle()))

    assert _run("checkout", "t", "--project", "P") == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith('working copy "main" of t · from published rev-0 · ')
    assert "next:" in out and any(line.strip().startswith("publish  timelines status t --project P") for line in out)

    assert _run("status", "t", "--project", "P", "--text") == 0
    assert capsys.readouterr().out.startswith('WORKING COPY "main" · 0 clips changed')

    assert _run("edit", "t", "--project", "P", "--clip", "R", "--at-word", "viral") == 0
    out = capsys.readouterr().out
    assert out.strip()  # the edit says what it did (its wording belongs to the edit verb's tests)

    assert _run("status", "t", "--project", "P", "--text") == 0
    status = capsys.readouterr().out
    assert status.startswith('WORKING COPY "main" · 1 clip changed vs published rev-0')
    assert "a-rocket" in status

    assert _run("discard", "t", "--project", "P") == 0
    assert "1 clip changed, dropped" in capsys.readouterr().out
    assert _run("status", "t", "--project", "P", "--text") == 0
    assert "no working copy" in capsys.readouterr().out


# ---- voice lines (timeline-level; the film re-flows) -----------------------------------

def _words_file(tmp_path, name, rows):
    path = tmp_path / name
    path.write_text(json.dumps(rows), encoding="utf-8")
    return str(path)




class _Opened:
    def __init__(self, data):
        self.ok, self.data, self.error, self.receipt, self.idempotency_key = True, data, None, None, None


class _Timelines:
    def open_bundle(self, project, ref, revision_id=None):
        return _Opened({"bundle": copy.deepcopy(bundle()), "timeline_id": "t", "revision_id": "rev-0",
                        "project_id": "p", "is_current_head": True, "head_revision_id": "rev-0"})


class _Client:
    timelines = _Timelines()


def _run_with_client(*argv):
    parsed = _parse(*argv)
    parsed.client = _Client()
    return parsed.handler(parsed)


def test_lint_cli_marks_estimated_geometry_only(capsys):
    """The disposable CLI report labels estimated boxes but leaves timing findings alone."""
    from types import SimpleNamespace

    fixture = bundle()
    shot = copy.deepcopy(fixture["shots"]["A"])
    shot["internal_timeline"]["tracks"] = [
        {"id": "type", "kind": "visual"}, {"id": "plate", "kind": "visual"},
    ]
    shot["internal_timeline"]["clips"] = [
        {"id": "plate", "clipType": "am-snap-plate", "track": "plate", "at": 0.0, "hold": 0.25,
         "params": {}},
        {"id": "type", "clipType": "am-type", "track": "type", "at": 0.0, "hold": 0.25,
         "params": {"text": "TWO WORDS", "size": 20, "x": 1760, "y": 900, "width": 400}},
    ]
    fixture["shots"] = {"A": shot}
    fixture["placements"] = [
        {"shot_id": "A", "occurrence_id": "occ-A", "placement": {"start_ms": 0}, "duration_ms": 250},
    ]

    class Timelines:
        def open_bundle(self, project, ref, revision_id=None):
            return _Opened({"bundle": copy.deepcopy(fixture), "timeline_id": "t", "revision_id": "fixture"})

    parsed = _parse("lint", "t", "--project", "P", "--published", "--no-rules", "--json")
    parsed.client = SimpleNamespace(timelines=Timelines())
    assert parsed.handler(parsed) == 0

    findings = json.loads(capsys.readouterr().out)["data"]["findings"]
    assert {finding["code"] for finding in findings} == {"SAFE", "SMALL", "SHORT"}
    safe = next(finding for finding in findings if finding["code"] == "SAFE")
    from astrid.core.timeline import text_fit

    if text_fit.available():
        assert "60 px outside title-safe" in safe["message"]
    else:
        # The fallback estimate uses a 90 px box: x=1760 is 26 px beyond 1824.
        assert "26 px outside title-safe (right)" in safe["message"]
        assert "set params.x ≤ 1734" in safe["message"]
    assert "estimated geometry" in safe["message"]
    small = next(finding for finding in findings if finding["code"] == "SMALL")
    assert "20 px < 32 px at 1080p" in small["message"]
    assert "estimated geometry" in small["message"]
    short = next(finding for finding in findings if finding["code"] == "SHORT")
    assert "0.25 s (< 0.5 s)" in short["message"]
    assert "estimated" not in short["message"]


@pytest.fixture
def draft_with_edit(tmp_path, monkeypatch):
    monkeypatch.setenv("BANODOCO_LOCAL_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setattr(tc, "resolve_ids", lambda project, timeline, client=None: ("p", "t", "rev-0"))
    monkeypatch.setattr(tc, "fetch_bundle", lambda project, timeline, revision_id=None, client=None: copy.deepcopy(bundle()))
    assert _run("checkout", "t", "--project", "P") == 0
    assert _run("edit", "t", "--project", "P", "--clip", "R", "--nudge-frames", "3") == 0


def test_lint_reads_the_working_copy_by_default(draft_with_edit, capsys):
    capsys.readouterr()
    _run_with_client("lint", "t", "--project", "P")
    first = capsys.readouterr().out.splitlines()[0]
    assert first.startswith('WORKING COPY · 1 clip changed vs published rev-0')


def test_lint_published_has_no_banner(draft_with_edit, capsys):
    capsys.readouterr()
    _run_with_client("lint", "t", "--project", "P", "--published")
    assert not capsys.readouterr().out.startswith("WORKING COPY")


def test_diff_without_from_compares_head_with_working_copy(draft_with_edit, capsys):
    capsys.readouterr()
    assert _run_with_client("diff", "t", "--project", "P") == 0
    out = capsys.readouterr().out
    assert out.startswith('WORKING COPY · 1 clip changed vs published rev-0')
    assert "a-rocket" in out
    assert "working copy" in out



def test_duplicate_is_all_or_nothing(monkeypatch, tmp_path):
    """A refused duplicate leaves no empty timeline: an invalid head is refused before creating, a failure
    after creating archives what was created."""
    from types import SimpleNamespace

    from astrid.sdk import timeline_duplicate as td

    monkeypatch.setenv("BANODOCO_LOCAL_DATA_ROOT", str(tmp_path / "data"))
    calls, heads = [], []

    class Timelines:
        def head_canonical(self, project, ref):
            assert project == "p" and ref in {"t", "dup-1"}
            heads.append(ref)
            return SimpleNamespace(ok=True, data={
                "revision_id": "rev-0" if ref == "t" else "dup-rev-0",
                "publication_id": "pub-source" if ref == "t" else "pub-bootstrap",
            })

        def create_empty(self, **kw):
            assert kw["duplicate_source"] == {
                "timeline_id": "t", "expected_revision_id": "rev-0",
                "expected_publication_id": "pub-source",
            }
            calls.append("create")
            return SimpleNamespace(ok=True, data={"timeline_id": "dup-1"})

        def _call_generated(self, operation, project, ref):
            assert (operation, project, ref) == ("get_project_timeline", "p", "dup-1")
            return {"version": 7}

        def open_composition(self, project, ref):
            assert (project, ref) == ("p", "dup-1")
            return SimpleNamespace(ok=True, data={
                "native_inspection": {"project_id": project, "timeline_id": ref},
                "summary": {"head_revision_id": "dup-rev-0"},
            })

        def open_bundle(self, project, ref, *, revision_id=None):
            assert (project, ref) == ("p", "dup-1")
            return SimpleNamespace(ok=True, data={"bundle": {
                "project_id": project, "timeline_id": ref,
                "base_parent": {"revision_id": "dup-rev-0"},
                "parent": {"config": {}, "clips": []}, "placements": [], "shots": {},
                "source_mapping": {"placements": {}, "shots": {}},
            }})

        def list_project_parent_composition_revisions(self, project, ref, *, limit):
            assert (project, ref, limit) == ("p", "t", 200)
            # The current duplicate path imports a pinned closure, rather
            # than calling the legacy copy_content helper.
            raise TimelineEditError("the copy failed")

        def archive(self, project, ref, **kw):
            assert (project, ref) == ("p", "dup-1")
            assert kw == {
                "expected_version": 7,
                "withdraw_publication": {
                    "expected_revision_id": "dup-rev-0",
                    "expected_publication_id": "pub-bootstrap",
                },
                "idempotency_key": "duplicate-rollback-dup-1-dup-rev-0",
            }
            calls.append(f"archive {ref}")
            return SimpleNamespace(ok=True)

    client = SimpleNamespace(timelines=Timelines())
    client._transport = client.timelines
    bad = Checkout(bundle())
    bad.clip("a-rocket").data["params"]["scale"] = '"oops"'  # text in a number: check blocks it
    monkeypatch.setattr(td.Checkout, "open", classmethod(lambda cls, p, t, client=None: Checkout(copy.deepcopy(bad.bundle))))
    with pytest.raises(TimelineEditError, match="nothing was created"):
        td._duplicate(client, "p", "t", slug=None)
    assert calls == []
    assert heads == []
    good = Checkout(bundle())
    good.check = lambda: SimpleNamespace(valid=True, blocking=[], problems=[])
    monkeypatch.setattr(td.Checkout, "open", classmethod(lambda cls, p, t, client=None: good))
    # Keep the same safety predicate, using the current acknowledged-archive
    # wording and retaining the injected copy failure in the diagnostic.
    with pytest.raises(TimelineEditError, match=r"the copy failed .*new timeline 'dup-1' was archived"):
        td._duplicate(client, "p", "t", slug=None)
    assert calls == ["create", "archive dup-1"]
    assert heads == ["t", "dup-1"]



# ---- apply: a sheet exported before a later edit never reverts it --------------------------------

def test_a_sheet_exported_before_an_edit_never_reverts_it(tmp_path, monkeypatch, capsys):
    """export -> edit via the verb -> apply the OLD sheet with an unrelated change: both survive; a clash refuses."""
    from pathlib import Path

    from astrid.sdk.timeline_sheet import render_sheet

    root = Path(__file__).resolve().parents[2] / "fixtures" / "timeline_editing"
    tiny = json.loads((root / "tiny.json").read_text(encoding="utf-8"))
    monkeypatch.setenv("BANODOCO_LOCAL_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setattr(tc, "resolve_ids", lambda project, timeline, client=None: ("p", "t", "rev-0"))
    monkeypatch.setattr(tc, "fetch_bundle", lambda project, timeline, revision_id=None, client=None: copy.deepcopy(tiny))
    assert _run("checkout", "t", "--project", "P") == 0
    old = render_sheet(Checkout.draft("P", "t"))
    sheet_file = tmp_path / "old.sheet"
    assert _run("edit", "t", "--project", "P", "--clip", "c1.rocket", "--set", "x=120") == 0  # after the export
    sheet_file.write_text(old.replace('"Live."', '"Now live."'), encoding="utf-8")  # an unrelated change
    capsys.readouterr()
    assert _run("apply", "t", "--project", "P", str(sheet_file)) == 0
    tl = Checkout.draft("P", "t")
    assert tl.clip("c1.rocket").get("x") == 120 and tl.clip("c2.card").text == "Now live."
    # the same line changed in both: refused, named, nothing applied
    sheet_file.write_text(old.replace("x=60", "x=90"), encoding="utf-8")
    assert _run("apply", "t", "--project", "P", str(sheet_file)) == 2
    err = capsys.readouterr().err
    assert "c1.rocket" in err and "Re-export" in err
    assert Checkout.draft("P", "t").clip("c1.rocket").get("x") == 120


def _prepare_apply_file_draft(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[2] / "fixtures" / "timeline_editing"
    tiny = json.loads((root / "tiny.json").read_text(encoding="utf-8"))
    # Current undo retains cut reservations; start this two-cut draft with its
    # existing mark so the exact-document assertion measures only file edits.
    tiny["parent"]["config"].setdefault("app", {}).setdefault("astrid", {})["cut_id_high_water"] = {
        "number": 2, "suffixes": {},
    }
    monkeypatch.setenv("BANODOCO_LOCAL_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setattr(tc, "resolve_ids", lambda project, timeline, client=None: ("p", "t", "rev-0"))
    monkeypatch.setattr(tc, "fetch_bundle", lambda project, timeline, revision_id=None, client=None: copy.deepcopy(tiny))
    assert _run("checkout", "t", "--project", "P") == 0
    return Checkout.draft("P", "t")


def _json_files(folder):
    return {path.name: path.read_bytes() for path in folder.glob("*.json")}


def test_apply_file_single_opcall_noop_matches_cli_and_python_records(tmp_path, monkeypatch, capsys):
    from astrid.sdk.timeline import journal, store
    from astrid.sdk.timeline.apply_file import apply_file
    from astrid.sdk.timeline.commands import run as run_command

    def exercise(route):
        workspace = tmp_path / route
        workspace.mkdir()
        monkeypatch.setenv("BANODOCO_LOCAL_DATA_ROOT", str(workspace / "data"))
        tl = _prepare_apply_file_draft(workspace, monkeypatch)
        capsys.readouterr()
        value = tl.clip("c1.rocket").get("x")
        assert run_command(tl, {"op": "clip.set", "select": "c1.rocket", "args": {"x": 120}}).changed
        assert len(tl.undo()) == 1

        call = {"op": "clip.set", "select": "c1.rocket", "args": {"x": value}}
        path = Path(tl.path)
        journal_path = journal.journal_path("p", "t")
        history_dir = store._history_dir(path)
        redo_dir = store._redo_dir(path)
        document_bytes = path.read_bytes()
        records_before = journal.read(journal_path)
        history_before, redo_before = _json_files(history_dir), _json_files(redo_dir)

        if route == "cli":
            assert _run("edit", "--file", str(path), "--command", json.dumps(call)) == 3
            capsys.readouterr()
        elif route == "sheet":
            sheet = workspace / "noop.sheet"
            sheet.write_text("edit " + json.dumps(call, separators=(",", ":")) + "\n", encoding="utf-8")
            assert apply_file(tl, sheet) == []
        else:
            assert not run_command(tl, call).changed

        records = journal.read(journal_path)
        assert len(records) == len(records_before) + 1
        record = records[-1]
        assert record.kind == "edit" and record.outcome == "no-change"
        assert record.label == call["op"] and len(record.calls) == 1
        assert record.calls[0]["op"] == call["op"]
        assert record.before == record.after and record.before
        assert record.undo_step is None
        assert path.read_bytes() == document_bytes
        assert _json_files(history_dir) == history_before
        assert _json_files(redo_dir) == redo_before
        assert journal.verify(journal_path) == []
        assert len(Checkout.load(path).redo()) == 1
        return record.to_json()

    records = [exercise(route) for route in ("cli", "sheet", "python")]
    variable = {"source", "actor", "at", "id", "seq", "prev", "hash"}
    comparable = [{key: value for key, value in record.items() if key not in variable} for record in records]
    assert comparable[0] == comparable[1] == comparable[2]


def test_apply_file_dynamic_operation_refusal_matches_sdk_rollback_record(tmp_path, monkeypatch, capsys):
    from astrid.core.pack.operations import operation_discovery_scope, resolve_operation
    from astrid.sdk.timeline import journal, store
    from astrid.sdk.timeline.apply_file import apply_file
    from astrid.sdk.timeline.commands import run as run_command

    repo = Path(__file__).resolve().parents[3]
    discovery = {
        "project_root": repo / "out/extension-harness",
        "extra_pack_roots": (str(repo / "examples/packs"),),
    }
    identity = "timeline.operation/beat_tools.fit_to_beats"
    with operation_discovery_scope(**discovery):
        operation = resolve_operation(identity)
    assert operation.implementation_digest == "dfd107e08a5ff42948cc24b83d72f09c42c67cec8f8a8c5cfef2ef417a0e125b"
    call = {
        "op": "timeline.operation",
        "select": None,
        "args": {
            "identity": identity,
            "implementation_digest": operation.implementation_digest,
            "inputs": {
                "clips": ["c1.rocket", "c1.plate"],
                "beats": ["beat 1 after c1", "beat 2 after c1", "beat 3 after c1"],
            },
        },
    }
    label = f"{identity}@{operation.implementation_digest}"
    refusal = "fit_to_beats refuses audio and picture beds; select overlay clips"

    def exercise(route):
        workspace = tmp_path / route
        workspace.mkdir()
        monkeypatch.setenv("BANODOCO_LOCAL_DATA_ROOT", str(workspace / "data"))
        tl = _prepare_apply_file_draft(workspace, monkeypatch)
        capsys.readouterr()
        document_path = Path(tl.path)
        journal_path = journal.journal_path("p", "t")
        history_dir = store._history_dir(document_path)
        redo_dir = store._redo_dir(document_path)
        document_bytes = document_path.read_bytes()
        journal_before = journal.read(journal_path)
        history_before = _json_files(history_dir)
        redo_before = _json_files(redo_dir)

        if route == "sheet":
            source = workspace / "refused.sheet"
            source.write_text("edit " + json.dumps(call, separators=(",", ":")) + "\n", encoding="utf-8")
            with operation_discovery_scope(**discovery), pytest.raises(TimelineEditError, match=refusal):
                apply_file(tl, source)
        else:
            with operation_discovery_scope(**discovery), pytest.raises(TimelineEditError, match=refusal):
                run_command(tl, call)

        records = journal.read(journal_path)
        assert len(records) == len(journal_before) + 1
        assert records[:-1] == journal_before
        record = records[-1]
        assert record.kind == "edit" and record.outcome == "rolled-back"
        assert record.label == label and record.error == refusal
        assert record.undo_step is None
        assert document_path.read_bytes() == document_bytes
        assert _json_files(history_dir) == history_before
        assert _json_files(redo_dir) == redo_before
        assert journal.verify(journal_path) == []
        return record.to_json()

    sheet_record = exercise("sheet")
    sdk_record = exercise("sdk")
    variable = {"source", "actor", "at", "id", "seq", "prev", "hash"}
    comparable = [{key: value for key, value in record.items() if key not in variable}
                  for record in (sheet_record, sdk_record)]
    assert comparable[0] == comparable[1]


def _apply_file_contents(kind):
    first = {"op": "clip.set", "select": "c1.rocket", "args": {"x": 120}}
    second = {"op": "clip.set-duration", "select": "c2.card", "args": {"seconds": 2}}
    if kind == "sheet":
        from astrid.sdk.timeline_sheet import render_sheet

        return ".sheet", lambda tl: render_sheet(tl).replace("x=60", "x=120").replace("for 1s", "for 2s")
    if kind == "jsonl":
        return ".jsonl", lambda _tl: "\n".join(json.dumps(row) for row in (first, second)) + "\n"
    return ".py", lambda _tl: (
        'tl.clip("c1.rocket").set(x=120)\n'
        'tl.clip("c2.card").set_duration(2)\n'
        'tl.save()\n'
    )


@pytest.mark.parametrize("kind", ["sheet", "jsonl", "python"])
def test_apply_file_formats_share_one_journal_record_and_undo_step(tmp_path, monkeypatch, capsys, kind):
    from astrid.sdk.timeline import journal, store

    draft = _prepare_apply_file_draft(tmp_path, monkeypatch)
    before_document = draft.document()
    before_records = journal.read(journal.journal_path("p", "t"))
    before_steps = list(store._history_dir(Path(draft.path)).glob("*.json"))
    suffix, make_contents = _apply_file_contents(kind)
    input_file = tmp_path / f"edit{suffix}"
    input_file.write_text(make_contents(draft), encoding="utf-8")

    assert _run("apply", "t", "--project", "P", str(input_file)) == 0
    capsys.readouterr()
    applied = Checkout.draft("P", "t")
    assert applied.clip("c1.rocket").get("x") == 120
    assert applied.clip("c2.card").duration == pytest.approx(2)
    after_records = journal.read(journal.journal_path("p", "t"))
    after_steps = list(store._history_dir(Path(applied.path)).glob("*.json"))
    assert len(after_records) == len(before_records) + 1
    assert len(after_steps) == len(before_steps) + 1

    undone = applied.undo()
    assert len(undone) == 1
    assert applied.document() == before_document


def _fail_during_apply_file(kind, draft, tmp_path, monkeypatch, failure):
    suffix, make_contents = _apply_file_contents(kind)
    if kind == "jsonl":
        valid = {"op": "clip.set", "select": "c1.rocket", "args": {"x": 120}}
        invalid = {"op": "not.registered", "select": None, "args": {}}
        contents = "\n".join(json.dumps(row) for row in (valid, invalid)) + "\n"
        if failure == "crash":
            from astrid.sdk.timeline import commands
            original = commands._invoke

            def crash_after_write(*args, **kwargs):
                original(*args, **kwargs)
                raise RuntimeError("injected mid-file failure")

            monkeypatch.setattr(commands, "_invoke", crash_after_write)
    elif kind == "python":
        contents = (
            'tl.clip("c1.rocket").set(x=120)\n'
            'tl.save()\n'
            + ('raise RuntimeError("injected mid-file failure")\n' if failure == "crash" else
               'tl.clip("c2.card").set(zzbogus=2)\n')
        )
    else:
        from astrid.sdk import timeline_sheet

        original = timeline_sheet._update_layer
        calls = 0

        def fail_after_second_layer(*args, **kwargs):
            nonlocal calls
            calls += 1
            result = original(*args, **kwargs)
            if calls == 2:
                if failure == "crash":
                    raise RuntimeError("injected mid-file failure")
                # Exercise an actual typed SDK refusal after earlier sheet writes.
                args[0].clip("c2.card").set(zzbogus=2)
            return result

        monkeypatch.setattr(timeline_sheet, "_update_layer", fail_after_second_layer)
        contents = make_contents(draft).replace("x=75", "x=120")

    input_file = tmp_path / f"failed{suffix}"
    input_file.write_text(contents, encoding="utf-8")
    return input_file


@pytest.mark.parametrize("kind", ["sheet", "jsonl", "python"])
@pytest.mark.parametrize("failure", ["refusal", "crash"])
def test_apply_file_mid_file_failure_preserves_document_and_journal_bytes(tmp_path, monkeypatch, capsys, kind, failure):
    from astrid.sdk.timeline import journal, store

    draft = _prepare_apply_file_draft(tmp_path, monkeypatch)
    draft.clip("c1.rocket").set(x=75)
    draft.save(quiet=True)
    draft.clip("c1.rocket").set(y=45)
    draft.save(quiet=True)
    draft.undo()
    document_path = Path(draft.path)
    journal_file = journal.journal_path("p", "t")
    history_dir = store._history_dir(document_path)
    before_document_bytes = document_path.read_bytes()
    before_journal_bytes = journal_file.read_bytes()
    before_history = {path.name: path.read_bytes() for path in history_dir.glob("*.json")}
    redo_dir = store._redo_dir(document_path)
    before_redo = _json_files(redo_dir)
    assert before_history and before_redo
    input_file = _fail_during_apply_file(kind, draft, tmp_path, monkeypatch, failure)

    parsed = _parse("apply", "t", "--project", "P", str(input_file))
    if failure == "crash" and kind != "jsonl":
        with pytest.raises(RuntimeError, match="injected mid-file failure") as caught:
            parsed.handler(parsed)
        assert not isinstance(caught.value, TimelineEditError)
        assert not getattr(parsed, "_edit_refusal", None)
    else:
        assert parsed.handler(parsed) == 2
        refusal = parsed._edit_refusal
        assert refusal["type"] in {"CommandError", "TimelineEditError", "SheetError"}
        # JSONL deliberately wraps line failures in CommandError, including crashes.
        message = ("injected mid-file failure" if failure == "crash" else
                   "not.registered" if kind == "jsonl" else "zzbogus")
        assert message in refusal["message"]
        assert message in capsys.readouterr().err
    capsys.readouterr()
    assert document_path.read_bytes() == before_document_bytes
    assert journal_file.read_bytes() == before_journal_bytes
    assert {path.name: path.read_bytes() for path in history_dir.glob("*.json")} == before_history
    assert _json_files(redo_dir) == before_redo
    assert journal.verify(journal_file) == []
    assert Checkout.draft("P", "t").document() == draft.document()


def test_apply_file_does_not_add_a_timelines_run_verb():
    assert "run" not in {spec.name for spec in cli.COMMANDS}


# ---- show: the sheet, one address, the summary, the working copy ---------------------------------

@pytest.fixture
def drafted(tmp_path, monkeypatch):
    monkeypatch.setenv("BANODOCO_LOCAL_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setattr(tc, "resolve_ids", lambda project, timeline, client=None: ("p", "t", "rev-0"))
    monkeypatch.setattr(tc, "fetch_bundle", lambda project, timeline, revision_id=None, client=None: copy.deepcopy(bundle()))
    assert _run("checkout", "t", "--project", "P") == 0


def test_show_as_sheet_prints_the_working_copy_sheet_with_a_banner(tmp_path, monkeypatch, capsys):
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "fixtures" / "timeline_editing"
    tiny = json.loads((root / "tiny.json").read_text(encoding="utf-8"))
    monkeypatch.setenv("BANODOCO_LOCAL_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setattr(tc, "resolve_ids", lambda project, timeline, client=None: ("p", "t", "rev-0"))
    monkeypatch.setattr(tc, "fetch_bundle", lambda project, timeline, revision_id=None, client=None: copy.deepcopy(tiny))
    assert _run("checkout", "t", "--project", "P") == 0
    capsys.readouterr()
    assert _run_with_client("show", "t", "--project", "P", "--as", "sheet") == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].startswith("film P") and lines[1].startswith("WORKING COPY · 0 clips changed vs published")
    assert any(line.startswith("lines") for line in lines)


def test_show_published_flag_and_address_parse():
    parsed = _parse("show", "t", "c30.cover", "--project", "P", "--published", "--keys", "x,y", "--full")
    assert parsed.address == "c30.cover" and parsed.published and parsed.keys == "x,y" and parsed.full
    assert _parse("show", "t", "--project", "P", "--range", "c1", "--range", "c3").range == "c1,c3"
    assert _parse("show", "t", "--project", "P", "--as", "sheet").presentation == "sheet"


def test_show_summary_is_one_line_per_chapter(drafted, capsys):
    capsys.readouterr()
    assert _run_with_client("show", "t", "--project", "P", "--summary") == 0
    out = capsys.readouterr().out
    assert "chapters" in out and "full table:" in out


def test_show_at_takes_m_ss(drafted, capsys):
    capsys.readouterr()
    assert _run_with_client("show", "t", "--project", "P", "--at", "0:05") == 0
    assert "at 5.00 s" in capsys.readouterr().out


# ---- lines / words / duplicate / discard on the local checkout -----------------------------------

def test_lines_lists_the_narration(drafted, capsys):
    capsys.readouterr()
    assert _run("lines", "t", "--project", "P") == 0
    out = capsys.readouterr().out
    assert "2 lines" in out and "s1" in out and "s2" in out
    assert _run("lines", "t", "--project", "P", "--line", "s2") == 0
    assert "Live now" in capsys.readouterr().out


def test_words_for_one_word_prints_its_record(drafted, capsys):
    capsys.readouterr()
    assert _run("words", "t", "viral", "--project", "P") == 0
    out = capsys.readouterr().out
    assert "viral" in out and "anchored to it" in out


def test_discard_without_a_working_copy_is_not_an_error(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("BANODOCO_LOCAL_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setattr(tc, "resolve_ids", lambda project, timeline, client=None: ("p", "t", "rev-0"))
    assert _run("discard", "t", "--project", "P") == 0
    assert "no working copy" in capsys.readouterr().out


def test_duplicate_reports_the_new_timeline(monkeypatch, capsys):
    from astrid.sdk import timeline_duplicate as td

    monkeypatch.setattr(td, "duplicate_timeline", lambda project, source, slug=None, client=None: {
        "timeline_id": "dup-1", "slug": slug, "source": source, "shots": 2, "old_head": "a", "new_head": "b",
        "notes": ["narration: bound again"]})
    assert _run("duplicate", "t", "--project", "P", "--slug", "dup-1", "--name", "Dup") == 0
    out = capsys.readouterr().out
    assert "duplicated t → dup-1 (2 shots)" in out and "note: narration: bound again" in out


def test_a_positional_after_the_flags_fills_the_open_positional():
    assert _parse("show", "t", "--project", "P", "c41.mink").address == "c41.mink"
    assert _parse("words", "t", "--project", "P", "viral").word == "viral"
    assert _parse("find", "t", "--project", "P", "the conclusion").query == "the conclusion"
    assert _parse("show", "t", "--project", "P", "a", "b", "c").more_addresses == ["b", "c"]
    with pytest.raises(SystemExit):
        _parse("show", "t", "--project", "P", "--bogus")


def test_undo_takes_steps_as_a_position_or_an_option():
    assert cli.build_parser(None).parse_args(["undo", "t", "--project", "P", "3"]).steps == 3
    parsed = cli.build_parser(None).parse_args(["redo", "t", "--project", "P", "--steps", "2"])
    assert parsed.steps_option == 2
