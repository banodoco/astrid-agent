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


def strict_json_object(text):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON field")
            result[key] = value
        return result
    value = json.loads(text, object_pairs_hook=unique)
    if not isinstance(value, dict):
        raise ValueError("expected JSON object")
    return value


def validate_provider_mapping(provider, *, pod_id, volume_id, nonce, mount):
    """Validate a fresh, exact-pod typed observation; never infer a mount source."""
    if (not isinstance(provider, dict)
            or set(provider) != {"schema", "pod_id", "nonce", "volume_mounts", "legacy_evidence"}
            or provider["schema"] != "runpod.provider-mounts.v1"
            or not isinstance(pod_id, str) or not re.fullmatch(r"[a-z0-9]+", pod_id)
            or not isinstance(volume_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", volume_id)
            or not isinstance(nonce, str) or not re.fullmatch(r"[0-9a-f]{32}", nonce)
            or provider["pod_id"] != pod_id or provider["nonce"] != nonce):
        raise ValueError("provider mapping is absent or cannot join this observation")
    legacy = provider["legacy_evidence"]
    if (not isinstance(legacy, dict) or set(legacy) != {"present", "valid", "conflict"}
            or any(type(value) is not bool for value in legacy.values())
            or not legacy["valid"] or legacy["conflict"]):
        raise ValueError("provider legacy evidence conflicts or is malformed")
    mounts = provider["volume_mounts"]
    if not isinstance(mounts, list) or not 1 <= len(mounts) <= 16:
        raise ValueError("provider mounts are absent or ambiguous")
    paths, identities = set(), set()
    for item in mounts:
        if (not isinstance(item, dict)
                or set(item) != {"volume_id", "volume_type", "mount_path", "read_only"}
                or not isinstance(item["volume_id"], str)
                or not re.fullmatch(r"[A-Za-z0-9_-]+", item["volume_id"])
                or not isinstance(item["volume_type"], str)
                or item["volume_type"] not in {"NETWORK_VOLUME", "OBJECT_STORE_VOLUME"}
                or type(item["read_only"]) is not bool
                or not isinstance(item["mount_path"], str)
                or not item["mount_path"].startswith("/")
                or "\\" in item["mount_path"]
                or str(Path(item["mount_path"])) != item["mount_path"]
                or ".." in Path(item["mount_path"]).parts
                or any(ord(char) < 32 or ord(char) == 127 for char in item["mount_path"])):
            raise ValueError("provider mount is malformed")
        identity = (item["volume_type"], item["volume_id"])
        if item["mount_path"] in paths or identity in identities:
            raise ValueError("provider mount is duplicated")
        paths.add(item["mount_path"])
        identities.add(identity)
    selected = [item for item in mounts if item["mount_path"] == mount]
    if (len(selected) != 1 or selected[0]["volume_id"] != volume_id
            or selected[0]["volume_type"] != "NETWORK_VOLUME" or selected[0]["read_only"] is not False):
        raise ValueError("provider mapping does not establish the requested writable volume")


def storage_probe(request, *, mountinfo=None, diagnostic=None):
    # Only fixed labels and a boolean leave the probe; mount rows remain private.
    observation = {"schema": DIAGNOSTIC_SCHEMA, "stage": "unknown", "reason": "error",
                   "identity_matches": None}

    def fail(stage, reason, message):
        observation.update(stage=stage, reason=reason)
        raise ValueError(message)

    try:
        if (not isinstance(request, dict)
                or set(request) != {"pod_id", "volume_id", "nonce", "mount", "model_cache_root", "engine_root",
                                    "required_volume_bytes", "required_local_bytes", "provider_mapping"}):
            fail("unknown", "error", "storage request has unexpected fields")
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
            rows.append({"mount_id": fields[0], "mount": _unescape(fields[4]), "device": fields[2],
                         "options": fields[5] if len(fields) > 5 else "",
                         "filesystem": filesystem[0] if filesystem else None,
                         "source": _unescape(filesystem[1]) if len(filesystem) > 1 else None})
        exact = [row for row in rows if row["mount"] == str(mount)]
        if malformed:
            fail("mountinfo", "malformed", "mount evidence is malformed")
        observation.update(stage="identity", reason="error")
        try:
            validate_provider_mapping(request.get("provider_mapping"), pod_id=request.get("pod_id"),
                                      volume_id=request.get("volume_id"), nonce=request.get("nonce"), mount=str(mount))
        except ValueError:
            observation["identity_matches"] = False
            fail("identity", "mismatch", "typed provider mapping does not establish requested volume")
        observation["identity_matches"] = True

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
        if (not evidence["mount_id"].isdigit() or int(evidence["mount_id"]) <= 0
                or "rw" not in evidence["options"].split(",") or "ro" in evidence["options"].split(",")):
            fail("mount", "invalid_request", "mount identity or writable policy is invalid")
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
        local_space = os.statvfs(ancestor)
        local_available = local_space.f_bavail * local_space.f_frsize
        if type(request["required_local_bytes"]) is not int or request["required_local_bytes"] <= 0:
            fail("local", "invalid_requirement", "requested local capacity must be positive")
        if local_available < request["required_local_bytes"]:
            fail("local", "insufficient", "private engine filesystem has insufficient usable capacity")
        observation.update(stage="complete", reason="verified")
        return {**{key: value for key, value in request.items() if key != "provider_mapping"},
                "cache_device": info.st_dev, "cache_mount_id": int(evidence["mount_id"]),
                "free_bytes": available, "total_bytes": space.f_blocks * space.f_frsize,
                "local_free_bytes": local_available}
    finally:
        if diagnostic is not None:
            diagnostic(observation)


def main():
    def diagnostic(observation):
        print(DIAGNOSTIC_PREFIX + json.dumps(observation, sort_keys=True), file=sys.stderr)
    try:
        try:
            request = strict_json_object(base64.b64decode(sys.argv[1], validate=True))
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
