"""CPU refusal checks; these do not execute or qualify real upstream init."""
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys

import pytest


REPO = Path(__file__).resolve().parents[3]

# Observe Python-level attempts as well as final state. This test guard is not
# OS isolation and must never be used to execute an actual Wan2GP checkout.
AUDITED_ENTRY = '''
import atexit, json, os, runpy, sys
from pathlib import Path
root, report = map(Path, sys.argv[1:])
attempts = []
def audit(event, args):
    forbidden = event.startswith(("socket.", "urllib."))
    forbidden |= event == "import" and args[0].split(".")[0] == "shared"
    paths = ()
    if event == "open":
        _, mode, flags = args
        if (isinstance(mode, str) and any(c in mode for c in "wax+")) or flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
            paths = args[:1]
    elif event in {"os.mkdir", "os.remove", "os.rmdir", "os.chmod", "os.truncate"}:
        paths = args[:1]
    elif event in {"os.rename", "os.link", "os.symlink"}:
        paths = args[:2]
    for path in paths:
        if isinstance(path, (str, bytes)):
            # Lexical Python paths only; snapshots also check links and final
            # state. This is not an audit of native or dirfd-relative writes.
            forbidden |= Path(os.fsdecode(path)).absolute().is_relative_to(root)
    if forbidden:
        attempts.append({"event": event, "args": repr(args)})
        raise AssertionError("no-touch fixture rejected " + event)
sys.addaudithook(audit)
atexit.register(lambda: report.write_text(json.dumps(attempts)))
sys.argv = ["astrid.packs.wan2gp.src.driver", "--wan-session"]
runpy.run_module(sys.argv[0], run_name="__main__", alter_sys=True)
'''


def snapshot(root):
    """Record names, hashes, modes and link targets without following links."""
    result = {}
    for path in [root, *sorted(root.rglob("*"))]:
        mode = path.lstat().st_mode
        result[str(path.relative_to(root))] = (
            mode,
            hashlib.sha256(path.read_bytes()).hexdigest() if stat.S_ISREG(mode) else None,
            os.readlink(path) if stat.S_ISLNK(mode) else None,
        )
    return result


@pytest.fixture
def installation(tmp_path):
    root = tmp_path / "engine"
    (root / "shared").mkdir(parents=True)
    (root / "shared/__init__.py").write_text('raise AssertionError("must not import upstream")\n')
    (root / "shared/api.py").write_text('raise AssertionError("must not import upstream")\n')
    (root / "wgp_config.json").write_text('{"config": 42}')
    (root / "config-link.json").symlink_to("wgp_config.json")
    return root


def invoke(tmp_path, root, frame, *, path=None, following_frames=()):
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO) + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    if path is not None:
        env["PATH"] = path
    before = snapshot(root)
    report = tmp_path / "attempts.json"
    result = subprocess.run(
        [sys.executable, "-B", "-c", AUDITED_ENTRY, str(root), str(report)],
        input="".join(json.dumps(item) + "\n" for item in (frame, *following_frames)),
        text=True, capture_output=True, env=env, cwd=tmp_path, timeout=20,
    )
    assert result.returncode == 1, (result.stdout, result.stderr)
    assert snapshot(root) == before
    assert json.loads(report.read_text()) == []
    frames = [json.loads(line) for line in result.stdout.splitlines()]
    assert len(frames) == 1 and frames[0]["kind"] == "error", result.stderr
    assert not list(tmp_path.glob("wan-config-*"))
    return frames[0]


def initialization(root, output):
    return {"birth": "b", "root": str(root), "init": {
        "root": str(root), "output_dir": str(output),
        "config_path": str(root / "wgp_config.json"),
    }}


def assert_refusal(error, root):
    assert error["birth"] == "b" and error["status"] == "blocked"
    assert error["code"] == "wan2gp_init_requires_consent"
    fix, = error["fixes"]
    assert fix["id"] == error["code"]
    assert fix["scope"] == "user_env" and fix["root"] == str(root)
    assert fix["requires_consent"] is True and fix["retry"] == {"allow_user_env": True}
    for detail in (str(root), "shared.api.init", "migrate", "FFmpeg", "download", "user_env", "--wan-session", "allow_user_env", "JSON true"):
        assert detail in error["error"] and detail in fix["description"]


@pytest.mark.parametrize("case", ["normal", "missing_ffmpeg", "old_settings"])
def test_default_probe_refuses_before_import_write_or_download(tmp_path, installation, case):
    root = installation
    if case == "normal":
        (root / "ffmpeg_bins").mkdir()
        (root / "ffmpeg_bins/ffmpeg").write_text("fixture only; never execute")
    if case == "old_settings":
        (root / "t2v_settings.json").write_text('{"legacy": true}')
        (root / "i2v_settings.json.bak").write_text('{"legacy": true}')
    spool = tmp_path / "owner/spool"
    frame = initialization(root, spool)
    error = invoke(tmp_path, root, frame, path="" if case == "missing_ffmpeg" else None)
    assert_refusal(error, root)
    assert not spool.parent.exists()


@pytest.mark.parametrize("consent", [None, False, 0, 1, "", "true", "confirmed", [], {"user_env": True}])
def test_only_json_true_in_initial_frame_is_consent(tmp_path, installation, consent):
    spool = tmp_path / "owner/spool"
    frame = initialization(installation, spool)
    frame["allow_user_env"] = consent
    # A later run/frame must not turn default readiness into setup permission.
    later = {**frame, "allow_user_env": True, "op": "run", "settings": {"allow_user_env": True}}
    assert_refusal(invoke(tmp_path, installation, frame, following_frames=[later]), installation)
    assert not spool.parent.exists()


@pytest.mark.parametrize("case", ["nested_output", "missing_config", "symlink_output", "relative_output"])
def test_rejected_consented_initialization_does_not_touch_root(tmp_path, installation, case):
    root = installation
    spool = tmp_path / "owner/spool"
    frame = initialization(root, spool)
    frame["allow_user_env"] = True
    if case == "nested_output":
        frame["init"]["output_dir"] = str(root / "new/spool")
        expected = "output_dir must be outside Wan root"
    elif case == "missing_config":
        frame["init"]["config_path"] = str(root / "missing.json")
        expected = "missing.json"
    elif case == "symlink_output":
        alias = tmp_path / "engine-alias"
        alias.symlink_to(root, target_is_directory=True)
        frame["init"]["output_dir"] = str(alias / "new/spool")
        expected = "engine-alias"
    else:
        frame["init"]["output_dir"] = "relative/spool"
        expected = "absolute output_dir"
    error = invoke(tmp_path, root, frame)
    assert expected in error["error"]


def test_upstream_options_cannot_carry_consent(tmp_path, installation):
    spool = tmp_path / "owner/spool"
    frame = initialization(installation, spool)
    frame["init"]["allow_user_env"] = True
    error = invoke(tmp_path, installation, frame)
    assert "unsupported Wan initialization option" in error["error"]
    assert not spool.parent.exists()
