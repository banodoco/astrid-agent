from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import os
import shutil
import subprocess
import tarfile
import time
import zipfile
from collections.abc import Mapping
from dataclasses import asdict, dataclass, is_dataclass, replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.core.execution.generic_host import (
    GenericPackHost,
    HostError,
    RuntimeProtocolClient,
    source_checkout_digest,
)
from astrid.core.receipts.canonical import canonical_json
from astrid.packs.rendering.actions.assemble_timeline.run import (
    build_authoring_proposal,
    build_native_proposal,
)
from astrid.packs.rendering.shared.runtime_stitch import publish_authoring_proposal
from astrid.sdk.client import AstridClient
from astrid.sdk.timeline.assembly import read_assembly_proposal
from astrid.sdk.timeline.sync import TimelineSession
from astrid.sdk.workspace_client import WorkspaceClientError
from tests.sdk.test_timeline_render_publication import (
    publish as publish_requester_commit,
)
from tests.sdk.test_timeline_render_publication import (
    requester as requester_commit_fixture,
)

_D13_REQUIRED_PROFILE_KEYS = frozenset(
    {"profile", "runtime_checkout", "source_checkout", "runtime_environment"}
)
_D13_REQUIRED_ORIGINS = (
    "host", "child", "action", "client", "sdk", "runtime_protocol",
    "runtime_protocol.platform", "astrid_runtime",
)


@dataclass(frozen=True)
class _D13Inputs:
    """Manager-selected D13 inputs; none are product authority fields."""

    profile: Path
    bindings: Path
    expected_source: Path
    expected_environment: Path
    agent_commit: str
    agent_tree: str
    agent_wheel: Path
    agent_wheel_sha256: str
    source_digest: str
    node: Path
    node_sha256: str
    remotion: Path
    remotion_lock_sha256: str
    browser: Path
    browser_sha256: str
    ffmpeg: Path
    ffmpeg_sha256: str
    ffprobe: Path
    ffprobe_sha256: str
    node_modules: Path
    source_repo: Path | None = None


_D13_BINDING_KEYS = frozenset({
    "agent_commit", "agent_tree", "agent_wheel", "agent_wheel_sha256",
    "source_checkout", "source_digest", "runtime_environment",
})


def _d13_inputs() -> tuple[_D13Inputs, list[str]]:
    """Read the explicit manager contract and the existing tool environment."""
    failures: list[str] = []

    def required(name: str) -> str:
        value = os.environ.get(name, "").strip()
        if not value:
            failures.append(f"missing required D13 environment input: {name}")
        return value

    profile = Path(required("ASTRID_D13_SOURCE_PROFILE"))
    bindings_path = Path(required("ASTRID_D13_EXPECTED_BINDINGS"))
    bindings: dict[str, str] = {}
    if bindings_path.is_file() and not bindings_path.is_symlink():
        try:
            raw = json.loads(bindings_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and set(raw) == _D13_BINDING_KEYS and all(
                isinstance(value, str) and value.strip() for value in raw.values()
            ):
                bindings = {key: value.strip() for key, value in raw.items()}
            else:
                failures.append("D13 expected bindings must contain the exact Agent/source keys")
        except (OSError, ValueError) as exc:
            failures.append(f"D13 expected bindings are unreadable: {exc}")
    else:
        failures.append(f"D13 expected bindings are not a regular file: {bindings_path}")

    def binding(name: str) -> str:
        value = bindings.get(name, "")
        if not value:
            failures.append(f"D13 expected bindings missing {name}")
        return value

    return (
        _D13Inputs(
            profile=profile,
            bindings=bindings_path,
            expected_source=Path(binding("source_checkout")),
            expected_environment=Path(binding("runtime_environment")),
            agent_commit=binding("agent_commit"),
            agent_tree=binding("agent_tree"),
            agent_wheel=Path(binding("agent_wheel")),
            agent_wheel_sha256=binding("agent_wheel_sha256"),
            source_digest=binding("source_digest"),
            node=Path(required("ASTRID_NODE_EXECUTABLE")),
            node_sha256=required("ASTRID_D13_NODE_SHA256"),
            remotion=Path(required("ASTRID_REMOTION_PROJECT_DIR")),
            remotion_lock_sha256=required("ASTRID_D13_REMOTION_LOCK_SHA256"),
            browser=Path(required("ASTRID_REMOTION_BROWSER_EXECUTABLE")),
            browser_sha256=required("ASTRID_D13_BROWSER_SHA256"),
            ffmpeg=Path(required("ASTRID_FFMPEG_EXECUTABLE")),
            ffmpeg_sha256=required("ASTRID_D13_FFMPEG_SHA256"),
            ffprobe=Path(required("ASTRID_FFPROBE_EXECUTABLE")),
            ffprobe_sha256=required("ASTRID_D13_FFPROBE_SHA256"),
            node_modules=Path(required("ASTRID_TEST_NODE_MODULES")),
            source_repo=Path(__file__).resolve().parents[3],
        ),
        failures,
    )


def _plain(value):
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _d13_git_blob_digest(data: bytes, algorithm: str) -> str:
    header = f"blob {len(data)}\0".encode("ascii")
    return hashlib.new(algorithm, header + data).hexdigest()


def _d13_source_archive_failures(wheel: Path, inputs: _D13Inputs) -> list[str]:
    """Tie the selected wheel to the declared commit/tree through Git blobs."""
    if inputs.source_repo is None:
        return []
    failures: list[str] = []
    repo = inputs.source_repo.resolve()
    commit = inputs.agent_commit.lower()

    def git(*args: str) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    verified = git("rev-parse", "--verify", f"{commit}^{{commit}}")
    if verified.returncode != 0:
        return [f"declared Agent commit is not present in selected source repository: {commit}"]
    actual_tree = git("rev-parse", "--verify", f"{commit}^{{tree}}")
    if actual_tree.returncode != 0:
        return [f"declared Agent commit tree could not be resolved: {commit}"]
    if actual_tree.stdout.decode().strip() != inputs.agent_tree.lower():
        failures.append(
            "declared Agent tree does not match the declared commit: "
            f"{actual_tree.stdout.decode().strip()} != {inputs.agent_tree}"
        )
    object_format = git("rev-parse", "--show-object-format")
    if object_format.returncode != 0 or object_format.stdout.decode().strip() not in {"sha1", "sha256"}:
        failures.append("selected source repository object format is unavailable or unsupported")
        return failures
    algorithm = object_format.stdout.decode().strip()
    listing = git("ls-tree", "-r", "-z", commit, "--", "astrid")
    if listing.returncode != 0:
        failures.append("declared Agent commit tree could not be listed")
        return failures
    blobs: dict[str, str] = {}
    for entry in listing.stdout.split(b"\0"):
        if not entry:
            continue
        header, path_bytes = entry.split(b"\t", 1)
        mode, kind, oid = header.split()
        if kind != b"blob" or mode not in {b"100644", b"100755"}:
            continue
        blobs[path_bytes.decode("utf-8")] = oid.decode("ascii")

    try:
        with tarfile.open(
            fileobj=io.BytesIO(git("archive", "--format=tar", commit, "--", "astrid").stdout),
            mode="r:",
        ) as archive:
            archived = {
                member.name.removeprefix("./"): archive.extractfile(member).read()
                for member in archive.getmembers()
                if member.isfile() and member.name.removeprefix("./").startswith("astrid/")
            }
    except (OSError, tarfile.TarError, ValueError) as exc:
        failures.append(f"declared Agent source archive could not be read: {exc}")
        return failures

    try:
        with zipfile.ZipFile(wheel) as package:
            package_members = {
                name: package.read(name)
                for name in package.namelist()
                if name.startswith("astrid/") and not name.endswith("/")
            }
    except (OSError, zipfile.BadZipFile) as exc:
        failures.append(f"Agent wheel could not be read for source parity: {exc}")
        return failures

    for path, package_bytes in package_members.items():
        archive_bytes = archived.get(path)
        if archive_bytes is None:
            failures.append(f"Agent wheel member is absent from declared source archive: {path}")
            continue
        if archive_bytes != package_bytes:
            failures.append(f"Agent wheel member differs from declared source archive: {path}")
        blob = blobs.get(path)
        if blob is None:
            failures.append(f"declared source tree has no blob for Agent wheel member: {path}")
        elif _d13_git_blob_digest(archive_bytes, algorithm) != blob:
            failures.append(f"declared source archive blob differs from Git object: {path}")
    return failures


def _d13_record_rows(text: str) -> dict[str, tuple[str, str]]:
    rows: dict[str, tuple[str, str]] = {}
    for row in csv.reader(text.splitlines()):
        if len(row) != 3 or not row[0] or row[0] in rows:
            raise ValueError("malformed or duplicate RECORD row")
        rows[row[0]] = (row[1], row[2])
    return rows


def _d13_record_digest(data: bytes, algorithm: str) -> str:
    digest = hashlib.new(algorithm, data).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _d13_wheel_record_failures(wheel: Path, source: Path) -> list[str]:
    """Require wheel RECORD hashes and installed package bytes to agree."""
    failures: list[str] = []
    try:
        with zipfile.ZipFile(wheel) as archive:
            record_names = [name for name in archive.namelist() if name.endswith(".dist-info/RECORD")]
            if len(record_names) != 1:
                return [f"Agent wheel must contain exactly one RECORD, found {len(record_names)}"]
            record_name = record_names[0]
            wheel_rows = _d13_record_rows(archive.read(record_name).decode("utf-8"))
            wheel_members = {name for name in archive.namelist() if not name.endswith("/")}
            if set(wheel_rows) != wheel_members:
                failures.append("Agent wheel RECORD does not enumerate exactly its file members")
            wheel_bytes = {
                name: archive.read(name)
                for name in wheel_members
                if name != record_name
            }
    except (OSError, UnicodeDecodeError, ValueError, zipfile.BadZipFile) as exc:
        return [f"Agent wheel RECORD is unreadable: {exc}"]

    record_path = Path(record_name)
    if (
        not record_name
        or record_path.is_absolute()
        or any(part in {"", ".", ".."} for part in record_name.split("/"))
    ):
        failures.append(f"Agent wheel RECORD contains an unsafe path: {record_name}")
        return failures
    if source.is_symlink():
        failures.append(f"installed Agent RECORD is missing or symlinked: {source}")
        return failures
    installed_record = source
    for part in record_path.parts:
        installed_record /= part
        if installed_record.is_symlink():
            failures.append(f"installed Agent RECORD is missing or symlinked: {installed_record}")
            return failures
    if not installed_record.is_file() or installed_record.is_symlink():
        failures.append(f"installed Agent RECORD is missing or symlinked: {installed_record}")
        return failures
    try:
        installed_rows = _d13_record_rows(installed_record.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        return [f"installed Agent RECORD is unreadable: {exc}"]

    for name, (expected_hash, expected_size) in wheel_rows.items():
        if name == record_name:
            if expected_hash or expected_size:
                failures.append("wheel RECORD must leave its own row unhashed")
            continue
        if not name or name.startswith("/") or ".." in Path(name).parts:
            failures.append(f"Agent wheel RECORD contains an unsafe path: {name}")
            continue
        target = source / Path(name)
        if not target.is_file() or target.is_symlink():
            failures.append(f"installed Agent RECORD member is missing: {name}")
            continue
        actual = target.read_bytes()
        if str(len(actual)) != expected_size:
            failures.append(f"installed Agent RECORD size differs: {name}")
        if not expected_hash.startswith("sha256="):
            failures.append(f"Agent wheel RECORD uses an unsupported digest: {name}")
        elif _d13_record_digest(actual, "sha256") != expected_hash.removeprefix("sha256="):
            failures.append(f"installed Agent RECORD hash differs: {name}")
        if name in wheel_bytes and actual != wheel_bytes[name]:
            failures.append(f"installed Agent package bytes differ from wheel: {name}")
        installed_hash, installed_size = installed_rows.get(name, ("", ""))
        if (installed_hash, installed_size) != (expected_hash, expected_size):
            failures.append(f"installed Agent RECORD does not preserve wheel row: {name}")
    return failures


def _d13_resource_locks(profile: Mapping[str, str], inputs: _D13Inputs) -> dict[str, str]:
    source = Path(profile["source_checkout"]).resolve()
    resources = {
        "source": str(source),
        "source_digest": source_checkout_digest(source),
        "node": _sha256_file(inputs.node),
        "remotion_package_lock": _sha256_file(inputs.remotion / "package-lock.json"),
        "browser": _sha256_file(inputs.browser),
        "ffmpeg": _sha256_file(inputs.ffmpeg),
        "ffprobe": _sha256_file(inputs.ffprobe),
    }
    return resources


def _d13_binding_failures(profile: Mapping[str, str], inputs: _D13Inputs) -> list[str]:
    """Compare selected profile/resources with manager-supplied expectations."""
    failures: list[str] = []
    expected_source = inputs.expected_source.resolve()
    expected_environment = inputs.expected_environment.resolve()
    actual_source = Path(str(profile.get("source_checkout", ""))).resolve()
    actual_runtime = Path(str(profile.get("runtime_checkout", ""))).resolve()
    actual_environment = Path(str(profile.get("runtime_environment", ""))).resolve()
    if actual_source != expected_source:
        failures.append(f"selected source differs from manager binding: {actual_source} != {expected_source}")
    if actual_runtime != expected_source:
        failures.append(f"selected runtime tree differs from manager binding: {actual_runtime} != {expected_source}")
    if actual_environment != expected_environment:
        failures.append(
            f"selected runtime environment differs from manager binding: "
            f"{actual_environment} != {expected_environment}"
        )
    if actual_source.is_dir():
        try:
            actual_digest = source_checkout_digest(actual_source)
        except (OSError, ValueError) as exc:
            failures.append(f"selected source digest could not be measured: {exc}")
        else:
            if actual_digest != inputs.source_digest:
                failures.append(
                    f"selected source digest differs from manager binding: "
                    f"{actual_digest} != {inputs.source_digest}"
                )

    for label, value in (("commit", inputs.agent_commit), ("tree", inputs.agent_tree)):
        if len(value) != 40 or any(char not in "0123456789abcdef" for char in value.lower()):
            failures.append(f"manager Agent {label} binding is not a full object id")
    for label, value in (
        ("wheel", inputs.agent_wheel_sha256), ("source", inputs.source_digest),
        ("node", inputs.node_sha256), ("Remotion lock", inputs.remotion_lock_sha256),
        ("browser", inputs.browser_sha256), ("ffmpeg", inputs.ffmpeg_sha256),
        ("ffprobe", inputs.ffprobe_sha256),
    ):
        if len(value) != 64 or any(char not in "0123456789abcdef" for char in value.lower()):
            failures.append(f"manager {label} binding is not a SHA-256 digest")
    wheel = inputs.agent_wheel
    if not wheel.is_file() or wheel.is_symlink():
        failures.append(f"manager Agent wheel is unavailable or symlinked: {wheel}")
    else:
        actual_wheel = _sha256_file(wheel)
        if actual_wheel != inputs.agent_wheel_sha256:
            failures.append(
                f"Agent wheel differs from manager binding: {actual_wheel} != {inputs.agent_wheel_sha256}"
            )
        else:
            try:
                with zipfile.ZipFile(wheel) as archive:
                    for member in archive.namelist():
                        if member.endswith("/RECORD"):
                            continue
                        member_path = Path(member)
                        if member_path.is_absolute() or ".." in member_path.parts:
                            failures.append(f"Agent wheel member escapes its package root: {member}")
                            continue
                        target = actual_source / member_path
                        if target.is_symlink() or not target.is_file():
                            failures.append(f"installed Agent wheel member is missing: {member}")
                        elif target.read_bytes() != archive.read(member):
                            failures.append(f"installed Agent wheel member differs: {member}")
            except (OSError, ValueError, zipfile.BadZipFile) as exc:
                failures.append(f"Agent wheel could not be compared with installed files: {exc}")

    resources = (
        ("node", inputs.node, inputs.node_sha256),
        ("Remotion package lock", inputs.remotion / "package-lock.json", inputs.remotion_lock_sha256),
        ("browser", inputs.browser, inputs.browser_sha256),
        ("ffmpeg", inputs.ffmpeg, inputs.ffmpeg_sha256),
        ("ffprobe", inputs.ffprobe, inputs.ffprobe_sha256),
    )
    for label, path, expected in resources:
        if not path.is_file() or path.is_symlink():
            failures.append(f"{label} is unavailable or symlinked: {path}")
        elif _sha256_file(path) != expected:
            failures.append(f"{label} differs from manager-selected tool binding: {path}")
    if not inputs.remotion.is_dir() or inputs.remotion.is_symlink():
        failures.append(f"Remotion project is unavailable or symlinked: {inputs.remotion}")
    if not inputs.node_modules.is_dir() or inputs.node_modules.is_symlink():
        failures.append(f"Node modules are unavailable or symlinked: {inputs.node_modules}")
    failures.extend(_d13_source_archive_failures(wheel, inputs))
    failures.extend(_d13_wheel_record_failures(wheel, actual_source))
    return failures


def _d13_installed_profile(tmp_path: Path) -> tuple[dict[str, str], dict[str, str]]:
    inputs, failures = _d13_inputs()
    if not inputs.profile.is_file() or inputs.profile.is_symlink():
        failures.append(f"profile is not a regular file: {inputs.profile}")
        return {}, {"failures": "; ".join(failures)}
    try:
        profile = json.loads(inputs.profile.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {}, {"failures": f"profile unreadable: {exc}"}
    if not isinstance(profile, dict) or set(profile) != _D13_REQUIRED_PROFILE_KEYS:
        failures.append(f"profile keys are not the existing four-field carrier: {sorted(profile) if isinstance(profile, dict) else type(profile).__name__}")
        profile = {}
    if profile.get("profile") != "astrid":
        failures.append("profile id is not astrid")
    if profile:
        failures.extend(_d13_binding_failures(profile, inputs))
        source = Path(str(profile["source_checkout"]))
        runtime = Path(str(profile["runtime_checkout"]))
        environment = Path(str(profile["runtime_environment"]))
        if not source.is_absolute() or source.is_symlink() or not source.is_dir():
            failures.append(f"installed source root is not a regular absolute directory: {source}")
        if source.resolve() != runtime.resolve():
            failures.append("runtime_checkout and source_checkout do not select the same installed tree")
        if not environment.is_absolute() or environment.is_symlink() or not environment.is_dir():
            failures.append(f"runtime environment is not a regular absolute directory: {environment}")
        interpreter = environment / "bin" / "python"
        if not interpreter.is_file() or interpreter.is_symlink():
            failures.append(f"installed interpreter is unavailable or symlinked: {interpreter}")
        elif Path(os.sys.executable).resolve() != interpreter.resolve():
            failures.append(f"pytest interpreter is not the selected installed interpreter: {os.sys.executable} != {interpreter}")
        for key in ("source_checkout", "runtime_checkout", "runtime_environment"):
            path = Path(str(profile[key]))
            if any(part.is_symlink() for part in (path, *path.parents)):
                failures.append(f"profile path traverses a symlink: {key}={path}")
        try:
            from astrid.sdk.host_bootstrap import _installed_execution_origins

            origins = _installed_execution_origins(source, str(interpreter))
            for key in _D13_REQUIRED_ORIGINS:
                if not origins.get(key):
                    failures.append(f"installed origin missing: {key}")
                elif not Path(str(origins[key])).resolve().is_relative_to(source.resolve()):
                    failures.append(f"installed origin escaped selected tree: {key}={origins[key]}")
        except (AttributeError, ImportError, KeyError, OSError, TypeError, ValueError) as exc:
            failures.append(f"installed origin probe failed: {exc}")
    fonts = inputs.remotion / "public" / "fonts"
    if not fonts.is_dir() or not (fonts / "FONT_PROVENANCE.md").is_file():
        failures.append("Remotion font provenance is unavailable")
    else:
        font_files = tuple(fonts.glob("*.woff2"))
        if not font_files or any(not item.is_file() or item.is_symlink() or item.stat().st_size == 0 for item in font_files):
            failures.append("Remotion font closure is empty, symlinked, or zero-sized")
    attempt = tmp_path / "preflight-attempt"
    if not attempt.is_relative_to(tmp_path):
        failures.append("attempt root is not private to this fixture")
    else:
        attempt.mkdir()
        probe = attempt / "writable-probe"
        probe.write_text("d13", encoding="utf-8")
        if probe.read_text(encoding="utf-8") != "d13" or probe.is_symlink():
            failures.append("private attempt root is not writable and isolated")
    usage = shutil.disk_usage(tmp_path)
    if usage.free < 5 * 1024**3:
        failures.append(f"free disk below 5 GiB: {usage.free}")
    if failures:
        return profile, {"failures": "; ".join(failures)}
    return profile, _d13_resource_locks(profile, inputs)


class _InstalledRuntimeService:
    """Small test-only facade over the generated installed Runtime client."""

    def __init__(self, owner, worker, realm_id: str):
        self.owner = owner
        self.worker = worker
        self.realm = {"id": realm_id}

    def health(self):
        return _plain(self.worker.health())

    def create_project(self, value, *, idempotency_key: str | None = None):
        result = self.owner._remote._transport.create_project(
            value["name"], slug=value.get("slug"),
            idempotency_key=idempotency_key or f"project-{value.get('slug', 'd13')}",
        )
        return {"id": result.get("project_id", result.get("id")), **dict(result)}

    def register_capability(self, value):
        return _plain(self.worker.register_capability(
            value["capability_id"], value["definition_digest"],
            status=value.get("status", "ready"),
            idempotency_key=value.get("idempotency_key"),
        ))

    def register_executor(self, value, *, idempotency_key: str):
        return _plain(self.worker.register_executor(value, idempotency_key=idempotency_key))

    def create_timeline(self, project_id: str, timeline_id: str, *, name: str | None = None, idempotency_key: str):
        return _plain(self.owner._remote._transport.create_timeline(project_id, timeline_id, name=name, idempotency_key=idempotency_key))

    def create_task(self, value):
        result = self.owner._remote._transport.admit_task(
            capability_id=value["capability_id"],
            capability_digest=value["capability_digest"],
            input_object_ids=value.get("input_object_ids", []),
            project_id=value.get("project"), spec=value.get("spec"),
            settlement_effect=value.get("settlement_effect"),
            idempotency_key=value["idempotency_key"],
        )
        task_id = result.get("task_id", result.get("id"))
        return {"task": self.task(task_id)["task"]}

    def claim_next(self, value, *, idempotency_key: str):
        claim = self.worker.claim_task(
            executor_id=value["executor_id"], capability_ids=value["capability_ids"],
            runtime_epoch=value["runtime_epoch"], idempotency_key=idempotency_key,
        )
        result = _plain(claim)
        if result is None:
            raise AssertionError("installed Runtime returned no claim")
        return {**result, "id": result.get("task_id"), "capability": result.get("capability_id")}

    def settle_attempt(self, attempt_id: str, settlement, *, idempotency_key: str):
        return _plain(self.worker.settle_attempt(attempt_id, settlement, idempotency_key=idempotency_key))

    def task(self, task_id: str):
        result = _plain(self.owner._remote._transport.get_task(task_id))
        result["id"] = result["task_id"]
        result["capability"] = result["capability_id"]
        return {"task": result}

    def managed_outputs(self, task_id: str):
        rows, cursor = self.owner._remote._transport.list_managed_outputs(task_id)
        assert cursor is None
        return [_plain(row) for row in rows]

    def object(self, object_id: str):
        result = self.owner._remote._transport.get_object(object_id)
        return result.media_type, result.data

    def count_tasks(self, project_id: str, capability: str) -> int:
        rows, cursor = self.owner._remote._transport.list_project_tasks(project_id, limit=100)
        assert cursor is None
        return sum(row.capability_id == capability for row in rows)


@pytest.fixture
def installed_runtime(tmp_path, monkeypatch):
    """Own the normal installed Runtime authority for the D13 positive."""
    profile, preflight = _d13_installed_profile(tmp_path)
    assert "failures" not in preflight, preflight["failures"]
    inputs, input_failures = _d13_inputs()
    assert not input_failures, "; ".join(input_failures)
    source = Path(profile["source_checkout"]).resolve()
    environment = Path(profile["runtime_environment"]).resolve()
    runtime_cli = environment / "bin" / "astrid-runtime"
    support = tmp_path / "runtime-support"
    realm_root = tmp_path / "realm"
    home = tmp_path / "home"
    temp_root = tmp_path / "tmp"
    for path in (support, realm_root, home, temp_root):
        path.mkdir()
    runtime_env = {
        "HOME": str(home),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": f"{inputs.node.parent}:{environment / 'bin'}:/usr/bin:/bin",
        "PYTHONDONTWRITEBYTECODE": "1",
        "TMPDIR": str(temp_root),
        "ASTRID_HOST_EXECUTION_PROFILE": "installed",
        "ASTRID_PACK_HOST_PYTHON": str(environment / "bin" / "python"),
        "ASTRID_PACKS_PATH": str(source / "astrid" / "packs" / "rendering"),
        "ASTRID_REMOTION_PROJECT_DIR": str(inputs.remotion),
        "ASTRID_REMOTION_BROWSER_EXECUTABLE": str(inputs.browser),
        "ASTRID_NODE_EXECUTABLE": str(inputs.node),
        "ASTRID_TEST_NODE_MODULES": str(inputs.node_modules),
        "ASTRID_FFMPEG_EXECUTABLE": str(inputs.ffmpeg),
        "ASTRID_FFPROBE_EXECUTABLE": str(inputs.ffprobe),
        "ASTRID_TIMELINE_SCHEMA_PYTHONPATH": str(source / "astrid" / "core" / "timeline" / "schema" / "timeline-config.schema.json"),
        "ASTRID_RUNTIME_SOURCE_MANIFEST": str(inputs.profile),
    }
    def run_runtime(*args):
        completed = subprocess.run(
            [str(runtime_cli), *args, "--json"], env=runtime_env, cwd=tmp_path,
            check=False, text=True, capture_output=True,
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr
        return json.loads(completed.stdout)

    bootstrap = None
    try:
        run_runtime(
            "workspace", "create", "--profile", "astrid", "--source-manifest", str(inputs.profile),
            "--data-root", str(support), "--realm-root", str(realm_root),
            "--hosted-origin", "off",
        )
        bootstrap = run_runtime(
            "up", "--profile", "astrid", "--source-manifest", str(inputs.profile),
            "--data-root", str(support), "--hosted-origin", "https://astrid.local",
            "--door-port", "0", "--wait",
        )
        worker_file = Path(bootstrap["worker_credential_file"])
        worker_token = worker_file.read_text(encoding="utf-8").strip()
        worker = RuntimeProtocolClient(bootstrap["endpoint"], worker_token, credential_file=worker_file)
        owner = AstridClient.open(
            endpoint=bootstrap["endpoint"], credential=Path(bootstrap["credential_file"]),
            realm_id=bootstrap["realm_id"], actor_id=bootstrap["actor_id"],
            client_name="astrid-d13-installed", client_version="stage1", protocol_version="workspace.v1",
        )
        service = _InstalledRuntimeService(owner, worker, bootstrap["realm_id"])
        yield SimpleNamespace(
            service=service, owner=owner, worker=worker, bootstrap=bootstrap,
            runtime_env=runtime_env, support=support, profile=profile,
            resource_locks=preflight,
        )
    finally:
        from astrid.sdk.upgrade import stop_pack_host

        if bootstrap is not None:
            stop_pack_host(support)
            completed = subprocess.run(
                [str(runtime_cli), "down", "--profile", "astrid", "--data-root", str(support), "--json"],
                env=runtime_env, cwd=tmp_path, check=False, text=True, capture_output=True,
            )
            assert completed.returncode == 0, completed.stdout + completed.stderr
            assert _d13_resource_locks(profile, inputs) == preflight


def test_d13_installed_native_preflight(tmp_path):
    """Check the exact installed tuple without opening Runtime or native tools."""
    profile, result = _d13_installed_profile(tmp_path)
    assert "failures" not in result, result["failures"]
    assert profile["profile"] == "astrid"
    assert result["source_digest"]


def _offline_d13_inputs(tmp_path: Path) -> tuple[_D13Inputs, dict[str, str]]:
    source = tmp_path / "site-packages"
    (source / "astrid" / "packs").mkdir(parents=True)
    (source / "astrid" / "packs" / "marker.txt").write_text("selected", encoding="utf-8")
    environment = tmp_path / "venv"
    (environment / "bin").mkdir(parents=True)
    remotion = tmp_path / "remotion"
    (remotion / "public" / "fonts").mkdir(parents=True)
    (remotion / "package-lock.json").write_text("lock", encoding="utf-8")
    browser = tmp_path / "browser"
    node = tmp_path / "node"
    ffmpeg = tmp_path / "ffmpeg"
    ffprobe = tmp_path / "ffprobe"
    for path, content in ((browser, "browser"), (node, "node"), (ffmpeg, "ffmpeg"), (ffprobe, "ffprobe")):
        path.write_text(content, encoding="utf-8")
    node_modules = remotion / "node_modules"
    node_modules.mkdir()
    wheel = tmp_path / "astrid-0.1.0-py3-none-any.whl"
    marker = b"selected"
    record_name = "astrid-0.1.0.dist-info/RECORD"
    marker_digest = _d13_record_digest(marker, "sha256")
    record = (
        f"astrid/packs/marker.txt,sha256={marker_digest},{len(marker)}\n"
        f"{record_name},,\n"
    ).encode()
    (source / record_name).parent.mkdir(parents=True)
    (source / record_name).write_bytes(record)
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("astrid/packs/marker.txt", marker)
        archive.writestr(record_name, record)
    inputs = _D13Inputs(
        profile=tmp_path / "astrid.json", bindings=tmp_path / "bindings.json",
        expected_source=source, expected_environment=environment,
        agent_commit="1" * 40, agent_tree="2" * 40,
        agent_wheel=wheel, agent_wheel_sha256=_sha256_file(wheel),
        source_digest=source_checkout_digest(source), node=node,
        node_sha256=_sha256_file(node), remotion=remotion,
        remotion_lock_sha256=_sha256_file(remotion / "package-lock.json"),
        browser=browser, browser_sha256=_sha256_file(browser),
        ffmpeg=ffmpeg, ffmpeg_sha256=_sha256_file(ffmpeg),
        ffprobe=ffprobe, ffprobe_sha256=_sha256_file(ffprobe),
        node_modules=node_modules,
        source_repo=None,
    )
    profile = {
        "profile": "astrid", "runtime_checkout": str(source),
        "source_checkout": str(source), "runtime_environment": str(environment),
    }
    return inputs, profile


def test_d13_explicit_bindings_accept_exact_offline_positive(tmp_path):
    inputs, profile = _offline_d13_inputs(tmp_path)
    assert _d13_binding_failures(profile, inputs) == []


def _d13_wheel_with_record_member(wheel: Path, destination: Path, record_name: str) -> Path:
    with zipfile.ZipFile(wheel) as source, zipfile.ZipFile(destination, "w") as archive:
        for name in source.namelist():
            data = source.read(name)
            if name.endswith(".dist-info/RECORD"):
                data = data.replace(name.encode(), record_name.encode())
                name = record_name
            archive.writestr(name, data)
    return destination


@pytest.mark.parametrize(
    "record_name",
    [
        "/tmp/astrid-0.1.0.dist-info/RECORD",
        "../astrid-0.1.0.dist-info/RECORD",
    ],
)
def test_d13_record_member_rejects_absolute_and_parent_paths(tmp_path, record_name):
    inputs, _ = _offline_d13_inputs(tmp_path)
    wheel = _d13_wheel_with_record_member(
        inputs.agent_wheel, tmp_path / "unsafe.whl", record_name
    )
    failures = _d13_wheel_record_failures(wheel, inputs.expected_source)
    assert any("Agent wheel RECORD contains an unsafe path" in failure for failure in failures)


def test_d13_record_member_rejects_symlinked_component(tmp_path):
    inputs, _ = _offline_d13_inputs(tmp_path)
    alias = inputs.expected_source / "record-alias"
    alias.symlink_to(inputs.expected_source, target_is_directory=True)
    wheel = _d13_wheel_with_record_member(
        inputs.agent_wheel, tmp_path / "symlinked.whl", "record-alias/astrid-0.1.0.dist-info/RECORD"
    )
    failures = _d13_wheel_record_failures(wheel, inputs.expected_source)
    assert any("installed Agent RECORD is missing or symlinked" in failure for failure in failures)


def test_d13_record_member_accepts_normal_dist_info_path(tmp_path):
    inputs, _ = _offline_d13_inputs(tmp_path)
    assert _d13_wheel_record_failures(inputs.agent_wheel, inputs.expected_source) == []


def test_d13_explicit_bindings_reject_wrong_source_profile(tmp_path):
    inputs, profile = _offline_d13_inputs(tmp_path)
    wrong = tmp_path / "wrong-site-packages"
    (wrong / "astrid" / "packs").mkdir(parents=True)
    profile["source_checkout"] = str(wrong)
    profile["runtime_checkout"] = str(wrong)
    failures = _d13_binding_failures(profile, inputs)
    assert any("selected source differs" in failure for failure in failures)
    assert any("selected source digest differs" in failure for failure in failures)


def test_d13_explicit_bindings_reject_substituted_tool(tmp_path):
    inputs, profile = _offline_d13_inputs(tmp_path)
    substitute = tmp_path / "substitute-ffmpeg"
    substitute.write_text("foreign tool", encoding="utf-8")
    substituted = replace(inputs, ffmpeg=substitute)
    failures = _d13_binding_failures(profile, substituted)
    assert any("ffmpeg differs from manager-selected tool binding" in failure for failure in failures)


def test_d13_explicit_bindings_reject_installed_package_tamper(tmp_path):
    inputs, profile = _offline_d13_inputs(tmp_path)
    installed = inputs.expected_source / "astrid" / "packs" / "marker.txt"
    installed.write_text("tampered", encoding="utf-8")
    failures = _d13_binding_failures(profile, inputs)
    assert any("installed Agent RECORD hash differs" in failure for failure in failures)
    assert any("installed Agent package bytes differ from wheel" in failure for failure in failures)


def _digest(value: str | bytes) -> str:
    raw = value.encode() if isinstance(value, str) else value
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _claim_child(service, capability: str, *, key: str) -> dict:
    epoch = service.health()["runtime_epoch"]
    claim = service.claim_next(
        {"executor_id": "chain-worker", "capability_ids": [capability], "runtime_epoch": epoch},
        idempotency_key=key,
    )
    row = service.store.conn.execute("SELECT * FROM attempts WHERE task_id=?", (claim["task_id"],)).fetchone()
    return {"task_id": claim["task_id"], "attempt_id": row["id"], "lease_id": row["lease_id"], "fence": row["fence"], "runtime_epoch": row["runtime_epoch"]}


def _settle_child(
    service, attempt: dict, *, index: int, value: bytes, digest: str, project_id: str
) -> dict:
    service.settle_attempt(
        attempt["attempt_id"],
        {
            "attempt_id": attempt["attempt_id"],
            "lease_id": attempt["lease_id"],
            "fence": attempt["fence"],
            "runtime_epoch": attempt["runtime_epoch"],
            "outputs": [{
                "digest": digest,
                "data_base64": base64.b64encode(value).decode(),
                "kind": "object",
                "media_type": "video/mp4",
                "size": len(value),
                "name": f"child-{index}",
                "output_port": "video",
                "group_key": "main",
                "variant_key": "primary",
                "ordinal": 0,
                "role": "primary",
                "is_primary": True,
                "duration_seconds": 1.0,
            }],
            "effect": _media_effect(project_id),
        },
        idempotency_key=f"chain-settle-{index}",
    )
    return {"task_id": attempt["task_id"], "digest": digest}


def _media_effect(project_id: str) -> dict:
    return {
        "effect_type": "generation.publish_v1",
        "target_id": project_id,
        "payload": {
            "version": 1,
            "modality": "video",
            "generation_type": "multi_output_render",
            "metadata": {"params": {"duration_seconds": 1.0, "width": 64, "height": 36}},
            "partial_success_policy": "reject",
            "groups": [{"group_key": "main", "selectors": [{
                "selector": "primary", "ordinal": 0, "variant_key": "primary", "output_port": "video",
            }]}],
        },
    }


@pytest.mark.integration
def test_daemon_backed_authoring_proposal_publishes_one_canonical_render_task(tmp_path, real_runtime):
    """Exercise child settlement -> authoring -> fenced publication -> render admission."""
    from tests.helpers.runtime import vendored_runtime_commit

    daemon = real_runtime(commit=vendored_runtime_commit(), authoring_profile="astrid")
    service = daemon.service
    try:
        project = service.create_project({"slug": "chain", "name": "Chain"})
        child_cap, author_cap, render_cap = "video.child", "rendering.assemble_timeline", "rendering.render"
        digests = {cap: _digest(cap) for cap in (child_cap, author_cap, render_cap)}
        for capability, digest in digests.items():
            service.register_capability({"capability_id": capability, "definition_digest": digest, "status": "ready"})
        service.register_executor(
            {"executor_id": "chain-worker", "capabilities": [child_cap, author_cap, render_cap], "max_concurrency": 4},
            idempotency_key="chain-register",
        )
        service.create_timeline(project["id"], "main", name="Main", idempotency_key="chain-timeline")
        child_ids = []
        for index in range(2):
            task = service.create_task({
                "project": project["id"], "capability_id": child_cap, "capability_digest": digests[child_cap],
                "input_object_ids": [], "spec": {"index": index},
                "settlement_effect": _media_effect(project["id"]),
                "idempotency_key": f"chain-child-{index}",
            })
            child_ids.append(task["task"]["id"])
        outputs = [_digest(b"child-zero"), _digest(b"child-one")]
        claims = [_claim_child(service, child_cap, key=f"chain-claim-child-{index}") for index in range(2)]
        assert {claim["task_id"] for claim in claims} == set(child_ids)
        by_task = {claim["task_id"]: claim for claim in claims}
        # Deliberately settle ordinal 1 before ordinal 0; claim order is
        # runtime-owned and may differ from admission order.
        _settle_child(
            service, by_task[child_ids[1]], index=1, value=b"child-one", digest=outputs[1],
            project_id=project["id"],
        )
        _settle_child(
            service, by_task[child_ids[0]], index=0, value=b"child-zero", digest=outputs[0],
            project_id=project["id"],
        )

        client = AstridClient.open(
            endpoint=daemon.endpoint,
            credential=daemon.credential_path,
            realm_id=service.realm["id"], actor_id="owner", client_name="astrid-chain-test",
            client_version="stage1", protocol_version="workspace.v1",
        )
        session = TimelineSession.open(
            project["id"], "main", client=client,
            outbox_path=tmp_path / "chain-outbox.json",
        )
        selectors = []
        for child_id in child_ids:
            association = next(
                row for row in service.managed_outputs(child_id)
                if row["output_port"] == "video"
            )
            selectors.append({
                "association_id": association["association_id"],
                "object_id": association["object_id"],
                "version": association["lifecycle"]["version"],
            })
        admission = session.admit_assembly(
            selectors, replace_clips=[], assembler_digest=digests[author_cap],
            idempotency_key="chain-author",
        )
        author = admission["data"]["task_id"]

        author_claim = service.claim_next(
            {"executor_id": "chain-worker", "capability_ids": [author_cap], "runtime_epoch": service.health()["runtime_epoch"]},
            idempotency_key="chain-claim-author",
        )
        author_row = service.store.conn.execute("SELECT * FROM attempts WHERE task_id=?", (author_claim["task_id"],)).fetchone()
        runtime_attempt = {
            "attempt_id": author_row["id"], "lease_id": author_row["lease_id"],
            "fence": author_row["fence"], "runtime_epoch": author_row["runtime_epoch"],
        }
        author_task = service.task(author_claim["task_id"])["task"]
        admitted_spec = author_task["spec"]["spec"]
        capture = admitted_spec["inputs"]["resolved_children"]
        dependencies = admitted_spec["runtime_dependencies"]
        payload = {
            "family": admitted_spec["family"],
            "continuation_id": author_claim["task_id"],
            "resolved_children": dependencies["resolved_children"],
            "edges": dependencies["edges"],
            "aggregation": dependencies["aggregation"],
            "capture": capture,
            "associations": [
                service.managed_output(child["association"]["association_id"])
                for child in capture["children"]
            ],
            "producer": {
                "task_id": author_claim["task_id"], "attempt_id": runtime_attempt["attempt_id"],
                "executor_id": author_row["executor_id"], "fence": runtime_attempt["fence"],
                "runtime_epoch": runtime_attempt["runtime_epoch"],
            },
        }
        # Reproduce the registered assembler's exact source seam: the claimed
        # Runtime envelope becomes a native producer proposal before settlement.
        proposal = build_native_proposal(payload, build_authoring_proposal(payload))
        raw_proposal = canonical_json(proposal).encode()
        service.settle_attempt(
            author_claim["attempt_id"],
            {
                "attempt_id": author_claim["attempt_id"], "lease_id": author_claim["lease_id"],
                "fence": author_claim["fence"], "runtime_epoch": author_claim["runtime_epoch"],
                "outputs": [{
                    "digest": _digest(raw_proposal), "data_base64": base64.b64encode(raw_proposal).decode(),
                    "kind": "object", "size": len(raw_proposal),
                    "media_type": "application/json", "name": "authoring_proposal",
                    "output_port": "authoring_proposal", "group_key": "default",
                    "variant_key": "0", "ordinal": 0, "role": "output",
                }],
            },
            idempotency_key="chain-settle-author",
        )
        settled = read_assembly_proposal(session, author, attempt_id=author_claim["attempt_id"])
        assert settled == proposal
        result = publish_authoring_proposal(
            client, proposal=proposal, timeline_id="main", expected_version=1,
            runtime_attempt=runtime_attempt, render_capability_digest=digests[render_cap],
            output_name="chain.mp4", requester_session=session, idempotency_key="chain-publish",
        )
        accepted = result["data"]
        assert accepted["status"] in {"accepted", "merged", "rebased"}
        assert accepted["accepted_revision_id"]
        assert accepted["accepted_publication_id"] == result["receipt"]["receipt_id"]
        assert service.store.conn.execute("SELECT COUNT(*) FROM tasks WHERE capability=?", (render_cap,)).fetchone()[0] == 1
        render = service.task(result["render_task_id"])["task"]
        render_params = render["spec"]["spec"]["params"]
        assert render_params["timeline_ref"] == "main"
        assert render_params["canonical_revision"]["revision_id"] == accepted["accepted_revision_id"]
        assert render_params["canonical_revision"]["publication_id"] == accepted["accepted_publication_id"]
        # The checkpoint admits the ordinary public renderer; the next worker
        # claim therefore sees rendering.render without a stitch-specific task.
        render_claim = service.claim_next(
            {"executor_id": "chain-worker", "capability_ids": [render_cap], "runtime_epoch": service.health()["runtime_epoch"]},
            idempotency_key="chain-claim-render",
        )
        assert render_claim["task_id"] == result["render_task_id"]
    finally:
        daemon.stop()


def test_requester_commit_publishes_without_local_save_or_invoke(tmp_path):
    """The surviving requester commit path has one Runtime publication route."""
    session, generated, proposal, renders, _ = requester_commit_fixture(tmp_path)

    result = publish_requester_commit(
        session, proposal, runtime_attempt=proposal["producer"],
    )

    assert result["render_task_id"] == "render-1"
    assert len(generated.calls) == 1
    body = json.loads(generated.calls[0][0])
    assert (body["project_id"], body["timeline_id"]) == (
        proposal["admitted_target"]["project_id"], proposal["admitted_target"]["timeline_id"],
    )
    pin = body["commands"][0]["args"]["pin"]
    assert pin["attempt_id"] == proposal["producer"]["attempt_id"]
    assert pin["fence"] == proposal["producer"]["fence"]
    assert body["base_revision_id"] == proposal["admitted_target"]["base_revision_id"]
    assert body["base_publication_id"] == proposal["admitted_target"]["base_publication_id"]
    assert len(renders) == 1
    assert renders[0]["capability"] == "rendering.render"
    assert "save" not in repr(generated.calls) and "invoke" not in repr(generated.calls)


def test_legacy_assembler_admission_refuses_before_execution_and_keeps_artifacts(tmp_path):
    """Unsafe legacy admission is visible, terminal, and leaves its artifact untouched."""
    proposal_path = tmp_path / "attempt" / "outputs" / "authoring-proposal.json"
    proposal_path.parent.mkdir(parents=True)
    original = json.dumps({
        "timeline": {"tracks": [], "clips": []}, "registry": {"assets": {}},
        "publication": {"authority": "workspace_runtime", "render_capability": "rendering.render"},
    }).encode()
    proposal_path.write_bytes(original)

    class Runtime:
        def __init__(self):
            self.calls = []

        def fail(self, *args, **kwargs):
            self.calls.append((args, kwargs))

        def heartbeat(self, *args, **kwargs):
            self.calls.append((args, kwargs))

        def task(self, *args, **kwargs):
            self.calls.append((args, kwargs))

        def settle(self, *args, **kwargs):
            self.calls.append((args, kwargs))

        def upload_object(self, *args, **kwargs):
            self.calls.append((args, kwargs))

    runtime = Runtime()
    host = GenericPackHost(pack_roots=[], client=runtime, attempt_root=tmp_path / "host-attempt")
    host.capabilities = {
        "rendering.assemble_timeline": SimpleNamespace(
            ready=True, definition=SimpleNamespace(metadata={}), matrix={},
        )
    }
    host.preflight = lambda _capability: None
    host._registration_admission_reason = lambda: None
    task = {
        "id": "author-1", "capability": "rendering.assemble_timeline",
        "spec": {"runtime_dependencies": {"publication": {"timeline_id": "main"}}},
        "input_object_ids": [], "project_id": "project-1", "attempt_id": "attempt-1",
        "fence": 1, "runtime_epoch": 1,
    }
    try:
        with pytest.raises(HostError, match="immutable original target"):
            host.run_task(task, lease_token="lease-1", attempt_id="attempt-1", fence=1)
    finally:
        host.shutdown()

    assert proposal_path.read_bytes() == original
    assert len(runtime.calls) == 1
    args, _kwargs = runtime.calls[0]
    assert args[:2] == ("author-1", "lease-1")
    assert not any("publish_timeline_render" in repr(call) for call in runtime.calls)


def test_commit_render_publication_preserves_runtime_authority_without_local_save_or_invoke(tmp_path):
    """The requester-owned commit route has one Runtime publication path."""
    session, generated, proposal, renders, _ = requester_commit_fixture(tmp_path)
    result = publish_authoring_proposal(
        session.client,
        proposal=proposal,
        timeline_id="t",
        expected_version=1,
        runtime_attempt=proposal["producer"],
        render_capability_digest=_digest("rendering.render"),
        output_name="chain.mp4",
        requester_session=session,
        idempotency_key="publish",
    )
    assert result["render_task_id"] == "render-1"
    assert len(generated.calls) == 1
    assert len(renders) == 1
    body = json.loads(generated.calls[0][0])
    assert (body["project_id"], body["timeline_id"]) == (
        proposal["admitted_target"]["project_id"], proposal["admitted_target"]["timeline_id"],
    )
    pin = body["commands"][0]["args"]["pin"]
    assert pin["attempt_id"] == proposal["producer"]["attempt_id"]
    assert pin["fence"] == proposal["producer"]["fence"]
    assert body["base_revision_id"] == proposal["admitted_target"]["base_revision_id"]
    assert body["base_publication_id"] == proposal["admitted_target"]["base_publication_id"]
    assert renders[0]["capability"] == "rendering.render"
    assert renders[0]["spec"]["params"]["canonical_revision"]["timeline_id"] == "t"
    assert renders[0]["spec"]["params"]["canonical_revision"]["revision_id"] == result["data"]["accepted_revision_id"]
    assert "save" not in repr(generated.calls) and "invoke" not in repr(generated.calls)


# D66 restore of upstream-924 selector 6635, migrated to the requester-owned
# v2 commit route after the generic-host publication hook was retired.
def test_d66_upstream_generic_host_hook_materializes_and_publishes_without_local_save_or_invoke(tmp_path):
    """The settled proposal has one requester-owned Runtime publication route."""
    session, generated, proposal, renders, _ = requester_commit_fixture(tmp_path)
    result = publish_authoring_proposal(
        session.client,
        proposal=proposal,
        timeline_id="t",
        expected_version=1,
        runtime_attempt=proposal["producer"],
        render_capability_digest=_digest("rendering.render"),
        output_name="chain.mp4",
        requester_session=session,
        idempotency_key="publish",
    )
    assert result["render_task_id"] == "render-1"
    assert len(generated.calls) == 1
    assert len(renders) == 1
    body = json.loads(generated.calls[0][0])
    assert body["project_id"] == proposal["admitted_target"]["project_id"]
    assert body["timeline_id"] == proposal["admitted_target"]["timeline_id"]
    assert body["base_revision_id"] == proposal["admitted_target"]["base_revision_id"]
    assert body["base_publication_id"] == proposal["admitted_target"]["base_publication_id"]
    assert body["commands"][0]["op"] == "producer.guard"
    assert body["commands"][0]["args"]["pin"]["attempt_id"] == proposal["producer"]["attempt_id"]
    assert body["commands"][0]["args"]["pin"]["fence"] == proposal["producer"]["fence"]
    assert renders[0]["capability"] == "rendering.render"
    assert renders[0]["spec"]["params"]["output_name"] == "chain.mp4"
    assert "save" not in repr(generated.calls) and "invoke" not in repr(generated.calls)


@pytest.mark.integration
def test_daemon_backed_host_executes_authoring_and_final_render(tmp_path, monkeypatch, installed_runtime):
    """Run registered native assembly through requester, host, Runtime, and render."""
    bootstrap = installed_runtime.bootstrap
    service = installed_runtime.service
    source = Path(installed_runtime.profile["source_checkout"]).resolve()
    allowed = set(installed_runtime.runtime_env)
    for name, value in installed_runtime.runtime_env.items():
        monkeypatch.setenv(name, value)
    for name in tuple(os.environ):
        if name.startswith("ASTRID_") and name not in allowed:
            monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("PYTHONPATH", raising=False)
    from astrid.sdk.host_bootstrap import ensure_pack_host

    pack_roots = (source / "astrid/packs/rendering",)
    client = None
    host = GenericPackHost(
        pack_roots=pack_roots,
        client=installed_runtime.worker,
        executor_id="astrid-pack-host",
        attempt_root=tmp_path / "attempt",
    )
    try:
        host.discover()
        assemble_digest = host.capabilities["rendering.assemble_timeline"].capability_digest
        ensure_pack_host(
            bootstrap,
            reconfigure_action="restart the D13 installed pack host",
            startup_idle_seconds=30,
        )
        child_capability = "video.child"
        child_digest = _digest(child_capability)
        project = service.create_project({"slug": "host-chain", "name": "Host chain"})
        service.register_capability(
            {"capability_id": child_capability, "definition_digest": child_digest, "status": "ready"}
        )
        service.register_executor(
            {
                "executor_id": "chain-worker",
                "capabilities": [child_capability],
                "max_concurrency": 2,
            },
            idempotency_key="host-chain-register-child",
        )
        client = installed_runtime.owner
        service.create_timeline(project["id"], "main", name="Main", idempotency_key="host-chain-timeline")
        blobs = []
        for index, colour in enumerate(("blue", "red")):
            path = tmp_path / f"child-{index}.mp4"
            subprocess.run(
                [
                    installed_runtime.runtime_env["ASTRID_FFMPEG_EXECUTABLE"],
                    "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                    "-i", f"color=c={colour}:s=64x36:r=30:d=1", "-an", "-c:v", "libx264",
                    "-pix_fmt", "yuv420p", str(path),
                ],
                check=True,
                capture_output=True,
            )
            blobs.append(path.read_bytes())
        child_ids = []
        for index in range(2):
            child_ids.append(
                service.create_task(
                    {
                        "project": project["id"],
                        "capability_id": child_capability,
                        "capability_digest": child_digest,
                        "input_object_ids": [],
                        "spec": {"index": index},
                        "settlement_effect": _media_effect(project["id"]),
                        "idempotency_key": f"host-chain-child-{index}",
                    }
                )["task"]["id"]
            )
        claims = []
        for index in range(2):
            claim = service.claim_next(
                {
                    "executor_id": "chain-worker",
                    "capability_ids": [child_capability],
                    "runtime_epoch": service.health()["runtime_epoch"],
                },
                idempotency_key=f"host-chain-claim-child-{index}",
            )
            claims.append(claim)
        by_task = {claim["task_id"]: claim for claim in claims}
        assert set(by_task) == set(child_ids)
        # Finish ordinal 1 first; Runtime still resolves the declared order.
        for index in (1, 0):
            claim = by_task[child_ids[index]]
            service.settle_attempt(
                claim["attempt_id"],
                {
                    "attempt_id": claim["attempt_id"],
                    "lease_id": claim["lease_id"],
                    "fence": claim["fence"],
                    "runtime_epoch": claim["runtime_epoch"],
                    "outputs": [{
                        "digest": _digest(blobs[index]),
                        "data_base64": base64.b64encode(blobs[index]).decode(),
                        "media_type": "video/mp4",
                        "name": f"child-{index}",
                        "output_port": "video",
                        "group_key": "main",
                        "variant_key": "primary",
                        "ordinal": 0,
                        "role": "primary",
                        "is_primary": True,
                        "duration_seconds": 1.0,
                    }],
                    "effect": _media_effect(project["id"]),
                },
                idempotency_key=f"host-chain-settle-child-{index}",
            )
        selectors = []
        for child_id in child_ids:
            associations = service.managed_outputs(child_id)
            association = next(row for row in associations if row["output_port"] == "video")
            selectors.append({
                "association_id": association["association_id"],
                "object_id": association["object_id"],
                "version": association["lifecycle"]["version"],
            })
        session = TimelineSession.open(
            project["id"], "main", client=client, outbox_path=tmp_path / "host-chain-outbox.json",
        )
        admission = session.admit_assembly(
            selectors, replace_clips=[], assembler_digest=assemble_digest,
            idempotency_key="host-chain-author",
        )
        author = admission["data"]["task_id"]
        admitted_task = service.task(author)["task"]
        admitted_spec = admitted_task["spec"]["spec"]
        admitted_capture = admitted_spec["inputs"]["resolved_children"]
        admitted_target = admitted_capture["admitted_target"]
        assert admitted_task["capability"] == "rendering.assemble_timeline"
        assert admitted_spec["runtime_dependencies"]["publication"]["admitted_target"] == admitted_target
        assert admitted_capture["replace_clips"] == []
        assert [child["association"] for child in admitted_capture["children"]] == selectors
        assert [child["media"]["object_id"] for child in admitted_capture["children"]] == [
            selector["object_id"] for selector in selectors
        ]
        assert admitted_spec["runtime_dependencies"]["aggregation"]["order"] == [
            selector["object_id"] for selector in selectors
        ]
        assert admitted_spec["outputs"]["authoring_proposal"] == {
            "type": "file", "artifact_type": "timeline/authoring-proposal",
        }
        author_claim = None
        for _ in range(600):
            author_task = service.task(author)["task"]
            if author_task.get("state") == "succeeded":
                author_claim = {"attempt_id": author_task["attempt_id"]}
                break
            if author_task.get("state") in {"failed", "cancelled"}:
                raise AssertionError(f"installed pack host did not settle authoring task: {author_task}")
            time.sleep(0.1)
        assert author_claim is not None, "installed pack host did not settle authoring task"
        author_result = service.task(author)["task"]["result"]
        assert {item["name"] for item in author_result["outputs"]} >= {"authoring_proposal", "timeline", "assets"}
        settled = read_assembly_proposal(session, author, attempt_id=author_claim["attempt_id"])
        assert [command["op"] for command in settled["commands"][:3]] == [
            "timeline.assembled-target", "track.create", "clip.add-media",
        ]
        assert [command["args"]["media"]["object_id"] for command in settled["commands"] if command["op"] == "clip.add-media"] == [
            selector["object_id"] for selector in selectors
        ]
        assert settled["admitted_target"] == admitted_target
        assert settled["commands"][0]["args"]["replace_clips"] == admitted_capture["replace_clips"]
        session.queue_assembly(author, attempt_id=author_claim["attempt_id"])
        pending = json.loads((tmp_path / "host-chain-outbox.json").read_text())
        assert pending["pending"][0]["op"] == "producer.guard"
        original_commit = client.timelines.commit

        def lose_ack(*args, **kwargs):
            original_commit(*args, **kwargs)
            raise WorkspaceClientError(0, "transport_error", "lost commit acknowledgement")

        client.timelines.commit = lose_ack
        with pytest.raises(WorkspaceClientError, match="lost commit acknowledgement"):
            session.flush(idempotency_key="host-chain-commit")
        saved_flight = json.loads((tmp_path / "host-chain-outbox.json").read_text())["flight"]
        assert saved_flight["key"] == "host-chain-commit"
        client.timelines.commit = original_commit
        restarted = TimelineSession.open(
            project["id"], "main", client=client, outbox_path=tmp_path / "host-chain-outbox.json",
        )
        committed = restarted.flush(idempotency_key="host-chain-commit")
        accepted_revision = committed["data"]["accepted_revision_id"]
        accepted_publication = committed["data"]["accepted_publication_id"]
        assert accepted_revision and accepted_publication
        assert json.loads((tmp_path / "host-chain-outbox.json").read_text())["flight"] is None

        scope = client.timelines.resolve_scope(project["id"], "main")
        assert scope.ok and isinstance(scope.data.get("version"), int)
        render_admission = client.invoke_result(
            "rendering.render", kind="executor", project=project["id"],
            inputs={"timeline_ref": "main", "expected_version": scope.data["version"], "output_name": "host-chain.mp4"},
            wait=False,
        )
        assert render_admission.ok
        render_task_id = render_admission.kernel_task_id
        assert isinstance(render_task_id, str) and render_task_id
        assert service.count_tasks(project["id"], "rendering.render") == 1
        for _ in range(1200):
            render_task = service.task(render_task_id)["task"]
            if render_task.get("state") == "succeeded":
                break
            if render_task.get("state") in {"failed", "cancelled"}:
                raise AssertionError(f"installed pack host did not settle render task: {render_task}")
            time.sleep(0.1)
        else:
            raise AssertionError("installed pack host did not settle render task")
        render_result = service.task(render_task_id)["task"]["result"]
        assert {item["name"] for item in render_result["outputs"]} >= {"video", "provenance"}
        video = next(item for item in render_result["outputs"] if item["name"] == "video")
        _, video_bytes = service.object(video["digest"])
        assert len(video_bytes) > 100
        probe = subprocess.run(
            [installed_runtime.runtime_env["ASTRID_FFPROBE_EXECUTABLE"], "-v", "error",
             "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", "-"],
            input=video_bytes,
            capture_output=True,
        )
        assert probe.returncode == 0
        assert float(probe.stdout.strip()) > 0
        render_task = service.task(render_task_id)["task"]
        render_inputs = render_task["spec"]["spec"]["inputs"]
        assert render_inputs["timeline_authority"]["config_version"] == scope.data["version"]
        assert render_inputs["timeline_authority"]["parent_revision_id"] == accepted_revision
        undone = restarted.undo([accepted_publication], idempotency_key="host-chain-undo")
        assert undone["data"]["accepted_revision_id"]
    finally:
        host.shutdown()


# Parent coverage retained during the Release-1 + Timeline merge.
def test_generic_host_hook_materializes_and_publishes_without_local_save_or_invoke(tmp_path):
    """The registered authoring host hook has one Runtime publication route."""
    proposal_path = tmp_path / "attempt" / "outputs" / "authoring-proposal.json"
    proposal_path.parent.mkdir(parents=True)
    proposal_path.write_text(json.dumps({
        "timeline": {"tracks": [], "clips": []}, "registry": {"assets": {}},
        "publication": {"authority": "workspace_runtime", "render_capability": "rendering.render"},
    }))

    class Runtime:
        def __init__(self): self.calls = []
        def publish_timeline_render(self, *args, **kwargs):
            self.calls.append((args, kwargs))
            return {"timeline_version": 2, "render_task_id": "render-1"}

    runtime = Runtime()
    host = GenericPackHost.__new__(GenericPackHost)
    host.client = runtime
    host.capabilities = {}
    result = host._publish_assembled_timeline(
        task_data={"id": "author-1", "spec": {"family": "stitch_finalization", "runtime_dependencies": {
            "publication": {"timeline_id": "main", "expected_version": 1, "render": {
                "capability_id": "rendering.render", "capability_digest": _digest("rendering.render"),
                "spec": {"inputs": {"selector": "rendering.ffmpeg"}},
            }},
        }}},
        attempt_id="attempt-1", fence=1, lease_token="lease-1",
        outputs=[{"path": str(proposal_path), "artifact_type": "timeline/authoring-proposal"}],
        attempt_root=proposal_path.parents[1],
    )
    assert result["render_task_id"] == "render-1"
    assert len(runtime.calls) == 1
    args, kwargs = runtime.calls[0]
    assert args[:2] == ("attempt-1", "lease-1")
    assert kwargs["timeline_id"] == "main"
    assert kwargs["expected_version"] == 1
    assert "save" not in repr(runtime.calls) and "invoke" not in repr(runtime.calls)
