"""Two ordered H3 edits through the public local action with synthetic ComfyUI."""
from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.core import action_cli
from astrid.packs.h3_av import cli
from astrid.packs.h3_av.shared.generation import generation_timing
from tests.helpers import h3_attached_engine as attached

pytestmark = [pytest.mark.integration, pytest.mark.ffmpeg]


def _json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _capture_runtime_diagnostics(tmp_path):
    destination = os.environ.get("ASTRID_B9_DIAGNOSTICS_DIR")
    if not destination:
        return
    destination = Path(destination).expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    files = sorted(path for path in tmp_path.rglob("*")
                   if path.is_file() and not path.is_symlink())
    (destination / "pre-cleanup-inventory.json").write_text(
        json.dumps([{"path": path.relative_to(tmp_path).as_posix(),
                     "size_bytes": path.stat().st_size}
                    for path in files], indent=2) + "\n",
        encoding="utf-8",
    )
    secret_field = re.compile(
        r'(?i)(["\']?(?:authorization|(?:access|refresh|worker|runtime)?[_ -]?(?:credential|token|secret|password)(?:[_ -]file)?)["\']?\s*[:=]\s*)(["\'])(.*?)(\2)'
    )
    bearer = re.compile(r"(?i)\bBearer\s+\S+")
    jwt = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")
    captured = []
    for path in files:
        name = path.name
        if not (name in {"generic-host.log", "runtime.log"}
                or (name.startswith("generic-host-") and name.endswith(".log"))
                or (name.startswith("service-") and name.endswith(".log"))):
            continue
        content = path.read_text(encoding="utf-8", errors="replace")
        content = secret_field.sub(r"\1\2[REDACTED]\4", content)
        content = bearer.sub("Bearer [REDACTED]", content)
        content = jwt.sub("[REDACTED JWT]", content)
        relative = path.relative_to(tmp_path)
        target = destination / "runtime-logs" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        captured.append({"source": relative.as_posix(),
                         "retained": target.relative_to(destination).as_posix(),
                         "size_bytes": len(content.encode("utf-8"))})
    (destination / "capture-manifest.json").write_text(
        json.dumps({"logs": captured}, indent=2) + "\n", encoding="utf-8"
    )


def test_two_edit_local_action_cpu_qualification(tmp_path, monkeypatch, capfd):
    source = tmp_path / "source.mp4"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "color=c=blue:s=1024x576:r=24:d=1",
                    "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=1",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                    "-shortest", str(source)], check=True, capture_output=True)
    spec = {"version": 2, "duration": 1, "prompt": "Change the selected regions.",
            "media": [{"id": "source", "asset": "source.mp4", "role": "timeline",
                       "modality": "video", "at": {"frame": 0}, "range": [0, 1],
                       "edit": [{"stream": "video", "during": [0, 1],
                                 "mask": {"rectangle": [0, 0, 256, 256]}},
                                {"stream": "video", "during": [0, 1],
                                 "mask": {"rectangle": [768, 320, 256, 256]}}]}],
            "settings": {}}
    request = tmp_path / "spec.json"
    request.write_text(json.dumps(spec))
    asset_map = tmp_path / "assets.json"
    asset_map.write_text(json.dumps({"source.mp4": str(source)}))
    assert cli.main(["plan", "--spec", str(request), "--asset-map", str(asset_map),
                     "--out", str(tmp_path / "plan")]) == 0
    capfd.readouterr()
    plan = _json(tmp_path / "plan" / "plan.json")
    assert plan["pass_count"] == 2

    # The existing fake validates the graph's native-edit topology and staged
    # bytes. Derive expectations from the actual staged inputs, retaining one
    # evidence row per prompt for independent comparison to the compiled bundles.
    original_code = attached.server_code
    evidence = tmp_path / "submissions.jsonl"
    def server_code():
        code = original_code()
        assert code.count("color=c=red:s=64x64") == 1
        code = code.replace("color=c=red:s=64x64", "color=c=red:s=1024x576")
        anchor = '            loader_id, basename, digest, staged = "", None, None, None\n'
        replacement = '''            expected["native_masked_edit"] = True
            expected["asset_files"] = {p.name: {"sha256": hashlib.sha256(p.read_bytes()).hexdigest(), "size": p.stat().st_size} for p in input_directory.iterdir() if p.is_file() and not p.is_symlink()}
            with open(%r, "a", encoding="utf-8") as f:
                f.write(json.dumps({"prompt": prompt, "assets": expected["asset_files"]}) + "\\n")
''' % str(evidence)
        assert code.count(anchor) == 1
        return code.replace(anchor, replacement + anchor)
    monkeypatch.setattr(attached, "server_code", server_code)
    home = tmp_path / "home"
    # tests/conftest.py's autouse sandbox creates the per-test HOME already.
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    # Python's source-tree sitecustomize keeps this hook out of the shared venv.
    hook = Path(__file__).resolve().parents[3] / "sitecustomize.py"
    assert not hook.exists()
    hook.write_text("from tests.helpers.h3_attached_engine import activate\nactivate()\n")
    # macOS exposes its default TMPDIR through /var, a symlink rejected by
    # Runtime's support-root guard. Keep action_cli's ephemeral workspace in
    # this test's canonical, private tmp_path instead.
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    # Do not claim the well-known Runtime door if a developer Runtime is up.
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        runtime_port = probe.getsockname()[1]
    monkeypatch.setenv("ASTRID_RUNTIME_DOOR_PORT", str(runtime_port))
    real_rmtree = action_cli.shutil.rmtree
    def capture_action_cleanup(path, *args, **kwargs):
        candidate = Path(path)
        if (candidate.name.startswith("astrid-action-")
                and candidate.parent.resolve() == tmp_path.resolve()):
            _capture_runtime_diagnostics(tmp_path)
        return real_rmtree(path, *args, **kwargs)
    # Keep the hook local to action_cli: it snapshots the task host and
    # Runtime logs immediately before action_cli removes its ephemeral workspace.
    monkeypatch.setattr(action_cli, "shutil", SimpleNamespace(rmtree=capture_action_cleanup))
    try:
        timing = generation_timing(1)
        engine = attached.AttachedEngine(home, generated_seconds=timing["raw_frames"] / timing["fps"])
        try:
            out = tmp_path / "run"
            code = action_cli.main(["run", "h3_av.transform", "--request", str(request),
                                    "--asset-map", str(asset_map), "--placement", "local", "--out", str(out)])
        finally:
            engine.close()
    finally:
        hook.unlink(missing_ok=True)
    captured = capfd.readouterr()
    (tmp_path / "action-stdout.log").write_text(captured.out)
    (tmp_path / "action-stderr.log").write_text(captured.err)
    assert code == 0, f"exit {code}\n{captured.out}\n{captured.err}"
    ledger = _json(out / "ordered-passes.json")
    passes = ledger["passes"]
    assert ledger["pass_count"] == len(passes) == 2
    assert [row["index"] for row in passes] == [1, 2]
    assert [row["status"] for row in passes] == ["verified", "verified"]
    assert [row["reads"] for row in passes] == ["source", "previous_verified_output"]
    assert passes[0]["input"]["sha256"] == _digest(source)
    assert passes[1]["input"]["sha256"] == passes[0]["output"]["sha256"]
    for row in passes:
        verification = _json(Path(row["verification"]))
        assert verification["status"] == "verified"
        assert verification["candidate_sha256"] == row["output"]["sha256"]
        assert verification["preservation"]["spatial_complement"]
        assert verification["preservation"]["audio"] == [[0, 1]]
    cumulative = ledger["cumulative"]["verification_result"]
    assert cumulative["status"] == "verified"
    assert cumulative["preservation"]["spatial_complement"]
    assert cumulative["preservation"]["audio"] == [[0, 1]]
    events = _json(out / "operation-state.json")["events"]
    phases = [(e["phase"], e["status"]) for e in events]
    assert phases.index(("pass-1", "verified")) < phases.index(("pass-2", "started"))
    submissions = [json.loads(line) for line in evidence.read_text().splitlines()]
    assert len(submissions) == 2
    for submission in submissions:
        nodes = submission["prompt"].values()
        assert sum(node.get("class_type") == "H3V2VGranularFractionalDenoise" for node in nodes) == 1
        assert not any(node.get("class_type") == "LanPaint_KSampler" for node in submission["prompt"].values())
