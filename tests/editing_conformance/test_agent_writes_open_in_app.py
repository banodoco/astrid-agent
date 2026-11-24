"""Qualification row 5, phase 1: actual Agent writes through the actual App parser.

Select the default-off app_checkout lane with -m app_checkout. A required
qualification run sets ASTRID_REQUIRE_APP_PARSER=1 and ASTRID_APP_ROOT to the
proof owner's pinned App checkout (npm ci already done). Nothing in this file
installs dependencies or starts Runtime. Only transcripts refresh changes the
typed envelope; the other cases exercise the writers and re-validate the seed.
See app_open/README.md and the PR's write ledger.
"""
from __future__ import annotations

import copy
import json
import os
import shutil
import socket
import subprocess
from argparse import Namespace
from pathlib import Path

import pytest

from astrid.sdk.timeline.commands import execute
from astrid.sdk.timeline.commit import _digest, bundle_from_document
from astrid.sdk.timeline_checkout import Checkout
from tests.packs.timeline.test_cli_commit_move import command, sync_connection  # noqa: F401
from tests.packs.timeline.test_cli_commit_trim import trim  # noqa: F401

# Already registered as default-off in tests/lanes.py and pyproject.toml.
# A selected manual lane must fail, not skip, if its prerequisites are missing.
pytestmark = pytest.mark.app_checkout

PARSER = Path(__file__).parent / "app_open" / "open-bundle.mjs"
PARSER_SOURCE = "src/tools/video-editor/data/typed/timelineBundle.ts"


@pytest.fixture
def app_parser(monkeypatch, tmp_path):
    root = os.environ.get("ASTRID_APP_ROOT")
    node = shutil.which("node")
    missing = []
    if not root:
        missing.append("ASTRID_APP_ROOT is unset")
    if not node:
        missing.append("Node is unavailable")
    if missing:
        required = os.environ.get("ASTRID_REQUIRE_APP_PARSER") == "1"
        mode = "Required App parser qualification" if required else "Selected app_checkout lane"
        pytest.fail(f"{mode} unavailable: {'; '.join(missing)}")

    app = Path(root).resolve()
    assert (app / PARSER_SOURCE).is_file(), f"App parser not found in {app}"

    def no_network(*args, **kwargs):
        raise AssertionError("App parser qualification must not access the network")

    for name in ("create_connection", "getaddrinfo", "gethostbyname", "gethostbyname_ex"):
        monkeypatch.setattr(socket, name, no_network)
    for name in ("connect", "connect_ex", "sendto"):
        monkeypatch.setattr(socket.socket, name, no_network)

    def parse(value):
        # Only serialize the actual envelope extracted from the writer, never
        # a locally validated/repaired substitute for it.
        path = tmp_path / "app-bundle.json"
        path.write_text(json.dumps(value, allow_nan=False), encoding="utf-8")
        run = subprocess.run(
            [node, "--import", "tsx", str(PARSER.resolve()), str(path)],
            cwd=app,
            env={**os.environ, "ASTRID_APP_ROOT": str(app)},
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        assert run.returncode in (0, 1), (
            f"App parser invocation failed (exit {run.returncode}): "
            f"{run.stdout}\n{run.stderr}"
        )
        try:
            result = json.loads(run.stdout)
        except json.JSONDecodeError as exc:
            pytest.fail(f"App parser did not return JSON: {exc}: {run.stdout}\n{run.stderr}")
        assert type(result["ok"]) is bool
        assert result["ok"] == (run.returncode == 0)
        assert isinstance(result["name"], str) and isinstance(result["message"], str)
        if not result["ok"]:
            assert result["name"] == "TimelineBundleParseError", result
        return result

    return parse


def _seed(bundle):
    """Seed the App-authored envelope before a write; never repair an output."""
    bundle["parent"].setdefault("config", {}).setdefault(
        "bundle", {"schema_version": 1, "itemsBySchemaRef": {}}
    )
    return bundle


def _seed_document(document):
    parent = document["parent_revision"]
    parent["payload"].setdefault("config", {}).setdefault(
        "bundle", {"schema_version": 1, "itemsBySchemaRef": {}}
    )
    parent["content_digest"] = _digest(parent["payload"])
    return document


def _envelope(bundle):
    # Absence is a failure, not a reason to substitute an empty envelope.
    return bundle["parent"]["config"]["bundle"]


def _saved(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _accept_and_mutate(app_parser, label, bundle, *, transcript=False):
    value = _envelope(bundle)
    accepted = app_parser(value)
    damaged = copy.deepcopy(value)
    if transcript:
        rows = damaged["transcriptReferences"]["associations"]
        assert rows, f"{label}: writer did not record a transcript association"
        for row in rows:
            row.pop("assetId", None)
        field = "assetId"
    else:
        damaged.pop("schema_version", None)
        field = "schema_version"
    rejected = app_parser(damaged)
    assert not rejected["ok"], f"{label}: parser accepted missing {field}"
    assert field in rejected["message"], f"{label}: {rejected['message']}"
    # Run the mutation even on the historical red transcript case; the
    # unmodified writer's rejection remains the actual assertion failure.
    assert accepted["ok"], f"{label}: {accepted['message']}"


def test_transcripts_refresh(app_parser, tmp_path, monkeypatch):
    from astrid.packs.timeline import cli_working
    from astrid.sdk.timeline import words as W
    from tests.sdk import test_timeline_transcript_refresh as fixture

    whisper = W.default_source_order()[0]
    media = fixture.MEDIA_A
    transcript = fixture._doc(media, whisper, [(0, 0.5, "hi")])
    original = _seed(fixture._bundle({"s1": (media, [fixture._clip("c1", to=4.0)])}))
    path = tmp_path / "refresh.checkout.json"
    working = Checkout(original, path=path)
    monkeypatch.setattr(
        Checkout, "open", classmethod(lambda cls, *args, **kwargs: working)
    )
    monkeypatch.setattr(cli_working, "working_copy", lambda parsed, create: (working, True))
    monkeypatch.setattr(
        W, "load_timeline_transcripts",
        lambda client, project, bundle, **kwargs: {media: {whisper: transcript}},
    )
    args = Namespace(
        client=object(), project=fixture.PROJECT, ref="cut", shot=None, run_missing=False,
        placement=None, dry_run=False, draft=None, use_source=whisper, use_document=None,
    )
    assert cli_working._cmd_transcripts_refresh(args) == 0
    saved = _saved(path)
    rows = _envelope(saved)["transcriptReferences"]["associations"]
    assert any(row["documentId"] == transcript["document_id"] for row in rows)

    # Independent positive/negative parser control works even before the
    # writer fix, when there is no assetId to remove from the actual draft.
    # Do not use this control as the output of the writer.
    control = copy.deepcopy(_envelope(saved))
    control["transcriptReferences"]["associations"] = [
        {"assetId": "a", "documentId": transcript["document_id"]}
    ]
    result = app_parser(control)
    assert result["ok"], result["message"]
    del control["transcriptReferences"]["associations"][0]["assetId"]
    result = app_parser(control)
    assert not result["ok"] and "assetId" in result["message"], result
    _accept_and_mutate(app_parser, "transcripts refresh", saved, transcript=True)


def test_edit_add_media(app_parser, tmp_path, monkeypatch):
    from astrid.packs.timeline import cli, cli_sweep
    from tests.packs.timeline.test_cli_media import _tiny

    working = _tiny()
    _seed(working.bundle)
    path = tmp_path / "add-media.checkout.json"
    working.path = path
    monkeypatch.setattr(cli, "_edit_checkout", lambda parsed: working)
    monkeypatch.setattr(cli_sweep, "touched_cuts", lambda *args: set())
    monkeypatch.setattr(cli_sweep, "auto_sweep_after_edit", lambda *args, **kwargs: 0)
    parsed = cli.build_parser(None).parse_args([
        "edit", "--project", "p1", "main", "--add-media", "Q", "--track", "music",
        "--on", "1", "--id", "bed",
    ])
    assert parsed.handler(parsed) == 0
    assert working.clip("bed").data["to"] == 10
    _accept_and_mutate(app_parser, "edit --add-media", _saved(path))


@pytest.mark.parametrize("verb", ["checkout", "apply", "undo", "redo"])
def test_detached_cli_writes(app_parser, tmp_path, monkeypatch, verb):
    from astrid.packs.timeline import cli_sweep
    from astrid.sdk import timeline_checkout as tc
    from astrid.sdk.timeline_sheet import render_sheet
    from tests.packs.timeline.test_verbs_working_copy import _run
    from tests.packs.timeline.test_cli_media import _tiny

    original = _seed(_tiny().bundle)
    monkeypatch.setenv("ASTRID_RUNTIME_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setattr(tc, "resolve_ids", lambda *a, **kw: ("p", "t", "rev-0"))
    monkeypatch.setattr(tc, "fetch_bundle", lambda *a, **kw: copy.deepcopy(original))
    monkeypatch.setattr(cli_sweep, "touched_cuts", lambda *args: set())
    monkeypatch.setattr(cli_sweep, "auto_sweep_after_edit", lambda *args, **kwargs: 0)
    assert _run("checkout", "t", "--project", "P") == 0
    path = tc.draft_path("p", "t", "main")
    if verb == "apply":
        old = render_sheet(Checkout.draft("P", "t"))
        text = old.replace('"Live."', '"Now live."')
        assert text != old
        sheet = tmp_path / "edited.sheet"
        sheet.write_text(text, encoding="utf-8")
        assert _run("apply", "t", "--project", "P", str(sheet)) == 0
        assert Checkout.draft("P", "t").clip("c2.card").text == "Now live."
    elif verb in ("undo", "redo"):
        assert _run("edit", "t", "--project", "P", "--clip", "c1.rocket", "--set", "x=120") == 0
        assert _run("undo", "t", "--project", "P") == 0
        if verb == "redo":
            assert _run("redo", "t", "--project", "P") == 0
            assert Checkout.draft("P", "t").clip("c1.rocket").get("x") == 120
        else:
            assert Checkout.draft("P", "t").clip("c1.rocket").get("x") != 120
    _accept_and_mutate(app_parser, verb, _saved(path))


@pytest.mark.parametrize("op", ["clip.move", "timeline.inverse"])
def test_connected_cli_move_and_inverse(app_parser, command, op):
    run, generated = command
    _seed(generated.bundles["r0"])
    assert run("edit", "t", "--clip", "a", "--on", "5", "--json") == 0
    if op == "timeline.inverse":
        assert run("undo", "t", "--commit", "pub1", "--json") == 0
    revision = generated.head["revision_id"]
    assert revision == ("r1" if op == "clip.move" else "r2")
    actual = json.loads(generated.calls[-1][0])
    assert actual["commands"][0]["op"] == op
    _accept_and_mutate(app_parser, op, generated.bundles[revision])


def test_connected_cli_trim(app_parser, trim):
    run, generated, _ = trim
    _seed(generated.bundles["r0"])
    assert run("edit", "t", "--clip", "a", "--trim-out", "5", "--json") == 0
    assert json.loads(generated.calls[-1][0])["commands"][0]["op"] == "clip.trim"
    _accept_and_mutate(app_parser, "clip.trim", generated.bundles["r1"])


# Every case uses the same capture/execute API and the existing conformance
# fixture, with an App-authored envelope supplied before the operation. No
# alternate command semantics, test-only save, or parser substitute.
def _native_case(op, monkeypatch):
    from astrid.sdk.timeline import capture, native_occurrence_media_commands as occurrence_media
    from astrid.sdk.timeline import native_parent_media_commands as parent_media
    from tests.editing_conformance import (
        test_doc_patch_v2 as patch,
        test_native_word_anchor_vectors as anchor,
        test_prepared_video_birth as video,
        test_recipe_clip_delete as clip_delete,
        test_recipe_clip_scalar_fields as clip_scalar,
        test_recipe_clip_set_element as element,
        test_recipe_m1_commands as m1,
        test_recipe_m2_clip_add as clip_add,
        test_recipe_m2_clip_split as clip_split,
        test_recipe_m2_parent_media as parent,
        test_recipe_m2_placement as placement,
        test_recipe_m2_placement_create_unplace as place,
        test_recipe_m2_shot_pool as pool,
        test_recipe_occurrence_create as occurrence_create,
        test_recipe_occurrence_delete as occurrence_delete,
        test_recipe_occurrence_duplicate as occurrence_duplicate,
        test_recipe_occurrence_fork as fork,
        test_recipe_occurrence_media as media,
        test_recipe_parent_media as swap,
        test_recipe_scalar_commands as scalar,
        test_recipe_shot_set as shot,
        test_recipe_text_binding_native as text_binding,
        test_recipe_timeline_source_copy as source_copy,
    )

    if op == "doc.patch":
        from astrid.sdk.timeline._command_state import ABSENT

        base = _seed(patch.bundle())
        call = patch.patch(patch.change(["unknown", "app-open-probe"], ABSENT, True))
    elif op == "occurrence.create":
        base = _seed(occurrence_create.empty_bundle())
        call = occurrence_create.birth(base, on=1)
    elif op == "clip.anchor":
        base, pin, content = anchor.fixture()
        _seed(base)
        choice = {
            "source": "occurrence:occ-a/clip:voice",
            "document": pin,
            "word_ids": [content["segments"][0]["words"][0]["id"]],
        }
        call = anchor.capture_anchor(base, "parent/clip:clip-a", "on", "again", choice)
    elif op in ("occurrence.add-media", "occurrence.swap-media"):
        document, old, new, verified = media.inputs()
        base = bundle_from_document(_seed_document(document))
        if op == "occurrence.add-media":
            row = {"id": "reserved-child", "clipType": "image", "asset": new["asset"],
                   "track": "picture", "at": .125, "hold": 1.25,
                   "params": None, "opaque": [None, False, {}]}
            call = occurrence_media.capture_occurrence_add_media(
                base, media.OWNER, row, new, verified_media=verified
            )
        else:
            call = occurrence_media.capture_occurrence_swap_media(
                base, media.TARGET, old, new, verified_media=verified
            )
        return execute(base, [call], verified_media=verified)
    elif op == "clip.add-media":
        document, _, new, verified = parent.parent_inputs()
        base = bundle_from_document(_seed_document(document))
        call = parent.capture(base, parent.args(new, "image"), verified)
        return execute(base, parent.calls(call), verified_media=verified)
    elif op == "clip.swap-media":
        base, old, new, verified = swap.parent_input()
        _seed(base)
        call = occurrence_media.capture_parent_swap_media(
            base, swap.TARGET, old, new, verified_media=verified
        )
        return execute(base, [call], verified_media=verified)
    elif op == "clip.duplicate-media":
        base, old, new, verified = video.case()
        _seed(base)
        call = video.capture(base, old, new, verified)
        return execute(base, [call], verified_media=verified)
    elif op in ("placement.create", "placement.unplace"):
        _, base, _, verified = place.create_case()
        _seed(base)
        new = verified[-1]["selection"]
        call = place.capture_placement_create(
            base, place.OWNER, "placed", new, place.ON, place.HOLD,
            ownership="occurrence", verified_media=verified,
        )
        if op == "placement.unplace":
            created = execute(base, [call], verified_media=verified)
            from astrid.sdk.timeline.commit import document_from_bundle

            base = bundle_from_document(document_from_bundle(created.bundle))
            call = place.capture_placement_unplace(
                base, place.OWNER, "placed", ownership="occurrence"
            )
            return execute(base, [call])
        return execute(base, [call], verified_media=verified)
    elif op == "placement.move":
        base = bundle_from_document(_seed_document(placement.placement_document()))
        call = placement.capture(base)
    elif op == "shot.pool":
        _, base, action, pin, verified = pool.build("add-first")
        _seed(base)
        return pool.step(base, action, pin, verified)[1]
    elif op == "occurrence.delete":
        base = bundle_from_document(_seed_document(occurrence_delete.delete_document()))
        call = occurrence_delete.capture_occurrence_delete(base, "occurrence:occ-1")
    elif op == "occurrence.duplicate":
        base = bundle_from_document(_seed_document(
            occurrence_duplicate.duplicate_document(2, shared_shot=True)
        ))
        call = occurrence_duplicate.duplicate(base, "occ-0")
    elif op in ("occurrence.fork", "occurrence.place-shared"):
        base = bundle_from_document(_seed_document(fork.fork_document()))
        if op == "occurrence.fork":
            call = fork.fork(base)
        else:
            from astrid.sdk.timeline.native_occurrence_shared import capture_occurrence_place_shared

            call = capture_occurrence_place_shared(base, "occurrence:occ-0", "f2-occurrence-copy", on=12)
    elif op == "clip.delete":
        base = bundle_from_document(_seed_document(clip_delete.delete_document()))
        call = clip_delete.capture_clip_delete(base, "parent/clip:clip-b")
    elif op == "track.create":
        base = _seed(occurrence_create.empty_bundle())
        call = capture.capture_track_create(
            base, "parent", {"id": "extra", "kind": "visual", "role": "output"}
        )
    elif op == "clip.set":
        base = _seed(clip_scalar.app_bundle())
        return execute(base, clip_scalar.app_calls(base))
    elif op == "clip.set-element":
        base = _seed(element.seeded())
        call = element.capture_clip_set_element(base, element.REF, element=element.ADMITTED)
        return execute(base, [call], **element.INPUTS)
    elif op == "clip.add":
        base = _seed(clip_add.with_track())
        call = clip_add.capture_clip_add(base, "parent", clip_add.literal_row())
    elif op == "clip.split":
        base = bundle_from_document(_seed_document(clip_split.source_document()))
        return clip_split.split(base)
    elif op == "track.set":
        base = _seed(m1.bundle())
        call = scalar.capture_track_set(base, "parent/track:picture", values={"label": "Picture"})
    elif op == "clip.set-params":
        base = _seed(m1.bundle())
        call = scalar.capture_clip_set_params(base, scalar.REF, values={"x": 2})
    elif op == "shot.set":
        base = _seed(shot.bundle())
        call = shot.capture_shot_set(base, shot.REF, values={"name": "A"})
    elif op == "text-binding.set":
        base = text_binding.bundle(shared=False)
        text_binding.payload(base)["text_bindings"] = []
        base = _seed(text_binding._attach_authenticated_source(base))
        return text_binding.apply(base)
    elif op == "timeline.duplicate":
        # The source-copy fixture binds the source digest and resolver at
        # construction. Seed its source image before those pins are built,
        # not the pristine target or the result.
        document_fixture = source_copy.document_fixture
        monkeypatch.setattr(
            source_copy, "document_fixture",
            lambda *a, **kw: _seed_document(document_fixture(*a, **kw)),
        )
        base, _, call, resolver, _ = source_copy.inputs()
        return execute(base, [call], resolve_inputs=resolver)
    else:
        raise AssertionError(f"unlisted native case: {op}")

    return execute(base, [call])


NATIVE_CASES = (
    "occurrence.create", "clip.anchor", "occurrence.add-media", "clip.add-media",
    "doc.patch", "occurrence.swap-media", "clip.swap-media", "clip.duplicate-media",
    "placement.create", "placement.unplace", "placement.move", "shot.pool",
    "occurrence.delete", "occurrence.duplicate", "occurrence.fork", "occurrence.place-shared",
    "clip.delete", "track.create", "clip.set", "clip.set-element", "clip.add", "clip.split",
    "track.set", "clip.set-params", "shot.set", "text-binding.set", "timeline.duplicate",
)


@pytest.mark.parametrize("op", NATIVE_CASES)
def test_native_write(app_parser, monkeypatch, op):
    plan = _native_case(op, monkeypatch)
    assert op in {call["op"] for call in plan.commands}
    _accept_and_mutate(app_parser, op, plan.bundle)
