"""Build and exercise one isolated, installed Astrid wheel.

The m8 gate uses the installed artifact as its trust boundary.  This module
keeps that boundary small and reusable: a build is made once in a temporary
source snapshot, exactly one wheel is selected and hashed, the wheel is
installed without editable/source access into a private virtual environment,
and every lane receives the same isolated roots and artifact identity.

The module intentionally uses only the Python standard library.  It is used by
the packaging tests before Astrid's optional runtime dependencies are
available, and by later lanes which may choose to install those dependencies
explicitly in the private environment.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from email.parser import Parser
from pathlib import Path
from typing import Any, Mapping, Sequence

from astrid.core.foundation.paths import venv_python
from astrid.core.util.time import utc_now_iso

SCHEMA = "astrid.installed_artifact.v1"
DEFAULT_DISTRIBUTION = "astrid"
_TIMELINE_SCHEMA_ENV = "ASTRID_TIMELINE_SCHEMA_PYTHONPATH"
BUILD_LOCK = Path("requirements/build.lock")
RUNTIME_LOCK = Path("requirements/runtime.lock")
PROOF_LOCK = Path("requirements/proof.lock")


class InstalledArtifactError(RuntimeError):
    """Base error for build, installation, and isolation failures."""


class WheelSelectionError(InstalledArtifactError):
    """Raised when a release workspace does not contain exactly one wheel."""


class ArtifactIdentityError(InstalledArtifactError):
    """Raised when an installed process cannot prove its artifact identity."""


class LaneExecutionError(InstalledArtifactError):
    """Raised by a checked lane after retaining its failing lane record."""

    def __init__(self, message: str, record: LaneRecord) -> None:
        super().__init__(message)
        self.record = record


@dataclass(frozen=True, slots=True)
class LockedEnvironment:
    """A disposable interpreter provisioned exclusively from a hashed lock."""

    workspace: Path
    venv_dir: Path
    python_executable: Path
    lock_path: Path
    owned_workspace: bool

    def environment(self) -> dict[str, str]:
        """Return the dependency-only child environment for proof probes."""
        bin_dir = self.venv_dir / ("Scripts" if os.name == "nt" else "bin")
        home = self.workspace / "home"
        temp = self.workspace / "tmp"
        home.mkdir(parents=True, exist_ok=True)
        temp.mkdir(parents=True, exist_ok=True)
        return {
            "PATH": os.pathsep.join((str(bin_dir), os.defpath)),
            "HOME": str(home),
            "TMPDIR": str(temp),
            "TMP": str(temp),
            "TEMP": str(temp),
            "PYTHONNOUSERSITE": "1",
            "PYTHONSAFEPATH": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "VIRTUAL_ENV": str(self.venv_dir),
        }

    def close(self) -> None:
        """Remove only an auto-created provisioning workspace."""
        if self.owned_workspace:
            shutil.rmtree(self.workspace, ignore_errors=True)

    def __enter__(self) -> LockedEnvironment:
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _tb: Any) -> None:
        self.close()


@dataclass(frozen=True, slots=True)
class IsolatedRoots:
    """Writable roots supplied to an installed lane."""

    root: Path
    home: Path
    state: Path
    project: Path
    media: Path
    cache: Path
    config: Path
    browser: Path

    @classmethod
    def create(cls, root: str | Path) -> IsolatedRoots:
        base = Path(root).expanduser().resolve()
        names = {
            "home": "home",
            "state": "state",
            "project": "project",
            "media": "media",
            "cache": "cache",
            "config": "config",
            "browser": "browser-profile",
        }
        paths = {key: base / value for key, value in names.items()}
        for path in paths.values():
            path.mkdir(parents=True, exist_ok=True)
        return cls(root=base, **paths)

    def as_dict(self) -> dict[str, str]:
        return {
            "root": str(self.root),
            "home": str(self.home),
            "state": str(self.state),
            "project": str(self.project),
            "media": str(self.media),
            "cache": str(self.cache),
            "config": str(self.config),
            "browser": str(self.browser),
        }


@dataclass(frozen=True, slots=True)
class WheelArtifact:
    """Identity of the one wheel selected by the harness."""

    path: Path
    sha256: str
    distribution: str
    version: str

    def as_dict(self) -> dict[str, str]:
        return {
            "path": str(self.path),
            "sha256": self.sha256,
            "distribution": self.distribution,
            "version": self.version,
        }


@dataclass(frozen=True, slots=True)
class InstalledIdentity:
    """Identity and import-boundary evidence from a venv child process."""

    executable: str
    import_path: str
    version: str
    wheel_sha256: str
    prefix: str
    base_prefix: str
    sys_path: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "executable": self.executable,
            "import_path": self.import_path,
            "version": self.version,
            "wheel_sha256": self.wheel_sha256,
            "prefix": self.prefix,
            "base_prefix": self.base_prefix,
            "sys_path": list(self.sys_path),
        }


@dataclass(frozen=True, slots=True)
class LaneRecord:
    """Serializable evidence retained for one installed-artifact command."""

    lane: str
    command: tuple[str, ...]
    executable: str
    import_path: str | None
    version: str | None
    wheel_sha256: str
    status: str
    returncode: int | None
    started_at: str
    finished_at: str
    duration_seconds: float
    stdout: str
    stderr: str
    output: str
    roots: Mapping[str, str]
    error: str | None = None

    @property
    def passed(self) -> bool:
        return self.status == "passed"

    def as_dict(self) -> dict[str, Any]:
        """Return the stable JSON evidence shape used by later m8 lanes."""
        return {
            "schema": SCHEMA,
            "lane": self.lane,
            "command": list(self.command),
            "executable": self.executable,
            "python_executable": self.executable,
            "import_path": self.import_path,
            "installed_version": self.version,
            "version": self.version,
            "wheel_sha256": self.wheel_sha256,
            "digest": self.wheel_sha256,
            "status": self.status,
            "returncode": self.returncode,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_seconds": self.duration_seconds,
            "timestamps": {
                "started_at": self.started_at,
                "finished_at": self.finished_at,
            },
            "stdout": self.stdout,
            "stderr": self.stderr,
            "output": self.output,
            "roots": dict(self.roots),
            "error": self.error,
        }


_SECRET_NAME_RE = re.compile(
    r"(^|_)(API[_-]?KEY|ACCESS[_-]?KEY|AUTH|CREDENTIAL|PASSWORD|SECRET|TOKEN)($|_)",
    re.IGNORECASE,
)
_SECRET_PREFIXES = (
    "AWS_",
    "AZURE_",
    "ANTHROPIC_",
    "FAL_",
    "GCP_",
    "GEMINI_",
    "GOOGLE_",
    "HF_",
    "HUGGINGFACE_",
    "OPENAI_",
    "ASTRID_APP_",
    "RUNPOD_",
)
_REMOVED_ENV_NAMES = {
    "ASTRID_ACTOR",
    "ASTRID_AUTHOR_TEST",
    "ASTRID_GATEWAY_RESOLVED_PROJECT",
    "ASTRID_INTERNAL_INVOCATION",
    "ASTRID_PACKS_PATH",
    "ASTRID_PROJECT_RUN",
    "ASTRID_PROJECT_SLUG",
    "ASTRID_REPO_ROOT",
    "ASTRID_TASK_ITEM_ID",
    "ASTRID_TASK_ITERATION",
    "ASTRID_TASK_PROJECT",
    "ASTRID_TASK_RUN_ID",
    "ASTRID_TASK_STEP_ID",
    "ASTRID...TEST",
    "ASTRID_AUTHOR_TEST_LEGACY",
    "ASTRID_THEMES_ROOT",
    "DATABASE_URL",
    "PIP_INDEX_URL",
    "PIP_EXTRA_INDEX_URL",
    "PIP_TRUSTED_HOST",
}


# Provisioning (and only provisioning) resolves packages over the network, so
# pip needs the caller's proxy and CA settings (a corporate or sandbox proxy
# re-terminates TLS).  Lanes never see these: ``scrub_environment`` is separate.
_PIP_NETWORK_ENV_NAMES = (
    "CURL_CA_BUNDLE",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "PIP_CERT",
    "REQUESTS_CA_BUNDLE",
    "SSL_CERT_FILE",
)


def _pip_network_environment(parent: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return the proxy/CA variables pip needs to reach its index."""
    source = os.environ if parent is None else parent
    found: dict[str, str] = {}
    for name in _PIP_NETWORK_ENV_NAMES:
        for candidate in (name, name.lower()):
            if source.get(candidate):
                found[candidate] = str(source[candidate])
    return found


def is_secret_env_name(name: str) -> bool:
    """Return whether an environment name may carry credentials/configuration."""
    upper = name.upper()
    return (
        upper in _REMOVED_ENV_NAMES
        or upper.startswith(_SECRET_PREFIXES)
        or bool(_SECRET_NAME_RE.search(upper))
    )


def scrub_environment(
    *,
    roots: IsolatedRoots,
    venv_dir: str | Path,
    parent: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build a minimal credential-free environment for a lane.

    Only ordinary process-locale/path variables are inherited.  Python path
    injection, user-site packages, Astrid session identity, cloud/provider
    configuration, and secret-like names are removed before isolated roots are
    applied.
    """
    source = os.environ if parent is None else parent
    safe = {
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "LOGNAME",
        "PATH",
        "SHELL",
        "SYSTEMROOT",
        "USER",
        "USERNAME",
    }
    env = {
        key: str(value)
        for key, value in source.items()
        if key in safe and not is_secret_env_name(key)
    }
    # The canonical timeline schema is an explicitly configured, optional
    # external dependency.  Preserve only a validated absolute install root;
    # never pass an arbitrary PYTHONPATH or an invalid value into the
    # installed-artifact lane.  This keeps the lane hermetic while allowing a
    # release worker that deliberately provisioned the schema to exercise the
    # schema-bearing kernel tests.
    schema_root_raw = str(source.get(_TIMELINE_SCHEMA_ENV, "")).strip()
    if schema_root_raw:
        schema_root = Path(schema_root_raw).expanduser()
        package_root = schema_root / "banodoco_timeline_schema"
        if (
            schema_root.is_absolute()
            and package_root.is_dir()
            and (package_root / "timeline.schema.json").is_file()
        ):
            env[_TIMELINE_SCHEMA_ENV] = str(schema_root.resolve())
    venv_path = Path(venv_dir).expanduser().resolve()
    bin_dir = venv_path / ("Scripts" if os.name == "nt" else "bin")
    existing_path = env.get("PATH", os.defpath)
    env["PATH"] = os.pathsep.join((str(bin_dir), existing_path))

    env.update(
        {
            "HOME": str(roots.home),
            "USERPROFILE": str(roots.home),
            "ASTRID_HOME": str(roots.state),
            "ASTRID_STATE_HOME": str(roots.state / "state"),
            "ASTRID_PROJECTS_ROOT": str(roots.project),
            # The product derives managed media beneath the project root; this
            # additional explicit root gives later lanes a stable media target
            # for commands that accept one directly.
            "ASTRID_MEDIA_ROOT": str(roots.media),
            "ASTRID_CACHE_ROOT": str(roots.cache),
            "ASTRID_CONFIG_ROOT": str(roots.config),
            "ASTRID_WORKSPACE_CONFIG_DIR": str(roots.config / "workspace"),
            "ASTRID_BROWSER_PROFILE": str(roots.browser),
            "XDG_CACHE_HOME": str(roots.cache),
            "XDG_CONFIG_HOME": str(roots.config),
            "XDG_STATE_HOME": str(roots.state),
            "TMPDIR": str(roots.root / "tmp"),
            "TMP": str(roots.root / "tmp"),
            "TEMP": str(roots.root / "tmp"),
            "PYTHONNOUSERSITE": "1",
            "PYTHONSAFEPATH": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "ASTRID_NO_NUDGE": "1",
            "VIRTUAL_ENV": str(venv_path),
        }
    )
    Path(env["TMPDIR"]).mkdir(parents=True, exist_ok=True)
    Path(env["ASTRID_STATE_HOME"]).mkdir(parents=True, exist_ok=True)
    Path(env["ASTRID_WORKSPACE_CONFIG_DIR"]).mkdir(parents=True, exist_ok=True)
    for key in tuple(env):
        if is_secret_env_name(key) or key.startswith("PYTHON") and key not in {
            "PYTHONNOUSERSITE",
            "PYTHONSAFEPATH",
            "PYTHONDONTWRITEBYTECODE",
        }:
            env.pop(key, None)
    return env


def sha256_file(path: str | Path) -> str:
    """Hash a file without loading the whole wheel into memory."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def select_single_wheel(dist_dir: str | Path) -> Path:
    """Select exactly one wheel from ``dist_dir`` or fail closed."""
    directory = Path(dist_dir).expanduser().resolve()
    wheels = sorted(path for path in directory.glob("*.whl") if path.is_file())
    if len(wheels) != 1:
        names = ", ".join(path.name for path in wheels) or "none"
        raise WheelSelectionError(
            f"expected exactly one wheel in {directory}, found {len(wheels)}: {names}"
        )
    return wheels[0]


def _wheel_metadata(wheel: Path) -> tuple[str, str]:
    with zipfile.ZipFile(wheel) as archive:
        metadata_paths = sorted(
            name
            for name in archive.namelist()
            if name.endswith(".dist-info/METADATA") and "/" in name
        )
        if len(metadata_paths) != 1:
            raise WheelSelectionError(
                f"wheel {wheel.name} must contain exactly one dist-info/METADATA"
            )
        metadata = Parser().parsestr(archive.read(metadata_paths[0]).decode("utf-8"))
        distribution = (metadata.get("Name") or "").strip()
        version = (metadata.get("Version") or "").strip()
        if not distribution or not version:
            raise WheelSelectionError(
                f"wheel {wheel.name} is missing Name or Version metadata"
            )
        return distribution, version


def inspect_wheel(wheel: str | Path) -> WheelArtifact:
    """Return the immutable distribution/version/digest identity of a wheel."""
    path = Path(wheel).expanduser().resolve()
    if not path.is_file() or path.suffix != ".whl":
        raise WheelSelectionError(f"wheel does not exist: {path}")
    distribution, version = _wheel_metadata(path)
    return WheelArtifact(
        path=path,
        sha256=sha256_file(path),
        distribution=distribution,
        version=version,
    )


def _relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _copy_ignore(directory: str, names: list[str]) -> set[str]:
    """Ignore source-control and generated material in the build snapshot."""
    ignored = {
        ".git",
        ".megaplan",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "__pycache__",
        "build",
        "dist",
        "out",
        "runs",
        "projects",
        "node_modules",
        ".venv",
        "venv",
        "astrid.egg-info",
    }
    return {name for name in names if name in ignored or name.endswith(".egg-info")}


_IDENTITY_PROBE = r"""
import astrid
import importlib.metadata
import json
import sys
import sysconfig

print(json.dumps({
    "executable": sys.executable,
    "import_path": str(astrid.__file__ or ""),
    "version": importlib.metadata.version("astrid"),
    "prefix": sys.prefix,
    "base_prefix": sys.base_prefix,
    "purelib": sysconfig.get_path("purelib"),
    "sys_path": sys.path,
}))
"""


def _json_from_output(output: str) -> dict[str, Any]:
    for line in reversed(output.splitlines()):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise ArtifactIdentityError("installed identity probe did not emit a JSON object")


def _failure_detail(error: str | None, stderr: str, *, lines: int = 40) -> str:
    """Name the failure and carry the tail of the lane's stderr, so a failed check is diagnosable."""
    tail = "\n".join(stderr.strip().splitlines()[-lines:])
    return f"{error}\n--- lane stderr (last {lines} lines) ---\n{tail}" if tail else str(error)


def _redact_output(text: str) -> str:
    # The harness never passes secrets to a child, but redacting assignment-like
    # output keeps a lane record safe if a caller's command prints one anyway.
    return re.sub(
        r"(?i)(api[_-]?key|token|secret|password|credential)\s*[=:]\s*[^\s,;]+",
        r"\1=<redacted>",
        text,
    )


def _record_hashes(text: str) -> dict[str, str]:
    """Map RECORD path -> ``algo=digest`` for entries that carry a hash."""
    hashes: dict[str, str] = {}
    for row in csv.reader(text.splitlines()):
        if len(row) >= 2 and row[1]:
            hashes[row[0]] = row[1]
    return hashes


def _require_installed_matches_wheel(wheel: Path, import_path: Path) -> None:
    """Fail unless the installed copy carries every hashed file of ``wheel``."""
    with zipfile.ZipFile(wheel) as archive:
        record_name = next(
            (n for n in archive.namelist() if n.endswith(".dist-info/RECORD") and "/" in n),
            None,
        )
        if record_name is None:
            raise ArtifactIdentityError(f"wheel {wheel.name} has no dist-info/RECORD")
        expected = _record_hashes(archive.read(record_name).decode("utf-8"))
    site_packages = import_path.parent.parent
    installed_record = site_packages / record_name
    if not installed_record.is_file():
        raise ArtifactIdentityError(f"installed RECORD is missing: {installed_record}")
    installed = _record_hashes(installed_record.read_text(encoding="utf-8"))
    expected.pop(record_name, None)
    wrong = sorted(path for path, digest in expected.items() if installed.get(path) != digest)
    if not expected or wrong:
        raise ArtifactIdentityError(
            f"installed Astrid is not the supplied wheel {wheel.name}: "
            f"{len(wrong)} of {len(expected)} hashed files differ or are missing "
            f"(first: {wrong[:3]})"
        )


class InstalledArtifactHarness:
    """One build-once wheel installation and its installed lane runner."""

    def __init__(
        self,
        *,
        repo_root: Path,
        workspace: Path,
        owned_workspace: bool,
        source_snapshot: Path,
        dist_dir: Path,
        venv_dir: Path,
        artifact: WheelArtifact,
        roots: IsolatedRoots,
        python_executable: Path,
    ) -> None:
        self.repo_root = repo_root
        self.workspace = workspace
        self.source_snapshot = source_snapshot
        self.dist_dir = dist_dir
        self.venv_dir = venv_dir
        self.artifact = artifact
        self.roots = roots
        self.python_executable = python_executable
        self._owned_workspace = owned_workspace
        self.identity: InstalledIdentity | None = None
        self.last_record: LaneRecord | None = None

    @classmethod
    def build(
        cls,
        repo_root: str | Path,
        *,
        workspace: str | Path | None = None,
        python_executable: str | Path | None = None,
        install_dependencies: bool = False,
        dependency_lock: str | Path = RUNTIME_LOCK,
    ) -> InstalledArtifactHarness:
        """Build, install, and identity-check one wheel.

        ``install_dependencies`` is opt-in.  The default is deliberately
        ``pip --no-deps`` so the basic artifact proof cannot accidentally pass
        because it borrowed packages from the host interpreter.  A later lane
        may explicitly install the declared dependencies into this same venv.
        """
        root = Path(repo_root).expanduser().resolve()
        if not (root / "pyproject.toml").is_file():
            raise InstalledArtifactError(f"repository root is missing pyproject.toml: {root}")
        owned = workspace is None
        if workspace is None:
            workspace_path = Path(tempfile.mkdtemp(prefix="astrid-installed-artifact-")).resolve()
        else:
            workspace_path = Path(workspace).expanduser().resolve()
            if _relative_to(workspace_path, root):
                raise InstalledArtifactError(
                    f"isolated workspace must be outside the checkout: {workspace_path}"
                )
            workspace_path.mkdir(parents=True, exist_ok=True)

        source_snapshot = workspace_path / "source"
        dist_dir = workspace_path / "dist"
        build_venv_dir = workspace_path / "build-venv"
        venv_dir = workspace_path / "venv"
        try:
            shutil.copytree(root, source_snapshot, ignore=_copy_ignore)
            dist_dir.mkdir(parents=True, exist_ok=True)
            build_lock = source_snapshot / BUILD_LOCK
            runtime_lock = source_snapshot / RUNTIME_LOCK
            dependency_lock_path = source_snapshot / dependency_lock
            for lock in (build_lock, runtime_lock):
                if not lock.is_file():
                    raise InstalledArtifactError(f"required hashed lock is missing: {lock}")
            if not dependency_lock_path.is_file():
                raise InstalledArtifactError(
                    f"requested dependency lock is missing: {dependency_lock_path}"
                )

            build_venv_result = subprocess.run(
                [
                    str(python_executable or sys.executable),
                    "-m",
                    "venv",
                    str(build_venv_dir),
                ],
                cwd=workspace_path,
                capture_output=True,
                text=True,
                check=False,
            )
            if build_venv_result.returncode != 0:
                raise InstalledArtifactError(
                    "isolated build venv creation failed:\n"
                    + _redact_output(build_venv_result.stdout + build_venv_result.stderr)
                )
            build_python = venv_python(build_venv_dir)
            build_install = subprocess.run(
                [
                    str(build_python),
                    "-m",
                    "pip",
                    "install",
                    "--disable-pip-version-check",
                    "--no-input",
                    "--require-hashes",
                    "-r",
                    str(build_lock),
                ],
                cwd=workspace_path,
                capture_output=True,
                text=True,
                check=False,
            )
            if build_install.returncode != 0:
                raise InstalledArtifactError(
                    "hashed build-tool installation failed:\n"
                    + _redact_output(build_install.stdout + build_install.stderr)
                )
            build_command = [
                str(build_python),
                "-m",
                "build",
                "--wheel",
                "--no-isolation",
                "--outdir",
                str(dist_dir),
            ]
            build_result = subprocess.run(
                build_command,
                cwd=source_snapshot,
                capture_output=True,
                text=True,
                check=False,
            )
            if build_result.returncode != 0:
                raise InstalledArtifactError(
                    "wheel build failed:\n"
                    + _redact_output(build_result.stdout + build_result.stderr)
                )
            wheel = select_single_wheel(dist_dir)
            artifact = inspect_wheel(wheel)
            if artifact.distribution.lower().replace("-", "_") != DEFAULT_DISTRIBUTION:
                raise InstalledArtifactError(
                    f"expected {DEFAULT_DISTRIBUTION!r} wheel, found {artifact.distribution!r}"
                )

            venv_command = [str(python_executable or sys.executable), "-m", "venv", str(venv_dir)]
            venv_result = subprocess.run(
                venv_command,
                cwd=workspace_path,
                capture_output=True,
                text=True,
                check=False,
            )
            if venv_result.returncode != 0:
                raise InstalledArtifactError(
                    "isolated venv creation failed:\n"
                    + _redact_output(venv_result.stdout + venv_result.stderr)
                )
            child_python = venv_python(venv_dir)
            if not child_python.is_file():
                raise InstalledArtifactError(f"venv Python executable is missing: {child_python}")

            install_base = [
                str(child_python),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--no-input",
            ]
            install_env = {
                "PATH": os.environ.get("PATH", os.defpath),
                "HOME": str(workspace_path / "pip-home"),
                "TMPDIR": str(workspace_path / "pip-tmp"),
                "TMP": str(workspace_path / "pip-tmp"),
                "TEMP": str(workspace_path / "pip-tmp"),
                "PYTHONNOUSERSITE": "1",
                "PIP_DISABLE_PIP_VERSION_CHECK": "1",
                **_pip_network_environment(),
            }
            Path(install_env["HOME"]).mkdir(parents=True, exist_ok=True)
            Path(install_env["TMPDIR"]).mkdir(parents=True, exist_ok=True)
            if install_dependencies:
                dependency_result = subprocess.run(
                    [
                        *install_base,
                        "--require-hashes",
                        "-r",
                        str(dependency_lock_path),
                    ],
                    cwd=workspace_path,
                    env=install_env,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if dependency_result.returncode != 0:
                    raise InstalledArtifactError(
                        "hashed runtime dependency installation failed:\n"
                        + _redact_output(
                            dependency_result.stdout + dependency_result.stderr
                        )
                    )
            install_command = [
                *install_base,
                "--no-deps",
                "--no-index",
                str(artifact.path),
            ]
            install_result = subprocess.run(
                install_command,
                cwd=workspace_path,
                env=install_env,
                capture_output=True,
                text=True,
                check=False,
            )
            if install_result.returncode != 0:
                raise InstalledArtifactError(
                    "wheel installation failed:\n"
                    + _redact_output(install_result.stdout + install_result.stderr)
                )
            if install_dependencies:
                check_result = subprocess.run(
                    [str(child_python), "-m", "pip", "check"],
                    cwd=workspace_path,
                    env=install_env,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if check_result.returncode != 0:
                    raise InstalledArtifactError(
                        "locked installed environment failed pip check:\n"
                        + _redact_output(check_result.stdout + check_result.stderr)
                    )

            roots = IsolatedRoots.create(workspace_path / "roots")
            harness = cls(
                repo_root=root,
                workspace=workspace_path,
                owned_workspace=owned,
                source_snapshot=source_snapshot,
                dist_dir=dist_dir,
                venv_dir=venv_dir,
                artifact=artifact,
                roots=roots,
                python_executable=child_python,
            )
            harness.probe_identity()
            return harness
        except Exception:
            if owned:
                shutil.rmtree(workspace_path, ignore_errors=True)
            raise

    @classmethod
    def attach(
        cls,
        repo_root: str | Path,
        *,
        wheel: str | Path,
        venv_dir: str | Path,
        workspace: str | Path | None = None,
    ) -> InstalledArtifactHarness:
        """Run lanes against a wheel and venv that something else built.

        CI's release job builds one wheel with the hashed build lock and installs
        it with only the hashed runtime lock; this adopts that exact pair instead
        of building a second one.  Nothing is built or installed here, so the
        pair is verified rather than trusted: the wheel must be a real
        ``astrid`` wheel, the venv must import Astrid from its own
        site-packages at the wheel's version, and every hashed file in the
        wheel's RECORD must appear with the same hash in the installed copy (a
        stale venv or a different build of the same version fails here).

        ``workspace`` behaves as in :meth:`build`: omitted means the harness
        owns a temporary one and removes it on :meth:`close`.
        """
        root = Path(repo_root).expanduser().resolve()
        if not (root / "pyproject.toml").is_file():
            raise InstalledArtifactError(f"repository root is missing pyproject.toml: {root}")
        artifact = inspect_wheel(wheel)
        if artifact.distribution.lower().replace("-", "_") != DEFAULT_DISTRIBUTION:
            raise InstalledArtifactError(
                f"expected {DEFAULT_DISTRIBUTION!r} wheel, found {artifact.distribution!r}"
            )
        venv_path = Path(venv_dir).expanduser().absolute()
        python = venv_python(venv_path)
        if not python.is_file():
            raise InstalledArtifactError(f"venv Python executable is missing: {python}")
        owned = workspace is None
        if workspace is None:
            workspace_path = Path(tempfile.mkdtemp(prefix="astrid-installed-artifact-")).resolve()
        else:
            workspace_path = Path(workspace).expanduser().resolve()
            workspace_path.mkdir(parents=True, exist_ok=True)
        try:
            if _relative_to(workspace_path, root):
                raise InstalledArtifactError(
                    f"isolated workspace must be outside the checkout: {workspace_path}"
                )
            harness = cls(
                repo_root=root,
                workspace=workspace_path,
                owned_workspace=owned,
                # Nothing is copied from the checkout in this mode; the path
                # only feeds the "no source path in lane output" check.
                source_snapshot=workspace_path / "source",
                dist_dir=artifact.path.parent,
                venv_dir=venv_path,
                artifact=artifact,
                roots=IsolatedRoots.create(workspace_path / "roots"),
                python_executable=python,
            )
            identity = harness.probe_identity()
            _require_installed_matches_wheel(artifact.path, Path(identity.import_path))
            return harness
        except Exception:
            if owned:
                shutil.rmtree(workspace_path, ignore_errors=True)
            raise

    def __enter__(self) -> InstalledArtifactHarness:
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _tb: Any) -> None:
        self.close()

    @property
    def artifact_digest(self) -> str:
        return self.artifact.sha256

    @property
    def installed_version(self) -> str:
        if self.identity is None:
            self.probe_identity()
        assert self.identity is not None
        return self.identity.version

    def environment(self, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        """Return a scrubbed lane environment, optionally with safe overrides."""
        env = scrub_environment(roots=self.roots, venv_dir=self.venv_dir)
        for key, value in (extra or {}).items():
            if is_secret_env_name(key):
                raise InstalledArtifactError(f"secret-like environment override is forbidden: {key}")
            if key in {"PYTHONPATH", "PYTHONHOME"}:
                raise InstalledArtifactError(
                    f"Python path injection is forbidden (checkout imports are not allowed): {key}"
                )
            env[str(key)] = str(value)
        return env

    def _cwd(self, cwd: str | Path | None) -> Path:
        path = (
            Path(cwd).expanduser().resolve()
            if cwd is not None
            else (self.workspace / "outside-checkout").resolve()
        )
        if _relative_to(path, self.repo_root) or _relative_to(path, self.source_snapshot):
            raise InstalledArtifactError(f"lane cwd must be outside source trees: {path}")
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _python_command(self, args: Sequence[str]) -> list[str]:
        normalized = list(args)
        if normalized and (
            Path(normalized[0]).name in {"python", "python3", "python.exe"}
            or normalized[0] == str(self.python_executable)
        ):
            normalized = normalized[1:]
        return [str(self.python_executable), "-I", *normalized]

    def probe_identity(self) -> InstalledIdentity:
        """Prove the venv import path/version and reject source/dependency leaks."""
        cwd = self._cwd(None)
        env = self.environment()
        completed = subprocess.run(
            self._python_command(["-c", _IDENTITY_PROBE]),
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        output = _redact_output(completed.stdout + completed.stderr)
        if completed.returncode != 0:
            raise ArtifactIdentityError(f"identity probe failed: {output}")
        payload = _json_from_output(output)
        try:
            executable = str(payload["executable"])
            import_path = Path(str(payload["import_path"])).expanduser().resolve()
            version = str(payload["version"])
            prefix = Path(str(payload["prefix"])).expanduser().resolve()
            base_prefix = Path(str(payload["base_prefix"])).expanduser().resolve()
            child_purelib = Path(str(payload["purelib"])).expanduser().resolve()
            sys_path = tuple(str(item) for item in payload["sys_path"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ArtifactIdentityError(f"identity probe omitted required fields: {payload}") from exc
        if version != self.artifact.version:
            raise ArtifactIdentityError(
                f"installed version {version!r} does not match wheel version {self.artifact.version!r}"
            )
        if _relative_to(import_path, self.repo_root) or _relative_to(import_path, self.source_snapshot):
            raise ArtifactIdentityError(f"Astrid imported from a checkout/source tree: {import_path}")
        if prefix != self.venv_dir.resolve() or base_prefix == prefix:
            raise ArtifactIdentityError(
                f"child interpreter is not an isolated venv: prefix={prefix}, base_prefix={base_prefix}"
            )
        purelib_candidates = {
            child_purelib,
            (prefix / "Lib" / "site-packages").resolve(),
        }
        if not any(_relative_to(import_path, candidate) for candidate in purelib_candidates):
            raise ArtifactIdentityError(
                f"Astrid import path is outside the venv site-packages roots: {import_path}"
            )
        for entry in sys_path:
            entry_path = Path(entry or cwd).expanduser().resolve()
            if _relative_to(entry_path, self.repo_root) or _relative_to(entry_path, self.source_snapshot):
                raise ArtifactIdentityError(f"checkout path leaked into child sys.path: {entry_path}")
            if "site-packages" in entry_path.parts and not any(
                _relative_to(entry_path, candidate) for candidate in purelib_candidates
            ):
                raise ArtifactIdentityError(f"foreign site-packages path leaked into child sys.path: {entry_path}")
        pyvenv = self.venv_dir / "pyvenv.cfg"
        config = pyvenv.read_text(encoding="utf-8").lower() if pyvenv.is_file() else ""
        if "include-system-site-packages = true" in config:
            raise ArtifactIdentityError("isolated venv enables system site-packages")
        identity = InstalledIdentity(
            executable=executable,
            import_path=str(import_path),
            version=version,
            wheel_sha256=self.artifact.sha256,
            prefix=str(prefix),
            base_prefix=str(base_prefix),
            sys_path=sys_path,
        )
        self.identity = identity
        return identity

    def run_lane(
        self,
        lane: str,
        command: Sequence[str] | str,
        *,
        timeout: float = 120.0,
        cwd: str | Path | None = None,
        env: Mapping[str, str] | None = None,
        check: bool = False,
    ) -> LaneRecord:
        """Run Python args outside the checkout and retain an identity record."""
        if not isinstance(lane, str) or not lane.strip():
            raise ValueError("lane must be a non-empty string")
        args = shlex.split(command) if isinstance(command, str) else [str(item) for item in command]
        if not args:
            raise ValueError("lane command must not be empty")
        identity = self.probe_identity()
        full_command = self._python_command(args)
        started_at = utc_now_iso()
        started_clock = __import__("time").monotonic()
        completed = subprocess.run(
            full_command,
            cwd=self._cwd(cwd),
            env=self.environment(env),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        finished_at = utc_now_iso()
        stdout = _redact_output(completed.stdout)
        stderr = _redact_output(completed.stderr)
        output = stdout + stderr
        source_markers = (str(self.repo_root), str(self.source_snapshot))
        source_leak = next((marker for marker in source_markers if marker in output), None)
        status = "passed" if completed.returncode == 0 and source_leak is None else "failed"
        error = None
        if source_leak is not None:
            error = f"lane output exposed a source-tree path: {source_leak}"
        elif completed.returncode != 0:
            error = f"lane exited with status {completed.returncode}"
        record = LaneRecord(
            lane=lane,
            command=tuple(full_command),
            executable=identity.executable,
            import_path=identity.import_path,
            version=identity.version,
            wheel_sha256=self.artifact.sha256,
            status=status,
            returncode=completed.returncode,
            started_at=started_at,
            finished_at=finished_at,
            duration_seconds=round(__import__("time").monotonic() - started_clock, 6),
            stdout=stdout,
            stderr=stderr,
            output=output,
            roots=self.roots.as_dict(),
            error=error,
        )
        self.last_record = record
        if check and not record.passed:
            raise LaneExecutionError(f"installed lane {lane!r} failed: {_failure_detail(error, stderr)}", record)
        return record

    def run_module(
        self,
        lane: str,
        module: str,
        args: Sequence[str] = (),
        **kwargs: Any,
    ) -> LaneRecord:
        return self.run_lane(lane, ["-m", module, *map(str, args)], **kwargs)

    def run_console(
        self,
        lane: str,
        args: Sequence[str] = (),
        *,
        executable: str = "astrid",
        timeout: float = 120.0,
        cwd: str | Path | None = None,
        env: Mapping[str, str] | None = None,
        check: bool = False,
    ) -> LaneRecord:
        """Run the installed console script while retaining the same identity."""
        identity = self.probe_identity()
        script = self.venv_dir / ("Scripts" if os.name == "nt" else "bin") / executable
        if not script.is_file():
            raise InstalledArtifactError(f"installed console entry point is missing: {script}")
        full_command = [str(script), *map(str, args)]
        started_at = utc_now_iso()
        import time

        started_clock = time.monotonic()
        completed = subprocess.run(
            full_command,
            cwd=self._cwd(cwd),
            env=self.environment(env),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        finished_at = utc_now_iso()
        stdout = _redact_output(completed.stdout)
        stderr = _redact_output(completed.stderr)
        output = stdout + stderr
        source_markers = (str(self.repo_root), str(self.source_snapshot))
        source_leak = next((marker for marker in source_markers if marker in output), None)
        status = "passed" if completed.returncode == 0 and source_leak is None else "failed"
        error = (
            f"console output exposed a source-tree path: {source_leak}"
            if source_leak
            else None
        ) or (f"lane exited with status {completed.returncode}" if completed.returncode else None)
        record = LaneRecord(
            lane=lane,
            command=tuple(full_command),
            executable=identity.executable,
            import_path=identity.import_path,
            version=identity.version,
            wheel_sha256=self.artifact.sha256,
            status=status,
            returncode=completed.returncode,
            started_at=started_at,
            finished_at=finished_at,
            duration_seconds=round(time.monotonic() - started_clock, 6),
            stdout=stdout,
            stderr=stderr,
            output=output,
            roots=self.roots.as_dict(),
            error=error,
        )
        self.last_record = record
        if check and not record.passed:
            raise LaneExecutionError(f"installed lane {lane!r} failed: {_failure_detail(error, stderr)}", record)
        return record

    def close(self) -> None:
        """Remove only an auto-created workspace; explicit workspaces persist."""
        if self._owned_workspace:
            shutil.rmtree(self.workspace, ignore_errors=True)


def provision_locked_environment(
    repo_root: str | Path,
    *,
    workspace: str | Path | None = None,
    python_executable: str | Path | None = None,
    lock_path: str | Path = PROOF_LOCK,
) -> LockedEnvironment:
    """Provision a proof interpreter from one repository-owned hash lock.

    The returned interpreter is suitable for both source-copy and installed
    artifact lanes.  Provisioning is the only operation allowed to resolve
    packages; proof subprocesses run with a scrubbed environment, disabled
    user-site discovery, and no checkout path except the lane's explicit
    ``PYTHONPATH``.
    """
    root = Path(repo_root).expanduser().resolve()
    lock = (root / lock_path).resolve() if not Path(lock_path).is_absolute() else Path(lock_path).resolve()
    if not lock.is_file():
        raise InstalledArtifactError(f"required hashed proof lock is missing: {lock}")
    owned = workspace is None
    if workspace is None:
        workspace_path = Path(tempfile.mkdtemp(prefix="astrid-proof-environment-")).resolve()
    else:
        workspace_path = Path(workspace).expanduser().resolve()
        if _relative_to(workspace_path, root):
            raise InstalledArtifactError(
                f"isolated proof workspace must be outside the checkout: {workspace_path}"
            )
        workspace_path.mkdir(parents=True, exist_ok=True)
    venv_dir = workspace_path / "venv"
    try:
        venv_result = subprocess.run(
            [str(python_executable or sys.executable), "-m", "venv", str(venv_dir)],
            cwd=workspace_path,
            capture_output=True,
            text=True,
            check=False,
        )
        if venv_result.returncode != 0:
            raise InstalledArtifactError(
                "isolated proof venv creation failed:\n"
                + _redact_output(venv_result.stdout + venv_result.stderr)
            )
        child_python = venv_python(venv_dir)
        if not child_python.is_file():
            raise InstalledArtifactError(f"proof venv Python executable is missing: {child_python}")
        install_root = workspace_path / "pip"
        install_env = {
            "PATH": os.environ.get("PATH", os.defpath),
            "HOME": str(install_root / "home"),
            "TMPDIR": str(install_root / "tmp"),
            "TMP": str(install_root / "tmp"),
            "TEMP": str(install_root / "tmp"),
            "PYTHONNOUSERSITE": "1",
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            **_pip_network_environment(),
        }
        Path(install_env["HOME"]).mkdir(parents=True, exist_ok=True)
        Path(install_env["TMPDIR"]).mkdir(parents=True, exist_ok=True)
        install = subprocess.run(
            [
                str(child_python),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--no-input",
                "--require-hashes",
                "-r",
                str(lock),
            ],
            cwd=workspace_path,
            env=install_env,
            capture_output=True,
            text=True,
            check=False,
        )
        if install.returncode != 0:
            raise InstalledArtifactError(
                "hashed proof dependency installation failed:\n"
                + _redact_output(install.stdout + install.stderr)
            )
        check = subprocess.run(
            [str(child_python), "-m", "pip", "check"],
            cwd=workspace_path,
            env=install_env,
            capture_output=True,
            text=True,
            check=False,
        )
        if check.returncode != 0:
            raise InstalledArtifactError(
                "locked proof environment failed pip check:\n"
                + _redact_output(check.stdout + check.stderr)
            )
        return LockedEnvironment(
            workspace=workspace_path,
            venv_dir=venv_dir,
            python_executable=child_python,
            lock_path=lock,
            owned_workspace=owned,
        )
    except Exception:
        if owned:
            shutil.rmtree(workspace_path, ignore_errors=True)
        raise


def build_once(
    repo_root: str | Path,
    *,
    workspace: str | Path | None = None,
    python_executable: str | Path | None = None,
    install_dependencies: bool = False,
    dependency_lock: str | Path = RUNTIME_LOCK,
) -> InstalledArtifactHarness:
    """Convenience entry point for the shared build-once harness."""
    return InstalledArtifactHarness.build(
        repo_root,
        workspace=workspace,
        python_executable=python_executable,
        install_dependencies=install_dependencies,
        dependency_lock=dependency_lock,
    )


# Friendly aliases for later lane modules and external callers.
ArtifactHarness = InstalledArtifactHarness
Harness = InstalledArtifactHarness


__all__ = [
    "ArtifactHarness",
    "ArtifactIdentityError",
    "DEFAULT_DISTRIBUTION",
    "Harness",
    "InstalledArtifactError",
    "InstalledArtifactHarness",
    "InstalledIdentity",
    "IsolatedRoots",
    "LockedEnvironment",
    "LaneExecutionError",
    "LaneRecord",
    "SCHEMA",
    "WheelArtifact",
    "WheelSelectionError",
    "build_once",
    "inspect_wheel",
    "is_secret_env_name",
    "scrub_environment",
    "select_single_wheel",
    "sha256_file",
    "provision_locked_environment",
]
