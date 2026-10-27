"""Pinned user-local tool and source materialization. Never consult shell profiles."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tarfile
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath
from runtime_protocol import platform

from ..core.generation.vibecomfy_dependency import VIBECOMFY_FORK_URL
from ..version import ASTRID_VERSION

ENCODERS = {"libx264", "libx265", "libvpx-vp9", "libaom-av1", "libopus"}
FILTERS = {"ebur128", "silencedetect", "astats", "signalstats", "blackdetect",
           "loudnorm", "psnr", "ssim", "scale", "fps", "aresample"}
DEFAULT_OMP_SOURCE = "@oh-my-pi/pi-coding-agent@18.12.6"


def omp_source(pin: dict) -> str | dict:
    """One source selection: an exact npm release or a repository and commit."""
    if not isinstance(pin, dict) or set(pin) - {"source", "_comment"}:
        raise ValueError("OMP uses only tools.omp.source: exact npm name@version or {repo, sha}.")
    if "_comment" in pin and not isinstance(pin["_comment"], str):
        raise ValueError("OMP _comment must be a string.")
    selected = pin.get("source", DEFAULT_OMP_SOURCE)
    if isinstance(selected, str):
        if not re.fullmatch(r"(?:@[a-z0-9._-]+/)?[a-z0-9][a-z0-9._-]*@\d+\.\d+\.\d+", selected):
            raise ValueError("OMP npm source must be an exact package name@version.")
    elif isinstance(selected, dict) and set(selected) == {"repo", "sha"}:
        if not isinstance(selected["sha"], str) or not re.fullmatch(r"[0-9a-f]{40}", selected["sha"]):
            raise ValueError("OMP source must be pinned to a full git SHA.")
        omp_source_url(selected["repo"], selected["sha"])
    else:
        raise ValueError("OMP source must be an exact npm name@version or {repo, sha}.")
    return selected


def omp_source_url(repo: str, sha: str) -> str | None:
    """GitHub archives need no Git; explicit developer repositories stay local."""
    if isinstance(repo, str) and repo.startswith("https://"):
        match = re.fullmatch(r"https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?", repo)
        if not match or any(part in {".", ".."} for part in match.groups()):
            raise ValueError("OMP HTTPS source repo must be a GitHub owner/repository URL.")
        return f"https://codeload.github.com/{match[1]}/{match[2]}/tar.gz/{sha}"
    local_repository(repo)
    return None


def validate_manifest(value: dict) -> dict:
    if value.get("schema_version") != 1:
        raise ValueError("Unsupported install manifest.")
    release = value.get("release", 0)
    if type(release) is not int or release < 0:
        raise ValueError("The install manifest release must be a nonnegative integer.")
    sources = ("agent", "runtime") + (("vibecomfy",) if "vibecomfy" in value else ())
    for name in sources:
        pin = value[name]
        if (not isinstance(pin, dict) or not isinstance(pin.get("sha"), str)
                or not re.fullmatch(r"[0-9a-f]{40}", pin["sha"])):
            raise ValueError(f"The {name} source must be pinned to a full git SHA.")
        if "repo" in pin:
            local_repository(pin["repo"])
    for name in ("uv", "python", "bun", "node", "ffmpeg"):
        if not re.fullmatch(r"\d+\.\d+\.\d+", value["tools"][name]["version"]):
            raise ValueError(f"The {name} tool must have an exact version.")
        for asset in value["tools"][name].get("assets", {}).values():
            asset_downloads(asset)
    omp_source(value["tools"]["omp"])
    if value["tools"]["node"]["version"] != "20.19.4":
        raise ValueError("Remotion requires Node 20.19.4.")
    return value


def asset_downloads(asset: dict) -> list[tuple[str, str | None]]:
    """Pair each archive with its digest; retain legacy single-URL manifests."""
    if "urls" in asset:
        urls = asset["urls"]
        checksums = asset.get("sha256")
        if "url" in asset or not isinstance(urls, list) or not urls:
            raise ValueError("An installer asset needs either url or a nonempty urls list.")
        if not isinstance(checksums, list) or len(checksums) != len(urls):
            raise ValueError("Installer urls require an ordered sha256 list of equal length.")
    else:
        urls = [asset.get("url")]
        checksums = [asset.get("sha256")]
    for url, checksum in zip(urls, checksums):
        if not isinstance(url, str) or not url.startswith("https://"):
            raise ValueError("Installer asset downloads require HTTPS URLs.")
        # Omitted single-archive digests predate this manifest contract. Explicit
        # digests and every multi-archive digest must be a lowercase SHA256.
        if "sha256" in asset or "urls" in asset:
            if not isinstance(checksum, str) or not re.fullmatch(r"[0-9a-f]{64}", checksum):
                raise ValueError("Installer asset sha256 must be 64 lowercase hex characters.")
    return list(zip(urls, checksums))


def download(url: str, target: Path, *, sha256: str | None = None) -> None:
    if not url.startswith("https://"):
        raise ValueError("Installer downloads require HTTPS.")
    request = urllib.request.Request(url, headers={"User-Agent": f"astrid-installer/{ASTRID_VERSION}"})
    with urllib.request.urlopen(request, timeout=120) as response, target.open("wb") as output:
        shutil.copyfileobj(response, output)
    if sha256 and hashlib.sha256(target.read_bytes()).hexdigest() != sha256:
        raise ValueError(f"Download checksum mismatch: {url}")


def archive_path(name: str) -> Path:
    value = PurePosixPath(name)
    if value.is_absolute() or ".." in value.parts or "\\" in name or ":" in name:
        raise ValueError(f"Unsafe archive entry: {name}")
    return Path(*value.parts)


def extract(archive: Path, destination: Path, *, reject_links: bool = False) -> None:
    """Extract regular files/directories only, including on Python 3.11/Windows."""
    destination.mkdir(parents=True, exist_ok=True)
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as bundle:
            for zip_member in bundle.infolist():
                relative = archive_path(zip_member.filename)
                if stat.S_ISLNK(zip_member.external_attr >> 16):
                    raise ValueError(f"Archive contains a link: {zip_member.filename}")
                target = destination / relative
                if zip_member.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with bundle.open(zip_member) as source, target.open("wb") as output:
                        shutil.copyfileobj(source, output)
                    target.chmod(0o755 if zip_member.external_attr >> 16 & 0o111 else 0o644)
        return
    with tarfile.open(archive) as bundle:
        for member in bundle:
            target = destination / archive_path(member.name)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                target.parent.mkdir(parents=True, exist_ok=True)
                tar_source = bundle.extractfile(member)
                if tar_source is None:
                    raise ValueError(f"Archive regular entry cannot be read: {member.name}")
                with tar_source, target.open("wb") as output:
                    shutil.copyfileobj(tar_source, output)
                target.chmod(0o755 if member.mode & 0o111 else 0o644)
            elif member.issym() or member.islnk():
                if reject_links:
                    raise ValueError(f"Source archive contains a link: {member.name}")
                # Official Node archives contain npm/npx links. The installer
                # invokes npm-cli.js directly and needs neither link.
                continue
            else:
                raise ValueError(f"Archive contains a special entry: {member.name}")


def fetch_archive(url: str, destination: Path, *, sha256: str | None = None,
                  reject_links: bool = False) -> None:
    archive = destination.with_name(destination.name + ".download")
    archive.parent.mkdir(parents=True, exist_ok=True)
    try:
        download(url, archive, sha256=sha256)
        extract(archive, destination, reject_links=reject_links)
    finally:
        archive.unlink(missing_ok=True)


def local_repository(value: str) -> Path:
    """Resolve a developer source override without permitting remote Git access."""
    if not isinstance(value, str) or not value:
        raise ValueError("A source repo must be an absolute local path or file:// URL.")
    if value.startswith("file://"):
        url = urllib.parse.urlsplit(value)
        if url.netloc not in {"", "localhost"} or url.query or url.fragment:
            raise ValueError("A source repo file:// URL must refer to this computer.")
        value = urllib.request.url2pathname(url.path)
    repository = Path(value)
    if not repository.is_absolute():
        raise ValueError("A source repo must be an absolute local path or file:// URL.")
    return repository


def local_source(repo: str, sha: str, archive: Path) -> None:
    """Archive a committed Git tree, without consulting the checkout's worktree."""
    repository = local_repository(repo)
    # Inherited Git overrides must not redirect reads to some other repository.
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_NO_REPLACE_OBJECTS": "1"})

    def git(*args, **kwargs):
        try:
            result = subprocess.run(["git", "-C", str(repository), *args], env=env,
                                    stderr=subprocess.PIPE, check=False, **kwargs)
        except FileNotFoundError as error:
            raise ValueError("Local source overrides require Git on PATH.") from error
        if result.returncode:
            detail = result.stderr.decode("utf-8", errors="replace").strip()
            raise ValueError(f"Cannot read local source {repository} at {sha}: {detail}")
        return result

    if git("cat-file", "-t", sha, stdout=subprocess.PIPE).stdout.strip() != b"commit":
        raise ValueError("A local source SHA must identify a Git commit.")
    entries = git("ls-tree", "-r", "-z", sha, stdout=subprocess.PIPE).stdout
    for entry in entries.split(b"\0"):
        if entry and entry.split(b" ", 1)[0] not in {b"100644", b"100755"}:
            path = os.fsdecode(entry.split(b"\t", 1)[1])
            raise ValueError(f"Local source contains a link or submodule: {path}")
    with archive.open("wb") as output:
        git("archive", "--format=tar", sha, stdout=output)


def source(name: str, sha: str, destination: Path, *, repo: str | None = None) -> Path:
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError(f"The {name} source must be pinned to a full git SHA.")
    staging = destination.with_name(destination.name + ".unpack")
    archive = destination.with_name(destination.name + ".source.tar")
    try:
        if repo is not None:
            local_source(repo, sha, archive)
            extract(archive, staging)
            platform.replace_path(staging, destination)
        else:
            repository = (VIBECOMFY_FORK_URL.removesuffix(".git") if name == "vibecomfy"
                          else f"https://github.com/banodoco/astrid-{name}")
            fetch_archive(f"{repository}/archive/{sha}.tar.gz", staging)
            children = list(staging.iterdir())
            if len(children) != 1 or not children[0].is_dir():
                raise ValueError(f"Unexpected {name} source archive layout.")
            # Discard GitHub's long SHA directory name to keep Windows paths shallow.
            platform.replace_path(children[0], destination)
    finally:
        archive.unlink(missing_ok=True)
        if staging.exists():
            shutil.rmtree(staging)
    return destination


def binary(root: Path, name: str) -> Path:
    matches = sorted(p for p in root.rglob(name) if p.is_file())
    if len(matches) != 1:
        raise ValueError(f"Expected exactly one {name} in {root}, found {len(matches)}.")
    matches[0].chmod(0o755)
    return matches[0]


def probe_ffmpeg(encoders: str, filters: str) -> None:
    def names(output: str) -> set[str]:
        return {line.split()[1] for line in output.splitlines() if len(line.split()) >= 2}
    missing = sorted(ENCODERS - names(encoders)) + sorted(FILTERS - names(filters))
    if missing:
        raise ValueError("FFmpeg is missing required capabilities: " + ", ".join(missing))


def _media_tool_metadata(ffmpeg: Path, ffprobe: Path, run, origin: str) -> dict[str, str]:
    versions = {}
    for name, executable in (("ffmpeg", ffmpeg), ("ffprobe", ffprobe)):
        output = run([executable, "-version"])
        match = re.match(rf"{name} version (\S+)", output.strip())
        if not match:
            raise ValueError(f"Cannot identify the selected {name} version.")
        versions[name] = match[1]
    probe_ffmpeg(run([ffmpeg, "-hide_banner", "-encoders"]),
                 run([ffmpeg, "-hide_banner", "-filters"]))
    return {
        "ASTRID_FFMPEG_EXECUTABLE": str(ffmpeg),
        "ASTRID_FFPROBE_EXECUTABLE": str(ffprobe),
        "ASTRID_INSTALL_FFMPEG_SOURCE": origin,
        "ASTRID_INSTALL_FFMPEG_VERSION": versions["ffmpeg"],
        "ASTRID_INSTALL_FFPROBE_VERSION": versions["ffprobe"],
    }


def _media_tools(pin: dict, root: Path, target: str, run) -> dict[str, str]:
    selected = [Path(os.path.abspath(value)) for name in ("ffmpeg", "ffprobe")
                if (value := shutil.which(name))]
    versions = root.parent.parent
    # Managed versions are pruned after updates; they cannot supply a future
    # version's media executables, including through an external symlink.
    managed = versions.name == "versions" and any(
        path.is_relative_to(versions) or path.resolve().is_relative_to(versions) for path in selected)
    if len(selected) == 2 and not managed:
        try:
            return _media_tool_metadata(selected[0], selected[1], run, "path")
        except (OSError, ValueError, subprocess.SubprocessError):
            # An absent/broken capability falls back to the qualified release.
            pass
    asset = pin["assets"].get(target)
    if not asset:
        raise ValueError(f"There is no qualified ffmpeg build for {target}.")
    folder = root / "ffmpeg"
    for url, checksum in asset_downloads(asset):
        fetch_archive(url, folder, sha256=checksum)
    suffix = ".exe" if target.startswith("windows-") else ""
    return _media_tool_metadata(binary(folder, "ffmpeg" + suffix), binary(folder, "ffprobe" + suffix), run, "download")


def source_tree_sha256(root: Path) -> str:
    """Pin extracted source independently of archive compression or top prefix.

    Each sorted POSIX relative UTF-8 path is length-prefixed with eight big-endian
    bytes, followed by its file's raw SHA256 digest. Links/special files refuse.
    """
    digest = hashlib.sha256(b"astrid-omp-source-tree-v1\0")
    for path in sorted(root.rglob("*"), key=lambda p: p.relative_to(root).as_posix()):
        if path.is_symlink() or (not path.is_dir() and not path.is_file()):
            raise ValueError(f"OMP source contains a nonregular entry: {path}")
        if path.is_file():
            relative = path.relative_to(root).as_posix().encode("utf-8")
            digest.update(len(relative).to_bytes(8, "big"))
            digest.update(relative)
            digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def load_omp_recipe() -> dict:
    folder = Path(__file__).with_name("omp")
    recipe = json.loads((folder / "recipe.json").read_text(encoding="utf-8"))
    if recipe["schema_version"] != 1 or recipe["version"] != "18.12.6":
        raise ValueError("Unsupported bundled OMP source recipe.")
    for asset in recipe["native"].values():
        if "sha256" not in asset:
            raise ValueError("OMP native archives require SHA256.")
        asset_downloads(asset)
    return recipe


def _check_omp_lock(source_root: Path, expected: str) -> None:
    if hashlib.sha256((source_root / "bun.lock").read_bytes()).hexdigest() != expected:
        raise ValueError("OMP frozen Bun lock checksum mismatch.")


def _install_omp_source(selected: dict, recipe: dict, root: Path, target: str, run, env: dict) -> None:
    native = recipe["native"].get(target)
    if native is None:
        raise ValueError(f"There is no pinned OMP native build for {target}.")
    root.mkdir(parents=True, exist_ok=True)
    container = root / "source-archive"
    source_url = omp_source_url(selected["repo"], selected["sha"])
    if source_url:
        fetch_archive(source_url, container, reject_links=True)
        children = list(container.iterdir())
        if len(children) != 1 or not children[0].is_dir():
            raise ValueError("Unexpected OMP source directory layout.")
        source_root = children[0]
    else:
        archive = root / "source.download"
        try:
            local_source(selected["repo"], selected["sha"], archive)
            extract(archive, container)
        finally:
            archive.unlink(missing_ok=True)
        source_root = container
    # The selected commit is authoritative. Never apply a downstream producer
    # patch here: owners land the separately delivered fix before selecting it.
    normalized = root / "source"
    platform.replace_path(source_root, normalized)
    if container.exists():
        shutil.rmtree(container)
    source_root = normalized
    tree_sha256 = source_tree_sha256(source_root)
    _check_omp_lock(source_root, recipe["bun_lock_sha256"])
    package = json.loads((source_root / "packages/coding-agent/package.json").read_text(encoding="utf-8"))
    if package.get("version") != recipe["version"]:
        raise ValueError("OMP source version is incompatible with the pinned native build recipe.")
    bun = env["ASTRID_BUN_EXECUTABLE"]
    build_env = _omp_build_env(root, env)
    run([bun, "install", "--frozen-lockfile", "--ignore-scripts", "--omit", "optional"],
        extra_env=build_env, cwd=source_root, clean_bun_env=True)
    _check_omp_lock(source_root, recipe["bun_lock_sha256"])
    run([bun, "run", "gen:tool-views"], extra_env=build_env, cwd=source_root, clean_bun_env=True)
    _check_omp_lock(source_root, recipe["bun_lock_sha256"])
    native_archive = root / "native-archive"
    fetch_archive(native["url"], native_archive, sha256=native["sha256"])
    destination = source_root / "packages/natives/native"
    destination.mkdir(parents=True, exist_ok=True)
    try:
        for name in native["files"]:
            relative = archive_path(name)
            native_file = native_archive / "package" / relative
            if not native_file.is_file() or relative.name != name or not name.endswith(".node"):
                raise ValueError(f"The pinned OMP native archive is missing {name}.")
            platform.replace_path(native_file, destination / name)
    finally:
        shutil.rmtree(native_archive)
    # Import the producer's CPU-aware loader, then enforce the exact ABI marker
    # even in workspace mode, where the producer normally omits that guard.
    probe = ("import {pathToFileURL} from 'node:url';"
             "const b=await import(pathToFileURL(process.argv[1]).href);"
             "if(b.__piNativesBuildVersion?.()!=='18.12.6')throw Error('OMP native ABI mismatch');")
    run([bun, "-e", probe, destination / "index.js"], extra_env=build_env, cwd=source_root,
        clean_bun_env=True)
    run([bun, "link", "--frozen-lockfile", "--ignore-scripts", "--no-save"],
        extra_env=build_env, cwd=source_root / "packages/coding-agent", clean_bun_env=True)
    _check_omp_lock(source_root, recipe["bun_lock_sha256"])
    _write_omp_provenance(root, {
        "source": "fork", "version": recipe["version"], "repo": selected["repo"],
        "commit": selected["sha"], "source_url": source_url, "tree_sha256": tree_sha256,
        "bun_lock_sha256": recipe["bun_lock_sha256"],
        "native_url": native["url"], "native_sha256": native["sha256"],
        "native_integrity": native.get("integrity"), "source_mode": "https" if source_url else "local-repo",
    })
    env.update({"ASTRID_INSTALL_OMP_SOURCE": "fork", "ASTRID_INSTALL_OMP_VERSION": recipe["version"],
                "ASTRID_INSTALL_OMP_COMMIT": selected["sha"]})


def _omp_build_env(root: Path, env: dict) -> dict:
    # Ignore user Bun policy/preloads and global config for these build commands.
    # Keep only the deliberately selected download cache; never inherit lock or
    # integrity bypasses, global-store links, or registration paths. The private
    # build home/config does not change the user's files or the service env.
    build_home = root / "build-home"
    build_config = build_home / "config"
    build_config.mkdir(parents=True)
    build_env = {key: value for key, value in env.items()
                 if not key.startswith("BUN_") and key != "NODE_OPTIONS"}
    build_env.update({"BUN_INSTALL": str(root), "BUN_INSTALL_BIN": str(root / "bin"),
                      "BUN_INSTALL_GLOBAL_DIR": str(root / "install/global"),
                      "HOME": str(build_home), "USERPROFILE": str(build_home),
                      "XDG_CONFIG_HOME": str(build_config),
                      "npm_config_registry": "https://registry.npmjs.org",
                      "NPM_CONFIG_REGISTRY": "https://registry.npmjs.org"})
    cache = env.get("BUN_INSTALL_CACHE_DIR", os.environ.get("BUN_INSTALL_CACHE_DIR"))
    if cache:
        build_env["BUN_INSTALL_CACHE_DIR"] = cache
    return build_env


def _write_omp_provenance(root: Path, value: dict) -> None:
    (root / "source-provenance.json").write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def _install_omp_package(selected: str, root: Path, run, env: dict) -> None:
    build_env = _omp_build_env(root, env)
    bun = env["ASTRID_BUN_EXECUTABLE"]
    run([bun, "add", "--global", "--exact", "--ignore-scripts", selected],
        extra_env=build_env, cwd=root, clean_bun_env=True)
    name, version = selected.rsplit("@", 1)
    global_root = root / "install/global"
    package_root = global_root / "node_modules" / name
    package = json.loads((package_root / "package.json").read_text(encoding="utf-8"))
    if package.get("name") != name or package.get("version") != version:
        raise ValueError("The installed OMP npm package does not match its exact source pin.")
    cli = archive_path(package.get("bin", {}).get("omp", ""))
    if not cli.parts or not (package_root / cli).is_file():
        raise ValueError("The installed OMP npm package has no omp launcher.")
    reported = run([bun, package_root / cli, "--version"], extra_env=build_env, cwd=root,
                   clean_bun_env=True).strip()
    if not re.fullmatch(r"(?:[A-Za-z0-9._-]+/)?" + re.escape(version), reported):
        raise ValueError("The installed OMP CLI does not match its exact source pin.")
    lock = global_root / "bun.lock"
    if not lock.is_file():
        raise ValueError("The OMP npm install did not record its dependency lock.")
    _write_omp_provenance(root, {"source": "npm", "package": selected, "version": version,
        "bun_lock_sha256": hashlib.sha256(lock.read_bytes()).hexdigest(),
        "package_json_sha256": hashlib.sha256((package_root / "package.json").read_bytes()).hexdigest(),
        "native_files": {path.relative_to(global_root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                         for path in sorted(global_root.rglob("*.node")) if path.is_file()}})
    env.update({"ASTRID_INSTALL_OMP_SOURCE": "npm", "ASTRID_INSTALL_OMP_VERSION": version,
                "ASTRID_INSTALL_OMP_PACKAGE": selected})


def install_tools(manifest: dict, root: Path, target: str, run) -> dict[str, str]:
    pins = manifest["tools"]
    selected = omp_source(pins["omp"])
    if isinstance(selected, dict) and pins["bun"]["version"] != load_omp_recipe()["bun_version"]:
        raise ValueError("OMP source staging requires the bundled recipe's Bun version.")
    windows = target.startswith("windows-")
    suffix = ".exe" if windows else ""
    # Keep UTF-8 stdio in service environments.
    env = {"PYTHONIOENCODING": "utf-8"}
    paths = []
    for name in ("bun", "node"):
        pin = pins[name]
        asset = pin["assets"].get(target)
        if not asset:
            raise ValueError(f"There is no qualified {name} build for {target}.")
        folder = root / name
        for url, checksum in asset_downloads(asset):
            fetch_archive(url, folder, sha256=checksum)
        executable = binary(folder, name + suffix)
        paths.append(str(executable.parent))
        if name == "node":
            version = run([executable, "--version"]).strip()
            if version != "v" + pin["version"]:
                raise ValueError(f"The installed Node does not match {pin['version']}: {version}")
            env["ASTRID_NODE_EXECUTABLE"] = str(executable)
        else:
            if run([executable, "--version"]).strip() != pin["version"]:
                raise ValueError("The installed Bun does not match its pin.")
            env["ASTRID_BUN_EXECUTABLE"] = str(executable)
    env.update(_media_tools(pins["ffmpeg"], root, target, run))
    for name in ("ASTRID_FFMPEG_EXECUTABLE", "ASTRID_FFPROBE_EXECUTABLE"):
        directory = str(Path(env[name]).parent)
        if directory not in paths:
            paths.append(directory)
    # Deliberately construct the service PATH. No user profile or refreshed PATH.
    system_path = (str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32")
                   if windows else "/usr/bin:/bin:/usr/sbin:/sbin")
    omp = root / "omp"
    omp_bin = omp / "bin"
    omp_bin.mkdir(parents=True)
    env.update({"BUN_INSTALL": str(omp), "BUN_INSTALL_BIN": str(omp_bin),
                "PATH": (";" if windows else ":").join([str(omp_bin), *paths, system_path])})
    if isinstance(selected, str):
        _install_omp_package(selected, omp, run, env)
    else:
        _install_omp_source(selected, load_omp_recipe(), omp, target, run, env)
    launchers = [omp_bin / ("omp" + extension) for extension in (suffix, ".cmd")
                 if (omp_bin / ("omp" + extension)).is_file()]
    if not launchers:
        raise ValueError("OMP did not create its user-local launcher.")
    if any(not launcher.resolve().is_relative_to(omp.resolve()) for launcher in launchers):
        raise ValueError("The OMP launcher escapes its owned installation prefix.")
    return env
