"""Portable installation CLI, with real consent/custody and fake transfer steps."""

from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import pytest

from astrid.core.generation import cli, comfy_portable as portable, gpu_environment, targets
from tests.core.generation.environment_states import fake_disk_usage, fake_gpu, foreign_destination, snapshot
from tests.core.generation.test_comfy_portable_install import case  # noqa: F401


@pytest.fixture
def command(case, monkeypatch, capsys):
    for key, value in case.machine.environ.items():
        monkeypatch.setenv(key, value)
    # Do not change the process-wide platform (pytest and pathlib use it).
    monkeypatch.setattr(portable, "sys", SimpleNamespace(platform="win32", stderr=sys.stderr))
    monkeypatch.setattr(portable, "load_pin", lambda: case.pin)
    monkeypatch.setattr(gpu_environment, "detect_nvidia", lambda: fake_gpu("rtx4090"))
    monkeypatch.setattr(portable.shutil, "disk_usage", fake_disk_usage(30_000_000_000))
    monkeypatch.setattr(portable, "default_steps", lambda: case.fake.steps)

    def unexpected(*args, **kwargs):
        pytest.fail("Portable setup must not enter the generation facade or open its client.")

    monkeypatch.setattr(cli, "_open_client", unexpected)
    monkeypatch.setattr(cli, "_generate_facade", unexpected)

    def run(*args):
        return cli.main(["install-comfyui", *args])

    return run


def test_help_and_flags(command, case, capsys, tmp_path):
    before = snapshot(tmp_path)
    with pytest.raises(SystemExit) as caught:
        cli.main(["--help"])
    assert caught.value.code == 0
    assert "install-comfyui" in capsys.readouterr().out
    with pytest.raises(SystemExit) as caught:
        command("--help")
    assert caught.value.code == 0
    help_text = capsys.readouterr().out
    assert "no cost" in help_text
    assert "--destination" in help_text
    assert "--apply OFFER_SHA256" in help_text
    assert "--json" in help_text
    assert "--url" not in help_text and "--archive" not in help_text
    for option in ("--url", "--comfy-url", "--archive"):
        with pytest.raises(SystemExit) as caught:
            command(option, "https://example.invalid/elsewhere.zip")
        assert caught.value.code == 2
        capsys.readouterr()
    existing = cli._parser().parse_args(["fetch-models", "entry", "mode", "--comfy-url",
                                       "http://127.0.0.1:8188"])
    assert (existing.entry, existing.mode, existing.comfy_url) == (
        "entry", "mode", "http://127.0.0.1:8188"
    )
    assert snapshot(tmp_path) == before
    assert case.fake.work_calls == (0, 0)


def test_offer_and_decline_write_nothing(command, case, capsys, tmp_path):
    before = snapshot(tmp_path)
    assert command() == 0
    text = capsys.readouterr()
    assert "no cost" in text.out
    assert case.consent in text.out
    assert str(case.destination) in text.out
    assert text.err == ""
    assert command("--destination", str(case.destination), "--json") == 0
    output = capsys.readouterr()
    offer = json.loads(output.out)
    assert offer["status"] == "offered"
    assert offer["offer_sha256"] == case.consent
    assert offer["destination"] == str(case.destination)
    assert offer["cost_usd"] == 0
    assert output.err == ""
    assert snapshot(tmp_path) == before
    assert case.fake.work_calls == (0, 0)
    assert targets.read_target() is None


@pytest.mark.parametrize(("reason", "message"), [
    ("unsupported_platform", "Windows install"),
    ("no_gpu", "No usable NVIDIA GPU"),
    ("unsupported_gpu", "Unsupported NVIDIA GPU"),
    ("no_verified_release", "No verified official"),
    ("destination_invalid", "absolute folder path"),
    ("destination_occupied", "A different folder is already there"),
    ("insufficient_space", "bytes are free"),
])
def test_refusals_are_read_only(command, case, capsys, tmp_path, monkeypatch, reason, message):
    destination = case.destination
    if reason == "unsupported_platform":
        monkeypatch.setattr(portable.sys, "platform", "linux")
    elif reason == "no_gpu":
        monkeypatch.setattr(gpu_environment, "detect_nvidia", lambda: fake_gpu("none"))
    elif reason == "unsupported_gpu":
        monkeypatch.setattr(gpu_environment, "detect_nvidia", lambda: fake_gpu("gtx1060"))
    elif reason == "no_verified_release":
        monkeypatch.setattr(portable, "load_pin", lambda: {**case.pin, "release": None})
    elif reason == "destination_invalid":
        destination = "relative"
    elif reason == "destination_occupied":
        destination = foreign_destination(tmp_path)
    elif reason == "insufficient_space":
        monkeypatch.setattr(portable.shutil, "disk_usage", fake_disk_usage(1))
    before = snapshot(tmp_path)
    args = ("--destination", str(destination))
    assert command(*args) == 2
    output = capsys.readouterr()
    assert message in output.out
    assert "Nothing was written." in output.out
    assert output.err == ""
    assert command(*args, "--json") == 2
    output = capsys.readouterr()
    offer = json.loads(output.out)
    assert offer["status"] == "refused"
    assert offer["refusal"]["code"] == reason
    assert message in offer["refusal"]["message"]
    assert snapshot(tmp_path) == before
    assert case.fake.work_calls == (0, 0)
    assert targets.read_target() is None


def test_confirm_attaches_and_second_apply_is_safe(command, case, capsys, tmp_path):
    previous = targets.TargetRecord(layout="desktop", endpoint="http://127.0.0.1:8190",
                                    root=str(tmp_path / "old"))
    targets.save_target(previous)
    assert command("--json") == 0
    consent = json.loads(capsys.readouterr().out)["offer_sha256"]
    args = ("--destination", str(case.destination), "--apply", consent, "--json")
    assert command(*args) == 0
    output = capsys.readouterr()
    script = case.root / case.pin["release"]["start_script"]
    payload = json.loads(output.out)
    assert payload == {
        "status": "installed",
        "root": str(case.root),
        "python": str(case.root / "python_embeded" / "python.exe"),
        "endpoint": "http://127.0.0.1:8188",
        "kept_previous": None,
        "previous_target": previous.as_dict(),
        "undo": "astrid generate settings unset placement",
        "next_action": {"who": "person",
                        "text": f"Start ComfyUI by opening {script}. Then ask me to check again."},
        "then": "astrid generate targets --json",
    }
    assert "Downloading ComfyUI:" in output.err
    assert "Unpacking ComfyUI" in output.err
    assert "Checking the install" in output.err
    record = targets.read_target()
    assert record.endpoint == payload["endpoint"]
    assert record.layout == "portable"
    assert record.python == payload["python"]
    from astrid.core.generation import settings
    assert settings.placement_default() == {"kind": "attached", "endpoint": payload["endpoint"]}
    before = snapshot(tmp_path)
    version = settings.read().version
    assert command(*args) == 0
    again = capsys.readouterr()
    assert json.loads(again.out) == {**payload, "status": "already_installed",
                                     "previous_target": record.as_dict()}
    assert "Downloading ComfyUI:" not in again.err
    assert "Unpacking ComfyUI" not in again.err
    assert snapshot(tmp_path) == before
    assert settings.read().version == version
    assert case.fake.work_calls == (1, 1)


def test_wrong_digest_reports_new_offer_and_writes_nothing(command, case, capsys, tmp_path):
    before = snapshot(tmp_path)
    assert command("--apply", "wrong", "--json") == 1
    output = capsys.readouterr()
    assert output.out == ""
    error, offer = output.err.split("\n", 1)
    assert json.loads(error)["error"]["code"] == "consent_mismatch"
    assert case.consent in offer and "To confirm:" in offer
    assert snapshot(tmp_path) == before
    assert case.fake.work_calls == (0, 0)
    assert targets.read_target() is None


def test_step_error_and_resume_progress(command, case, capsys, monkeypatch):
    def interrupted(pin, archive_path, part_path, progress):
        part_path.write_bytes(case.fake.archive_bytes[:6])
        raise portable.PortableError("download_interrupted", "Try again.")

    monkeypatch.setattr(portable, "default_steps", lambda: portable.Steps(
        interrupted, case.fake.extract, case.fake.missing,
    ))
    assert command("--apply", case.consent, "--json") == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert json.loads(output.err)["error"] == {
        "code": "download_interrupted", "message": "Try again.",
    }
    assert targets.read_target() is None
    monkeypatch.setattr(portable, "default_steps", lambda: case.fake.steps)
    assert command("--apply", case.consent) == 0
    output = capsys.readouterr()
    assert "Picking up where we left off: 6 of 400 done." in output.err
    assert "ComfyUI is installed." in output.out
    assert "Start ComfyUI by opening" in output.out
    assert "astrid generate targets --json" in output.out
