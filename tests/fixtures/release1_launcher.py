"""The Release 1 R1 launcher script, verbatim, for field-state fixtures.

Copied from ``stage_runtime_launcher`` in ``astrid/installer/boundary_registration.py``
at Agent commit 7247ea921098eb54ab9b7643977e8443d04a42f8 (the Release 1 candidate
Hannah's handoff staged). That launcher admits only the handoff's own records
(``r1_data_root`` present, ``marker.r1_data_root == data``), so a normalisation
that rewrites the records without first replacing it would stop R1 starting.
"""
from __future__ import annotations

import sys
from pathlib import Path

from astrid.installer.core import absolute
from astrid.installer.service import SERVICE_SETTINGS, install_selection

AGENT_COMMIT = "7247ea921098eb54ab9b7643977e8443d04a42f8"
R1_LINUX = "astrid-r1.service"
R1_MAC = "haus.astrid.runtime.r1"


def release1_launcher_script(version) -> str:
    program = absolute(version.root).parent.parent
    script = f'''import json, os, re, sys
from pathlib import Path
root = Path({str(program)!r})
version = {version.document()!r}
args = sys.argv[1:]
selection = json.loads(Path({str(install_selection(Path.home(), sys.platform))!r}).read_text(encoding="utf-8"))
marker = json.loads((root / "install.json").read_text(encoding="utf-8"))
data = selection.get("r1_data_root")
if (selection.get("owner") != "haus.astrid.runtime" or selection.get("program_root") != str(root)
        or not isinstance(data, str) or not Path(data).is_absolute()
        or not isinstance(selection.get("data_root"), str) or data != selection["data_root"] + "-r1"
        or marker.get("owner") != "haus.astrid.runtime"
        or marker.get("data_root") != selection["data_root"]
        or marker.get("r1_data_root") != data
        or not isinstance(selection.get("realm_id"), str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{{0,63}}", selection["realm_id"])
        or selection.get("r1_realm_root") != str(Path(data) / "runtime/realms" / selection["realm_id"])
        or not isinstance(selection.get("realm_root"), str)
        or not Path(selection["realm_root"]).is_relative_to(Path(selection["data_root"]))
        or marker.get("identity") != version["identity"]
        or selection.get("identity") != version["identity"]
        or selection.get("service_label") != {R1_LINUX if sys.platform == 'linux' else R1_MAC!r}
        or args != ["run", "--profile", "astrid", "--data-root", data]
        or (root / "current").read_text(encoding="utf-8").rstrip("\\n") != Path(version["root"]).name):
    raise SystemExit("R1 adoption and selection have not been committed; run the install line again.")
settings_path = root / "service-env.json"
settings = json.loads(settings_path.read_text(encoding="utf-8")) if settings_path.exists() else {{}}
if (not isinstance(settings, dict) or set(settings) - set({sorted(SERVICE_SETTINGS)!r})
        or any(not isinstance(v, str) for v in settings.values())):
    raise SystemExit("Invalid Astrid service-env.json.")
for key in ("PYTHONPATH", "PYTHONHOME", "ASTRID_WORKSPACE_ENDPOINT", "ASTRID_WORKSPACE_CREDENTIAL",
            "ASTRID_RUNTIME_SOURCE_MANIFEST", "ASTRID_RUNTIME_DATA_ROOT", "ASTRID_PACKS_PATH",
            "ASTRID_INSTALLER_OPERATION_ID", "ASTRID_TIMELINE_SCHEMA_PYTHONPATH"):
    os.environ.pop(key, None)
os.environ.update(version["env"])
os.environ.update(settings)
os.environ["ASTRID_RUNTIME_DATA_ROOT"] = data
os.environ["ASTRID_RUNTIME_SOURCE_MANIFEST"] = version["profile"]
os.environ["ASTRID_INSTALL_PROGRAM_ROOT"] = str(root)
from astrid_runtime.cli import main
raise SystemExit(main([*args, "--source-manifest", version["profile"]]))
'''
    return script


def install_release1_launcher(version) -> None:
    """Put the Release 1 launcher script back in place of any regenerated one."""
    path = Path(version.root) / "bin" / "runtime.py"
    path.write_text(release1_launcher_script(version), encoding="utf-8")
    path.chmod(0o600)
