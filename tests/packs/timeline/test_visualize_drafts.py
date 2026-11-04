"""Offline checks: visualize defaults to a pinned publication and accepts a named draft explicitly.

Runtime, checkout and render are monkeypatched; nothing here touches the workspace.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.packs.timeline import cli, cli_visualize
from astrid.sdk.contracts import DomainResult


class FakeCheckout:
    """Stands in for Checkout.load(path): one working copy with two changed cuts."""

    base_revision = "rev-published-1"

    def __init__(self, changed_cuts):
        self._changed = list(changed_cuts)

    @classmethod
    def load(cls, path):
        return cls([12, 13, 22])

    def edits(self):
        return {"changes": [{"clip_id": "c1"}, {"clip_id": "c2"}, {"clip_id": "c3"}]}

    def check(self):
        return SimpleNamespace(changed_cuts=self._changed)

    def document(self):
        return {"kind": "candidate", "base_parent": {"revision_id": self.base_revision}}

    def changed_cut_ids(self):
        return ["c12", "c13", "c22"]

    def changes(self):
        return ["✎ c12.a  x 1 → 2", "✎ c13.b  x 1 → 2", "✎ c22.c  x 1 → 2"]


def _render_result(run="run-1"):
    return SimpleNamespace(ok=True, capability_id="rendering.render", kernel_run_id=run, run_id=run,
                           kernel_task_id=None, kernel_attempt_id=None, outputs={},
                           raw_result={"state": "completed"}, error=None)


class FakeTimelines:
    def __init__(self):
        self.visualize_calls = []

    def visualize(self, project, ref, *, mode="auto", options=None, revision_id=None, out=None):
        self.visualize_calls.append({"project": project, "ref": ref, "options": dict(options or {}),
                                     "revision_id": revision_id})
        return DomainResult.success({"pack_root": "/tmp/unused", "formats": []})

    def open_bundle(self, project, ref, revision_id=None):
        return SimpleNamespace(ok=True, data={"bundle": {"base_parent": {"revision_id": "rev-head"}}})


class FakeClient:
    def __init__(self):
        self.timelines = FakeTimelines()


@pytest.fixture()
def draft(tmp_path, monkeypatch):
    """A working copy exists for the timeline; Checkout reads the fake."""
    from astrid.sdk import timeline_checkout

    path = tmp_path / "main.json"
    path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(timeline_checkout, "find_draft", lambda project, timeline, name="main", client=None: path)
    monkeypatch.setattr(timeline_checkout, "Checkout", FakeCheckout)
    return path


@pytest.fixture()
def no_draft(monkeypatch):
    from astrid.sdk import timeline_checkout

    monkeypatch.setattr(timeline_checkout, "find_draft", lambda project, timeline, name="main", client=None: None)
    monkeypatch.setattr(timeline_checkout, "Checkout", FakeCheckout)


def _visualize(argv, client):
    parser = cli.build_parser(client)
    parsed = parser.parse_args(["visualize", "tl", "--project", "demo", *argv])
    parsed.client = client
    return cli._cmd_visualize(parsed)


def _render(argv, client):
    parser = cli.build_parser(client)
    parsed = parser.parse_args(["render", "tl", "--project", "demo", *argv])
    parsed.client = client
    return cli._cmd_render(parsed)


def _summary(outputs, *, requested_revision='requested-revision'):
    return cli_visualize._visualization_summary(
        outputs,
        parsed=SimpleNamespace(project='demo', beats=None),
        inputs={'view': 'filmstrip', 'timeline_slug': 'main', 'revision_id': requested_revision},
    )


def test_visualize_summary_uses_native_actual_revision_and_separate_head():
    summary = _summary({'inspection': {
        'revision_id': 'native-actual', 'head_revision_id': 'native-head',
    }})

    lines = summary.splitlines()
    assert '  committed revision native-actual' in lines
    assert '  known head native-head' in lines
    assert not any('requested-revision' in line for line in lines if line.startswith('  committed revision'))


def test_visualize_summary_uses_rendered_snapshot_label_for_candidate_and_unavailable():
    candidate = _summary({'snapshot_label': {
        'status': 'candidate',
        'label': 'unsaved candidate based on committed revision accepted-base',
    }})
    unavailable = _summary({'snapshot_label': {'status': 'unavailable', 'label': 'revision unavailable'}})

    candidate_lines = candidate.splitlines()
    assert '  unsaved candidate based on committed revision accepted-base' in candidate_lines
    assert not any('requested-revision' in line for line in candidate_lines if line.startswith('  unsaved candidate'))
    assert '  revision unavailable' in unavailable.splitlines()


def test_visualize_reads_an_explicit_working_copy_with_banner_and_changed_cuts(draft, capsys, monkeypatch):
    import astrid.core.timeline.authoring_bundle as ab

    monkeypatch.setattr(ab, "preview_authoring_candidate", lambda doc: {"kind": "authoring-candidate-preview", "candidate": doc})
    client = FakeClient()
    code = _visualize(["--draft", "main"], client)
    out = capsys.readouterr().out
    assert code == 0
    assert out.splitlines()[0] == (
        "WORKING COPY main · 3 clips changed · base revision rev-published-1"
    )
    assert out.splitlines()[1].startswith("showing the 3 cuts you changed (12, 13, 22) · --every-cut for all cuts")
    assert "[✎ c12, c13, c22]" in out.splitlines()[1]
    options = client.timelines.visualize_calls[0]["options"]
    assert options["authoring_preview"]["kind"] == "authoring-candidate-preview"  # frames come from the working copy
    assert options["authoring_preview"]["label"] == "working copy main · base revision rev-published-1"
    assert client.timelines.visualize_calls[0]["revision_id"] == "rev-published-1"
    assert options["cuts"] == "12,13,22" and options["view"] == "contact"


def test_visualize_every_cut_drops_the_changed_cut_selection(draft, capsys, monkeypatch):
    import astrid.core.timeline.authoring_bundle as ab

    monkeypatch.setattr(ab, "preview_authoring_candidate", lambda doc: {"kind": "authoring-candidate-preview"})
    client = FakeClient()
    _visualize(["--draft", "main", "--every-cut"], client)
    out = capsys.readouterr().out
    assert "showing every cut of the working copy" in out
    assert "you changed" not in out


def test_published_bypasses_the_draft(draft, capsys):
    client = FakeClient()
    code = _visualize(["--published", "--json"], client)
    envelope = json.loads(capsys.readouterr().out)
    assert code == 0
    assert envelope["ok"] is True
    assert envelope["data"]["outputs"]["working_copy"] is None
    assert len(client.timelines.visualize_calls) == 1
    assert "cuts" not in client.timelines.visualize_calls[0]["options"]
    assert client.timelines.visualize_calls[0]["revision_id"] == "rev-head"


def test_no_draft_reads_the_published_head(no_draft, capsys):
    client = FakeClient()
    code = _visualize(["--json"], client)
    envelope = json.loads(capsys.readouterr().out)
    assert code == 0
    assert envelope["data"]["outputs"]["working_copy"] is None
    assert len(client.timelines.visualize_calls) == 1
    assert client.timelines.visualize_calls[0]["revision_id"] == "rev-head"


def test_unselected_visualize_ignores_a_conflicting_draft(draft, capsys):
    client = FakeClient()
    assert _visualize(["--json"], client) == 0
    result = json.loads(capsys.readouterr().out)["data"]["outputs"]
    assert result["working_copy"] is None
    assert result["source_identity"] == {"kind": "revision", "revision_id": "rev-head"}
    assert client.timelines.visualize_calls[0]["revision_id"] == "rev-head"


def test_visualize_keeps_the_revision_pinned_when_head_advances(draft, capsys):
    class AdvancingTimelines(FakeTimelines):
        head = "rev-head"

        def open_bundle(self, project, ref, revision_id=None):
            result = super().open_bundle(project, ref, revision_id)
            self.head = "rev-next"
            return result

    client = FakeClient()
    client.timelines = AdvancingTimelines()

    assert _visualize(["--json"], client) == 0
    result = json.loads(capsys.readouterr().out)["data"]["outputs"]

    assert client.timelines.head == "rev-next"
    assert result["source_identity"] == {"kind": "revision", "revision_id": "rev-head"}
    assert client.timelines.visualize_calls[0]["revision_id"] == "rev-head"


def test_changed_cut_line_pages_explicitly():
    assert cli._changed_cuts_line([12, 13, 22]) == (
        "showing the 3 cuts you changed (12, 13, 22) · --every-cut for all cuts"
    )
    assert cli._changed_cuts_line([7]) == "showing the 1 cut you changed (7) · --every-cut for all cuts"
    assert cli._changed_cuts_line([]) == "the working copy changes no cut · --every-cut for all cuts"
    long = list(range(1, 21))
    line = cli._changed_cuts_line(long)
    assert line.startswith("showing 1–12 of 20 cuts you changed (1, 2, 3")
    assert "next: --cut 13" in line


def test_render_draft_calls_the_candidate_preview(draft, monkeypatch, capsys):
    from astrid.sdk import authoring_render_preview

    calls = []

    def fake_preview(candidate, client, *, project, timeline_ref, wait=True, **inputs):
        calls.append({"candidate": candidate, "project": project, "timeline_ref": timeline_ref,
                      "wait": wait, "inputs": inputs})
        return _render_result()

    monkeypatch.setattr(authoring_render_preview, "render_authoring_candidate_preview", fake_preview)
    client = FakeClient()
    client.invoke_result = lambda *a, **k: pytest.fail("published render must not run for --draft")
    code = _render(["--draft", "--json"], client)
    envelope = json.loads(capsys.readouterr().out)
    assert code == 0
    assert len(calls) == 1
    assert calls[0]["candidate"]["kind"] == "candidate"
    assert calls[0]["project"] == "demo"
    assert calls[0]["timeline_ref"] == "tl"
    assert "timeline_ref" not in calls[0]["inputs"]
    assert envelope["data"]["working_copy"] == {
        "draft": str(draft), "base_revision": "rev-published-1", "edits": 3,
    }


def test_render_draft_human_banner(draft, monkeypatch, capsys):
    from astrid.sdk import authoring_render_preview

    monkeypatch.setattr(authoring_render_preview, "render_authoring_candidate_preview",
                        lambda *a, **k: _render_result())
    parser = cli.build_parser(FakeClient())
    parsed = parser.parse_args(["render", "tl", "--project", "demo", "--draft"])
    parsed.client = FakeClient()
    parsed.json = False
    assert cli._cmd_render(parsed) == 0
    first = capsys.readouterr().out.splitlines()[0]
    assert first.startswith("WORKING COPY · 3 clips changed vs published rev-published-1")


def test_render_draft_named(draft, monkeypatch):
    from astrid.sdk import authoring_render_preview

    seen = {}

    def fake_find(project, timeline, name="main", client=None):
        seen["name"] = name
        return draft

    from astrid.sdk import timeline_checkout

    monkeypatch.setattr(timeline_checkout, "find_draft", fake_find)
    monkeypatch.setattr(authoring_render_preview, "render_authoring_candidate_preview",
                        lambda *a, **k: _render_result("r"))
    _render(["--draft", "alt", "--json"], FakeClient())
    assert seen["name"] == "alt"


def test_render_without_draft_is_the_published_render(no_draft, monkeypatch):
    from astrid.sdk import authoring_render_preview

    monkeypatch.setattr(authoring_render_preview, "render_authoring_candidate_preview",
                        lambda *a, **k: pytest.fail("--draft is not set"))
    client = FakeClient()
    seen = {}

    def invoke_result(name, **kwargs):
        seen["name"] = name
        seen["inputs"] = kwargs["inputs"]
        return _render_result("r")

    client.invoke_result = invoke_result
    assert _render(["--json"], client) == 0
    assert seen["name"] == "rendering.render"
    assert "authoring_preview" not in seen["inputs"]


def test_render_draft_without_checkout_fails_before_rendering(no_draft, monkeypatch, capsys):
    from astrid.sdk import authoring_render_preview

    monkeypatch.setattr(authoring_render_preview, "render_authoring_candidate_preview",
                        lambda *a, **k: pytest.fail("no working copy to render"))
    code = _render(["--draft", "--json"], FakeClient())
    envelope = json.loads(capsys.readouterr().out)
    assert code != 0
    assert envelope["ok"] is False
    assert "timelines checkout" in envelope["error"]["message"]


def test_render_audio_target_maps_to_delivery_audio():
    from astrid.packs.timeline import cli

    parsed = cli.build_parser(object()).parse_args(["render", "--project", "P", "tl", "--true-peak", "-1.5"])
    assert cli._delivery_audio(parsed) == {"lufs": -14.0, "true_peak": -1.5}
    parsed = cli.build_parser(object()).parse_args(["render", "--project", "P", "tl", "--audio-target", "-16"])
    assert cli._delivery_audio(parsed) == {"lufs": -16.0, "true_peak": -1.0}
    parsed = cli.build_parser(object()).parse_args(["render", "--project", "P", "tl"])
    assert cli._delivery_audio(parsed) is None


def test_compare_published_with_a_working_copy_does_not_claim_there_is_none(draft, capsys, monkeypatch):
    import astrid.core.timeline.authoring_bundle as ab

    monkeypatch.setattr(ab, "preview_authoring_candidate", lambda doc: {"kind": "authoring-candidate-preview"})
    client = FakeClient()
    code = _visualize(["--draft", "main", "--compare", "published"], client)
    out = capsys.readouterr().out
    assert code == 0
    assert "── PUBLISHED" in out and "── WORKING COPY" in out
    assert "no working copy" not in out


def test_compare_published_without_a_working_copy_still_says_so(no_draft, capsys):
    client = FakeClient()
    _visualize(["--compare", "published"], client)
    assert "no working copy: nothing to compare" in capsys.readouterr().out


# Parent coverage retained during the Release-1 + Timeline merge.
def test_visualize_reads_the_working_copy_with_banner_and_changed_cuts(draft, capsys, monkeypatch):
    import astrid.core.timeline.authoring_bundle as ab

    monkeypatch.setattr(ab, "preview_authoring_candidate", lambda doc: {"kind": "authoring-candidate-preview", "candidate": doc})
    client = FakeClient()
    code = _visualize([], client)
    out = capsys.readouterr().out
    assert code == 0
    assert out.splitlines()[0] == (
        "WORKING COPY · 3 clips changed vs published rev-publ · --published for the live version"
    )
    assert out.splitlines()[1].startswith("showing the 3 cuts you changed (12, 13, 22) · --every-cut for all cuts")
    assert "[✎ c12, c13, c22]" in out.splitlines()[1]
    options = client.timelines.visualize_calls[0]["options"]
    assert options["authoring_preview"]["kind"] == "authoring-candidate-preview"  # frames come from the working copy
    assert options["cuts"] == "12,13,22" and options["view"] == "contact"
