"""CAS-install owned D73 rows without touching other suites."""

from __future__ import annotations

import datetime
import fcntl
import hashlib
import os
import stat
import sys
import tempfile
from pathlib import Path


candidate, registry, evidence = map(Path, sys.argv[1:4])
expected = sys.argv[4]
requested_name = sys.argv[5].encode() if len(sys.argv) == 6 else None
target_keys = (
    {
        (b"agent", b"p4-current-becfe-http-3legs"),
        (b"agent", b"p4-current-single"),
        (b"agent", b"final-core"),
    }
    if requested_name is None
    else {(b"agent", requested_name)}
)
rows = {
    tuple(line.split()[:2]): line
    for line in candidate.read_bytes().splitlines(keepends=True)
    if line.startswith(b"agent ")
}
assert target_keys <= set(rows)
assert registry.is_file() and not registry.is_symlink()
assert stat.S_IMODE(registry.stat().st_mode) == 0o600
evidence.mkdir(parents=True, exist_ok=True)

with registry.open("rb") as locked:
    fcntl.flock(locked.fileno(), fcntl.LOCK_EX)
    before = locked.read()
    assert hashlib.sha256(before).hexdigest() == expected
    lines = before.splitlines(keepends=True)
    found: set[tuple[bytes, bytes]] = set()
    after_lines: list[bytes] = []
    for line in lines:
        key = tuple(line.split()[:2])
        if key in target_keys:
            assert key not in found
            line = rows[key]
            found.add(key)
        after_lines.append(line)
    for key in sorted(target_keys - found):
        after_lines.append(rows[key])
        found.add(key)
    assert found == target_keys

    stamp = datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y%m%dT%H%M%S.%fZ"
    )
    backup = evidence / f"suites.conf.pre-{stamp}.bak"
    with backup.open("xb") as output:
        os.fchmod(output.fileno(), 0o600)
        output.write(before)
        output.flush()
        os.fsync(output.fileno())

    after = b"".join(after_lines)
    fd, temporary = tempfile.mkstemp(
        prefix=".suites.conf.d73-launch-", dir=registry.parent
    )
    with os.fdopen(fd, "wb") as output:
        os.fchmod(output.fileno(), 0o600)
        output.write(after)
        output.flush()
        os.fsync(output.fileno())
    assert registry.read_bytes() == before
    os.replace(temporary, registry)
    directory_fd = os.open(registry.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)

    assert registry.read_bytes() == after
    assert stat.S_IMODE(registry.stat().st_mode) == 0o600

    def outside(entries: list[bytes]) -> bytes:
        return b"".join(
            line for line in entries if tuple(line.split()[:2]) not in target_keys
        )

    assert outside(lines) == outside(after_lines)
    print("backup", backup)
    print("before", expected)
    print("after", hashlib.sha256(after).hexdigest())
    print("outside", hashlib.sha256(outside(lines)).hexdigest(), "byte-identical")
    print("mode 0600; only the requested owned D73 rows changed")
