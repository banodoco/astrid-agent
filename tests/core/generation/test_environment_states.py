"""Contract tests for the shared CPU environment state fixtures."""

import io
from http.client import IncompleteRead
from urllib.request import Request, urlopen
import zipfile

import pytest

from tests.core.generation.environment_states import (
    GIB,
    clean_machine,
    fake_disk_usage,
    fake_gpu,
    foreign_destination,
    make_zip,
    no_comfyui,
    serve_release,
    snapshot,
)


@pytest.mark.parametrize(
    ("kind", "device"),
    [
        ("rtx5070ti", "NVIDIA GeForce RTX 5070 Ti Laptop GPU"),
        ("rtx4090", "NVIDIA GeForce RTX 4090"),
        ("gtx1060", "NVIDIA GeForce GTX 1060"),
    ],
)
def test_fake_available_gpu(kind, device):
    assert fake_gpu(kind) == {"available": True, "devices": [device], "reason": ""}
    fake_gpu(kind)["devices"].clear()
    assert fake_gpu(kind)["devices"] == [device]


def test_fake_missing_and_unknown_gpu():
    assert fake_gpu("none") == {
        "available": False,
        "devices": [],
        "reason": "No NVIDIA GPU was detected because nvidia-smi is not on PATH.",
    }
    with pytest.raises(KeyError):
        fake_gpu("unknown")


def test_fake_disk_usage(tmp_path):
    disk = fake_disk_usage(7 * GIB)
    result = disk(tmp_path)
    assert result == (2000 * GIB, 1993 * GIB, 7 * GIB)
    assert (result.total, result.used, result.free) == result
    assert disk(tmp_path / "absent") == result
    assert fake_disk_usage(3, 11)(tmp_path) == (11, 8, 3)


@pytest.mark.parametrize("builder", [clean_machine, no_comfyui])
def test_clean_machine(builder, tmp_path):
    machine = builder(tmp_path)
    assert set(tmp_path.iterdir()) == {machine.home, machine.localappdata}
    assert machine.home.is_dir() and not list(machine.home.iterdir())
    assert machine.localappdata.is_dir() and not list(machine.localappdata.iterdir())
    assert machine.destination == machine.localappdata / "Astrid" / "ComfyUI"
    assert not machine.destination.exists()
    assert not (machine.home / ".vibecomfy").exists()
    assert machine.environ == {
        "LOCALAPPDATA": str(machine.localappdata),
        "HOME": str(machine.home),
        "USERPROFILE": str(machine.home),
    }


def test_foreign_destination_and_snapshot(tmp_path):
    foreign = foreign_destination(tmp_path)
    assert foreign.is_dir()
    note = foreign / "notes.txt"
    assert list(foreign.iterdir()) == [note]
    assert note.is_file()
    assert snapshot(tmp_path / "absent") == {}
    before = snapshot(tmp_path)
    assert set(before) == {foreign.name, f"{foreign.name}/notes.txt"}
    stat = note.stat()
    assert before[f"{foreign.name}/notes.txt"] == (stat.st_size, stat.st_mtime_ns)
    note.read_bytes()
    assert snapshot(tmp_path) == before
    note.write_bytes(b"changed" * 100)
    assert snapshot(tmp_path) != before


def test_make_zip_round_trip():
    entries = {"folder/file.txt": b"hello", "binary": bytes(range(256))}
    with zipfile.ZipFile(io.BytesIO(make_zip(entries))) as archive:
        assert set(archive.namelist()) == set(entries)
        assert all(info.compress_type == zipfile.ZIP_DEFLATED for info in archive.infolist())
        assert {name: archive.read(name) for name in entries} == entries


def test_serve_release_whole_and_range():
    data = b"0123456789"
    with serve_release({"release.zip": data}) as (url, server):
        with urlopen(f"{url}/release.zip", timeout=5) as response:
            assert response.status == 200
            assert int(response.headers["Content-Length"]) == len(data)
            assert response.read() == data
        request = Request(f"{url}/release.zip", headers={"Range": "bytes=3-"})
        with urlopen(request, timeout=5) as response:
            assert response.status == 206
            assert response.headers["Content-Range"] == "bytes 3-9/10"
            assert int(response.headers["Content-Length"]) == 7
            assert response.read() == data[3:]
        assert server.requests == [("/release.zip", None), ("/release.zip", "bytes=3-")]


def test_serve_release_interrupt_and_resume():
    data = b"0123456789"
    with serve_release({"a.zip": data, "b.zip": data}, break_after=4) as (url, server):
        for name in ("a.zip", "b.zip"):
            with urlopen(f"{url}/{name}", timeout=5) as response:
                assert int(response.headers["Content-Length"]) == len(data)
                with pytest.raises(IncompleteRead) as error:
                    response.read()
                prefix = error.value.partial
                assert prefix == data[:4]
            request = Request(f"{url}/{name}", headers={"Range": f"bytes={len(prefix)}-"})
            with urlopen(request, timeout=5) as response:
                assert response.status == 206
                assert response.headers["Content-Range"] == "bytes 4-9/10"
                assert prefix + response.read() == data
            with urlopen(f"{url}/{name}", timeout=5) as response:
                assert response.read() == data
        assert server.requests == [
            (f"/{name}", header)
            for name in ("a.zip", "b.zip")
            for header in (None, "bytes=4-", None)
        ]
