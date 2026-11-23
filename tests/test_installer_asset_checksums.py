"""Archive checksum pairing, validation and real ZIP extraction regressions."""

from __future__ import annotations

import copy
import hashlib
import io
import os
import subprocess
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
    """Fresh installs publish pinned defaults before activation; updates do not."""
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

    if mode == "install":
        assert events.index("stage") < events.index("defaults")
        assert events.index("defaults") < events.index("switch")
        assert events.index("defaults") < events.index("register")
        assert events.index("defaults") < events.index("start")
    else:
        assert "defaults" not in events


def test_installer_does_not_publish_defaults_when_reattaching_retained_workspace(
        tmp_path, monkeypatch):
    """Reinstalling around a kept workspace must not change its pack collection."""
    from astrid.installer import core, r0_stop

    monkeypatch.setattr(r0_stop, "is_r0_installation", lambda *_args: False)
    root, data = tmp_path / "program", tmp_path / "workspace"
    events = []
    pins = _manifest_with_pack_pins({"astrid_motion": MOTION_PACK_PIN.copy()})
    candidate = core.Version(root / "versions" / "v20", root / "versions" / "v20" / "bin/python",
                             root / "versions" / "v20" / "profile", {}, release=20)

    class Backend:
        def existing(self, _data):
            return None

        def has_workspace(self, selected_data):
            assert selected_data == data
            return True

        def reinstallable(self, selected_data):
            assert selected_data == data
            return True

        def stage(self, *_args):
            events.append("stage")
            return candidate

        def install_defaults(self, *_args):
            events.append("defaults")

        def attach_existing(self, *_args):
            events.append("attach")
            return {"realm_id": "realm-retained"}

        def create(self, *_args):
            pytest.fail("a retained workspace must be attached, not recreated")

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
    monkeypatch.setattr(core, "switch", lambda *_args: events.append("switch"))

    core.Installer(root, data, backend, say=lambda _line: None).run("install", pins)

    assert "defaults" not in events
    assert events.index("stage") < events.index("attach") < events.index("switch")


def test_retry_after_aborted_default_publication_fails_loudly_if_collection_is_workspace(
        tmp_path, monkeypatch):
    """An exchanged pack collection cannot make a retry silently skip defaults."""
    import sys
    from astrid.core.pack import transaction
    from astrid.core.pack.transaction import read_group
    from astrid.installer import core, r0_stop
    from astrid.installer.backend import Backend

    fixture_manifest, _ = _real_default_fixture(tmp_path / "repository")
    manifest = {"release": 20, **fixture_manifest()}
    root = tmp_path / "program"
    packs_root = tmp_path / "home" / "Astrid" / "packs"
    swapped = False
    sync = transaction._sync_directory

    def fail_after_exchange(path):
        if swapped:
            raise OSError("injected directory durability failure")
        return sync(path)

    def boundary(phase):
        nonlocal swapped
        if phase == "swapped":
            swapped = True

    backend, candidate = _default_child_backend(
        tmp_path, monkeypatch, manifest, packs_root=packs_root)
    # Use the product's workspace predicates and service reader. The selected
    # data root overlaps this test's pack collection so the actual exchange is
    # visible to the next install attempt as nonempty persisted state.
    backend.root = root
    backend.service = Backend(root, tmp_path / "service.log", Path(sys.executable)).service
    backend.stage = lambda *_args: candidate
    backend.stop = lambda *_args: None
    backend.create = lambda *_args: pytest.fail("the failed first attempt must not create a realm")
    backend.attach_existing = lambda *_args: pytest.fail(
        "a collection without a retained service record must not be attached as a workspace")
    monkeypatch.setattr(r0_stop, "is_r0_installation", lambda *_args: False)
    monkeypatch.setattr(core.Installer, "_recover_update", lambda _self: None)
    monkeypatch.setattr(core.Installer, "_observe_autostart", lambda _self: None)
    monkeypatch.setattr(core, "install_launcher", lambda _root: None)
    monkeypatch.setattr(core, "switch", lambda *_args: None)

    from astrid.installer import default_packs
    original_install_defaults = default_packs.install_defaults
    monkeypatch.setattr(transaction, "_sync_directory", fail_after_exchange)
    monkeypatch.setattr(default_packs, "install_defaults",
                        lambda *args, **kwargs: original_install_defaults(
                            *args, boundary=boundary, **kwargs))

    installer = core.Installer(root, packs_root, backend, say=lambda _line: None)
    with pytest.raises(RuntimeError):
        installer.run("install", manifest)

    snapshot = read_group(packs_root)
    assert snapshot.transaction_id is not None
    assert snapshot.refresh_pending
    assert Backend.has_workspace(backend, packs_root)
    assert not Backend.reinstallable(backend, packs_root)

    retry = core.Installer(root, packs_root, backend, say=lambda _line: None)
    with pytest.raises(ValueError, match="existing workspace has no usable source profile"):
        retry.run("install", manifest)


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


def _real_default_fixture(root: Path):
    """Small local Git source for exercising the actual defaults transaction."""
    from astrid.core.pack.identity import pack_digest

    work, bare = root / "work", root / "source.git"
    work.mkdir(parents=True)
    def git(path, *args):
        return subprocess.run(["git", "-C", str(path), *args], check=True,
                              text=True, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE).stdout.strip()
    git(work, "init", "-b", "main")
    git(work, "config", "user.name", "Installer checksum test")
    git(work, "config", "user.email", "installer@example.invalid")
    pack = work / "astrid_motion"
    pack.mkdir()
    (pack / "pack.yaml").write_text(
        'schema_version: 3\nid: astrid_motion\nname: Motion\nversion: 1.0.0\n'
        'pack_api: "2.0"\ndocumentation: {kind: none, reason: Fixture.}\n',
        encoding="utf-8")
    payload = pack / "payload.txt"
    payload.write_text("first generation\n", encoding="utf-8")
    def publish():
        git(work, "add", "-A")
        git(work, "commit", "-m", "fixture")
        revision = git(work, "rev-parse", "HEAD")
        if bare.exists():
            git(work, "push", str(bare), "HEAD:refs/heads/main")
        return revision
    revision = publish()
    git(root, "clone", "--bare", str(work), str(bare))
    def manifest():
        return {"packs": {"astrid_motion": {
            "repo": str(bare), "commit": revision,
            "digest_rule": "pack-tree-v1", "digest": pack_digest(pack)}}}
    def next_revision():
        nonlocal revision
        payload.write_text("second generation\n", encoding="utf-8")
        revision = publish()
        return manifest()
    return manifest, next_revision


def _default_child_backend(tmp_path, monkeypatch, manifest, *, packs_root, boundary=None):
    """Exercise the real default_packs child through Backend's subprocess boundary."""
    import contextlib
    import json
    import subprocess
    import sys
    from astrid.installer import default_packs
    from astrid.installer.backend import Backend
    from astrid.installer.core import Version

    monkeypatch.setenv("HOME", str(packs_root.parent.parent))
    monkeypatch.setattr(default_packs, "_selected_root", lambda _override: packs_root)
    monkeypatch.setattr(default_packs, "_validation_snapshot", lambda: ({}, {}))
    monkeypatch.setattr(default_packs, "_build_inputs", lambda *_args: ({}, {}))
    monkeypatch.setattr(default_packs, "_composition_roots", lambda *_args, **_kwargs: ())
    core_root = tmp_path / "empty-core"
    core_root.mkdir(exist_ok=True)
    monkeypatch.setattr(default_packs, "_core_root", lambda: core_root)
    if boundary is not None:
        from astrid.core.pack import defaults
        original = defaults.install_defaults
        monkeypatch.setattr(default_packs, "install_defaults",
                            lambda *args, **kwargs: original(*args, boundary=boundary, **kwargs))
    candidate = tmp_path / "candidate"
    candidate.mkdir(exist_ok=True)
    manifest_path = candidate / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    version = Version(candidate, Path(sys.executable), candidate / "profile.json", {}, release=20)
    backend = object.__new__(Backend)
    backend.log = tmp_path / "install.log"
    def run(_argv, **_kwargs):
        output = io.StringIO()
        try:
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                default_packs.main(str(manifest_path))
        except Exception as exc:
            raise subprocess.CalledProcessError(
                1, [sys.executable, "-m", "astrid.installer.default_packs"],
                output=output.getvalue() + f"{type(exc).__name__}: {exc}", stderr="") from exc
        return output.getvalue()
    backend.run = run
    return backend, version


def _run_installer_default(backend, version, manifest):
    return backend.install_defaults(version, manifest)


def test_wrong_default_digest_rejects_first_install_before_group_publication(tmp_path, monkeypatch):
    from astrid.core.pack.transaction import read_group

    manifest, _ = _real_default_fixture(tmp_path / "repository")
    pins = manifest()
    pins["packs"]["astrid_motion"]["digest"] = "0" * 64
    packs_root = tmp_path / "Astrid" / "packs"
    backend, candidate = _default_child_backend(tmp_path, monkeypatch, pins, packs_root=packs_root)
    with pytest.raises(RuntimeError) as failure:
        _run_installer_default(backend, candidate, pins)
    assert "digest" in str(failure.value).lower()
    snapshot = read_group(packs_root)
    assert snapshot.revision == 0
    assert snapshot.transaction_id is None
    assert snapshot.paths == {}


def test_wrong_default_digest_rejects_direct_reconciliation_and_preserves_existing_group(
        tmp_path, monkeypatch):
    from astrid.core.pack.transaction import read_group

    manifest, update_manifest = _real_default_fixture(tmp_path / "repository")
    packs_root = tmp_path / "Astrid" / "packs"
    backend, candidate = _default_child_backend(tmp_path, monkeypatch, manifest(), packs_root=packs_root)
    _run_installer_default(backend, candidate, manifest())
    initial = read_group(packs_root)
    state_before = (packs_root / ".group.json").read_bytes()
    payload_before = (read_group(packs_root).paths["astrid_motion"] / "payload.txt").read_bytes()
    pins = update_manifest()
    pins["packs"]["astrid_motion"]["digest"] = "0" * 64

    backend, candidate = _default_child_backend(tmp_path, monkeypatch, pins, packs_root=packs_root)
    with pytest.raises(RuntimeError) as failure:
        _run_installer_default(backend, candidate, pins)
    assert "digest" in str(failure.value).lower()
    current = read_group(packs_root)
    assert current.transaction_id == initial.transaction_id
    assert current.revision == initial.revision
    assert (packs_root / ".group.json").read_bytes() == state_before
    assert (current.paths["astrid_motion"] / "payload.txt").read_bytes() == payload_before


def test_default_group_commit_error_preserves_published_identity_and_recovers(tmp_path, monkeypatch):
    from astrid.core.pack import transaction
    from astrid.core.pack.transaction import read_group, retry_refresh

    manifest, update_manifest = _real_default_fixture(tmp_path / "repository")
    packs_root = tmp_path / "Astrid" / "packs"
    backend, candidate = _default_child_backend(tmp_path, monkeypatch, manifest(), packs_root=packs_root)
    _run_installer_default(backend, candidate, manifest())
    pins = update_manifest()
    swapped = False
    sync = transaction._sync_directory
    def fail_after_exchange(path):
        if swapped:
            raise OSError("injected directory durability failure")
        return sync(path)
    def boundary(phase):
        nonlocal swapped
        if phase == "swapped":
            swapped = True
    monkeypatch.setattr(transaction, "_sync_directory", fail_after_exchange)

    backend, candidate = _default_child_backend(tmp_path, monkeypatch, pins,
                                                packs_root=packs_root, boundary=boundary)
    with pytest.raises(RuntimeError) as failure:
        _run_installer_default(backend, candidate, pins)

    snapshot = read_group(packs_root)
    assert snapshot.revision == 2
    assert snapshot.refresh_pending
    assert (snapshot.paths["astrid_motion"] / "payload.txt").read_bytes() == b"second generation\n"
    monkeypatch.setattr(transaction, "_sync_directory", sync)
    assert retry_refresh(packs_root=packs_root, refresh=lambda **kw: {
        "version": 1, "accepted": True, "revision": kw["committed_revision"], "reason": "ok"}) is False
    assert not read_group(packs_root).refresh_pending


def test_failed_update_does_not_publish_defaults_and_receipt_claims_no_publication(
        tmp_path, monkeypatch, capsys):
    """Updates do not publish release defaults, including before a busy-stop refusal."""
    import sys
    from astrid.core.pack.transaction import read_group
    from astrid.installer import cli, core, r0_stop
    from astrid.installer.core import RuntimeOperationError, Version
    from astrid.installer.installation import State
    from astrid.installer.outcome import RECORD, read_record

    fixture_manifest, _ = _real_default_fixture(tmp_path / "repository")
    root, data = tmp_path / "program", tmp_path / "workspace"
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(r0_stop, "is_r0_installation", lambda *_args: False)
    old_root = tmp_path / "old"
    old_root.mkdir()
    old = Version(old_root, Path(sys.executable), old_root / "profile.json", {}, release=19)

    packs_root = home / "Astrid" / "packs"
    backend, candidate = _default_child_backend(
        tmp_path, monkeypatch, {"release": 20, **fixture_manifest()}, packs_root=packs_root)
    backend.service = None
    backend.managed_by = None
    backend.publish_launch_admission = lambda *_args: None
    backend.existing = lambda _data: old
    backend.stage = lambda _manifest, _root, _data: candidate
    backend.has_workspace = lambda _data: False
    backend.workspace = lambda *_args: {"realm_id": "realm-test"}
    backend.validate_candidate = lambda *_args: None
    def stop(_version, _data):
        raise RuntimeOperationError("busy", details={"safe": False, "code": "busy"})
    backend.stop = stop
    # Host snapshots keep the fixture independent of installed packages.
    monkeypatch.setattr(core.Installer, "_recover_update", lambda _self: None)
    monkeypatch.setattr(core.Installer, "_observe_autostart", lambda _self: None)
    monkeypatch.setattr(core, "install_runtime", lambda _root: None)
    active = {"version": old}
    monkeypatch.setattr(core, "current", lambda _root: active["version"])
    monkeypatch.setattr(core, "switch", lambda _root, version: active.__setitem__("version", version))
    monkeypatch.setattr(core, "schema_unchanged", lambda *_args: True)

    installer = core.Installer(root, data, backend, say=lambda _line: None)
    with pytest.raises(RuntimeOperationError, match="busy") as refusal:
        installer.run("update", {"release": 20, **fixture_manifest()})

    snapshot = read_group(packs_root)
    assert snapshot.revision == 0
    assert snapshot.transaction_id is None
    assert snapshot.paths == {}
    assert active["version"] is old
    state = State("direct", root, data, None, old, live_identity={"release": old.release})
    monkeypatch.setattr(cli.Installation, "resolve", lambda *_args, **_kwargs: state)
    log = tmp_path / "logs" / "install.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    run = cli._Run("update")
    run.target, run.target_source = candidate.release, "manifest"
    run.before, run.home, run.system, run.root = state, home, sys.platform, root
    run.log, run.installer = log, installer
    run.protected = []
    run.started_journal = None
    run.finish(failure=refusal.value)
    receipt = read_record(log.with_name(RECORD))
    assert "default_packs" not in receipt
    assert "Default packs were published" not in receipt["headline"]
    assert "nothing was changed" in capsys.readouterr().out.lower()


@pytest.mark.skipif(os.name == "nt", reason="_publish injection covers the POSIX publication path")
def test_fresh_install_swap_back_preserves_retained_write_on_abort(tmp_path, monkeypatch):
    """A fresh-install abort preserves the retained write when its transaction swaps back."""
    from astrid.core.pack import transaction
    from astrid.installer import core, r0_stop
    from astrid.installer.core import Installer

    fixture_manifest, _ = _real_default_fixture(tmp_path / "repository")
    manifest = {"release": 20, **fixture_manifest()}
    root, data = tmp_path / "program", tmp_path / "workspace"
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    packs_root = home / "Astrid" / "packs"
    backend, candidate = _default_child_backend(
        tmp_path, monkeypatch, manifest, packs_root=packs_root)
    monkeypatch.setattr(r0_stop, "is_r0_installation", lambda *_args: False)
    backend.existing = lambda _data: None
    backend.stage = lambda *_args: candidate
    backend.has_workspace = lambda _data: False
    backend.create = lambda *_args: pytest.fail("workspace creation must not begin after defaults reject")
    backend.stop = lambda *_args: None
    monkeypatch.setattr(Installer, "_recover_update", lambda _self: None)
    monkeypatch.setattr(Installer, "_observe_autostart", lambda _self: None)
    monkeypatch.setattr(core, "install_launcher", lambda _root: None)
    monkeypatch.setattr(core, "switch", lambda *_args: pytest.fail("failed defaults must not switch versions"))
    from astrid.core.pack.personal import ensure_personal_pack
    personal = ensure_personal_pack(packs_root)
    (personal / "kept.txt").write_text("user content\n", encoding="utf-8")
    original_verify = transaction._verify_retained_collection
    original_publish = transaction._publish
    hits = []
    publish_calls = []
    late_write = b"raced the exchange\n"
    window_write = b"written in the new generation before swap-back\n"

    def write_into_retiring_generation(stage, destination):
        publish_calls.append(Path(destination))
        if len(publish_calls) == 1:
            (packs_root / "personal" / "late.txt").write_bytes(late_write)
        original_publish(stage, destination)

    def write_during_post_swap_check(live, changed, copied):
        if ".astrid-retired" in Path(live).parts and not hits:
            hits.append(Path(live))
            (packs_root / "personal" / "window.txt").write_bytes(window_write)
        original_verify(live, changed, copied)

    monkeypatch.setattr(transaction, "_publish", write_into_retiring_generation)
    monkeypatch.setattr(transaction, "_verify_retained_collection", write_during_post_swap_check)
    installer = Installer(root, data, backend, say=lambda _line: None)
    with pytest.raises(RuntimeError) as failure:
        installer.run("install", manifest)
    assert len(hits) == 1
    assert len(publish_calls) == 2
    assert (packs_root / "personal" / "late.txt").read_bytes() == late_write
    assert not (packs_root / "personal" / "window.txt").exists()
    refused = list((packs_root.parent / ".astrid-retired").glob("refused-*"))
    assert len(refused) == 1
    assert (refused[0] / "personal" / "window.txt").read_bytes() == window_write
    assert str(refused[0]) in str(failure.value)
    assert "personal" in str(failure.value)


def test_backend_preserves_child_default_pack_error_text(tmp_path):
    """Backend failures retain the child transaction error in the ordinary failure path."""
    import subprocess
    import sys
    from astrid.installer.backend import Backend
    from astrid.installer.core import Version

    error_text = "GroupCommitError: retained at /packs/.astrid-retired/refused-a1"
    backend = object.__new__(Backend)
    backend.log = tmp_path / "install.log"
    def failed_child(*_args, **_kwargs):
        raise subprocess.CalledProcessError(1, ["python", "default_packs"],
                                            output=error_text, stderr="")
    backend.run = failed_child
    version = Version(tmp_path / "candidate", Path(sys.executable),
                      tmp_path / "candidate" / "profile.json", {}, release=20)

    with pytest.raises(RuntimeError) as failure:
        backend.install_defaults(version, {})

    assert error_text in str(failure.value)
