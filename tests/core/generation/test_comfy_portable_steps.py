"""CPU-only proof of the real Portable steps using the local release fixture."""

import hashlib
import io
import json
import stat
import subprocess
import zipfile

import httpx
import pytest

from astrid.core.generation import comfy_portable as portable
from tests.core.generation.environment_states import (
    fake_disk_usage,
    fake_gpu,
    make_zip,
    serve_release,
    snapshot,
)

INNER = "ComfyUI_windows_portable"
REQUIRED = ["python_embeded/python.exe", "ComfyUI/main.py"]
ARCHIVE = "portable.zip"
ENTRIES = {
    f"{INNER}/{REQUIRED[0]}": b"python executable",
    f"{INNER}/{REQUIRED[1]}": b"comfy main",
}


@pytest.fixture
def payload():
    return make_zip(ENTRIES)


def pin_for(base_url, data, *, archive_format="zip", extracted_bytes=None):
    # Keep the production HTTPS pin contract. The client hook below connects
    # only the synthetic test URL to the actual local HTTP fixture.
    url = base_url.replace("http://", "https://", 1)
    return {
        "schema": "astrid.comfy-portable-pin.v1",
        "platform": "win32",
        "note": "Synthetic local release for tests only.",
        "supported_gpu_patterns": [r"RTX (20|30|40|50)\d\d"],
        "release": {
            "tag": "test-release",
            "url": f"{url}/{ARCHIVE}",
            "archive_name": ARCHIVE,
            "archive_format": archive_format,
            "archive_bytes": len(data),
            "archive_sha256": hashlib.sha256(data).hexdigest(),
            "extracted_bytes": (
                sum(map(len, ENTRIES.values())) if extracted_bytes is None else extracted_bytes
            ),
            "estimated_minutes": 1,
            "inner_directory": INNER,
            "required_files": REQUIRED.copy(),
            "start_script": "run_nvidia_gpu.bat",
            "launch_args": ["--listen", "127.0.0.1"],
        },
    }


def local_client(base_url):
    """Use a real httpx socket connection to serve_release, not a fake response."""
    local = httpx.URL(base_url)

    def route(request):
        assert request.url.host == local.host
        assert request.url.port == local.port
        request.url = request.url.copy_with(scheme=local.scheme)

    return httpx.Client(trust_env=False, event_hooks={"request": [route]})


@pytest.fixture
def archive_paths(tmp_path):
    folder = tmp_path / "archive"
    folder.mkdir()
    return folder / ARCHIVE, folder / (ARCHIVE + ".part")


def test_download_whole_archive(payload, archive_paths):
    archive, part = archive_paths
    events = []
    with serve_release({ARCHIVE: payload}) as (base, server), local_client(base) as client:
        portable.download_archive(
            pin_for(base, payload), archive, part, lambda *event: events.append(event),
            client=client,
        )
        assert server.requests == [(f"/{ARCHIVE}", None)]
    assert archive.read_bytes() == payload
    assert list(archive.parent.iterdir()) == [archive]
    assert events[-1] == ("download", len(payload), len(payload))


def test_interrupted_download_keeps_prefix_then_resumes(payload, archive_paths):
    archive, part = archive_paths
    cut = len(payload) // 2
    with serve_release({ARCHIVE: payload}, break_after=cut) as (base, server):
        pin = pin_for(base, payload)
        with local_client(base) as client:
            with pytest.raises(portable.PortableError) as caught:
                portable.download_archive(pin, archive, part, None, client=client)
            assert caught.value.code == "download_failed"
            assert "rerun" in str(caught.value)
            assert not archive.exists()
            assert part.read_bytes() == payload[:cut]
            assert list(archive.parent.iterdir()) == [part]
            events = []
            portable.download_archive(
                pin, archive, part, lambda *event: events.append(event), client=client,
            )
        assert server.requests == [(f"/{ARCHIVE}", None), (f"/{ARCHIVE}", f"bytes={cut}-")]
    assert archive.read_bytes() == payload
    assert list(archive.parent.iterdir()) == [archive]
    assert events[-1] == ("download", len(payload), len(payload))


@pytest.mark.parametrize("wrong_size", [False, True])
def test_bad_archive_is_refused_and_removed(payload, archive_paths, wrong_size):
    archive, part = archive_paths
    bad = payload + b"x" if wrong_size else bytes([payload[0] ^ 1]) + payload[1:]
    with serve_release({ARCHIVE: bad}) as (base, _), local_client(base) as client:
        with pytest.raises(portable.PortableError) as caught:
            portable.download_archive(pin_for(base, payload), archive, part, None, client=client)
    assert caught.value.code == "archive_mismatch"
    assert ARCHIVE in str(caught.value)
    assert "refused and removed" in str(caught.value)
    assert not archive.exists()
    assert not part.exists()
    assert list(archive.parent.iterdir()) == []


def test_good_zip_and_required_files(tmp_path, payload, archive_paths):
    archive, _ = archive_paths
    archive.write_bytes(payload)
    staging = tmp_path / "staging"
    pin = pin_for("https://example.invalid", payload)
    portable.extract_archive(archive, staging, pin)
    assert portable.missing_files(staging / INNER, pin) == []
    for name, data in ENTRIES.items():
        assert (staging / name).read_bytes() == data

    root = staging / INNER
    (root / REQUIRED[0]).write_bytes(b"")
    (root / REQUIRED[1]).unlink()
    (root / REQUIRED[1]).mkdir()
    assert portable.missing_files(root, pin) == sorted(REQUIRED)


@pytest.mark.parametrize(
    "name", ["../evil", "C:/evil", r"..\evil", "safe/../../evil"],
)
def test_zip_refuses_escaping_member(tmp_path, archive_paths, name):
    archive, _ = archive_paths
    data = make_zip({**ENTRIES, name: b"evil"})
    archive.write_bytes(data)
    before = snapshot(tmp_path)
    staging = tmp_path / "staging"
    with pytest.raises(portable.PortableError) as caught:
        portable.extract_archive(
            archive, staging, pin_for("https://example.invalid", data, extracted_bytes=10_000),
        )
    assert caught.value.code == "unsafe_archive"
    assert {k: v for k, v in snapshot(tmp_path).items() if not k.startswith("staging")} == before
    assert list(staging.iterdir()) == []


def test_zip_refuses_absolute_member_targeting_real_outside_file(tmp_path, archive_paths):
    archive, _ = archive_paths
    outside = tmp_path / "personal.txt"
    outside.write_bytes(b"keep me")
    data = make_zip({outside.as_posix(): b"overwrite"})
    archive.write_bytes(data)
    staging = tmp_path / "staging"
    with pytest.raises(portable.PortableError) as caught:
        portable.extract_archive(
            archive, staging, pin_for("https://example.invalid", data, extracted_bytes=10_000),
        )
    assert caught.value.code == "unsafe_archive"
    assert outside.read_bytes() == b"keep me"
    assert list(staging.iterdir()) == []


def test_zip_refuses_symlink_member(tmp_path, archive_paths):
    archive, _ = archive_paths
    outside = tmp_path / "personal.txt"
    outside.write_bytes(b"keep me")
    buffer = io.BytesIO()
    link = zipfile.ZipInfo(f"{INNER}/link")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(buffer, "w") as zipped:
        zipped.writestr(link, str(outside))
    data = buffer.getvalue()
    archive.write_bytes(data)
    staging = tmp_path / "staging"
    with pytest.raises(portable.PortableError) as caught:
        portable.extract_archive(
            archive, staging, pin_for("https://example.invalid", data, extracted_bytes=10_000),
        )
    assert caught.value.code == "unsafe_archive"
    assert outside.read_bytes() == b"keep me"
    assert list(staging.iterdir()) == []


def test_zip_refuses_unpacked_size_bomb(tmp_path, archive_paths):
    archive, _ = archive_paths
    data = make_zip({"big.txt": b"x" * 111})
    archive.write_bytes(data)
    staging = tmp_path / "staging"
    with pytest.raises(portable.PortableError) as caught:
        portable.extract_archive(
            archive, staging, pin_for("https://example.invalid", data, extracted_bytes=100),
        )
    assert caught.value.code == "unsafe_archive"
    assert list(staging.iterdir()) == []


def test_7z_invokes_tar_with_exact_arguments(tmp_path, archive_paths, monkeypatch):
    archive, _ = archive_paths
    archive.write_bytes(b"test")
    staging = tmp_path / "staging"
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        for name, data in ENTRIES.items():
            path = staging / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)

    monkeypatch.setattr(portable.shutil, "which", lambda name: "test-tar" if name == "tar" else None)
    monkeypatch.setattr(portable.subprocess, "run", run)
    pin = pin_for("https://example.invalid", b"test", archive_format="7z")
    portable.extract_archive(archive, staging, pin)
    assert calls == [
        (["test-tar", "-xf", str(archive), "-C", str(staging)],
         {"check": True, "stdin": subprocess.DEVNULL, "timeout": 3600}),
    ]
    assert portable.missing_files(staging / INNER, pin) == []


def test_7z_without_tar_reports_no_extractor(tmp_path, archive_paths, monkeypatch):
    archive, _ = archive_paths
    archive.write_bytes(b"test")
    monkeypatch.setattr(portable.shutil, "which", lambda name: None)
    monkeypatch.setattr(portable.subprocess, "run", lambda *a, **k: pytest.fail("unexpected tar"))
    pin = pin_for("https://example.invalid", b"test", archive_format="7z")
    with pytest.raises(portable.PortableError) as caught:
        portable.extract_archive(archive, tmp_path / "staging", pin)
    assert caught.value.code == "no_extractor"
    assert "tar" in str(caught.value)


def test_7z_refuses_symlink_left_by_tar(tmp_path, archive_paths, monkeypatch):
    archive, _ = archive_paths
    archive.write_bytes(b"test")
    outside = tmp_path / "personal.txt"
    outside.write_bytes(b"keep me")
    staging = tmp_path / "staging"

    def run(argv, **kwargs):
        (staging / "link").symlink_to(outside)

    monkeypatch.setattr(portable.shutil, "which", lambda name: "test-tar")
    monkeypatch.setattr(portable.subprocess, "run", run)
    pin = pin_for("https://example.invalid", b"test", archive_format="7z")
    with pytest.raises(portable.PortableError) as caught:
        portable.extract_archive(archive, staging, pin)
    assert caught.value.code == "unsafe_archive"
    assert outside.read_bytes() == b"keep me"


@pytest.mark.parametrize("interrupted", [False, True])
def test_install_with_default_steps(tmp_path, payload, interrupted):
    destination = tmp_path / "destination"
    metadata = destination / ".astrid-install"
    cut = len(payload) // 2
    with serve_release(
        {ARCHIVE: payload}, break_after=cut if interrupted else None,
    ) as (base, server), local_client(base) as client:
        pin = pin_for(base, payload)
        kwargs = {
            "pin": pin,
            "platform": "win32",
            "gpu": fake_gpu("rtx4090"),
            "disk_usage": fake_disk_usage(30_000_000_000),
        }
        offer = portable.build_offer(destination, **kwargs)
        assert offer["status"] == "offered"

        def install():
            return portable.install_portable(
                destination, offer["offer_sha256"], steps=portable.default_steps(client), **kwargs,
            )

        if interrupted:
            with pytest.raises(portable.PortableError) as caught:
                install()
            assert caught.value.code == "download_failed"
            assert json.loads((metadata / "journal.json").read_text())["phase"] == "started"
            assert (metadata / "archive" / (ARCHIVE + ".part")).read_bytes() == payload[:cut]
            assert not (destination / INNER).exists()

        result = install()
        requests = [(f"/{ARCHIVE}", None)]
        if interrupted:
            requests.append((f"/{ARCHIVE}", f"bytes={cut}-"))
        assert server.requests == requests
    assert result["status"] == "installed"
    assert result["resumed_from"] == ("started" if interrupted else None)
    assert json.loads((metadata / "journal.json").read_text())["phase"] == "complete"
    assert (metadata / "receipt.json").is_file()
    assert list((metadata / "archive").iterdir()) == []
    assert portable.missing_files(destination / INNER, pin) == []
    for name, data in ENTRIES.items():
        assert (destination / name).read_bytes() == data
