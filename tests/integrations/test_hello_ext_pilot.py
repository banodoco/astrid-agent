"""PK-6: a pilot extension pack loads from outside core.

``hello_ext`` lives only in the user's pack collection (``~/Astrid/packs``), never under
``astrid/packs``. A fresh interpreter discovers it through the one discovery path, imports its
code through the ``astrid.packs`` namespace from that folder, and the generic host admits its
action; deleting the folder removes it while core packs keep loading.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CORE_PACKS = {"vibecomfy", "generation", "rendering", "timeline"}

PROBE = r"""
import json, sys
from pathlib import Path
from astrid.core.pack.discovery import discover_pack_metadata
from astrid.core.execution.generic_host import GenericPackHost
collection = Path(sys.argv[1])
found = {item.pack.id: str(item.pack.root) for item in discover_pack_metadata(project_root=collection)}
payload = {"discovered": found}
if "hello_ext" in found:
    import astrid.packs.hello_ext.greeting as greeting
    payload["module"] = greeting.__file__
    payload["value"] = greeting.MESSAGE
    host = GenericPackHost(pack_roots=[collection])
    host.discover()
    record = host.capabilities.get("hello_ext.greet")
    payload["capability_root"] = str(record.source_root) if record is not None else None
print(json.dumps(payload))
"""


def _write_hello_ext(collection: Path) -> Path:
    pack = collection / "hello_ext"
    pack.mkdir(parents=True)
    (pack / "__init__.py").write_text("", encoding="utf-8")
    (pack / "greeting.py").write_text("MESSAGE = 'hello from outside core'\n", encoding="utf-8")
    manifest = {
        "schema_version": 3, "id": "hello_ext", "name": "Hello extension", "version": "1.0.0",
        "pack_api": "1.0", "documentation": {"kind": "none", "reason": "PK-6 pilot fixture."},
        "actions": {"greet": {
            "description": "Print a greeting", "short_description": "Print a greeting",
            "invocation": {"kind": "command", "command": {"argv": ["echo", "hello"]}},
            "inputs": [], "outputs": [],
        }},
    }
    (pack / "pack.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
    return pack


def _probe(home: Path, collection: Path) -> dict:
    env = os.environ.copy()
    env["HOME"] = str(home)
    env.pop("ASTRID_PACKS_PATH", None)
    env["PYTHONPATH"] = os.pathsep.join((str(REPO_ROOT), env["ASTRID_RUNTIME_CHECKOUT"]))
    result = subprocess.run([sys.executable, "-c", PROBE, str(collection)], cwd=home, env=env,
                            text=True, capture_output=True, timeout=180, check=False)
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-3000:]
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_pilot_extension_loads_from_outside_core_and_leaves_with_its_folder(tmp_path: Path) -> None:
    home = tmp_path / "home"
    collection = home / "Astrid" / "packs"
    pack = _write_hello_ext(collection)
    core = (REPO_ROOT / "astrid").resolve()

    loaded = _probe(home, collection)
    assert Path(loaded["discovered"]["hello_ext"]).resolve() == pack.resolve()
    assert not Path(loaded["discovered"]["hello_ext"]).resolve().is_relative_to(core)
    assert CORE_PACKS <= set(loaded["discovered"])
    assert Path(loaded["module"]).resolve() == (pack / "greeting.py").resolve()
    assert loaded["value"] == "hello from outside core"
    assert loaded["capability_root"] is not None
    assert Path(loaded["capability_root"]).resolve().is_relative_to(pack.resolve())

    shutil.rmtree(pack)
    removed = _probe(home, collection)
    assert "hello_ext" not in removed["discovered"]
    assert CORE_PACKS <= set(removed["discovered"])
