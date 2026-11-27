"""CPU-only proof of the Portable install consent boundary."""

from __future__ import annotations

import copy
import hashlib
import json

import pytest

from astrid.core.generation.comfy_portable import (
    MARGIN_BYTES,
    PortableError,
    build_offer,
    default_destination,
    destination_state,
    load_pin,
    peak_bytes,
    render_offer,
)
from tests.core.generation.environment_states import (
    clean_machine,
    fake_disk_usage,
    fake_gpu,
    foreign_destination,
    snapshot,
)


@pytest.fixture
def pin():
    # Synthetic metadata only; never used by the product's shipped pin.
    return {
        "schema": "astrid.comfy-portable-pin.v1",
        "platform": "win32",
        "supported_gpu_patterns": [r"RTX (20|30|40|50)\d\d"],
        "note": "Synthetic unit test release, not an actual pin.",
        "release": {
            "tag": "test-release",
            "url": "https://example.invalid/test-portable.zip",
            "archive_name": "test-portable.zip",
            "archive_format": "zip",
            "archive_bytes": 4_000_000_000,
            "archive_sha256": "a" * 64,
            "extracted_bytes": 9_000_000_000,
            "estimated_minutes": 15,
            "inner_directory": "ComfyUI_windows_portable",
            "required_files": ["python_embeded/python.exe", "ComfyUI/main.py"],
            "start_script": "run_nvidia_gpu.bat",
            "launch_args": ["--listen", "127.0.0.1"],
        },
    }


@pytest.fixture
def machine(tmp_path):
    return clean_machine(tmp_path)


@pytest.fixture
def offer_for(tmp_path, machine, pin):
    def offer_for(destination=machine.destination, **overrides):
        kwargs = {
            "pin": pin,
            "platform": "win32",
            "gpu": fake_gpu("rtx4090"),
            "disk_usage": fake_disk_usage(30_000_000_000),
            "environ": machine.environ,
        }
        kwargs.update(overrides)
        before = snapshot(tmp_path)
        offer = build_offer(destination, **kwargs)
        rendered = render_offer(offer)
        assert snapshot(tmp_path) == before
        assert len(offer["offer_sha256"]) == 64
        assert offer["cost_usd"] == 0
        if offer["status"] == "refused":
            assert offer["refusal"]["works"]
            assert rendered == (
                f"{offer['refusal']['message']}\n"
                f"What works: {offer['refusal']['works']}\nNothing was written."
            )
        return offer
    return offer_for


def test_shipped_pin_refuses(machine, offer_for):
    shipped = load_pin()
    assert shipped == {
        "schema": "astrid.comfy-portable-pin.v1",
        "platform": "win32",
        "supported_gpu_patterns": [r"RTX (20|30|40|50)\d\d"],
        "release": None,
        "note": (
            "No release is pinned until its URL, size and SHA-256 are taken from the official "
            "release page (plan 94, ER-L0). Until then the install refuses."
        ),
    }
    offer = offer_for(pin=shipped)
    assert offer["refusal"]["code"] == "no_verified_release"
    assert offer["release"] is None
    assert offer["download_bytes"] is None
    assert not machine.destination.exists()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("tag", None),
        ("url", "http://example.invalid/archive.zip"),
        ("url", "https:///archive.zip"),
        ("archive_sha256", "z" * 64),
        ("archive_bytes", True),
        ("extracted_bytes", 0),
        ("estimated_minutes", -1),
        ("archive_format", "tar"),
        ("archive_name", "../archive.zip"),
        ("inner_directory", "/absolute"),
        ("required_files", ["python_embeded/python.exe"]),
        ("required_files", ["python_embeded/python.exe", "ComfyUI/main.py", "../outside"]),
        ("start_script", r"..\outside.bat"),
        ("launch_args", "not a list"),
    ],
)
def test_load_pin_rejects_invalid_release(tmp_path, pin, field, value):
    if value is None:
        del pin["release"][field]
    else:
        pin["release"][field] = value
    path = tmp_path / "pin.json"
    path.write_text(json.dumps(pin), encoding="utf-8")
    before = snapshot(tmp_path)
    with pytest.raises(PortableError) as caught:
        load_pin(path)
    assert caught.value.code == "pin_invalid"
    assert snapshot(tmp_path) == before


def test_load_valid_pin_and_peak(tmp_path, pin):
    path = tmp_path / "pin.json"
    path.write_text(json.dumps(pin), encoding="utf-8")
    before = snapshot(tmp_path)
    assert load_pin(path) == pin
    assert peak_bytes(pin["release"]) == 13_000_000_000 + MARGIN_BYTES
    assert peak_bytes({"archive_bytes": 10, "extracted_bytes": 1}) == 20 + MARGIN_BYTES
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize("kind", ["rtx5070ti", "rtx4090"])
def test_offered_exact_consent(machine, pin, offer_for, kind):
    gpu = fake_gpu(kind)
    offer = offer_for(gpu=gpu)
    expected_peak = 13_000_000_000 + MARGIN_BYTES
    identity = {
        "destination": str(machine.destination),
        "release": "test-release",
        "archive_sha256": "a" * 64,
        "platform": "win32",
        "peak_bytes": expected_peak,
        "gpu_devices": gpu["devices"],
    }
    digest = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    assert offer == {
        "schema": "astrid.comfy-install-offer.v1",
        "status": "offered",
        "refusal": None,
        "destination": str(machine.destination),
        "release": pin["release"]["tag"],
        "download_bytes": 4_000_000_000,
        "installed_bytes": 9_000_000_000,
        "peak_bytes": expected_peak,
        "free_bytes": 30_000_000_000,
        "minutes": 15,
        "cost_usd": 0,
        "state": "absent",
        "offer_sha256": digest,
    }
    assert render_offer(offer) == (
        f"No working ComfyUI here. I can install one in {machine.destination}: about "
        "4.0 GB to download, 14.1 GB of free disk needed at the peak, about 15 minutes, "
        "nothing else on your computer touched, no cost. It becomes the ComfyUI Astrid uses, "
        "and your current choice is kept so you can switch back. Go ahead?\n"
        f"To confirm: astrid generate install-comfyui --destination {machine.destination} --apply {digest}"
    )
    assert not machine.destination.exists()


@pytest.mark.parametrize(
    ("platform", "gpu", "code", "guidance"),
    [
        ("linux", "none", "unsupported_platform", "astrid engines install comfyui --platform linux-x86_64-cu130"),
        ("darwin", "none", "unsupported_platform", "own running ComfyUI"),
        ("win32", "none", "no_gpu", "RTX 20, 30, 40 or 50"),
        ("win32", "gtx1060", "unsupported_gpu", "RTX 20, 30, 40 or 50"),
    ],
)
def test_host_refusals(offer_for, platform, gpu, code, guidance):
    offer = offer_for(platform=platform, gpu=fake_gpu(gpu))
    assert offer["status"] == "refused"
    assert offer["refusal"]["code"] == code
    assert guidance in offer["refusal"]["works"]
    assert offer["download_bytes"] == 4_000_000_000
    if gpu == "gtx1060":
        assert "GTX 1060" in offer["refusal"]["message"]


def test_one_unsupported_card_refuses_all(offer_for):
    gpu = fake_gpu("rtx4090")
    gpu["devices"] += fake_gpu("gtx1060")["devices"]
    assert offer_for(gpu=gpu)["refusal"]["code"] == "unsupported_gpu"


def test_invalid_destination(tmp_path, offer_for):
    assert offer_for("relative/path")["refusal"]["code"] == "destination_invalid"
    file = tmp_path / "file"
    file.write_text("keep", encoding="utf-8")
    assert offer_for(file)["refusal"]["code"] == "destination_invalid"


def test_foreign_destination(tmp_path, offer_for):
    destination = foreign_destination(tmp_path)
    offer = offer_for(destination)
    assert offer["refusal"]["code"] == "destination_occupied"
    assert offer["state"] == "foreign"
    assert "Nothing was touched" in offer["refusal"]["message"]
    assert (destination / "notes.txt").read_text(encoding="utf-8") == "My personal notes.\n"


def test_low_space_and_nearest_ancestor(machine, pin, offer_for):
    checked = []

    def usage(path):
        checked.append(path)
        return fake_disk_usage(100)(path)

    offer = offer_for(disk_usage=usage)
    assert checked == [machine.localappdata]
    assert offer["refusal"]["code"] == "insufficient_space"
    assert offer["free_bytes"] == 100
    assert "100 bytes" in offer["refusal"]["message"]
    assert str(peak_bytes(pin["release"])) in offer["refusal"]["message"]
    assert offer_for(disk_usage=fake_disk_usage(peak_bytes(pin["release"])))["status"] == "offered"


def test_digest_stability_and_decline(machine, pin, offer_for):
    first = offer_for()
    assert offer_for(disk_usage=fake_disk_usage(99_000_000_000))["offer_sha256"] == first["offer_sha256"]
    assert offer_for(machine.destination.parent / "Other")["offer_sha256"] != first["offer_sha256"]
    changed = copy.deepcopy(pin)
    changed["release"]["tag"] = "other"
    assert offer_for(pin=changed)["offer_sha256"] != first["offer_sha256"]
    changed = copy.deepcopy(pin)
    changed["release"]["archive_sha256"] = "b" * 64
    assert offer_for(pin=changed)["offer_sha256"] != first["offer_sha256"]
    assert offer_for(gpu=fake_gpu("rtx5070ti"))["offer_sha256"] != first["offer_sha256"]
    assert offer_for() == first
    assert not machine.destination.exists()


def test_default_and_destination_states(tmp_path, machine, offer_for):
    before = snapshot(tmp_path)
    assert default_destination(machine.environ, "win32") == machine.destination
    assert default_destination({"HOME": str(machine.home)}, "win32") == (
        machine.home / "AppData" / "Local" / "Astrid" / "ComfyUI"
    )
    assert destination_state(machine.destination) == "absent"
    assert offer_for(None)["destination"] == str(machine.destination)
    assert snapshot(tmp_path) == before

    machine.destination.mkdir(parents=True)
    assert offer_for()["state"] == "empty"
    journal = machine.destination / ".astrid-install" / "journal.json"
    journal.parent.mkdir()
    journal.write_text('{"schema":"astrid.comfy-install-journal.v1"}', encoding="utf-8")
    assert offer_for()["state"] == "ours"
    journal.write_text("not JSON", encoding="utf-8")
    assert offer_for()["refusal"]["code"] == "destination_occupied"


def test_first_refusal_stops_before_disk(machine, pin, offer_for):
    unpinned = {**pin, "release": None}

    def no_disk(_):
        pytest.fail("refusal must not probe disk")

    assert offer_for("relative", pin=unpinned, platform="linux", gpu=fake_gpu("none"),
                     disk_usage=no_disk)["refusal"]["code"] == "unsupported_platform"
    assert offer_for("relative", pin=unpinned, gpu=fake_gpu("none"),
                     disk_usage=no_disk)["refusal"]["code"] == "no_gpu"
    assert offer_for("relative", pin=unpinned, gpu=fake_gpu("gtx1060"),
                     disk_usage=no_disk)["refusal"]["code"] == "unsupported_gpu"
    assert offer_for("relative", pin=unpinned, disk_usage=no_disk)["refusal"]["code"] == "no_verified_release"
    assert offer_for("relative", disk_usage=no_disk)["refusal"]["code"] == "destination_invalid"
    assert not machine.destination.exists()
