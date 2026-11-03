"""Linux principal-boundary discriminator, never M4-6 admission or native proof.

Run as root on a disposable host. Only synthetic files are modified. The optional
protected files are opened for write without writing any bytes. This probes a
necessary isolation primitive; it does not implement the native session broker.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


CHILD = r'''
import errno, json, os, sys
from pathlib import Path
receipt, uploader, writable, parent, *protected = sys.argv[1:]
def attempt(operation):
    try:
        operation()
        return "allowed"
    except OSError as exc:
        if exc.errno not in (errno.EACCES, errno.EPERM):
            raise
        return "denied"
def write_open(path):
    descriptor = os.open(path, os.O_WRONLY)
    os.close(descriptor)
status = dict(line.split(":", 1) for line in Path("/proc/self/status").read_text().splitlines() if ":" in line)
result = {
    "uid": os.getuid(), "gid": os.getgid(), "groups": os.getgroups(),
    "no_new_privs": status["NoNewPrivs"].strip(),
    "cap_eff": status["CapEff"].strip(), "cap_bnd": status["CapBnd"].strip(),
    "read_export": attempt(lambda: Path(receipt).read_bytes()),
    "replace_export": attempt(lambda: Path(receipt).write_text("synthetic replacement\n")),
    "replace_uploader_sentinel": attempt(lambda: Path(uploader).write_text("synthetic replacement\n")),
    "signal_collector": attempt(lambda: os.kill(int(parent), 0)),
    "become_root": attempt(lambda: os.setuid(0)),
    "protected_file_write_open": [attempt(lambda p=p: write_open(p)) for p in protected],
}
Path(writable).write_text("candidate workspace works\n")
result["candidate_write"] = True
print(json.dumps(result))
'''


def has_identity_authority() -> bool:
    """UID zero inside the task executor is not a host root capability."""
    if sys.platform != "linux" or os.geteuid() != 0:
        return False
    status = dict(line.split(":", 1) for line in Path("/proc/self/status").read_text().splitlines() if ":" in line)
    # CHOWN, SETGID, SETUID and SETPCAP (to clear the bounding set).
    required = (1 << 0) | (1 << 6) | (1 << 7) | (1 << 8)
    return int(status["CapEff"].strip(), 16) & required == required


def run_probe(candidate_uid: int, candidate_gid: int, protected_files: list[Path]) -> dict:
    if sys.platform != "linux" or os.geteuid() != 0:
        raise RuntimeError("NEEDS disposable Linux host with root collector")
    if candidate_uid <= 0 or candidate_gid <= 0:
        raise ValueError("candidate UID/GID must be nonzero")
    if not has_identity_authority():
        raise RuntimeError("NEEDS host CHOWN/SETUID/SETGID/SETPCAP authority; executor UID 0 is insufficient")
    import pwd

    account = pwd.getpwuid(candidate_uid)
    if account.pw_gid != candidate_gid or os.getgrouplist(account.pw_name, candidate_gid) != [candidate_gid]:
        raise ValueError("candidate must have only its primary group")
    setpriv = shutil.which("setpriv")
    if setpriv is None:
        raise RuntimeError("NEEDS util-linux setpriv")
    python = Path("/usr/bin/python3")
    if not python.is_file():
        raise RuntimeError("NEEDS system Python executable")
    protected_files = [path.resolve(strict=True) for path in protected_files]
    # Root-owned synthetic publication using the existing native export helper.
    from scripts import native_service_adapter as native

    results = {}
    with tempfile.TemporaryDirectory(prefix="m46-boundary-", dir="/tmp") as temporary:
        root = Path(temporary)
        root.chmod(0o755)
        for mode in ("same-principal-control", "separate-principal"):
            case = root / mode
            case.mkdir(mode=0o755)
            observation = {"slot_id": "direct-linux", "execution_scope": native.FIXTURE_SCOPE}
            observation["evidence_files"] = native._evidence_bundle(observation)[0]
            receipt = case / "collector" / "receipt.json"
            native._publish_atomic(observation, {"synthetic": True, "admitted": False}, receipt)
            uploader = receipt.parent / "uploader-sentinel.js"
            uploader.write_text("synthetic uploader; no credentials\n")
            original = [path.read_bytes() for path in (receipt, uploader)]
            work = case / "candidate"
            work.mkdir(mode=0o700)
            if mode == "separate-principal":
                os.chown(work, candidate_uid, candidate_gid)
            prefix = [] if mode == "same-principal-control" else [
                setpriv, f"--reuid={candidate_uid}", f"--regid={candidate_gid}",
                "--clear-groups", "--no-new-privs", "--inh-caps=-all",
                "--ambient-caps=-all", "--bounding-set=-all",
            ]
            completed = subprocess.run(
                [*prefix, str(python), "-I", "-c", CHILD, str(receipt), str(uploader),
                 str(work / "write-test"), str(os.getpid()), *map(str, protected_files)],
                cwd="/", env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
                capture_output=True, text=True, timeout=20, check=False,
            )
            if completed.returncode:
                raise RuntimeError(f"candidate probe failed: exit {completed.returncode}: {completed.stderr}")
            row = json.loads(completed.stdout)
            expected = "allowed" if mode == "same-principal-control" else "denied"
            for name in ("read_export", "replace_export", "replace_uploader_sentinel", "signal_collector", "become_root"):
                if row[name] != expected:
                    raise RuntimeError(f"{mode}: unexpected {name}: {row[name]}")
            if row["protected_file_write_open"] != [expected] * len(protected_files):
                raise RuntimeError(f"{mode}: protected file write authority differs")
            if mode == "separate-principal":
                if (row["uid"], row["gid"], row["groups"], row["no_new_privs"],
                    int(row["cap_eff"], 16), int(row["cap_bnd"], 16)) != (
                    candidate_uid, candidate_gid, [], "1", 0, 0,
                ):
                    raise RuntimeError("candidate token differs from requested restriction")
                if original != [path.read_bytes() for path in (receipt, uploader)]:
                    raise RuntimeError("protected bytes changed")
            row["child_exit"] = completed.returncode
            row["export_sha256_after"] = hashlib.sha256(receipt.read_bytes()).hexdigest()
            row["export_unchanged"] = original[0] == receipt.read_bytes()
            results[mode] = row
    return {
        "kind": "m46-linux-principal-boundary-experiment", "admitted": False,
        "native_lifecycle_observed": False, "hosted_proof": False,
        "collector_uid": os.getuid(), "candidate_account": account.pw_name,
        "protected_files": list(map(str, protected_files)), "results": results,
        "limits": ["synthetic export and uploader sentinel", "Linux only",
                   "no session broker or service-manager isolation proven",
                   "no credentials read, copied, or captured"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-uid", required=True, type=int)
    parser.add_argument("--candidate-gid", required=True, type=int)
    parser.add_argument("--protected-file", action="append", type=Path, default=[])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = run_probe(args.candidate_uid, args.candidate_gid, args.protected_file)
    # The destination must be trusted; the dispatcher chooses it before candidate execution.
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
