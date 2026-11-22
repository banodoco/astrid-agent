"""Archive checksum pairing, validation and real ZIP extraction regressions."""

from __future__ import annotations

import copy
import hashlib
import io
import ssl
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

    def response(request, timeout, *, context):
        assert context.verify_mode == ssl.CERT_REQUIRED
        assert context.check_hostname is True
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
    def response(request, timeout, *, context):
        assert context.verify_mode == ssl.CERT_REQUIRED
        assert context.check_hostname is True
        return io.BytesIO(archives[0])

    monkeypatch.setattr(tools.urllib.request, "urlopen", response)
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

    def response(request, timeout, *, context):
        assert context.verify_mode == ssl.CERT_REQUIRED
        assert context.check_hostname is True
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


MOTION_PACK_PIN = {
    "repo": "https://github.com/banodoco/poms_packs",
    "commit": "2379ab54aca522019ff4f869cfdf9375bdb56dfc",
    "digest_rule": "pack-tree-v1",
    "digest": "75234c10217e67ec0f111a4cb6be4a6252e172a9cd382506498d07ce1d241029",
}


def _manifest_with_pack_pins(packs):
    value, _ = asset()
    pins = manifest(value)
    pins["packs"] = packs
    return pins


def test_manifest_accepts_the_approved_astrid_motion_pack_pin():
    pins = _manifest_with_pack_pins({"astrid_motion": MOTION_PACK_PIN.copy()})
    assert tools.validate_manifest(pins) is pins


@pytest.mark.parametrize("changes", [
    {"commit": "2379ab54"},
    {"digest_rule": "sha256"},
    {"digest": None},
])
def test_manifest_rejects_invalid_astrid_motion_pack_pin(changes):
    pin = MOTION_PACK_PIN.copy()
    pin.update(changes)
    if changes == {"digest": None}:
        del pin["digest"]
    with pytest.raises(ValueError, match="pack|manifest|commit|digest"):
        tools.validate_manifest(_manifest_with_pack_pins({"astrid_motion": pin}))


@pytest.mark.parametrize("second_pin", [
    {**MOTION_PACK_PIN, "repo": "https://github.com/another/packs"},
    {**MOTION_PACK_PIN, "commit": "b" * 40},
])
def test_manifest_rejects_default_packs_from_different_repository_or_commit(second_pin):
    with pytest.raises(ValueError, match="pack|manifest|repository|commit"):
        tools.validate_manifest(_manifest_with_pack_pins({
            "astrid_motion": MOTION_PACK_PIN.copy(),
            "another_default": second_pin,
        }))


@pytest.mark.parametrize("mode", ["install", "update"])
def test_installer_installs_candidate_defaults_after_stage_before_activation(
        tmp_path, monkeypatch, mode):
    """The candidate's pinned defaults are installed before its version is selected."""
    from astrid.installer import core, r0_stop

    monkeypatch.setattr(r0_stop, "is_r0_installation", lambda *_args: False)
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    root, data = tmp_path / "program", tmp_path / "workspace"
    events = []
    pins = _manifest_with_pack_pins({"astrid_motion": MOTION_PACK_PIN.copy()})
    candidate = core.Version(root / "versions" / "v20", root / "versions" / "v20" / "bin/python",
                             root / "versions" / "v20" / "profile", {}, release=20)
    old_python = tmp_path / "old" / "bin" / "python"
    old_python.parent.mkdir(parents=True)
    old_python.touch()
    old = core.Version(tmp_path / "old", old_python,
                       tmp_path / "old/profile", {}, release=19)

    class Backend:
        service = None

        def existing(self, _data):
            return old if mode == "update" else None

        def stage(self, selected_manifest, _root, _data):
            assert selected_manifest is pins
            events.append("stage")
            return candidate

        def install_defaults(self, selected_candidate, selected_manifest):
            assert selected_candidate is candidate
            assert selected_manifest is pins
            events.append("defaults")

        def has_workspace(self, _data):
            return False

        def create(self, _candidate, _data):
            events.append("create")
            return {"realm_id": "realm-test"}

        def validate_candidate(self, *_args):
            events.append("validate")

        def workspace(self, *_args):
            return {"realm_id": "realm-test"}

        def register(self, *_args):
            events.append("register")

        def start(self, *_args):
            events.append("start")

        def verify(self, *_args, **_kwargs):
            events.append("verify")

        def sync_skills(self, *_args):
            return {"ok": True}

        def stop(self, *_args):
            events.append("stop")

        def finish(self, *_args):
            events.append("finish")

        def open(self):
            events.append("open")

    backend = Backend()
    monkeypatch.setattr(core.Installer, "_recover_update", lambda _self: None)
    monkeypatch.setattr(core.Installer, "_observe_autostart", lambda _self: None)
    monkeypatch.setattr(core, "install_launcher", lambda _root: None)
    monkeypatch.setattr(core, "switch", lambda _root, version: events.append("switch"))

    installer = core.Installer(root, data, backend, say=lambda _line: None)
    if mode == "update":
        monkeypatch.setattr(core, "schema_unchanged", lambda *_args: True)

        def update_transaction(_self, _old, _realm, *, prepare, switch_candidate, activate,
                               **_kwargs):
            staged = prepare()
            switch_candidate(staged)
            activate(staged, data, {"realm_id": "realm-test"})

        monkeypatch.setattr(core.Installer, "_run_update_transaction", update_transaction)

    installer.run(mode, pins)

    assert events.index("stage") < events.index("defaults")
    assert events.index("defaults") < events.index("switch")
    assert events.index("defaults") < events.index("register")
    assert events.index("defaults") < events.index("start")


def test_default_packs_main_uses_explicit_personal_root_and_transaction_inputs(
        tmp_path, monkeypatch):
    from astrid.installer import default_packs

    personal_root = tmp_path / "user" / "Astrid" / "packs"
    selected_roots = (personal_root, tmp_path / "discovered-packs")
    core_root = tmp_path / "installed-core"
    packages = {"numpy": "2.0"}
    release_pins = {"astrid_motion": MOTION_PACK_PIN["commit"]}
    builds = {"astrid_motion": {"target": "installed-python"}}
    toolchain = {"version": "pinned-toolchain"}
    calls = {}

    monkeypatch.setattr(default_packs, "load_release_manifest", lambda _path: {"packs": {"astrid_motion": {}}})
    monkeypatch.setattr(default_packs, "_default_pins", lambda _manifest: (
        "https://example.invalid/packs", "a" * 40, {"astrid_motion": "astrid_motion"}, {}))
    monkeypatch.setattr(default_packs, "_selected_root", lambda override: (
        calls.setdefault("override", override), personal_root)[1])
    monkeypatch.setattr(default_packs, "_validation_snapshot", lambda: (packages, release_pins))
    monkeypatch.setattr(default_packs, "_build_inputs", lambda source, revision, packs, selected: (
        calls.setdefault("build_inputs", (source, revision, packs, selected)), (builds, toolchain))[1])
    monkeypatch.setattr(default_packs, "_composition_roots", lambda root, *, include_default: (
        calls.setdefault("composition", (root, include_default)), selected_roots)[1])
    monkeypatch.setattr(default_packs, "_core_root", lambda: core_root)

    def install(manifest_value, **options):
        calls["install"] = (manifest_value, options)

    monkeypatch.setattr(default_packs, "install_defaults", install)
    default_packs.main(str(tmp_path / "candidate" / "manifest.json"))

    assert calls["override"] is None
    assert calls["build_inputs"] == (
        "https://example.invalid/packs", "a" * 40,
        {"astrid_motion": "astrid_motion"}, None)
    assert calls["composition"] == (personal_root, True)
    assert calls["install"] == ({"packs": {"astrid_motion": {}}}, {
        "packs_root": personal_root,
        "python_packages": packages,
        "selected_roots": selected_roots,
        "core_root": core_root,
        "release_pins": release_pins,
        "builds": builds,
        "toolchain": toolchain,
    })
