"""Archive checksum pairing, validation and real ZIP extraction regressions."""

from __future__ import annotations

import copy
import hashlib
import io
import zipfile
from pathlib import Path

import pytest

from astrid.installer import tools


def archive(name: str, content: bytes) -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as bundle:
        bundle.writestr(name, content)
    return stream.getvalue()


def asset():
    archives = [archive("ffmpeg", b"encoder"), archive("ffprobe", b"probe")]
    return ({"urls": ["https://example.invalid/ffmpeg.zip", "https://example.invalid/ffprobe.zip"],
             "sha256": [hashlib.sha256(data).hexdigest() for data in archives]}, archives)


def manifest(value):
    pins = {name: {"version": version} for name, version in (
        ("uv", "0.11.32"), ("python", "3.11.13"), ("bun", "1.3.14"),
        ("node", "20.19.4"), ("ffmpeg", "7.0.2"))}
    pins["omp"] = {"source": tools.DEFAULT_OMP_SOURCE}
    pins["ffmpeg"]["assets"] = {"macos-x64": value}
    return {"schema_version": 1, "agent": {"sha": "a" * 40},
            "runtime": {"sha": "b" * 40}, "tools": pins}


def test_two_archives_use_distinct_digests_and_extract_both(tmp_path, monkeypatch):
    value, archives = asset()
    requested = []

    def response(request, timeout):
        url = request.full_url
        requested.append(url)
        return io.BytesIO(archives[value["urls"].index(url)])

    monkeypatch.setattr(tools.urllib.request, "urlopen", response)
    destination = tmp_path / "ffmpeg"
    for url, checksum in tools.asset_downloads(value):
        tools.fetch_archive(url, destination, sha256=checksum)

    assert requested == value["urls"]
    assert (destination / "ffmpeg").read_bytes() == b"encoder"
    assert (destination / "ffprobe").read_bytes() == b"probe"
    assert not destination.with_name("ffmpeg.download").exists()


def test_swapped_archive_digests_fail_before_extraction(tmp_path, monkeypatch):
    value, archives = asset()
    value["sha256"].reverse()
    monkeypatch.setattr(tools.urllib.request, "urlopen", lambda *args, **kwargs: io.BytesIO(archives[0]))
    url, checksum = tools.asset_downloads(value)[0]
    with pytest.raises(ValueError, match="checksum mismatch"):
        tools.fetch_archive(url, tmp_path / "ffmpeg", sha256=checksum)
    assert not (tmp_path / "ffmpeg").exists()
    assert not (tmp_path / "ffmpeg.download").exists()


def test_single_archive_uses_its_string_digest():
    value, _ = asset()
    single = {"url": value["urls"][0], "sha256": value["sha256"][0]}
    assert tools.asset_downloads(single) == [(single["url"], single["sha256"])]
    assert tools.validate_manifest(manifest(single))["tools"]["ffmpeg"]["assets"]["macos-x64"] == single


def test_legacy_single_archive_without_digest_retains_compatibility():
    single = {"url": "https://example.invalid/archive.zip"}
    assert tools.asset_downloads(single) == [(single["url"], None)]
    tools.validate_manifest(manifest(single))


@pytest.mark.parametrize("changes", [
    {"sha256": None}, {"sha256": "a" * 64}, {"sha256": []},
    {"sha256": ["a" * 64]}, {"sha256": ["a" * 64] * 3},
    {"sha256": ["A" * 64, "b" * 64]}, {"sha256": ["a" * 63, "b" * 64]},
    {"sha256": ["a" * 64, None]}, {"sha256": ["a" * 64, 42]},
    {"urls": []}, {"urls": "https://example.invalid/archive.zip"},
    {"urls": ["https://example.invalid/archive.zip", "http://example.invalid/probe.zip"]},
    {"urls": ["https://example.invalid/archive.zip", None]},
    {"url": "https://example.invalid/ambiguous.zip"},
])
def test_invalid_archive_pairs_fail_manifest_validation(changes):
    value, _ = asset()
    value.update(changes)
    with pytest.raises(ValueError, match="sha256|asset|HTTPS"):
        tools.asset_downloads(value)
    with pytest.raises(ValueError, match="sha256|asset|HTTPS"):
        tools.validate_manifest(manifest(value))


@pytest.mark.parametrize("checksum", [None, "", "A" * 64, "a" * 63, ["a" * 64], 42])
def test_explicit_invalid_single_archive_digest_is_rejected(checksum):
    with pytest.raises(ValueError, match="sha256"):
        tools.asset_downloads({"url": "https://example.invalid/tool.zip", "sha256": checksum})


def test_installed_tools_forward_the_matching_digest_for_every_archive(tmp_path, monkeypatch):
    media, archives = asset()
    pins = manifest(media)
    for name in ("bun", "node"):
        data = archive(name, name.encode())
        media["urls"].append(f"https://example.invalid/{name}.zip")
        archives.append(data)
        pins["tools"][name]["assets"] = {"macos-x64": {
            "url": media["urls"][-1], "sha256": hashlib.sha256(data).hexdigest()}}
    # Keep only the two media URLs; the network fixture handles all four.
    payloads = dict(zip(media["urls"], archives))
    media["urls"] = media["urls"][:2]
    downloads = []

    def response(request, timeout):
        url = request.full_url
        downloads.append(url)
        return io.BytesIO(payloads[url])

    def run(argv, **kwargs):
        argv = [str(value) for value in argv]
        if Path(argv[0]).name in {"git", "git.exe"}:
            # The installer records the installing user's git; answer it, whatever PATH holds.
            return "git version 2.47.0\n"
        if argv[-1] == "--version":
            return "1.3.14" if Path(argv[0]).name == "bun" else "v20.19.4"
        if argv[-1] == "-version":
            return f"{Path(argv[0]).name} version 7.0.2"
        if argv[-1] == "-encoders":
            return "\n".join(f" V..... {name} encoder" for name in tools.ENCODERS)
        if argv[-1] == "-filters":
            return "\n".join(f" ... {name} filter" for name in tools.FILTERS)
        raise AssertionError(argv)

    monkeypatch.setattr(tools.urllib.request, "urlopen", response)
    monkeypatch.setattr(tools.shutil, "which", lambda _: None)
    def omp_source(_selected, _root, _run, env, **_kwargs):
        (Path(env["BUN_INSTALL_BIN"]) / "omp").write_bytes(b"launcher")

    monkeypatch.setattr(tools, "_install_omp_package", omp_source)
    (tmp_path / "tools").mkdir()
    tools.install_tools(copy.deepcopy(pins), tmp_path / "tools", "macos-x64", run)
    assert set(downloads) == set(payloads)
    assert len(downloads) == 4
    assert (tmp_path / "tools/ffmpeg/ffmpeg").read_bytes() == b"encoder"
    assert (tmp_path / "tools/ffmpeg/ffprobe").read_bytes() == b"probe"


def test_multi_archive_manifest_preserves_digest_order():
    value, _ = asset()
    pins = manifest(value)
    assert tools.validate_manifest(pins) is pins
    assert tools.asset_downloads(value) == list(zip(value["urls"], value["sha256"]))
