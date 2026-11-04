"""Read-only, fresh-install diagnostic of the installed Agent's selection record."""
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path

RECEIPT = "ffmpeg-diagnostic-receipt.json"
BOOTSTRAP_LOG = "fresh-install-bootstrap-full.log"


def unavailable(reason):
    return {"schema_version": 1, "kind": "fresh-install-ffmpeg-diagnostic",
            "classification": "NON-PROOF", "status": "inconclusive", "reason": reason}


def capture(journey, gate, args, root, data, state, evidence):
    receipt = unavailable("capture did not complete")
    receipt["bootstrap"] = "succeeded" if state.get("updated") else "not-succeeded"
    log = evidence / "release1" / "install-bootstrap.log"
    receipt["bootstrap_log"] = {"status": "unavailable", "artifact": None}
    try:
        if log.is_file() and not log.is_symlink():
            checksum = hashlib.sha256()
            with log.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    checksum.update(chunk)
            shutil.copyfile(log, evidence / BOOTSTRAP_LOG)
            receipt["bootstrap_log"] = {"status": "captured", "artifact": "before-cleanup/" + BOOTSTRAP_LOG,
                                        "sha256": checksum.hexdigest(), "bytes": log.stat().st_size}
        if "app" not in state:
            raise ValueError("candidate admission did not succeed")
        candidate = journey.read_json(args.candidate)
        manifest = gate.validate_tuple(candidate, state["app"], state["candidate_path"])
        if manifest != state["manifest"]:
            raise ValueError("candidate manifest changed after admission")
        receipt.update(candidate_app_sha=candidate["app"], candidate_agent_sha=candidate["agent"],
                       candidate_manifest_sha256=candidate["manifest_sha256"])
        if sys.platform != "linux":
            raise ValueError("capture is limited to Linux")
        arch = {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64", "arm64": "arm64"}.get(platform.machine().lower())
        if not arch:
            raise ValueError("unsupported Linux architecture")
        target = "linux-" + arch
        asset = manifest["tools"]["ffmpeg"]["assets"][target]
        url, digest = asset.get("url"), asset.get("sha256")
        if not isinstance(url, str) or not url.startswith("https://") or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest) or "urls" in asset:
            raise ValueError("expected one HTTPS FFmpeg asset with SHA-256")
        receipt["ffmpeg_asset"] = {"target": target, "url": url, "sha256": digest}
        if not state.get("updated"):
            raise ValueError("bootstrap did not succeed; installed selection unavailable")
        if receipt["bootstrap_log"]["status"] != "captured":
            raise ValueError("complete bootstrap log is unavailable")
        version_path, _, version = journey.installation(root, data)
        version_root = version_path.parent.resolve(strict=True)
        journey.selected_tuple(manifest, version)
        installed_manifest = journey.read_json(version_root / "manifest.json")
        if installed_manifest.get("tools", {}).get("ffmpeg") != manifest["tools"]["ffmpeg"]:
            raise ValueError("installed FFmpeg manifest differs from candidate")
        receipt.update(installed_version_root=str(version_root),
                       installed_manifest_sha256=hashlib.sha256((version_root / "manifest.json").read_bytes()).hexdigest())
        python = Path(version["python"])
        if not python.is_absolute() or not python.is_relative_to(version_root / "py") or not python.is_file():
            raise ValueError("installed Python is outside the candidate version environment")
        # -I removes the checkout and PYTHONPATH from imports. Reject any module
        # that nevertheless resolves outside this selected version's environment.
        script = ("import json,sys; from pathlib import Path; "
                  "import astrid.core.gateway.doctor_bundle as d; "
                  "print(json.dumps({'module': d.__file__, 'selected_tools': d._selected_tools(Path(sys.argv[1]))}))")
        env = {k: v for k, v in os.environ.items() if k not in {"PYTHONPATH", "PYTHONHOME"}}
        result = subprocess.run([str(python), "-I", "-c", script, str(root)], cwd=str(root), env=env,
                                capture_output=True, text=True, timeout=120, check=True)
        observed = json.loads(result.stdout)
        module = Path(observed["module"]).resolve(strict=True)
        if not module.is_relative_to(version_root / "py"):
            raise ValueError("doctor_bundle did not come from the installed version environment")
        receipt["doctor_bundle_module"] = str(module)
        selected = observed["selected_tools"]
        receipt["selected_tools"] = selected
        if not isinstance(selected, dict):
            raise ValueError("selected-tools capture is unavailable")
        for name in ("ffmpeg", "ffprobe"):
            row = selected.get(name)
            if not isinstance(row, dict) or not isinstance(row.get("version"), str) or not row["version"]:
                raise ValueError(f"{name} selected-tools record is unavailable")
            if row.get("source") != "download":
                raise ValueError(f"{name} source is {row.get('source')!r}, not download")
            executable = row.get("executable")
            if not isinstance(executable, str) or not Path(executable).is_absolute():
                raise ValueError(f"{name} executable path is unavailable")
            path = Path(executable)
            expected = version_root / "tools" / "ffmpeg"
            resolved_expected = expected.resolve(strict=True)
            if (not path.is_relative_to(expected)
                    or not resolved_expected.is_relative_to(version_root)
                    or not path.resolve(strict=True).is_relative_to(resolved_expected)
                    or not path.is_file() or not os.access(path, os.X_OK)):
                raise ValueError(f"{name} executable is not within the selected version's tools/ffmpeg")
        receipt.update(status="observed-download", reason=None)
    except Exception as exc:
        receipt["reason"] = f"{type(exc).__name__}: {exc}"
    journey.write_json(evidence / RECEIPT, receipt)
    if receipt["status"] != "observed-download":
        raise RuntimeError(receipt["reason"])
