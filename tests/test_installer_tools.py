"""Use qualified PATH media tools or the pinned archive, with recorded selection."""

import hashlib
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


def test_download_checksum_mismatch_records_digests_without_url_credentials(tmp_path, monkeypatch):
    import io

    received = b"unexpected archive bytes"
    expected = hashlib.sha256(b"expected archive bytes").hexdigest()

    class Response(io.BytesIO):
        status = 206
        headers = {
            "Content-Type": "application/x-xz; charset=ignored",
            "Content-Length": str(len(received)),
            "ETag": 'W/"archive-7.0.2"',
            "Last-Modified": "Sun, 02 Nov 2026 23:00:00 GMT",
            "Authorization": "Bearer response-secret",
            "Proxy-Authorization": "Basic proxy-secret",
            "Cookie": "response-cookie",
            "Set-Cookie": "set-cookie-secret",
            "Proxy-Authenticate": "proxy-authenticate-secret",
            "X-Unallowlisted": "unallowlisted-secret",
        }

        def geturl(self):
            return "https://example.invalid/archive.tar.xz"

    def urlopen(*_args, **_kwargs):
        return Response(received)

    monkeypatch.setattr(tools.urllib.request, "urlopen", urlopen)

    with pytest.raises(ValueError) as error:
        tools.download(
            "https://user:secret@example.invalid/archive.zip",
            tmp_path / "archive.zip",
            sha256=expected,
        )

    message = str(error.value)
    assert f"expected SHA-256 {expected}" in message
    assert f"received SHA-256 {hashlib.sha256(received).hexdigest()}" in message
    assert f"network SHA-256 {hashlib.sha256(received).hexdigest()}" in message
    assert f"network byte count {len(received)}" in message
    assert f"file byte count {len(received)}" in message
    assert "HTTP status 206" in message
    assert "final URL https://example.invalid/archive.tar.xz" in message
    assert "Content-Type application/x-xz" in message
    assert f"Content-Length {len(received)}" in message
    assert 'ETag W/"archive-7.0.2"' in message
    assert "Last-Modified 2026-11-02T23:00:00Z" in message
    assert "user:secret" not in message
    assert "redirect-user" not in message
    assert "redirect-secret" not in message
    assert "query-secret" not in message
    assert "fragment-secret" not in message
    assert "response-secret" not in message
    assert "proxy-secret" not in message
    assert "response-cookie" not in message
    assert "set-cookie-secret" not in message
    assert "proxy-authenticate-secret" not in message
    assert "unallowlisted-secret" not in message


def test_download_checksum_mismatch_omits_unsafe_final_url(tmp_path, monkeypatch):
    import io

    received = b"unexpected archive bytes"
    expected = hashlib.sha256(b"expected archive bytes").hexdigest()

    class Response(io.BytesIO):
        def geturl(self):
            return "https://redirect-user:redirect-secret@example.invalid/archive.tar.xz?token=query-secret#fragment-secret"

    monkeypatch.setattr(tools.urllib.request, "urlopen", lambda *_args, **_kwargs: Response(received))

    with pytest.raises(ValueError) as error:
        tools.download("https://user:secret@example.invalid/archive.zip", tmp_path / "archive.zip", sha256=expected)

    message = str(error.value)
    assert "final URL omitted (unsafe component)" in message
    assert "user:secret" not in message
    assert "redirect-user" not in message
    assert "redirect-secret" not in message
    assert "query-secret" not in message
    assert "fragment-secret" not in message


def test_download_checksum_mismatch_distinguishes_network_bytes_from_local_mutation(tmp_path, monkeypatch):
    import io

    network_bytes = b"network archive bytes"
    locally_mutated_bytes = b"locally mutated archive bytes"
    expected = hashlib.sha256(b"expected archive bytes").hexdigest()
    original_file_sha256 = tools._file_sha256

    def file_sha256_after_local_mutation(path):
        path.write_bytes(locally_mutated_bytes)
        return original_file_sha256(path)

    monkeypatch.setattr(tools.urllib.request, "urlopen", lambda *_args, **_kwargs: io.BytesIO(network_bytes))
    monkeypatch.setattr(tools, "_file_sha256", file_sha256_after_local_mutation)

    with pytest.raises(ValueError) as error:
        tools.download("https://example.invalid/archive.zip", tmp_path / "archive.zip", sha256=expected)

    message = str(error.value)
    assert f"network SHA-256 {hashlib.sha256(network_bytes).hexdigest()}" in message
    assert f"received SHA-256 {hashlib.sha256(locally_mutated_bytes).hexdigest()}" in message
    assert f"network byte count {len(network_bytes)}" in message
    assert f"file byte count {len(locally_mutated_bytes)}" in message


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


def test_qualified_path_media_tools_are_selected_and_recorded(tmp_path, monkeypatch):
    root, ffmpeg, ffprobe, other, downloads, calls, run = harness(tmp_path, monkeypatch)

    env = tools.install_tools(manifest(), root, TARGET, run)

    assert downloads == ["https://example.invalid/bun.zip", "https://example.invalid/node.zip"]
    assert env["ASTRID_FFMPEG_EXECUTABLE"] == str(ffmpeg)
    assert env["ASTRID_FFPROBE_EXECUTABLE"] == str(ffprobe)
    assert env["ASTRID_INSTALL_FFMPEG_SOURCE"] == "path"
    assert env["ASTRID_INSTALL_FFMPEG_VERSION"] == "6.1.2-system"
    assert env["ASTRID_INSTALL_FFPROBE_VERSION"] == "6.1.2-system"
    path = env["PATH"].split(os.pathsep)
    assert str(ffmpeg.parent) in path and str(ffprobe.parent) in path
    assert shutil.which("ffmpeg", path=env["PATH"]) == str(ffmpeg)
    assert shutil.which("ffprobe", path=env["PATH"]) == str(ffprobe)
    assert str(other) not in path
    assert (str(ffmpeg), ["-hide_banner", "-encoders"]) in calls
    assert (str(ffmpeg), ["-hide_banner", "-filters"]) in calls
    assert not (root / "ffmpeg").exists()


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
