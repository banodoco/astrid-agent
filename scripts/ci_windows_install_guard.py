"""One real candidate bootstrap under the workflow's fresh-profile constraints."""

from __future__ import annotations

import argparse
import json
import os
import re
import runpy
import shutil
import subprocess
import sys
import tempfile
import time
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
# A real generation must complete well inside this; the tester's failure sat "running" for 300 s.
GENERATION_PROMPT = "a copper teapot"
GENERATION_TIMEOUT_SECONDS = 420
GENERATION_BUDGET_SECONDS = 240
EVIDENCE_FILE_LIMIT = 64 * 1024 * 1024


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
    # The App's published manifest carries the last release's engine pins. The
    # installer refuses a manifest whose pins differ from the candidate Agent's
    # own, so take them from the checkout under test.
    pins = json.loads((root / "astrid" / "packs" / "runpod" / "pins.json").read_text(encoding="utf-8"))["sources"]
    manifest["lifecycle"] = {"repo": pins["lifecycle"]["repository"],
                             "sha": pins["lifecycle"]["revision"],
                             "version": pins["lifecycle"]["version"]}
    manifest["vibecomfy"] = {"sha": pins["vibecomfy"]["revision"]}
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


def _text(value):
    return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value or ""


def _skip_large_files(directory, names):
    """copytree filter: attempt roots may hold large media, and only evidence-sized files are kept."""
    skipped = []
    for name in names:
        path = os.path.join(directory, name)
        if os.path.isfile(path) and not os.path.islink(path) and os.path.getsize(path) > EVIDENCE_FILE_LIMIT:
            skipped.append(name)
    return skipped


def _attempt_roots():
    """Host attempt roots: astrid-attempt-<token> under the Runtime's attempts/<digest> spool, or in TEMP."""
    found = []
    data = windows.data_root()
    if data.is_dir():
        found.extend(path for path in data.glob("attempts/*/astrid-attempt-*") if path.is_dir())
    found.extend(path for path in Path(tempfile.gettempdir()).glob("astrid-attempt-*") if path.is_dir())
    return found


def capture_generation_failure():
    """Retain Runtime logs, attempt roots and python.exe process state before the step fails."""
    evidence = windows.EVIDENCE / "generation-failure"
    evidence.mkdir(parents=True, exist_ok=True)
    report = {"runtime_logs": None, "attempts": [], "tasklist": None}
    try:
        logs = Path(os.environ["LOCALAPPDATA"]) / "Astrid Runtime" / "logs"
        shutil.copytree(logs, evidence / "runtime-logs", symlinks=True, dirs_exist_ok=True,
                        ignore=_skip_large_files)
        report["runtime_logs"] = "captured"
    except (OSError, KeyError) as error:
        report["runtime_logs"] = f"unavailable: {type(error).__name__}"
    for index, root in enumerate(_attempt_roots()):
        destination = evidence / "attempts" / f"{index}-{root.name}"
        try:
            shutil.copytree(root, destination, symlinks=True, dirs_exist_ok=True, ignore=_skip_large_files)
            report["attempts"].append({"source": str(root), "evidence": destination.name, "status": "captured"})
        except OSError as error:
            report["attempts"].append({"source": str(root), "status": f"capture failed: {type(error).__name__}"})
    try:
        listing = subprocess.run(["tasklist", "/v", "/fi", "imagename eq python.exe"], capture_output=True,
                                 text=True, encoding="utf-8", errors="replace", timeout=60, check=False)
        (evidence / "tasklist-python.txt").write_text(listing.stdout + listing.stderr, encoding="utf-8")
        report["tasklist"] = "captured"
    except (OSError, subprocess.SubprocessError) as error:
        report["tasklist"] = f"unavailable: {type(error).__name__}"
    windows.save("generation-failure.json", report)


def generation_smoke(comfy, command):
    """One real generation through the installed product, into the stand-in ComfyUI server."""
    output = windows.EVIDENCE / "generation-output"
    timing = {"status": "failed", "seconds": None, "budget_seconds": GENERATION_BUDGET_SECONDS,
              "timeout_seconds": GENERATION_TIMEOUT_SECONDS}
    try:
        # The Runtime and its pack host are started by the installer (autostart off
        # still spawns the foreground Runtime); wait for the same readiness the journey uses.
        windows.wait_for("Runtime health is ok and belongs to this installation", windows.healthy, seconds=180)
        windows.wait_for("pack-host is running with its current ready file", windows.host_ready, seconds=180)
        started = time.monotonic()
        try:
            payload = command(["generate", "image", GENERATION_PROMPT, "--entry", "qwen-image-2.1-turbo",
                               "--mode", "t2i", "--placement", comfy.url, "--width", "512", "--height", "512",
                               "--seed", "42", "--out", str(output), "--json"], "generate-image",
                              timeout=GENERATION_TIMEOUT_SECONDS)
        finally:
            timing["seconds"] = round(time.monotonic() - started, 3)
        posts = [request for request in comfy.requests if request.method == "POST" and request.path == "/prompt"]
        if len(posts) != 1:
            raise RuntimeError(f"Real generation sent {len(posts)} POST /prompt requests to ComfyUI; expected exactly one")
        images = [Path(path) for path in payload.get("image_paths", [])]
        if not images or not all(path.is_file() and path.stat().st_size > 0 for path in images):
            raise RuntimeError(f"Real generation delivered no output file: {payload.get('image_paths')!r}")
        if timing["seconds"] > GENERATION_BUDGET_SECONDS:
            raise RuntimeError(f"Real generation took {timing['seconds']} s; budget is {GENERATION_BUDGET_SECONDS} s")
        timing.update(status="passed", prompt_posts=len(posts), outputs=[str(path) for path in images])
    except BaseException as error:
        timing["error"] = f"{type(error).__name__}: {error}"
        capture_generation_failure()
        raise RuntimeError(f"Installed real generation failed; see generation-failure/ and generation-timing.json: "
                           f"{timing['error']}") from error
    finally:
        windows.save("generation-timing.json", timing)


def command_smoke():
    """Run only the installed candidate; the checkout supplies an HTTP fixture."""
    installed = windows.version()
    env = {key: value for key, value in os.environ.items() if key not in {"PYTHONPATH", "PYTHONHOME"}}
    env.update(installed["env"])

    def command(args, name, *, timeout=60):
        try:
            result = subprocess.run([installed["python"], "-I", "-m", "astrid", *args],
                                    cwd=windows.EVIDENCE, env=env, capture_output=True,
                                    text=True, encoding="utf-8", errors="replace", timeout=timeout)
        except subprocess.TimeoutExpired as error:
            (windows.EVIDENCE / ("command-" + name + ".log")).write_text(
                _text(error.stdout) + _text(error.stderr), encoding="utf-8")
            raise
        output = result.stdout + result.stderr
        (windows.EVIDENCE / ("command-" + name + ".log")).write_text(output, encoding="utf-8")
        if "Traceback (most recent call last)" in output or result.returncode:
            raise RuntimeError(f"Installed command smoke {name} failed (exit {result.returncode}): {output[-2000:]}")
        return json.loads(result.stdout) if "--json" in args else None

    command(["doctor", "--json"], "doctor")
    command(["generate", "--help"], "generate-help")
    command(["generate", "scan", "--json"], "scan")
    command(["generate", "defaults", "--json"], "defaults")
    command(["generate", "fetch-models", "--help"], "fetch-help")
    # The installed-child protocol fixture is pure stdlib and runs on Windows.
    # Add the discovery and folder endpoints needed by public CLI preparation.
    fixture = runpy.run_path(str(windows.ROOT / "tests/fixtures/comfyui_protocol/server.py"))
    with fixture["ComfyUIProtocolServer"]() as comfy:
        folders = {}
        for name in ("diffusion_models", "text_encoders", "vae"):
            folder = windows.EVIDENCE / "smoke-models" / name
            folder.mkdir(parents=True, exist_ok=True)
            folders[name] = [str(folder)]
        original = comfy.server.RequestHandlerClass

        class Handler(original):
            def do_GET(self):
                if self.path == "/system_stats":
                    self.send(200, {"system": {"comfyui_version": "0.0.0"}, "devices": []})
                elif self.path == "/internal/folder_paths":
                    self.send(200, folders)
                else:
                    super().do_GET()

        comfy.server.RequestHandlerClass = Handler
        menus = []
        for node, field in (("UNETLoader", "unet_name"), ("CLIPLoader", "clip_name"), ("VAELoader", "vae_name")):
            inputs = comfy.object_info[node]["input"]["required"]
            menus.append((inputs, field, inputs[field]))
            inputs[field] = [[]]
        plan = command(["generate", "fetch-models", "qwen-image-2.1-turbo", "t2i",
                        "--comfy-url", comfy.url, "--json"], "fetch-plan")
        if not plan.get("plan_sha256") or len(plan.get("files", [])) != 3:
            raise RuntimeError("Installed fetch-models did not produce the three-model plan")
        for inputs, field, menu in menus:
            inputs[field] = menu
        command(["generate", "image", "a copper teapot", "--entry", "qwen-image-2.1-turbo",
                 "--placement", comfy.url, "--width", "512", "--height", "512", "--seed", "42",
                 "--dry-run", "--json"], "attached-dry-run")
        if any(request.method == "POST" for request in comfy.requests):
            raise RuntimeError("Plan/dry run unexpectedly submitted to ComfyUI")
        # Menus are restored above, so this is the same model resolution a real submit performs.
        generation_smoke(comfy, command)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("step", choices=("prepare", "install", "smoke"))
    args = parser.parse_args()
    if sys.platform != "win32":
        parser.error("Run the install guard only on Windows")
    windows.EVIDENCE = Path(os.environ["RUNNER_TEMP"]) / "windows-install-guard"
    windows.EVIDENCE.mkdir(parents=True, exist_ok=True)
    if args.step == "prepare":
        prepare()
    elif args.step == "install":
        install()
    else:
        command_smoke()


if __name__ == "__main__":
    main()
