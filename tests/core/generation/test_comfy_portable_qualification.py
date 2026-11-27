"""94-C CPU qualification regressions; fixtures are not Windows archive receipts.

These tests deliberately exercise the public consent/undo contract on the
existing synthetic machine. A failure on the recovery base is evidence for
the owning author, not permission to weaken it or ship a production pin.
"""

from __future__ import annotations

import copy
import json
import shlex
from contextlib import nullcontext

import pytest

from astrid.core.generation import cli, comfy_portable as portable, settings, targets
from tests.core.generation.environment_states import snapshot
from tests.core.generation.test_comfy_portable_cli import command  # noqa: F401
from tests.core.generation.test_comfy_portable_install import case, apply  # noqa: F401


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("url", "https://example.invalid/different-source.zip"),
        ("archive_sha256", "b" * 64),
        ("archive_name", "different.zip"),
        ("archive_format", "7z"),
        ("inner_directory", "different-root"),
        ("start_script", "different-start.bat"),
        ("launch_args", ["--listen", "0.0.0.0"]),
        ("required_files", ["python_embeded/python.exe", "ComfyUI/main.py", "run_nvidia_gpu.bat"]),
        ("estimated_minutes", 2),
        ("archive_bytes", 101),
        ("extracted_bytes", 301),
    ],
)
def test_changed_release_needs_fresh_consent_before_any_install_write(tmp_path, case, field, value):
    before = snapshot(tmp_path)
    changed = copy.deepcopy(case.pin)
    changed["release"][field] = value
    new_offer = portable.build_offer(case.destination, **{**case.kwargs, "pin": changed})
    assert new_offer["status"] == "offered"
    assert new_offer["cost_usd"] == 0
    assert snapshot(tmp_path) == before

    # The refusal must occur at consent validation, not after creating a
    # journal or starting a transfer. No actual upstream archive is used.
    with pytest.raises(portable.PortableError) as error:
        apply(case, pin=changed)
    assert error.value.code == "consent_mismatch"
    assert new_offer["offer_sha256"] != case.consent
    assert case.fake.work_calls == (0, 0)
    assert snapshot(tmp_path) == before
    assert not case.destination.exists()


def test_changed_byte_cost_needs_fresh_consent_even_at_same_peak(tmp_path, case):
    original = copy.deepcopy(case.pin)
    original["release"]["extracted_bytes"] = 20
    changed = copy.deepcopy(original)
    changed["release"]["extracted_bytes"] = 21
    assert portable.peak_bytes(original["release"]) == portable.peak_bytes(changed["release"])
    first = portable.build_offer(case.destination, **{**case.kwargs, "pin": original})
    second = portable.build_offer(case.destination, **{**case.kwargs, "pin": changed})
    assert first["status"] == second["status"] == "offered"
    assert first["peak_bytes"] == second["peak_bytes"]
    assert first["installed_bytes"] != second["installed_bytes"]
    assert first["cost_usd"] == second["cost_usd"] == 0

    before = snapshot(tmp_path)
    with pytest.raises(portable.PortableError) as error:
        portable.install_portable(
            case.destination, first["offer_sha256"], steps=case.fake.steps,
            **{**case.kwargs, "pin": changed},
        )
    assert error.value.code == "consent_mismatch"
    assert second["offer_sha256"] != first["offer_sha256"]
    assert case.fake.work_calls == (0, 0)
    assert snapshot(tmp_path) == before


def test_changed_destination_needs_fresh_consent_before_any_write(tmp_path, case):
    other = case.destination.parent / "Another Portable"
    before = snapshot(tmp_path)
    offer = portable.build_offer(other, **case.kwargs)
    assert offer["status"] == "offered"
    assert offer["offer_sha256"] != case.consent
    with pytest.raises(portable.PortableError) as error:
        portable.install_portable(other, case.consent, steps=case.fake.steps, **case.kwargs)
    assert error.value.code == "consent_mismatch"
    assert snapshot(tmp_path) == before
    assert case.fake.work_calls == (0, 0)


def test_missing_release_refusal_independent_of_production_pin(tmp_path, case):
    # This negative must remain executable after a real production pin lands.
    missing = {**case.pin, "release": None}
    before = snapshot(tmp_path)
    offer = portable.build_offer(case.destination, **{**case.kwargs, "pin": missing})
    assert offer["status"] == "refused"
    assert offer["refusal"]["code"] == "no_verified_release"
    with pytest.raises(portable.PortableError) as error:
        portable.install_portable(
            case.destination, offer["offer_sha256"], steps=case.fake.steps,
            **{**case.kwargs, "pin": missing},
        )
    assert error.value.code == "no_verified_release"
    assert snapshot(tmp_path) == before
    assert case.fake.work_calls == (0, 0)


def _previous_choice(tmp_path):
    root = tmp_path / "previous-desktop"
    root.mkdir()
    personal = root / "personal.txt"
    personal.write_bytes(b"previous installation belongs to its person")
    previous = targets.TargetRecord(
        layout="desktop", endpoint="http://127.0.0.1:8190", root=str(root), prefer="attached",
    )
    targets.save_target(previous)
    placement = settings.set_placement(previous.endpoint)["placement"]
    assert targets.read_target() == previous
    assert placement == {"kind": "attached", "endpoint": previous.endpoint}
    return previous, placement, personal


def test_reported_undo_actually_restores_nonempty_previous_choice(command, case, capsys, tmp_path, monkeypatch):
    previous, placement, personal = _previous_choice(tmp_path)
    assert command("--json") == 0
    consent = json.loads(capsys.readouterr().out)["offer_sha256"]
    assert command("--apply", consent, "--json") == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["previous_target"] == previous.as_dict()
    assert targets.read_target().layout == "portable"
    assert settings.placement_default()["endpoint"] == payload["endpoint"]

    # Exercise what the actual CLI tells the user to run. The existing test
    # settings store remains in memory; no Runtime or real Windows claim.
    argv = shlex.split(payload["undo"])
    assert argv[:2] == ["astrid", "generate"]
    monkeypatch.setattr(cli, "_open_client", lambda: nullcontext(None))
    assert cli.main(argv[2:]) == 0
    assert settings.placement_default() == placement
    assert targets.read_target() == previous
    assert personal.read_bytes() == b"previous installation belongs to its person"


@pytest.mark.parametrize("after_placement_write", [False, True])
def test_interrupted_attachment_preserves_previous_choice(
    command, case, capsys, tmp_path, monkeypatch, after_placement_write,
):
    previous, placement, personal = _previous_choice(tmp_path)
    result = apply(case)
    real_set = settings.set_placement
    calls = 0

    def fail_once(url, client=None):
        nonlocal calls
        calls += 1
        if calls != 1:
            return real_set(url, client)
        if after_placement_write:
            real_set(url, client)
        raise OSError("injected interruption at placement commit")

    monkeypatch.setattr(settings, "set_placement", fail_once)
    with pytest.raises((OSError, portable.PortableError)):
        portable.attach_installation(result, pin=case.pin)

    assert calls >= 1
    assert targets.read_target() == previous
    assert settings.placement_default() == placement
    assert personal.read_bytes() == b"previous installation belongs to its person"
    assert case.root.is_dir()  # Verified installation is not discarded.
    assert case.fake.work_calls == (1, 1)
