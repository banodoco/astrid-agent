"""Validate the install marker's handoff roots before journey observations."""
from pathlib import Path
import json
import os


def canonical(value):
    if not isinstance(value, (str, os.PathLike)):
        raise ValueError("Invalid data root")
    raw = os.fspath(value)
    path = Path(raw)
    if (not path.is_absolute() or raw != str(path) or path != path.resolve(strict=False)):
        raise ValueError("Data root must be an absolute canonical path without aliases")
    return path


def selection(root, requested):
    root = canonical(root)
    requested = canonical(requested)
    marker_path = root / "install.json"
    if marker_path.is_symlink():
        raise ValueError("Install marker cannot be a symlink")
    if not marker_path.is_file():
        return requested, requested
    marker = json.loads(marker_path.read_text(encoding="utf-8-sig"))
    retained = marker.get("retained_r0_data_root")
    if retained is not None:
        # A normalised storage move (UR-15): R1 is the data root, R0 is provenance.
        r0, r1 = canonical(retained), canonical(marker["data_root"])
        if "r1_data_root" in marker or r1 != Path(str(r0) + "-r1") or requested not in (r0, r1):
            raise ValueError("Requested data root does not match the normalised R1 handoff")
        return r0, r1
    recorded = canonical(marker["data_root"])
    r1_value = marker.get("r1_data_root")
    if r1_value is None:
        if requested != recorded:
            raise ValueError("Requested data root does not match install marker")
        return recorded, recorded
    r1 = canonical(r1_value)
    if r1 != Path(str(recorded) + "-r1") or requested not in (recorded, r1):
        raise ValueError("Requested data root does not match the owned R1 handoff")
    return recorded, r1
