"""Child of the PK-12 installed import-policy test: check the installed packs only."""
import json
import sys
from pathlib import Path

root, site = map(Path, sys.argv[1:3])
import astrid
from astrid import pack_api, sdk
from astrid.core.pack.discovery import discover_pack_metadata
from astrid.core.pack.loader import PACK_API_VERSION
from astrid.pack_imports import ImportContract, check_pack_imports

assert sys.flags.isolated and Path(astrid.__file__).is_relative_to(site)
rows = [row for row in discover_pack_metadata(project_root=site)
        if Path(row.pack.root).resolve().is_relative_to((site / "astrid").resolve())]
contract = ImportContract(
    facade_exports=frozenset(pack_api.__all__), facade_version=PACK_API_VERSION,
    sdk_names=frozenset(sdk.__all__),
    # The production loader's contract: core packs publish astrid.packs.<id>.api.
    core_pack_apis={row.id: f"astrid.packs.{row.id}.api" for row in rows
                    if (Path(row.pack.root) / "api.py").is_file()},
)
report = {}
for row in rows:
    manifest = row.pack.to_dict()
    manifest["schema_version"] = int(row.pack.schema_version)
    report[row.id] = [[v.path, v.line, v.module, v.code] for v in
                      check_pack_imports(Path(row.pack.root), contract=contract, manifest=manifest)]
(root / "imports.json").write_text(json.dumps(report))
