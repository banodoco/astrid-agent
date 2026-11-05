"""Child of the PK-12 installed pack-graph test: read the installed packs only."""
import ast
import json
import re
import sys
from pathlib import Path

root, site = map(Path, sys.argv[1:3])
import astrid
from astrid.core.pack.discovery import discover_pack_metadata

assert sys.flags.isolated and Path(astrid.__file__).is_relative_to(site)
rows = [row for row in discover_pack_metadata(project_root=site)
        if Path(row.pack.root).resolve().is_relative_to((site / "astrid").resolve())]
ids = {row.id for row in rows}
declared, references = {}, {}
module_ref = re.compile(r"astrid\.packs\.([a-z][a-z0-9_]*)")
for row in rows:
    pack = row.pack
    deps = pack.dependencies.get("packs", ()) if isinstance(pack.dependencies, dict) else ()
    declared[row.id] = sorted(deps)
    found = set()
    pack_root = Path(pack.root)
    for path in pack_root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            for name in names:
                match = module_ref.match(name)
                if match:
                    found.add(match.group(1))
    # Manifest module references load code at run time (e.g. metadata.command_builder).
    for match in module_ref.finditer((pack_root / "pack.yaml").read_text(encoding="utf-8")):
        found.add(match.group(1))
    references[row.id] = sorted(found & ids - {row.id})
(root / "graph.json").write_text(json.dumps({"declared": declared, "references": references}))
