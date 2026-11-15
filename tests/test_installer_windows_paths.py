"""Windows path budgets are checked from indexes; exercise real I/O on Linux too."""

import io
import tarfile
import zipfile
from pathlib import Path, PureWindowsPath

import pytest

from astrid.installer import tools

AGENT = "astrid-agent-" + "a" * 40
VIBE = "VibeComfy-" + "b" * 40
# Same lengths as the reported release entries, including the archive folder.
FILES = {
    AGENT: "remotion/artifacts/" + "x" * 105,
    VIBE: "docs/handover/" + "d" * 60 + "/" + "f" * 80,
}


def _archive(path, files, kind):
    if kind == "zip":
        with zipfile.ZipFile(path, "w") as bundle:
            for name, content in files.items():
                bundle.writestr(name, content)
    else:
        with tarfile.open(path, "w:gz") as bundle:
            for name, content in files.items():
                member = tarfile.TarInfo(name)
                member.size = len(content)
                bundle.addfile(member, io.BytesIO(content))


@pytest.mark.parametrize("username_length", [1, 6, 20])
@pytest.mark.parametrize("prefix,total", [(AGENT, 178), (VIBE, 206)])
def test_reported_archives_fit_every_windows_source_path(username_length, prefix, total):
    folder = (PureWindowsPath("C:/Users") / ("u" * username_length) / "AppData/Local/Astrid/versions"
              / ("a" * 8 + "-" + "b" * 8 + "-" + "c" * 8))
    entry = prefix + "/" + FILES[prefix]
    assert len(entry) == total
    old_length = len(str(folder / "a.unpack")) + 1 + total
    assert old_length == 76 + username_length + total
    if old_length >= 260:
        with pytest.raises(ValueError, match="shorter Windows path"):
            tools.archive_destinations([(entry, False)], folder / "a.unpack", platform_name="win32")
    planned, = tools.archive_destinations([(entry, False)], folder / "a.u",
                                          platform_name="win32", strip_root=True)
    assert len(str(planned)) == 69 + username_length + 2 + len(FILES[prefix])
    assert len(str(planned)) <= 246
    final, = tools.archive_destinations([(FILES[prefix], False)], folder / "a",
                                        platform_name="win32")
    assert len(str(final)) <= 244


@pytest.mark.parametrize("kind", ["tar", "zip"])
@pytest.mark.parametrize("prefix", [AGENT, VIBE])
def test_windows_source_moves_every_file_and_preserves_tree_digest(tmp_path, monkeypatch, kind, prefix):
    # Keep the real test prefix short too; the pure test covers default profiles.
    destination = tmp_path.parent / (kind + prefix[:4]) / "v"
    payload = tmp_path / "source.archive"
    files = {FILES[prefix]: b"deep evidence", "README.md": b"readme", "hidden/.keep": b"keep"}
    _archive(payload, {prefix + "/" + name: content for name, content in files.items()}, kind)
    monkeypatch.setattr(tools, "download", lambda _url, target, **_kw: target.write_bytes(payload.read_bytes()))
    expected = tmp_path / "expected"
    for name, content in files.items():
        path = expected / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    tools.source("vibecomfy", "b" * 40, destination, platform_name="win32")

    assert {p.relative_to(destination).as_posix(): p.read_bytes()
            for p in destination.rglob("*") if p.is_file()} == files
    assert tools.source_tree_sha256(destination) == tools.source_tree_sha256(expected)
    assert not destination.with_name("v.u").exists()
    assert not destination.with_name("v.u.download").exists()


@pytest.mark.parametrize("kind", ["tar", "zip"])
def test_path_refusal_happens_before_any_extraction(tmp_path, kind):
    archive = tmp_path / "source.archive"
    destination = tmp_path / "unpack"
    _archive(archive, {"root/first": b"must not be written", "root/" + "x" * 250: b"long"}, kind)
    with pytest.raises(ValueError, match=r"shorter Windows path.*--root C:\\Astrid") as caught:
        tools.extract(archive, destination, platform_name="win32", strip_root=True)
    assert str(caught.value).count(".") == 1
    assert not destination.exists()


@pytest.mark.parametrize("length,ok", [(259, True), (260, False)])
def test_file_limit_counts_utf16_units(length, ok):
    root = PureWindowsPath("C:/short")
    name = "\U0001f600" * 100 + "x" * (length - len(str(root)) - 1 - 200)
    if ok:
        tools.archive_destinations([(name, False)], root, platform_name="win32")
    else:
        with pytest.raises(ValueError, match="shorter Windows path"):
            tools.archive_destinations([(name, False)], root, platform_name="win32")


@pytest.mark.parametrize("explicit", [False, True])
def test_directory_limit_includes_implicit_parents(explicit):
    root = PureWindowsPath("C:/short")
    directory = "d" * (248 - len(str(root)) - 1)
    entries = [(directory, True)] if explicit else [(directory + "/f", False)]
    with pytest.raises(ValueError, match="shorter Windows path"):
        tools.archive_destinations(entries, root, platform_name="win32")


@pytest.mark.parametrize("entries", [[("one/file", False), ("two/file", False)],
                                     [("root", False)], [("root/../escape", False)]])
def test_stripping_cannot_hide_bad_layout_or_traversal(entries):
    with pytest.raises(ValueError, match="layout|Unsafe archive"):
        tools.archive_destinations(entries, Path("/short"), platform_name="win32", strip_root=True)


@pytest.mark.parametrize("platform_name", ["linux", "darwin"])
def test_other_platforms_keep_long_paths_and_archive_prefix(platform_name):
    root = Path("/long")
    name = "root/" + "d/" * 150 + "file"
    assert tools.archive_destinations([(name, False)], root, platform_name=platform_name) == [root / name]


@pytest.mark.parametrize("platform_name", ["win32", "linux", "darwin"])
def test_source_git_enables_longpaths_only_on_windows(platform_name):
    expected = ["C:/Git/git.exe"]
    if platform_name == "win32":
        expected += ["-c", "core.longpaths=true"]
    assert tools.source_git_command(expected[0], platform_name=platform_name) == expected
