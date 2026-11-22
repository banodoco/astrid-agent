"""Deterministic synthetic stage attribution and CLI timing diagnostics.

This covers the named US-1 stage inventory and the timing display/counting
path. Injected stage scopes are not a claim that product instrumentation detects
delays in each of these real operations.
"""
from __future__ import annotations

import re
from io import StringIO
from types import SimpleNamespace

import pytest

from astrid.sdk import command_timing


# Concrete stage labels from US-1's client setup, pre-admission work, and
# Runtime request-by-path timings. Request paths use the normalized labels from
# the former US-2 CPU fixture so object IDs and project IDs stay deterministic.
US1_STAGE_NAMES = (
    "Runtime acquisition lock",
    "Runtime launcher connect",
    "Runtime launcher up",
    "Runtime launcher connection",
    "Runtime GET /v1/health",
    "Runtime POST /v1/handshake",
    "Runtime GET /v1/app-state/user/{key}",
    "Runtime GET /v1/capabilities",
    "Runtime GET /v1/objects/{digest}",
    "Runtime GET /v1/projects/{id}",
    "Runtime POST /v1/projects/{id}/objects",
    "Runtime POST /v1/tasks",
    "Runtime GET /v1/projects/{id}/timelines",
    "Runtime GET /v1/projects/{id}/timelines/{timeline_id}/commits",
    "Runtime GET /v1/projects/{id}/timelines/{timeline_id}/composition-revisions/{revision}",
    "Runtime POST /v1/projects/{id}/timelines/{timeline_id}/commit",
    "Runtime POST /v1/projects/{id}/timelines/{timeline_id}/inspect",
    "pack host attachment",
    "pack host lifecycle lock",
    "pack host readiness",
    "pack host source identity",
    "pack discovery",
    "entry lookup",
    "model registry",
    "target check",
    "workflow build",
    # Named generation stages exercised by the separate public-route stage
    # tree test, kept here so the synthetic diagnostic covers that inventory.
    "argument parsing",
    "Runtime connection",
    "candidate qualification",
    "candidate preparation",
    "target node list",
    "workflow emission",
    "approval compile",
    "bundle materialisation",
    "object import",
    "invocation",
    "capability registries",
    "capability lookup",
    "SDK admission preflight",
    "Workspace admission preflight",
    "Remote admission preflight",
    "task creation",
    "workflow snapshot",
    "source retention",
    "workflow inputs",
    "entry needs",
    "entry discovery",
)

BASE_SECONDS = 0.125
INJECTED_DELTA_SECONDS = 1.5


def _collect_synthetic_stages(monkeypatch, delayed_stage: str | None):
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(
        command_timing,
        "time",
        SimpleNamespace(monotonic=lambda: clock.now),
    )
    collector = command_timing.TimingCollector(started=clock.now)
    # The monitor's real thread is irrelevant to arithmetic and cannot observe
    # a clock advanced synchronously by this fixture.
    collector.monitor = SimpleNamespace(join=lambda: None)

    with command_timing.timing_scope(collector):
        for name in US1_STAGE_NAMES:
            with command_timing.stage(name):
                clock.now += BASE_SECONDS
                if name == delayed_stage:
                    clock.now += INJECTED_DELTA_SECONDS
        rows, span = collector.snapshot_with_span()
    return rows, span


def _rendered_seconds(diagnostic: str, stage_name: str) -> float:
    match = re.search(
        rf"^{re.escape(stage_name)}: ([0-9.]+)s \(total [0-9.]+s\)$",
        diagnostic,
        flags=re.MULTILINE,
    )
    assert match, f"missing rendered timing for {stage_name!r}:\n{diagnostic}"
    return float(match.group(1))


def _diagnostic(rows) -> str:
    rendered = StringIO()
    command_timing.print_timings(rows, file=rendered)
    return rendered.getvalue()


def test_named_us1_stages_attribute_synthetic_delta_and_render_diagnostic(monkeypatch):
    baseline_rows, baseline_span = _collect_synthetic_stages(monkeypatch, delayed_stage=None)
    baseline_output = _diagnostic(baseline_rows)
    baseline_by_name = {
        name: _rendered_seconds(baseline_output, name) for name in US1_STAGE_NAMES
    }

    expected_baseline = BASE_SECONDS * len(US1_STAGE_NAMES)
    assert baseline_span == pytest.approx(expected_baseline)
    assert abs(sum(row["seconds"] for row in baseline_rows) - baseline_span) <= 0.1 * baseline_span

    for delayed_stage in US1_STAGE_NAMES:
        delayed_rows, delayed_span = _collect_synthetic_stages(monkeypatch, delayed_stage=delayed_stage)
        diagnostic = _diagnostic(delayed_rows)
        attributed = {
            name: _rendered_seconds(diagnostic, name) - baseline_by_name[name]
            for name in US1_STAGE_NAMES
        }

        assert attributed[delayed_stage] == pytest.approx(INJECTED_DELTA_SECONDS)
        assert all(delta == 0.0 for name, delta in attributed.items() if name != delayed_stage)
        assert delayed_span == pytest.approx(expected_baseline + INJECTED_DELTA_SECONDS)
        assert abs(sum(row["seconds"] for row in delayed_rows) - delayed_span) <= 0.1 * delayed_span
        assert f"{delayed_stage}:" in diagnostic

