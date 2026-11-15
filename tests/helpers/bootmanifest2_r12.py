"""Run the retained release's real composition/provenance code without a network."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

R12_COMMIT = "0e1543e0da2215db9cb3320cfd1083174c3c62a9"
LEGACY_FIXTURE = Path(__file__).parents[1] / "fixtures/boot_manifest/r9-stamp.json"


@pytest.fixture(scope="session")
def r12_export(tmp_path_factory):
    destination = tmp_path_factory.mktemp("bootmanifest2-r12")
    archive = destination / "r12.tar"
    with archive.open("wb") as stream:
        subprocess.run(["git", "archive", R12_COMMIT], cwd=LEGACY_FIXTURE.parents[3],
                       stdout=stream, check=True)
    subprocess.run(["tar", "-xf", str(archive), "-C", str(destination)], check=True)
    archive.unlink()
    return destination


def r12_probe(export, support, *, resident_hash=None):
    script = '''
import json, sys
from pathlib import Path
from astrid.core.gateway.dispatch import compose_profile_handoff
from astrid.core._shared.boot_manifest import build_manifest
from astrid.core.gateway.boot_conformance import VIBE_PROFILE_REGISTRY, vibe_profile_specs
support = Path(sys.argv[1])
path = support / 'astrid-host' / 'boot-manifest.json'
before = path.read_bytes() if path.exists() else None
try:
    if sys.argv[2]:
        from astrid.core.execution.generic_host import GenericPackHost
        host = GenericPackHost(pack_roots=[support], boot_manifest_path=path,
                               boot_manifest_hash=sys.argv[2])
        result = host.boot_manifest_provenance()
        equals_live = result['sha256'] == sys.argv[2]
    else:
        result = compose_profile_handoff(path, support_root=support)
        live = build_manifest(registry=VIBE_PROFILE_REGISTRY, fixtures=vibe_profile_specs())
        equals_live = result['manifest'] == live
    print(json.dumps({'outcome': 'PASS', 'equals_live': equals_live,
                      'unchanged': path.read_bytes() == before, 'created': before is None}))
except Exception as exc:
    print(json.dumps({'outcome': type(exc).__name__, 'message': str(exc)}))
'''
    completed = subprocess.run(
        ["unshare", "-n", "env", "-i", "PATH=/usr/bin:/bin", f"HOME={support}",
         "PYTHONDONTWRITEBYTECODE=1", f"PYTHONPATH={export}", sys.executable,
         "-c", script, str(support), resident_hash or ""],
        cwd=export, capture_output=True, text=True, check=True,
    )
    return json.loads(completed.stdout)
