"""Independent read-only observations for Runtime's offline-copy receipt v1.

Never open SQLite on the source. Hash the complete namespace, including opaque
support files, without interpreting legacy process/discovery records.
"""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
import os
import re
import stat

from .core import absolute, read_json

RECEIPT = "adopt-receipt.r1.json"


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns)


def observe(root):
    """Return the documented full-tree and SQLite digests plus root identity."""
    root = absolute(root)
    rows = []

    def walk(fd, relative):
        before = os.fstat(fd)
        rows.append([relative, "dir", stat.S_IMODE(before.st_mode), before.st_size,
                     before.st_mtime_ns, None])
        for name in sorted(os.listdir(fd)):
            name.encode("utf-8", "strict")
            if name in {".", ".."} or "/" in name or "\\" in name:
                raise ValueError("Unsafe source inventory name.")
            path = relative + "/" + name if relative else name
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            directory = stat.S_ISDIR(info.st_mode)
            if not directory and not stat.S_ISREG(info.st_mode):
                raise ValueError("Unsafe source inventory entry: " + path)
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
            if directory:
                flags |= os.O_DIRECTORY
            child = os.open(name, flags, dir_fd=fd)
            try:
                if _identity(info) != _identity(os.fstat(child)):
                    raise ValueError("Source changed during observation.")
                if directory:
                    walk(child, path)
                else:
                    digest = hashlib.sha256()
                    while block := os.read(child, 1024 * 1024):
                        digest.update(block)
                    rows.append([path, "file", stat.S_IMODE(info.st_mode), info.st_size,
                                 info.st_mtime_ns, digest.hexdigest()])
                if _identity(info) != _identity(os.fstat(child)):
                    raise ValueError("Source changed during observation.")
                if _identity(info) != _identity(os.stat(name, dir_fd=fd, follow_symlinks=False)):
                    raise ValueError("Source entry replaced during observation.")
            finally:
                os.close(child)
        if _identity(before) != _identity(os.fstat(fd)):
            raise ValueError("Source changed during observation.")

    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        walk(fd, "")
        if _identity(info) != _identity(root.lstat()):
            raise ValueError("Source root replaced during observation.")
    finally:
        os.close(fd)
    rows.sort(key=lambda row: row[0].encode("utf-8"))
    encode = lambda value: json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
    endings = tuple(ext + suffix for ext in (".sqlite", ".sqlite3", ".db")
                    for suffix in ("", "-wal", "-shm", "-journal"))
    sqlite = [row for row in rows if row[1] == "file" and row[0].endswith(endings)]
    return {"device": info.st_dev, "inode": info.st_ino,
            "inventory": hashlib.sha256(b"astrid-full-root-inventory-v1\n" + encode(rows)).hexdigest(),
            "sqlite": hashlib.sha256(encode(sqlite)).hexdigest()}


def validate(result, before, after, *, data, realm, realm_id, destination, profile):
    """A successful process response AND its durable receipt must agree."""
    from .boundary_registration import _regular

    receipt = read_json(_regular(destination / RECEIPT))
    expected = {"ok": True, "source_unchanged": True, "started": False,
                "receipt_version": 1, "realm_id": realm_id, "source": str(realm),
                "destination": str(destination / "runtime/realms" / realm_id),
                "source_data_root": str(data), "source_root_device": before["device"],
                "source_root_inode": before["inode"], "source_inventory_sha256": before["inventory"],
                "post_inventory_sha256": before["inventory"],
                "destination_inventory_sha256": before["inventory"],
                "sqlite_digest_sha256": before["sqlite"],
                "source_manifest_sha256": hashlib.sha256(_regular(profile).read_bytes()).hexdigest()}
    if (before != after or result != receipt
            or any(type(result.get(k)) is not type(v) or result.get(k) != v for k, v in expected.items())):
        raise ValueError("Offline adoption receipt or independent source observations disagree; both roots were kept.")
    if (not re.fullmatch(r"[0-9a-f]{32}", str(result.get("operation_id", "")))
            or any(type(result.get(k)) is not int or result[k] < 0 for k in ("source_version", "target_version"))
            or result["target_version"] < result["source_version"]
            or not isinstance(result.get("migration"), dict)):
        raise ValueError("Invalid offline adoption receipt identity or schema.")
    verified = datetime.fromisoformat(result["verified_at"])
    if verified.utcoffset() is None or verified.utcoffset().total_seconds() != 0:
        raise ValueError("Offline adoption receipt has no UTC verification time.")
    catalog = read_json(_regular(destination / "runtime/catalog.json"))
    matches = [row for row in catalog.get("realms", []) if row.get("realm_id") == realm_id]
    if (catalog.get("version") != 1 or catalog.get("selected_realm_id") != realm_id
            or len(catalog.get("realms", [])) != 1 or len(matches) != 1
            or matches[0].get("data_root") != expected["destination"]):
        raise ValueError("Adopted catalog does not select the receipted R1 realm.")
    return receipt
