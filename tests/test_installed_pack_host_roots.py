"""Installed staging keeps wheel packs separate from source-only resources."""

import logging
import shutil
from pathlib import Path

from astrid.core.execution.generic_host import GenericPackHost, _launch_worker_probe_environments
from astrid.core.pack import loader


def test_installed_host_uses_bundled_packs_during_launch_probe(tmp_path, monkeypatch, caplog):
    checkout = Path(__file__).resolve().parents[1]
    agent = tmp_path / "a"
    wheel = tmp_path / "py/lib/python3.11/site-packages/astrid"
    shutil.copytree(checkout / "astrid/packs", agent / "astrid/packs")
    shutil.copytree(checkout / "astrid/packs", wheel / "packs")
    shutil.copytree(checkout / "config", agent / "config")
    shutil.copytree(checkout / "astrid/core/model_catalog", agent / "astrid/core/model_catalog")
    monkeypatch.setattr(loader, "packs_root", lambda: wheel / "packs")
    monkeypatch.setenv("ASTRID_HOST_EXECUTION_PROFILE", "installed")
    matrix = agent / "config/astrid-beta-capabilities.json"
    roots = (wheel / "packs",)

    with caplog.at_level(logging.WARNING):
        _launch_worker_probe_environments(roots, {"HOME": str(tmp_path)}, matrix, remotion_only=True, installed_root=wheel.parent)
        host = GenericPackHost(pack_roots=list(roots), capability_matrix=matrix)
        records = host.discover()

    assert not [r for r in caplog.records if "quarantined optional pack 'runpod'" in r.message or "quarantined optional pack 'setup'" in r.message]
    scan = loader.scan_pack_roots(roots, source_records={})
    packs = {pack.id: pack for pack in scan.packs}
    assert all(packs[name].root == wheel / "packs" / name for name in ("runpod", "setup"))
    assert not {"runpod", "setup"} & {row.pack_id for row in scan.quarantined}
    assert {"runpod.claim", "runpod.keep", "runpod.provision"} <= {record.id for record in records}
    assert not host.ledger["sources"]["coverage"]["executor_inventory"]["added"]
    inventory = {row["id"]: row for row in host.ledger["sources"]["executor_inventory"]}
    for name in ("runpod.claim", "runpod.keep", "runpod.provision"):
        assert inventory[name]["discovery_status"] == "discovered"
        assert host.matrix[name]["disposition"] == "optional"
