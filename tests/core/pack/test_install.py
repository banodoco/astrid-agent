"""Real Git sources, canonical validator and OS publication; no component fakes."""

from __future__ import annotations

import json
import os
import selectors
import subprocess
import sys
from pathlib import Path

import pytest

from astrid.core.pack.identity import pack_digest, read_pin, write_pin
from astrid.core.pack.install import PackInstallError, install_pack
from astrid.core.pack.personal import ensure_personal_pack


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], check=True, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout.strip()


def manifest(pack_id="hello_ext", api="1.0", extra="") -> str:
    return (f'schema_version: 3\nid: {pack_id}\nname: Hello\nversion: 1.0.0\npack_api: "{api}"\n'
            'documentation: {kind: none, reason: Fixture.}\n' + extra)


class Repository:
    def __init__(self, root: Path, *, pack_id="hello_ext", subpath="."):
        self.work = root / "work"
        self.bare = root / "source.git"
        self.work.mkdir(parents=True)
        git(self.work, "init", "-b", "main")
        git(self.work, "config", "user.name", "Install Test")
        git(self.work, "config", "user.email", "install@example.invalid")
        self.pack = self.work / subpath
        self.pack.mkdir(exist_ok=True, parents=True)
        (self.pack / "pack.yaml").write_text(manifest(pack_id))
        (self.pack / "payload.txt").write_text("old\n")
        self.commit = self.publish()
        git(root, "clone", "--bare", str(self.work), str(self.bare))

    def publish(self) -> str:
        git(self.work, "add", "-A")
        git(self.work, "commit", "-m", "fixture")
        self.commit = git(self.work, "rev-parse", "HEAD")
        if self.bare.exists():
            git(self.work, "push", str(self.bare), "HEAD:refs/heads/main")
        return self.commit


@pytest.fixture
def repo(tmp_path):
    return Repository(tmp_path / "repo")


@pytest.fixture
def root(tmp_path):
    return tmp_path / "Astrid/packs"


def install(repo, root, **kwargs):
    return install_pack(source=repo.bare, revision=repo.commit, pack_id="hello_ext",
                        packs_root=root, python_packages={}, **kwargs)


def snapshot(root: Path):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def assert_clean(root):
    assert not list(root.glob(".install-*"))


def test_git_install_resolves_ref_checks_digest_and_writes_pin_without_executing(repo, root, tmp_path):
    sentinel = tmp_path / "EXECUTED"
    (repo.pack / "__init__.py").write_text(f"from pathlib import Path\nPath({str(sentinel)!r}).touch()\nraise RuntimeError('executed')\n")
    # Tracked Git attributes and hooks must not make installing execute code.
    (repo.pack / ".gitattributes").write_text("*.txt filter=explode\n")
    hook = repo.work / ".git/hooks/post-checkout"
    hook.write_text(f"#!/bin/sh\ntouch '{sentinel}'\n")
    hook.chmod(0o755)
    git(repo.work, "config", "filter.explode.smudge", f"touch '{sentinel}'")
    repo.publish()
    digest = pack_digest(repo.pack)

    result = install_pack(source=repo.bare.as_uri(), revision="main", pack_id="hello_ext",
                          packs_root=root, expected_digest=digest)

    assert result.changed and result.commit == repo.commit
    assert result.observed_digest == pack_digest(root / "hello_ext") == digest
    assert read_pin(result.path / "PIN") == {
        "version": 1, "source": repo.bare.as_uri(), "commit": repo.commit,
        "digest_rule": "pack-tree-v1", "digest": digest, "origin": "user",
    }
    assert not sentinel.exists()
    assert not (result.path / ".git").exists()
    assert_clean(root)


@pytest.mark.parametrize("revision_kind", ["ancestor", "tag", "head"])
def test_reachable_revisions(repo, root, revision_kind):
    previous = repo.commit
    git(repo.work, "tag", "-a", "v1", "-m", "annotated")
    git(repo.work, "push", str(repo.bare), "--tags")
    (repo.pack / "payload.txt").write_text("new\n")
    repo.publish()
    revision = {"ancestor": previous, "tag": "v1", "head": "HEAD"}[revision_kind]
    result = install_pack(source=repo.bare, revision=revision, pack_id="hello_ext", packs_root=root)
    assert result.commit == (repo.commit if revision_kind == "head" else previous)


@pytest.mark.parametrize("kind", ["unknown", "other_repository", "dangling_head", "dangling_sha"])
def test_unreachable_revision_preserves_old_install(repo, root, tmp_path, kind):
    install(repo, root)
    before = snapshot(root / "hello_ext")
    revision = "f" * 40
    if kind == "other_repository":
        other = Repository(tmp_path / "other")
        (other.pack / "payload.txt").write_text("different\n")
        revision = other.publish()
    elif kind in {"dangling_head", "dangling_sha"}:
        (repo.pack / "payload.txt").write_text("unreferenced\n")
        revision = repo.publish()
        git(repo.bare, "update-ref", "refs/heads/main", read_pin(root / "hello_ext/PIN")["commit"])
        git(repo.bare, "update-ref", "--no-deref", "HEAD", revision)
        if kind == "dangling_head":
            revision = "HEAD"
    with pytest.raises(PackInstallError) as failure:
        install_pack(source=repo.bare, revision=revision, pack_id="hello_ext", packs_root=root)
    assert failure.value.code == "unreachable_revision"
    assert snapshot(root / "hello_ext") == before
    assert_clean(root)


@pytest.mark.parametrize("kind", ["expected_digest", "source_pin_digest", "source_pin_commit", "source_pin_source", "release_pin"])
def test_pin_mismatches_preserve_old_install(repo, root, kind):
    install(repo, root)
    before = snapshot(root / "hello_ext")
    kwargs = {}
    if kind == "expected_digest":
        kwargs["expected_digest"] = "0" * 64
    elif kind == "release_pin":
        kwargs["release_pins"] = {"hello_ext": {"commit": repo.commit, "digest": "0" * 64}}
    else:
        pin = read_pin(root / "hello_ext/PIN")
        pin[{"source_pin_digest": "digest", "source_pin_commit": "commit", "source_pin_source": "source"}[kind]] = {
            "source_pin_digest": "0" * 64, "source_pin_commit": "0" * 40,
            "source_pin_source": "file:///wrong.git",
        }[kind]
        write_pin(repo.pack / "PIN", pin)
        repo.publish()
    with pytest.raises(PackInstallError) as failure:
        install(repo, root, **kwargs)
    assert failure.value.code == "pin_mismatch"
    assert snapshot(root / "hello_ext") == before
    assert_clean(root)


@pytest.mark.parametrize(("extra", "api", "code"), [
    ("", "2.0", "api_major"),
    ("dependencies: {packs: [absent]}\n", "1.0", "missing_dep"),
    ("dependencies: {python_requirements: [no-such-installed-distribution]}\n", "1.0", "missing_python"),
    ("", "1.1", "api_minor"),
])
def test_real_pure_validator_refusals_preserve_install(repo, root, extra, api, code):
    install(repo, root)
    before = snapshot(root / "hello_ext")
    (repo.pack / "pack.yaml").write_text(manifest(api=api, extra=extra))
    repo.publish()
    with pytest.raises(PackInstallError) as failure:
        install(repo, root)
    assert failure.value.code == code
    assert failure.value.compatibility["ok"] is False
    assert snapshot(root / "hello_ext") == before
    assert_clean(root)


@pytest.mark.parametrize("payload", ["import astrid.core.audit\n", "def bad(:\n"])
def test_real_import_checker_refusal(repo, root, payload):
    install(repo, root)
    before = snapshot(root / "hello_ext")
    (repo.pack / "unsafe.py").write_text(payload)
    repo.publish()
    with pytest.raises(PackInstallError) as failure:
        install(repo, root)
    assert failure.value.code == "import_violation"
    assert snapshot(root / "hello_ext") == before


@pytest.mark.parametrize("extra", ["surprise: true\n", "engines: {local: {recipe: ../escape.json}}\n"])
def test_canonical_schema_refusal(repo, root, extra):
    install(repo, root)
    before = snapshot(root / "hello_ext")
    (repo.pack / "pack.yaml").write_text(manifest(extra=extra))
    repo.publish()
    with pytest.raises(PackInstallError):
        install(repo, root)
    assert snapshot(root / "hello_ext") == before


def test_folder_is_unpinned_and_does_not_publish_generated_artifacts(repo, root):
    (repo.pack / ".build").mkdir()
    (repo.pack / ".build/untrusted.js").write_text("throw 'unchecked'\n")
    result = install_pack(source=repo.pack, pack_id="hello_ext", packs_root=root)
    pin = read_pin(result.path / "PIN")
    assert result.origin == "folder" and result.commit is None
    assert pin["source"] == repo.pack.as_uri()
    assert pin["commit"] is pin["digest"] is None
    assert result.observed_digest == pack_digest(repo.pack)
    assert not (result.path / ".build").exists()


def test_monorepo_subpath_and_source_identity(tmp_path, root):
    repo = Repository(tmp_path / "mono", subpath="packs/hello_ext")
    (repo.work / "other.py").write_text("import astrid.core.private\n")
    repo.publish()
    result = install(repo, root, subpath="packs/hello_ext")
    assert read_pin(result.path / "PIN")["source"] == repo.bare.as_uri() + "#packs/hello_ext"
    assert not (result.path / "other.py").exists()


@pytest.mark.parametrize("kind", ["payload", "excluded", "nested_excluded", "destination", "ancestor", "source", "lock"])
def test_symlinks_refused_and_previous_install_intact(repo, root, tmp_path, kind):
    install(repo, root)
    before = snapshot(root / "hello_ext")
    outside = tmp_path / "outside"
    outside.mkdir()
    kwargs = {}
    if kind in {"payload", "excluded", "nested_excluded"}:
        link = repo.pack / {"payload": "evil", "excluded": ".build", "nested_excluded": ".cache/evil"}[kind]
        link.parent.mkdir(exist_ok=True)
        link.symlink_to(outside, target_is_directory=True)
        repo.publish()
    elif kind == "destination":
        (root / "hello_ext").rename(outside / "old")
        (root / "hello_ext").symlink_to(outside / "old", target_is_directory=True)
    elif kind == "ancestor":
        alias = tmp_path / "alias"
        alias.symlink_to(root.parent, target_is_directory=True)
        root = alias / "packs"
    elif kind == "source":
        alias = tmp_path / "alias"
        alias.symlink_to(repo.bare, target_is_directory=True)
        kwargs["source"] = alias
    else:
        (root / ".install.lock").unlink()
        (root / ".install.lock").symlink_to(outside / "lock")
    with pytest.raises(PackInstallError):
        install_pack(source=kwargs.get("source", repo.bare), revision=repo.commit,
                     pack_id="hello_ext", packs_root=root)
    assert snapshot(root / "hello_ext") == before
    assert not list(outside.glob("*.py"))


@pytest.mark.parametrize("pack_id", ["personal", "../personal", "/personal", "hello_ext/../../personal", "HELLO", "core", "generation"])
def test_unsafe_and_reserved_ids_cannot_touch_personal(repo, root, pack_id):
    personal = ensure_personal_pack(root)
    (personal / "precious.txt").write_bytes(b"user code\0\xff")
    before = snapshot(personal)
    with pytest.raises(PackInstallError):
        install_pack(source=repo.bare, revision=repo.commit, pack_id=pack_id, packs_root=root)
    assert snapshot(personal) == before


@pytest.mark.parametrize("subpath", ["../hello_ext", "/hello_ext", "packs/../../personal", "packs\\hello_ext", ".git", "C:/escape"])
def test_subpath_escape_refused(repo, root, subpath):
    with pytest.raises(PackInstallError):
        install(repo, root, subpath=subpath)
    assert not (root / "hello_ext").exists()


def test_personal_manifest_disguised_as_normal_pack_refused(repo, root):
    install(repo, root)
    before = snapshot(root / "hello_ext")
    (repo.pack / "pack.yaml").write_text(manifest("personal"))
    repo.publish()
    with pytest.raises(PackInstallError):
        install(repo, root)
    assert snapshot(root / "hello_ext") == before


def test_idempotence_and_atomic_update(repo, root):
    first = install(repo, root)
    inode = first.path.stat().st_ino
    before = snapshot(first.path)
    assert install(repo, root).changed is False
    assert first.path.stat().st_ino == inode
    assert snapshot(first.path) == before
    (repo.pack / "payload.txt").write_text("new\n")
    repo.publish()
    assert install(repo, root).changed is True
    assert first.path.stat().st_ino != inode
    assert (first.path / "payload.txt").read_text() == "new\n"
    assert read_pin(first.path / "PIN")["digest"] == pack_digest(first.path)
    assert_clean(root)


def test_host_hook_failure_and_tampering_never_publish(repo, root):
    install(repo, root)
    before = snapshot(root / "hello_ext")
    (repo.pack / "payload.txt").write_text("new\n")
    repo.publish()

    def fail(stage):
        raise RuntimeError("quiescence/build failed")

    with pytest.raises(RuntimeError, match="quiescence"):
        install(repo, root, before_swap=fail)
    assert snapshot(root / "hello_ext") == before

    def tamper(stage):
        (stage / "payload.txt").write_text("changed after validation")

    with pytest.raises(PackInstallError, match="changed before publication"):
        install(repo, root, before_swap=tamper)
    assert snapshot(root / "hello_ext") == before
    assert_clean(root)


def child_install(repo, root):
    script = """
import sys
from astrid.core.pack.install import install_pack
def ready(stage):
    print(str(stage), flush=True)
    sys.stdin.read(1)
result = install_pack(source=sys.argv[1], revision=sys.argv[2], pack_id='hello_ext', packs_root=sys.argv[3], before_swap=ready)
print(result.commit, flush=True)
"""
    return subprocess.Popen([sys.executable, "-c", script, str(repo.bare), repo.commit, str(root)],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def ready_line(child):
    with selectors.DefaultSelector() as selector:
        selector.register(child.stdout, selectors.EVENT_READ)
        assert selector.select(timeout=20), "installer did not reach verified staging boundary"
    line = child.stdout.readline().strip()
    assert line, child.stderr.read()
    return line


@pytest.mark.parametrize("previous", [True, False])
def test_kill_between_verified_stage_and_swap_leaves_complete_old_install(repo, root, previous):
    before = None
    if previous:
        install(repo, root)
        before = snapshot(root / "hello_ext")
    (repo.pack / "payload.txt").write_text("new\n")
    repo.publish()
    child = child_install(repo, root)
    try:
        stage = Path(ready_line(child))
        assert read_pin(stage / "PIN")["commit"] == repo.commit
        assert (stage / "payload.txt").read_text() == "new\n"
        # Real SIGKILL, with the real primitive blocked after full verification.
        child.kill()
        child.wait(timeout=10)
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=10)
    if previous:
        assert snapshot(root / "hello_ext") == before
    else:
        assert not (root / "hello_ext").exists()
    assert all(p.name.startswith(".") or p.name == "hello_ext" for p in root.iterdir())
    # Retry ignores abandoned staging and releases the dead process's OS lock.
    assert install(repo, root).commit == repo.commit


def test_real_concurrent_installers_serialize_and_revalidate(repo, root):
    install(repo, root)
    (repo.pack / "payload.txt").write_text("first\n")
    repo.publish()
    first = child_install(repo, root)
    second = None
    try:
        ready_line(first)
        (repo.pack / "payload.txt").write_text("second\n")
        repo.publish()
        second = child_install(repo, root)
        with selectors.DefaultSelector() as selector:
            selector.register(second.stdout, selectors.EVENT_READ)
            assert not selector.select(timeout=0.15)
        assert (root / "hello_ext/payload.txt").read_text() == "old\n"
        first.stdin.write("x")
        first.stdin.flush()
        first.wait(timeout=20)
        assert first.returncode == 0, first.stderr.read()
        ready_line(second)
        assert (root / "hello_ext/payload.txt").read_text() == "first\n"
        second.stdin.write("x")
        second.stdin.flush()
        second.wait(timeout=20)
        assert second.returncode == 0, second.stderr.read()
        assert (root / "hello_ext/payload.txt").read_text() == "second\n"
        assert read_pin(root / "hello_ext/PIN")["commit"] == repo.commit
        assert_clean(root)
    finally:
        for child in (first, second):
            if child is not None:
                if child.poll() is None:
                    child.kill()
                child.communicate(timeout=10)


def test_shared_root_resolver_and_personal_preservation(repo, root, monkeypatch):
    personal = ensure_personal_pack(root)
    (personal / "precious.py").write_text("user content\n")
    before = snapshot(personal)
    monkeypatch.setenv("ASTRID_PACKS_PATH", str(root))
    result = install_pack(source=repo.bare, revision=repo.commit, pack_id="hello_ext")
    assert result.path == root / "hello_ext"
    assert snapshot(personal) == before


def test_default_cannot_overwrite_user_pack(repo, root):
    install(repo, root)
    before = snapshot(root / "hello_ext")
    with pytest.raises(PackInstallError) as failure:
        install(repo, root, origin="default", expected_digest=pack_digest(repo.pack))
    assert failure.value.code == "default_pin_conflict"
    assert snapshot(root / "hello_ext") == before


def test_staged_dependencies_use_destination_identity(repo, root):
    result = install(repo, root)
    # Updates accept the same selected identity, but refuse another selected root.
    facts = {"hello_ext": {"id": "hello_ext", "canonical_root": str(result.path)}}
    assert not install(repo, root, selected=facts).changed
    facts["hello_ext"]["canonical_root"] += "_elsewhere"
    with pytest.raises(PackInstallError) as failure:
        install(repo, root, selected=facts)
    assert failure.value.code == "duplicate_id"


def test_folder_special_file_refusal_does_not_block_or_publish(repo, root):
    install(repo, root)
    before = snapshot(root / "hello_ext")
    os.mkfifo(repo.pack / "pipe")
    with pytest.raises(PackInstallError):
        install_pack(source=repo.pack, pack_id="hello_ext", packs_root=root)
    assert snapshot(root / "hello_ext") == before


def test_git_submodule_refused_without_fetching_or_running_it(repo, root):
    install(repo, root)
    before = snapshot(root / "hello_ext")
    git(repo.work, "update-index", "--add", "--cacheinfo", f"160000,{repo.commit},submodule")
    git(repo.work, "commit", "-m", "unsafe gitlink")
    repo.commit = git(repo.work, "rev-parse", "HEAD")
    git(repo.work, "push", str(repo.bare), "HEAD:refs/heads/main")
    with pytest.raises(PackInstallError) as failure:
        install(repo, root)
    assert failure.value.code == "unsafe_path"
    assert snapshot(root / "hello_ext") == before


def test_case_colliding_git_directory_prefixes_refused(repo, root):
    install(repo, root)
    before = snapshot(root / "hello_ext")
    for directory in ("Foo", "foo"):
        (repo.pack / directory).mkdir()
        (repo.pack / directory / (directory + ".txt")).write_text("payload")
    repo.publish()
    with pytest.raises(PackInstallError) as failure:
        install(repo, root)
    assert failure.value.code == "unsafe_path"
    assert snapshot(root / "hello_ext") == before


def test_actual_atomic_swap_permission_failure_preserves_old_install(repo, root, tmp_path):
    install(repo, root)
    before = snapshot(root / "hello_ext")
    (repo.pack / "payload.txt").write_text("new\n")
    repo.publish()
    # Root would bypass directory permissions. Drop its DAC capabilities in
    # the subprocess; this also works in a single-uid Linux user namespace.
    # No installer or publication component is mocked.
    script = """
import ctypes, json, os, sys
from pathlib import Path
from astrid.core.pack.install import install_pack, PackInstallError
root = Path(sys.argv[3])
if os.getuid() == 0:
    class Header(ctypes.Structure):
        _fields_ = [('version', ctypes.c_uint32), ('pid', ctypes.c_int)]
    class Data(ctypes.Structure):
        _fields_ = [('effective', ctypes.c_uint32), ('permitted', ctypes.c_uint32), ('inheritable', ctypes.c_uint32)]
    libc = ctypes.CDLL(None, use_errno=True)
    header, caps = Header(0x20080522, 0), (Data * 2)()
    assert libc.capget(ctypes.byref(header), caps) == 0
    caps[0].effective &= ~6
    caps[0].permitted &= ~6
    caps[0].inheritable &= ~6
    assert libc.capset(ctypes.byref(header), caps) == 0
def deny_swap(stage):
    root.chmod(0o500)
try:
    try:
        install_pack(source=sys.argv[1], revision=sys.argv[2], pack_id='hello_ext', packs_root=root, before_swap=deny_swap)
    except PackInstallError as error:
        print(json.dumps({'code': error.code}), flush=True)
    else:
        raise AssertionError('publication should have failed')
finally:
    root.chmod(0o700)
"""
    child = subprocess.run([sys.executable, "-c", script, str(repo.bare), repo.commit, str(root)],
                           text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
    assert child.returncode == 0, child.stderr
    assert json.loads(child.stdout) == {"code": "swap_failed"}
    assert snapshot(root / "hello_ext") == before
    assert install(repo, root).commit == repo.commit


@pytest.mark.parametrize("source", ["ext::sh -c evil", "https://TOKEN@github.com/repo.git", "https://user:secret@github.com/repo.git"])
def test_unsafe_git_sources_refused_without_execution_or_credentials(repo, root, source):
    with pytest.raises(PackInstallError):
        install_pack(source=source, revision=repo.commit, pack_id="hello_ext", packs_root=root)
    assert not (root / "hello_ext").exists()


def test_valid_default_is_digest_pinned_and_idempotent(repo, root):
    digest = pack_digest(repo.pack)
    result = install(repo, root, origin="default", expected_digest=digest,
                     release_pins={"hello_ext": {"commit": repo.commit, "digest": digest}})
    assert result.origin == "default"
    assert read_pin(result.path / "PIN")["origin"] == "default"
    assert not install(repo, root, origin="default", expected_digest=digest).changed


def test_folder_pin_source_cannot_be_relabelled_and_source_cannot_contain_staging(repo, root):
    folder = install_pack(source=repo.pack, pack_id="hello_ext", packs_root=root).path
    before = snapshot(folder)
    with pytest.raises(PackInstallError) as failure:
        install_pack(source=folder, pack_id="hello_ext", packs_root=root)
    assert failure.value.code == "pin_mismatch"
    with pytest.raises(PackInstallError) as failure:
        install_pack(source=root, pack_id="hello_ext", packs_root=root)
    assert failure.value.code == "unsafe_path"
    assert snapshot(folder) == before


def test_reachable_commit_with_unborn_remote_head_is_accepted(repo, root):
    git(repo.bare, "symbolic-ref", "HEAD", "refs/heads/does_not_exist")
    assert install(repo, root).commit == repo.commit


def test_default_cannot_overwrite_an_existing_folder_without_a_pin(repo, root):
    folder = root / "hello_ext"
    folder.mkdir(parents=True)
    (folder / "pack.yaml").write_text(manifest())
    (folder / "precious.py").write_text("user content\n")
    before = snapshot(folder)
    with pytest.raises(PackInstallError) as failure:
        install(repo, root, origin="default", expected_digest=pack_digest(repo.pack))
    assert failure.value.code == "default_pin_conflict"
    assert snapshot(folder) == before


def test_personal_origin_cannot_be_replaced_even_under_another_directory_id(repo, root):
    install(repo, root)
    pin = read_pin(root / "hello_ext/PIN")
    write_pin(root / "hello_ext/PIN", {**pin, "origin": "personal", "commit": None, "digest": None})
    before = snapshot(root / "hello_ext")
    with pytest.raises(PackInstallError) as failure:
        install(repo, root)
    assert failure.value.code == "reserved_id"
    assert snapshot(root / "hello_ext") == before


def test_host_build_outputs_publish_even_when_source_and_pin_are_unchanged(repo, root):
    install(repo, root)

    def build(stage):
        (stage / ".build").mkdir()
        (stage / ".build/entry.js").write_text("// verified host-generated fixture\n")

    assert install(repo, root, before_swap=build).changed
    assert (root / "hello_ext/.build/entry.js").read_text() == "// verified host-generated fixture\n"
    assert read_pin(root / "hello_ext/PIN")["digest"] == pack_digest(root / "hello_ext")
