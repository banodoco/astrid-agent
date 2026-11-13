"""CPU only CLI proof for non registry generation catalog rows."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from astrid.core.generation import cli, entries, listing
from astrid.core.model_catalog.registry import ModelRegistry
from scripts.gen_capability_index import _in_repo_packs_only


@pytest.fixture(scope="module")
def catalog_snapshot():
    with _in_repo_packs_only():
        return (ModelRegistry.load_default(), listing.catalog_rows(), listing.discover(),
                entries.contributed_entries())


@pytest.fixture(autouse=True)
def isolated_catalog(monkeypatch, catalog_snapshot):
    registry, rows, discovery, contributed = catalog_snapshot
    monkeypatch.setattr(ModelRegistry, "load_default", classmethod(lambda cls, **_kwargs: registry))
    monkeypatch.setattr(listing, "catalog_rows", lambda: rows)
    monkeypatch.setattr(listing, "discover", lambda: discovery)
    monkeypatch.setattr(entries, "user_entries", lambda: contributed)


def test_non_registry_catalog_entries_use_default_placement(monkeypatch, capsys):
    from astrid.sdk.generation import GenerationFacade

    snapshot_rows = listing.catalog_rows()
    registry_keys = {(row["id"], row.get("mode")) for row in snapshot_rows if row["source"] == "catalog"}
    rows, seen = [], set()
    for row in snapshot_rows:
        key = (row["id"], row.get("mode"))
        if (row["source"] not in {"ready-template", "user-entry", "direct-image", "catalog-alias", "action-entry"}
                or key in registry_keys or key in seen):
            continue
        rows.append(row)
        seen.add(key)
    assert rows, "expected non registry catalog rows"
    routed = []

    def intercept(self, **kwargs):
        routed.append({"placement": kwargs.get("placement"), "mode": kwargs.get("mode"),
                       "entry": (kwargs.get("workflow_entry") or {}).get("id") or kwargs.get("model")})
        return {"status": "intercepted"}

    monkeypatch.setattr(cli, "_generate_facade", lambda: GenerationFacade())
    monkeypatch.setattr(GenerationFacade, "_generate_on_target_impl", intercept)
    monkeypatch.setattr(GenerationFacade, "_invoke", lambda *a, **k: pytest.fail("executor invocation forbidden"))

    report = []
    for row in rows:
        declared_mode = row.get("mode") or ""
        record = {"entry": row["id"], "source": row["source"], "kind": row["kind"],
                  "declared_mode": declared_mode, "declared_execution": "local",
                  "resolved_mode": None, "resolved_path": None, "result": None}
        if row["kind"] == "generation":
            before = len(routed)
            code = cli.main(["generation", "fixture prompt", "--model", row["id"], "--dry-run", "--json"])
            payload = json.loads(capsys.readouterr().out)
            assert code == 2 and payload["status"] == "blocked"
            assert len(payload["issues"]) == 1
            issue = payload["issues"][0]
            assert issue["code"] == "input_rejected"
            assert issue["message"].startswith("argument modality: invalid choice: 'generation'")
            assert len(routed) == before
            record["result"] = "classification refusal: CLI has no generation media subcommand"
            report.append(record)
            continue

        args = [row["kind"], "fixture prompt", "--model", row["id"]]
        if declared_mode:
            args.extend(["--mode", declared_mode])
        args.extend(["--dry-run", "--json"])
        before = len(routed)
        code = cli.main(args)
        stdout = capsys.readouterr().out
        if len(routed) > before:
            assert len(routed) == before + 1, f"{row['id']} {declared_mode} routed more than once"
            capture = routed[-1]
            assert capture["placement"] == "auto"
            assert capture["entry"] == row["id"]
            if declared_mode:
                assert capture["mode"] == declared_mode
            assert code == 0, stdout
            record["resolved_mode"] = capture["mode"]
            record["resolved_path"] = "comfyui"
            record["result"] = "PASS: CLI routed default auto placement to SDK target boundary"
        else:
            assert not declared_mode, f"classified row {row['id']} {declared_mode} failed to route: {stdout}"
            payload = json.loads(stdout) if stdout.strip().startswith("{") else {}
            issues = payload.get("issues") or []
            assert code != 0 and len(issues) == 1 and issues[0].get("code") in {"input_rejected", "missing_dependency"}, (
                f"{row['id']} has an unclassified mode and returned an untyped or unexpected refusal: {stdout}"
            )
            assert issues[0].get("message") == "the following arguments are required: --mode", stdout
            record["result"] = f"unclassified mode refusal: {issues[0]['code']}: {issues[0]['message']}"
        report.append(record)

    report_path = Path.cwd() / ".tmp-tests" / "workflow-table.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    assert len(report) == len(rows)
