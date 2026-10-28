"""Pinned local source overrides use real Git commits, never mutable checkouts."""

import io
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
from runtime_protocol import platform

from astrid.core.generation.vibecomfy_dependency import VIBECOMFY_ENGINE_REVISION
from astrid.installer import cli, tools
from astrid.installer.backend import Backend
from astrid.installer.core import (
    RuntimeOperationError,
    VerificationError,
    Version,
    switch,
    write_json,
)
from astrid.installer.service import LABEL, venv_python


def test_first_tool_archive_creates_its_missing_parent(tmp_path, monkeypatch):
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as bundle:
        bundle.writestr("bun-linux-x64/bun", b"tool executable")
    requests = []

    def response(request, *, timeout):
        url = request.full_url
        requests.append((url, timeout))
        return io.BytesIO(payload.getvalue())

    monkeypatch.setattr(tools.urllib.request, "urlopen", response)
    destination = tmp_path / "fresh-version" / "tools" / "bun"
    assert not destination.parent.exists()

    tools.fetch_archive("https://example.invalid/bun.zip", destination)

    assert requests == [("https://example.invalid/bun.zip", 120)]
    assert (destination / "bun-linux-x64" / "bun").read_bytes() == b"tool executable"
    assert not destination.with_name("bun.download").exists()


def manifest():
    value = {
        "schema_version": 1,
        "agent": {"sha": "a" * 40},
        "runtime": {"sha": "b" * 40},
        "tools": {name: {"version": "20.19.4" if name == "node" else "1.2.3"}
                  for name in ("uv", "python", "bun", "node", "ffmpeg")},
    }
    value["tools"]["omp"] = {"source": tools.DEFAULT_OMP_SOURCE}
    return value


def git(repo, *args):
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull})
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=Installer test",
         "-c", "user.email=installer@example.invalid", *args],
        env=env, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    ).stdout.decode().strip()


@pytest.fixture
def repository(tmp_path):
    repo = tmp_path / "checkout with spaces"
    repo.mkdir()
    git(repo, "init", "--quiet")
    (repo / "pyproject.toml").write_text("pinned source\n", encoding="utf-8")
    (repo / "nested").mkdir()
    (repo / "nested" / "file.txt").write_text("nested content\n", encoding="utf-8")
    (repo / "launch").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "update-index", "--chmod=+x", "launch")
    git(repo, "commit", "--quiet", "-m", "Pinned source")
    return repo, git(repo, "rev-parse", "HEAD")


@pytest.mark.parametrize("as_url", [False, True])
@pytest.mark.parametrize("name", ["agent", "vibecomfy"])
def test_local_sources_materialize_only_the_pinned_commit(repository, tmp_path, monkeypatch, as_url, name):
    repo, sha = repository
    (repo / "pyproject.toml").write_text("later commit\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "--quiet", "-m", "Later source")
    (repo / "nested" / "file.txt").write_text("uncommitted edit\n", encoding="utf-8")
    (repo / "untracked").write_text("do not install", encoding="utf-8")
    monkeypatch.setattr(tools, "download", lambda *_args, **_kwargs: pytest.fail("local source downloaded"))
    # A caller's Git settings cannot redirect the selected repository or index.
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "wrong repository"))
    monkeypatch.setenv("GIT_INDEX_FILE", str(tmp_path / "wrong index"))

    destination = tmp_path / "installed"
    assert tools.source(name, sha, destination, repo=repo.as_uri() if as_url else str(repo)) == destination

    assert (destination / "pyproject.toml").read_text() == "pinned source\n"
    assert (destination / "nested" / "file.txt").read_text() == "nested content\n"
    assert not (destination / "untracked").exists()
    assert not (destination / ".git").exists()
    if os.name != "nt":
        assert (destination / "launch").stat().st_mode & 0o111
    assert not (tmp_path / "installed.unpack").exists()
    assert not (tmp_path / "installed.source.tar").exists()
    assert (repo / "nested" / "file.txt").read_text() == "uncommitted edit\n"


@pytest.mark.parametrize("repo", ["relative/path", "https://example.invalid/repo.git", "git@example.invalid:repo",
                                  "file://other-host/repo", "file:///repo?query=1", "file:///repo#fragment",
                                  "", None, 12])
def test_manifest_rejects_nonlocal_or_ambiguous_source_repositories(repo):
    value = manifest()
    value["agent"]["repo"] = repo
    with pytest.raises(ValueError, match="source repo"):
        tools.validate_manifest(value)


@pytest.mark.parametrize("name", ["agent", "runtime", "vibecomfy"])
@pytest.mark.parametrize("as_url", [False, True])
def test_manifest_accepts_local_source_overrides_and_preserves_pins(tmp_path, name, as_url):
    value = manifest()
    value["vibecomfy"] = {"sha": VIBECOMFY_ENGINE_REVISION}
    repo = tmp_path / "source checkout"
    value[name]["repo"] = repo.as_uri() if as_url else str(repo)
    assert tools.validate_manifest(value) is value
    assert value[name]["sha"] == {"agent": "a" * 40, "runtime": "b" * 40,
                                  "vibecomfy": VIBECOMFY_ENGINE_REVISION}[name]


@pytest.mark.parametrize("sha", ["main", "a" * 7, "A" * 40, "a" * 39 + "/"])
def test_source_override_still_requires_full_commit_sha(tmp_path, sha):
    value = manifest()
    value["agent"].update(repo=str(tmp_path), sha=sha)
    with pytest.raises(ValueError, match="full git SHA"):
        tools.validate_manifest(value)
    with pytest.raises(ValueError, match="full git SHA"):
        tools.source("agent", sha, tmp_path / "installed", repo=str(tmp_path))


@pytest.mark.parametrize("revision", ["missing", "tree", "tag"])
def test_local_source_requires_an_available_commit_object(repository, tmp_path, revision):
    repo, sha = repository
    if revision == "missing":
        sha = "0" * 40
    elif revision == "tree":
        sha = git(repo, "rev-parse", "HEAD^{tree}")
    else:
        git(repo, "tag", "-a", "tagged", "-m", "Annotated tag")
        sha = git(repo, "rev-parse", "tagged")
    with pytest.raises(ValueError, match="Cannot read local source|must identify a Git commit"):
        tools.source("agent", sha, tmp_path / "installed", repo=str(repo))
    assert not (tmp_path / "installed").exists()
    assert not (tmp_path / "installed.unpack").exists()
    assert not (tmp_path / "installed.source.tar").exists()


@pytest.mark.parametrize("mode", ["120000", "160000"])
def test_local_source_rejects_tracked_links_and_submodules(repository, tmp_path, mode):
    repo, sha = repository
    object_id = sha if mode == "160000" else git(repo, "rev-parse", "HEAD:pyproject.toml")
    git(repo, "update-index", "--add", "--cacheinfo", f"{mode},{object_id},linked-entry")
    git(repo, "commit", "--quiet", "-m", "Unsupported source entry")
    sha = git(repo, "rev-parse", "HEAD")
    with pytest.raises(ValueError, match="link or submodule: linked-entry"):
        tools.source("agent", sha, tmp_path / "installed", repo=str(repo))
    assert not (tmp_path / "installed").exists()


def test_local_source_reports_missing_git(repository, tmp_path, monkeypatch):
    repo, sha = repository

    def missing_git(*_args, **_kwargs):
        raise FileNotFoundError("git")

    monkeypatch.setattr(tools.subprocess, "run", missing_git)
    with pytest.raises(ValueError, match="require Git on PATH"):
        tools.source("agent", sha, tmp_path / "installed", repo=str(repo))


def test_local_source_cleans_failed_extraction(repository, tmp_path, monkeypatch):
    repo, sha = repository

    def failed_extraction(_archive, staging):
        staging.mkdir()
        (staging / "partial").write_text("incomplete")
        raise ValueError("failed extraction")

    monkeypatch.setattr(tools, "extract", failed_extraction)
    with pytest.raises(ValueError, match="failed extraction"):
        tools.source("agent", sha, tmp_path / "installed", repo=str(repo))
    assert not (tmp_path / "installed").exists()
    assert not (tmp_path / "installed.unpack").exists()
    assert not (tmp_path / "installed.source.tar").exists()


@pytest.mark.parametrize("with_vibecomfy", [False, True])
@pytest.mark.parametrize("managed_by", [None, "pinokio"])
@pytest.mark.parametrize("release", [None, 73])
def test_stage_uses_source_overrides_and_records_manifest(tmp_path, monkeypatch, with_vibecomfy, managed_by, release):
    value = manifest()
    if release is not None:
        value["release"] = release
    value["agent"]["repo"] = str(tmp_path / "agent")
    value["runtime"]["repo"] = (tmp_path / "runtime").as_uri()
    if with_vibecomfy:
        value["vibecomfy"] = {"sha": "c" * 40, "repo": str(tmp_path / "vibecomfy")}
    backend = Backend(tmp_path / "program", tmp_path / "log", tmp_path / "uv", managed_by=managed_by)
    backend.uv.touch()
    # The CLI writes this marker before staging; install_identity reads managed_by from it.
    write_json(tmp_path / "program" / "install.json",
               {"owner": LABEL, "data_root": str(tmp_path / "data"), **({"managed_by": managed_by} if managed_by else {})})
    calls = []

    def materialize(name, sha, destination, *, repo=None):
        calls.append((name, sha, destination, repo))
        if name == "agent":
            (destination / "remotion/node_modules").mkdir(parents=True)
            (destination / "remotion/package-lock.json").write_text("{}\n")
        return destination

    monkeypatch.setattr("astrid.installer.backend.source", materialize)
    monkeypatch.setattr("astrid.installer.backend.install_tools", lambda *_args: {
        "PATH": "/tools", "ASTRID_NODE_EXECUTABLE": "/tools/node", "ASTRID_BUN_EXECUTABLE": "/tools/bun",
    })
    monkeypatch.setattr("astrid.installer.backend.binary", lambda *_args: tmp_path / "npm-cli.js")
    monkeypatch.setattr(backend, "run", lambda *_args, **_kwargs: "uv 1.2.3")
    monkeypatch.setattr(backend, "existing", lambda _data: None)
    monkeypatch.setattr(backend, "_installed_product_root", lambda python, _env: python.parent / "Lib/site-packages")
    monkeypatch.setattr(backend, "_declare_services", lambda **kwargs: kwargs["folder"] / "services.json")

    version = backend.stage(value, tmp_path / "program", tmp_path / "data")

    effective = {**value, "vibecomfy": value.get("vibecomfy", {"sha": VIBECOMFY_ENGINE_REVISION})}
    assert calls == [(name, effective[name]["sha"], version.root / directory, effective[name].get("repo"))
                     for name, directory in (("agent", "a"), ("runtime", "r"), ("vibecomfy", "v"))]
    assert json.loads((version.root / "manifest.json").read_text()) == effective
    expected_release = release or 0
    assert json.loads((version.root / "version.json").read_text())["identity"] == {
        "release": expected_release, "managed_by": managed_by,
    }
    assert version.env["ASTRID_INSTALL_RELEASE"] == str(expected_release)
    assert version.env["ASTRID_INSTALL_MANAGED_BY"] == (managed_by or "")
    assert ("vibecomfy" in value) is with_vibecomfy
    assert "ASTRID_VIBECOMFY_CHECKOUT" not in version.env


@pytest.mark.parametrize("damage", ["release-missing", "release-differs", "managed-by-differs"])
def test_installed_profile_refuses_disagreeing_install_identity_markers(tmp_path, monkeypatch, damage):
    backend = Backend(tmp_path / "program", tmp_path / "log", tmp_path / "uv")
    folder = backend.root / "versions" / "candidate"
    version = Version(folder, folder / "py" / "bin" / "python", folder / "profile.json",
                      {"ASTRID_HOST_EXECUTION_PROFILE": "installed"}, release=5, managed_by="pinokio")
    if damage == "release-missing":
        del version.env["ASTRID_INSTALL_RELEASE"]
    elif damage == "release-differs":
        version.env["ASTRID_INSTALL_RELEASE"] = "6"
    else:
        version.env["ASTRID_INSTALL_MANAGED_BY"] = ""
    monkeypatch.setattr(backend, "run", lambda *_args, **_kwargs: pytest.fail("candidate was executed"))

    with pytest.raises(ValueError, match="install identity markers disagree"):
        backend.validate_candidate(version, tmp_path / "data", {"realm_id": "r", "realm_root": "/x"})


@pytest.mark.parametrize("uv_kind", ["relative", "missing", "directory"])
def test_stage_refuses_unusable_uv_before_creating_candidate(tmp_path, monkeypatch, uv_kind):
    if uv_kind == "relative":
        uv = Path("uv")
    elif uv_kind == "directory":
        uv = tmp_path / "uv-directory"
        uv.mkdir()
    else:
        uv = tmp_path / "missing-uv"
    backend = Backend(tmp_path / "program", tmp_path / "log", uv)
    monkeypatch.setattr(backend, "run", lambda *_args, **_kwargs: pytest.fail(
        "subprocess setup must not start without the bootstrap uv"))
    monkeypatch.setattr("astrid.installer.backend.source", lambda *_args, **_kwargs: pytest.fail(
        "source materialization must not start without the bootstrap uv"))

    with pytest.raises(ValueError, match="pinned user-local uv"):
        backend.stage(manifest(), backend.root, tmp_path / "data")

    assert not backend.root.exists()


@pytest.mark.parametrize("outcome", ["ready", "preparation-failure", "busy", "rollback"])
def test_forced_update_restages_damaged_agent_and_tools_from_fresh_candidate(
        tmp_path, monkeypatch, capsys, outcome):
    """A usable old Runtime/profile can carry ownership while payload is rebuilt."""
    home, root, data = tmp_path / "home", tmp_path / "program", tmp_path / "workspace"
    old_root = root / "versions" / "old"
    for path in (old_root / "a", old_root / "r", old_root / "tools"):
        path.mkdir(parents=True)
    (old_root / "a" / "broken-source.txt").write_text("damaged old Agent")
    (old_root / "tools" / "broken-tool").write_text("damaged old tool")
    old_profile = old_root / "profile.json"
    old_services = old_root / "services.json"
    old_services.write_text(json.dumps({"services": [{"name": "chat", "env": {
        "ASTRID_ACP_CWD": str(home / "saved-chat"),
        "ASTRID_ACP_PROFILE": "saved-profile",
    }}]}), encoding="utf-8")
    write_json(old_profile, {
        "profile": "astrid", "runtime_checkout": str(old_root / "r"),
        "source_checkout": str(old_root / "a"), "runtime_environment": str(old_root),
        "service_declaration": str(old_services),
    })
    old = Version(old_root, Path(sys.executable), old_profile,
                  {"PATH": os.defpath, "ASTRID_RUNTIME_DOOR_PORT": "17399"}, release=9)
    write_json(old_root / "version.json", old.document())
    root.mkdir(parents=True, exist_ok=True)
    switch(root, old)
    data.mkdir()
    workspace_sentinel = data / "user-work.txt"
    workspace_sentinel.write_bytes(b"user workspace stays byte-identical")
    write_json(root / "install.json", {"owner": LABEL, "data_root": str(data)})
    manifest_path = tmp_path / "candidate.json"
    candidate_manifest = manifest()
    candidate_manifest["release"] = 9
    write_json(manifest_path, candidate_manifest)
    uv = tmp_path / "uv"
    uv.touch()

    backend = Backend(root, tmp_path / "install.log", uv)
    backend.service.recorded = lambda: {}
    backend.service.login_service_exists = lambda: False
    backend.source_checkout_ownership = lambda *_args: False
    backend.preflight_stop = lambda *_args: None
    backend.workspace = lambda *_args: {"realm_id": "retained-realm", "realm_root": str(data / "realm")}
    backend.has_workspace = lambda _data: True
    calls = {name: [] for name in ("validate", "stop", "backup", "upgrade", "register", "start", "verify", "finish")}

    def validate_candidate(*args):
        calls["validate"].append(args[0])
        if outcome == "preparation-failure":
            raise ValueError("candidate rejected during preparation")

    def stop(version, _data):
        calls["stop"].append(version.root)
        if outcome == "busy" and version.root == old_root:
            raise RuntimeOperationError("Runtime is busy", details={"safe": False})

    def verify(version, *_args, **_kwargs):
        calls["verify"].append(version.root)
        if outcome == "rollback" and version.root != old_root:
            raise VerificationError("candidate did not become ready")

    backend.validate_candidate = validate_candidate
    backend.stop = stop
    backend.backup = lambda *_args: calls["backup"].append("called")
    backend.upgrade = lambda *_args: calls["upgrade"].append("called")
    backend.register = lambda version, *_args: calls["register"].append(version.root)
    backend.start = lambda version, *_args: calls["start"].append(version.root)
    backend.verify = verify
    backend.finish = lambda *_args: calls["finish"].append("called")
    backend.open = lambda: None
    materialized = []

    def source(name, sha, destination, **_kwargs):
        destination.mkdir(parents=True)
        (destination / "fresh-source.txt").write_text(f"{name}:{sha}")
        if name == "agent":
            (destination / "remotion/node_modules").mkdir(parents=True)
            (destination / "remotion/package-lock.json").write_text("{}\n")
        materialized.append((name, destination))
        return destination

    def run(argv, **_kwargs):
        args = [str(part) for part in argv]
        if args[1:] == ["--version"]:
            return "uv 1.2.3"
        if args[1] == "venv":
            environment = Path(args[-1])
            python = venv_python(environment)
            python.parent.mkdir(parents=True)
            python.touch()  # The subprocess boundary below supplies this candidate's commands.
            return ""
        if "find_spec" in args[-1]:
            # The real product-root probe runs for this candidate's interpreter; answer it from its venv.
            py = Path(args[0]).parent.parent
            purelib = py / "lib/site-packages"
            purelib.mkdir(parents=True, exist_ok=True)
            origins = {}
            for key in ("host", "child", "action", "client", "sdk"):
                origin = purelib / f"{key}.py"
                origin.write_text("", encoding="utf-8")
                origins[key] = str(origin)
            return json.dumps({"prefix": str(py), "purelib": str(purelib), "origins": origins})
        if any("source_checkout_digest" in part for part in args):
            return "fresh-source-digest"
        return ""

    monkeypatch.setattr("astrid.installer.backend.source", source)
    monkeypatch.setattr("astrid.installer.backend.install_tools", lambda _m, dest, *_args: (
        dest.mkdir(parents=True) or {
            "PATH": str(dest), "ASTRID_NODE_EXECUTABLE": str(dest / "node"),
            "ASTRID_BUN_EXECUTABLE": str(dest / "bun"),
        }))
    monkeypatch.setattr("astrid.installer.backend.binary", lambda *_args: tmp_path / "npm-cli.js")
    monkeypatch.setattr(backend, "run", run)
    monkeypatch.setattr(backend, "install_remotion_dependencies", lambda *_args: False)
    monkeypatch.setattr(backend, "_declare_services", lambda **kwargs: kwargs["folder"] / "services.json")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(platform, "log_dir", lambda: tmp_path / "logs")
    monkeypatch.setattr(platform, "private_directory", lambda path: Path(path).mkdir(
        parents=True, exist_ok=True))
    monkeypatch.setattr(cli, "Backend", lambda *_args, **_kwargs: backend)

    result = cli.main(["--update", "--root", str(root), "--data-root", str(data),
                       "--manifest", str(manifest_path), "--uv", str(uv)])

    candidates = [path for name, path in materialized if name == "agent"]
    assert len(candidates) == 1
    fresh_root = candidates[0].parent
    assert fresh_root != old_root
    assert json.loads((fresh_root / "profile.json").read_text(encoding="utf-8"))["source_checkout"] == str(
        (fresh_root / "py/lib/site-packages").resolve())
    assert (old_root / "a" / "broken-source.txt").read_text() == "damaged old Agent"
    assert (old_root / "tools" / "broken-tool").read_text() == "damaged old tool"
    assert (fresh_root / "a" / "fresh-source.txt").is_file()
    assert (fresh_root / "tools").is_dir()
    assert [name for name, _path in materialized] == ["agent", "runtime", "vibecomfy"]
    assert all(path.is_relative_to(fresh_root) for _name, path in materialized)
    staged = json.loads((fresh_root / "version.json").read_text(encoding="utf-8"))
    assert staged["identity"]["release"] == 9
    assert staged["env"]["ASTRID_RUNTIME_DOOR_PORT"] == "17399"
    assert (fresh_root / "profile.json").is_file()
    assert workspace_sentinel.read_bytes() == b"user workspace stays byte-identical"

    selected = (root / "current").read_text(encoding="utf-8").strip()
    transaction = json.loads((root / "update-transaction.json").read_text(encoding="utf-8")) \
        if (root / "update-transaction.json").exists() else None
    if outcome == "ready":
        assert result == 0
        assert selected == fresh_root.name
        assert transaction["step"] == "complete"
        assert calls["stop"] == [old_root, fresh_root]
        assert calls["backup"] == ["called"] and calls["upgrade"] == ["called"]
        assert calls["finish"] == ["called"]
    elif outcome == "preparation-failure":
        assert result == 1
        assert selected == old_root.name
        assert transaction is None
        assert len(calls["validate"]) == 1
        assert calls["stop"] == calls["backup"] == calls["upgrade"] == []
        assert calls["register"] == calls["start"] == calls["verify"] == []
    elif outcome == "busy":
        assert result == 1
        assert selected == old_root.name
        assert transaction["step"] == "complete"
        assert transaction["outcome"] == "stop_refused"
        assert calls["stop"] == [old_root]
        assert calls["backup"] == calls["upgrade"] == []
        assert calls["register"] == calls["start"] == calls["verify"] == []
    else:
        assert result == 1
        assert selected == old_root.name
        assert transaction["step"] == "rolled_back"
        assert calls["stop"] == [old_root, fresh_root, fresh_root]
        assert calls["backup"] == ["called"] and calls["upgrade"] == ["called"]
        assert calls["register"] == [fresh_root, old_root]
        assert calls["start"] == [fresh_root, old_root]
        assert calls["verify"] == [fresh_root, old_root]
    output = capsys.readouterr().out.lower()
    assert ("couldn't finish" not in output) is (outcome == "ready")


@pytest.mark.parametrize("pin", [None, {}, {"sha": None}, {"sha": "main"}, {"sha": "A" * 40}])
def test_manifest_rejects_invalid_vibecomfy_pin(pin):
    value = manifest()
    value["vibecomfy"] = pin
    with pytest.raises(ValueError, match="vibecomfy source must be pinned to a full git SHA"):
        tools.validate_manifest(value)


@pytest.mark.parametrize("repo", ["relative/path", "https://github.com/peteromallet/VibeComfy.git", None])
def test_manifest_rejects_invalid_vibecomfy_repository(repo):
    value = manifest()
    value["vibecomfy"] = {"sha": VIBECOMFY_ENGINE_REVISION, "repo": repo}
    with pytest.raises(ValueError, match="source repo"):
        tools.validate_manifest(value)


def test_legacy_manifest_needs_no_vibecomfy_entry():
    value = manifest()
    assert tools.validate_manifest(value) is value


def test_vibecomfy_download_uses_the_pinned_fork(tmp_path, monkeypatch):
    requests = []

    def fetch(url, destination):
        requests.append(url)
        package = destination / "VibeComfy-source" / "vibecomfy"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("# installed source\n")

    monkeypatch.setattr(tools, "fetch_archive", fetch)
    destination = tmp_path / "v"
    tools.source("vibecomfy", VIBECOMFY_ENGINE_REVISION, destination)
    assert requests == [
        f"https://github.com/peteromallet/VibeComfy/archive/{VIBECOMFY_ENGINE_REVISION}.tar.gz"
    ]
    assert (destination / "vibecomfy/__init__.py").is_file()


def test_installed_vibecomfy_is_visible_to_pack_host_without_checkout(tmp_path, monkeypatch):
    import importlib.util

    from astrid.core.execution.generic_host import (
        GenericPackHost,
        _install_host_vibecomfy_import_root,
    )
    from astrid.core.generation import gpu_environment
    from astrid.core.generation.vibecomfy_dependency import configured_vibecomfy_checkout

    # Package discovery is independent of platform GPU qualification.
    monkeypatch.setattr(gpu_environment, "windows_gpu_status", lambda *_args, **_kwargs: None)
    root = Path(__file__).resolve().parents[1]
    host = GenericPackHost(pack_roots=[root / "astrid/packs/vibecomfy"],
                           capability_matrix=root / "config/astrid-beta-capabilities.json")
    host.discover()
    find_spec = importlib.util.find_spec
    with monkeypatch.context() as missing:
        missing.setattr(importlib.util, "find_spec", lambda name: None if name == "vibecomfy" else find_spec(name))
        record, = host.preflight("vibecomfy.run")
        assert record.preflight["packages"] == {"ok": False, "missing": ["vibecomfy"]}
        assert not record.ready

    # Stand in for uv's installed wheel, with no source checkout on sys.path.
    site_packages = tmp_path / "site-packages"
    package = site_packages / "vibecomfy"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("# installed wheel fixture\n")
    monkeypatch.syspath_prepend(str(site_packages))
    monkeypatch.delitem(sys.modules, "vibecomfy", raising=False)
    monkeypatch.delenv("ASTRID_VIBECOMFY_CHECKOUT", raising=False)
    monkeypatch.setenv("ASTRID_HOST_EXECUTION_PROFILE", "installed")
    assert configured_vibecomfy_checkout() is None
    assert _install_host_vibecomfy_import_root() is None
    assert importlib.util.find_spec("vibecomfy").origin == str(package / "__init__.py")

    record, = host.preflight("vibecomfy.run")
    assert record.preflight["packages"] == {"ok": True, "missing": []}
    assert record.ready
