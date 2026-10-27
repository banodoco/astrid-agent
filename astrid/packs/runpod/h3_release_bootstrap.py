"""Package the H3 helper's exact stdlib-only source closure for cold targets.

This is transport packaging, not an installer or another downloader. All module
bodies come from the controller's product package, including A1's real contracts.
The temporary package is private to one isolated target process and is removed
after provisioning. No installed Astrid or third-party target package is needed.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
from pathlib import Path
import zipfile

import runtime_protocol

from astrid import __version__

_SOURCES = (
    "__init__.py",
    "version.py",
    "core/__init__.py",
    "core/generation/__init__.py",
    "core/generation/engine_contracts.py",
    "core/generation/model_downloader.py",
    "packs/__init__.py",
    "packs/runpod/h3_release_bootstrap.py",
    "packs/runpod/h3_release_remote.py",
)

# Complete import/call closure of platform.lock_file(fd, blocking=False) on the
# Linux cold target: package initializer -> errors, platform -> cancellation,
# then stdlib fcntl. The fd is already open: file_lock(path)'s dirfd and the
# unrelated process, handoff and Windows backends are not invoked or packaged.
# Bind the controller's deadline primitive as well, so both native boundaries
# are covered by the release source identity. It imports only stdlib and is
# not invoked by the cold provisioner. Keep exact module bodies throughout.
_RUNTIME_SOURCES = ("__init__.py", "errors.py", "platform.py", "cancellation.py", "deadline.py")


def source_archive() -> bytes:
    """Return deterministic bytes from either a checkout or installed package."""
    package = Path(__file__).resolve().parents[2]
    files = {"astrid/" + name: (package / name).read_bytes() for name in _SOURCES}
    runtime = Path(runtime_protocol.__file__).resolve().parent
    files.update({"runtime_protocol/" + name: (runtime / name).read_bytes()
                  for name in _RUNTIME_SOURCES})
    # astrid.version uses distribution metadata. Supply it without depending on
    # a checkout's pyproject.toml or unrelated installed target distributions.
    files[f"astrid-{__version__}.dist-info/METADATA"] = (
        "Metadata-Version: 2.1\nName: astrid\nVersion: " + __version__ + "\n"
    ).encode()
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name, data in sorted(files.items()):
            entry = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            entry.create_system = 3
            entry.external_attr = 0o100644 << 16
            # Exact Runtime module bodies enlarge the closure. Compress it so
            # the isolated wrapper still fits a single native argv element.
            archive.writestr(entry, data, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    return output.getvalue()


def release_digest(recipe, lock, archive: bytes) -> str:
    """Keep the old identity vocabulary, binding every delivered source byte."""
    identity = {"recipe": recipe, "lock": lock,
                "provisioner_sha256": hashlib.sha256(archive).hexdigest()}
    return hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def command_source(request, archive: bytes) -> str:
    """Build the source passed to python -I -S by the existing transport."""
    expected = release_digest(request["recipe"], request["lock"], archive)
    if request["recipe_sha256"] != expected or request["release_id"] != "h3-cu130-" + expected:
        raise ValueError("H3 provisioning source identity changed; recompute release inputs")
    return (
        "import base64,hashlib,io,json,sys,tempfile,zipfile\n"
        "from pathlib import Path\n"
        "sys.dont_write_bytecode=True\n"
        f"payload=base64.b64decode({base64.b64encode(archive).decode()!r},validate=True)\n"
        f"if hashlib.sha256(payload).hexdigest() != {hashlib.sha256(archive).hexdigest()!r}:\n"
        "    raise RuntimeError('H3 provisioning package digest mismatch')\n"
        f"request=json.loads(base64.b64decode({base64.b64encode(json.dumps(request).encode()).decode()!r}))\n"
        "with tempfile.TemporaryDirectory(prefix='astrid-h3-helper-') as package:\n"
        "    with zipfile.ZipFile(io.BytesIO(payload)) as archive:\n"
        "        archive.extractall(package)\n"
        "    sys.path.insert(0,package)\n"
        "    from astrid.packs.runpod import h3_release_bootstrap as bootstrap\n"
        "    identity=bootstrap.release_digest(request['recipe'],request['lock'],payload)\n"
        "    if identity != request['recipe_sha256'] or request['release_id'] != 'h3-cu130-'+identity:\n"
        "        raise RuntimeError('H3 release source identity mismatch')\n"
        "    from astrid.packs.runpod import h3_release_remote as remote\n"
        "    result=remote.provision(request)\n"
        "    result['manifest_base64']=base64.b64encode((Path(request['root'])/'release-manifest.json').read_bytes()).decode()\n"
        "    print(json.dumps(result))\n"
    )
