"""One real candidate bootstrap under the workflow's fresh-profile constraints."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

if __package__:
    from . import ci_windows_journey as windows
else:
    import ci_windows_journey as windows


GUARDS = {
    "B1/B2 long path": r"FileNotFoundError|WinError 206|filename or extension is too long|MAX_PATH",
    "B3 Git missing": r"require Git on PATH|git executable|git.*not (?:found|recognized)",
    "F1 TLS": r"CERTIFICATE_VERIFY_FAILED|unable to get local issuer|SSL.*certificate|trust relationship",
    "F4 DACL": r"DACL|protected owner|require owner access|reparse points",
}


def prepare():
    """Use checkout identities, with the App's tool pins and archive source path."""
    root = windows.ROOT
    manifest = windows.install_manifest(root, root / "journey-runtime", root / "journey-app")
    sources = {name: windows.source(path) for name, path in {
        "agent": root, "runtime": root / "journey-runtime", "app": root / "journey-app",
    }.items()}
    lock = json.loads((root / "runtime.lock.json").read_text(encoding="utf-8"))
    if sources["runtime"]["sha"] != lock["source_commit"]:
        raise ValueError("Runtime checkout does not match runtime.lock.json")
    # Do not use local repo records: uv would need Git before the candidate's
    # installer can provision it, and agent extraction would omit GitHub's long
    # top-level directory. The bootstrap builds these exact commit archives.
    for name in ("agent", "runtime"):
        manifest[name] = {"sha": sources[name]["sha"]}
    windows.save("install-manifest.json", manifest)
    windows.save("sources.json", sources)


def install():
    evidence = windows.EVIDENCE
    manifest_path = evidence / "install-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    receipt = {"status": "failed", "candidate": manifest, "guards": [], "files": {}}
    # powershell.exe started under pwsh inherits PowerShell 7's module path and then
    # cannot load its own modules (Get-Acl); the journey driver drops it the same way.
    env = {key: value for key, value in os.environ.items() if key.casefold() != "psmodulepath"}
    env["BROWSER"] = f'"{sys.executable}" -c "pass" %s'
    try:
        windows.run_bootstrap_process([
            "powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-File", windows.ROOT / "journey-app" / "public" / "install.ps1",
            "--manifest", manifest_path, "--yes", "--autostart", "off", "--command", "off",
            "--skills", "off", "--skill-copies", "off",
        ], evidence / "install-bootstrap.log", "install", env=env, timeout=3600)
        installed = windows.version()
        actual = json.loads((Path(installed["root"]) / "manifest.json").read_text(encoding="utf-8"))
        for name in ("agent", "runtime"):
            if actual[name]["sha"] != manifest[name]["sha"]:
                raise RuntimeError(f"Installed {name} is not the checked-out candidate")
        receipt["status"] = "passed"
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        receipt["error"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        # These named files cannot include Runtime auth/credential state. Early
        # bootstrap/DACL failures may precede both install.log and last-run.json;
        # the bootstrap process receipt and guard-result.json still record them.
        logs = Path(env["LOCALAPPDATA"]) / "Astrid Runtime" / "logs"
        for name in ("install.log", "last-run.json"):
            try:
                source = logs / name
                if source.is_file():
                    shutil.copyfile(source, evidence / name)
                    receipt["files"][name] = "captured"
                else:
                    receipt["files"][name] = "unavailable"
            except OSError as error:
                receipt["files"][name] = f"capture failed: {type(error).__name__}"
        text = "\n".join(path.read_text(encoding="utf-8", errors="replace")
                         for path in evidence.glob("*.log"))
        if receipt["status"] == "failed":
            for guard, pattern in GUARDS.items():
                if re.search(pattern, text, re.IGNORECASE):
                    receipt["guards"].append(guard)
                    print(f"::error title=Windows install guard - {guard}::"
                          "Candidate installation failed; see install.log and bootstrap evidence.")
            if not receipt["guards"]:
                print("::error title=Windows install guard - unclassified failure::"
                      "See guard-result.json and bootstrap evidence; failure is not waived.")
        windows.save("guard-result.json", receipt)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("step", choices=("prepare", "install"))
    args = parser.parse_args()
    if sys.platform != "win32":
        parser.error("Run the install guard only on Windows")
    windows.EVIDENCE = Path(os.environ["RUNNER_TEMP"]) / "windows-install-guard"
    windows.EVIDENCE.mkdir(parents=True, exist_ok=True)
    if args.step == "prepare":
        prepare()
    else:
        install()


if __name__ == "__main__":
    main()
