"""Install verified manifest-pinned media tools, with recorded selection."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from astrid.installer import tools

TARGET = "windows-x64" if sys.platform == "win32" else "linux-x64"
EXE_SUFFIX = ".exe" if sys.platform == "win32" else ""


def manifest():
    value = {"tools": {
        name: {"version": version, "assets": {TARGET: {"url": f"https://example.invalid/{name}.zip"}}}
        for name, version in (("bun", "1.3.14"), ("node", "20.19.4"), ("ffmpeg", "7.0.2"))
    }}
    value["tools"]["omp"] = {"source": tools.DEFAULT_OMP_SOURCE}
    return value


def test_download_sends_astrid_user_agent(tmp_path, monkeypatch):
    import io

    requests = []

    class Response(io.BytesIO):
        pass

    def urlopen(request, *, timeout):
        requests.append((request, timeout))
        return Response(b"archive bytes")

    monkeypatch.setattr(tools.urllib.request, "urlopen", urlopen)
    target = tmp_path / "archive.zip"

    tools.download("https://example.invalid/archive.zip", target)

    assert requests[0][0].get_header("User-agent") == f"astrid-installer/{tools.ASTRID_VERSION}"
    assert requests[0][1] == 120
    assert target.read_bytes() == b"archive bytes"


def media_binary(directory, name):
    directory.mkdir(parents=True, exist_ok=True)
    executable = directory / (name + EXE_SUFFIX)
    executable.write_bytes(b"test executable")
    executable.chmod(0o755)
    return executable


def harness(tmp_path, monkeypatch, *, availability="qualified"):
    existing = tmp_path / "existing media"
    other = tmp_path / "unrelated path"
    other.mkdir()
    ffmpeg = media_binary(existing, "ffmpeg") if availability != "missing" else None
    ffprobe = media_binary(existing / "probe", "ffprobe") if availability not in {"missing", "missing_ffprobe"} else None
    if sys.platform == "win32":
        monkeypatch.setenv("PATHEXT", ".exe")
    monkeypatch.setenv("PATH", os.pathsep.join((str(existing), str(existing / "probe"), str(other))))
    root = tmp_path / "installed tools"
    downloads, calls = [], []

    def fetch(url, destination, **_kwargs):
        downloads.append(url)
        name = destination.name
        media_binary(destination / "bundle", name)
        if name == "ffmpeg":
            media_binary(destination / "bundle", "ffprobe")

    def run(argv, *, extra_env=None):
        executable, *args = [str(value) for value in argv]
        calls.append((executable, args))
        if args == ["--version"]:
            return "1.3.14" if Path(executable).stem == "bun" else "v20.19.4"
        host = executable in {str(ffmpeg), str(ffprobe)}
        if host and availability == "broken_ffprobe" and executable == str(ffprobe):
            raise subprocess.CalledProcessError(1, executable)
        if args == ["-version"]:
            name = Path(executable).stem
            if host and availability == "unidentifiable":
                return "unknown tool"
            return f"{name} version {'6.1.2-system' if host else '7.0.2-static'} Copyright test"
        if args == ["-hide_banner", "-encoders"]:
            encoders = tools.ENCODERS - ({"libopus"} if host and availability == "missing_encoder" else set())
            return "\n".join(f" V..... {name} encoder" for name in encoders)
        if args == ["-hide_banner", "-filters"]:
            filters = tools.FILTERS - ({"loudnorm"} if host and availability == "missing_filter" else set())
            return "\n".join(f" ... {name} filter" for name in filters)
        raise AssertionError(f"Unexpected tool invocation: {argv}")

    monkeypatch.setattr(tools, "fetch_archive", fetch)
    # These cases isolate media selection; the source recipe has dedicated
    # source/lock/ABI/launcher tests.
    def omp_source(_selected, _root, _run, env, **_kwargs):
        media_binary(Path(env["BUN_INSTALL_BIN"]), "omp")

    monkeypatch.setattr(tools, "_install_omp_package", omp_source)
    return root, ffmpeg, ffprobe, other, downloads, calls, run


def test_qualified_path_media_tools_do_not_override_pinned_archive(tmp_path, monkeypatch):
    root, ffmpeg, ffprobe, other, downloads, calls, run = harness(tmp_path, monkeypatch)

    env = tools.install_tools(manifest(), root, TARGET, run)

    downloaded_ffmpeg = root / "ffmpeg" / "bundle" / ("ffmpeg" + EXE_SUFFIX)
    downloaded_ffprobe = root / "ffmpeg" / "bundle" / ("ffprobe" + EXE_SUFFIX)
    assert downloads == [f"https://example.invalid/{name}.zip" for name in ("bun", "node", "ffmpeg")]
    assert env["ASTRID_FFMPEG_EXECUTABLE"] == str(downloaded_ffmpeg)
    assert env["ASTRID_FFPROBE_EXECUTABLE"] == str(downloaded_ffprobe)
    assert env["ASTRID_INSTALL_FFMPEG_SOURCE"] == "download"
    assert env["ASTRID_INSTALL_FFMPEG_VERSION"] == "7.0.2-static"
    assert env["ASTRID_INSTALL_FFPROBE_VERSION"] == "7.0.2-static"
    path = env["PATH"].split(os.pathsep)
    assert str(downloaded_ffmpeg.parent) in path
    assert shutil.which("ffmpeg", path=env["PATH"]) == str(downloaded_ffmpeg)
    assert shutil.which("ffprobe", path=env["PATH"]) == str(downloaded_ffprobe)
    assert Path(env["ASTRID_FFMPEG_EXECUTABLE"]).is_relative_to(root)
    assert Path(env["ASTRID_FFPROBE_EXECUTABLE"]).is_relative_to(root)
    assert str(ffmpeg.parent) not in path
    assert str(ffprobe.parent) not in path
    assert str(other) not in path
    assert (str(downloaded_ffmpeg), ["-hide_banner", "-encoders"]) in calls
    assert (str(downloaded_ffmpeg), ["-hide_banner", "-filters"]) in calls


def test_generated_service_environment_uses_utf8_stdio(tmp_path, monkeypatch):
    root, *_unused, run = harness(tmp_path, monkeypatch)
    env = tools.install_tools(manifest(), root, TARGET, run)
    assert env["PYTHONIOENCODING"] == "utf-8"


@pytest.mark.parametrize("availability", ["missing", "missing_ffprobe", "missing_encoder", "missing_filter",
                                         "broken_ffprobe", "unidentifiable"])
def test_absent_or_unqualified_media_tools_download_pinned_fallback(tmp_path, monkeypatch, availability):
    root, host_ffmpeg, host_ffprobe, _other, downloads, calls, run = harness(tmp_path, monkeypatch, availability=availability)

    env = tools.install_tools(manifest(), root, TARGET, run)

    ffmpeg = root / "ffmpeg" / "bundle" / ("ffmpeg" + EXE_SUFFIX)
    ffprobe = root / "ffmpeg" / "bundle" / ("ffprobe" + EXE_SUFFIX)
    assert downloads == [f"https://example.invalid/{name}.zip" for name in ("bun", "node", "ffmpeg")]
    assert env["ASTRID_FFMPEG_EXECUTABLE"] == str(ffmpeg)
    assert env["ASTRID_FFPROBE_EXECUTABLE"] == str(ffprobe)
    assert env["ASTRID_INSTALL_FFMPEG_SOURCE"] == "download"
    assert env["ASTRID_INSTALL_FFMPEG_VERSION"] == "7.0.2-static"
    assert env["ASTRID_INSTALL_FFPROBE_VERSION"] == "7.0.2-static"
    assert (str(ffmpeg), ["-hide_banner", "-encoders"]) in calls
    assert (str(ffmpeg), ["-hide_banner", "-filters"]) in calls
    path = env["PATH"].split(os.pathsep)
    for executable in (host_ffmpeg, host_ffprobe):
        if executable:
            assert str(executable.parent) not in path


def test_downloaded_media_tools_still_require_every_capability(tmp_path, monkeypatch):
    root, *_unused, run = harness(tmp_path, monkeypatch, availability="missing")

    def missing_filter(argv, **kwargs):
        output = run(argv, **kwargs)
        if [str(value) for value in argv[1:]] == ["-hide_banner", "-filters"]:
            return output.replace("loudnorm", "absent")
        return output

    with pytest.raises(ValueError, match="loudnorm"):
        tools.install_tools(manifest(), root, TARGET, missing_filter)


def test_an_older_managed_version_cannot_supply_media_tools_to_its_successor(tmp_path, monkeypatch):
    _root, *_unused, run = harness(tmp_path, monkeypatch)
    versions = tmp_path / "program" / "versions"
    old_media = versions / "old" / "tools" / "ffmpeg"
    media_binary(old_media, "ffmpeg")
    media_binary(old_media, "ffprobe")
    monkeypatch.setenv("PATH", str(old_media))
    root = versions / "new" / "tools"

    env = tools.install_tools(manifest(), root, TARGET, run)

    assert env["ASTRID_INSTALL_FFMPEG_SOURCE"] == "download"
    assert Path(env["ASTRID_FFMPEG_EXECUTABLE"]).is_relative_to(root)
    assert Path(env["ASTRID_FFPROBE_EXECUTABLE"]).is_relative_to(root)
    assert str(old_media) not in env["PATH"].split(os.pathsep)


@pytest.mark.parametrize("selected", [tools.DEFAULT_OMP_SOURCE,
    {"repo": "https://github.com/owner/fixed-fork", "sha": "c" * 40}])
def test_install_routes_the_authoritative_source_without_an_implicit_fork(tmp_path, monkeypatch, selected):
    root, *_unused, run = harness(tmp_path, monkeypatch)
    pins = manifest()
    pins["tools"]["omp"] = {"source": selected}
    choices = []

    def npm(package, _root, _run, env):
        choices.append(package)
        media_binary(Path(env["BUN_INSTALL_BIN"]), "omp")

    def fork(source, _recipe, _root, _target, _run, env):
        choices.append(source)
        media_binary(Path(env["BUN_INSTALL_BIN"]), "omp")

    monkeypatch.setattr(tools, "_install_omp_package", npm)
    monkeypatch.setattr(tools, "_install_omp_source", fork)
    tools.install_tools(pins, root, TARGET, run)
    assert choices == [selected]


def test_launcher_must_resolve_inside_the_owned_prefix(tmp_path, monkeypatch):
    root, *_unused, run = harness(tmp_path, monkeypatch)
    outside = media_binary(tmp_path / "outside", "unowned-omp")

    def redirect(_selected, _root, _run, env):
        (Path(env["BUN_INSTALL_BIN"]) / ("omp" + EXE_SUFFIX)).symlink_to(outside)

    monkeypatch.setattr(tools, "_install_omp_package", redirect)
    with pytest.raises(ValueError, match="launcher escapes"):
        tools.install_tools(manifest(), root, TARGET, run)
