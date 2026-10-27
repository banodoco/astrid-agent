# Frozen terminal runner from Agent 4f704d8, before X6 stamp-only consumption.
import json, os, re, sys
from pathlib import Path

root = Path(__file__).resolve().parent
name = (root / "current").read_text(encoding="utf-8").strip()
if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", name):
    raise SystemExit("Astrid's current version pointer is invalid.")
version_root = root / "versions" / name
record = json.loads((version_root / "version.json").read_text(encoding="utf-8"))
if Path(record["root"]).resolve() != version_root.resolve():
    raise SystemExit("Astrid's current version points outside its program folder.")
python = str(Path(record["python"]))
env = dict(os.environ)
env.update({str(key): str(value) for key, value in record["env"].items()})
env["ASTRID_INSTALL_PROGRAM_ROOT"] = str(root)
identity = record.get("identity")
if "identity" in record:
    if not isinstance(identity, dict):
        raise SystemExit("Astrid's selected install identity is invalid.")
else:
    manifest_path = version_root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else {}
    marker_path = root / "install.json"
    marker = json.loads(marker_path.read_text(encoding="utf-8")) if marker_path.is_file() else {}
    identity = {"release": manifest.get("release", 0),
                "managed_by": marker.get("managed_by")}
release = identity.get("release")
managed_by = identity.get("managed_by")
install_marker = root / "install.json"
if install_marker.is_file():
    marker = json.loads(install_marker.read_text(encoding="utf-8"))
    if "managed_by" in marker:
        managed_by = marker.get("managed_by")
if type(release) is not int or release < 0 or managed_by not in (None, "pinokio"):
    raise SystemExit("Astrid's selected install identity is invalid.")
env["ASTRID_INSTALL_RELEASE"] = str(release)
env["ASTRID_INSTALL_MANAGED_BY"] = managed_by or ""
if sys.argv[1:2] == ["credential"]:
    os.environ.update(env)
    from astrid.core.util.credential_store import main
    raise SystemExit(main(sys.argv[2:]))
replace_process([python, "-X", "utf8", "-I", "-m", "astrid.omp_agent", *sys.argv[1:]], env)
