from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace

from astrid.packs.timeline import cli as timeline_cli
from astrid.packs.timeline import cli_log
from astrid.packs.timeline import cli_sweep
from astrid.packs.timeline import cli_verify
import astrid.sdk.timeline_checkout as timeline_checkout
from astrid.sdk.contracts import DomainResult
from astrid.sdk.timeline_checkout import Checkout


def test_timeline_cli_binds_check_log_and_sweep_to_the_shared_client() -> None:
    client = object()
    parser = timeline_cli.build_parser(client)

    edit = parser.parse_args(["edit", "--project", "project-1", "main", "--sweep"])
    assert edit.client is client
    assert edit.sweep is True
    assert edit.handler.__name__ == "_cmd_edit"

    no_sweep = parser.parse_args(["edit", "--project", "project-1", "main", "--no-sweep"])
    assert no_sweep.client is client
    assert no_sweep.no_sweep is True

    check = parser.parse_args(["check", "main", "--project", "project-1", "--sweep"])
    assert check.client is client
    assert check.sweep is True
    assert check.handler.__name__ == "_cmd_check"

    log = parser.parse_args(["log", "main", "--project", "project-1", "--verify", "--json"])
    assert log.client is client
    assert log.verify is True
    assert log.json is True
    assert log.handler is cli_log.run_log


def test_log_verify_broken_hash_chain_json_is_failure(tmp_path, monkeypatch, capsys) -> None:
    client = object()
    parser = timeline_cli.build_parser(client)
    path = tmp_path / "broken.journal.jsonl"
    path.write_text('{"seq":1,"prev":null,"hash":"sha256:broken"}\n', encoding="utf-8")
    monkeypatch.setattr(cli_log.store, "resolve_ids", lambda project, timeline, *, client=None: ("p", "t", "head"))
    monkeypatch.setattr(cli_log.journal, "journal_path", lambda project, timeline: path)

    parsed = parser.parse_args(["log", "main", "--project", "project-1", "--verify", "--json"])
    code = parsed.handler(parsed)

    payload = json.loads(capsys.readouterr().out)
    assert code == 1
    assert set(payload) == {"ok", "data", "error", "receipt", "idempotency_key"}
    assert payload["ok"] is False
    assert payload["data"] is None
    assert payload["error"]["code"] == "validation_error"
    assert payload["error"]["details"]["problem_count"] >= 1


_TIMELINE_FIXTURE = (
    Path(__file__).resolve().parents[2] / "fixtures" / "timeline_editing" / "tiny.json"
)


def _tiny_checkout() -> Checkout:
    return Checkout(json.loads(_TIMELINE_FIXTURE.read_text(encoding="utf-8")))


def _seeded_edit_parser(tmp_path, monkeypatch):
    # Use the actual Checkout draft store and CLI handler; only the published-head
    # lookup is fixture-backed, as in the existing timeline CLI tests.
    bundle = json.loads(_TIMELINE_FIXTURE.read_text(encoding="utf-8"))
    monkeypatch.setenv("ASTRID_RUNTIME_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setattr(
        timeline_checkout,
        "resolve_ids",
        lambda project, timeline, client=None: ("p", "t", "rev-0"),
    )
    monkeypatch.setattr(
        timeline_checkout,
        "fetch_bundle",
        lambda project, timeline, revision_id=None, client=None: copy.deepcopy(bundle),
    )
    return timeline_cli.build_parser(None)


def _saved_edit_draft() -> Checkout:
    path = timeline_checkout.find_draft("P", "t")
    assert path is not None
    return Checkout.load(path)


def test_edit_auto_sweep_uses_only_cuts_changed_between_checkout_documents(monkeypatch) -> None:
    checkout = _tiny_checkout()
    before = copy.deepcopy(checkout.document())
    checkout.clip("c2.card").nudge(0.1)

    changed_cuts = cli_sweep.touched_cuts(before, checkout)
    assert changed_cuts == {"c2"}
    calls = []
    monkeypatch.setattr(cli_sweep, "print_sweep", lambda *_args: calls.append(True) or 0)
    parsed = SimpleNamespace(no_sweep=False, sweep=False)

    cli_sweep.auto_sweep_after_edit(
        parsed, checkout, reflow=False, touched_cuts=len(changed_cuts)
    )

    assert calls == []


def test_edit_no_sweep_suppresses_reflow_and_explicit_sweep(monkeypatch) -> None:
    checkout = _tiny_checkout()
    before = copy.deepcopy(checkout.document())
    checkout.clip("c2.card").nudge(0.1)
    changed_cuts = cli_sweep.touched_cuts(before, checkout)
    calls = []
    monkeypatch.setattr(cli_sweep, "print_sweep", lambda *_args: calls.append(True) or 2)
    parsed = SimpleNamespace(no_sweep=True, sweep=True)

    cli_sweep.auto_sweep_after_edit(
        parsed, checkout, reflow=True, touched_cuts=len(changed_cuts)
    )

    assert calls == []


def test_edit_sweep_propagates_unexpected_and_not_captured_status(monkeypatch) -> None:
    checkout = _tiny_checkout()
    parsed = SimpleNamespace(no_sweep=False, sweep=True)

    for expected in (1, 2):
        monkeypatch.setattr(
            cli_sweep, "print_sweep", lambda *_args, result=expected: result
        )
        assert cli_sweep.auto_sweep_after_edit(
            parsed, checkout, reflow=False, touched_cuts=0
        ) == expected


def test_cli_edit_handler_sweeps_a_saved_structural_reflow(tmp_path, monkeypatch) -> None:
    parser = _seeded_edit_parser(tmp_path, monkeypatch)
    before = Checkout.draft("P", "t").document()
    captured = []

    def capture(_parsed, checkout):
        assert isinstance(checkout, Checkout)
        captured.append(checkout.document())
        return 0

    monkeypatch.setattr(cli_sweep, "print_sweep", capture)
    parsed = parser.parse_args(["edit", "t", "--project", "P", "--film-end", 'after "now" +2.3s'])

    assert parsed.handler(parsed) == 0
    assert len(captured) == 1
    assert captured[0] != before
    assert _saved_edit_draft().document() == captured[0]


def test_cli_edit_handler_does_not_sweep_an_ordinary_single_cut_nudge(tmp_path, monkeypatch) -> None:
    parser = _seeded_edit_parser(tmp_path, monkeypatch)
    captured = []
    monkeypatch.setattr(
        cli_sweep, "print_sweep", lambda *_args: captured.append(True) or 0
    )
    parsed = parser.parse_args([
        "edit", "t", "--project", "P", "--clip", "c2.card", "--nudge", "0.1",
    ])

    assert parsed.handler(parsed) == 0
    assert captured == []
    assert _saved_edit_draft().clip("c2.card").start == 4.1


def test_cli_edit_handler_no_sweep_suppresses_reflow_capture(tmp_path, monkeypatch) -> None:
    parser = _seeded_edit_parser(tmp_path, monkeypatch)
    before = Checkout.draft("P", "t").document()
    captured = []
    monkeypatch.setattr(
        cli_sweep, "print_sweep", lambda *_args: captured.append(True) or 0
    )
    parsed = parser.parse_args([
        "edit", "t", "--project", "P", "--film-end", 'after "now" +2.3s', "--no-sweep",
    ])

    assert parsed.handler(parsed) == 0
    assert captured == []
    assert _saved_edit_draft().document() != before


def test_cli_edit_handler_propagates_uncaptured_sweep_after_saving(tmp_path, monkeypatch) -> None:
    parser = _seeded_edit_parser(tmp_path, monkeypatch)
    before = Checkout.draft("P", "t").document()
    captured = []

    def capture(_parsed, checkout):
        captured.append(checkout.document())
        return 2

    monkeypatch.setattr(cli_sweep, "print_sweep", capture)
    parsed = parser.parse_args(["edit", "t", "--project", "P", "--film-end", 'after "now" +2.3s'])

    assert parsed.handler(parsed) == 2
    assert len(captured) == 1
    assert captured[0] != before
    assert _saved_edit_draft().document() == captured[0]


def test_check_no_sweep_suppresses_explicit_sweep(monkeypatch, capsys) -> None:
    parser = timeline_cli.build_parser(object())
    swept = []

    class Report:
        valid = True

        def __str__(self) -> str:
            return "working copy valid"

    class FakeCheckout:
        def check(self):
            return Report()

    class FakeCheckoutClass:
        @classmethod
        def load(cls, path):
            return FakeCheckout()

    monkeypatch.setattr(timeline_checkout, "find_draft", lambda *args, **kwargs: "draft.json")
    monkeypatch.setattr(timeline_checkout, "Checkout", FakeCheckoutClass)
    monkeypatch.setattr(cli_sweep, "print_sweep", lambda *_args: swept.append(True) or 0)

    with_sweep = parser.parse_args(["check", "main", "--project", "project-1", "--sweep"])
    assert with_sweep.handler(with_sweep) == 0
    assert swept == [True]

    suppressed = parser.parse_args(["check", "main", "--project", "project-1", "--sweep", "--no-sweep"])
    assert suppressed.handler(suppressed) == 0
    assert swept == [True]
    assert "working copy valid" in capsys.readouterr().out


def test_check_labels_parser_and_helper_preserve_existing_layer_order() -> None:
    client = object()
    parser = timeline_cli.build_parser(client)

    check = parser.parse_args([
        "check", "main", "--project", "project-1", "--at", "1.0",
        "--at", "c08.loras", "--labels",
    ])
    assert check.at == ["1.0", "c08.loras"]
    assert check.labels is True

    visualize = parser.parse_args([
        "visualize", "--view", "motion", "--layer", "motion,labels",
        "--layer", "contact", "--labels",
    ])
    assert visualize.labels is True
    assert cli_verify.with_labels_layer(
        ["motion", "labels", "contact", "labels"], True
    ) == ["motion", "labels", "contact"]
    assert cli_verify.with_labels_layer(["motion", "motion"], True) == [
        "motion", "motion", "labels"
    ]
    assert cli_verify.with_labels_layer(["motion", "labels", "labels"], False) == [
        "motion", "labels", "labels"
    ]


def test_check_labels_calls_existing_verify_only_when_at_is_supplied(monkeypatch, capsys) -> None:
    client = object()
    parser = timeline_cli.build_parser(client)
    calls = []

    class Report:
        valid = True

        def __str__(self) -> str:
            return "working copy valid"

        def full_text(self) -> str:
            return "working copy valid (full)"

    class FakeCheckout:
        def check(self):
            return Report()

        def verify(self, **kwargs):
            calls.append(kwargs)
            return {"lines": ["verify: labelled frames compared"]}

    class FakeCheckoutClass:
        @classmethod
        def load(cls, path):
            return FakeCheckout()

    monkeypatch.setattr(timeline_checkout, "find_draft", lambda *args, **kwargs: "draft.json")
    monkeypatch.setattr(timeline_checkout, "Checkout", FakeCheckoutClass)

    without_at = parser.parse_args(["check", "main", "--project", "project-1", "--labels"])
    assert without_at.handler(without_at) == 0
    assert calls == []

    with_at = parser.parse_args([
        "check", "main", "--project", "project-1", "--at", "1.0",
        "--at", "c08.loras", "--labels",
    ])
    assert with_at.handler(with_at) == 0
    assert calls == [{"at": ["1.0", "c08.loras"], "client": client, "labels": True}]
    assert "verify: labelled frames compared" in capsys.readouterr().out


def test_visualize_labels_adds_layer_without_reordering_other_layers(monkeypatch) -> None:
    captured = []

    class Timelines:
        def visualize(self, project, timeline_slug, **kwargs):
            captured.append(kwargs)
            return DomainResult.success({"artifacts": []})

    client = SimpleNamespace(timelines=Timelines())
    parser = timeline_cli.build_parser(client)
    parsed = parser.parse_args([
        "visualize", "--project", "project-1", "--view", "motion",
        "--layer", "motion,labels", "--layer", "contact", "--labels", "--json",
    ])
    monkeypatch.setattr(timeline_cli, "print_result", lambda *_args, **_kwargs: 0)

    assert parsed.handler(parsed) == 0
    assert captured[0]["options"]["layers"] == ["motion", "labels", "contact"]
