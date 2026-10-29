#!/usr/bin/env python3
"""Read-only Linux mount/capacity evidence, executed through the claimed transport."""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import re
import stat
import sys


DIAGNOSTIC_SCHEMA = "astrid.pod-storage-diagnostic.v1"
DIAGNOSTIC_PREFIX = "ASTRID_STORAGE_DIAGNOSTIC "


def _unescape(value):
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), value)


def storage_probe(request, *, mountinfo=None, diagnostic=None):
    # Only fixed labels and a boolean leave the probe; mount rows remain private.
    observation = {"schema": DIAGNOSTIC_SCHEMA, "stage": "unknown", "reason": "error",
                   "identity_matches": None}

    def fail(stage, reason, message):
        observation.update(stage=stage, reason=reason)
        raise ValueError(message)

    try:
        observation.update(stage="mountinfo", reason="error")
        mount = Path(request["mount"])
        content = Path("/proc/self/mountinfo").read_text() if mountinfo is None else mountinfo
        rows, malformed = [], False
        for line in content.splitlines():
            left, separator, right = line.partition(" - ")
            fields, filesystem = left.split(), right.split()
            if not separator or len(fields) < 6 or len(filesystem) < 3:
                malformed = True
            if len(fields) < 5:
                continue
            rows.append({"mount": _unescape(fields[4]), "device": fields[2],
                         "filesystem": filesystem[0] if filesystem else None,
                         "source": _unescape(filesystem[1]) if len(filesystem) > 1 else None})
        exact = [row for row in rows if row["mount"] == str(mount)]
        if malformed:
            fail("mountinfo", "malformed", "mount evidence is malformed")
        volume_id = request.get("volume_id")
        if (len(exact) == 1 and isinstance(exact[0]["source"], str)
                and isinstance(volume_id, str) and volume_id):
            # Evaluate against the original source, never against a redacted form.
            observation["identity_matches"] = volume_id in re.split(r"[/:#]", exact[0]["source"])

        observation.update(stage="paths", reason="error")
        cache = Path(request["model_cache_root"])
        local = Path(request["engine_root"])
        for path in (mount, cache, local):
            if not path.is_absolute() or ".." in path.parts:
                fail("paths", "invalid", "storage paths must be absolute without traversal")
            for ancestor in (*reversed(path.parents), path):
                if ancestor.is_symlink():
                    fail("paths", "symlink", "storage path traverses a symlink")
        if mount == Path("/") or mount not in cache.parents or mount in local.parents or local == mount:
            fail("paths", "not_separate", "cache mount and private engine root must be separate")
        observation.update(stage="mount", reason="error")
        info = mount.lstat()
        if not stat.S_ISDIR(info.st_mode):
            fail("mount", "not_directory", "requested mount is not a directory")
        if len(exact) != 1:
            fail("mount", "absent_or_ambiguous", "requested volume mount is absent or ambiguous")
        evidence = exact[0]
        if evidence["device"] != f"{os.major(info.st_dev)}:{os.minor(info.st_dev)}":
            fail("mount", "device_mismatch", "mount evidence differs from selected filesystem")
        # Keep the existing literal complete-component identity boundary. An
        # opaque source that does not establish it must still fail closed.
        if observation["identity_matches"] is None:
            fail("identity", "invalid_request", "requested volume identity is invalid")
        if observation["identity_matches"] is not True:
            fail("identity", "mismatch", "mount source does not establish the requested volume identity")
        observation.update(stage="cache", reason="error")
        if any(Path(row["mount"]) == cache or mount in Path(row["mount"]).parents
               and Path(row["mount"]) in cache.parents for row in rows if row is not evidence):
            fail("cache", "nested_mount", "model cache crosses another mount")
        cursor = mount
        for component in cache.relative_to(mount).parts:
            cursor = cursor / component
            try:
                fact = cursor.lstat()
            except FileNotFoundError:
                break  # Missing cache is allowed; the pinned downloader handles it.
            if not stat.S_ISDIR(fact.st_mode) or fact.st_dev != info.st_dev:
                fail("cache", "unsafe", "cache root is unsafe or on another filesystem")
        observation.update(stage="capacity", reason="error")
        space = os.statvfs(mount)
        available = space.f_bavail * space.f_frsize
        if type(request["required_volume_bytes"]) is not int or request["required_volume_bytes"] <= 0:
            fail("capacity", "invalid_requirement", "requested usable capacity must be positive")
        if available < request["required_volume_bytes"]:
            fail("capacity", "insufficient", "mounted volume has insufficient usable capacity")
        observation.update(stage="local", reason="error")
        ancestor = local
        while not ancestor.exists():
            ancestor = ancestor.parent
        if ancestor.stat().st_dev == info.st_dev:
            fail("local", "same_filesystem", "private engine root is on the mounted cache filesystem")
        if type(request["required_local_bytes"]) is not int or request["required_local_bytes"] <= 0:
            fail("local", "invalid_requirement", "requested local capacity must be positive")
        local_space = os.statvfs(ancestor)
        local_available = local_space.f_bavail * local_space.f_frsize
        if local_available < request["required_local_bytes"]:
            fail("local", "insufficient", "private engine filesystem has insufficient usable capacity")
        observation.update(stage="complete", reason="verified")
        return {**request, "free_bytes": available, "total_bytes": space.f_blocks * space.f_frsize,
                "local_free_bytes": local_available}
    finally:
        if diagnostic is not None:
            diagnostic(observation)


def main():
    def diagnostic(observation):
        print(DIAGNOSTIC_PREFIX + json.dumps(observation, sort_keys=True), file=sys.stderr)
    try:
        try:
            request = json.loads(base64.b64decode(sys.argv[1]))
        except Exception:
            diagnostic({"schema": DIAGNOSTIC_SCHEMA, "stage": "unknown", "reason": "error",
                        "identity_matches": None})
            raise
        receipt = storage_probe(request, diagnostic=diagnostic)
        print(json.dumps(receipt, sort_keys=True))
        return 0
    except Exception:
        print("pod storage mount/identity/capacity verification failed", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
