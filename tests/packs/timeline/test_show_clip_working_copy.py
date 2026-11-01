"""show --clip X (flag) and show TL X (positional) read the same thing: the working copy when one exists,
the published head with --published. An edited param shows its working-copy value in both forms."""
from __future__ import annotations

import copy
import json
import re
from types import SimpleNamespace

import pytest

from astrid.packs.timeline import cli
from astrid.sdk import timeline_checkout as tc
from tests.sdk.test_timeline_reflow_golden import film

EDITED_X = 777  # c1-rocket's x is 10 in the head; the working copy edits it to 777


class _Client:
    """Only what show reads: the published head, by open_bundle."""

    def __init__(self, bundle):
        self.bundle = bundle
        self.timelines = self

    def open_bundle(self, project, ref, revision_id=None, **_):
        return SimpleNamespace(ok=True, data={"bundle": copy.deepcopy(self.bundle), "revision_id": "rev-head"})


def _x_line(out: str) -> str:
    """The value of the x param in a clip record (its own line under params)."""
    match = re.search(r"^\s+x\s+(\S+)", out, re.MULTILINE)
    assert match, out
    return match.group(1)


@pytest.fixture
def world(tmp_path, monkeypatch):
    head = film()
    draft = copy.deepcopy(head)
    rocket = next(c for c in draft["shots"]["F"]["internal_timeline"]["clips"] if c["id"] == "c1-rocket")
    rocket["params"]["x"] = EDITED_X
    path = tmp_path / "working.json"
    path.write_text(json.dumps(draft))
    monkeypatch.setattr(tc, "find_draft", lambda project, timeline, name=None, client=None: path)
    return _Client(head)


def _run(client, *args, capsys):
    parsed = cli.build_parser(client).parse_args(["show", "--project", "p", "t", *args])
    assert parsed.handler(parsed) == 0
    return capsys.readouterr().out


def test_the_flag_form_reads_the_working_copy(world, capsys):
    out = _run(world, "--clip", "c1-rocket", capsys=capsys)
    assert out.startswith("WORKING COPY"), out
    edited = _x_line(out)
    head = _x_line(_run(world, "--clip", "c1-rocket", "--published", capsys=capsys))
    assert edited != head  # the working copy's x is not the head's


def test_the_positional_form_reads_the_working_copy_the_same_way(world, capsys):
    out = _run(world, "c1-rocket", capsys=capsys)
    assert out.startswith("WORKING COPY"), out
    assert _x_line(out) == _x_line(_run(world, "--clip", "c1-rocket", capsys=capsys))


def test_published_reads_the_head_in_the_flag_form_and_the_positional_form(world, capsys):
    flag = _run(world, "--clip", "c1-rocket", "--published", capsys=capsys)
    positional = _run(world, "c1-rocket", "--published", capsys=capsys)
    assert flag.startswith("PUBLISHED") and positional.startswith("PUBLISHED")
    assert _x_line(flag) == _x_line(positional) != _x_line(_run(world, "c1-rocket", capsys=capsys))


def test_the_layer_projection_is_still_there_with_its_own_selectors(world, capsys):
    """--layers keeps the bounded per-track projection (the composition read), not the record: the fake client
    has no such read, so the projection reports it unavailable, and no working-copy record is printed."""
    parsed = cli.build_parser(world).parse_args(["show", "--project", "p", "t", "--clip", "c1-rocket", "--layers"])
    assert parsed.handler(parsed) == 1
    captured = capsys.readouterr()
    assert "canonical timeline inspection is unavailable" in captured.err
    assert "WORKING COPY" not in captured.out
