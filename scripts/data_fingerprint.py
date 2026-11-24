#!/usr/bin/env python3
"""Fingerprint an Astrid data root so "no project data lost" is a comparison, not a belief (items 2a, 2e).

  python3 -I data_fingerprint.py snap DATA_ROOT --out before.json [--realm-glob 'realms/*'] [--exclude GLOB ...]
  python3 -I data_fingerprint.py diff before.json after.json [--allow-new GLOB ...]

Take the snapshot only with the stack STOPPED (a live SQLite store changes under you); `snap` reads each file
twice and refuses a tree that changed while it was read. Records, for every regular file under the root: relative
path, size, sha256. Symlinks and non-regular entries are an error. Skipped by default: owner.lock (a live lock
file, recorded by presence and size only), *.log, tmp/ and cache/ directories. The realm-ID list is the names
matched by --realm-glob (default `realms/*`, one directory per realm: confirm the layout on the build under test
and pass the right glob; an empty list is reported, never silently accepted).

`diff` exits 1 if a realm id disappeared, a file present before is absent or has different bytes after, or a new
file appears that --allow-new does not name; new files under an update are listed so a reader can judge them.
Exit 2 on bad input.
"""
import argparse
import fnmatch
import hashlib
import json
import os
import stat
import sys
from pathlib import Path

DEFAULT_EXCLUDES = ["*.log", "tmp/*", "cache/*", "*/tmp/*", "*/cache/*"]


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def walk(root, excludes):
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames) + sorted(dirnames):
            p = Path(dirpath) / name
            rel = p.relative_to(root).as_posix()
            st = p.lstat()
            if stat.S_ISLNK(st.st_mode):
                raise SystemExit(f"bad input: symlink at {rel}")
            if stat.S_ISDIR(st.st_mode):
                continue
            if not stat.S_ISREG(st.st_mode):
                raise SystemExit(f"bad input: non-regular entry at {rel}")
            if name == "owner.lock":
                out[rel] = {"size": st.st_size, "sha256": None, "live": True}
            elif not any(fnmatch.fnmatch(rel, g) for g in excludes):
                out[rel] = {"size": st.st_size, "sha256": sha256(p)}
    return out


def snap(root, realm_glob, excludes):
    root = Path(root).resolve()
    if not root.is_dir():
        raise SystemExit(f"bad input: {root} is not a directory")
    first = walk(root, excludes)
    second = walk(root, excludes)
    if first != second:
        raise SystemExit("refused: the tree changed while it was read (is the stack stopped?)")
    realms = sorted(p.name for p in root.glob(realm_glob) if p.is_dir())
    return {"root": str(root), "realm_glob": realm_glob, "realms": realms, "files": first}


def diff(before, after, allow_new):
    b, a = json.load(open(before)), json.load(open(after))
    problems, notes = [], []
    if not b["realms"]:
        problems.append(f"the before snapshot found no realm ids with {b['realm_glob']!r}: wrong glob, so realm survival is unproven")
    for r in sorted(set(b["realms"]) - set(a["realms"])):
        problems.append(f"realm id lost: {r}")
    for r in sorted(set(a["realms"]) - set(b["realms"])):
        notes.append(f"realm id added: {r}")
    for rel, meta in sorted(b["files"].items()):
        got = a["files"].get(rel)
        if got is None:
            problems.append(f"file lost: {rel}")
        elif not meta.get("live") and got["sha256"] != meta["sha256"]:
            problems.append(f"bytes changed: {rel} ({meta['size']} -> {got['size']} bytes)")
    for rel in sorted(set(a["files"]) - set(b["files"])):
        (notes if any(fnmatch.fnmatch(rel, g) for g in allow_new) else problems).append(f"new file: {rel}")
    print(f"before: {len(b['files'])} files, {len(b['realms'])} realms; after: {len(a['files'])} files, {len(a['realms'])} realms")
    for n in notes:
        print("note:", n)
    for p in problems:
        print("PROBLEM:", p)
    return 1 if problems else 0


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("snap")
    s.add_argument("root")
    s.add_argument("--out", required=True)
    s.add_argument("--realm-glob", default="realms/*")
    s.add_argument("--exclude", action="append", default=[])
    d = sub.add_parser("diff")
    d.add_argument("before")
    d.add_argument("after")
    d.add_argument("--allow-new", action="append", default=[])
    a = ap.parse_args(argv[1:])
    if a.cmd == "snap":
        res = snap(a.root, a.realm_glob, DEFAULT_EXCLUDES + a.exclude)
        json.dump(res, open(a.out, "w"), indent=1, sort_keys=True)
        print(f"{len(res['files'])} files, realms {res['realms'] or 'NONE FOUND'} -> {a.out}")
        return 0
    return diff(a.before, a.after, a.allow_new)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
