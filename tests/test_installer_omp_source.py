"""Exact published releases and selected fork commits build in owned prefixes."""

import hashlib
import io
import json
import subprocess
import tarfile
import zipfile
from pathlib import Path

import pytest

from astrid.installer import tools


def digest(value):
    return hashlib.sha256(value).hexdigest()


def tree_digest(files):
    output = hashlib.sha256(b"astrid-omp-source-tree-v1\0")
    for name, data in sorted(files.items()):
        name = name.encode("utf-8")
        output.update(len(name).to_bytes(8, "big"))
        output.update(name)
        output.update(hashlib.sha256(data).digest())
    return output.hexdigest()


def zipped(files):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        for path, value in files.items():
            archive.writestr(path, value)
    return stream.getvalue()


def fixture(tmp_path, monkeypatch):
    lock = b"frozen lock\n"
    files = {"bun.lock": lock, "main.ts": b"already fixed\n",
             "packages/coding-agent/package.json": b'{"version":"18.12.6"}'}
    source_url = tools.omp_source_url("https://github.com/owner/fork", "c" * 40)
    source = zipped({f"source/{name}": data for name, data in files.items()})
    native = zipped({"package/native.node": b"native"})
    recipe = {"version": "18.12.6", "bun_lock_sha256": digest(lock),
              "source": {"url": source_url},
              "native": {"linux-x64": {"url": "https://example.invalid/native.zip",
                         "sha256": digest(native), "files": ["native.node"]}}}
    payloads = {source_url: source, recipe["native"]["linux-x64"]["url"]: native}
    requests, calls = [], []

    def response(request, timeout):
        url = request.full_url
        requests.append(url)
        return io.BytesIO(payloads[url])

    def run(argv, *, extra_env=None, cwd=None, clean_bun_env=False):
        calls.append(([str(v) for v in argv], dict(extra_env or {}), cwd, clean_bun_env))
        if "link" in argv:
            folder = Path(extra_env["BUN_INSTALL_BIN"])
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "omp").write_bytes(b"launcher")
        return ""

    env = {"ASTRID_BUN_EXECUTABLE": "/qualified/bun", "BUN_INSTALL": str(tmp_path / "omp"),
           "BUN_INSTALL_BIN": str(tmp_path / "omp/bin"), "PATH": "/qualified:/usr/bin:/bin"}
    monkeypatch.setattr(tools.urllib.request, "urlopen", response)
    return recipe, payloads, requests, calls, run, env


def test_selected_fork_freezes_graph_generates_only_views_and_does_not_patch(tmp_path, monkeypatch):
    recipe, _payloads, requests, calls, run, env = fixture(tmp_path, monkeypatch)
    build_env = {**env, "BUN_INSTALL_GLOBAL_DIR": str(tmp_path / "omp/install/global"),
                 "HOME": str(tmp_path / "omp/build-home"),
                 "USERPROFILE": str(tmp_path / "omp/build-home"),
                 "XDG_CONFIG_HOME": str(tmp_path / "omp/build-home/config"),
                 "npm_config_registry": "https://registry.npmjs.org",
                 "NPM_CONFIG_REGISTRY": "https://registry.npmjs.org"}
    if "BUN_INSTALL_CACHE_DIR" in tools.os.environ:
        build_env["BUN_INSTALL_CACHE_DIR"] = tools.os.environ["BUN_INSTALL_CACHE_DIR"]
    tools._install_omp_source({"repo": "https://github.com/owner/fork", "sha": "c" * 40}, recipe, tmp_path / "omp", "linux-x64", run, env)
    source = tmp_path / "omp/source"
    assert requests == [recipe["source"]["url"], recipe["native"]["linux-x64"]["url"]]
    assert (source / "main.ts").read_bytes() == b"already fixed\n"
    assert (source / "bun.lock").read_bytes() == b"frozen lock\n"
    assert (source / "packages/natives/native/native.node").read_bytes() == b"native"
    assert not (tmp_path / "omp/native-archive").exists()
    assert not (tmp_path / "omp/source-archive").exists()
    assert calls[0][0] == ["/qualified/bun", "install", "--frozen-lockfile", "--ignore-scripts", "--omit", "optional"]
    assert calls[0][2] == source
    assert calls[1][0] == ["/qualified/bun", "run", "gen:tool-views"]
    assert calls[1][2] == source
    assert calls[2][0][:2] == ["/qualified/bun", "-e"]
    assert "__piNativesBuildVersion" in calls[2][0][2]
    assert calls[3][0] == ["/qualified/bun", "link", "--frozen-lockfile", "--ignore-scripts", "--no-save"]
    assert calls[3][2] == source / "packages/coding-agent"
    assert all(row[1] == build_env for row in calls)
    assert all(row[3] is True for row in calls)
    assert env["ASTRID_INSTALL_OMP_SOURCE"] == "fork"
    assert env["ASTRID_INSTALL_OMP_VERSION"] == "18.12.6"
    assert env["ASTRID_INSTALL_OMP_COMMIT"] == "c" * 40
    assert "ASTRID_INSTALL_OMP_PATCH_SHA256" not in env
    provenance = json.loads((tmp_path / "omp/source-provenance.json").read_text())
    assert provenance["commit"] == "c" * 40
    assert provenance["repo"] == "https://github.com/owner/fork"
    assert "patch_sha256" not in provenance
    assert provenance["tree_sha256"] == tree_digest({"bun.lock": b"frozen lock\n",
        "main.ts": b"already fixed\n", "packages/coding-agent/package.json": b'{"version":"18.12.6"}'})
    assert provenance["native_sha256"] == recipe["native"]["linux-x64"]["sha256"]
    assert provenance["version"] == "18.12.6"


def test_native_archive_digest_failure_prevents_using_untrusted_bytes(tmp_path, monkeypatch):
    recipe, payloads, _requests, calls, run, env = fixture(tmp_path, monkeypatch)
    payloads[recipe["native"]["linux-x64"]["url"]] += b"tampered"
    with pytest.raises(ValueError, match="checksum mismatch"):
        tools._install_omp_source({"repo": "https://github.com/owner/fork", "sha": "c" * 40}, recipe,
                                  tmp_path / "omp", "linux-x64", run, env)
    assert not any("link" in argv for argv, *_ in calls)


@pytest.mark.parametrize("change,match", [("directory", "directory layout"), ("lock", "lock"),
    ("version", "version"), ("path", "Unsafe archive entry"), ("native", "native")])
def test_invalid_source_lock_version_or_native_fails_before_link(tmp_path, monkeypatch, change, match):
    recipe, payloads, _requests, calls, run, env = fixture(tmp_path, monkeypatch)
    if change == "directory":
        payloads[recipe["source"]["url"]] = zipped({"one/a": b"a", "two/b": b"b"})
    elif change == "lock":
        recipe["bun_lock_sha256"] = "0" * 64
    elif change == "version":
        payloads[recipe["source"]["url"]] = zipped({"source/bun.lock": b"frozen lock\n",
            "source/packages/coding-agent/package.json": b'{"version":"18.0.0"}'})
    elif change == "path":
        payloads[recipe["source"]["url"]] = zipped({"../outside": b"unsafe"})
    else:
        recipe["native"]["linux-x64"]["files"] = ["missing.node"]
    with pytest.raises(ValueError, match=match):
        tools._install_omp_source({"repo": "https://github.com/owner/fork", "sha": "c" * 40}, recipe,
                                  tmp_path / "omp", "linux-x64", run, env)
    assert not any("link" in argv for argv, *_ in calls)


@pytest.mark.parametrize("step", ["install", "generator", "link"])
def test_lock_changes_at_any_build_step_are_refused(tmp_path, monkeypatch, step):
    recipe, _payloads, _requests, _calls, run, env = fixture(tmp_path, monkeypatch)

    def mutate(argv, **kwargs):
        result = run(argv, **kwargs)
        selected = {"install": "install", "generator": "gen:tool-views", "link": "link"}[step]
        if selected in argv:
            (tmp_path / "omp/source/bun.lock").write_bytes(b"resolved again")
        return result

    with pytest.raises(ValueError, match="lock"):
        tools._install_omp_source({"repo": "https://github.com/owner/fork", "sha": "c" * 40}, recipe, tmp_path / "omp", "linux-x64", mutate, env)
    assert not (tmp_path / "omp/source-provenance.json").exists()


def test_host_native_abi_failure_never_registers_the_launcher(tmp_path, monkeypatch):
    recipe, _payloads, _requests, calls, run, env = fixture(tmp_path, monkeypatch)

    def refuse(argv, **kwargs):
        if "-e" in argv:
            raise subprocess.CalledProcessError(1, argv, output="wrong native ABI")
        return run(argv, **kwargs)

    with pytest.raises(subprocess.CalledProcessError, match="non-zero"):
        tools._install_omp_source({"repo": "https://github.com/owner/fork", "sha": "c" * 40}, recipe, tmp_path / "omp", "linux-x64", refuse, env)
    assert not any("link" in argv for argv, *_ in calls)


def test_unsupported_native_target_fails_before_source_download(tmp_path, monkeypatch):
    recipe, _payloads, requests, calls, run, env = fixture(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="native.*windows-arm64"):
        tools._install_omp_source({"repo": "https://github.com/owner/fork", "sha": "c" * 40}, recipe, tmp_path / "omp", "windows-arm64", run, env)
    assert not requests and not calls


def test_staging_rejects_old_manifest_before_any_download(tmp_path, monkeypatch):
    requested = []
    monkeypatch.setattr(tools, "fetch_archive", lambda *a, **k: requested.append(a))
    with pytest.raises(ValueError, match="tools.omp.source"):
        tools.install_tools({"tools": {"omp": {"version": "12.0.0"}}}, tmp_path, "linux-x64", lambda *_: None)
    assert not requested


def test_bundled_recipe_pins_build_closure_without_selecting_or_patching_source():
    recipe = tools.load_omp_recipe()
    assert recipe["version"] == "18.12.6" and recipe["bun_version"] == "1.3.14"
    assert "source" not in recipe and "patch" not in recipe and "base_commit" not in recipe
    assert set(recipe["native"]) == {"linux-x64", "linux-arm64", "macos-x64", "macos-arm64", "windows-x64"}
    for asset in recipe["native"].values():
        assert tools.asset_downloads(asset) == [(asset["url"], asset["sha256"])]


def test_local_fork_uses_selected_commit_and_records_actual_tree(tmp_path, monkeypatch):
    recipe, _payloads, requests, calls, run, env = fixture(tmp_path, monkeypatch)
    archived = []

    def local(repo, commit, archive):
        archived.append((repo, commit))
        archive.write_bytes(zipped({"bun.lock": b"frozen lock\n", "main.ts": b"already fixed\n",
            "packages/coding-agent/package.json": b'{"version":"18.12.6"}'}))

    monkeypatch.setattr(tools, "local_source", local)
    tools._install_omp_source({"repo": "/own/producer", "sha": "e" * 40}, recipe, tmp_path / "omp", "linux-x64", run, env)
    assert archived == [("/own/producer", "e" * 40)]
    assert requests == [recipe["native"]["linux-x64"]["url"]]
    assert calls[0][2] == tmp_path / "omp/source"
    assert json.loads((tmp_path / "omp/source-provenance.json").read_text())["source_mode"] == "local-repo"
    assert not (tmp_path / "omp/source.download").exists()


def test_https_fork_uses_commit_archive_without_system_git(tmp_path, monkeypatch):
    recipe, _payloads, requests, _calls, run, env = fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(tools.subprocess, "run", lambda *_a, **_k: pytest.fail("HTTPS route called Git"))
    tools._install_omp_source({"repo": "https://github.com/owner/fork", "sha": "c" * 40}, recipe,
                              tmp_path / "omp", "linux-x64", run, env)
    assert requests[0].endswith("/" + "c" * 40)


@pytest.mark.parametrize("kind", [tarfile.SYMTYPE, tarfile.LNKTYPE])
def test_source_archive_links_are_refused_before_build(tmp_path, monkeypatch, kind):
    recipe, payloads, _requests, calls, run, env = fixture(tmp_path, monkeypatch)
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        member = tarfile.TarInfo("source/link")
        member.type, member.linkname = kind, "../outside"
        archive.addfile(member)
    payloads[recipe["source"]["url"]] = stream.getvalue()
    with pytest.raises(ValueError, match="contains a link"):
        tools._install_omp_source({"repo": "https://github.com/owner/fork", "sha": "c" * 40}, recipe,
                                  tmp_path / "omp", "linux-x64", run, env)
    assert not calls


def test_inherited_bun_registration_paths_are_overridden_only_during_build(tmp_path, monkeypatch):
    recipe, _payloads, _requests, calls, run, env = fixture(tmp_path, monkeypatch)
    outside = tmp_path / "unrelated-global"
    monkeypatch.setenv("BUN_INSTALL_GLOBAL_DIR", str(outside))
    user_home = tmp_path / "user-home"
    user_home.mkdir()
    monkeypatch.setenv("HOME", str(user_home))
    user_config = user_home / ".bunfig.toml"
    user_config.write_text('[install]\nglobalDir="outside-global"\nglobalBinDir="outside-bin"\n')
    before = (user_config.read_bytes(), user_config.stat().st_mtime_ns)
    tools._install_omp_source({"repo": "https://github.com/owner/fork", "sha": "c" * 40}, recipe, tmp_path / "omp", "linux-x64", run, env)
    assert all(row[1]["BUN_INSTALL_GLOBAL_DIR"] == str(tmp_path / "omp/install/global") for row in calls)
    assert all(row[1]["BUN_INSTALL_BIN"] == str(tmp_path / "omp/bin") for row in calls)
    assert "BUN_INSTALL_GLOBAL_DIR" not in env
    assert not outside.exists()
    assert (user_config.read_bytes(), user_config.stat().st_mtime_ns) == before


def test_default_and_comment_select_published_exact_release():
    assert tools.omp_source({}) == "@oh-my-pi/pi-coding-agent@18.12.6"
    assert tools.omp_source({"source": "@owner/fixed@17.4.3", "_comment": "See LANDING.md"}) == "@owner/fixed@17.4.3"


@pytest.mark.parametrize("value", ["latest", "omp", "omp@latest", "omp@^18.12.6", "omp@17.4", "omp@18.12.6 --preload=evil",
    "https://example.invalid/a.tgz", "file:///tmp/omp", "../omp@18.12.6", "@owner/omp@18.12.6#bad",
    {"repo": "https://github.com/owner/repo", "sha": "main"},
    {"repo": "https://github.com/owner/repo", "sha": "A" * 40},
    {"repo": "https://github.com/owner/repo", "sha": "a" * 7},
    {"repo": "https://github.com/owner/repo", "sha": "a" * 40, "patch": "evil"},
    {"repo": "https://github.com/owner/repo"}, None, 12])
def test_invalid_or_unpinned_source_is_refused(value):
    with pytest.raises(ValueError, match="OMP"):
        tools.omp_source({"source": value})


@pytest.mark.parametrize("pin", [{"version": "18.12.6"}, {"source": tools.DEFAULT_OMP_SOURCE, "repo": "/tmp/repo"},
                                  {"source": tools.DEFAULT_OMP_SOURCE, "_comment": 12}])
def test_ambiguous_old_source_knobs_and_invalid_metadata_are_refused(pin):
    with pytest.raises(ValueError, match="OMP"):
        tools.omp_source(pin)


@pytest.mark.parametrize("repo", ["https://github.com/owner/repo?x=1", "http://github.com/owner/repo",
    "https://other.invalid/owner/repo", "https://user@github.com/owner/repo", "https://github.com/owner/repo/tree/main",
    "relative/repo", "git@example.invalid:repo", "file://other-host/repo", "file:///repo?x=1", "", None])
def test_unsafe_fork_repositories_are_refused(repo):
    with pytest.raises(ValueError, match="repo"):
        tools.omp_source({"source": {"repo": repo, "sha": "a" * 40}})


@pytest.mark.parametrize("repo", ["https://github.com/owner/fork", "https://github.com/owner/fork.git"])
def test_remote_source_uses_commit_archive(repo):
    assert tools.omp_source_url(repo, "d" * 40) == f"https://codeload.github.com/owner/fork/tar.gz/{'d' * 40}"


def package_fixture(tmp_path):
    root = tmp_path / "omp"
    calls = []
    env = {"ASTRID_BUN_EXECUTABLE": "/qualified/bun", "PATH": "/qualified:/usr/bin:/bin"}

    def run(argv, **kwargs):
        calls.append(([str(v) for v in argv], kwargs))
        if "add" in argv:
            package = root / "install/global/node_modules/@owner/fixed"
            package.mkdir(parents=True)
            (package / "package.json").write_text(json.dumps({"name": "@owner/fixed", "version": "17.4.3", "bin": {"omp": "cli.js"}}))
            (package / "cli.js").write_text("native ACP CLI")
            (root / "install/global/bun.lock").write_text("integrity-checked closure")
            (root / "bin").mkdir()
            (root / "bin/omp").write_text("launcher")
        return "omp/17.4.3"

    return root, calls, run, env


@pytest.mark.parametrize("reported", ["17.4.3", "omp/17.4.3", "arnold/17.4.3"])
def test_exact_npm_source_uses_isolated_owned_global_prefix_and_records_lock(tmp_path, reported):
    root, calls, run, env = package_fixture(tmp_path)

    def branded(argv, **kwargs):
        run(argv, **kwargs)
        return reported

    tools._install_omp_package("@owner/fixed@17.4.3", root, branded, env)
    assert calls[0][0] == ["/qualified/bun", "add", "--global", "--exact", "--ignore-scripts", "@owner/fixed@17.4.3"]
    assert calls[1][0][-1] == "--version"
    assert all(row[1]["clean_bun_env"] is True and row[1]["cwd"] == root for row in calls)
    provenance = json.loads((root / "source-provenance.json").read_text())
    assert provenance["package"] == "@owner/fixed@17.4.3" and provenance["source"] == "npm"
    assert len(provenance["bun_lock_sha256"]) == 64
    assert env["ASTRID_INSTALL_OMP_VERSION"] == "17.4.3"


@pytest.mark.parametrize("change", ["name", "version", "cli", "lock", "reported_version"])
def test_npm_mismatch_or_missing_closure_never_records_success(tmp_path, change):
    root, _calls, run, env = package_fixture(tmp_path)

    def altered(argv, **kwargs):
        result = run(argv, **kwargs)
        if "add" in argv:
            package = root / "install/global/node_modules/@owner/fixed"
            if change in {"name", "version"}:
                metadata = json.loads((package / "package.json").read_text())
                metadata[change] = "wrong"
                (package / "package.json").write_text(json.dumps(metadata))
            elif change == "cli":
                (package / "cli.js").unlink()
            elif change == "lock":
                (root / "install/global/bun.lock").unlink()
        return "wrong" if change == "reported_version" else result

    with pytest.raises(ValueError, match="OMP"):
        tools._install_omp_package("@owner/fixed@17.4.3", root, altered, env)
    assert not (root / "source-provenance.json").exists()


@pytest.mark.parametrize("source", ["fork", "npm"])
def test_hostile_bun_settings_and_registration_paths_are_build_only(tmp_path, monkeypatch, source):
    outside = tmp_path / "unrelated-global"
    user_home = tmp_path / "user-home"
    user_home.mkdir()
    user_config = user_home / ".bunfig.toml"
    user_config.write_text('[install]\nglobalDir="outside-global"\nglobalBinDir="outside-bin"\n')
    before = (user_config.read_bytes(), user_config.stat().st_mtime_ns)
    monkeypatch.setenv("HOME", str(user_home))
    monkeypatch.setenv("BUN_INSTALL_GLOBAL_DIR", str(outside))
    monkeypatch.setenv("BUN_OPTIONS", "--preload=evil")
    monkeypatch.setenv("NODE_OPTIONS", "--require=evil")
    if source == "fork":
        recipe, _payloads, _requests, rows, run, env = fixture(tmp_path, monkeypatch)
        env.update({"BUN_OPTIONS": "--preload=evil", "BUN_CONFIG_NO_VERIFY": "1", "NODE_OPTIONS": "--require=evil"})
        tools._install_omp_source({"repo": "https://github.com/owner/fork", "sha": "c" * 40}, recipe,
                                  tmp_path / "omp", "linux-x64", run, env)
        builds = [row[1] for row in rows]
    else:
        root, rows, run, env = package_fixture(tmp_path)
        env.update({"BUN_OPTIONS": "--preload=evil", "BUN_CONFIG_NO_VERIFY": "1", "NODE_OPTIONS": "--require=evil"})
        tools._install_omp_package("@owner/fixed@17.4.3", root, run, env)
        builds = [row[1]["extra_env"] for row in rows]
    for build in builds:
        assert build["BUN_INSTALL_GLOBAL_DIR"] == str(tmp_path / "omp/install/global")
        assert build["BUN_INSTALL_BIN"] == str(tmp_path / "omp/bin")
        assert build["HOME"] == build["USERPROFILE"] == str(tmp_path / "omp/build-home")
        assert build["XDG_CONFIG_HOME"] == str(tmp_path / "omp/build-home/config")
        assert build["npm_config_registry"] == build["NPM_CONFIG_REGISTRY"] == "https://registry.npmjs.org"
        assert "BUN_OPTIONS" not in build and "BUN_CONFIG_NO_VERIFY" not in build and "NODE_OPTIONS" not in build
    assert "HOME" not in env and "BUN_INSTALL_GLOBAL_DIR" not in env
    assert env["NODE_OPTIONS"] == "--require=evil"
    assert not outside.exists()
    assert (user_config.read_bytes(), user_config.stat().st_mtime_ns) == before


@pytest.mark.parametrize("tamper", [False, True])
def test_recipe_refresh_emits_only_the_candidate_build_closure(tmp_path, monkeypatch, tamper):
    import base64
    import runpy
    import sys
    import urllib.request

    tags = ("linux-x64", "linux-arm64", "darwin-x64", "darwin-arm64", "win32-x64")
    payloads = {}
    for tag in tags:
        stream = io.BytesIO()
        filename = f"pi_natives.{tag}-baseline.node" if tag.endswith("x64") else f"pi_natives.{tag}.node"
        with tarfile.open(fileobj=stream, mode="w:gz") as bundle:
            member = tarfile.TarInfo("package/" + filename)
            member.size = len(tag)
            bundle.addfile(member, io.BytesIO(tag.encode()))
        data = stream.getvalue()
        package = "@oh-my-pi/pi-natives-" + tag
        url = f"https://example.invalid/{tag}.tgz"
        metadata = {"name": package, "version": "18.12.6", "dist": {
            "tarball": url, "integrity": "sha512-" + base64.b64encode(hashlib.sha512(data).digest()).decode()}}
        payloads[f"https://registry.npmjs.org/{package}/18.12.6"] = json.dumps(metadata).encode()
        payloads[url] = data + (b"tampered" if tamper else b"")

    def git(argv):
        if "rev-parse" in argv:
            return b"cccccccccccccccccccccccccccccccccccccccc\n"
        if argv[-1].endswith(":bun.lock"):
            return b"candidate lock\n"
        return b'{"version":"18.12.6"}'

    monkeypatch.setattr(subprocess, "check_output", git)
    monkeypatch.setattr(urllib.request, "urlopen", lambda url, **_kw: io.BytesIO(payloads[url]))
    output = tmp_path / "recipe"
    monkeypatch.setattr(sys, "argv", ["refresh_omp_recipe.py", "--producer-repo", str(tmp_path),
        "--candidate", "reviewed", "--version", "18.12.6", "--bun-version", "1.3.14",
        "--cache", str(tmp_path / "cache"), "--output", str(output)])
    script = Path(__file__).parents[1] / "scripts/refresh_omp_recipe.py"
    if tamper:
        with pytest.raises(ValueError, match="SRI mismatch"):
            runpy.run_path(str(script), run_name="__main__")
        assert not (output / "recipe.json").exists()
        return
    runpy.run_path(str(script), run_name="__main__")
    recipe = json.loads((output / "recipe.json").read_text())
    assert set(recipe) == {"schema_version", "version", "bun_version", "bun_lock_sha256", "native"}
    assert recipe["bun_lock_sha256"] == digest(b"candidate lock\n")
    assert recipe["version"] == "18.12.6" and recipe["bun_version"] == "1.3.14"
    assert len(recipe["native"]) == 5
    for asset in recipe["native"].values():
        assert asset["sha256"] == digest(payloads[asset["url"]])
        assert len(asset["files"]) == 1
    assert not (output / "source.patch").exists()
