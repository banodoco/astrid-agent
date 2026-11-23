from __future__ import annotations

import time

from astrid.core.cli.domain_timelines import build_parser
from astrid.core.pack.discovery import discover_pack_metadata
from astrid.sdk.command_timing import TimingCollector, stage, timing_scope


def test_pack_discovery_is_attributed_inside_registry_stage(tmp_path, monkeypatch):
    monkeypatch.delenv("ASTRID_PACKS_PATH", raising=False)

    def scan(*args, **kwargs):
        time.sleep(0.05)
        return ()

    collector = TimingCollector()
    with timing_scope(collector), stage("registry"):
        discover_pack_metadata(project_root=tmp_path, personal_packs_root=tmp_path,
                               discover_packs_fn=scan)
    rows = collector.snapshot()
    discovery = sum(row["seconds"] for row in rows if row["stage"] == "pack discovery")
    assert discovery >= 0.05
    assert any(row["stage"] == "registry" for row in rows)


def test_timeline_help_exposes_timing_option():
    parser = build_parser(None)
    assert "--timings" in parser.format_help()
    assert "--timings" in parser._subparsers._group_actions[0].choices["commit"].format_help()
