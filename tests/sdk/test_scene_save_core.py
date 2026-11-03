"""Save keeps core validity under real renderer import and process-launch fences."""
from __future__ import annotations

import builtins
import copy
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from astrid.sdk import timeline_intent as intent
from astrid.sdk.timeline_checkout import Checkout, TimelineEditError
from tests.editing_conformance.test_commit_runtime_source_copy import saved_source  # noqa: F401
from tests.sdk import test_timeline_transaction as transactions

draft = transactions.draft
tiny = transactions.tiny


@pytest.fixture
def no_renderer(monkeypatch):
    original_import = builtins.__import__

    def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
        names = [name, *(f"{name}.{item}" for item in fromlist or ())]
        if any(item.startswith(("astrid.packs.rendering", "astrid.core.rendering",
                                "astrid.sdk.authoring_bundle", "astrid.sdk.authoring_render_preview",
                                "composition", "remotion"))
               or item.endswith(".onscreen") or ".scene" in item for item in names):
            raise AssertionError("save imported renderer/scene: " + name)
        return original_import(name, globals, locals, fromlist, level)

    def forbidden(*args, **kwargs):
        raise AssertionError("save launched a process or evaluated diagnostics")

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    for method in ("check", "_on_screen", "lint", "_sprite_problems", "_text_overflow"):
        monkeypatch.setattr(Checkout, method, forbidden)


def client_for(checkout, writes):
    def publish(project, timeline, publication, *, idempotency_key):
        writes.append(copy.deepcopy(publication))
        return {"data": {"new_head": "accepted"}}

    return SimpleNamespace(
        timelines=SimpleNamespace(open_composition=lambda *a: SimpleNamespace(
            ok=True, data={"summary": {"head_revision_id": checkout.base_revision}}),
            open_bundle=lambda *a, **kw: SimpleNamespace(ok=True, data={"bundle": checkout.document()})),
        _remote=SimpleNamespace(_transport=SimpleNamespace(publish_parent_composition=publish)),
    )


def stub_checkout_commit(monkeypatch, checkout, writes):
    """Keep this compiler/renderer fence unit test at the commit facade seam.

    test_timeline_normal_publish exercises the actual Runtime provider under
    the same renderer fence with authenticated media and a real base pair.
    """
    from astrid.core.timeline.authoring_bundle import compile_authoring_candidate

    def commit(document, message, **kwargs):
        writes.append(compile_authoring_candidate(document).publication)
        return {"old_head": checkout.base_revision, "new_head": "accepted"}

    monkeypatch.setattr(checkout, "_commit_document", commit)


@pytest.mark.parametrize("operation", ["core", "publish", "transaction", "dry-run"])
def test_real_save_paths_need_neither_scene_imports_nor_node(draft, no_renderer, operation, monkeypatch):
    checkout = draft
    before = checkout.document()
    writes = []
    if operation in {"transaction", "dry-run"}:
        with checkout.edit("move", dry_run=operation == "dry-run", quiet=True) as tx:
            checkout.clip("c1.rocket").set(x=90)
        assert tx.report.valid and tx.report.lint == []
        if operation == "dry-run":
            assert checkout.document() == before
        else:
            assert Checkout.load(checkout.path).clip("c1.rocket").get("x") == 90
            checkout.undo()
            assert Checkout.load(checkout.path).document() == before
    else:
        checkout.clip("c1.rocket").set(x=90)
        report = checkout._check_core()
        assert report.valid and not report.blocking and report.lint == []
        if operation == "publish":
            # An in-memory candidate avoids a follow-up head read; the compiler is real.
            checkout.path = None
            stub_checkout_commit(monkeypatch, checkout, writes)
            receipt = checkout.publish(client=client_for(checkout, writes))
            assert receipt["new_head"] == "accepted" and len(writes) == 1


@pytest.mark.parametrize("damage", ["schema", "reference", "type", "length", "outside", "shot-length", "orphan"])
def test_publish_still_refuses_core_invalidity_before_writing(no_renderer, damage):
    checkout = Checkout(tiny())
    if damage == "schema":
        checkout.bundle["schema_version"] = 999
    elif damage == "reference":
        checkout.clip("c1.rocket").data["asset"] = "missing-asset"
    elif damage == "type":
        checkout.clip("c1.rocket").data["params"]["scale"] = "large"
    elif damage == "length":
        card = checkout.clip("c2.card")
        intent.set_for(card.data, None)
        intent.set_cut(card.data, None)
        card.data["hold"] = 0
    elif damage == "shot-length":
        card = checkout.clip("c2.card")
        intent.set_for(card.data, 10)
        card.data["hold"] = 10
    else:
        intent.set_formula(checkout.clip("c1.rocket").data, "params.allAt",
                           {"moment": '"now"' if damage == "outside" else '"gone"', "as": "clip_frame"})
    writes = []
    report = checkout._check_core()
    assert not report.valid and report.problems
    with pytest.raises(TimelineEditError, match="not publishing"):
        checkout.publish(client=client_for(checkout, writes))
    assert writes == []


def test_private_check_override_still_fences_publication(monkeypatch):
    checkout = Checkout(tiny())
    writes = []
    monkeypatch.setattr(checkout, "_check_core", lambda: SimpleNamespace(valid=False, problems=["guard refused"]))
    with pytest.raises(TimelineEditError, match="not publishing: guard refused"):
        checkout.publish(client=client_for(checkout, writes))
    assert writes == []


def test_offcanvas_stays_explicitly_invalid_but_saves_and_publishes_exactly(tmp_path, request, monkeypatch):
    # PLAN §§2.5/4.2: visual checks advise; only core validity blocks publication.
    draft = Checkout(tiny())
    draft.clip("c1.rocket").data["params"]["x"] = 10000
    explicit = draft.check()
    assert explicit.validation["valid"] and not explicit.valid
    assert len(explicit.blocking) == 1 and explicit.blocking[0].startswith("OFFCANVAS c1.rocket:")
    request.getfixturevalue("no_renderer")
    core = draft._check_core()
    assert core.valid and core.blocking == [] and core.lint == []
    draft.save(tmp_path / "draft.json", quiet=True)
    assert Checkout.load(draft.path).clip("c1.rocket").data["params"]["x"] == 10000
    writes = []
    stub_checkout_commit(monkeypatch, draft, writes)
    receipt = draft.publish(client=client_for(draft, writes))
    assert receipt["new_head"] == "accepted" and len(writes) == 1
    rocket = next(clip for row in writes[0]["internal_timeline_revisions"]
                  for clip in row["payload"]["clips"] if clip["id"] == "a-rocket")
    assert rocket["params"]["x"] == 10000
    assert draft.clip("c1.rocket").data["params"]["x"] == 10000


def test_duplicate_preflights_and_publication_are_core_only(saved_source, no_renderer):  # noqa: F811
    from astrid.sdk import timeline_duplicate as duplicate
    from tests.sdk.test_timeline_restructure import flat_film

    source = flat_film()
    next(clip for clip in source["shots"]["film"]["internal_timeline"]["clips"]
         if clip["id"] == "inside")["params"]["x"] = 10000
    client, project, document, writes = saved_source(source)
    before = document("t")
    client.timelines.archive = lambda *a: pytest.fail("core-valid duplicate was archived")
    result = duplicate.duplicate_timeline(project, "t", slug="copy", client=client)
    copied = document("copy")
    assert result["timeline_id"] == "copy" and result["new_head"] == copied["parent_revision"]["revision_id"]
    assert len(writes) == 1 and writes[0]["commands"][0]["op"] == "timeline.duplicate"
    sprite = next(clip for row in copied["internal_timeline_revisions"]
                  for clip in row["payload"]["clips"] if clip["id"] == "inside")
    assert sprite["params"]["x"] == 10000
    assert document("t") == before


def test_implicit_cli_edit_report_and_save_are_core_only(tmp_path, request):
    from astrid.packs.timeline import cli

    path = Checkout(tiny()).save(tmp_path / "draft.json", quiet=True)
    parsed = cli.build_parser(None).parse_args(["edit", "--file", str(path),
                                               "--clip", "c1.rocket", "--set", "x=90"])
    request.getfixturevalue("no_renderer")
    assert parsed.handler(parsed) == 0
    assert Checkout.load(path).clip("c1.rocket").get("x") == 90


def test_fresh_check_import_and_core_validation_do_not_import_renderer():
    script = '''
import sys
class Blocked:
    def find_spec(self, fullname, *args):
        if (fullname.startswith(('astrid.packs.rendering', 'astrid.core.rendering',
                                 'astrid.sdk.authoring_bundle', 'astrid.sdk.authoring_render_preview',
                                 'composition', 'remotion'))
                or fullname.endswith('.onscreen') or '.scene' in fullname):
            raise AssertionError('renderer dependency: ' + fullname)
sys.meta_path.insert(0, Blocked())
from astrid.sdk.timeline_checkout import Checkout
from tests.sdk.test_timeline_transaction import tiny
assert Checkout(tiny())._check_core().valid
'''
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    assert result.returncode == 0, result.stderr
