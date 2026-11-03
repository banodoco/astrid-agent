"""Print an exact-head hosted request; pass --dispatch to submit it with gh."""

import argparse
import json
from pathlib import Path
import subprocess


REPO = "banodoco/astrid-agent"
BRANCH = "lane/M-QUAL/R-03-WINDOWS-PACKHOST-DIAG-R1"
CANDIDATE = {
    "agent": "ab43bad818ef65ca1e929b973a2f61cd9fc4bf61",
    "runtime": "2776ba973b6c8f3555b5629b0840d9691a98d3c2",
    "app": "79f7e5397c044c4c97dc7e97b20ad96f787e5247",
    "pinokio": "3a6a98ae251ca126505245286e2d3e760f4348e9",
    "manifest_sha256": "b2584f0bb5a949890364004572a0182f2b568adcdba061e73b739e1d3d2d623f",
    "artifacts": {
        "public/install": "100bab1deb15d620abd910c8fb0c9a6d77f4945428a3229dac6b506705e3fd0d",
        "public/install.sh": "591c4c51b148fccef2a7d1c45782d9ff949d5fd03d46cd82b5591e3a23d1b285",
        "public/install.ps1": "bba3f2edc41c047cb51475ceeac4489a04610af73a3af44bdc05287964d28258",
    },
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dispatch", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    remote = subprocess.check_output(
        ["git", "ls-remote", "origin", f"refs/heads/{BRANCH}"], cwd=root, text=True,
    ).split()
    if not remote or remote[0] != head:
        raise SystemExit("The requested workflow head is not the pushed lane head")
    inputs = {
        "candidate": json.dumps(CANDIDATE, separators=(",", ":")),
        "old_launcher_ref": "bbba620e3d89eeff8021417f365e6ea584203105",
        "stopped_path_agent_ref": CANDIDATE["agent"],
        "direct_only": "true",
    }
    command = ["gh", "workflow", "run", "journey.yml", "--repo", REPO, "--ref", BRANCH, "--json"]
    print(json.dumps({
        "repository": REPO, "workflow": "journey.yml", "ref": BRANCH,
        "expected_workflow_head": head, "inputs": inputs, "dispatch_argv": command,
        "target_row": "direct-windows/direct/release0-to-release1/windows-2022/powershell.exe",
        "inventory_limitation": "direct_only executes all three direct OS rows; no Windows-only selector exists",
        "changed_input": "native log roots, finite readiness captures, nested retention; product tuple unchanged",
        "collection": "journey-receipt-direct-windows/before-cleanup/runtime-service-service-readiness-1-*.log",
        "acceptance": "diagnostic only; verify run head_sha before attributing observations; no row pass or handoff",
        "dispatched": args.dispatch,
    }, indent=2), flush=True)
    if args.dispatch:
        subprocess.run(command, input=json.dumps(inputs), text=True, check=True, cwd=root)


if __name__ == "__main__":
    main()
