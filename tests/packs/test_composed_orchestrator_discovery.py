"""Real SDK discovery must register each physical pack declaration once."""
from __future__ import annotations

import json
import os

import pytest

from astrid.core.pack.discovery import discover_pack_metadata
from astrid.core.pack.loader import packs_root
from astrid.core.execution.orchestrator.registry import OrchestratorRegistryError
from astrid.sdk.discovery import _load_registries


@pytest.mark.parametrize("exposure", ["env-parent", "env-pack", "extra-parent", "overlapping-external"])
def test_sdk_registries_share_one_bundled_pack_owner(tmp_path, monkeypatch, exposure):
    bundled = packs_root()
    extra = ()
    if exposure == "env-parent":
        monkeypatch.setenv("ASTRID_PACKS_PATH", str(bundled))
    elif exposure == "env-pack":
        monkeypatch.setenv("ASTRID_PACKS_PATH", str(bundled / "h3_av"))
    elif exposure == "extra-parent":
        monkeypatch.delenv("ASTRID_PACKS_PATH", raising=False)
        extra = (str(bundled),)
    else:
        monkeypatch.setenv("ASTRID_PACKS_PATH", str(bundled / "h3_av"))
        extra = (str(bundled),)
    # Match the SDK call made by an installed host action, including the
    # unrelated executor registry that loads before orchestrator registration.
    executors, orchestrators, _ = _load_registries(project_root=tmp_path, extra_pack_roots=extra)
    owners = [row for row in discover_pack_metadata(project_root=tmp_path, extra_pack_roots=extra)
              if row.pack.root.resolve() == (bundled / "h3_av").resolve()]
    assert len(owners) == 1
    assert owners[0].source_kind == "source"
    for local_id, action in owners[0].pack.actions.items():
        if action.get("metadata", {}).get("local_orchestrator") is True:
            identity = f"{owners[0].id}.{local_id}"
            assert orchestrators.get(identity).metadata["pack_root"] == str(owners[0].pack.root)
            assert identity not in executors.as_mapping()


def test_sdk_registries_reject_distinct_pack_owners_of_one_orchestrator(tmp_path, monkeypatch):
    roots = []
    for name in ("left", "right"):
        root = tmp_path / name / "fixture"
        root.mkdir(parents=True)
        (root / "pack.yaml").write_text(json.dumps({
            "schema_version": 3, "id": "fixture", "name": "Fixture", "version": "1.0.0",
            "actions": {"parent": {
                "name": "Parent", "description": "Local parent", "inputs": [], "outputs": [],
                "invocation": {"kind": "command", "command": {"argv": ["{python_exec}", "-c", "pass"]}},
                "metadata": {"local_orchestrator": True},
            }},
        }))
        roots.append(str(root))
    monkeypatch.setenv("ASTRID_PACKS_PATH", os.pathsep.join(roots))
    with pytest.raises(OrchestratorRegistryError, match="local orchestrator collides with orchestrator identity"):
        _load_registries(project_root=tmp_path)
