from __future__ import annotations

from types import SimpleNamespace

from astrid.sdk.contracts import DomainResult
from astrid.sdk.remote import RemoteShots
from astrid.sdk.shot_grouping import group_timeline_clips
from astrid.core.cli.domain_shots import build_parser


def test_legacy_grouping_is_typed_410_without_runtime_calls() -> None:
    calls = []
    result = group_timeline_clips(
        shots=SimpleNamespace(), timelines=SimpleNamespace(), project="demo",
        timeline="main", clip_ids=["clip"], name="Shot", expected_version=1,
        idempotency_key="stable",
    )
    assert not result.ok
    assert result.error.code == "retired_route"
    assert result.error.details["status"] == 410
    assert result.error.details["replacement"] == "publish_parent_composition"
    assert calls == []


def test_remote_shots_group_is_typed_410_without_runtime_calls() -> None:
    class Transport:
        def list_timelines(self, *args, **kwargs):
            raise AssertionError("retired grouping must not read timelines")

    result = RemoteShots(Transport()).group(
        "demo", "main", clip_ids=["clip"], name="Shot", expected_version=1,
        idempotency_key="stable",
    )
    assert not result.ok
    assert result.error.code == "retired_route"
    assert result.error.details["status"] == 410


def test_shots_cli_has_no_group_command() -> None:
    client = SimpleNamespace(shots=SimpleNamespace())
    parser = build_parser(client)
    assert "group" not in parser._subparsers._group_actions[0].choices
