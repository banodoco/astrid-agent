"""Admission of the one reviewed identityless Release-0 predecessor.

The frozen values are from tests/upgrade/releases.json's served legacy schema;
that test-only manifest is not shipped with an installed Agent.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

MANIFEST_SHA256 = "4610f44b4dfe39d932753115668779390ed2287d15e2bd8c9568e62bbb6d60ff"
AGENT_SHA = "3204bb00135bec8ee2d2d3fb21eed5abfceca649"
RUNTIME_SHA = "6d53a19070faf63fc15ae1f81417722edc2c8a5f"
SCHEMA_DIGEST = "sha256:9061d4982c786e04d99e45470b8ea4f3d9c9ff9855c4a2f83d751a93408c205b"
PROTOCOL = "workspace.v1"


def _contract(version):
    script = (
        "import importlib.util,json,sys; "
        "from runtime_protocol.store import SCHEMA_VERSION; "
        "from astrid_runtime_client.contract_metadata import PROTOCOL,SCHEMA_DIGEST; "
        "print(json.dumps({'python':sys.executable,'python_version':'.'.join(map(str,sys.version_info[:3])),"
        "'workspace_schema_version':SCHEMA_VERSION,'protocol':PROTOCOL,'schema_digest':SCHEMA_DIGEST,"
        "**{n:importlib.util.find_spec(n).origin for n in ('astrid','astrid_runtime','runtime_protocol','astrid_runtime_client')}}))"
    )
    result = subprocess.run([str(version.python), "-I", "-c", script], check=True,
                            capture_output=True, text=True, timeout=30,
                            env={**os.environ, **version.env}, cwd=str(version.root))
    return json.loads(result.stdout)


def _installed_python(python, root):
    if not python.is_absolute() or not python.is_file() or not python.is_relative_to(root):
        return False
    target = python.resolve()
    if target.is_relative_to(root):
        return True
    if sys.platform == "win32":
        bootstrap = Path(os.environ["LOCALAPPDATA"]) / "Astrid Installer" / "bootstrap" / "python"
    else:
        home = Path.home()
        data = (Path(os.environ.get("XDG_DATA_HOME", str(home / ".local/share")))
                if sys.platform != "darwin" else home / ".local/share")
        bootstrap = data / "astrid" / "bootstrap" / "python"
    try:
        parts = target.relative_to(bootstrap.resolve()).parts
    except ValueError:
        return False
    return (len(parts) >= 2 and parts[0].startswith("cpython-3.11.13-")
            and (len(parts) == 3 and parts[1:] == ("bin", "python3.11")
                 or len(parts) == 2 and parts[1].lower() == "python.exe"))


def admitted(version, health, discovery, *, program_root):
    """Return true only for the exact selected, live, direct-installed tuple."""
    if version is None or version.release != 0 or version.managed_by is not None:
        return False
    if not isinstance(health, Mapping) or not isinstance(discovery, Mapping):
        return False
    if health.get("identity") is not None or discovery.get("identity") is not None:
        return False
    instance = discovery.get("runtime_instance_id")
    if (not isinstance(instance, str) or not instance.strip()
            or health.get("runtime_instance_id") != instance
            or health.get("status") != "ok" or health.get("protocol") != PROTOCOL
            or health.get("schema_digest") != SCHEMA_DIGEST):
        return False
    try:
        root = version.root
        if (root.parent != program_root / "versions" or not root.name.startswith("3204bb00-6d53a190-")
                or (program_root / "current").read_text(encoding="utf-8").strip() != root.name):
            return False
        marker = json.loads((program_root / "install.json").read_text(encoding="utf-8"))
        if marker.get("owner") != "haus.astrid.runtime" or marker.get("managed_by") is not None:
            return False
        record = json.loads((root / "version.json").read_text(encoding="utf-8"))
        if (set(record) != {"root", "python", "profile", "env"}
                or Path(record["root"]).resolve() != root
                or Path(record["python"]) != version.python or Path(record["profile"]) != version.profile
                or not isinstance(record["env"], dict)
                or any(not isinstance(k, str) or not isinstance(v, str) for k, v in record["env"].items())):
            return False
        raw = (root / "manifest.json").read_bytes()
        manifest = json.loads(raw)
        if (hashlib.sha256(raw).hexdigest() != MANIFEST_SHA256
                or raw != (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
                or manifest.get("schema_version") != 1 or manifest.get("release", 0) != 0
                or manifest["agent"]["sha"] != AGENT_SHA or manifest["runtime"]["sha"] != RUNTIME_SHA
                or manifest["runtime"]["schema_digest"] != SCHEMA_DIGEST
                or manifest["tools"]["python"]["version"] != "3.11.13"):
            return False
        if (not _installed_python(version.python, root)
                or not version.profile.is_file() or not version.profile.resolve().is_relative_to(root)):
            return False
        profile = json.loads(version.profile.read_text(encoding="utf-8"))
        for key, name in (("runtime_checkout", "r"), ("runtime_environment", "py")):
            path = root / name
            if Path(profile[key]).absolute() != path or path.is_symlink() or not path.is_dir():
                return False
        for key in ("source_checkout", "service_declaration"):
            path = Path(profile[key]).resolve()
            if not path.is_relative_to(root) or (not path.is_dir() if key == "source_checkout" else not path.is_file()):
                return False
        contract = _contract(version)
        if (contract.get("python") != str(version.python) or contract.get("python_version") != "3.11.13"
                or contract.get("protocol") != PROTOCOL or contract.get("schema_digest") != SCHEMA_DIGEST
                or type(contract.get("workspace_schema_version")) is not int
                or contract["workspace_schema_version"] != 31):
            return False
        for name in ("astrid", "astrid_runtime", "runtime_protocol", "astrid_runtime_client"):
            if not Path(contract[name]).resolve().is_relative_to(root):
                return False
        return True
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError):
        return False
