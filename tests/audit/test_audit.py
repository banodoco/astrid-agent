from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from astrid import audit
from astrid.core.audit import AuditContext


def test_audit_collects_ephemeral_provenance_without_ledger(tmp_path: Path) -> None:
    run = tmp_path / "run"
    artifact = run / "frames" / "frame.txt"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("hello audit", encoding="utf-8")

    ctx = AuditContext.for_run(run)
    source_id = ctx.register_asset(kind="source", path=artifact, label="Source")
    output_id = ctx.register_asset(kind="text", path=artifact, label="Output", parents=[source_id])
    ctx.register_node(stage="demo", parents=[source_id], outputs=[output_id])

    assert [row["asset_id"] for row in ctx.records if "asset_id" in row] == [source_id, output_id]
    assert not (run / "audit" / "ledger.jsonl").exists()


def test_ephemeral_provenance_ids_are_stable(tmp_path: Path) -> None:
    run = tmp_path / "run"
    artifact = run / "asset.txt"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("v1", encoding="utf-8")
    ctx = AuditContext.for_run(run)
    parent_id = ctx.register_asset(kind="source", label="Source")
    first_id = ctx.register_asset(kind="text", path=artifact, label="Output", parents=[parent_id])
    artifact.write_text("v2", encoding="utf-8")
    second_id = ctx.register_asset(kind="text", path=artifact, label="Output", parents=[parent_id])

    assert first_id == second_id
    assert len(ctx.records) == 3
    assert not (run / "audit" / "ledger.jsonl").exists()


def test_audit_redacts_secret_like_values(tmp_path: Path) -> None:
    ctx = AuditContext.for_run(tmp_path / "run")
    ctx.register_node(
        stage="secret-test",
        metadata={"OPENAI_API_KEY": "sk-testsecret1234567890", "nested": {"token": "hf_abcdefghijklmnop"}},
    )
    event = ctx.records[0]
    assert event["metadata"]["OPENAI_API_KEY"] == "<redacted>"
    assert event["metadata"]["nested"]["token"] == "<redacted>"


def test_pipeline_audit_cli_is_retired() -> None:
    with pytest.raises(ModuleNotFoundError):
        __import__("astrid.core.audit.cli")


