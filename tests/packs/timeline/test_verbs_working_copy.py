"""The non-edit working-copy verbs: lint, lines, words, discard, duplicate (restored from f840ec1c)."""
from __future__ import annotations

import copy
import json
import re

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
    monkeypatch.setenv("ASTRID_RUNTIME_DATA_ROOT", str(tmp_path / "data"))
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


@pytest.fixture
def draft_with_edit(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTRID_RUNTIME_DATA_ROOT", str(tmp_path / "data"))
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



def test_duplicate_is_all_or_nothing(monkeypatch):
    """A refused duplicate leaves no empty timeline: an invalid head is refused before creating, a failure
    after creating archives what was created."""
    from types import SimpleNamespace

    from astrid.sdk import timeline_duplicate as td

    calls = []

    class Timelines:
        def create_empty(self, **kw):
            calls.append("create")
            return SimpleNamespace(ok=True, data={"timeline_id": "dup-1"})

        def archive(self, project, ref):
            calls.append(f"archive {ref}")
            return SimpleNamespace(ok=True)

    client = SimpleNamespace(timelines=Timelines())
    bad = Checkout(bundle())
    bad.clip("a-rocket").data["params"]["scale"] = '"oops"'  # text in a number: check blocks it
    monkeypatch.setattr(td.Checkout, "open", classmethod(lambda cls, p, t, revision_id=None, client=None: Checkout(copy.deepcopy(bad.bundle))))
    with pytest.raises(TimelineEditError, match="nothing was created"):
        td._duplicate(client, "p", "t", slug=None, name=None, revision_id=None)
    assert calls == []
    good = Checkout(bundle())
    good.check = lambda: SimpleNamespace(valid=True, blocking=[], problems=[])
    monkeypatch.setattr(td.Checkout, "open", classmethod(lambda cls, p, t, revision_id=None, client=None: good))
    monkeypatch.setattr(td, "copy_content", lambda *a, **k: (_ for _ in ()).throw(TimelineEditError("the copy failed")))
    with pytest.raises(TimelineEditError, match="archived again"):
        td._duplicate(client, "p", "t", slug=None, name=None, revision_id=None)
    assert calls == ["create", "archive dup-1"]



# ---- apply: a sheet exported before a later edit never reverts it --------------------------------

def test_a_sheet_exported_before_an_edit_never_reverts_it(tmp_path, monkeypatch, capsys):
    """export -> edit via the verb -> apply the OLD sheet with an unrelated change: both survive; a clash refuses."""
    from pathlib import Path

    from astrid.sdk.timeline_sheet import render_sheet

    root = Path(__file__).resolve().parents[2] / "fixtures" / "timeline_editing"
    tiny = json.loads((root / "tiny.json").read_text(encoding="utf-8"))
    monkeypatch.setenv("ASTRID_RUNTIME_DATA_ROOT", str(tmp_path / "data"))
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


# ---- show: the sheet, one address, the summary, the working copy ---------------------------------

@pytest.fixture
def drafted(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTRID_RUNTIME_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setattr(tc, "resolve_ids", lambda project, timeline, client=None: ("p", "t", "rev-0"))
    monkeypatch.setattr(tc, "fetch_bundle", lambda project, timeline, revision_id=None, client=None: copy.deepcopy(bundle()))
    assert _run("checkout", "t", "--project", "P") == 0


def test_show_as_sheet_prints_the_working_copy_sheet_with_a_banner(tmp_path, monkeypatch, capsys):
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "fixtures" / "timeline_editing"
    tiny = json.loads((root / "tiny.json").read_text(encoding="utf-8"))
    monkeypatch.setenv("ASTRID_RUNTIME_DATA_ROOT", str(tmp_path / "data"))
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
    monkeypatch.setenv("ASTRID_RUNTIME_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setattr(tc, "resolve_ids", lambda project, timeline, client=None: ("p", "t", "rev-0"))
    assert _run("discard", "t", "--project", "P") == 0
    assert "no working copy" in capsys.readouterr().out


def test_duplicate_reports_the_new_timeline(monkeypatch, capsys):
    from astrid.sdk import timeline_duplicate as td

    seen = {}

    def fake_duplicate(project, source, slug=None, name=None, revision_id=None, client=None):
        seen.update(slug=slug, name=name, revision_id=revision_id)
        return {"timeline_id": "dup-1", "slug": slug, "source": source, "shots": 2, "old_head": "a", "new_head": "b",
                "notes": ["narration: bound again"]}

    monkeypatch.setattr(td, "duplicate_timeline", fake_duplicate)
    assert _run("duplicate", "t", "--project", "P", "--slug", "dup-1", "--name", "Dup", "--revision", "rev-3") == 0
    out = capsys.readouterr().out
    assert seen == {"slug": "dup-1", "name": "Dup", "revision_id": "rev-3"}
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


# ---- a reused working copy behind the head, and refusals that print one plain line ----------

EDITOR_HEAD = "astrid.editor.timeline.5fb2249e" + "0" * 24


def _head_at(monkeypatch, tmp_path, head: dict) -> None:
    from astrid.sdk.timeline import store

    monkeypatch.setenv("ASTRID_RUNTIME_DATA_ROOT", str(tmp_path / "data"))
    for module in (tc, store):
        monkeypatch.setattr(module, "resolve_ids", lambda project, timeline, client=None: ("p", "t", head["id"]))
        monkeypatch.setattr(module, "fetch_bundle", lambda project, timeline, revision_id=None, client=None: copy.deepcopy(bundle()))


def test_checkout_reusing_a_copy_behind_the_head_says_so(tmp_path, monkeypatch, capsys):
    head = {"id": "rev-0"}
    _head_at(monkeypatch, tmp_path, head)
    assert _run("checkout", "t", "--project", "P") == 0
    capsys.readouterr()
    assert _run("edit", "t", "--project", "P", "--clip", "R", "--nudge-frames", "3") == 0
    capsys.readouterr()

    head["id"] = EDITOR_HEAD  # the App editor publishes while this copy is still on rev-0
    assert _run("checkout", "t", "--project", "P") == 0
    out = capsys.readouterr().out.splitlines()

    assert out[0].startswith('working copy "main" of t · from published rev-0 · ')
    assert out[1] == ('STALE: working copy "main" was made from rev-0, but the head is now 5fb2249e '
                      "(written by the App editor).")
    assert out[2] == "  checkout reused the copy and did not pick up the head; it holds 1 clip changed vs rev-0."
    assert "  to start from the head: timelines discard t --project P, then timelines checkout t --project P --fresh" in out
    assert "STALE" not in "\n".join(out[3:])


def test_checkout_of_a_current_copy_says_nothing_stale(tmp_path, monkeypatch, capsys):
    head = {"id": "rev-0"}
    _head_at(monkeypatch, tmp_path, head)
    assert _run("checkout", "t", "--project", "P") == 0
    capsys.readouterr()
    assert _run("checkout", "t", "--project", "P") == 0
    assert "STALE" not in capsys.readouterr().out


def test_fresh_checkout_after_discard_is_not_stale(tmp_path, monkeypatch, capsys):
    head = {"id": "rev-0"}
    _head_at(monkeypatch, tmp_path, head)
    assert _run("checkout", "t", "--project", "P") == 0
    assert _run("edit", "t", "--project", "P", "--clip", "R", "--nudge-frames", "3") == 0
    assert _run("discard", "t", "--project", "P") == 0
    head["id"] = EDITOR_HEAD
    capsys.readouterr()
    assert _run("checkout", "t", "--project", "P", "--fresh") == 0
    assert "STALE" not in capsys.readouterr().out


@pytest.mark.parametrize("argv", [
    ["checkout", "t", "--project", "P"],
    ["status", "t", "--project", "P"],
    ["publish", "t", "--project", "P", "-m", "x"],
])
def test_a_refusal_from_the_head_lookup_is_one_plain_line(tmp_path, monkeypatch, capsys, argv):
    from astrid.sdk.timeline import store

    monkeypatch.setenv("ASTRID_RUNTIME_DATA_ROOT", str(tmp_path / "data"))

    def refuse(project, timeline, client=None):
        raise TimelineEditError("could not open timeline 't' in project 'P': no such timeline")

    monkeypatch.setattr(store, "resolve_ids", refuse)
    monkeypatch.setattr(tc, "resolve_ids", refuse)
    assert _run(*argv) == 2  # the guarded exit code, not a bug report
    err = capsys.readouterr().err
    assert err.strip() == "could not open timeline 't' in project 'P': no such timeline"
    assert "unstructured" not in err


# ---- the conflict message names the editor and says how to start over ----------------------

def test_short_revision_strips_every_known_prefix():
    from astrid.sdk.timeline.revisions import short_revision

    assert short_revision(EDITOR_HEAD) == "5fb2249e"
    assert short_revision("authoring-parent-revision-38cecaa5" + "f" * 24) == "38cecaa5"
    assert short_revision("restore-0a1b2c3d4e5f") == "0a1b2c3d"
    assert short_revision("rev-0") == "rev-0"
    assert short_revision(None) == ""


@pytest.mark.parametrize("head, who", [
    (EDITOR_HEAD, "the editor published 5fb2249e after your checkout (rev-0)"),
    ("authoring-parent-revision-38cecaa5" + "f" * 24, "someone published 38cecaa5 after your checkout (rev-0)"),
])
def test_publish_conflict_names_the_writer_and_offers_discard_first(monkeypatch, head, who):
    from astrid.sdk.timeline import store

    class _Timelines:
        def open_composition(self, *_args, **_kwargs):
            return type("Shown", (), {"ok": True, "error": None,
                                      "data": {"summary": {"head_revision_id": head}}})()

    class _Client:
        timelines = _Timelines()

    monkeypatch.setattr(store, "fetch_bundle", lambda *_args, **_kwargs: copy.deepcopy(bundle()))
    monkeypatch.setattr(store, "three_way", lambda _base, _head, candidate, force: (candidate, ["p/a-rocket (clip) moved 1.8s"]))

    with pytest.raises(TimelineEditError) as refused:
        store.publish_bundle(copy.deepcopy(bundle()), "key", client=_Client())
    message = str(refused.value)

    assert message.splitlines()[0].startswith(f"not published: {who}, and you both changed:")
    assert "next: timelines discard t --project p, then timelines checkout t --project p --fresh, and re-apply your edit." in message
    assert "  publish --force overwrites the newer published version." in message
    assert "to overwrite theirs" not in message
    assert "astrid.e\n" not in message and "astrid.e " not in message
