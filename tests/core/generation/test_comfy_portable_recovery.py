"""94-C2 pair recovery, including a fresh public CLI process and real Runtime."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from astrid.core.generation import cli, comfy_portable as portable, settings, targets
from tests.core.generation.environment_states import snapshot
from tests.core.generation.test_comfy_portable_attach import installed, pin  # noqa: F401
from tests.core.generation.test_comfy_portable_cli import command  # noqa: F401
from tests.core.generation.test_comfy_portable_install import case  # noqa: F401


def previous_choice(tmp_path, present):
    if present:
        root = tmp_path / "previous"
        root.mkdir()
        (root / "work.txt").write_text("stored work")
        targets.save_target(targets.TargetRecord(
            layout="desktop", endpoint="http://127.0.0.1:8190", root=str(root), prefer="attached",
        ))
        settings.update(lambda doc: doc.update(placement={
            "kind": "local", "device": 2, "engine": "comfyui", "machine": "previous-machine",
            "policy": {"idle_timeout_s": 17, "gpu_types": ["previous-gpu"]},
        }))
    return targets.target_snapshot(), settings.read(migrate=False).content["placement"]


def assert_previous(before):
    assert targets.target_snapshot() == before[0]
    assert settings.read(migrate=False).content["placement"] == before[1]


def run_undo(attached):
    argv = shlex.split(attached["undo"])
    assert argv[:2] == ["astrid", "generate"]
    return cli.main(argv[2:])


@pytest.mark.parametrize("present", [False, True])
@pytest.mark.parametrize("owner", ["target", "placement"])
@pytest.mark.parametrize("after", [False, True])
def test_either_write_failure_rolls_back_and_retry_retains_first_pair(
    tmp_path, installed, pin, monkeypatch, present, owner, after,
):
    before = previous_choice(tmp_path, present)
    module, name = (targets, "save_target") if owner == "target" else (settings, "set_placement")
    operation = getattr(module, name)
    calls = 0

    def fail_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            if after:
                operation(*args, **kwargs)
            raise OSError("injected before/after acknowledgement")
        return operation(*args, **kwargs)

    monkeypatch.setattr(module, name, fail_once)
    with pytest.raises(portable.PortableError, match="previous choice was restored"):
        portable.attach_installation(installed, pin=pin)
    assert_previous(before)
    receipt_path = tmp_path / ".astrid-install" / "attachment.json"
    original = json.loads(receipt_path.read_text())
    assert original["phase"] == "restored"
    attached = portable.attach_installation(installed, pin=pin)
    completed = json.loads(receipt_path.read_text())
    assert completed["previous"] == original["previous"]
    assert completed["sha256"] == original["sha256"]
    assert run_undo(attached) == 0
    assert_previous(before)
    assert Path(installed["root"]).is_dir()
    if present:
        assert (tmp_path / "previous" / "work.txt").read_text() == "stored work"


class ProcessExit(BaseException):
    """Skip exception rollback, as a process disappearing between commits does."""


@pytest.mark.parametrize("present", [False, True])
@pytest.mark.parametrize("point", ["prepared", "target", "placement"])
def test_crash_recovery_reuses_original_pair(tmp_path, installed, pin, monkeypatch, present, point):
    before = previous_choice(tmp_path, present)
    module, name = {
        "prepared": (portable, "_write_attachment"),
        "target": (targets, "save_target"),
        "placement": (settings, "set_placement"),
    }[point]
    operation = getattr(module, name)

    def crash_after_commit(*args, **kwargs):
        operation(*args, **kwargs)
        raise ProcessExit()

    monkeypatch.setattr(module, name, crash_after_commit)
    with pytest.raises(ProcessExit):
        portable.attach_installation(installed, pin=pin)
    receipt_path = tmp_path / ".astrid-install" / "attachment.json"
    pending = json.loads(receipt_path.read_text())
    assert pending["previous"] == {"target": before[0], "placement": before[1]}
    assert pending["phase"] == "prepared"
    monkeypatch.setattr(module, name, operation)
    attached = portable.attach_installation(installed, pin=pin)
    assert attached["previous_placement"] == before[1]
    assert json.loads(receipt_path.read_text())["sha256"] == pending["sha256"]
    assert run_undo(attached) == 0
    assert_previous(before)


@pytest.mark.parametrize("present", [False, True])
def test_interrupted_undo_is_recoverable(tmp_path, installed, pin, monkeypatch, present):
    before = previous_choice(tmp_path, present)
    attached = portable.attach_installation(installed, pin=pin)
    restore = settings.restore_placement

    def crash_after_placement(*args, **kwargs):
        restore(*args, **kwargs)
        raise ProcessExit()

    monkeypatch.setattr(settings, "restore_placement", crash_after_placement)
    with pytest.raises(ProcessExit):
        run_undo(attached)
    assert settings.read(migrate=False).content["placement"] == before[1]
    assert targets.read_target().layout == "portable"
    assert json.loads((tmp_path / ".astrid-install" / "attachment.json").read_text())["phase"] == "restoring"
    monkeypatch.setattr(settings, "restore_placement", restore)
    assert run_undo(attached) == 0
    assert_previous(before)
    files, version = snapshot(tmp_path), settings.read().version
    assert run_undo(attached) == 0
    assert snapshot(tmp_path) == files
    assert settings.read().version == version


@pytest.mark.parametrize("later", ["target", "placement", "both"])
@pytest.mark.parametrize("already_undone", [False, True])
def test_later_deliberate_choice_is_not_undone_or_reapplied(
    tmp_path, installed, pin, later, already_undone, capsys,
):
    previous_choice(tmp_path, True)
    attached = portable.attach_installation(installed, pin=pin)
    if already_undone:
        assert run_undo(attached) == 0
    if later in ("target", "both"):
        targets.save_target(targets.TargetRecord(endpoint="http://127.0.0.1:9000", layout="git"))
    if later in ("placement", "both"):
        settings.set_placement("http://127.0.0.1:9000")
    before, placement = snapshot(tmp_path), settings.placement_default()
    assert run_undo(attached) == 1
    assert "attachment_choice_changed" in capsys.readouterr().err
    with pytest.raises(portable.PortableError) as error:
        portable.attach_installation(installed, pin=pin)
    assert error.value.code == "attachment_choice_changed"
    assert snapshot(tmp_path) == before
    assert settings.placement_default() == placement


@pytest.mark.parametrize("later", ["target", "placement"])
def test_failure_unwinds_owned_component_and_keeps_intervening_choice(
    tmp_path, installed, pin, monkeypatch, later,
):
    before = previous_choice(tmp_path, True)
    set_placement = settings.set_placement
    selected = targets.TargetRecord(endpoint="http://127.0.0.1:9000", layout="git", prefer="attached")

    def interleaving(url, client=None):
        if later == "target":
            targets.save_target(selected)
            set_placement(url, client)
        else:
            # This deliberately changes the placement after the guarded
            # attachment call commits, before its acknowledgement returns.
            set_placement(url, client)
            settings.update(lambda doc: doc.update(placement={"kind": "local", "device": 5}))
        raise OSError("write acknowledgement interrupted after deliberate choice")

    monkeypatch.setattr(settings, "set_placement", interleaving)
    with pytest.raises(portable.PortableError) as error:
        portable.attach_installation(installed, pin=pin)
    assert error.value.code == "attachment_recovery_required"
    if later == "target":
        assert targets.read_target() == selected
        assert settings.placement_default() == before[1]
    else:
        assert targets.target_snapshot() == before[0]
        assert settings.placement_default() == {"kind": "local", "device": 5}


def test_undo_restores_legacy_target_and_keeps_unrelated_edits(tmp_path, installed, pin):
    targets.update_config(lambda doc: doc.update({
        "local": {"url": "http://localhost:8190", "layout": "desktop", "prefer": "attached",
                  "custom_old_field": "keep", "facts": {"launch_argv": ["original", "--argument"]}},
        "engines": {"schema_version": 1, "primary": "local", "other": "keep"},
        "library": {"path": "previous-library"},
    }))
    before = targets.target_snapshot()
    placement = {"kind": "attached", "endpoint": "http://127.0.0.1:8190", "device": 3,
                 "policy": {"idle_timeout_s": 17}}
    settings.update(lambda doc: doc.update(placement=placement))
    attached = portable.attach_installation(installed, pin=pin)
    assert attached["placement"] == {**placement, "endpoint": "http://127.0.0.1:8188"}
    targets.update_config(lambda doc: doc["library"].update(path="later-library"))
    settings.update(lambda doc: doc.update(models={"trust_unchanged": False},
                                          defaults={"image.preview": {"pack": "another", "entry": "work"}}))
    assert run_undo(attached) == 0
    assert targets.target_snapshot() == before
    assert targets.read_config()["library"] == {"path": "later-library"}
    assert targets.read_config()["engines"]["other"] == "keep"
    content = settings.read().content
    assert content["placement"] == placement
    assert content["models"] == {"trust_unchanged": False}
    assert content["defaults"] == {"image.preview": {"pack": "another", "entry": "work"}}


def test_effective_legacy_settings_are_captured_without_a_preliminary_write(
    tmp_path, installed, pin, generation_settings_store, monkeypatch,
):
    legacy = {"kind": "local", "device": 4, "policy": {"idle_timeout_s": 23}}
    targets.update_config(lambda doc: doc.update(placement=legacy, runpod={"gpu_types": ["old-gpu"]}))
    persist = portable._write_attachment

    def observe_first_receipt(*args, **kwargs):
        if args[2] == "prepared":
            assert generation_settings_store.documents == {}
        return persist(*args, **kwargs)

    monkeypatch.setattr(portable, "_write_attachment", observe_first_receipt)
    attached = portable.attach_installation(installed, pin=pin)
    assert attached["previous_placement"] == legacy
    assert settings.read().content["machines"] == {"runpod": {"gpu_types": ["old-gpu"]}}
    assert run_undo(attached) == 0
    assert settings.placement_default() == legacy
    assert settings.read().content["machines"] == {"runpod": {"gpu_types": ["old-gpu"]}}
    assert targets.read_config()["placement"] == legacy
    assert targets.read_config()["runpod"] == {"gpu_types": ["old-gpu"]}


def test_unavailable_runtime_is_not_recorded_as_prior_absence(tmp_path, installed, pin, monkeypatch):
    before = snapshot(tmp_path)
    monkeypatch.setattr(settings, "current_store", lambda client=None: None)
    with pytest.raises(portable.PortableError) as error:
        portable.attach_installation(installed, pin=pin)
    assert error.value.code == "attachment_unavailable"
    assert snapshot(tmp_path) == before


def test_receipt_is_durable_before_first_target_write(tmp_path, installed, pin, monkeypatch):
    before = previous_choice(tmp_path, True)

    def unavailable_receipt(*args, **kwargs):
        raise OSError("cannot publish recovery receipt")

    monkeypatch.setattr(portable, "_write_attachment", unavailable_receipt)
    with pytest.raises(OSError):
        portable.attach_installation(installed, pin=pin)
    assert_previous(before)


def test_text_undo_roundtrip_preserves_old_install_and_new_work(command, case, tmp_path, capsys):
    before = previous_choice(tmp_path, True)
    assert command("--apply", case.consent) == 0
    lines = capsys.readouterr().out.splitlines()
    undo = next(line.removeprefix("Undo: ") for line in lines if line.startswith("Undo: "))
    stored = case.root / "ComfyUI" / "my-work.txt"
    stored.write_text("new stored work")
    assert cli.main(shlex.split(undo)[2:]) == 0
    assert_previous(before)
    assert stored.read_text() == "new stored work"
    assert (tmp_path / "previous" / "work.txt").read_text() == "stored work"
    assert case.fake.work_calls == (1, 1)


def test_changed_target_before_first_commit_is_not_overwritten(tmp_path, installed, pin, monkeypatch):
    before = previous_choice(tmp_path, True)
    selected = targets.TargetRecord(endpoint="http://127.0.0.1:9000", layout="git", prefer="attached")
    persist = portable._write_attachment

    def intervene(*args, **kwargs):
        record = persist(*args, **kwargs)
        if args[2] == "prepared":
            targets.save_target(selected)
        return record

    monkeypatch.setattr(portable, "_write_attachment", intervene)
    with pytest.raises(portable.PortableError) as error:
        portable.attach_installation(installed, pin=pin)
    assert error.value.code == "attachment_recovery_required"
    assert targets.read_target() == selected
    assert settings.placement_default() == before[1]


@pytest.mark.integration
@pytest.mark.parametrize("present", [False, True])
@pytest.mark.parametrize("crash", [None, "target", "placement"])
def test_printed_undo_in_fresh_cli_process_uses_real_runtime(
    tmp_path, installed, pin, real_runtime, present, crash,
):
    from astrid.sdk.workspace_client import WorkspaceClient
    from tests.helpers.runtime import vendored_runtime_commit

    daemon = real_runtime(commit=vendored_runtime_commit())
    client = WorkspaceClient(daemon.endpoint, daemon.token)
    with settings.using(client):
        before = previous_choice(tmp_path, present)
        connection = tmp_path / "prepared-connection.json"
        connection.write_text(json.dumps({
            "endpoint": daemon.endpoint, "credential_file": str(daemon.credential_path),
            "realm_id": daemon.service.realm["id"], "actor_id": "owner",
        }))
        env = {**os.environ, "ASTRID_PREPARED_CONNECTION": str(connection),
               "HOME": str(Path.home()), "USERPROFILE": str(Path.home())}
        attach_code = """
import json, os, sys
from astrid.core.generation import comfy_portable, settings
request = json.load(sys.stdin)
if request['crash']:
    real_set = settings.set_placement
    def disappear(url, client=None):
        if request['crash'] == 'placement':
            real_set(url, client)
        os._exit(73)
    settings.set_placement = disappear
print(json.dumps(comfy_portable.attach_installation(request['installed'], pin=request['pin'])))
"""

        def attach_child(crash):
            return subprocess.run([sys.executable, "-c", attach_code], env=env,
                                  input=json.dumps({"installed": installed, "pin": pin, "crash": crash}),
                                  text=True, capture_output=True, timeout=30)

        child = attach_child(crash)
        if crash:
            assert child.returncode == 73, child.stdout + child.stderr
            pending = json.loads((tmp_path / ".astrid-install" / "attachment.json").read_text())
            assert pending["phase"] == "prepared"
            assert pending["previous"] == {"target": before[0], "placement": before[1]}
            child = attach_child(None)
        assert child.returncode == 0, child.stdout + child.stderr
        attached = json.loads(child.stdout)
        assert attached["previous_placement"] == before[1]
        # The writer process has exited. Only the printed command and the
        # durable receipt carry recovery into this second public CLI process.
        argv = shlex.split(attached["undo"])
        child = subprocess.run([sys.executable, "-m", "astrid", *argv[1:], "--json"],
                               env=env, text=True, capture_output=True, timeout=30)
        assert child.returncode == 0, child.stdout + child.stderr
        assert json.loads(child.stdout)["status"] == "restored"
        assert_previous(before)
        assert Path(installed["root"]).is_dir()
