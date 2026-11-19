"""Check source identity and record only names, versions, and content hashes."""
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
from pathlib import Path


def command(*args):
    return subprocess.check_output(args, text=True).strip()


def read(path):
    return json.loads(Path(path).read_text())


def source_identity():
    root = Path(os.environ["GITHUB_WORKSPACE"])
    refs = {name: command("git", "-C", root / name, "rev-parse", "HEAD")
            for name in ("src", "astrid-runtime", "astrid-pinokio", "vibecomfy-checkout",
                         "runpod-lifecycle-checkout", "comfyui-checkout")}
    for lock in ("runtime.lock.json", "astrid/resources/runtime.lock.json"):
        if read(lock)["source_commit"] != refs["astrid-runtime"]:
            raise ValueError(f"Runtime pin mismatch in {lock}")
    pins = read("astrid/packs/runpod/pins.json")["sources"]
    for pin, checkout in (("comfyui", "comfyui-checkout"), ("vibecomfy", "vibecomfy-checkout"),
                          ("lifecycle", "runpod-lifecycle-checkout")):
        if pins[pin]["revision"] != refs[checkout]:
            raise ValueError(f"Source pin mismatch for {pin}")
    spec = read("remotion/package.json")["dependencies"]["@banodoco/timeline-composition"]
    artifact = Path("remotion") / spec.removeprefix("file:")
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    if digest != Path(f"{artifact}.sha256").read_text().strip():
        raise ValueError("Composition digest mismatch")
    return {**refs, "composition_sha256": digest, "proof": os.environ["GITHUB_SHA"]}


def inventory():
    expected = {"os_release": "ubuntu-26.04", "runner_image_version": "20261111.196.1", "python": "3.11.18",
                "node": "v20.19.4", "npm": "10.8.2", "uv": "0.11.32",
                "ffmpeg": "7:8.0.1-3ubuntu2"}
    release = platform.freedesktop_os_release()
    actual = {"os_release": f"{release['ID']}-{release['VERSION_ID']}", "runner_image_version": os.environ["ImageVersion"], "python": platform.python_version(),
              "node": command("node", "-v"), "npm": command("npm", "-v"),
              "uv": command("uv", "--version").split()[1],
              "ffmpeg": command("dpkg-query", "-W", "-f=${Version}", "ffmpeg")}
    def canonical(name):
        return re.sub(r"[-_.]+", "-", name).lower()
    packages = {canonical(row["name"]): row["version"] for row in
                json.loads(command(sys.executable, "-m", "pip", "list", "--format=json"))}
    differences = {name: [value, actual.get(name)] for name, value in expected.items()
                   if actual.get(name) != value}
    for line in Path(__file__).with_name("original.txt").read_text().splitlines():
        name, version = line.split("==")
        if packages.get(canonical(name)) != version:
            differences[name] = [version, packages.get(canonical(name))]
    system = command("dpkg-query", "-W", "-f=${db:Status-Abbrev} ${binary:Package} ${Version}\\n")
    return {"original_version_differences": differences, "tools": actual,
            "architecture": platform.machine(), "kernel": platform.release(),
            "python_packages": packages, "os_packages":
            sorted(line[4:] for line in system.splitlines() if line.startswith("ii "))}


if __name__ == "__main__":
    kind, directory = sys.argv[1:]
    data = {"sources": source_identity, "inventory": inventory}[kind]()
    (Path(directory) / f"{kind}.json").write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
