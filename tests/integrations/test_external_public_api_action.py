"""PK-7: an external pack action uses only the public pack API and runs through the host.

The pack lives outside core. Its action module imports ``astrid.pack_api`` (the one
versioned facade); inventory admits it, the generic host discovers it, and the
command child runs the module from the selected pack root. A sibling pack whose
action imports Astrid internals is quarantined by the same inventory check.
"""
from __future__ import annotations

import json
from pathlib import Path

import yaml

from astrid import pack_api
from astrid.core.execution.generic_host import GenericPackHost
from astrid.core.pack import loader


def _write_pack(root: Path, pack_id: str, body: str) -> Path:
    pack = root / pack_id
    (pack / "actions" / "report").mkdir(parents=True)
    (pack / "actions" / "report" / "run.py").write_text(body, encoding="utf-8")
    manifest = {
        "schema_version": 3, "id": pack_id, "name": pack_id, "version": "1.0.0", "pack_api": "1.0",
        "documentation": {"kind": "none", "reason": "PK-7 fixture."},
        "actions": {"report": {
            "description": "Report the public API version", "short_description": "Report API version",
            "invocation": {"kind": "command", "command": {
                # Selected-pack module route (the host refuses scripts it cannot place in the pack).
                "argv": ["{python_exec}", "-m", f"astrid.packs.{pack_id}.actions.report.run", "{out}/report.json"]}},
            "inputs": [], "outputs": [],
        }},
    }
    (pack / "pack.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
    return pack


PUBLIC = """import json, sys
from pathlib import Path
from astrid.pack_api import VERSION
Path(sys.argv[1]).parent.mkdir(parents=True, exist_ok=True)
Path(sys.argv[1]).write_text(json.dumps({"pack_api": list(VERSION), "module": __file__}))
"""
INTERNAL = "from astrid.core.pack import loader\n"


def test_external_public_api_action_is_admitted_and_runs(tmp_path: Path) -> None:
    collection = tmp_path / "packs"
    public = _write_pack(collection, "publicapi", PUBLIC)
    _write_pack(collection, "internalapi", INTERNAL)

    scan = loader.scan_pack_roots((collection,))
    assert [pack.id for pack in scan.packs] == ["publicapi"]
    (refused,) = scan.quarantined
    assert refused.pack_id == "internalapi" and "imports code outside" in refused.error

    host = GenericPackHost(pack_roots=[collection])
    host.discover()
    assert "internalapi.report" not in host.capabilities
    record = host.capabilities["publicapi.report"]
    assert Path(record.source_root).resolve().is_relative_to(public.resolve())

    out = tmp_path / "attempt" / "outputs"
    out.mkdir(parents=True)
    host._run_command_definition(record, {}, out, tmp_path / "attempt", allow_runtime_connection=False)
    report = json.loads((out / "report.json").read_text(encoding="utf-8"))
    assert tuple(report["pack_api"]) == tuple(pack_api.VERSION)
    assert Path(report["module"]).resolve() == (public / "actions" / "report" / "run.py").resolve()
